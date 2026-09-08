from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QRect, Signal, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel

from core.animation import apply_interpolation
from core.commands import CompoundCommand, KeyframeCommand, PropertyCommand
from core.history import History
from core.model import Scene, SceneObject, Keyframe
from core.selection import (
    KeyframeSelection,
    KeyframeSelectionState,
    serialize_keyframe_selection,
)
from ui.theme import Theme
from ui.menus import stripe_menu_open
from ui.relative_drag import RelativeDrag


class TimelineWidget(QWidget):
    FRAME_WIDTH = 6
    RULER_HEIGHT = 28
    TRACK_HEIGHT = 24
    LABEL_WIDTH = 80
    MIN_FRAME_WIDTH = 1.0
    MAX_FRAME_WIDTH = 60.0
    ZOOM_STEP = 1.15

    playhead_moved = Signal(int)
    status_message = Signal(str)
    keyframe_selection_changed = Signal()

    _MODES = (
        ("constant", "Constant"),
        ("linear", "Linear"),
        ("adaptive", "Adaptive"),
    )

    def __init__(self, scene: Scene, history: History):
        super().__init__()
        self.scene = scene
        self.history = history
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._mouse_hover = False
        self._dragging_playhead = False

        # pan/zoom state
        # `_frame_width` is the current pixels-per-frame (zoom level); `_pan_frame`
        # is the (possibly fractional) frame that lines up with x == LABEL_WIDTH.
        # At the default zoom/pan these reproduce the original fixed layout.
        self._frame_width = float(self.FRAME_WIDTH)
        self._pan_frame = float(self.scene.start_frame)
        self._panning = False
        self._pan_start_pos = QPointF(0, 0)
        self._pan_start_frame = 0.0
        self._pan_y = 0.0

        # transform handles
        self._transform_mode: str | None = None  # "move" | "scale"
        self._drag_start = QPointF(0, 0)          # global cursor pos at start
        self._start_frames: list[int] = []
        self._drag_keyframes: list[list] = []
        self._scale_start_dists: list[float] = []
        self._scale_mid = 0.0
        # Shared relative-drag controller: hides the real cursor and anchors it
        # so keyframe drags never steal focus, and drives the fake cursor.
        self._drag = RelativeDrag(self)
        # Total horizontal pixel movement accumulated from the controller's
        # per-move deltas this gesture (a plain absolute cursor delta would not
        # survive the controller re-anchoring the real cursor every move).
        self._accum_dx = 0.0
        # Pending "insert" actions from _duplicate_selected, folded into the
        # transform's CompoundCommand at confirm/cancel.
        self._pending_insert_cmd: list[tuple] | None = None

        # marquee selection
        self._marquee: QPointF | None = None
        self._marquee_anchor = QPointF(0, 0)
        self._marquee_extend = False  # shift held -> add to selection

        # range edge dragging (animation start/end boundaries in ruler)
        self._dragging_range_edge: str | None = None  # "start" | "end"
        self._range_edge_hit_threshold = 6  # pixels

    # ------------------------------------------------------------------ #
    # Keyframe selection (global, shared by every timeline dock)
    # ------------------------------------------------------------------ #
    @property
    def selected(self) -> list[KeyframeSelection]:
        return KeyframeSelectionState.selected()

    @selected.setter
    def selected(self, value: list[KeyframeSelection]) -> None:
        KeyframeSelectionState.set_selected(value)

    def _commit_keyframe_selection(self):
        """Persist a user-initiated selection change as an undoable step and
        tell the other docks to repaint."""
        self.history.save_keyframe_selection(
            serialize_keyframe_selection(self.selected)
        )
        self.keyframe_selection_changed.emit()
        self.update()

    def _sync_keyframe_selection(self):
        """Remember the current selection in history without pushing a new undo
        step (used when a transform/duplicate/delete implicitly moves it)."""
        self.history.sync_keyframe_selection()
        self.keyframe_selection_changed.emit()
        self.update()

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.fillRect(self.rect(), QColor(35, 35, 35))

        # Everything that scrolls with the timeline (ruler, tracks, dividers,
        # keyframes, playhead, marquee) is confined to the area right of the
        # label column. Without this, a keyframe dragged to a frame that maps
        # to x < LABEL_WIDTH would render on top of the row labels.
        painter.save()
        painter.setClipRect(
            QRectF(self.LABEL_WIDTH, 0, max(0.0, self.width() - self.LABEL_WIDTH), self.height())
        )
        self._draw_ruler(painter)
        painter.restore()

        # The ruler is fixed at the top; only the track content scrolls
        # vertically beneath it, so clip it below the ruler.
        painter.save()
        painter.setClipRect(
            QRectF(
                self.LABEL_WIDTH,
                self.RULER_HEIGHT,
                max(0.0, self.width() - self.LABEL_WIDTH),
                max(0.0, self.height() - self.RULER_HEIGHT),
            )
        )
        self._draw_tracks(painter)
        self._draw_marquee(painter)
        painter.restore()

        # Full-height guides (range boundaries, playhead) are drawn after the
        # ruler so they still read across it no matter how far we pan.
        painter.save()
        painter.setClipRect(
            QRectF(self.LABEL_WIDTH, 0, max(0.0, self.width() - self.LABEL_WIDTH), self.height())
        )
        self._draw_range_boundaries(painter)
        self._draw_playhead(painter)
        painter.restore()

        # The label panel is drawn last, unclipped, so it always sits on top
        # of the timeline content and keeps its own dedicated space.
        self._draw_labels(painter)

        # The relative-drag fake cursor (drawn during keyframe move/scale) sits
        # on top, in plain widget-local space.
        self._drag.paint(painter)

        painter.end()

    def _draw_labels(self, painter: QPainter):
        """Fixed panel on the left holding each track's object name. Drawn
        last (and unclipped) so timeline content never overlaps it."""
        stack = self._stack_objects()

        painter.fillRect(QRectF(0, 0, self.LABEL_WIDTH, self.height()), QColor(26, 26, 26))

        for i, obj in enumerate(stack):
            ty = self._track_y(obj)
            bg = QColor(32, 32, 32) if i % 2 == 0 else QColor(38, 38, 38)
            painter.fillRect(QRectF(0, ty, self.LABEL_WIDTH, self.TRACK_HEIGHT), bg)

            painter.setPen(QColor(180, 180, 180))
            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(8, int(ty + 16), obj.name[:10])

            painter.setPen(QPen(QColor(50, 50, 50), 1))
            painter.drawLine(0, int(ty + self.TRACK_HEIGHT), self.LABEL_WIDTH, int(ty + self.TRACK_HEIGHT))

        # Crisp divider separating the label panel from the scrollable timeline.
        painter.setPen(QPen(QColor(70, 70, 70), 1))
        painter.drawLine(self.LABEL_WIDTH, 0, self.LABEL_WIDTH, self.height())

    def _draw_range_boundaries(self, painter: QPainter):
        """Bright guide lines at the animation's exact start/end frames, drawn
        the full height of the widget so the range reads clearly no matter how
        far the view has been panned or zoomed, or how many tracks there are."""
        start = self._x_to_frame(0)
        end = self._x_to_frame(self.width())
        painter.setPen(QPen(QColor(150, 150, 150), 1))
        if start <= self.scene.start_frame <= end:
            bx = self._frame_to_x(self.scene.start_frame)
            painter.drawLine(int(bx), 0, int(bx), self.height())
        if start <= self.scene.end_frame <= end:
            bx = self._frame_to_x(self.scene.end_frame)
            painter.drawLine(int(bx), 0, int(bx), self.height())

    def _draw_ruler(self, painter: QPainter):
        ruler_rect = QRectF(0, 1, self.width(), self.RULER_HEIGHT - 1)
        painter.fillRect(ruler_rect, QColor(30, 30, 30))

        # Highlight the strip that's actually inside the animation range so it
        # pops out against the dimmed out-of-range background.
        range_left = self._frame_to_x(self.scene.start_frame)
        range_right = self._frame_to_x(self.scene.end_frame)
        in_range_rect = QRectF(
            range_left, 1, max(0.0, range_right - range_left), self.RULER_HEIGHT - 1
        )
        painter.fillRect(in_range_rect, QColor(45, 45, 45))

        # Draw guides across the whole visible viewport now, not just the
        # animation's own frame range, so panning/zooming past either end
        # still shows frame numbers (e.g. -10, -50).
        start = self._x_to_frame(0)
        end = self._x_to_frame(self.width())

        for frame in range(start, end + 1):
            x = self._frame_to_x(frame)
            in_range = self.scene.start_frame <= frame <= self.scene.end_frame
            if frame % 10 == 0:
                painter.setPen(QColor(180, 180, 180) if in_range else QColor(80, 80, 80))
                font = painter.font()
                font.setPointSize(8)
                painter.setFont(font)
                painter.drawText(int(x + 2), int(self.RULER_HEIGHT - 8), str(frame))
                painter.drawLine(int(x), int(self.RULER_HEIGHT - 16), int(x), int(self.RULER_HEIGHT))
            elif frame % 5 == 0:
                painter.setPen(QColor(120, 120, 120) if in_range else QColor(55, 55, 55))
                painter.drawLine(int(x), int(self.RULER_HEIGHT - 8), int(x), int(self.RULER_HEIGHT))
            else:
                painter.setPen(QColor(80, 80, 80) if in_range else QColor(40, 40, 40))
                painter.drawLine(int(x), int(self.RULER_HEIGHT - 4), int(x), int(self.RULER_HEIGHT))

        painter.setPen(QPen(QColor(60, 60, 60), 1))
        painter.drawLine(0, int(self.RULER_HEIGHT), self.width(), int(self.RULER_HEIGHT))

    def _selection_frames_for(self, obj: SceneObject):
        frames = set()
        for channel_kfs in obj.keyframes.values():
            for kf in channel_kfs:
                frames.add(kf.frame)
        return frames

    def _draw_tracks(self, painter: QPainter):
        stack = self._stack_objects()
        y0 = self.RULER_HEIGHT - self._pan_y
        total_height = len(stack) * self.TRACK_HEIGHT

        range_left = self._frame_to_x(self.scene.start_frame)
        range_right = self._frame_to_x(self.scene.end_frame)

        # Pass 1: row backgrounds, dimmed outside the animation range (mirrors
        # the ruler above). The label column is drawn separately on top, so
        # it doesn't matter that these rects extend under it.
        for i, obj in enumerate(stack):
            ty = self._track_y(obj)
            in_range_color = QColor(32, 32, 32) if i % 2 == 0 else QColor(38, 38, 38)
            out_range_color = QColor(20, 20, 20) if i % 2 == 0 else QColor(24, 24, 24)

            painter.fillRect(QRectF(0, ty, self.width(), self.TRACK_HEIGHT), out_range_color)
            if range_right > range_left:
                painter.fillRect(
                    QRectF(range_left, ty, range_right - range_left, self.TRACK_HEIGHT),
                    in_range_color,
                )

        # Pass 2: a divider line per frame, spanning every track, so each
        # frame column is easy to line up at a glance.
        self._draw_frame_dividers(painter, y0, total_height)

        # Pass 3: keyframe diamonds and row separators on top.
        for i, obj in enumerate(stack):
            ty = self._track_y(obj)

            for frame in sorted(self._selection_frames_for(obj)):
                kx = self._frame_to_x(frame)
                ky = ty + self.TRACK_HEIGHT / 2
                is_selected = self._is_selected(obj, frame)
                mode = self._mode_at(obj, frame)
                self._draw_diamond(
                    painter, kx, ky, dim=obj.locked, selected=is_selected, interpolation=mode
                )

            painter.setPen(QPen(QColor(50, 50, 50), 1))
            painter.drawLine(0, int(ty + self.TRACK_HEIGHT), self.width(), int(ty + self.TRACK_HEIGHT))

    def _draw_frame_dividers(self, painter: QPainter, top: float, height: float):
        """Thin vertical guide at every visible frame across the track area,
        matching the ruler's tick rhythm (dimmed outside the animation range)."""
        if height <= 0:
            return
        start = self._x_to_frame(self.LABEL_WIDTH)
        end = self._x_to_frame(self.width())

        for frame in range(start, end + 1):
            x = self._frame_to_x(frame)
            if x < self.LABEL_WIDTH:
                continue
            in_range = self.scene.start_frame <= frame <= self.scene.end_frame
            if frame % 10 == 0:
                color = QColor(68, 68, 68) if in_range else QColor(38, 38, 38)
            elif frame % 5 == 0:
                color = QColor(54, 54, 54) if in_range else QColor(32, 32, 32)
            else:
                color = QColor(44, 44, 44) if in_range else QColor(27, 27, 27)
            painter.setPen(QPen(color, 1))
            painter.drawLine(int(x), int(top), int(x), int(top + height))

    def _mode_at(self, obj: SceneObject, frame: int) -> str:
        for channel_kfs in obj.keyframes.values():
            for kf in channel_kfs:
                if kf.frame == frame:
                    return kf.interpolation
        return "adaptive"

    def _is_selected(self, obj: SceneObject, frame: int) -> bool:
        sel = KeyframeSelection(obj, frame)
        return sel in self.selected

    def _diamond_path(self, x, y, s, interpolation):
        path = QPainterPath()
        if interpolation == "linear":
            path.moveTo(x - s, y)
            path.lineTo(x, y - s)
            path.lineTo(x + s, y)
            path.lineTo(x, y + s)
            path.closeSubpath()
        elif interpolation == "constant":
            path.addRect(x - s, y - s, s * 2, s * 2)
        else:  # adaptive
            path.addEllipse(QPointF(x, y), s, s)
        return path

    def _draw_diamond(self, painter, x, y, dim=False, selected=False, interpolation="adaptive"):
        if dim and not selected:
            color = QColor(90, 90, 90)
        elif selected:
            color = Theme.ACCENT
        else:
            color = QColor(255, 255, 255)

        s = 4

        if selected:
            # Draw a slightly larger white silhouette of the *same* shape first,
            # then the normal-size colored shape on top. The larger shape peeks
            # out on every side, giving an outline that hugs the actual silhouette
            # instead of a bounding rectangle.
            outline_path = self._diamond_path(x, y, s + 2, interpolation)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255))
            painter.drawPath(outline_path)

        path = self._diamond_path(x, y, s, interpolation)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)

    def _draw_playhead(self, painter: QPainter):
        x = self._frame_to_x(self.scene.current_frame)

        painter.setPen(QPen(QColor(255, 0, 0), 1))
        painter.drawLine(int(x), 0, int(x), self.height())

        tri = 6
        path = QPainterPath()
        path.moveTo(x - tri, 0)
        path.lineTo(x + tri, 0)
        path.lineTo(x, tri + 2)
        path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 0, 0))
        painter.drawPath(path)

    def _draw_marquee(self, painter: QPainter):
        if self._marquee is None:
            return
        rect = QRect(self._marquee_anchor.toPoint(), self._marquee.toPoint()).normalized()
        painter.setBrush(QColor(0, 200, 255, 30))
        painter.setPen(QPen(QColor(0, 200, 255), 1))
        painter.drawRect(rect)

    # ------------------------------------------------------------------ #
    # Coordinate helpers
    # ------------------------------------------------------------------ #
    def _frame_to_x(self, frame: int) -> float:
        return self.LABEL_WIDTH + (frame - self._pan_frame) * self._frame_width

    def _x_to_frame(self, x: float) -> int:
        return round((x - self.LABEL_WIDTH) / self._frame_width + self._pan_frame)

    def _stack_objects(self) -> list[SceneObject]:
        """Track display order: top-most layer first, each object followed by
        its children (matches the outliner's tree order)."""
        out: list[SceneObject] = []

        def walk(objs: list[SceneObject]) -> None:
            for o in reversed(objs):
                out.append(o)
                walk(o.children)

        walk(self.scene.objects)
        return out

    def _track_y(self, obj: SceneObject) -> float:
        try:
            i = self._stack_objects().index(obj)
        except ValueError:
            return self.RULER_HEIGHT - self._pan_y
        return self.RULER_HEIGHT + i * self.TRACK_HEIGHT - self._pan_y

    def _y_to_track(self, y: float) -> int:
        return int((y - self.RULER_HEIGHT + self._pan_y) // self.TRACK_HEIGHT)

    def _max_pan_y(self) -> float:
        """Largest vertical pan offset before blank space appears either above
        the first track or below the last one."""
        content_height = len(self._stack_objects()) * self.TRACK_HEIGHT
        viewport_height = self.height() - self.RULER_HEIGHT
        return max(0.0, content_height - viewport_height)

    def _track_at(self, y: float) -> SceneObject | None:
        if y < self.RULER_HEIGHT:
            return None
        stack = self._stack_objects()
        i = self._y_to_track(y)
        if 0 <= i < len(stack):
            return stack[i]
        return None

    def _hit_test_range_edge(self, pos: QPointF) -> str | None:
        """Check if the mouse position is near a range boundary in the ruler zone."""
        if pos.y() >= self.RULER_HEIGHT:
            return None
        start_x = self._frame_to_x(self.scene.start_frame)
        end_x = self._frame_to_x(self.scene.end_frame)
        threshold = self._range_edge_hit_threshold
        if abs(pos.x() - start_x) <= threshold:
            return "start"
        if abs(pos.x() - end_x) <= threshold:
            return "end"
        return None

    def _hit_test(self, pos: QPointF) -> KeyframeSelection | None:
        obj = self._track_at(pos.y())
        if obj is None:
            return None
        frame = self._x_to_frame(pos.x())
        ty = self._track_y(obj)
        ky = ty + self.TRACK_HEIGHT / 2
        if abs(pos.y() - ky) > 8:
            return None
        best = None
        best_dist = 12
        for f in self._selection_frames_for(obj):
            kx = self._frame_to_x(f)
            d = abs(pos.x() - kx)
            if d <= best_dist:
                best_dist = d
                best = KeyframeSelection(obj, f)
        return best

    def _frames_under_marquee(self, rect: QRect) -> list[KeyframeSelection]:
        result = []
        for obj in self._stack_objects():
            ky = self._track_y(obj) + self.TRACK_HEIGHT / 2
            if not (rect.top() - 8 <= ky <= rect.bottom() + 8):
                continue
            for f in self._selection_frames_for(obj):
                kx = self._frame_to_x(f)
                if rect.left() - 6 <= kx <= rect.right() + 6:
                    result.append(KeyframeSelection(obj, f))
        return result

    def _refresh_interpolation(self):
        apply_interpolation(self.scene, self.scene.current_frame)
        self.playhead_moved.emit(self.scene.current_frame)

    def _duplicate_selected(self):
        # Insert the duplicates but don't push an undo step yet: the
        # duplicates immediately enter a move, so the final undo step is a
        # single CompoundCommand (insert + move) built at confirm time.
        new_selections: list[KeyframeSelection] = []
        dup_keyframes: list[list] = []
        insert_actions: list[tuple] = []
        for sel in self.selected:
            obj = sel.obj
            pair: list = []
            for channel, kf in list(sel.iterate()):
                new_kf = Keyframe(
                    frame=kf.frame, value=kf.value, interpolation=kf.interpolation
                )
                ch = obj.keyframes.setdefault(channel, [])
                ch.append(new_kf)
                ch.sort(key=lambda k: k.frame)
                pair.append((channel, new_kf))
                insert_actions.append(
                    KeyframeCommand.record_insert(obj, channel, new_kf)
                )
            dup_keyframes.append(pair)
            new_selections.append(KeyframeSelection(obj, sel.frame))
        self.selected = new_selections
        self._pending_insert_cmd = insert_actions
        self._sync_keyframe_selection()
        self._refresh_interpolation()
        self._enter_move([sel.frame for sel in new_selections], dup_keyframes)
        self.status_message.emit(
            "Duplicated keyframes: move mouse, click to confirm, Esc to cancel"
        )

    # ------------------------------------------------------------------ #
    # Mouse interaction
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event):
        self.setFocus()

        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start_pos = QPointF(event.position())
            self._pan_start_frame = self._pan_frame
            self._pan_start_y = self._pan_y
            self.setCursor(Qt.ClosedHandCursor)
            return

        if event.button() != Qt.LeftButton:
            return

        pos = QPointF(event.position())
        shift = bool(event.modifiers() & Qt.ShiftModifier)

        if self._transform_mode:
            self._confirm_transform()
            return

        # Ruler zone -> drag the range edges or the playhead. Track zone ->
        # selection/marquee.
        if pos.y() < self.RULER_HEIGHT:
            edge = self._hit_test_range_edge(pos)
            if edge is not None:
                self._dragging_range_edge = edge
                self._range_edge_start_x = pos.x()
                self._range_edge_start_frame = self.scene.start_frame
                self._range_edge_end_frame = self.scene.end_frame
                self.setCursor(Qt.SizeHorCursor)
                self.update()
                return
            self._dragging_playhead = True
            self.setCursor(Qt.ArrowCursor)
            self._scrub(pos.x())
            self.update()
            return

        hit = self._hit_test(pos)
        if hit is None:
            # start a marquee
            self._marquee_anchor = pos
            self._marquee = pos
            self._marquee_extend = shift
            self.update()
            return

        if shift:
            if hit in self.selected:
                self.selected.remove(hit)
            else:
                self.selected.append(hit)
        else:
            self.selected = [hit]
        self._commit_keyframe_selection()

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())

        if self._panning:
            dx = pos.x() - self._pan_start_pos.x()
            dy = pos.y() - self._pan_start_pos.y()
            self._pan_frame = self._pan_start_frame - dx / self._frame_width
            self._pan_y = max(0.0, min(self._max_pan_y(), self._pan_start_y - dy))
            self.update()
            return

        if self._transform_mode:
            self._update_transform(pos)
            return

        if self._dragging_range_edge:
            self._update_range_edge_drag(pos)
            return

        if self._dragging_playhead and (event.buttons() & Qt.LeftButton):
            self._scrub(pos.x())
            return

        if self._marquee is not None:
            self._marquee = pos
            self.update()

        # Hovering over a range boundary in the ruler -> show a resize cursor.
        if not (event.buttons() & Qt.LeftButton) and pos.y() < self.RULER_HEIGHT:
            edge = self._hit_test_range_edge(pos)
            if edge is not None and self.cursor().shape() != Qt.SizeHorCursor:
                self.setCursor(Qt.SizeHorCursor)
            elif edge is None and self.cursor().shape() == Qt.SizeHorCursor:
                self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            if self._panning:
                self._panning = False
                self.setCursor(Qt.ArrowCursor)
            return

        if event.button() != Qt.LeftButton:
            return

        if self._dragging_range_edge:
            was_start = self._dragging_range_edge == "start"
            old_start = self._range_edge_start_frame
            old_end = self._range_edge_end_frame
            self._dragging_range_edge = None
            self.setCursor(Qt.ArrowCursor)
            changes = {}
            if was_start and self.scene.start_frame != old_start:
                changes[None] = {"start_frame": (old_start, self.scene.start_frame)}
            elif not was_start and self.scene.end_frame != old_end:
                changes[None] = {"end_frame": (old_end, self.scene.end_frame)}
            if changes:
                self.history.push(PropertyCommand(changes))
            self.update()
            return

        if self._dragging_playhead:
            self._dragging_playhead = False
            self.update()
            return

        if self._marquee is not None:
            rect = QRect(self._marquee_anchor.toPoint(), self._marquee.toPoint()).normalized()
            if rect.width() > 3 or rect.height() > 3:
                found = self._frames_under_marquee(rect)
                if self._marquee_extend:
                    for sel in found:
                        if sel not in self.selected:
                            self.selected.append(sel)
                else:
                    self.selected = found
            else:
                # plain click on blank timeline space -> deselect all
                self.selected.clear()
            self._marquee = None
            self._commit_keyframe_selection()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return

        pos = QPointF(event.position())
        factor = self.ZOOM_STEP if delta > 0 else 1.0 / self.ZOOM_STEP

        # Keep the frame currently under the cursor stationary while zooming,
        # so the timeline zooms "into" wherever the mouse is pointing.
        frame_at_cursor = self._pan_frame + (pos.x() - self.LABEL_WIDTH) / self._frame_width
        new_width = max(
            self.MIN_FRAME_WIDTH, min(self.MAX_FRAME_WIDTH, self._frame_width * factor)
        )
        if new_width != self._frame_width:
            self._frame_width = new_width
            self._pan_frame = frame_at_cursor - (pos.x() - self.LABEL_WIDTH) / self._frame_width
            self.update()

        event.accept()

    def _scrub(self, x: float):
        frame = self._x_to_frame(x)
        frame = max(self.scene.start_frame, min(self.scene.end_frame, frame))

        # When scrubbing, update the scene frame to match mouse drag
        if frame != self.scene.current_frame:
            self.scene.current_frame = frame
            self.playhead_moved.emit(frame)
        self.update()

    def _update_range_edge_drag(self, pos: QPointF):
        """Move the animation start/end boundary to follow the cursor while the
        user drags it in the ruler, keeping start <= end."""
        delta = round((pos.x() - self._range_edge_start_x) / self._frame_width)
        if self._dragging_range_edge == "start":
            self.scene.start_frame = min(
                self._range_edge_start_frame + delta, self._range_edge_end_frame
            )
        else:
            self.scene.end_frame = max(
                self._range_edge_end_frame + delta, self._range_edge_start_frame
            )
        self.update()

    # ------------------------------------------------------------------ #
    # Keyboard interaction
    # ------------------------------------------------------------------ #
    def handle_key_press(self, event):
        key = event.key()

        if self._transform_mode:
            if key == Qt.Key_Escape:
                self._cancel_transform()
            return

        if key == Qt.Key_G and self.selected and self._mouse_hover:
            self._start_move()
        elif key == Qt.Key_S and self.selected and self._mouse_hover:
            self._start_scale()
        elif key == Qt.Key_D and self.selected and self._mouse_hover:
            self._duplicate_selected()
        elif key == Qt.Key_T and self.selected and self._mouse_hover:
            # Auto-repeat presses (the key is being held) must not re-enter
            # the popup's nested loop: the open menu itself drives selection
            # on the key release.
            if not stripe_menu_open():
                self._open_interpolation_menu()
        elif key == Qt.Key_Delete and self.selected:
            self._delete_selected()
        elif key == Qt.Key_Escape:
            self.selected.clear()
            self._commit_keyframe_selection()

    def keyPressEvent(self, event):
        self.handle_key_press(event)

    def enterEvent(self, event):
        self._mouse_hover = True
        if not stripe_menu_open():
            self.setFocus()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._mouse_hover = False
        self._marquee = None
        super().leaveEvent(event)

    def resizeEvent(self, event):
        self._pan_y = max(0.0, min(self._max_pan_y(), self._pan_y))
        super().resizeEvent(event)

    # ------------------------------------------------------------------ #
    # Move (G)
    # ------------------------------------------------------------------ #
    def _start_move(self):
        self._pending_insert_cmd = None
        self._enter_move(
            [sel.frame for sel in self.selected],
            [list(sel.iterate()) for sel in self.selected],
        )

    def _enter_move(self, start_frames: list[int], keyframes: list[list]):
        self._transform_mode = "move"
        # Keep the initial cursor x: it seeds the scale-style absolute distance
        # tracking if we ever need it, and `_accum_dx` measures relative motion.
        self._drag_start = QPointF(self.cursor().pos())
        self._accum_dx = 0.0
        self._start_frames = list(start_frames)
        self._drag_keyframes = list(keyframes)
        self._drag.begin("cross")
        self.grabMouse()
        self.status_message.emit("Move keyframes: move mouse, click to confirm, Esc to cancel")
        self.update()

    def _update_transform(self, pos: QPointF):
        if self._transform_mode == "move":
            self._update_move(pos)
        elif self._transform_mode == "scale":
            self._update_scale(pos)

    def _apply_moved_frames(self, start_frames: list[int], delta_frames: int):
        # Keyframes are allowed to move outside the scene's start/end range;
        # only the playhead (_scrub) is confined to it.
        for sel, captured, start_frame in zip(
            self.selected, self._drag_keyframes, start_frames
        ):
            new_frame = start_frame + delta_frames
            for _, kf in captured:
                kf.frame = new_frame
            sel.frame = new_frame

    def _update_move(self, pos: QPointF):
        delta = self._drag.delta(self.cursor().pos())
        if delta is None:
            return
        self._accum_dx += delta[0]
        delta_frames = round(self._accum_dx / self._frame_width)
        self._apply_moved_frames(self._start_frames, delta_frames)
        self._refresh_interpolation()
        self.repaint()

    def _confirm_transform(self):
        start_frames = (
            self._start_frames if self._transform_mode == "move" else self._scale_start_frames
        )
        # Collect how every captured keyframe actually moved (or stayed put).
        move_actions: list[tuple] = []
        for sel, captured, start_frame in zip(
            self.selected, self._drag_keyframes, start_frames
        ):
            new_frame = sel.frame
            if new_frame == start_frame:
                continue
            for channel, kf in captured:
                move_actions.append(
                    KeyframeCommand.record_move(
                        sel.obj, channel, kf, start_frame, new_frame
                    )
                )
        self._transform_mode = None
        self._drag.end()
        self.releaseMouse()
        self.setCursor(Qt.ArrowCursor)
        self.setFocus()
        self.status_message.emit("Ready")
        # Victims are pre-existing keyframes that were removed because a
        # dragged keyframe landed on the same frame. Capture them here so
        # undoing the move puts them back.
        victim_actions = self._merge_destination_keyframes()
        sub_commands = []
        if self._pending_insert_cmd:
            sub_commands.append(KeyframeCommand(self._pending_insert_cmd))
            self._pending_insert_cmd = None
        if move_actions or victim_actions:
            sub_commands.append(KeyframeCommand(move_actions + victim_actions))
        # Push BEFORE the selection sync so the recorded selection is the
        # pre-transform one (undo restores it).
        if sub_commands:
            self.history.push(CompoundCommand(sub_commands))
        self._sync_keyframe_selection()
        self._refresh_interpolation()
        self.update()

    def _merge_destination_keyframes(self):
        """After a move/scale, if a dragged keyframe lands exactly on top of an
        existing keyframe (same object, same frame), the dragged keyframe
        dominates: it overrides the victim so only one keyframe remains.

        Returns a list of "delete" actions for every removed victim so the
        move command can restore them on undo (forward re-applies the delete,
        backward re-inserts the victim)."""
        victim_actions: list[tuple] = []
        for sel, captured in zip(self.selected, self._drag_keyframes):
            obj = sel.obj
            dest = sel.frame
            if not captured:
                continue
            # remove every keyframe currently sitting at the destination frame
            for channel in list(obj.keyframes.keys()):
                removed = [
                    kf
                    for kf in obj.keyframes.get(channel, [])
                    if kf.frame == dest and kf not in [c for _, c in captured]
                ]
                for kf in removed:
                    victim_actions.append(
                        KeyframeCommand.record_delete(obj, channel, kf)
                    )
                obj.keyframes[channel] = [
                    kf for kf in obj.keyframes.get(channel, []) if kf.frame != dest
                ]
            # re-insert the dragged keyframes at the destination
            for channel, kf in captured:
                obj.keyframes.setdefault(channel, [])
                ch = obj.keyframes[channel]
                ch[:] = [existing for existing in ch if existing is not kf]
                ch.append(kf)
                ch.sort(key=lambda x: x.frame)
        return victim_actions

    def _cancel_transform(self):
        start_frames = (
            self._start_frames if self._transform_mode == "move" else self._scale_start_frames
        )
        for sel, captured, start_frame in zip(
            self.selected, self._drag_keyframes, start_frames
        ):
            for _, kf in captured:
                kf.frame = start_frame
            sel.frame = start_frame
        self._transform_mode = None
        self._drag.end()
        self.releaseMouse()
        self.setCursor(Qt.ArrowCursor)
        self.setFocus()
        self.status_message.emit("Ready")
        # A cancelled transform keeps any duplicates that were inserted (the
        # user pressed Esc to back out of the drag, not to drop the dupes).
        if self._pending_insert_cmd:
            self.history.push(KeyframeCommand(self._pending_insert_cmd))
            self._pending_insert_cmd = None
        self._sync_keyframe_selection()
        self._refresh_interpolation()
        self.update()

    # ------------------------------------------------------------------ #
    # Scale (S) - pivot is the playhead position
    # ------------------------------------------------------------------ #
    def _start_scale(self):
        self._pending_insert_cmd = None
        self._transform_mode = "scale"
        self._scale_mid = self.scene.current_frame  # pivot = playhead
        self._drag_start = QPointF(self.cursor().pos())
        frames = [sel.frame for sel in self.selected]
        self._scale_start_frames = list(frames)
        self._scale_start_dists = [f - self._scale_mid for f in frames]
        self._drag_keyframes = [list(sel.iterate()) for sel in self.selected]

        # Global X of the playhead pivot. The scale factor is measured against
        # the *initial* cursor distance from this pivot, so the selection does
        # not jump when the transform starts (factor starts at 1.0).
        pivot_widget_x = self._frame_to_x(self._scale_mid)
        origin_global_x = self.mapToGlobal(self.rect().topLeft()).x()
        self._scale_pivot_global_x = origin_global_x + pivot_widget_x
        self._scale_factor = 1.0
        self._accum_dx = 0.0
        self._drag.begin("cross")
        self.grabMouse()
        self.status_message.emit(
            "Scale keyframes (pivot: playhead): move horizontally, click to confirm, Esc to cancel"
        )
        self.update()

    def _update_scale(self, pos: QPointF):
        if not self._scale_start_dists:
            return
        delta = self._drag.delta(self.cursor().pos())
        if delta is None:
            return
        self._accum_dx += delta[0]
        pivot_x = self._scale_pivot_global_x

        start_dist = self._drag_start.x() - pivot_x
        cur_dist = start_dist + self._accum_dx
        if start_dist == 0:
            factor = 1.0
        else:
            factor = abs(cur_dist) / abs(start_dist)
            # keep the sign of the offset so a flip across the pivot inverts
            factor = factor if (start_dist * cur_dist) >= 0 else -factor
        self._scale_factor = min(max(factor, -10.0), 10.0)

        for sel, captured, start_frame, dist in zip(
            self.selected,
            self._drag_keyframes,
            self._scale_start_frames,
            self._scale_start_dists,
        ):
            new_frame = round(self._scale_mid + dist * self._scale_factor)
            for _, kf in captured:
                kf.frame = new_frame
            sel.frame = new_frame
        self._refresh_interpolation()
        self.repaint()

    # ------------------------------------------------------------------ #
    # Interpolation menu (T)
    # ------------------------------------------------------------------ #
    def _selection_popup_pos(self):
        """Global screen position for the interpolation menu: the cursor."""
        return self.cursor().pos()

    def _open_interpolation_menu(self):
        from ui.menus import StripeMenu
        menu = StripeMenu()
        menu.add_section("Interpolation Mode")

        current_modes = {sel.mode() for sel in self.selected if sel.mode() is not None}

        for mode, label in self._MODES:
            menu.add_action(
                label,
                checkable=True,
                checked=current_modes == {mode},
                data=mode,
            )

        chosen = menu.exec(self._selection_popup_pos(), trigger_key=Qt.Key_T)
        if chosen is None:
            return
        mode = chosen.data
        changes = []
        for sel in self.selected:
            for channel, kf in sel.iterate():
                if kf.interpolation == mode:
                    continue
                changes.append(
                    KeyframeCommand.record_set_interp(
                        sel.obj, channel, kf, kf.interpolation, mode
                    )
                )
        if changes:
            self.history.push(KeyframeCommand(changes))
            for sel in self.selected:
                for _, kf in sel.iterate():
                    kf.interpolation = mode
        self._refresh_interpolation()
        self.status_message.emit(f"Interpolation set to: {mode}")
        self.update()

    # ------------------------------------------------------------------ #
    # Delete
    # ------------------------------------------------------------------ #
    def _delete_selected(self):
        changes = []
        for sel in self.selected:
            for channel, kf in sel.iterate():
                changes.append(
                    KeyframeCommand.record_delete(sel.obj, channel, kf)
                )
                sel.obj.remove_keyframe(kf.frame, channel)
        if changes:
            self.history.push(KeyframeCommand(changes))
        self.selected.clear()
        self._sync_keyframe_selection()
        self._refresh_interpolation()
        self.status_message.emit("Keyframe deleted")
        self.update()


class TimelineTransport(QWidget):
    """Playback transport bar that lives in a dock's header.

    This is only a *control panel*: it reports button clicks (toggle_requested)
    and displays the current play state / frame. The actual playback timer is
    owned by the main window, so playback works even if no timeline dock is
    shown.
    """

    toggle_requested = Signal()

    def __init__(self, scene: Scene):
        super().__init__()
        self.scene = scene

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.play_button = QPushButton("Play")
        self.play_button.setFixedSize(70, 20)
        self.play_button.clicked.connect(self.toggle_requested.emit)
        layout.addWidget(self.play_button)

        self.frame_label = QLabel("Frame: 0")
        self.frame_label.setStyleSheet("color: #c0c0c0; font-size: 11px;")
        layout.addWidget(self.frame_label)

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")

    def set_frame(self, frame: int) -> None:
        self.frame_label.setText(f"Frame: {frame}")
