from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer, QEvent, QCoreApplication
from PySide6.QtGui import QKeySequence, QShortcut, QCursor
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QApplication,
    QHBoxLayout,
    QVBoxLayout,
    QFileDialog,
    QMessageBox,
)

from core.model import Scene
from core.selection import SelectionState
from core.animation import apply_interpolation
from core.commands import AddObjectCommand
from core.history import History
from core.save import (
    save_scene_to_file,
    load_scene_from_file,
    export_symbol_to_file,
    import_symbol_from_file,
)
from rendering.svg import import_svg_simple
from ui.stage import StageWidget
from ui.timeline import TimelineWidget, TimelineTransport
from ui.outliner import OutlinerWidget
from ui.properties import PropertiesWidget
from ui.docks import DockManager, DockWidget
from ui.area import AreaNode, AreaWidget
from ui.render import show_render_window
from ui.menus import StripeMenuBar


def _deep_copy_tree(node: AreaNode) -> AreaNode:
    """Return an independent copy of an AreaNode tree."""
    if node.is_leaf:
        return AreaNode.leaf(node.dock_id)
    return AreaNode.split(
        node.orientation,
        node.ratio,
        _deep_copy_tree(node.children[0]),
        _deep_copy_tree(node.children[1]),
    )


class MainWindow(QMainWindow):
    def __init__(self, scene: Scene):
        super().__init__()
        self.scene = scene
        self.history = History(scene)
        self._current_file_path: str | None = None

        self.setWindowTitle("funny animation software (FAS)")
        self.resize(1200, 800)
        self.setMinimumSize(800, 500)

        self._playing = False
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._advance_frame)
        self._render_windows: dict[str, object] = {}

        # Debounces playback-style scrubbing: while the playhead is being
        # dragged the outliner thumbnails stay frozen; once the user stops,
        # this timer unfreezes and rebuilds them (200 ms of quiet).
        self._thumb_unfreeze_timer = QTimer(self)
        self._thumb_unfreeze_timer.setSingleShot(True)
        self._thumb_unfreeze_timer.setInterval(200)
        self._thumb_unfreeze_timer.timeout.connect(self._on_playhead_quiet)

        # The dock under the cursor always keeps focus: a low-frequency timer
        # re-asserts that invariant so transient widget interactions (splitter
        # handles, area corners, cursor wrapping, repaints, etc.) can never
        # silently steal focus away from the active dock.
        self._focus_timer = QTimer(self)
        self._focus_timer.setInterval(30)
        self._focus_timer.timeout.connect(self._focus_dock_under_cursor)
        self._focus_timer.start()

        self._setup_ui()
        self._connect_signals()

        self._space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self._space_shortcut.setContext(Qt.ApplicationShortcut)
        self._space_shortcut.activated.connect(self._toggle_playback)

        # Debug: press Ctrl+D to print the current layout tree to stderr.
        self._dump_shortcut = QShortcut(QKeySequence(Qt.CTRL | Qt.Key_D), self)
        self._dump_shortcut.setContext(Qt.ApplicationShortcut)
        self._dump_shortcut.activated.connect(self._dump_area_layout)

        # Emergency: press Ctrl+R to reset the layout to the default 3 docks.
        self._reset_shortcut = QShortcut(QKeySequence(Qt.CTRL | Qt.Key_R), self)
        self._reset_shortcut.setContext(Qt.ApplicationShortcut)
        self._reset_shortcut.activated.connect(self._reset_area_layout)

        QCoreApplication.instance().installEventFilter(self)
        apply_interpolation(self.scene, self.scene.current_frame)
        self._update_all()

    def _setup_menus(self):
        file_menu = self._menubar.add_menu("&File")

        open_action = file_menu.add_action("&Open...", callback=lambda checked: self._open(), shortcut_text="Ctrl+O",
                                           description="Open a saved project file")

        file_menu.add_separator()

        file_menu.add_action("&Save", callback=lambda checked: self._save(), shortcut_text="Ctrl+S",
                             description="Save the project to its current file")
        file_menu.add_action("Save &As...", callback=lambda checked: self._save_as(), shortcut_text="Ctrl+Shift+S",
                             description="Save the project under a new file")
        file_menu.add_action("Save Copy...", callback=lambda checked: self._save_copy(), shortcut_text="Ctrl+Alt+S",
                             description="Save a copy of the project, keeping the current file")

        file_menu.add_separator()

        file_menu.add_action("&Import SVG...", callback=lambda checked: self._import_svg(),
                             description="Import vector artwork from an SVG file")
        file_menu.add_action("&Import Symbol...", callback=lambda checked: self._import_symbol(),
                             description="Import a reusable symbol into the project")

        file_menu.add_separator()

        file_menu.add_action("&Export Symbol...", callback=lambda checked: self._export_symbol(),
                             description="Export a symbol back to an SVG file")

        file_menu.add_separator()
        file_menu.add_action("E&xit", callback=lambda checked: self.close(), shortcut_text="Ctrl+Q",
                             description="Close the application")

        edit_menu = self._menubar.add_menu("&Edit")

        self.undo_action = edit_menu.add_action("&Undo", callback=lambda checked: self._undo(), shortcut_text="Ctrl+Z",
                                                description="Undo the last change")
        self.redo_action = edit_menu.add_action("&Redo", callback=lambda checked: self._redo(), shortcut_text="Ctrl+Shift+Z",
                                                description="Redo the last undone change")

        render_menu = self._menubar.add_menu("&Render")

        self.render_image_action = render_menu.add_action("&Image", callback=lambda checked: self._render_image(), shortcut_text="Ctrl+F12",
                                                          description="Render the current frame as an image")
        self.render_video_action = render_menu.add_action("&Video", callback=lambda checked: self._render_video(), shortcut_text="F12",
                                                          description="Render the animation as a video file")

    def _render_image(self):
        self._open_render_window("image")

    def _render_video(self):
        self._open_render_window("video")

    def _open_render_window(self, mode: str):
        """Open (or refocus) the render window for `mode`, keeping alive any
        window previously created so Qt doesn't garbage-collect it."""
        existing = self._render_windows.get(mode)
        if existing is not None:
            existing.refresh()
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        window = show_render_window(self, self.scene, mode=mode)
        self._render_windows[mode] = window

    def _import_svg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import SVG", "", "SVG Files (*.svg)"
        )
        if not path:
            return
        try:
            # import_svg_simple returns a single root container (a Symbol)
            # holding the whole SVG. Nested <g> groups are preserved as nested
            # containers, and the container is already positioned so the
            # artwork sits at the centre of the camera.
            symbol = import_svg_simple(path)
            self.scene.objects.append(symbol)
            self.history.push(AddObjectCommand(symbol, self.scene.objects))
            self._update_all()
            self.statusBar().showMessage(
                f"Imported {Path(path).name} as a Symbol"
            )
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Import Error", str(e))

    def _import_symbol(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Symbol", "", "Symbol Files (*.sym)"
        )
        if not path:
            return
        try:
            symbol = import_symbol_from_file(path)
        except (ValueError, OSError, KeyError) as e:
            QMessageBox.warning(self, "Import Error", str(e))
            return
        self.scene.objects.append(symbol)
        self.history.push(AddObjectCommand(symbol, self.scene.objects))
        self._update_all()
        self.statusBar().showMessage(f"Imported {Path(path).name} as a Symbol")

    def _export_symbol(self):
        selected = SelectionState.selected()
        symbols = [s for s in selected if s.is_container]
        if len(symbols) != 1:
            QMessageBox.information(
                self,
                "Export Symbol",
                "Select exactly one Symbol (a container) to export.",
            )
            return
        symbol = symbols[0]
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Symbol", "", "Symbol Files (*.sym)"
        )
        if not path:
            return
        if not path.endswith(".sym"):
            path += ".sym"
        try:
            export_symbol_to_file(symbol, path)
            self.statusBar().showMessage(
                f"Exported {symbol.name} to {Path(path).name}"
            )
        except OSError as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", "FAS Files (*.fas)"
        )
        if not path:
            return
        try:
            scene = load_scene_from_file(path)
        except (ValueError, OSError, KeyError) as e:
            QMessageBox.warning(self, "Open Error", str(e))
            return
        self.scene.objects[:] = scene.objects
        self.scene.start_frame = scene.start_frame
        self.scene.end_frame = scene.end_frame
        self.scene.fps = scene.fps
        self.scene.current_frame = scene.current_frame
        self._current_file_path = path
        self.history = History(self.scene)
        SelectionState.set_selected([])
        SelectionState.set_hovered(None)
        apply_interpolation(self.scene, self.scene.current_frame)
        self._update_all()
        self._update_window_title()
        self.statusBar().showMessage(f"Opened {Path(path).name}")

    def _save(self):
        if self._current_file_path is not None:
            self._save_to(self._current_file_path)
        else:
            self._save_as()

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", "", "FAS Files (*.fas)"
        )
        if not path:
            return
        if not path.endswith(".fas"):
            path += ".fas"
        self._save_to(path)
        self._current_file_path = path
        self._update_window_title()

    def _save_copy(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Copy", "", "FAS Files (*.fas)"
        )
        if not path:
            return
        if not path.endswith(".fas"):
            path += ".fas"
        self._save_to(path)

    def _save_to(self, path: str) -> None:
        try:
            save_scene_to_file(self.scene, path)
            self.statusBar().showMessage(f"Saved to {Path(path).name}")
        except OSError as e:
            QMessageBox.warning(self, "Save Error", str(e))

    def _update_window_title(self):
        if self._current_file_path:
            name = Path(self._current_file_path).stem
            self.setWindowTitle(f"{name} - funny animation software (FAS)")
        else:
            self.setWindowTitle("funny animation software (FAS)")

    def _undo(self):
        if self.history.undo():
            self._after_history_change()
            self.statusBar().showMessage("Undo")

    def _redo(self):
        if self.history.redo():
            self._after_history_change()
            self.statusBar().showMessage("Redo")

    def _after_history_change(self):
        objects = []
        for oid in self.history.selected_ids:
            obj = self.scene.get_object_by_id(oid)
            if obj is not None:
                objects.append(obj)
        SelectionState.set_selected(objects)
        SelectionState.set_hovered(None)
        for stage in self._docks_of(StageWidget):
            stage.hovered_object = None
            stage.update()
        for outliner in self._docks_of(OutlinerWidget):
            outliner._hovered_object = None
            outliner.invalidate_thumbnails()
        self._update_all()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and event.modifiers() & Qt.AltModifier:
            delta = event.angleDelta().x() or event.angleDelta().y() or event.pixelDelta().x() or event.pixelDelta().y()
            if delta == 0:
                return False
            if delta < 0:
                step = 1
            else:
                step = -1
            self._nudge_playhead(step)
            return True
        return super().eventFilter(obj, event)

    def _nudge_playhead(self, direction: int):
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()
        self._stop_playback()
        frame = self.scene.current_frame + direction
        frame = max(self.scene.start_frame, min(self.scene.end_frame, frame))
        self.scene.current_frame = frame
        apply_interpolation(self.scene, frame)
        for outliner in self._docks_of(OutlinerWidget):
            outliner.invalidate_thumbnails()
        for stage in self._docks_of(StageWidget):
            stage.update()
        for transport in self._transports():
            transport.set_frame(frame)

    def _setup_ui(self):
        self._menubar = StripeMenuBar(self)
        self.setMenuWidget(self._menubar)
        self._setup_menus()

        context = SimpleNamespace(scene=self.scene, history=self.history)
        self.docks = DockManager(context)
        self.docks.register("stage", "Stage", lambda ctx: StageWidget(ctx.scene, ctx.history))
        self.docks.register("outliner", "Outliner", lambda ctx: OutlinerWidget(ctx.scene, ctx.history))
        self.docks.register(
            "properties",
            "Properties",
            lambda ctx: PropertiesWidget(ctx.scene, ctx.history),
        )
        self.docks.register(
            "timeline",
            "Timeline",
            lambda ctx: TimelineWidget(ctx.scene, ctx.history),
            header_factory=lambda ctx: TimelineTransport(ctx.scene),
        )

        # Build initial Blender-style area layout tree:
        #   Root: horizontal split (outliner | right)
        #   Right: vertical split (top / timeline)
        #   Top: horizontal split (stage | properties)
        initial_tree = AreaNode.split(
            Qt.Horizontal, 0.18,
            AreaNode.leaf("outliner"),
            AreaNode.split(
                Qt.Vertical, 0.65,
                AreaNode.split(
                    Qt.Horizontal, 0.7,
                    AreaNode.leaf("stage"),
                    AreaNode.leaf("properties"),
                ),
                AreaNode.leaf("timeline"),
            ),
        )

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.area_root = AreaWidget(initial_tree, self.docks,
                                    rebuild_callback=self._on_area_rebuild)
        main_layout.addWidget(self.area_root)

        # Keep a deep copy as a template so the layout can be reset to the
        # default on demand (nodes are mutated in place by split/join).
        self._default_tree = _deep_copy_tree(initial_tree)

        self.statusBar().showMessage("Ready  |  Click: select  |  G: move  |  R: rotate  |  S: scale  |  I: keyframe  |  Space: play")

    def _connect_signals(self):
        self._wire_all_docks()

    def _on_area_rebuild(self):
        """Called after the area layout tree is rebuilt (split/join)."""
        self._wire_all_docks()

    def _dump_area_layout(self):
        """Print the current dock area layout tree to stderr (Ctrl+D)."""
        import sys
        print("=== AREA LAYOUT ===", file=sys.stderr)
        self.area_root.dump_layout()
        print("=== docks:", [s.current_dock_id for s in self._slots], "===",
              file=sys.stderr)
        self.statusBar().showMessage("Layout dumped to console (Ctrl+D)")

    def _reset_area_layout(self):
        """Reset the area layout back to the default 3-dock arrangement."""
        import sys
        print("=== RESET AREA LAYOUT to default ===", file=sys.stderr)
        try:
            self.area_root.reset_to_tree(self._default_tree)
            self._on_area_rebuild()
        except Exception:
            import traceback
            traceback.print_exc()
        self.statusBar().showMessage("Layout reset to default (Ctrl+R)")

    def _wire_all_docks(self):
        """Discover all DockWidget instances from the area tree and wire signals."""
        # Disconnect old signals
        for slot in getattr(self, "_slots", []):
            try:
                slot.dock_changed.disconnect()
            except (RuntimeError, TypeError):
                pass
            for signal in getattr(slot, "_wired_signals", []):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            slot._wired_signals = []

        # Find all DockWidgets in the area tree
        self._slots = self.area_root.findChildren(DockWidget)
        for slot in self._slots:
            slot.dock_changed.connect(self._on_dock_changed)
            self._wire_slot(slot)

    def _on_dock_changed(self):
        slot = self.sender()
        if slot is None:
            return
        # Persist the switched dock back into the owning leaf's node so a later
        # layout rebuild re-creates this slot with the new dock instead of
        # reverting to the original one from startup.
        self._persist_dock_switch(slot)
        self._wire_slot(slot)

    def _persist_dock_switch(self, slot: DockWidget) -> None:
        """Write `slot`'s current dock_id into its leaf AreaNode."""
        leaf = self.area_root._leaf_for_widget(slot)
        if leaf is not None and not leaf.node.is_split:
            leaf.node.dock_id = slot.current_dock_id

    def _wire_slot(self, slot: DockWidget) -> None:
        for signal in getattr(slot, "_wired_signals", []):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass
        slot._wired_signals = []

        widget = slot.current_widget()
        if widget is None:
            return

        def connect(signal, handler):
            signal.connect(handler)
            slot._wired_signals.append(signal)

        if isinstance(widget, StageWidget):
            connect(widget.selection_changed, self._on_selection_changed)
            connect(widget.status_message, self._on_status_message)
            connect(widget.toggle_playback, self._toggle_playback)
            connect(widget.keyframe_created, self._on_keyframe_created)
            connect(widget.transform_started, self._on_transform_started)
            connect(widget.transform_ended, self._on_transform_ended)
        elif isinstance(widget, OutlinerWidget):
            connect(widget.selection_changed, self._on_outliner_selected)
            connect(widget.object_changed, self._on_outliner_changed)
            connect(widget.object_hovered, self._on_outliner_hover)
        elif isinstance(widget, PropertiesWidget):
            connect(widget.object_changed, self._on_properties_changed)
        elif isinstance(widget, TimelineWidget):
            connect(widget.playhead_moved, self._on_playhead_moved)
            connect(widget.status_message, self._on_status_message)
            connect(widget.keyframe_selection_changed, self._on_keyframe_selection_changed)
            extra = slot.current_extra()
            if isinstance(extra, TimelineTransport):
                connect(extra.toggle_requested, self._toggle_playback)

    def _docks_of(self, cls):
        return [slot.current_widget() for slot in self._slots if isinstance(slot.current_widget(), cls)]

    def _dock_under_cursor(self) -> QWidget | None:
        """Return the dock content widget physically under the global cursor."""
        pos = QCursor.pos()
        for slot in self._slots:
            widget = slot.current_widget()
            if widget is not None and widget.isVisible() and widget.rect().contains(
                widget.mapFromGlobal(pos)
            ):
                return widget
        return None

    def _focus_dock_under_cursor(self):
        # Never steal focus away from an active menu / popup / dialog, or the
        # dock-switch menu would lose its keyboard interaction.
        if QApplication.activePopupWidget() is not None:
            return
        # During an active relative drag (hidden fake-cursor gesture) the real
        # cursor is parked at the gesture anchor but may still briefly roam;
        # yanking focus to whatever dock it passes over would focusOutEvent the
        # dragging dock and cancel the transform mid-drag.
        for slot in self._slots:
            drag = getattr(slot.current_widget(), "_drag", None)
            if drag is not None and getattr(drag, "active", False):
                return
        dock = self._dock_under_cursor()
        if dock is None:
            return
        focused = QApplication.focusWidget()
        # If keyboard focus already lives inside this dock (an active text
        # editor, a spinbox, etc.), leave it alone - yanking focus back to the
        # dock shell would cancel inline renames and typed edits the moment
        # they begin.
        if focused is not None and (focused is dock or dock.isAncestorOf(focused)):
            return
        if not dock.hasFocus():
            dock.setFocus()

    def _transports(self):
        return [
            slot.current_extra()
            for slot in self._slots
            if isinstance(slot.current_extra(), TimelineTransport)
        ]

    def _on_selection_changed(self, obj):
        # Selection state is global (core.selection.SelectionState), so there's
        # nothing to copy between docks. Just persist it to history (the whole
        # selection list, so undo/redo restores multi-selections too) and
        # refresh cross-dock hover highlights.
        selected_ids = [o.id for o in SelectionState.selected()]
        self.history.save_selection(selected_ids)
        for stage in self._docks_of(StageWidget):
            stage.hovered_object = None
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()

    def _on_transform_started(self):
        # A live stage drag is about to move/rotate/scale objects every frame;
        # freeze outliner thumbnails so the animation tick keeps blitting the
        # cached image instead of rebuilding it 30x/second. Invalidated once
        # the transform is confirmed or cancelled.
        for outliner in self._docks_of(OutlinerWidget):
            outliner._thumb_frozen = True

    def _on_transform_ended(self):
        for outliner in self._docks_of(OutlinerWidget):
            outliner._thumb_frozen = False
            outliner.invalidate_thumbnails()

    def _on_status_message(self, msg):
        if msg:
            self.statusBar().showMessage(msg)
        else:
            self.statusBar().showMessage(
                "Ready  |  Click: select  |  G: move  |  R: rotate  |  S: scale  |  I: keyframe  |  Space: play"
            )

    def _on_playhead_moved(self, frame):
        apply_interpolation(self.scene, frame)
        # Scrubbing changes object transforms every frame, so keep the outliner
        # thumbnails frozen and rebuild them only once scrubbing pauses.
        for outliner in self._docks_of(OutlinerWidget):
            outliner._thumb_frozen = True
        if not self._playing:
            self._thumb_unfreeze_timer.start()
        for stage in self._docks_of(StageWidget):
            stage.update()

    def _on_playhead_quiet(self):
        for outliner in self._docks_of(OutlinerWidget):
            outliner._thumb_frozen = False
            outliner.invalidate_thumbnails()

    def _on_keyframe_selection_changed(self):
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()

    def _on_outliner_selected(self, objects):
        # Selection state is already global; just persist + refresh hover.
        selected_ids = [o.id for o in objects]
        self.history.save_selection(selected_ids)
        for stage in self._docks_of(StageWidget):
            stage.hovered_object = None
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()

    def _on_outliner_hover(self, obj):
        for stage in self._docks_of(StageWidget):
            stage.set_hovered_direct(obj)
            stage.update()

    def _on_keyframe_created(self):
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()

    def _on_outliner_changed(self):
        for stage in self._docks_of(StageWidget):
            stage.update()
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()

    def _on_properties_changed(self):
        # A panel edit changed project timing or an object's transform/opacity.
        # Restage everything; the outliner thumbnails need an explicit rebuild
        # because (unlike the outliner's own edits) nothing self-invalidates.
        for stage in self._docks_of(StageWidget):
            stage.update()
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()
        for outliner in self._docks_of(OutlinerWidget):
            outliner.invalidate_thumbnails()

    def _update_all(self):
        for stage in self._docks_of(StageWidget):
            stage.update()
        for outliner in self._docks_of(OutlinerWidget):
            outliner.update()
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()

    def _toggle_playback(self):
        if self._playing:
            self._stop_playback()
        else:
            self._start_playback()

    def _start_playback(self):
        self._playing = True
        for outliner in self._docks_of(OutlinerWidget):
            outliner._thumb_frozen = True
        interval = 1000 / self.scene.fps
        self._play_timer.start(int(interval))
        for transport in self._transports():
            transport.set_playing(True)
        self.statusBar().showMessage("Playing...")

    def _stop_playback(self):
        self._playing = False
        self._play_timer.stop()
        for outliner in self._docks_of(OutlinerWidget):
            outliner._thumb_frozen = False
            outliner.invalidate_thumbnails()
        for transport in self._transports():
            transport.set_playing(False)
        self.statusBar().showMessage(
            "Ready  |  Click: select  |  G: move  |  R: rotate  |  S: scale  |  I: keyframe  |  Space: play"
        )

    def _advance_frame(self):
        # Do not advance playback if the user is scrubbing/holding the playhead
        if any(timeline._dragging_playhead for timeline in self._docks_of(TimelineWidget)):
            return

        self.scene.current_frame += 1
        if self.scene.current_frame > self.scene.end_frame:
            self.scene.current_frame = self.scene.start_frame
        apply_interpolation(self.scene, self.scene.current_frame)
        for transport in self._transports():
            transport.set_frame(self.scene.current_frame)
        for stage in self._docks_of(StageWidget):
            stage.update()
        for timeline in self._docks_of(TimelineWidget):
            timeline.update()
