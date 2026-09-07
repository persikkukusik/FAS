"""Blender-style area layout system.

Provides a flexible, resizable dock layout where areas can be split,
joined, and resized by dragging corners and edges -- just like Blender.

Architecture:
    AreaNode    -- tree data structure representing the layout hierarchy
    AreaWidget  -- recursive QWidget that renders one node of the tree
    SplitterHandle -- draggable divider between split areas
    AreaCorner  -- small hit-zone widgets at area corners for
                   split / join operations

When a split or join operation completes, the whole widget tree is
rebuilt from the (mutated) node tree. This keeps the widget hierarchy in
perfect sync with the data model and avoids stale-painter bugs.
"""
from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QTimer
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout

# ── constants ──────────────────────────────────────────────────────────────
HANDLE_WIDTH = 4          # width of the splitter handle between areas
CORNER_SIZE = 12          # size of the corner hit-zone (px)
_SPLIT_THRESHOLD = 20     # px of drag needed to start a split/join
_JOIN_OVERLAP = 40        # cursor must be this far inside a neighbor to fold
_MIN_RATIO = 0.12         # minimum area size as a fraction
_MAX_RATIO = 0.88
_MIN_PIXEL = 40           # absolute minimum px size of any child area
_SPLIT_MIN_RESULT = 120   # a split may not leave either child below this px
_MIN_SPLIT_PX = 60        # min px needed before an area may be split
_THEME_BG = QColor(30, 30, 30)
_THEME_HANDLE = QColor(52, 52, 52)
_THEME_HANDLE_HOVER = QColor(90, 90, 90)
_THEME_CORNER = QColor(255, 255, 255, 200)
_THEME_INDICATOR = QColor(0, 150, 255, 220)
_PREVIEW_RADIUS = 5       # rounded-corner radius for the drag preview rect
_PREVIEW_OUTLINE = 2      # outline width for the preview rect
_PREVIEW_ALPHA = 90       # fill alpha for the preview rect

AREA_DEBUG = os.environ.get("FAS_AREA_DEBUG", "0") == "1"


def _log(*args) -> None:
    if AREA_DEBUG:
        print("[area]", *args, file=sys.stderr, flush=True)


def _info(*args) -> None:
    # Always printed (visible from ./run.sh) so layout issues are diagnosable.
    print("[layout]", *args, file=sys.stderr, flush=True)


# ── drag operation state (class-level, shared across instances) ────────────
class _DragState:
    corner: "AreaCorner | None" = None
    operation: str | None = None          # "split" | "join"
    orientation: Qt.Orientation | None = None
    split_line: QRect | None = None       # deprecated line rect (root coords)
    preview_rect: QRect | None = None     # animated stripe preview (root coords)
    join_target: "AreaWidget | None" = None
    started_ms: int = 0                   # monotonic time drag began

    @classmethod
    def reset(cls) -> None:
        cls.corner = None
        cls.operation = None
        cls.orientation = None
        cls.split_line = None
        cls.preview_rect = None
        cls.join_target = None
        cls.started_ms = 0


# ── tree node ──────────────────────────────────────────────────────────────
class AreaNode:
    """A leaf holds a dock_id; a split holds two children + orientation."""

    def __init__(self, dock_id=None, orientation=None, ratio=0.5,
                 children=None):
        self.dock_id = dock_id
        self.orientation = orientation
        self.ratio = ratio
        self.children = list(children) if children else []

    @property
    def is_leaf(self):
        return self.dock_id is not None and not self.children

    @property
    def is_split(self):
        return self.orientation is not None and len(self.children) == 2

    @classmethod
    def leaf(cls, dock_id):
        return cls(dock_id=dock_id)

    @classmethod
    def split(cls, orientation, ratio, a, b):
        return cls(orientation=orientation, ratio=ratio, children=[a, b])

    def _find_parent(self, child):
        """Return the node that directly owns `child`, or None."""
        for c in self.children:
            if c is child:
                return self
            found = c._find_parent(child)
            if found is not None:
                return found
        return None


# ── splitter handle ────────────────────────────────────────────────────────
class SplitterHandle(QWidget):
    def __init__(self, area_widget, orientation):
        super().__init__(area_widget)
        self._orientation = orientation
        self._hovered = False
        self._dragging = False

        if orientation == Qt.Horizontal:
            self.setFixedWidth(HANDLE_WIDTH)
            self.setCursor(Qt.SplitHCursor)
        else:
            self.setFixedHeight(HANDLE_WIDTH)
            self.setCursor(Qt.SplitVCursor)

        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_StyledBackground, True)

    def paintEvent(self, event):
        p = QPainter(self)
        color = _THEME_HANDLE_HOVER if self._hovered else _THEME_HANDLE
        p.fillRect(self.rect(), color)
        p.end()

    def _area(self):
        p = self.parent()
        while p is not None and not isinstance(p, AreaWidget):
            p = p.parent()
        return p

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._dragging = True
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging:
            area = self._area()
            if area is not None:
                area._on_handle_drag(self, ev.globalPosition().toPoint())
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._dragging = False
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)

    def enterEvent(self, ev):
        self._hovered = True
        self.update()

    def leaveEvent(self, ev):
        self._hovered = False
        self.update()


# ── area corner ────────────────────────────────────────────────────────────
class AreaCorner(QWidget):
    def __init__(self, area_widget, corner_index):
        super().__init__(area_widget)
        self.area_widget = area_widget
        self.corner_index = corner_index  # 0 TL, 1 TR, 2 BL, 3 BR
        self._hovered = False
        self._dragging = False
        self._drag_start = None
        self.setFixedSize(CORNER_SIZE, CORNER_SIZE)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    def _sign(self):
        # returns (sx, sy) in {-1, 1} where -1 = towards origin
        return (1 if self.corner_index in (1, 3) else -1,
                1 if self.corner_index in (2, 3) else -1)

    def _reposition(self):
        sx, sy = self._sign()
        w, h = self.area_widget.width(), self.area_widget.height()
        x = w - self.width() if sx > 0 else 0
        y = h - self.height() if sy > 0 else 0
        self.move(x, y)

    def paintEvent(self, event):
        if not (self._hovered or self._dragging):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(_THEME_CORNER)
        p.drawEllipse(self.rect().adjusted(2, 2, -2, -2))
        p.end()

    def enterEvent(self, ev):
        self._hovered = True
        self.update()

    def leaveEvent(self, ev):
        self._hovered = False
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start = ev.globalPosition().toPoint()
            self.area_widget._begin_corner_drag(self)
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._dragging and self._drag_start is not None:
            self.area_widget._update_corner_drag(self, ev.globalPosition().toPoint())
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.area_widget._end_corner_drag(self, ev.globalPosition().toPoint())
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)


# ── area widget ────────────────────────────────────────────────────────────
class AreaWidget(QWidget):
    def __init__(self, node, dock_manager, parent_area=None, rebuild_callback=None):
        super().__init__(parent_area)
        self.node = node
        self.dock_manager = dock_manager
        self._parent_area = parent_area
        self._rebuild_callback = rebuild_callback
        self._children_areas = []
        self._handle = None
        self._corners = []
        self._dock_widget = None
        self._self_heal = None
        self._drag_anim = None
        self._pending_rebuild = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._build_from_node()

    # ── construction ───────────────────────────────────────────────────────
    def _build_from_node(self):
        for c in self._corners:
            c.hide()
            c.setParent(None)
            c.deleteLater()
        self._corners.clear()

        if self._handle is not None:
            self._handle.hide()
            self._handle.setParent(None)
            self._handle.deleteLater()
            self._handle = None

        for ch in self._children_areas:
            ch.hide()
            ch.setParent(None)
            ch.deleteLater()
        self._children_areas.clear()

        if self._dock_widget is not None:
            self._dock_widget.hide()
            self._dock_widget.setParent(None)
            self._dock_widget.deleteLater()
            self._dock_widget = None

        while self._layout.count():
            item = self._layout.takeAt()
            if item.widget():
                item.widget().hide()

        if self.node.is_leaf:
            self._build_leaf()
        else:
            self._build_split()
        self._layout_children()

    def _build_leaf(self):
        from ui.docks import DockWidget
        self._dock_widget = DockWidget(self.dock_manager, self.node.dock_id)
        self._layout.addWidget(self._dock_widget)
        self._dock_widget.show()
        self._corners = [AreaCorner(self, i) for i in range(4)]
        for c in self._corners:
            c.show()
        self._reposition_corners()

    def _build_split(self):
        cb = self._rebuild_callback
        self._child_a = AreaWidget(self.node.children[0], self.dock_manager,
                                   self, cb)
        self._child_a._parent_area = self
        self._child_a.show()
        self._child_b = AreaWidget(self.node.children[1], self.dock_manager,
                                   self, cb)
        self._child_b._parent_area = self
        self._child_b.show()
        self._children_areas = [self._child_a, self._child_b]
        self._handle = SplitterHandle(self, self.node.orientation)
        self._handle.show()
        self._layout_children()

    def _reposition_corners(self):
        for c in self._corners:
            c._reposition()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._layout_children()

    def _layout_children(self):
        if self.node.is_leaf:
            self._reposition_corners()
            return
        if (self.node.orientation is None or self._handle is None or
                len(self._children_areas) != 2):
            return

        r = self.rect()
        ratio = self.node.ratio
        handle = self._handle

        if self.node.orientation == Qt.Horizontal:
            # two panes + a handle must always fit within the parent
            avail = r.width() - HANDLE_WIDTH
            if avail > 0:
                half = _MIN_PIXEL / avail
                ratio = max(half, min(1 - half, ratio))
            handle_x = ratio * r.width()
            handle.move(handle_x, 0)
            handle.resize(HANDLE_WIDTH, r.height())
            self._child_a.setGeometry(0, 0, handle_x, r.height())
            self._child_b.setGeometry(handle_x + HANDLE_WIDTH, 0,
                                      r.width() - handle_x - HANDLE_WIDTH,
                                      r.height())
        else:
            avail = r.height() - HANDLE_WIDTH
            if avail > 0:
                half = _MIN_PIXEL / avail
                ratio = max(half, min(1 - half, ratio))
            handle_y = ratio * r.height()
            handle.move(0, handle_y)
            handle.resize(r.width(), HANDLE_WIDTH)
            self._child_a.setGeometry(0, 0, r.width(), handle_y)
            self._child_b.setGeometry(0, handle_y + HANDLE_WIDTH,
                                      r.width(), r.height() - handle_y - HANDLE_WIDTH)

        self._child_a._layout_children()
        self._child_b._layout_children()

    # ── handle resize ──────────────────────────────────────────────────────
    def _on_handle_drag(self, handle, global_pos):
        local = self.mapFromGlobal(global_pos)
        r = self.rect()
        if self.node.orientation == Qt.Horizontal and r.width() > HANDLE_WIDTH:
            avail = r.width() - HANDLE_WIDTH
            ratio = _clamp_ratio((local.x()) / r.width())
            half = _MIN_PIXEL / avail
            self.node.ratio = max(half, min(1 - half, ratio))
        elif self.node.orientation == Qt.Vertical and r.height() > HANDLE_WIDTH:
            avail = r.height() - HANDLE_WIDTH
            ratio = _clamp_ratio(local.y() / r.height())
            half = _MIN_PIXEL / avail
            self.node.ratio = max(half, min(1 - half, ratio))
        self._layout_children()

    # ── corner drag (split / join / resize) ────────────────────────────────
    def _begin_corner_drag(self, corner):
        _DragState.corner = corner
        _DragState.operation = None
        _DragState.orientation = None
        _DragState.split_line = None
        _DragState.preview_rect = None
        _DragState.join_target = None
        _DragState.started_ms = _now_ms()
        root = self.root_area()
        root._arm_self_heal()
        root._start_drag_anim()

    def _update_corner_drag(self, corner, global_pos):
        if _DragState.corner is not corner:
            return
        start = corner._drag_start
        if start is None:
            return
        dx = global_pos.x() - start.x()
        dy = global_pos.y() - start.y()
        root = self.root_area()
        sx, sy = corner._sign()

        if abs(dx) < _SPLIT_THRESHOLD and abs(dy) < _SPLIT_THRESHOLD:
            _DragState.operation = None
            _DragState.split_line = None
            _DragState.preview_rect = None
            _DragState.join_target = None
            root.update()
            return

        horiz = abs(dx) >= abs(dy)
        sx, sy = corner._sign()
        # inward = the drag moves AWAY from this corner (into `self`) on the
        # dominant axis; sx>0 = right edge so pushing in is dx<0, etc.
        inward = (dx * sx) < 0 if horiz else (dy * sy) < 0

        # --- FOLD (join): a clearly OUTWARD drag into the dock that is
        #     geometrically adjacent to this corner (never a distant/opposite
        #     one), and the cursor is actually inside it. Summoned soberly: a
        #     drag taken too far still only ever folds the nearest dock. ---
        # compute the adjacent leaf across the outward dominant edge
        neighbor = self._cross_edge_neighbor(root, corner, horiz)
        if (not inward and neighbor is not None and neighbor is not self and
                self._entered_neighbor(neighbor, global_pos, horiz)):
            _DragState.operation = "join"
            _DragState.orientation = None
            _DragState.join_target = neighbor
            tl = neighbor.mapTo(root, QPoint(0, 0))
            _DragState.preview_rect = QRect(tl, neighbor.size())
            _DragState.split_line = _DragState.preview_rect
            root.update()
            return

        # --- UNFOLD (split): a clearly INWARD drag that stays in `self` ---
        if inward and self._cursor_in_area(global_pos):
            _DragState.operation = "split"
            _DragState.orientation = Qt.Horizontal if horiz else Qt.Vertical
            _DragState.join_target = None
            self._set_split_preview(corner, root, global_pos, horiz)
            root.update()
            return

        # --- Not a clear gesture in either direction -> no-op ---
        _DragState.operation = None
        _DragState.split_line = None
        _DragState.preview_rect = None
        _DragState.join_target = None
        root.update()

    def _cursor_in_area(self, global_pos):
        local = self.mapFromGlobal(global_pos)
        return self.rect().contains(local)

    def _cross_edge_neighbor(self, root, corner, horiz):
        """Return the leaf immediately adjacent to `self` across the corner's
        outward edge along the dominant axis (global coords). Deterministic and
        always the closest dock -- never a distant or opposite one."""
        sx, sy = corner._sign()
        if horiz:
            px = self.width() if sx > 0 else 0
            probe_x = self.mapToGlobal(QPoint(px, self.height() // 2)).x()
            probe_x += 1 if sx > 0 else -1
            probe_y = self.mapToGlobal(QPoint(0, self.height() // 2)).y()
            return root._hit_leaf(QPoint(probe_x, probe_y))
        else:
            py = self.height() if sy > 0 else 0
            probe_y = self.mapToGlobal(QPoint(self.width() // 2, py)).y()
            probe_y += 1 if sy > 0 else -1
            probe_x = self.mapToGlobal(QPoint(self.width() // 2, 0)).x()
            return root._hit_leaf(QPoint(probe_x, probe_y))

    def _entered_neighbor(self, area, global_pos, horiz):
        """True if the cursor has clearly moved past the neighbour's edge along
        the chosen (dominant) axis. Only the outward axis needs to be crossed;
        the other axis just has to be inside the neighbour."""
        local = area.mapFromGlobal(global_pos)
        w, h = area.width(), area.height()
        if horiz:
            # entered horizontally: past a small margin in x, inside in y
            m = min(_JOIN_OVERLAP, w // 3)
            return m > 0 and (m <= local.x() <= w - m) and (0 <= local.y() < h)
        else:
            m = min(_JOIN_OVERLAP, h // 3)
            return (0 <= local.x() < w) and (m <= local.y() <= h - m)

    def _set_split_preview(self, corner, root, global_pos, horiz):
        """Compute the preview rectangle for the NEW area a split will create."""
        area = root._hit_leaf(global_pos) or self
        lc = area.mapFromGlobal(global_pos)
        sx, sy = corner._sign()
        tl = area.mapTo(root, QPoint(0, 0))
        if horiz:
            x = tl.x() + lc.x()
            # new area appears on the side the cursor is on (sign of movement)
            if lc.x() * sx <= 0:
                rect = QRect(tl.x(), tl.y(), max(1, lc.x()), area.height())
            else:
                rect = QRect(x, tl.y(), max(1, area.width() - lc.x()), area.height())
        else:
            y = tl.y() + lc.y()
            if lc.y() * sy <= 0:
                rect = QRect(tl.x(), tl.y(), area.width(), max(1, lc.y()))
            else:
                rect = QRect(tl.x(), y, area.width(), max(1, area.height() - lc.y()))
        _DragState.preview_rect = rect
        _DragState.split_line = QRect(rect.left(), rect.top(),
                                      max(2, rect.width()), max(2, rect.height()))

    def _end_corner_drag(self, corner, global_pos):
        root = self.root_area()
        root._disarm_self_heal()
        root._stop_drag_anim()
        op = _DragState.operation
        orient = _DragState.orientation
        jt = _DragState.join_target
        from_dock = corner.area_widget.node.dock_id if corner.area_widget else None

        _info(f"drag-end op={op} from={from_dock} "
              f"orient={'H' if orient == Qt.Horizontal else ('V' if orient == Qt.Vertical else None)} "
              f"to={jt.node.dock_id if jt else None}")
        _info(f"  press_start={corner._drag_start} "
              f"release={global_pos} "
              f"dxdy=({global_pos.x() - (corner._drag_start.x() if corner._drag_start else 0)},"
              f"{global_pos.y() - (corner._drag_start.y() if corner._drag_start else 0)})")
        _DragState.reset()

        # A bare corner click (no split/join) does not mutate the tree, so it
        # must NOT trigger a widget rebuild -- a pointless re-layout on a real
        # display is what let a freshly-created DockWidget repaint as stage.
        if op not in ("split", "join"):
            return

        try:
            # Mutate the node tree only (safe: pure data, no widget changes).
            if op == "split" and orient is not None:
                self._do_split(orient, global_pos)
            elif op == "join" and jt is not None:
                self._do_join(jt)

            # Defer the widget rebuild to the next event-loop tick. Rebuilding
            # right now (inside the corner's mouse-release handler) destroys the
            # very widget that currently owns the mouse grab, which corrupts
            # grab state and can hang the event loop / blank the canvas on a
            # real display server. Doing it once the current event has fully
            # returned avoids that re-entrancy crash entirely.
            root = self.root_area()
            if not getattr(root, "_pending_rebuild", False):
                root._pending_rebuild = True
                QTimer.singleShot(0, lambda: self._finish_rebuild(root))
            else:
                _info("  (rebuild already pending; skipping)")
            _info(f"  deferred rebuild for op={op}")
        except Exception:
            traceback.print_exc()
            # If anything went wrong mid-operation, force a clean rebuild and
            # clear any stale drag overlay so the UI never stays dark/stuck.
            _info("! exception during area operation; forcing clean rebuild")
            try:
                _DragState.reset()
                root = self.root_area()
                if not getattr(root, "_pending_rebuild", False):
                    root._pending_rebuild = True
                    QTimer.singleShot(0, lambda: self._finish_rebuild(root))
            except Exception:
                traceback.print_exc()

    def _finish_rebuild(self, root):
        """Apply a deferred widget rebuild and layout dump."""
        try:
            callback = self._rebuild_callback
            root._pending_rebuild = False
            root.rebuild_widgets()
            if callback is not None:
                callback()
            _info("  => post-op layout:")
            try:
                for line in _layout_lines(root):
                    _info("    " + line)
            except Exception:
                pass
        except Exception:
            traceback.print_exc()

    def _do_split(self, orientation, global_pos):
        root = self.root_area()
        target = root._hit_leaf(global_pos) or self
        if target.node.is_split:
            return
        # Refuse to split an area that is too small for two usable panes
        if (orientation == Qt.Horizontal and target.width() < _MIN_SPLIT_PX * 2) or \
           (orientation == Qt.Vertical and target.height() < _MIN_SPLIT_PX * 2):
            _info(f"! split refused: {target.node.dock_id} too small "
                  f"({target.width()}x{target.height()})")
            return
        # Refuse to split if either child would be below the minimum pane size
        cap = max(0.0, float(_SPLIT_MIN_RESULT) / max(1, target.width())) \
            if orientation == Qt.Horizontal else \
            max(0.0, float(_SPLIT_MIN_RESULT) / max(1, target.height()))
        if cap > 0.5:
            # Even 50/50 would leave one child under the minimum -> refuse
            _info(f"! split refused: {target.node.dock_id} cannot fit two usable panes "
                  f"({target.width()}x{target.height()}, need each >= {_SPLIT_MIN_RESULT}px)")
            return

        lc = target.mapFromGlobal(global_pos)
        _info(f"  _do_split orient={'H' if orientation == Qt.Horizontal else 'V'} "
              f"target={target.node.dock_id} size={target.width()}x{target.height()} "
              f"lc={lc.x()},{lc.y()}")

        # Compute the ratio; make sure BOTH resulting panes stay usable,
        # accounting for the handle width that sits between them.
        #   child_a size = ratio * total
        #   child_b size = total - ratio*total - HANDLE_WIDTH
        # Both must be >= _SPLIT_MIN_RESULT.
        lc = target.mapFromGlobal(global_pos)
        total = target.width() if orientation == Qt.Horizontal else target.height()
        if orientation == Qt.Horizontal and target.width() > 0:
            ratio = _clamp_ratio(lc.x() / target.width())
        elif target.height() > 0:
            ratio = _clamp_ratio(lc.y() / target.height())
        else:
            ratio = 0.5

        if total - HANDLE_WIDTH > 2 * _SPLIT_MIN_RESULT:
            lo = float(_SPLIT_MIN_RESULT) / total
            hi = float(total - HANDLE_WIDTH - _SPLIT_MIN_RESULT) / total
        else:
            # Not enough room for two usable panes -> do not split.
            _info(f"! split refused: {target.node.dock_id} cannot fit two usable panes "
                  f"({total}px along split axis, need > {2 * _SPLIT_MIN_RESULT + HANDLE_WIDTH}px)")
            return

        ratio = max(lo, min(hi, ratio))
        lo = max(lo, 0.05)
        hi = min(hi, 0.95)
        ratio = _clamp_ratio(ratio)

        child_a = AreaNode.leaf(target.node.dock_id)
        child_b = AreaNode.leaf(target.node.dock_id)
        target.node.children = [child_a, child_b]
        target.node.orientation = orientation
        target.node.ratio = ratio
        target.node.dock_id = None
        _info(f"  => split ratio={round(ratio, 3)}")

    def _do_join(self, target):
        """Fold `target` (a leaf) into `self`, removing target from tree."""
        if target is self or target is None:
            return
        root = self.root_area()
        parent_node = root.node._find_parent(target.node)
        if parent_node is None or parent_node.is_leaf:
            return
        if target.node.is_split:
            return

        # Remove target -> collapse parent to its remaining child
        remaining = [c for c in parent_node.children if c is not target.node]
        if not remaining:
            return

        remaining = remaining[0]
        grandparent = root.node._find_parent(parent_node)
        _info(f"  _do_join target={target.node.dock_id} remaining={remaining.dock_id or 'SPLIT'} "
              f"root_parent={grandparent is None}")
        if grandparent is not None:
            idx = grandparent.children.index(parent_node)
            grandparent.children[idx] = remaining
        else:
            # parent is the root itself
            root.node.dock_id = remaining.dock_id
            root.node.orientation = remaining.orientation
            root.node.ratio = remaining.ratio
            root.node.children = list(remaining.children)

    # ── helpers ────────────────────────────────────────────────────────────
    def root_area(self):
        w = self
        while w._parent_area is not None:
            w = w._parent_area
        return w

    def _leaf_for_widget(self, widget):
        """Return the leaf AreaWidget that owns `widget` (via its dock slot)."""
        if self.node.is_leaf and self._dock_widget is widget:
            return self
        if self.node.is_split:
            for ch in self._children_areas:
                r = ch._leaf_for_widget(widget)
                if r is not None:
                    return r
        return None

    def rebuild_widgets(self):
        """Rebuild this (root) widget's entire subtree from the node tree."""
        self._build_from_node()
        self._layout_children()
        # Newly created child AreaWidgets default to visible=False, so the whole
        # central area would render blank after a split/join. Show them all.
        self.show()
        repaired = self._repair_degenerate()
        if repaired:
            self._build_from_node()
            self._layout_children()
            self.show()

    def reset_to_tree(self, node):
        """Replace the entire layout with a new node tree (used for reset)."""
        self.node = node
        self._build_from_node()
        self._layout_children()
        self.show()
        repaired = self._repair_degenerate()
        if repaired:
            self._build_from_node()
            self._layout_children()
            self.show()

    def _repair_degenerate(self):
        """Merge any split whose children are too small / too skewed to be
        usable, so the layout never reaches a degenerate dark/stuck state.

        Returns True if the tree changed, else False.
        """
        changed = False

        def merge(n):
            nonlocal changed
            m = n.children[0]
            n.dock_id = m.dock_id
            n.orientation = m.orientation
            n.ratio = m.ratio
            n.children = m.children
            changed = True

        def visit(area):
            nonlocal changed
            if area.node.is_leaf:
                return
            for ch in area._children_areas:
                visit(ch)
            # Determine whether both children can stay usable.
            if area.node.orientation == Qt.Horizontal:
                size = area.width()
                ax = 'w'
            elif area.node.orientation == Qt.Vertical:
                size = area.height()
                ax = 'h'
            else:
                return
            if size <= 0:
                return
            eff = size - HANDLE_WIDTH
            if eff < 2 * _SPLIT_MIN_RESULT:
                # Can't fit two usable panes at all -> merge to one child.
                merge(area.node)
                return
            lo = _SPLIT_MIN_RESULT / size
            hi = (size - HANDLE_WIDTH - _SPLIT_MIN_RESULT) / size
            if not (lo <= area.node.ratio <= hi):
                new_ratio = max(lo, min(hi, area.node.ratio))
                if abs(new_ratio - area.node.ratio) > 1e-6:
                    area.node.ratio = new_ratio
                    changed = True

        root = self.root_area()
        visit(root)
        return changed

    # ── self-heal for a stuck drag overlay ─────────────────────────────────
    def _arm_self_heal(self):
        root = self.root_area()
        if root._self_heal is None:
            root._self_heal = QTimer(root)
            root._self_heal.setSingleShot(True)
            root._self_heal.setInterval(3000)  # ms before auto-clearing a stale drag
            root._self_heal.timeout.connect(root._self_heal_timeout)
        root._self_heal.start()

    def _disarm_self_heal(self):
        root = self.root_area()
        if root._self_heal is not None and root._self_heal.isActive():
            root._self_heal.stop()

    def _start_drag_anim(self):
        root = self.root_area()
        if root._drag_anim is None:
            root._drag_anim = QTimer(root)
            root._drag_anim.setInterval(30)  # animate stripes during drag
            root._drag_anim.timeout.connect(lambda: root.update())
        root._drag_anim.start()

    def _stop_drag_anim(self):
        root = self.root_area()
        if root._drag_anim is not None and root._drag_anim.isActive():
            root._drag_anim.stop()
        root.update()

    def _self_heal_timeout(self):
        _info("! self-heal: clearing stale drag overlay state")
        _DragState.reset()
        self._stop_drag_anim()
        try:
            self.rebuild_widgets()
            if self._rebuild_callback is not None:
                self._rebuild_callback()
        except Exception:
            traceback.print_exc()

    def _hit_leaf(self, global_pos):
        """Return the leaf AreaWidget under global_pos, or None."""
        local = self.mapFromGlobal(global_pos)
        if not self.rect().contains(local):
            return None
        if self.node.is_leaf:
            return self
        for ch in self._children_areas:
            r = ch._hit_leaf(global_pos)
            if r is not None:
                return r
        return None

    def dump_layout(self, indent: int = 0) -> None:
        import sys
        pad = "  " * indent
        if self.node.is_leaf:
            print(f"{pad}LEAF {self.node.dock_id}  ({self.width()}x{self.height()})",
                  file=sys.stderr)
        else:
            o = "H" if self.node.orientation == Qt.Horizontal else "V"
            print(f"{pad}SPLIT {o} ratio={round(self.node.ratio, 3)}  "
                  f"({self.width()}x{self.height()})", file=sys.stderr)
            for ch in self._children_areas:
                ch.dump_layout(indent + 1)

    # ── painting ───────────────────────────────────────────────────────────
    def paintEvent(self, event):
        super().paintEvent(event)
        if self is not self.root_area():
            return
        # Self-heal: if the drag state is stale (started too long ago with no
        # active grab) stop painting the overlay so we can never get stuck dark.
        if _DragState.started_ms and _DragState.corner is not None:
            if not _DragState.corner.isVisible() and _now_ms() - _DragState.started_ms > 200:
                _info("! paint: clearing stale drag (corner hidden)")
                _DragState.reset()
                return
        rect = _DragState.preview_rect
        if _DragState.operation is None or rect is None:
            return
        try:
            self._draw_preview(rect)
        except Exception:
            traceback.print_exc()

    def _draw_preview(self, rect: QRect) -> None:
        """Paint the animated, striped, rounded-corner preview rectangle."""
        from ui.stripes import StripeShader
        from ui.theme import Theme
        from PySide6.QtGui import QPainterPath

        radius = min(_PREVIEW_RADIUS, rect.width() // 2, rect.height() // 2)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)

        # 1) half-transparent accent fill
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fill = QColor(Theme.ACCENT)
        fill.setAlpha(_PREVIEW_ALPHA)
        p.fillPath(path, fill)

        # 2) animated stripes clipped to the rounded rect
        shader = StripeShader(color=Theme.ACCENT, speed=40.0)
        shader.paint(p, path, zoom=1.0)

        # 3) 2px accent outline
        p.setBrush(Qt.NoBrush)
        pen = QPen(Theme.ACCENT, _PREVIEW_OUTLINE)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(path)
        p.end()


def _clamp_ratio(v):
    return max(_MIN_RATIO, min(_MAX_RATIO, v))


def _now_ms() -> int:
    try:
        from PySide6.QtCore import QElapsedTimer
        t = QElapsedTimer()
        t.start()
        return t.elapsed()
    except Exception:
        import time
        return int(time.monotonic() * 1000)


def _layout_lines(area: "AreaWidget", indent: int = 0) -> list[str]:
    lines = []
    pad = "  " * indent
    if area.node.is_leaf:
        lines.append(f"{pad}LEAF {area.node.dock_id}  ({area.width()}x{area.height()})")
    else:
        o = "H" if area.node.orientation == Qt.Horizontal else "V"
        lines.append(f"{pad}SPLIT {o} ratio={round(area.node.ratio, 3)}  "
                     f"({area.width()}x{area.height()})")
        for ch in area._children_areas:
            lines.extend(_layout_lines(ch, indent + 1))
    return lines
