from __future__ import annotations

import weakref

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QTimer
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QPainterPath,
    QTransform,
    QLinearGradient,
    QImage,
    QCursor,
)
from PySide6.QtWidgets import QWidget, QLineEdit

from core.commands import CompoundCommand, PropertyCommand, ReorderCommand
from core.model import Scene, SceneObject
from core.selection import SelectionState
from core.history import History
from ui.theme import Theme
from ui.stripes import StripeShader
from ui.menus import StripeMenu, stripe_menu_open
from ui.relative_drag import RelativeDrag

from pathlib import Path
import math
from PySide6.QtSvg import QSvgRenderer

_ICONS_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
_svg_cache: dict = {}


def _load_svg(name: str):
    if name not in _svg_cache:
        path = _ICONS_DIR / name
        if path.exists():
            renderer = QSvgRenderer(str(path))
            if renderer.isValid():
                _svg_cache[name] = renderer
    return _svg_cache.get(name)


class _InlineRenameEdit(QLineEdit):
    cancelled = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.committed.emit(self.text())
            return
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


class OutlinerWidget(QWidget):
    ROW_HEIGHT = 30
    PADDING = 6
    THUMB_SIZE = 22
    BUTTON_SIZE = 18
    INDENT = 14
    ARROW_SIZE = 18
    THUMB_GAP = 4
    ROW_BG_OPACITY = 0.0
    # Minimum cursor travel (px) before a press on a row turns into a
    # reorder-drag. Below this, releasing the mouse is just a click.
    DRAG_START_DISTANCE = 6

    selection_changed = Signal(list)
    object_changed = Signal()
    object_hovered = Signal(object)

    def __init__(self, scene: Scene, history: History):
        super().__init__()
        self.scene = scene
        self.history = history
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._advance_animation)
        self._anim_timer.start()
        self.setMinimumWidth(160)
        self.setMinimumHeight(100)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._mouse_hover = False

        # --- layer drag / reorder state ---
        self._drag_obj: SceneObject | None = None
        self._drag_mode: str | None = None  # "lmb" | "g"
        self._drag_mouse_y = 0.0
        self._drag_offset = 0.0
        self._drag_active = False
        self._drag_sel_objs: list[SceneObject] = []
        # The row currently targeted as a "make into a child of" drop target
        # (obj, depth, parent_list), or None when dropping as a sibling.
        self._drag_over = None
        # Position of the row that was actually grabbed within
        # `_drag_sel_objs` (stack order). Lets the ghost stack and the drop
        # index track the cursor correctly even when the grabbed row isn't
        # the topmost item in a multi-selection.
        self._drag_anchor_index = 0
        self._drag_collapsed: set[SceneObject] = set()
        # Frozen snapshot of the display taken at grab time (before the
        # dragged subtree is collapsed). Used so the layout doesn't move:
        # every non-dragged row keeps its original slot and the grabbed rows
        # leave a blank gap behind until the grab ends.
        self._drag_base_rows: list[tuple] = []
        self._drag_hidden: set[SceneObject] = set()
        self._drag_layout_snapshot: list[tuple] = []
        # Shared relative-drag controller: hides + anchors the real cursor so
        # a reorder never hits a screen edge / stops at the widget boundary,
        # and measures relative motion the ghost row can follow. It draws no
        # glyph (the row ghost is the visual feedback).
        self._drag = RelativeDrag(self)
        # Widget-local y at grab time, so accumulated relative dy maps back to
        # a stable row target even though the real cursor is pinned at centre.
        self._drag_grab_y = 0.0
        self._accum_dy = 0.0

        # --- click-vs-drag distinction ---
        # A plain mouse press only records a "pending" drag candidate; the
        # actual reorder-drag (and, for an already multi-selected row,
        # whether the whole selection moves together) only begins once the
        # cursor has travelled past DRAG_START_DISTANCE. This keeps a plain
        # click from ever looking like - or briefly acting like - a drag.
        self._pending_obj: SceneObject | None = None
        self._pending_multi = False
        self._pending_press_pos: QPointF | None = None

        # --- eye/lock/mask "paint" drag state ---
        # Lets you toggle visibility/lock/mask for a whole run of layers by
        # pressing on one row's icon and dragging up/down across others,
        # instead of clicking every row individually.
        self._paint_active = False
        self._paint_kind: str | None = None
        self._paint_value: bool | None = None
        self._paint_seen: set[SceneObject] = set()

        # --- inline rename state ---
        self._rename_editor: QLineEdit | None = None
        self._rename_obj: SceneObject | None = None

        # Per-instance hover state (separate from the global shared selection).
        self._hovered_object: SceneObject | None = None

        # Last object explicitly (plain- or shift-) clicked. Used as the
        # starting point for Ctrl-click range selection, Krita-style.
        self._selection_anchor: SceneObject | None = None

        # The horizontal accent-gradient background and the vertical fade
        # mask used in the selection-row stripe effect only depend on the
        # row's pixel size, not on the animation offset. Cache them per size.
        self._bg_cache_size = None
        self._bg_cache_img = None
        self._vmask_cache_size = None
        self._vmask_cache_img = None

        # Tracks previously selected objects so unselected items get their
        # lingering animated states immediately redrawn and cleared.
        self._previously_selected: set[SceneObject] = set()

        # Baked row thumbnails, keyed per object (WeakKeyDictionary so old
        # objects replaced by an undo deep-copy drop out on their own). The
        # render reflects the subtree's real transforms (including position),
        # so it must be invalidated - and rebuilt - after any discrete scene
        # change, but left untouched while the stage is mid-drag or playback
        # is running to avoid a rebuild every frame.
        self._thumb_cache = weakref.WeakKeyDictionary()
        self._row_bg_cache = weakref.WeakKeyDictionary()
        self._thumb_frozen = False
        self.object_changed.connect(self.invalidate_thumbnails)
        # Prerendered mask-icon images keyed by (svg name, devicePixelRatio).
        self._icon_img_cache: dict = {}

    @property
    def selected_object(self) -> SceneObject | None:
        return SelectionState.selected()[0] if SelectionState.selected() else None

    @selected_object.setter
    def selected_object(self, value: SceneObject | None) -> None:
        if value is not None:
            SelectionState.set_selected([value])
        else:
            SelectionState.clear_selected()

    @property
    def selected_objects(self) -> list[SceneObject]:
        return SelectionState.selected()

    @property
    def hovered_object(self) -> SceneObject | None:
        return self._hovered_object

    @hovered_object.setter
    def hovered_object(self, value: SceneObject | None) -> None:
        if value is self._hovered_object:
            return
        old_hovered = self._hovered_object
        self._hovered_object = value
        for i, (obj, _d, _pl) in enumerate(self._display_rows()):
            if obj is old_hovered or obj is value:
                self.update(self._row_rect(i).toRect())

    def _set_selected_objects(self, objects: list[SceneObject]) -> None:
        SelectionState.set_selected(list(objects))
        for outliner in self._all_outliners():
            outliner.update()

    def _all_outliners(self):
        from PySide6.QtWidgets import QApplication
        outliners = []
        for widget in QApplication.topLevelWidgets():
            for child in widget.findChildren(OutlinerWidget):
                outliners.append(child)
        return outliners

    def _update_selection(self, obj: SceneObject | None, additive: bool = False) -> None:
        """Update the global selection. If additive (Shift held), toggle obj."""
        if additive:
            if obj is not None:
                SelectionState.toggle(obj)
        else:
            if obj is not None:
                SelectionState.set_selected([obj])
            else:
                SelectionState.clear_selected()

        self.selection_changed.emit(SelectionState.selected())

        # Force redraw across outliners
        for outliner in self._all_outliners():
            outliner.update()

    def _selection_targets(self, obj: SceneObject) -> list[SceneObject]:
        """Objects an icon-toggle on `obj` should apply to: the whole current
        selection when `obj` is part of a multi-selection, otherwise just
        `obj` itself. This is what makes toggling visibility/lock/mask for a
        handful of already-selected layers a single click instead of one
        click per layer."""
        sel = SelectionState.selected()
        if obj in sel and len(sel) > 1:
            return list(sel)
        return [obj]

    def _range_select(self, obj: SceneObject) -> None:
        """Add every layer between the last selection anchor and `obj`,
        inclusive, to the current selection (Ctrl-click range select).
        Additive: anything already selected (e.g. via Shift-click) stays
        selected, so Shift can pick out separate zones and Ctrl can fill
        in ranges between/around them without wiping the rest. This only
        ever changes *selection*, never layer order."""
        stack = self._stack_objects()

        if self._selection_anchor is None or self._selection_anchor not in stack or obj not in stack:
            # No usable anchor yet - add just this row to whatever's
            # already selected, and start the anchor here so the next
            # Shift-click has one to use.
            existing = set(self.selected_objects)
            combined = [o for o in stack if o in existing or o is obj]
            self._set_selected_objects(combined)
            self._selection_anchor = obj
            self.selection_changed.emit(SelectionState.selected())
            return

        i0 = stack.index(self._selection_anchor)
        i1 = stack.index(obj)
        lo, hi = min(i0, i1), max(i0, i1)
        range_objs = set(stack[lo:hi + 1])
        existing = set(self.selected_objects)
        combined = [o for o in stack if o in existing or o in range_objs]
        self._set_selected_objects(combined)
        self.selection_changed.emit(SelectionState.selected())
        # Deliberately leave the anchor unchanged, so repeated Shift-clicks
        # keep extending/shrinking the range from the same starting point.

    def _advance_animation(self):
        current_sel = set(SelectionState.selected())

        # 1. Identify any objects that were unselected since the last tick
        #    and force a repaint on their rows so the animation stops frozen.
        unselected = self._previously_selected - current_sel
        if unselected:
            for i, (obj, _depth, _pl) in enumerate(self._display_rows()):
                if obj in unselected:
                    self.update(self._row_rect(i).toRect())

        # Update cache for the next tick
        self._previously_selected = current_sel

        # 2. If nothing is currently selected and nothing is hovered, stop
        #    here - there's no stripe animation running for this widget.
        if not current_sel and self._hovered_object is None:
            return

        # 3. Repaint active animated rows for currently selected objects.
        for i, (obj, _depth, _pl) in enumerate(self._display_rows()):
            if obj in current_sel:
                self.update(self._row_rect(i).toRect())

        # 4. Keep the hovered row's stripe animation ticking too (it uses
        #    the same moving-stripe shader as selection, just tinted gray).
        if self._hovered_object is not None and self._hovered_object not in current_sel:
            for i, (obj, _depth, _pl) in enumerate(self._display_rows()):
                if obj is self._hovered_object:
                    self.update(self._row_rect(i).toRect())
                    break

    def _is_selected(self, obj: SceneObject) -> bool:
        return obj in SelectionState.selected()

    def _stack_objects(self):
        return [o for o, _, _ in self._display_rows()]

    # ------------------------------------------------------------------ #
    # Tree display
    # ------------------------------------------------------------------ #
    def _display_rows(self):
        """Visible outliner rows as (obj, depth, parent_list), top-most layer
        first. `parent_list` is the list the row lives in (scene.objects or a
        parent's children list), so reordering a row also knows where to put
        it back. Collapsed parents hide their children."""
        rows = []

        def walk(objs, depth, parent_list):
            for o in reversed(objs):
                rows.append((o, depth, parent_list))
                if o.expanded:
                    walk(o.children, depth + 1, o.children)

        walk(self.scene.objects, 0, self.scene.objects)
        return rows

    def _children_owner(self, p_list):
        """The object whose `children` list is `p_list`, or None when it is
        the scene root."""
        if p_list is self.scene.objects:
            return None
        for o in self.scene.iter_objects():
            if o.children is p_list:
                return o
        return None

    def _depth_of(self, obj: SceneObject) -> int:
        """The tree depth of `obj` (0 = direct child of the scene root)."""
        d = 0
        cur = self.scene.find_parent(obj)
        while cur is not None:
            d += 1
            cur = self.scene.find_parent(cur)
        return d

    def _would_create_cycle(self, drag_objs: list[SceneObject], parent_list) -> bool:
        owner = self._children_owner(parent_list)
        if owner is None:
            return False
        for d in drag_objs:
            cur = owner
            while cur is not None:
                if cur is d:
                    return True
                cur = self.scene.find_parent(cur)
        return False

    def _row_rect(self, i: int) -> QRectF:
        return QRectF(0, i * self.ROW_HEIGHT, self.width(), self.ROW_HEIGHT)

    def _thumb_rect(self, row: QRectF, depth: int) -> QRectF:
        left = row.left() + self.PADDING + depth * self.INDENT + self.ARROW_SIZE + self.THUMB_GAP
        return QRectF(
            left,
            row.center().y() - self.THUMB_SIZE / 2,
            self.THUMB_SIZE,
            self.THUMB_SIZE,
        )

    def _arrow_rect(self, row: QRectF, depth: int) -> QRectF:
        x = row.left() + self.PADDING + depth * self.INDENT
        y = row.center().y() - self.ARROW_SIZE / 2
        return QRectF(x, y, self.ARROW_SIZE, self.ARROW_SIZE)

    def _button_rects(self, row: QRectF):
        gap = 4
        y = row.center().y() - self.BUTTON_SIZE / 2
        right_x = self.width() - self.PADDING - self.BUTTON_SIZE
        eye_x = right_x
        mask_x = right_x - (self.BUTTON_SIZE + gap)
        lock_x = right_x - (self.BUTTON_SIZE + gap) * 2
        return (
            QRectF(eye_x, y, self.BUTTON_SIZE, self.BUTTON_SIZE),
            QRectF(mask_x, y, self.BUTTON_SIZE, self.BUTTON_SIZE),
            QRectF(lock_x, y, self.BUTTON_SIZE, self.BUTTON_SIZE),
        )

    def _eye_rect(self, row: QRectF) -> QRectF:
        return self._button_rects(row)[0]

    def _mask_rect(self, row: QRectF) -> QRectF:
        return self._button_rects(row)[1]

    def _lock_rect(self, row: QRectF) -> QRectF:
        return self._button_rects(row)[2]

    # ------------------------------------------------------------------ #
    # Click hitboxes for the arrow/eye/mask/lock buttons. These are kept
    # separate from the small rects above (which size and place the drawn
    # icons themselves): the icon stays the same visually, but the
    # clickable area is stretched to the row's full height and, for
    # eye/mask/lock, out to touch its neighbors, so there's no dead strip
    # above/below an icon or in the gap between two adjacent ones.
    # ------------------------------------------------------------------ #
    def _arrow_hit_rect(self, row: QRectF, depth: int) -> QRectF:
        arrow = self._arrow_rect(row, depth)
        # Stretch the hitbox left to the row's left edge (the expander arrow
        # lives at the far-left start of the row's indentation, so clicking
        # anywhere in that strip toggles the row's expansion).
        return QRectF(row.left(), row.top(), arrow.right() - row.left(), row.height())

    def _button_hit_rects(self, row: QRectF):
        gap = 4
        half_gap = gap / 2.0
        eye_v, mask_v, lock_v = self._button_rects(row)
        # Touching midpoints between neighboring icons close the gap;
        # the two outer edges (left of lock, right of eye) just extend
        # by half the gap so all three feel evenly generous.
        lock_mask_boundary = lock_v.right() + half_gap
        mask_eye_boundary = mask_v.right() + half_gap
        eye_rect = QRectF(
            mask_eye_boundary, row.top(),
            (eye_v.right() + half_gap) - mask_eye_boundary, row.height(),
        )
        mask_rect = QRectF(
            lock_mask_boundary, row.top(),
            mask_eye_boundary - lock_mask_boundary, row.height(),
        )
        lock_rect = QRectF(
            lock_v.left() - half_gap, row.top(),
            lock_mask_boundary - (lock_v.left() - half_gap), row.height(),
        )
        return eye_rect, mask_rect, lock_rect

    def _eye_hit_rect(self, row: QRectF) -> QRectF:
        return self._button_hit_rects(row)[0]

    def _mask_hit_rect(self, row: QRectF) -> QRectF:
        return self._button_hit_rects(row)[1]

    def _lock_hit_rect(self, row: QRectF) -> QRectF:
        return self._button_hit_rects(row)[2]

    def _name_rect(self, row: QRectF, depth: int) -> QRectF:
        # FIX: the title used to stretch to `self._eye_rect(row).left()`,
        # but the eye button is the RIGHTMOST of the three icon buttons
        # (order left-to-right is lock, mask, eye - see `_button_rects`).
        # That meant the title's right edge crept all the way to the eye
        # icon's left edge, overlapping the lock and mask buttons that sit
        # in between. The title should stop before the LEFTMOST button
        # (lock) instead, so it spans the full available width - from the
        # thumbnail on the left up to the button row on the right - without
        # ever drawing under any of the three icons.
        thumb = self._thumb_rect(row, depth)
        name_x = thumb.right() + 4
        name_right = self._lock_rect(row).left() - 4
        width = max(0.0, name_right - name_x)
        return QRectF(name_x, row.top(), width, row.height())

    # ------------------------------------------------------------------ #
    # Layer drag / reorder with reparenting
    # ------------------------------------------------------------------ #
    def _drag_set(self) -> set[SceneObject]:
        return set(self._drag_sel_objs) if self._drag_sel_objs else {self._drag_obj}

    def _others(self) -> list:
        """Non-dragged rows in display (top-first) order, packed. This is the
        ordering the drop slot is computed against and what the final flattened
        layout is built from."""
        drag_set = self._drag_set()
        return [r for r in self._display_rows() if r[0] not in drag_set]

    def _snapped_boundary(self) -> int:
        """The nearest row-boundary index in the frozen drag layout for the
        cursor. The divider is drawn at this boundary (between two rows),
        never tracking the mouse continuously. The frozen layout keeps every
        row at its original slot, so the grabbed block leaves a blank gap
        behind (no rows are compressed while dragging)."""
        base = self._drag_base_rows
        n = len(base)
        if n == 0:
            return 0
        y = max(0.0, min(float(self.height()), self._drag_mouse_y))
        k = int((y + self.ROW_HEIGHT / 2) / self.ROW_HEIGHT)
        return max(0, min(n + 1, k))

    def _original_drop_index(self) -> int:
        """Packed slot the dragged block originally occupied: the number of
        non-dragged rows displayed above its topmost row. Aiming the drop
        back at exactly this slot means the block goes right back where it
        came from."""
        count = 0
        for obj, _d, _pl in self._drag_base_rows:
            if obj in self._drag_hidden:
                break
            count += 1
        return count

    def _drop_index(self) -> int:
        """Insertion slot for the dragged block in the packed list of the
        other (non-dragged) rows. The boundary is snapped in the *frozen*
        layout (where the grabbed block's rows still occupy slots, leaving
        blank gaps), then converted to a packed index by skipping the hidden
        rows that precede it. Every boundary inside the old footprint maps to
        the same slot, so there are no usable drop "gaps" within it."""
        base = self._drag_base_rows
        k = self._snapped_boundary()
        others = self._others()
        hidden = self._drag_hidden
        hidden_before = sum(1 for r in base[:k] if r[0] in hidden)
        return max(0, min(len(others), k - hidden_before))

    def _update_drag_over(self):
        """Figure out which row, if any, is currently a "make child of this"
        drop target (the middle band of a row, in its frozen slot). Cleared to
        None otherwise, in which case a snapped sibling divider is shown."""
        self._drag_over = None
        if not self._drag_active or not self._drag_set():
            return
        drag_set = self._drag_set()
        y = self._drag_mouse_y
        band = self.ROW_HEIGHT * 0.25
        for obj, depth, p_list, slot in self._drag_layout():
            r = self._row_rect(slot)
            if r.center().y() - band <= y <= r.center().y() + band:
                if self._would_create_cycle(list(drag_set), obj.children):
                    continue
                self._drag_over = (obj, depth, p_list)
                return

    def _compute_draft_flat(self):
        """The desired flattened rows after the current drop: (flat, valid).

        `flat` is a list of (obj, depth, parent_list).

        - In "over a row" mode (``_drag_over`` set) the dragged block becomes
          the highest (topmost displayed) child of that row: it is inserted
          right after the parent row itself.
        - Otherwise the block is inserted as a sibling at the slot matching
          the cursor, re-using the parent list of the row above the slot so a
          drop between two children of the same parent stays inside that
          parent.

        Returns the unchanged display with valid=False when the drop would
        create a parent cycle."""
        drag_order = self._drag_sel_objs or ([self._drag_obj] if self._drag_obj else [])
        others = self._others()
        if not drag_order:
            return others, True

        drop = None
        ctx_parent = None
        ctx_depth = None

        if self._drag_over is not None:
            target_obj, target_depth, _ = self._drag_over
            if self._would_create_cycle(drag_order, target_obj.children):
                return self._display_rows(), False
            ctx_parent = target_obj.children
            ctx_depth = target_depth + 1
            for i, (o, _d, _pl) in enumerate(others):
                if o is target_obj:
                    drop = i + 1
                    break
            if drop is None:
                return self._display_rows(), False
        else:
            drop = self._drop_index()
            above = others[drop - 1] if drop > 0 else None
            if above is None:
                ctx_parent = self.scene.objects
                ctx_depth = 0
            elif above[0].expanded and above[0].children:
                # The gap right below an *unfolded* container sits between it
                # and its first child, so a drop there becomes the container's
                # new first child.
                ctx_parent = above[0].children
                ctx_depth = above[1] + 1
            else:
                # Re-use the parent of the row above the slot: a drop below a
                # folded container (or the container row itself) lands as a
                # sibling under it, while a drop below its last child keeps
                # the layer inside it as the new last child.
                ctx_parent = above[2]
                ctx_depth = above[1]
            if self._would_create_cycle(drag_order, ctx_parent):
                return self._display_rows(), False

        rows = others[:drop] + [(o, ctx_depth, ctx_parent) for o in drag_order] + others[drop:]
        return rows, True

    def _apply_drop(self):
        if self._drag_obj is None and not self._drag_sel_objs:
            return
        drag_order = self._drag_sel_objs or [self._drag_obj]
        if self._drag_over is None and self._drop_index() == self._original_drop_index():
            # Dropped back into the gap where it originally sat - this isn't
            # a move at all, it's a cancel. Nothing was changed, so no undo
            # step is needed.
            self._cancel_drag()
            return
        flat, valid = self._compute_draft_flat()
        if valid:
            if self._drag_over is not None:
                self._drag_over[0].expanded = True
            # Remember where things are now so reparented objects can be
            # re-based into their new parent's local space (no teleporting).
            old_parents = {d: self.scene.find_parent(d) for d in drag_order}
            old_world = {d: self._world_transform(d) for d in drag_order}

            def _local_values(o):
                t = o.transform
                return (t.x, t.y, t.rotation, t.scale_x, t.scale_y)

            # The reparent re-bases local transforms below; capture the old
            # values so the move can be undone/redone as one atomic step.
            old_local = {d: _local_values(d) for d in drag_order}

            # Capture old home + index for each dragged object before moving.
            moves = []
            for d in drag_order:
                old_parent = self.scene.objects
                if old_parents[d] is not None:
                    old_parent = old_parents[d].children
                old_idx = old_parent.index(d) if d in old_parent else len(old_parent)
                moves.append((d, old_parent, old_idx))
            # Take the dragged rows out of their old homes first so a parent
            # that ends up with no displayed rows doesn't keep holding them.
            for d in drag_order:
                if d in self.scene.objects:
                    self.scene.objects.remove(d)
                else:
                    parent = self.scene.find_parent(d)
                    if parent is not None:
                        parent.children.remove(d)
            self._assign_flat(flat)
            for d in drag_order:
                if self.scene.find_parent(d) is not old_parents[d]:
                    self._set_local_from_world(d, old_world[d])
            # Capture new homes + indices after the move.
            final_moves = []
            for d, old_parent, old_idx in moves:
                new_parent = self.scene.objects
                if self.scene.find_parent(d) is not None:
                    new_parent = self.scene.find_parent(d).children
                new_idx = (
                    new_parent.index(d) if d in new_parent else len(new_parent)
                )
                final_moves.append((d, old_parent, old_idx, new_parent, new_idx))
            # A parent change re-bases local transforms so the object doesn't
            # teleport; that transform edit must be undone alongside the
            # structural move, or undo/redo leaves objects floating at the
            # wrong local offset under their old/new parent.
            transform_changes = {}
            for d in drag_order:
                new = _local_values(d)
                if new == old_local[d]:
                    continue
                attrs = {}
                for attr, old_v, new_v in zip(
                    ("x", "y", "rotation", "scale_x", "scale_y"),
                    old_local[d],
                    new,
                ):
                    if old_v != new_v:
                        attrs[f"transform.{attr}"] = (old_v, new_v)
                if attrs:
                    transform_changes[d.id] = attrs
            cmds = [ReorderCommand(final_moves)]
            if transform_changes:
                cmds.append(PropertyCommand(transform_changes))
            self.history.push(
                CompoundCommand(cmds) if len(cmds) > 1 else cmds[0]
            )
        self._restore_collapsed_items()
        self._clear_drag_layout()
        self._drag_obj = None
        self._drag_sel_objs = []
        self._drag_anchor_index = 0
        self._drag_active = False
        self._drag_mode = None
        self._drag_over = None
        self._accum_dy = 0.0
        self._drag.end()
        self.releaseMouse()
        self.object_changed.emit()
        self.update()

    # ------------------------------------------------------------------ #
    # Reparenting keeps objects visually in place by re-basing their local
    # transforms instead of letting them teleport.
    # ------------------------------------------------------------------ #
    def _object_transform(self, obj: SceneObject) -> QTransform:
        t = QTransform()
        tr = obj.transform
        t.translate(tr.x, tr.y)
        t.rotate(tr.rotation)
        t.scale(tr.scale_x, tr.scale_y)
        return t

    def _ancestor_world_transform(self, obj: SceneObject) -> QTransform:
        """Composed transform of everything above `obj` (its parent chain).

        Qt's QTransform `*` stacks the RIGHT operand outermost, so the product
        the app builds for a world transform is `leaf * parent * ... * root`
        (see stage.py's `_world_transform`). Build the ancestor part the same
        way: immediate parent on the left, root on the right."""
        t = QTransform()
        chain = []
        cur = self.scene.find_parent(obj)
        while cur is not None:
            chain.append(cur)
            cur = self.scene.find_parent(cur)
        for o in chain:
            t = t * self._object_transform(o)
        return t

    def _world_transform(self, obj: SceneObject) -> QTransform:
        """Composed transform of `obj` and every ancestor (world space)."""
        return self._object_transform(obj) * self._ancestor_world_transform(obj)

    def _set_local_from_world(self, obj: SceneObject, world: QTransform) -> None:
        """Re-derive `obj`'s local transform so its world transform stays
        `world` under its *current* parent chain."""
        inv, ok = self._ancestor_world_transform(obj).inverted()
        if not ok:
            return
        m = world * inv
        tr = obj.transform
        tr.x = m.dx()
        tr.y = m.dy()
        tr.rotation = math.degrees(math.atan2(m.m12(), m.m11()))
        tr.scale_x = math.hypot(m.m11(), m.m12())
        tr.scale_y = math.hypot(m.m21(), m.m22())
        det = m.m11() * m.m22() - m.m21() * m.m12()
        if det < 0:
            tr.scale_y = -tr.scale_y

    def _assign_flat(self, flat):
        """Rebuild the tree so each parent's child list matches the order its
        rows appear in `flat` (which is top-first). Children hidden under a
        collapsed parent are preserved so they aren't silently dropped."""
        collected: dict[int, list] = {}
        for (obj, _depth, parent_list) in flat:
            key = id(parent_list)
            if key not in collected:
                collected[key] = [parent_list, []]
            collected[key][1].append(obj)
        for _key, (parent_list, objs) in collected.items():
            ordered = list(reversed(objs))
            if parent_list is self.scene.objects:
                self.scene.objects[:] = ordered
            else:
                hidden = [c for c in parent_list if c not in ordered]
                parent_list[:] = hidden + ordered

    def _drag_layout(self) -> list:
        """Rows to draw during a drag, from the frozen grab-time snapshot. Every
        non-dragged row keeps its original slot (blank edges stay where the
        grabbed rows were), and each entry carries its slot index for painting.
        The dragged block itself is drawn separately as a floating ghost."""
        return self._drag_layout_snapshot

    def _snapshot_layout(self):
        """Freeze the display layout at grab time, before collapsing the dragged
        subtree. Stores every non-dragged row with its original slot (so the
        list doesn't jump while dragging - the grabbed block simply leaves a
        blank gap behind), plus the set of rows hidden because they belong to
        a dragged subtree."""
        drag_set = self._drag_set()
        hidden = set()
        for d in drag_set:
            for o in d.iter_subtree():
                hidden.add(o)
        self._drag_hidden = hidden
        base = self._display_rows()
        self._drag_base_rows = base
        self._drag_layout_snapshot = [
            (o, d, pl, i) for i, (o, d, pl) in enumerate(base) if o not in hidden
        ]

    def _clear_drag_layout(self):
        self._drag_base_rows = []
        self._drag_hidden = set()
        self._drag_layout_snapshot = []

    def _collapse_dragged_items(self):
        """Fold every dragged object that was expanded, so its children don't
        visually trail behind while dragging. Their expansion is restored when
        the drag ends."""
        self._drag_collapsed.clear()
        for obj in self._drag_sel_objs:
            if obj.expanded:
                obj.expanded = False
                self._drag_collapsed.add(obj)

    def _restore_collapsed_items(self):
        for obj in self._drag_collapsed:
            obj.expanded = True
        self._drag_collapsed.clear()

    def _start_drag(self, obj: SceneObject, mode: str, grab_y: float):
        index = self._stack_objects().index(obj)
        row_top = index * self.ROW_HEIGHT
        self._drag_obj = obj
        self._drag_sel_objs = [obj]
        self._drag_anchor_index = 0
        self._drag_mode = mode
        self._drag_active = True
        self._drag_offset = grab_y - row_top
        self._drag_mouse_y = grab_y
        self._snapshot_layout()
        self._collapse_dragged_items()

        # In G-mode (keyboard drag) the object may not be selected yet; make
        # sure it is so the drag operates on it.
        if obj not in SelectionState.selected():
            SelectionState.set_selected([obj])
            self.selection_changed.emit([obj])

        self._drag_grab_y = grab_y
        self._accum_dy = 0.0
        self._drag.begin("none")
        self.grabMouse()
        self.update()

    def _start_multi_drag(self, clicked_obj: SceneObject, grab_y: float, mode: str = "lmb"):
        """Begin dragging every selected row as one block.

        Anchored on ``clicked_obj`` (the row the mouse actually grabbed),
        not just the first selected item, so the ghost stack and drop
        target track the cursor correctly no matter which selected row you
        press the mouse on. ``mode`` is "lmb" (released with a mouse click)
        or "g" (released by clicking again while the selection is grabbed).
        """
        stack = self._stack_objects()
        # Keep the dragged block in on-screen (stack) order rather than
        # selection order, so the ghost rows line up with each other.
        selected = [o for o in stack if o in SelectionState.selected()]
        if not selected or clicked_obj not in selected:
            return
        anchor_index = selected.index(clicked_obj)
        row_top = stack.index(clicked_obj) * self.ROW_HEIGHT
        self._drag_obj = clicked_obj
        self._drag_sel_objs = selected
        self._drag_anchor_index = anchor_index
        self._drag_mode = mode
        self._drag_active = True
        self._drag_offset = grab_y - row_top
        self._drag_mouse_y = grab_y
        self._snapshot_layout()
        self._collapse_dragged_items()
        self._drag_grab_y = grab_y
        self._accum_dy = 0.0
        self._drag.begin("none")
        self.grabMouse()
        self.update()

    def _cancel_drag(self):
        self._restore_collapsed_items()
        self._clear_drag_layout()
        self._drag_obj = None
        self._drag_sel_objs = []
        self._drag_anchor_index = 0
        self._drag_active = False
        self._drag_mode = None
        self._drag_over = None
        self._accum_dy = 0.0
        self._drag.end()
        self.releaseMouse()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.fillRect(self.rect(), QColor(35, 35, 35))

        font = painter.font()

        dragging = self._drag_active and self._drag_obj is not None
        if dragging:
            for obj, depth, _parent_list, slot in self._drag_layout():
                self._draw_row(painter, obj, self._row_rect(slot), depth)
        else:
            for i, (obj, depth, _parent_list) in enumerate(self._display_rows()):
                self._draw_row(painter, obj, self._row_rect(i), depth)

        # drop indicator (parent highlight or sibling divider) goes on top of
        # the rows so the divider is visible in the gap between them
        if dragging:
            self._draw_drag_ghost(painter)
            self._draw_drop_indicator(painter)

        painter.end()

    def _draw_drop_indicator(self, painter: QPainter):
        if self._drag_over is not None:
            self._draw_parent_target_highlight(painter)
        else:
            self._draw_divider(painter)

    def _draw_row(self, painter: QPainter, obj: SceneObject, row: QRectF, depth: int):
        # solid dark background so layers are opaque and the drop-preview box
        # underneath doesn't show through them
        painter.fillRect(row, QColor(35, 35, 35))

        if self._is_selected(obj):
            self._draw_selection_row(painter, row)
        elif obj is self._hovered_object and not self._drag_active:
            # Same idea as the stage's hover highlight (gray fill + gray
            # stripes) - just re-tuned for a flat list row.
            self._draw_hover_row(painter, row)

        if obj.children:
            # get mouse's position and arrow's hitbox
            mouse_pos = self.mapFromGlobal(QCursor.pos())
            arrow_hit = self._arrow_hit_rect(row, depth)

            # make a boolean that tells us if the arrow is hovered or not
            is_arrow_hovered = self._mouse_hover and arrow_hit.contains(mouse_pos)

            self._draw_arrow(
                painter,
                obj.expanded,
                self._arrow_rect(row, depth),
                is_hovered=is_arrow_hovered #pass that boolean there for the arrow sprite to use
            )

        self._draw_row_bg(painter, obj, row, depth)

        thumb = self._thumb_rect(row, depth)
        self._draw_thumb(painter, obj, thumb)
        self._draw_eye_button(painter, obj, self._eye_rect(row))
        self._draw_mask_button(painter, obj, self._mask_rect(row))
        self._draw_lock_button(painter, obj, self._lock_rect(row))

        # Skip drawing the name while it's being edited inline - the
        # QLineEdit sits on top of this exact rect.
        if self._rename_editor is not None and self._rename_obj is obj:
            painter.setPen(QPen(QColor(50, 50, 50), 1))
            painter.drawLine(
                int(row.left()), int(row.bottom()), int(row.right()), int(row.bottom())
            )
            return

        font = painter.font()
        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        name_rect = self._name_rect(row, depth)

        if self._is_selected(obj):
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, obj.name)
        else:
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, obj.name)

        painter.setPen(QPen(QColor(50, 50, 50), 1))
        painter.drawLine(
            int(row.left()), int(row.bottom()), int(row.right()), int(row.bottom())
        )

    def _draw_arrow(self, painter: QPainter, expanded: bool, rect: QRectF, is_hovered: bool):
        c = rect.center()
        s = 3.5
        path = QPainterPath()
        if expanded:
            name = "dropdown_expanded_hover.svg" if is_hovered else "dropdown_expanded.svg"
        else:
            name = "dropdown_folded_hover.svg" if is_hovered else "dropdown_folded.svg"

        renderer = _load_svg(name)
        if renderer is not None:

            renderer.render(painter, rect)
            return
        else:
            if expanded:
                path.moveTo(c.x() - s, c.y() - s + 2)
                path.lineTo(c.x() + s, c.y() - s + 2)
                path.lineTo(c.x(), c.y() + s - 1)
            else:
                path.moveTo(c.x() - s + 1, c.y() - s + 1)
                path.lineTo(c.x() + s - 1, c.y())
                path.lineTo(c.x() - s + 1, c.y() + s - 1)

        painter.save()
        path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(170, 170, 170))
        painter.drawPath(path)
        painter.restore()

    def _draw_divider(self, painter: QPainter):
        """Divider line drawn only between two rows (snapped to a row boundary
        in the frozen layout, never tracking the mouse continuously), with a
        gradient that fades out at the left/right edges. No divider is drawn
        over the dragged block's own old footprint - every boundary there maps
        back to the original slot (a cancel), not a real drop position."""
        if self._drop_index() == self._original_drop_index():
            return
        k = self._snapped_boundary()
        y = float(k * self.ROW_HEIGHT)

        def gradient(alpha: int) -> QLinearGradient:
            g = QLinearGradient(0, 0, float(self.width()), 0)
            c = QColor(Theme.ACCENT.red(), Theme.ACCENT.green(), Theme.ACCENT.blue(), 0)
            m = QColor(Theme.ACCENT.red(), Theme.ACCENT.green(), Theme.ACCENT.blue(), alpha)
            g.setColorAt(0.0, c)
            g.setColorAt(0.5, m)
            g.setColorAt(1.0, c)
            return g

        # soft glow band above and below the hard line
        painter.fillRect(QRectF(0, y - 3, self.width(), 6), gradient(45))
        painter.fillRect(QRectF(0, y - 1, self.width(), 2), gradient(255))

    def _draw_parent_target_highlight(self, painter: QPainter):
        if self._drag_over is None:
            return
        target_obj, _depth, _pl = self._drag_over
        for obj, _d, _pl2, slot in self._drag_layout():
            if obj is target_obj:
                r = self._row_rect(slot)
                path = QPainterPath()
                path.addRoundedRect(r.adjusted(2, 2, -2, -2), 5, 5)
                painter.setPen(QPen(Theme.ACCENT, 1))
                painter.setBrush(QColor(Theme.ACCENT.red(), Theme.ACCENT.green(), Theme.ACCENT.blue(), 40))
                painter.drawPath(path)
                return

    def _draw_drag_ghost(self, painter: QPainter):
        """Draw the dragged block as a floating ghost following the cursor,
        keeping the grabbed row locked to the mouse. It takes no space, so the
        rest of the list stays put while dragging."""
        if not self._drag_active or not self._drag_sel_objs:
            return
        ghost_top = max(0.0, self._drag_mouse_y - self._drag_offset)
        ghost_top -= self._drag_anchor_index * self.ROW_HEIGHT
        painter.save()
        painter.setOpacity(0.85)
        for i, obj in enumerate(self._drag_sel_objs):
            row = QRectF(0, ghost_top + i * self.ROW_HEIGHT, self.width(), self.ROW_HEIGHT)
            depth = self._depth_of(obj)
            self._draw_row(painter, obj, row, depth)
        painter.restore()

    def _get_selection_bg_images(self, w: int, h: int):
        """Return (bg_img, vmask_img) for the given size, building & caching
        them only when the size actually changes. Neither depends on the
        animation offset, so there's no reason to rebuild them every frame.
        """
        size = (w, h)
        if self._bg_cache_size == size and self._bg_cache_img is not None:
            return self._bg_cache_img, self._vmask_cache_img

        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(0, 0, 0, 0))
        ip = QPainter(img)
        ip.setRenderHint(QPainter.Antialiasing)
        hgrad = QLinearGradient(0, 0, w, 0)
        hgrad.setColorAt(0.0, Theme.ACCENT)
        hgrad.setColorAt(1.0, QColor(Theme.ACCENT.red(), Theme.ACCENT.green(), Theme.ACCENT.blue(), 0))
        ip.fillRect(img.rect(), QBrush(hgrad))
        ip.end()

        vmask = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        vmask.fill(QColor(0, 0, 0, 0))
        vp = QPainter(vmask)
        vp.setRenderHint(QPainter.Antialiasing)
        vgrad = QLinearGradient(0, 0, 0, h)
        vgrad.setColorAt(0.0, QColor(255, 255, 255, 255))
        vgrad.setColorAt(1.0, QColor(255, 255, 255, 0))
        vp.fillRect(vmask.rect(), QBrush(vgrad))
        vp.end()

        self._bg_cache_size = size
        self._bg_cache_img = img
        self._vmask_cache_size = size
        self._vmask_cache_img = vmask
        return img, vmask

    def _draw_selection_row(self, painter: QPainter, row: QRectF):
        w = int(row.width())
        h = int(row.height())

        # Static parts (horizontal accent gradient + vertical fade mask)
        # are cached per size instead of rebuilt every frame.
        img, vmask = self._get_selection_bg_images(w, h)

        # Only the moving stripes need to be redrawn each frame. The seamless
        # tile is cached & shared, so this is cheap.
        stripes = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        stripes.fill(QColor(0, 0, 0, 0))
        sp = QPainter(stripes)
        sp.setRenderHint(QPainter.Antialiasing)
        region = QPainterPath()
        region.addRect(0, 0, w, h)
        shader = StripeShader(color=Theme.ACCENT)
        shader.paint(sp, region, zoom=1.0)
        sp.end()

        # Apply the cached vertical fade mask via composition instead of
        # regenerating the gradient brush every frame.
        ip = QPainter(stripes)
        ip.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        ip.drawImage(0, 0, vmask)
        ip.end()

        painter.save()
        painter.setClipRect(row)
        painter.drawImage(int(row.left()), int(row.top()), img)
        painter.drawImage(int(row.left()), int(row.top()), stripes)
        painter.restore()

    def _draw_hover_row(self, painter: QPainter, row: QRectF):
        """Hover highlight for a row: a soft gray fill plus animated gray
        stripes, on top of the plain row background.

        This deliberately mirrors stage.py's ``_draw_hover_highlight`` in
        spirit (flat gray fill + ``StripeShader`` stripes over the hovered
        region) rather than in mechanics: the stage clips an arbitrary
        object path and adds a hairline outline, since it's highlighting a
        shape on an infinite canvas. A row is just a rect, so there's no
        path/clip math or outline needed - just fill + stripes, faded in
        alpha since a full-selection-strength highlight would be too loud
        for something you're only hovering over.
        """
        w = int(row.width())
        h = int(row.height())
        if w <= 0 or h <= 0:
            return

        layer = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        layer.fill(QColor(0, 0, 0, 0))
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.Antialiasing)

        hover_fill = QColor(190, 190, 190)
        hover_fill.setAlphaF(0.16)
        lp.fillRect(layer.rect(), hover_fill)

        hover_stripe = QColor(190, 190, 190)
        hover_stripe.setAlphaF(0.28)
        region = QPainterPath()
        region.addRect(0, 0, w, h)
        shader = StripeShader(color=hover_stripe)
        shader.paint(lp, region, zoom=1.0)
        lp.end()

        painter.save()
        painter.setClipRect(row)
        painter.drawImage(int(row.left()), int(row.top()), layer)
        painter.restore()

    def _thumb_fp(self, obj: SceneObject) -> int:
        """Fingerprint of everything that shapes ``obj``'s row thumbnail.

        Includes the full transform (translation, rotation, scale) of the
        whole subtree, plus the shape data and fill colors, so any move/edit
        invalidates the thumbnail. The expensive part is only paid at
        rebuild time; during a live stage drag or playback the outliner is
        ``_thumb_frozen`` and never computes this.
        """
        items: list = []
        t = obj.transform
        items.append(id(obj))
        items.append(id(obj.shape_data))
        items.append(hash(obj.color))
        items.append(obj.opacity)
        items.append(t.x)
        items.append(t.y)
        items.append(t.rotation)
        items.append(t.scale_x)
        items.append(t.scale_y)

        def walk(o: SceneObject) -> None:
            for c in o.children:
                ct = c.transform
                items.append(id(c))
                items.append(id(c.shape_data))
                items.append(hash(c.color))
                items.append(c.opacity)
                items.append(ct.x)
                items.append(ct.y)
                items.append(ct.rotation)
                items.append(ct.scale_x)
                items.append(ct.scale_y)
                walk(c)

        walk(obj)
        return hash(tuple(items))

    def _bake_thumb(self, shapes: list, bounds: QRectF, dpr: float) -> QImage:
        """Render the thumbnail shapes into a THUMB_SIZE x THUMB_SIZE image,
        baked at device-pixel-ratio resolution so drawing it back into the
        same-size row cell is a 1:1 blit (no upscaling, so no pixelation)."""
        size = self.THUMB_SIZE
        target = QRectF(0, 0, size, size)
        scale = min(
            target.width() / bounds.width() if bounds.width() > 0 else 1,
            target.height() / bounds.height() if bounds.height() > 0 else 1,
        )
        tx = target.center().x() - bounds.center().x() * scale
        ty = target.center().y() - bounds.center().y() * scale
        w = max(1, round(size * dpr))
        h = max(1, round(size * dpr))
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(dpr, dpr)
        p.translate(tx, ty)
        p.scale(scale, scale)
        p.setPen(Qt.NoPen)
        for shape_path, color, opacity in shapes:
            if color == "none":
                continue
            p.save()
            p.setOpacity(opacity)
            p.setBrush(QColor(color if color else "#cccccc"))
            p.drawPath(shape_path)
            p.restore()
        p.end()
        return img

    def _bake_row_bg(self, shapes: list, bounds: QRectF, width: int, height: int, dpr: float) -> QImage:
        """Render thumbnail shapes into a *width* x *height* image, scaled to
        fill the height so the shape overflows horizontally when needed.  The
        overflow is cropped by the clip-rect at draw time, giving a zoomed-in
        cropped look that is much larger than the small THUMB_SIZE icon."""
        target = QRectF(0, 0, width, height)
        scale = height / bounds.height() if bounds.height() > 0 else 1
        tx = target.center().x() - bounds.center().x() * scale
        ty = target.center().y() - bounds.center().y() * scale
        w = max(1, round(width * dpr))
        h = max(1, round(height * dpr))
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(dpr, dpr)
        p.translate(tx, ty)
        p.scale(scale, scale)
        p.setPen(Qt.NoPen)
        for shape_path, color, opacity in shapes:
            if color == "none":
                continue
            p.save()
            p.setOpacity(opacity)
            p.setBrush(QColor(color if color else "#cccccc"))
            p.drawPath(shape_path)
            p.restore()
        p.end()
        return img

    def invalidate_thumbnails(self):
        """Drop all cached thumbnails so the next paint rebuilds them.

        Discrete scene changes (stage transforms, undo/redo, own edits) call
        this; the 33 ms animation tick never does, so a live drag on the
        stage keeps rendering from the frozen cache instead of rebuilding a
        thumbnail every frame.
        """
        self._thumb_cache.clear()
        self._row_bg_cache.clear()
        self.update()

    def _draw_thumb(self, painter: QPainter, obj: SceneObject, rect: QRectF):
        dpr = self.devicePixelRatioF() or 1.0
        slot = self._thumb_cache.setdefault(obj, {})
        img = slot.get("img")
        if img is not None and slot.get("dpr") == dpr:
            # The outliner is frozen during a live stage drag or playback,
            # so blit whatever we already have instead of recomputing the
            # fingerprint (and rebuilding) every frame.
            if not self._thumb_frozen and slot.get("fp") != self._thumb_fp(obj):
                slot["img"] = None
                slot["path"] = None
                img = None
        if img is None:
            slot["fp"] = None if self._thumb_frozen else self._thumb_fp(obj)
            slot["dpr"] = dpr
            shapes = self._build_thumbnail_shapes(obj)
            path = None
            if shapes:
                merged = QPainterPath()
                for shape_path, _color, _op in shapes:
                    merged.addPath(shape_path)
                bounds = merged.boundingRect()
                if not bounds.isEmpty():
                    img = self._bake_thumb(shapes, bounds, dpr)
                    path = merged
            slot["img"] = img
            slot["path"] = path

        img = slot["img"]
        if img is None:
            if obj.is_container:
                self._draw_container_thumb(painter, obj, rect)
            return

        # Draw the selection outline first so it sits BEHIND the thumbnail
        # (no longer eating into the baked image's outer pixels).
        path = slot["path"]
        if self._is_selected(obj) and path is not None:
            bounds = path.boundingRect()
            scale = min(
                rect.width() / bounds.width() if bounds.width() > 0 else 1,
                rect.height() / bounds.height() if bounds.height() > 0 else 1,
            )
            tx = rect.center().x() - bounds.center().x() * scale
            ty = rect.center().y() - bounds.center().y() * scale
            painter.save()
            painter.translate(tx, ty)
            painter.scale(scale, scale)
            outline_pen = QPen(QColor(255, 255, 255), 3 / scale if scale > 0 else 1.5)
            outline_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(outline_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.restore()

        painter.drawImage(rect, img)

    def _draw_row_bg(self, painter: QPainter, obj: SceneObject, row: QRectF, depth: int):
        """Draw the baked thumbnail, cropped and enlarged, as a faint
        background wash across the row's name area (_name_rect). Drawn on top
        of any selection/hover stripes but under the text, so the object stays
        recognizable without harming readability."""
        if self.ROW_BG_OPACITY <= 0:
            return
        area = self._name_rect(row, depth)
        if area.width() <= 1 or area.height() <= 1:
            return
        dpr = self.devicePixelRatioF() or 1.0
        slot = self._row_bg_cache.setdefault(obj, {})
        img = slot.get("img")
        use_fp = None if self._thumb_frozen else self._thumb_fp(obj)
        if img is not None and slot.get("dpr") == dpr and slot.get("fp") == use_fp \
                and slot.get("size") == (round(area.width()), round(area.height())):
            img = slot["img"]
        else:
            slot["fp"] = use_fp
            slot["dpr"] = dpr
            slot["size"] = (round(area.width()), round(area.height()))
            img = None
            shapes = self._build_thumbnail_shapes(obj)
            if shapes:
                merged = QPainterPath()
                for shape_path, _color, _op in shapes:
                    merged.addPath(shape_path)
                bounds = merged.boundingRect()
                if not bounds.isEmpty():
                    img = self._bake_row_bg(
                        shapes, bounds,
                        round(area.width()), round(area.height()), dpr,
                    )
            slot["img"] = img
        if img is None:
            return

        painter.save()
        painter.setClipRect(area)
        painter.setOpacity(self.ROW_BG_OPACITY)
        painter.drawImage(area, img)
        painter.restore()

    def _draw_container_thumb(self, painter: QPainter, obj: SceneObject, rect: QRectF):
        renderer = _load_svg("symbol.svg")
        if renderer is not None:
            renderer.render(painter, rect)
            return
        inner = rect.adjusted(4, 5, -4, -5)

        if self._is_selected(obj):
            painter.setPen(QPen(QColor(255, 255, 255), 1.2))
        else:
            painter.setPen(QPen(QColor(110, 95, 55), 1))
        painter.setBrush(QColor(170, 140, 70))

        # Tab on top-left + body underneath
        tab = QRectF(
            inner.left(),
            inner.top(),
            inner.width() * 0.45,
            inner.height() * 0.38,
        )
        body = QRectF(
            inner.left(),
            inner.top() + inner.height() * 0.16,
            inner.width(),
            inner.height() * 0.78,
        )
        painter.drawRoundedRect(tab, 2, 2)
        painter.drawRoundedRect(body, 2, 2)

    def _build_thumbnail_shapes(self, obj: SceneObject) -> list[tuple[QPainterPath, str, float]]:
        shapes: list[tuple[QPainterPath, str, float]] = []
        cx = obj.transform.content_x
        cy = obj.transform.content_y
        if obj.shape_type == "rect":
            w = obj.shape_data.get("width", 100)
            h = obj.shape_data.get("height", 80)
            p = QPainterPath()
            p.addRect(-w / 2 + cx, -h / 2 + cy, w, h)
            shapes.append((p, obj.color, obj.opacity))
        elif obj.shape_type == "circle":
            r = obj.shape_data.get("radius", 30)
            p = QPainterPath()
            p.addEllipse(QPointF(cx, cy), r, r)
            shapes.append((p, obj.color, obj.opacity))
        elif obj.shape_type == "polygon":
            points = obj.shape_data.get("points", [])
            if points:
                p = QPainterPath()
                p.moveTo(points[0][0] + cx, points[0][1] + cy)
                for pt in points[1:]:
                    p.lineTo(pt[0] + cx, pt[1] + cy)
                p.closeSubpath()
                shapes.append((p, obj.color, obj.opacity))

        # Containers (Symbols) contribute no shapes of their own; their
        # children (shapes and nested containers) fold in below.

        for child in obj.children:
            t = QTransform()
            t.translate(child.transform.x, child.transform.y)
            t.rotate(child.transform.rotation)
            t.scale(child.transform.scale_x, child.transform.scale_y)
            for shape_path, color, opacity in self._build_thumbnail_shapes(child):
                shapes.append((t.map(shape_path), color, opacity))
        return shapes

    def _eye_icon(self, visible: bool, dpr: float) -> QImage:
        key = ("eye", visible, dpr)
        img = self._icon_img_cache.get(key)
        if img is not None:
            return img
        s = max(1, round(self.BUTTON_SIZE * dpr))
        img = QImage(s, s, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.BUTTON_SIZE, self.BUTTON_SIZE)
        if visible:
            p.setPen(QPen(QColor(180, 180, 180), 1))
            p.setBrush(QColor(70, 70, 70))
        else:
            p.setPen(QPen(QColor(100, 100, 100), 1))
            p.setBrush(QColor(45, 45, 45))
        p.drawEllipse(rect)
        c = rect.center()
        if visible:
            p.setPen(QPen(QColor(180, 180, 180), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(c.x(), c.y()), 3, 2)
            p.setBrush(QColor(180, 180, 180))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(c.x(), c.y()), 1.2, 1.2)
        else:
            p.setPen(QPen(QColor(120, 120, 120), 1.5))
            p.drawLine(c.x() - 4, c.y(), c.x() + 4, c.y())
        p.end()
        self._icon_img_cache[key] = img
        return img

    def _draw_eye_button(self, painter: QPainter, obj: SceneObject, rect: QRectF):
        dpr = self.devicePixelRatioF() or 1.0
        painter.drawImage(rect, self._eye_icon(obj.visible, dpr))

    def _mask_icon(self, name: str, dpr: float) -> QImage:
        """Mask/eye SVG rendered once per (name, dpr) instead of re-rendering
        the SVG into the row on every repaint."""
        key = (name, dpr)
        img = self._icon_img_cache.get(key)
        if img is None:
            renderer = _load_svg(name)
            s = max(1, round(self.BUTTON_SIZE * dpr))
            img = QImage(s, s, QImage.Format_ARGB32)
            img.fill(QColor(0, 0, 0, 0))
            img.setDevicePixelRatio(dpr)
            p = QPainter(img)
            if renderer is not None:
                renderer.render(p, QRectF(0, 0, self.BUTTON_SIZE, self.BUTTON_SIZE))
            p.end()
            self._icon_img_cache[key] = img
        return img

    def _draw_mask_button(self, painter: QPainter, obj: SceneObject, rect: QRectF):
        name = "mask_enabled.svg" if obj.is_mask else "mask_idle.svg"
        renderer = _load_svg(name)
        if renderer is not None:
            dpr = self.devicePixelRatioF() or 1.0
            painter.drawImage(rect, self._mask_icon(name, dpr))
            if obj.is_mask:
                self._draw_mask_mode_badge(painter, obj, rect)
            return

        painter.save()
        if obj.is_mask:
            painter.setPen(QPen(QColor(255, 100, 200), 1))
            painter.setBrush(QColor(90, 30, 60))
        else:
            painter.setPen(QPen(QColor(120, 120, 120), 1))
            painter.setBrush(QColor(45, 45, 45))

        painter.drawRect(rect)

        painter.setPen(QColor(255, 100, 200) if obj.is_mask else QColor(120, 120, 120))
        font = painter.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            rect, Qt.AlignCenter, "M"
        )
        painter.restore()

    def _draw_mask_mode_badge(self, painter: QPainter, obj: SceneObject, rect: QRectF):
        if obj.mask_mode == "erase":
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 100, 200))
            badge = QRectF(
                rect.right() - 5,
                rect.top() - 1,
                6,
                6,
            )
            painter.drawEllipse(badge)
            painter.restore()

    def _display_row_at(self, pos):
        """Return (obj, row_rect) for the row under `pos`, else (None, None)."""
        for i, (o, _d, _pl) in enumerate(self._display_rows()):
            r = self._row_rect(i)
            if r.contains(pos):
                return o, r
        return None, None

    def _show_mask_mode_menu(self, obj: SceneObject, global_pos):
        menu = StripeMenu()
        menu.add_section("Mask Mode")
        menu.add_action(
            "Wrap",
            callback=lambda checked: self._set_mask_mode(obj, "wrap"),
            checkable=True,
            checked=bool(obj.is_mask and obj.mask_mode == "wrap"),
        )
        menu.add_action(
            "Erase",
            callback=lambda checked: self._set_mask_mode(obj, "erase"),
            checkable=True,
            checked=bool(obj.is_mask and obj.mask_mode == "erase"),
        )
        menu.exec(global_pos, trigger_buttons=(Qt.RightButton,))

    def _set_mask_mode(self, obj: SceneObject, mode: str):
        targets = self._selection_targets(obj) if obj in SelectionState.selected() else [obj]
        changes = {}
        for target in targets:
            old_mask = target.is_mask
            old_mode = target.mask_mode
            if target.is_mask and target.mask_mode == mode:
                # Picking the already-active mode toggles the mask off.
                target.is_mask = False
            else:
                target.is_mask = True
                target.mask_mode = mode
            attrs = {}
            if old_mask != target.is_mask:
                attrs["is_mask"] = (old_mask, target.is_mask)
            if old_mode != target.mask_mode:
                attrs["mask_mode"] = (old_mode, target.mask_mode)
            if attrs:
                changes[target.id] = attrs
        if changes:
            self.history.push(PropertyCommand(changes))
        self.object_changed.emit()
        self.update()

    # ------------------------------------------------------------------ #
    # Mouse handling
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            obj, row = self._display_row_at(event.position())
            if (obj is not None and self._mask_hit_rect(row).contains(event.position())):
                # Hold-RMB to drive the mask-mode menu: popup on press, apply
                # the hovered option on release (or cancel on release outside).
                self._show_mask_mode_menu(obj, event.globalPosition().toPoint())
                event.accept()
                return
        if event.button() != Qt.LeftButton:
            return
        self.setFocus()

        if self._rename_editor is not None:
            self._commit_rename()

        # Defensive reset - a stray press shouldn't inherit a stale pending
        # click/drag from a release we somehow didn't see.
        self._pending_obj = None
        self._pending_multi = False
        self._pending_press_pos = None

        # In G mode a click confirms the drop.
        if self._drag_active:
            self._apply_drop()
            return

        y = event.position().y()
        for i, (obj, depth, _pl) in enumerate(self._display_rows()):
            row = self._row_rect(i)
            if not row.contains(event.position()):
                continue

            if obj.children and self._arrow_hit_rect(row, depth).contains(event.position()):
                obj.expanded = not obj.expanded
                self.update()
                return

            if self._eye_hit_rect(row).contains(event.position()):
                new_val = self._toggle_visible(obj)
                self._start_paint("visible", new_val, obj)
                return

            if self._mask_hit_rect(row).contains(event.position()):
                new_val = self._toggle_mask(obj)
                self._start_paint("mask", new_val, obj)
                return

            if self._lock_hit_rect(row).contains(event.position()):
                new_val = self._toggle_lock(obj)
                self._start_paint("lock", new_val, obj)
                return

            if event.modifiers() & Qt.ShiftModifier:
                # Shift+click: add/remove just this one row, leaving the
                # rest of the selection untouched. Selection-only - never
                # starts a drag, so layer order can't shift underneath you.
                self._update_selection(obj, additive=True)
                self._selection_anchor = obj
                return

            if event.modifiers() & Qt.ControlModifier:
                # Ctrl+click: select the contiguous range from the last
                # anchor to this row. Selection-only - never starts a drag.
                self._range_select(obj)
                return

            # Plain click on a row's body. We don't yet know if this is a
            # click or the start of a drag, so nothing beyond selection
            # happens here - the actual reorder-drag only begins in
            # mouseMoveEvent, once the cursor has moved past
            # DRAG_START_DISTANCE. If the row is already part of a
            # multi-selection we also defer *narrowing* the selection: drag
            # the whole group if the mouse moves, or collapse to just this
            # row on a plain click-release if it doesn't (Explorer/
            # Photoshop-style).
            already_multi = obj in SelectionState.selected() and len(SelectionState.selected()) > 1
            if not already_multi:
                self._update_selection(obj, additive=False)
                self._selection_anchor = obj

            self._pending_obj = obj
            self._pending_multi = already_multi
            self._pending_press_pos = QPointF(event.position())
            return

        # Click landed on empty space (padding or below the last row) -
        # clear the selection, Explorer-style.
        self._update_selection(None)
        self._selection_anchor = None
        self._pending_obj = None
        self._pending_multi = False
        self._pending_press_pos = None
        self.update()

    def _update_cursor(self, pos: QPointF) -> None:
        """Show a pointing-hand cursor while hovering any clickable sub-element
        (expander arrow / visibility / mask / lock), arrow otherwise. Skipped
        during an active drag or paint so the relative-drag's hidden cursor
        (and the paint's blank one) is preserved."""
        if self._drag_active or self._paint_active:
            return
        for i, (obj, depth, _pl) in enumerate(self._display_rows()):
            row = self._row_rect(i)
            if not row.contains(pos):
                continue
            if (obj.children and self._arrow_hit_rect(row, depth).contains(pos)) \
                    or self._eye_hit_rect(row).contains(pos) \
                    or self._mask_hit_rect(row).contains(pos) \
                    or self._lock_hit_rect(row).contains(pos):
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        self.setCursor(Qt.ArrowCursor)

    def mouseMoveEvent(self, event):
        if self._paint_active:
            self._paint_move(event.position())
            return

        if self._pending_obj is not None and not self._drag_active:
            delta = event.position() - self._pending_press_pos
            distance = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
            if distance < self.DRAG_START_DISTANCE:
                # Still a potential drag - keep repainting so the arrow /
                # button hover sprites recompute from the live cursor
                # position instead of going stale while LMB is held down.
                self._update_cursor(event.position())
                self.update()
                return
            # Threshold crossed - this is now a real drag. Grab at the
            # original press point so the row doesn't jump under the cursor.
            grab_y = self._pending_press_pos.y()
            pending_obj = self._pending_obj
            pending_multi = self._pending_multi
            self._pending_obj = None
            self._pending_multi = False
            self._pending_press_pos = None
            if pending_multi:
                self._start_multi_drag(pending_obj, grab_y=grab_y)
            else:
                self._start_drag(pending_obj, mode="lmb", grab_y=grab_y)

        if self._drag_active:
            delta = self._drag.delta(event.globalPosition().toPoint())
            if delta is None:
                return
            self._accum_dy += delta[1]
            # Map the accumulated relative motion back onto a widget-local y
            # (the real cursor stays pinned at the centre), so the ghost row
            # tracks the drag even once the cursor has traversed beyond the
            # widget / screen boundary.
            self._drag_mouse_y = self._drag_grab_y + self._accum_dy
            self._update_drag_over()
            self.repaint()
            return

        pos = event.position()
        self._update_cursor(pos)

        new_hovered = None
        for i, (obj, _d, _pl) in enumerate(self._display_rows()):
            row = self._row_rect(i)
            if row.contains(pos):
                new_hovered = obj
                break

        if new_hovered != self._hovered_object:
            old_hovered = self._hovered_object
            self._hovered_object = new_hovered
            self.object_hovered.emit(new_hovered)
            # Repaint both the row losing hover and the row gaining it so
            # the gray stripe highlight appears/disappears immediately
            # instead of waiting for the next 33ms animation tick.
        # Repaint so the arrow/button hover sprites track the cursor even
        # when it moves within a single row (the hovered object doesn't
        # change, but which sub-element is hovered does).
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return

        if self._paint_active:
            self._finish_paint()
            return

        if self._pending_obj is not None and not self._drag_active:
            # The cursor never crossed the drag threshold - this was a
            # plain click. A multi-selected row narrows down to just itself
            # now that we know it wasn't the start of a group-drag.
            if self._pending_multi:
                self._update_selection(self._pending_obj, additive=False)
                self._selection_anchor = self._pending_obj
            self._pending_obj = None
            self._pending_multi = False
            self._pending_press_pos = None
            return

        if self._drag_mode == "lmb":
            self._apply_drop()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton or self._drag_active:
            return
        pos = event.position()
        for i, (obj, depth, _pl) in enumerate(self._display_rows()):
            row = self._row_rect(i)
            if not row.contains(pos):
                continue

            # A fast second click on a button is really a second toggle, but
            # Qt swallows it into this double-click event (the second press
            # never reaches mousePressEvent). Apply the toggle right here so
            # the button responds immediately instead of stalling until the
            # double-click interval elapses.
            if self._eye_hit_rect(row).contains(pos):
                new_val = self._toggle_visible(obj)
                self._start_paint("visible", new_val, obj)
                return
            if self._mask_hit_rect(row).contains(pos):
                new_val = self._toggle_mask(obj)
                self._start_paint("mask", new_val, obj)
                return
            if self._lock_hit_rect(row).contains(pos):
                new_val = self._toggle_lock(obj)
                self._start_paint("lock", new_val, obj)
                return
            if obj.children and self._arrow_hit_rect(row, depth).contains(pos):
                obj.expanded = not obj.expanded
                self.update()
                return

            if self._name_rect(row, depth).contains(pos):
                self._begin_rename(obj, row, depth)
                return

            # Not on the title or any button - reject so the second press
            # falls through as a normal click.
            event.ignore()
            return

    def leaveEvent(self, event):
        if self._hovered_object is not None:
            old_hovered = self._hovered_object
            self._hovered_object = None
            self.object_hovered.emit(None)
            for i, (obj, _d, _pl) in enumerate(self._display_rows()):
                if obj is old_hovered:
                    self.update(self._row_rect(i).toRect())
                    break
        self._mouse_hover = False
        if not self._drag_active:
            self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def enterEvent(self, event):
        self._mouse_hover = True
        if not stripe_menu_open():
            self.setFocus()
        super().enterEvent(event)

    def handle_key_press(self, event):
        key = event.key()

        if self._drag_active:
            if key == Qt.Key_Escape:
                self._cancel_drag()
            return

        if key == Qt.Key_G and self._cursor_over_widget():
            # Same gather-style pattern as stage.py: G works anywhere the
            # cursor is inside this dock. It moves exactly the currently
            # selected rows - it never cares which row is hovered and never
            # changes the selection. QKeyEvent has no position(), so anchor
            # on the live cursor location, keeping the top-most selected
            # row locked to its current screen offset (no snap to cursor).
            pos = self.mapFromGlobal(QCursor.pos())
            if SelectionState.selected():
                stack = self._stack_objects()
                # Selected rows in on-screen (stack) order.
                selected = [o for o in stack if o in SelectionState.selected()]
                if selected:
                    # Anchor on the top-most selected row so the whole
                    # block keeps its place under the cursor and only
                    # moves by the user's drag delta.
                    self._start_multi_drag(selected[0], grab_y=pos.y(), mode="g")

    def keyPressEvent(self, event):
        self.handle_key_press(event)

    def _cursor_over_widget(self) -> bool:
        """True when the cursor is inside this dock. Same pattern as
        stage.py's ``_cursor_over_viewport`` - lets G work anywhere over the
        list, including empty space below the rows."""
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    # ------------------------------------------------------------------ #
    # Icon toggles (visibility / mask / lock), multi-selection aware
    # ------------------------------------------------------------------ #
    def _toggle_visible(self, obj: SceneObject) -> bool:
        """Toggle visibility. If `obj` is part of the current multi-selection,
        the whole selection is set to the same new state in one click -
        no more clicking the eye icon once per selected layer."""
        new_val = not obj.visible
        for target in self._selection_targets(obj):
            target.visible = new_val
        self.object_changed.emit()
        self.update()
        return new_val

    def _toggle_mask(self, obj: SceneObject) -> bool:
        new_val = not obj.is_mask
        for target in self._selection_targets(obj):
            target.is_mask = new_val
        self.object_changed.emit()
        self.update()
        return new_val

    def _toggle_lock(self, obj: SceneObject) -> bool:
        new_val = not obj.locked
        for target in self._selection_targets(obj):
            target.locked = new_val
        self.object_changed.emit()
        self.update()
        return new_val

    # ------------------------------------------------------------------ #
    # Icon "paint" drag - press one row's eye/lock/mask icon and drag
    # across others to apply the same new state to each one you pass over.
    # ------------------------------------------------------------------ #
    def _start_paint(self, kind: str, value: bool, obj: SceneObject):
        self._paint_active = True
        self._paint_kind = kind
        self._paint_value = value
        # Capture the value of every target *before* any paint edits, so all
        # rows touched by the whole paint-drag can be folded into ONE undo
        # step at the end (mouseReleaseEvent). The initial targets were just
        # flipped by _toggle_* to `value``, so their pre-toggle state is
        # ``not value``.
        self._paint_attr = {
            "visible": "visible",
            "lock": "locked",
            "mask": "is_mask",
        }[kind]
        self._paint_baseline: dict[str, bool] = {}
        for target in self._selection_targets(obj):
            # The initial targets were just flipped by _toggle_* to `value`,
            # so their pre-toggle state is ``not value``.
            self._paint_baseline[target.id] = not value
        # Rows already updated by the initial click (the clicked row, plus
        # its whole selection if it was multi-selected) shouldn't be
        # toggled again if the cursor happens to pass back over them.
        self._paint_seen = set(self._selection_targets(obj))

    def _finish_paint(self):
        """Push a single undo command covering every row the paint-drag
        touched (initial targets + all rows painted over), then reset."""
        changes: dict[str, dict] = {}
        for target in self._paint_seen:
            old = self._paint_baseline.get(target.id, getattr(target, self._paint_attr))
            new = getattr(target, self._paint_attr)
            if old != new:
                changes[target.id] = {self._paint_attr: (old, new)}
        if changes:
            self.history.push(PropertyCommand(changes))
        self._paint_active = False
        self._paint_kind = None
        self._paint_value = None
        self._paint_attr = None
        self._paint_baseline = {}
        self._paint_seen = set()

    def _paint_move(self, pos: QPointF):
        rect_for_kind = {
            "visible": self._eye_hit_rect,
            "lock": self._lock_hit_rect,
            "mask": self._mask_hit_rect,
        }
        for i, (obj, _d, _pl) in enumerate(self._display_rows()):
            row = self._row_rect(i)
            if not row.contains(pos):
                continue
            rect = rect_for_kind[self._paint_kind](row)
            if rect.contains(pos) and obj not in self._paint_seen:
                # Record this row's original value so the whole paint-drag
                # stays a single undo step.
                self._paint_baseline[obj.id] = getattr(obj, self._paint_attr)
                if self._paint_kind == "visible":
                    obj.visible = self._paint_value
                elif self._paint_kind == "lock":
                    obj.locked = self._paint_value
                elif self._paint_kind == "mask":
                    obj.is_mask = self._paint_value
                self._paint_seen.add(obj)
                self.object_changed.emit()
                self.update()
            return

    # ------------------------------------------------------------------ #
    # Inline rename
    # ------------------------------------------------------------------ #
    def _begin_rename(self, obj: SceneObject, row: QRectF, depth: int):
        if self._rename_editor is not None:
            self._commit_rename()

        editor = _InlineRenameEdit(self)
        editor.setText(obj.name)
        editor.setGeometry(self._name_rect(row, depth).adjusted(0, 2, 0, -2).toRect())
        editor.setStyleSheet(
            "QLineEdit {"
            " background-color: #454545;"
            " color: #f0f0f0;"
            " border: 1px solid " + Theme.ACCENT.name() + ";"
            " padding: 0 2px;"
            "}"
        )
        editor.selectAll()
        editor.committed.connect(self._commit_rename)
        editor.cancelled.connect(self._cancel_rename)
        editor.editingFinished.connect(self._commit_rename)
        editor.show()
        editor.setFocus()

        self._rename_editor = editor
        self._rename_obj = obj
        self.update()

    def _commit_rename(self, *_args):
        if self._rename_editor is None:
            return
        editor = self._rename_editor
        obj = self._rename_obj
        new_name = editor.text().strip()
        self._rename_editor = None
        self._rename_obj = None
        editor.blockSignals(True)
        editor.deleteLater()
        if obj is not None and new_name and new_name != obj.name:
            self.history.push(
                PropertyCommand({obj.id: {"name": (obj.name, new_name)}})
            )
            obj.name = new_name
            self.object_changed.emit()
        self.update()

    def _cancel_rename(self):
        if self._rename_editor is None:
            return
        editor = self._rename_editor
        self._rename_editor = None
        self._rename_obj = None
        editor.blockSignals(True)
        editor.deleteLater()
        self.update()

    def _lock_icon(self, locked: bool, dpr: float) -> QImage:
        key = ("lock", locked, dpr)
        img = self._icon_img_cache.get(key)
        if img is not None:
            return img
        s = max(1, round(self.BUTTON_SIZE * dpr))
        img = QImage(s, s, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.BUTTON_SIZE, self.BUTTON_SIZE)
        if locked:
            p.setPen(QPen(QColor(220, 170, 60), 1))
            p.setBrush(QColor(80, 60, 20))
        else:
            p.setPen(QPen(QColor(120, 120, 120), 1))
            p.setBrush(QColor(45, 45, 45))
        p.drawEllipse(rect)

        c = rect.center()
        lock_color = QColor(220, 170, 60) if locked else QColor(120, 120, 120)
        p.setPen(QPen(lock_color, 1.2))
        p.setBrush(Qt.NoBrush)
        body_w = 6
        body_h = 5
        body = QRectF(c.x() - body_w / 2, c.y() - 1, body_w, body_h)
        p.drawRect(body)
        p.drawArc(
            int(c.x() - body_w / 4), int(c.y() - body_h / 2 - 2),
            int(body_w / 2), int(body_h / 2 + 2),
            0, 180 * 16,
        )
        p.end()
        self._icon_img_cache[key] = img
        return img

    def _draw_lock_button(self, painter: QPainter, obj: SceneObject, rect: QRectF):
        dpr = self.devicePixelRatioF() or 1.0
        painter.drawImage(rect, self._lock_icon(obj.locked, dpr))
