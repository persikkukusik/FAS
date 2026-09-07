from __future__ import annotations

import os
import sys

import sys
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import math
import weakref
from enum import Enum, auto

from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QLinearGradient,
    QRadialGradient,
    QPainterPath,
    QTransform,
    QImage,
    QCursor,
    QGuiApplication,
)
from PySide6.QtWidgets import QWidget, QApplication

from core.commands import DeleteObjectsCommand, KeyframeCommand, PropertyCommand
from core.model import Scene, SceneObject
from core.selection import SelectionState
from core.animation import set_keyframe_at_current_frame
from core.history import History
from ui.theme import Theme
from ui.stripes import StripeShader
from ui.menus import stripe_menu_open


class TransformMode(Enum):
    NONE = auto()
    MOVE = auto()
    ROTATE = auto()
    SCALE = auto()


class StageWidget(QWidget):
    CANVAS_SIZE = 512

    selection_changed = Signal(object)
    status_message = Signal(str)
    toggle_playback = Signal()
    keyframe_created = Signal()
    transform_started = Signal()
    transform_ended = Signal()

    def __init__(self, scene: Scene, history: History):
        super().__init__()
        self.scene = scene
        self.history = history
        self.transform_mode = TransformMode.NONE
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self._last_canvas_pos = QPointF(0, 0)
        self._mouse_inside = False

        self._start_mouse = QPointF()
        self._start_pos = QPointF()
        self._start_rotation = 0.0
        self._start_scale = (1.0, 1.0)
        self._constraint_axis: str | None = None
        self._transform_delta = QPointF(0, 0)
        self._last_global = None
        self._just_wrapped = False
        self._panning = False

        self._paint_canvas_transform = None
        self._pan_last_global = QPointF(0, 0)

        self._start_obj_states: dict[str, tuple[QPointF, float, tuple[float, float]]] = {}
        self._avg_pivot = QPointF(0, 0)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(300, 300)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)

        self._hover_from_canvas = False

        self._selection_dash_offset = 0.0
        self._selection_timer = QTimer(self)
        self._selection_timer.setInterval(33)
        self._selection_timer.timeout.connect(self._animate_selection)
        self._selection_timer.start()

        self._hover_anim_timer = QTimer(self)
        self._hover_anim_timer.setInterval(33)
        self._hover_anim_timer.timeout.connect(self._animate_hover_highlight)

        # Smooth-then-sharpen zoom mechanics
        self._is_zooming = False
        self._cache_zoom = 1.0  # Zoom level at which the current cache image was baked
        self._sharpen_timer = QTimer(self)
        self._sharpen_timer.setSingleShot(True)
        self._sharpen_timer.setInterval(150)  # Wait 150ms after last scroll wheel tick
        self._sharpen_timer.timeout.connect(self._rebuild_high_res_cache)

        self._cache_img: QImage | None = None
        self._cache_key: tuple | None = None

        self._obj_render_cache: "weakref.WeakKeyDictionary[SceneObject, dict]" = (
            weakref.WeakKeyDictionary()
        )

        self._live_ids: set[str] = set()

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
        return SelectionState.hovered()

    @hovered_object.setter
    def hovered_object(self, value: SceneObject | None) -> None:
        SelectionState.set_hovered(value)

    def _outline_target_for(self, obj: SceneObject) -> SceneObject:
        """The object that should carry the outline highlight for ``obj``.

        By default this is ``obj``'s first parent (the container you're
        editing inside).  Holding Alt overrides it to ``obj`` itself, so you
        can always target the exact object under the cursor."""
        if QGuiApplication.queryKeyboardModifiers() & Qt.AltModifier:
            return obj
        return self.scene.find_parent(obj) or obj

    @property
    def _outline_target(self) -> SceneObject | None:
        """The object that should receive the hover outline highlight.

        When the hover was raised by aiming on the canvas itself the parent
        is highlighted (unless Alt is held, which pins the exact object).  A
        hover raised directly (e.g. from the outliner, where rows are already
        exact) is used as-is, without any parent/Alt override.
        """
        raw = self.hovered_object
        if raw is None:
            return None
        if not self._hover_from_canvas:
            return raw
        return self._outline_target_for(raw)

    def _update_outline_target(self) -> None:
        """Recompute and trigger a repaint when the outline target may have
        changed (mouse moved, Alt pressed/released)."""
        self.update()
        for stage in self._all_stages():
            if stage is not self:
                stage.update()

    def _set_selected_objects(self, objects: list[SceneObject]) -> None:
        SelectionState.set_selected(list(objects))
        for stage in self._all_stages():
            stage.update()

    def _all_stages(self):
        from PySide6.QtWidgets import QApplication
        stages = []
        for widget in QApplication.topLevelWidgets():
            for child in widget.findChildren(StageWidget):
                stages.append(child)
        return stages

    def _all_outliners(self):
        from PySide6.QtWidgets import QApplication
        from ui.outliner import OutlinerWidget
        outliners = []
        for widget in QApplication.topLevelWidgets():
            for child in widget.findChildren(OutlinerWidget):
                outliners.append(child)
        return outliners

    def _update_selection(self, obj: SceneObject | None, additive: bool = False) -> None:
        """Update the global selection. If additive (Shift held), toggle obj.

        Clicking an object that has a parent selects the parent (so you grab
        the whole container), unless Alt is held - then the exact object
        aimed at is selected. Shift-clicking empty canvas (``obj is None``)
        leaves the current selection untouched rather than adding ``None`` to
        it.
        """
        if obj is not None:
            obj = self._outline_target_for(obj)
        if additive:
            if obj is not None:
                SelectionState.toggle(obj)
        else:
            SelectionState.set_selected([obj] if obj else [])
        self.selection_changed.emit(obj)
        self.update()
        for stage in self._all_stages():
            if stage is not self:
                stage.update()

    def _deselect_all(self) -> None:
        SelectionState.clear_selected()
        self.update()
        for stage in self._all_stages():
            stage.update()

    def _update_hover(self, canvas_pos: QPointF) -> None:
        """Hit-test under the cursor and update the shared hover state.

        This is the trigger for hover highlighting on the canvas itself
        (as opposed to e.g. a layers panel setting ``hovered_object``
        directly). Starts/stops the hover animation timer as needed.
        """
        obj = self.hit_test(canvas_pos)
        if obj is self.hovered_object and self._hover_from_canvas:
            return
        self._hover_from_canvas = True
        self.hovered_object = obj
        if obj is not None:
            if not self._hover_anim_timer.isActive():
                self._hover_anim_timer.start()
        else:
            self._hover_anim_timer.stop()
        self._update_outline_target()

    def _clear_hover(self) -> None:
        if self.hovered_object is not None:
            self.hovered_object = None
            self._hover_from_canvas = False
            self._hover_anim_timer.stop()
            self.update()
            for stage in self._all_stages():
                if stage is not self:
                    stage.update()

    def set_hovered_direct(self, value: SceneObject | None) -> None:
        """Set the hovered object from an external panel (e.g. the outliner).

        Rows in the outliner are already exact objects, so the parent/Alt
        override that applies when aiming on the canvas is skipped."""
        if value is self.hovered_object and not self._hover_from_canvas:
            return
        self._hover_from_canvas = False
        self.hovered_object = value
        if value is not None:
            if not self._hover_anim_timer.isActive():
                self._hover_anim_timer.start()
        else:
            self._hover_anim_timer.stop()
        self._update_outline_target()

    def resizeEvent(self, event):
        margin = 40
        aw = self.width() - margin
        ah = self.height() - margin
        if aw > 0 and ah > 0:
            self.zoom = min(aw / self.CANVAS_SIZE, ah / self.CANVAS_SIZE, 2.0)
        super().resizeEvent(event)

    def enterEvent(self, event):
        self._mouse_inside = True
        if not stripe_menu_open():
            self.setFocus()
        self._last_canvas_pos = self.viewport_to_canvas(
            self.mapFromGlobal(self.cursor().pos())
        )
        if self.transform_mode == TransformMode.NONE:
            self._update_hover(self._last_canvas_pos)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._mouse_inside = False
        self._clear_hover()
        super().leaveEvent(event)

    def _cursor_over_viewport(self) -> bool:
        return self.rect().contains(self.mapFromGlobal(self.cursor().pos()))

    def paintEvent(self, event):
        dpr = self.devicePixelRatioF() or 1.0

        # Viewport dimensions in physical pixels (never exceeds screen bounds)
        img_w = max(1, round(self.width() * dpr))
        img_h = max(1, round(self.height() * dpr))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(50, 50, 50))

        # Cache key tracks viewport resolution, pan, zoom, and scene content
        key = (
            img_w,
            img_h,
            self.zoom,
            self._pan.x(),
            self._pan.y(),
            self._scene_content_hash(),
        )

        if (
            key != self._cache_key
            or self._cache_img is None
            or self._cache_img.width() != img_w
            or self._cache_img.height() != img_h
        ):
            self._cache_img = self._render_canvas_body(img_w, img_h, dpr)
            self._cache_key = key

        # Blit 1:1 onto the viewport (crisp pixel match)
        painter.drawImage(
            QRectF(0, 0, self.width(), self.height()),
            self._cache_img,
            QRectF(0, 0, img_w, img_h),
        )

        # Draw interactive vector overlays (handles, gizmos, selection)
        cw = round(self.CANVAS_SIZE * self.zoom)
        ch = round(self.CANVAS_SIZE * self.zoom)
        cx = (self.width() - cw) / 2 + self._pan.x()
        cy = (self.height() - ch) / 2 + self._pan.y()

        painter.save()
        painter.translate(cx, cy)
        painter.scale(self.zoom, self.zoom)
        self._paint_canvas_transform = painter.worldTransform()

        self._draw_live_overlays(painter)

        if self.hovered_object:
            outline = self._outline_target
            if outline is not None and outline not in SelectionState.selected():
                self._draw_hover_highlight(painter, outline)

        for obj in SelectionState.selected():
            if obj.is_mask and obj.mask_mode == "erase":
                self._draw_clipped_zone(painter, obj, QColor(230, 45, 45))
                continue
            if obj.is_mask:
                self._draw_masked_selection(painter, obj)
            else:
                self._draw_selection(painter, obj)

        if self.transform_mode != TransformMode.NONE:
            self._draw_transform_gizmo(painter)

        painter.restore()
        painter.end()

    def _render_canvas_body(self, img_w: int, img_h: int, dpr: float) -> QImage:
        """Rasterises only the visible window portion directly at 1:1 screen DPI."""
        img = QImage(img_w, img_h, QImage.Format_ARGB32)
        img.setDevicePixelRatio(dpr)
        img.fill(QColor(50, 50, 50))  # Match background surrounding canvas

        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)

        # Compute canvas position in logical widget space
        cw = self.CANVAS_SIZE * self.zoom
        ch = self.CANVAS_SIZE * self.zoom
        cx = (self.width() - cw) / 2 + self._pan.x()
        cy = (self.height() - ch) / 2 + self._pan.y()

        # Draw white canvas background
        painter.fillRect(QRectF(cx, cy, cw, ch), QColor(255, 255, 255))

        # Map transformation to canvas 0..CANVAS_SIZE space
        painter.save()
        painter.translate(cx, cy)
        painter.scale(self.zoom, self.zoom)
        self._paint_canvas_transform = painter.worldTransform()

        # Grid lines
        grid_pen = QPen(QColor(235, 235, 235), 0.5 / self.zoom)
        painter.setPen(grid_pen)
        for gx in range(0, self.CANVAS_SIZE + 1, 50):
            painter.drawLine(gx, 0, gx, self.CANVAS_SIZE)
        for gy in range(0, self.CANVAS_SIZE + 1, 50):
            painter.drawLine(0, gy, self.CANVAS_SIZE, gy)

        # Objects
        base_holes, clip_holes = self._compute_mask_paths()
        for obj in self.scene.objects:
            self._draw_object(painter, obj, base_holes, clip_holes)

        painter.restore()

        # Draw outer canvas border in widget space
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.drawRect(QRectF(cx, cy, cw, ch))

        painter.end()
        return img

    def _rebuild_high_res_cache(self) -> None:
        """Callback triggered by _sharpen_timer after zooming stops."""
        self._is_zooming = False
        self._cache_key = None  # Force snapshot cache invalidation
        self.update()  # Repaint canvas sharply at full vector detail

    def _scene_content_hash(self) -> int:
        """Cheap fingerprint of everything that changes what the canvas body
        looks like: object order/hierarchy, visibility, transforms, fill and
        stroke geometry, and which masks are selected (their clipped-zone
        markers are baked into the cached body render).

        It is recomputed on every repaint, so a mismatch after ANY mutation
        (import, edits, undo, drags, playback) naturally invalidates the
        snapshot cache without the callers needing to remember to.
        """
        items: list = []
        for obj in self.scene.iter_objects():
            if obj.id in self._live_ids:
                continue
            t = obj.transform
            items.append(id(obj))
            items.append(1 if obj.visible else 0)
            items.append(obj.is_mask)
            items.append(obj.mask_mode)
            items.append(obj.opacity)
            items.append(t.x)
            items.append(t.y)
            items.append(t.rotation)
            items.append(t.scale_x)
            items.append(t.scale_y)
            items.append(hash(obj.color))
            items.append(id(obj.shape_data))
        for m in SelectionState.selected():
            if m.is_mask:
                items.append(id(m))
        return hash(tuple(items))

    def _draw_object(self, painter: QPainter, obj: SceneObject, base_holes, clip_holes):
        """Draw one object's subtree, mask-aware, in its recursive position.

        Handles the mask semantics at any depth:
          - invisible objects and erase-mode masks draw nothing;
          - a base object (one that erase masks target) has its whole subtree
            clipped to its union surface minus those holes;
          - a wrap mask clips its subtree to its base's union (minus any
            erases above it in its run).

        The purple "clipped zone" markers for selected wrap masks are drawn
        right before their clip source, so they sit behind it in the stack.
        """
        if not obj.visible:
            return
        if obj.is_mask and obj.mask_mode == "erase":
            # Non-destructive erase mask: the layer itself is invisible - its
            # hole was already subtracted from whatever surface it targets.
            return
        if obj.id in self._live_ids:
            # Currently being moved live; its sprite is drawn on top of the
            # snapshot, so leave a gap here (and skip its whole subtree).
            return

        clip = self._clip_path_for(obj, base_holes, clip_holes)
        painter.save()
        if clip is not None:
            self._apply_clip(painter, obj, clip)

        for m in SelectionState.selected():
            if m.is_mask and m.mask_mode == "wrap" and self._mask_clip_source(m) is obj:
                self._draw_clipped_zone(painter, m)

        self._draw_subtree(painter, obj, base_holes, clip_holes)
        painter.restore()

    def _draw_subtree(self, painter: QPainter, obj: SceneObject, base_holes, clip_holes):
        """Draw `obj`'s own shape followed by every child (bottom-first),
        inside `obj`'s local coordinate space. Mask decisions are delegated
        back to ``_draw_object`` so they work recursively at any depth."""
        painter.save()
        painter.translate(obj.transform.x, obj.transform.y)
        painter.rotate(obj.transform.rotation)
        painter.scale(obj.transform.scale_x, obj.transform.scale_y)

        # Group opacity: when a parent fades, its whole subtree must be
        # composited first (children are solid against each other) and only
        # THEN faded as one layer. Applying setOpacity per child lets each
        # child alpha-blend against the ones beneath it, so overlapping
        # opaque children show through each other inside the parent.
        combined = painter.opacity() * obj.opacity
        if obj.children and combined < 0.999:
            painter.restore()
            self._draw_grouped(painter, obj, base_holes, clip_holes, combined)
            return

        painter.setOpacity(combined)
        self._paint_own_shape(painter, obj)

        for child in obj.children:
            if child.visible:
                self._draw_object(painter, child, base_holes, clip_holes)

        painter.restore()

    def _paint_own_shape(self, painter: QPainter, obj: SceneObject) -> None:
        """Draw `obj`'s own visible shape (containers draw nothing) at the
        painter's current origin, with no opacity applied by itself."""
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._flat_brush(obj))

        if obj.shape_type == "rect":
            w = obj.shape_data.get("width", 100)
            h = obj.shape_data.get("height", 80)
            painter.drawRect(-w / 2, -h / 2, w, h)
        elif obj.shape_type == "circle":
            r = obj.shape_data.get("radius", 50)
            painter.drawEllipse(QPointF(0, 0), r, r)
        elif obj.shape_type == "polygon":
            if obj.shape_data.get("points"):
                painter.drawPath(self._local_path(obj))

    def _draw_grouped(
        self,
        painter: QPainter,
        obj: SceneObject,
        base_holes,
        clip_holes,
        opacity: float,
    ):
        """Render `obj`'s whole subtree as one opaque group, then composite
        it with a single fade of ``opacity``.

        The subtree is drawn into an offscreen buffer in canvas (world)
        coordinates: children keep their own opacities and mask semantics and
        alpha-composite against each other *inside* the group, so overlapping
        opaque children appear solid. The finished buffer is then drawn back
        onto the scene at ``opacity`` - a group fade, not per-child fades.
        Nested fades (a container inside a container) simply recurse into
        this same path on the buffer's painter.
        """
        local_union = self._subtree_raw_union(obj)
        if local_union.isEmpty():
            return
        world_bounds = self._world_transform(obj).mapRect(local_union.boundingRect())
        if world_bounds.isEmpty():
            return

        dpr = self.devicePixelRatioF() or 1.0
        s = self.zoom * dpr
        margin = 2.0 / s  # a couple of pixels of bleed for antialiased edges
        left = world_bounds.left() - margin
        top = world_bounds.top() - margin
        w_img = max(1, math.ceil((world_bounds.width() + 2 * margin) * s))
        h_img = max(1, math.ceil((world_bounds.height() + 2 * margin) * s))

        img = QImage(w_img, h_img, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(QColor(0, 0, 0, 0))

        bp = QPainter(img)
        bp.setRenderHint(QPainter.Antialiasing)
        # Scene units -> image pixels: (point - origin) * s
        bp.translate(-left * s, -top * s)
        bp.scale(s, s)

        bp.save()
        # Position the painter at the group's world origin: content drawn in
        # local coords must land where their world-space ancestors put them.
        # (Root-level groups have no ancestors, so their "world" is identity.)
        for o in self._ancestors(obj):
            bp.translate(o.transform.x, o.transform.y)
            bp.rotate(o.transform.rotation)
            bp.scale(o.transform.scale_x, o.transform.scale_y)
        bp.translate(obj.transform.x, obj.transform.y)
        bp.rotate(obj.transform.rotation)
        bp.scale(obj.transform.scale_x, obj.transform.scale_y)
        self._paint_own_shape(bp, obj)
        for child in obj.children:
            if child.visible:
                self._draw_object(bp, child, base_holes, clip_holes)
        bp.restore()
        bp.end()

        painter.save()
        painter.setOpacity(max(0.0, min(opacity, 1.0)))
        # `painter` sits in the group's ancestor-applied space (COPY), while the
        # buffer's dest rect is in absolute world coordinates. Root-level groups
        # have an identity ancestor transform, but nested groups must map the
        # world rect back into their painter's current coordinate space.
        a_inv, ok = self._ancestor_world_transform(obj).inverted()
        dest = QRectF(left, top, w_img / s, h_img / s)
        if ok:
            dest = a_inv.mapRect(dest)
        painter.drawImage(dest, img, QRectF(0, 0, w_img, h_img))
        painter.restore()

    def _flat_brush(self, obj: SceneObject):
        """The object's solid fill brush. ``color="none"`` means no fill."""
        if not obj.color or obj.color.lower() == "none":
            return Qt.NoBrush
        return QBrush(QColor(obj.color))

    def _local_path(self, obj: SceneObject):
        """The object's shape as a QPainterPath in its own (untransformed)
        local coordinates, cached for the lifetime of the shape data.

        Polygons are simple point lists (the native primitive format - no
        curve or subpath data). The cache is keyed on the identity of the
        ``shape_data`` dict, so replacing the data (or mutating it through a
        fresh dict) rebuilds the path.
        """
        sd = obj.shape_data
        fp = id(sd)
        slot = self._obj_render_cache.setdefault(obj, {}).get("qpath")
        if slot is not None and slot[0] == fp:
            return slot[1]
        path = QPainterPath()
        points = sd.get("points", [])
        if len(points) >= 2:
            path.moveTo(points[0][0], points[0][1])
            for p in points[1:]:
                path.lineTo(p[0], p[1])
            path.closeSubpath()
        self._obj_render_cache.setdefault(obj, {})["qpath"] = (fp, path)
        return path

    def _get_object_path(self, obj: SceneObject) -> QPainterPath:
        """World-space VISIBLE silhouette of `obj`'s subtree, expressed as one
        unified surface: the boolean union of exactly what actually renders.

        Unlike ``_get_object_raw_path`` this honours the mask semantics INSIDE
        the subtree, so an object that wraps another folder (or is selected /
        hovered / used as a clip base) clips against the folder's *visible*
        outline instead of the masked children's hidden full shapes:

          - a wrap-masked child contributes only the part that survives its
            clip (its shape intersected with its clip base's silhouette);
          - an erase-masked child contributes nothing and instead punches a
            hole out of the accumulated surface;
          - ordinary shapes and containers contribute their full geometry.

        The result is cached per subtree content; only the world-transform
        mapping is re-applied."""
        local = self._subtree_visible_union(obj)
        return self._world_transform(obj).map(local)

    def _get_object_raw_path(self, obj: SceneObject) -> QPainterPath:
        """World-space shape of ``obj``'s subtree IGNORING mask semantics:
        the boolean union of every visible object's full geometry, even the
        hidden parts of masked children.

        Used for the carving/zone/border geometry of the mask objects
        THEMSELVES - an erase mask's eraser shape, a wrap mask's own boundary
        to trace - never for the surface something else is clipped into."""
        return self._world_transform(obj).map(self._subtree_raw_union(obj))

    def _subtree_has_mask(self, obj: SceneObject) -> bool:
        for o in obj.iter_subtree():
            if o.is_mask:
                return True
        return False

    def _subtree_visible_union(self, obj: SceneObject) -> QPainterPath:
        """Mask-aware boolean union of ``obj``'s subtree in ``obj``'s own
        local space: exactly the region of the subtree that paints.

          - an ordinary visible shape contributes its full local shape;
          - an erase mask contributes nothing (it is itself invisible) and its
            shape is SUBTRACTED from the region accumulated so far (it carves
            a hole in whatever sits beneath it);
          - a wrap mask contributes only ``its shape ∩ its clip base's visible
            silhouette`` - the part its mask actually lets through. When its
            clip base is the container being computed itself (the fallback for
            a mask with no non-mask sibling beneath it) no extra clip applies,
            mirroring the renderer's "nothing beneath it" behaviour.

        Clip bases resolve through ``_get_object_path`` so nested folders
        reduce to the same truth the rasterizer paints."""
        slot = self._obj_render_cache.setdefault(obj, {}).get("visible_union")
        if self._drag_cached(obj, slot):
            return slot[1]
        fp = self._subtree_content_fp(obj)
        if slot is not None and slot[0] == fp:
            return slot[1]

        if not self._subtree_has_mask(obj):
            raw = self._subtree_raw_union(obj)
            self._obj_render_cache[obj]["visible_union"] = (fp, raw)
            return raw

        union = QPainterPath()
        root_world = self._world_transform(obj)
        inv_root, ok_root = root_world.inverted()

        def child_transform(t: QTransform, o: SceneObject) -> QTransform:
            c = QTransform(t)
            c.translate(o.transform.x, o.transform.y)
            c.rotate(o.transform.rotation)
            c.scale(o.transform.scale_x, o.transform.scale_y)
            return c

        def piece_of(o: SceneObject) -> QPainterPath:
            """``o``'s subtree geometry before any OUTER wrap clip: the raw
            union with the holes of ``o``'s own inner erase masks removed."""
            raw = self._subtree_raw_union(o)
            if not self._subtree_has_mask(o):
                return raw
            holes = QPainterPath()
            def collect_erase(o2: SceneObject, t2: QTransform) -> None:
                nonlocal holes
                if not o2.visible:
                    return
                if o2.is_mask and o2.mask_mode == "erase":
                    # An erase mask may itself be a symbol (container): its
                    # whole rendered subtree carves the hole, not just the
                    # container's own (empty) local geometry.
                    p = t2.map(self._subtree_raw_union(o2))
                    if not p.isEmpty():
                        holes = holes.united(p) if not holes.isEmpty() else p
                for c in o2.children:
                    collect_erase(c, child_transform(t2, c))
            collect_erase(o, QTransform())
            if holes.isEmpty():
                return raw
            return raw.subtracted(holes)

        def walk(o: SceneObject, t: QTransform) -> None:
            nonlocal union
            if not o.visible:
                return
            if o.is_mask and o.mask_mode == "erase":
                hole = t.map(self._subtree_raw_union(o))
                if not hole.isEmpty() and not union.isEmpty():
                    union = union.subtracted(hole)
                # An erase mask draws nothing of its own and its subtree
                # cannot paint either.
                return
            if o.is_mask:  # wrap mask
                own = t.map(piece_of(o))
                clip = None
                base = self._base_for_mask(o)
                if base is not None and base is not obj and ok_root:
                    base_sil = self._get_object_path(base)
                    clip = inv_root.map(base_sil)
                if clip is not None and not clip.isEmpty():
                    piece = own.intersected(clip)
                else:
                    piece = own
                if not piece.isEmpty():
                    union = union.united(piece) if not union.isEmpty() else piece
                # The wrap's whole subtree (including any nested wrap/erase
                # masks) was already folded into piece_of above.
                return
            p = t.map(self._get_local_shape(o, include_children=False))
            if not p.isEmpty():
                union = union.united(p) if not union.isEmpty() else p
            for c in o.children:
                if c.visible:
                    walk(c, child_transform(t, c))

        walk(obj, QTransform())
        self._obj_render_cache[obj]["visible_union"] = (fp, union)
        return union

    def _subtree_raw_union(self, obj: SceneObject) -> QPainterPath:
        """Boolean union of ``obj``'s subtree in ``obj``'s own local space
        (its own shape plus every visible descendant mapped by the descendant
        transforms below it, not ``obj``'s own transform), IGNORING mask
        semantics - wrapped and erased children keep their full shapes. Mapping
        that result by ``_world_transform(obj)`` recovers the world-space
        union, which is what lets drags reuse the cached geometry instead of
        rebuilding the union on every frame."""
        slot = self._obj_render_cache.setdefault(obj, {}).get("local_union")
        if self._drag_cached(obj, slot):
            return slot[1]
        fp = self._subtree_content_fp(obj)
        if slot is not None and slot[0] == fp:
            return slot[1]

        union = QPainterPath()

        # A boolean union of more than this many overlapping curved paths is
        # seconds-slow (Qt's `united` is effectively quadratic on accumulated
        # paths), so very dense subtrees fall back to their bounding-rect
        # silhouette for overlay/mask geometry instead of freezing the UI.
        if not self._subtree_is_small(obj):
            rect = self._subtree_local_bbox(obj)
            if not rect.isEmpty():
                union.addRect(rect)
            self._obj_render_cache[obj]["local_union"] = (fp, union)
            return union

        def child_transform(t: QTransform, o: SceneObject) -> QTransform:
            c = QTransform(t)
            c.translate(o.transform.x, o.transform.y)
            c.rotate(o.transform.rotation)
            c.scale(o.transform.scale_x, o.transform.scale_y)
            return c

        def walk(o: SceneObject, t: QTransform) -> None:
            nonlocal union
            if not o.visible:
                return
            local = self._get_local_shape(o, include_children=False)
            p = t.map(local)
            union = union.united(p) if not union.isEmpty() else p
            for c in o.children:
                walk(c, child_transform(t, c))

        walk(obj, QTransform())
        self._obj_render_cache[obj]["local_union"] = (fp, union)
        return union

    def _any_masks(self) -> bool:
        """True if the scene uses erase/clip masks anywhere. The live-drag
        sprite path bypasses the mask pipeline, so it is only enabled when no
        mask semantics are in play."""
        for o in self.scene.iter_objects():
            if o.is_mask:
                return True
        return False

    def _subtree_content_fp(self, obj: SceneObject):
        """Fingerprint of everything that shapes ``obj``'s subtree render,
        in ``obj``'s OWN local coordinate space: visibility, mask-ness, the
        shapes' data, fills/strokes and every CHILD's transform. The root's
        own transform is deliberately excluded - the local-space caches
        (union, bbox, sprite) are independent of it and it is re-applied
        cheaply at use time, so a plain MOVE drag leaves this fingerprint
        untouched and those caches stay valid for the whole drag."""
        items: list = []
        def walk(o: SceneObject, root: bool) -> None:
            items.append(id(o))
            items.append(1 if o.visible else 0)
            items.append(o.is_mask)
            items.append(o.mask_mode)
            if not root:
                t = o.transform
                items.append(t.x)
                items.append(t.y)
                items.append(t.rotation)
                items.append(t.scale_x)
                items.append(t.scale_y)
            items.append(id(o.shape_data))
            items.append(hash(o.color))
            for c in o.children:
                walk(c, False)
        walk(obj, True)
        return hash(tuple(items))

    def _drag_cached(self, obj: SceneObject, slot) -> bool:
        """A subtree being live-dragged cannot change content (edits only
        happen outside a drag), so its cached geometry can be trusted without
        re-fingerprinting the whole subtree every frame."""
        return self.transform_mode == TransformMode.MOVE and obj.id in self._live_ids and slot is not None

    def _subtree_local_bbox(self, obj: SceneObject) -> QRectF:
        """Axis-aligned bounding rect of ``obj``'s subtree in its own local
        space, merged from each shape's cheap ``boundingRect`` (no boolean
        path operations - those are seconds-slow on hundreds of curves). Used
        to size drag sprites."""
        slot = self._obj_render_cache.setdefault(obj, {}).get("local_bbox")
        if self._drag_cached(obj, slot):
            return slot[1]
        fp = self._subtree_content_fp(obj)
        if slot is not None and slot[0] == fp:
            return slot[1]

        bbox = QRectF()
        def child_transform(t: QTransform, o: SceneObject) -> QTransform:
            c = QTransform(t)
            c.translate(o.transform.x, o.transform.y)
            c.rotate(o.transform.rotation)
            c.scale(o.transform.scale_x, o.transform.scale_y)
            return c

        def walk(o: SceneObject, t: QTransform) -> None:
            nonlocal bbox
            if not o.visible:
                return
            local = self._get_local_shape(o, include_children=False)
            if not local.isEmpty():
                r = t.mapRect(local.boundingRect())
                bbox = bbox.united(r) if not bbox.isEmpty() else r
            for c in o.children:
                walk(c, child_transform(t, c))

        walk(obj, QTransform())
        self._obj_render_cache[obj]["local_bbox"] = (fp, bbox)
        return bbox

    _SMALL_SUBTREE_CAP = 384

    def _subtree_is_small(self, obj: SceneObject) -> bool:
        """True if ``obj``'s subtree is small enough for an exact boolean
        silhouette union to stay affordable."""
        count = 0
        stack = list(obj.children)
        while stack and count <= self._SMALL_SUBTREE_CAP:
            o = stack.pop()
            if o.visible:
                count += 1
            stack.extend(o.children)
        return count <= self._SMALL_SUBTREE_CAP

    def _sprite_contents(self, painter: QPainter, obj: SceneObject):
        """Draw ``obj``'s own shape followed by every child inside ``obj``'s
        local coordinate space (no enclosing obj transform - the caller has
        already positioned the sprite). Mirrors ``_draw_subtree`` but without
        the translate/rotate/scale of ``obj`` itself."""
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._flat_brush(obj))
        if obj.shape_type == "rect":
            w = obj.shape_data.get("width", 100)
            h = obj.shape_data.get("height", 80)
            painter.drawRect(-w / 2, -h / 2, w, h)
        elif obj.shape_type == "circle":
            r = obj.shape_data.get("radius", 50)
            painter.drawEllipse(QPointF(0, 0), r, r)
        elif obj.shape_type == "polygon":
            if obj.shape_data.get("points"):
                painter.drawPath(self._local_path(obj))
        for child in obj.children:
            if child.visible:
                self._draw_subtree(painter, child, {}, {})
        painter.setBrush(Qt.NoBrush)

    def _sprite_for(self, obj: SceneObject):
        """A QImage of ``obj``'s whole subtree baked once at the current zoom
        (resolution = zoom x devicePixelRatio), plus the local-space rect it
        covers. Reused verbatim while the object slides in a live drag - the
        only per-move cost is one ``drawImage`` with a translated rect."""
        dpr = self.devicePixelRatioF() or 1.0
        scale = max(self.zoom * dpr, 0.01)
        geom = (obj.transform.rotation, obj.transform.scale_x, obj.transform.scale_y)
        slot = self._obj_render_cache.setdefault(obj, {}).get("sprite")
        if self._drag_cached(obj, slot):
            return slot[3]
        fp = self._subtree_content_fp(obj)
        if slot is not None and slot[0] == fp and slot[1] == geom and slot[2] == round(scale * 1000):
            return slot[3]

        rect = self._subtree_local_bbox(obj)
        if rect.isEmpty():
            return None
        pad = 6.0
        r = rect.adjusted(-pad, -pad, pad, pad)
        w = max(1, math.ceil(r.width() * scale))
        h = max(1, math.ceil(r.height() * scale))
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(scale, scale)
        painter.translate(-r.left(), -r.top())
        self._sprite_contents(painter, obj)
        painter.end()
        val = (img, r)
        self._obj_render_cache[obj]["sprite"] = (fp, geom, round(scale * 1000), val)
        return val

    def _draw_live_overlays(self, painter: QPainter):
        """Blit the cached sprites of objects currently being moved. Enabled
        only for axis-aligned, un-scaled objects in a MOVE drag with no masks
        in the scene; otherwise falls back to the full-snapshot reraster."""
        if self.transform_mode != TransformMode.MOVE:
            return
        if not self._live_ids:
            return
        for obj in SelectionState.selected():
            if obj.id not in self._live_ids:
                continue
            sprite = self._sprite_for(obj)
            if sprite is None:
                continue
            img, local_rect = sprite
            dest = self._world_transform(obj).mapRect(local_rect)
            painter.drawImage(dest, img)

    def _end_live_drag(self):
        self._live_ids = set()
        self._cache_key = None

    def _get_local_shape(self, obj: SceneObject, include_children: bool = True) -> QPainterPath:
        local = QPainterPath()
        if obj.shape_type == "rect":
            w = obj.shape_data.get("width", 100)
            h = obj.shape_data.get("height", 80)
            local.addRect(-w / 2, -h / 2, w, h)
        elif obj.shape_type == "circle":
            r = obj.shape_data.get("radius", 50)
            local.addEllipse(QPointF(0, 0), r, r)
        elif obj.shape_type == "polygon":
            if obj.shape_data.get("points"):
                local.addPath(self._local_path(obj))
        # Containers (Symbols) contribute no geometry of their own; only their
        # children (shapes and nested containers) shape the union below.
        if include_children:
            for child in obj.children:
                t = QTransform()
                t.translate(child.transform.x, child.transform.y)
                t.rotate(child.transform.rotation)
                t.scale(child.transform.scale_x, child.transform.scale_y)
                local.addPath(t.map(self._get_local_shape(child)))
        return local

    def _ancestors(self, obj: SceneObject) -> list[SceneObject]:
        """Ancestors from the root down to (but excluding) `obj`."""
        chain: list[SceneObject] = []
        cur = self.scene.find_parent(obj)
        while cur is not None:
            chain.append(cur)
            cur = self.scene.find_parent(cur)
        chain.reverse()
        return chain

    def _ancestor_world_transform(self, obj: SceneObject) -> QTransform:
        """Composed transform of everything above `obj` (its parent chain)."""
        t = QTransform()
        for o in self._ancestors(obj):
            tr = o.transform
            t.translate(tr.x, tr.y)
            t.rotate(tr.rotation)
            t.scale(tr.scale_x, tr.scale_y)
        return t

    def _world_transform(self, obj: SceneObject) -> QTransform:
        """Composed transform of `obj` and every ancestor (world space)."""
        t = self._ancestor_world_transform(obj)
        tr = obj.transform
        t.translate(tr.x, tr.y)
        t.rotate(tr.rotation)
        t.scale(tr.scale_x, tr.scale_y)
        return t

    def _world_position(self, obj: SceneObject) -> QPointF:
        return self._world_transform(obj).map(QPointF(0, 0))

    def _set_local_position(self, obj: SceneObject, world_pos: QPointF) -> None:
        """Convert a world-space position back into the object's local
        coordinates (relative to its parent chain) and store it."""
        inv, ok = self._ancestor_world_transform(obj).inverted()
        if ok:
            local = inv.map(world_pos)
            obj.transform.x = local.x()
            obj.transform.y = local.y()

    def _transform_point_to_local(self, point: QPointF, transform) -> QPointF:
        """Apply the inverse of a single Transform to a point (world -> local)."""
        lx = point.x() - transform.x
        ly = point.y() - transform.y
        angle = -math.radians(transform.rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        px = lx * cos_a - ly * sin_a
        py = lx * sin_a + ly * cos_a
        if transform.scale_x != 0:
            px /= transform.scale_x
        if transform.scale_y != 0:
            py /= transform.scale_y
        return QPointF(px, py)

    def _base_for_mask(self, obj: SceneObject) -> SceneObject | None:
        """The surface a mask layer clips into / erases.

        A mask reacts to what is directly BENEATH it, mirroring the flat-layer
        behaviour: inside a group it targets the nearest non-mask sibling
        under it (stepping up past any run of sibling masks); if there is no
        non-mask sibling beneath it falls back to the group's own unified
        surface (its parent). A top-level mask targets the nearest non-mask
        top-level object beneath it. Returns ``None`` for non-mask objects or
        when no target surface exists.
        """
        if not obj.is_mask:
            return None
        parent = self.scene.find_parent(obj)
        if parent is not None:
            children = parent.children
            try:
                idx = children.index(obj)
            except ValueError:
                return parent
            j = idx - 1
            while j >= 0 and children[j].is_mask:
                j -= 1
            return children[j] if j >= 0 else parent
        try:
            idx = self.scene.objects.index(obj)
        except ValueError:
            return None
        j = idx - 1
        while j >= 0 and self.scene.objects[j].is_mask:
            j -= 1
        return self.scene.objects[j] if j >= 0 else None

    def _mask_clip_source(self, obj: SceneObject) -> SceneObject | None:
        """The object that clips ``obj``'s rendering, if any."""
        if not obj.is_mask:
            return None
        return self._base_for_mask(obj)

    def _effective_clip_paths(self, obj: SceneObject) -> list[QPainterPath]:
        """World-space clip paths that constrain ``obj``'s painted region.

        A wrap mask clips its whole subtree to its base's silhouette, so a
        normal (non-mask) object nested inside one inherits that clip too.
        This walks up the hierarchy collecting every wrap-mask clip above (or
        on) ``obj``; the highlight is then clipped to their intersection -
        exactly the treatment ``obj`` gets when the wrap mask itself is the
        one being aimed at.
        """
        clips: list[QPainterPath] = []
        cur = obj
        while cur is not None:
            if cur.is_mask and cur.mask_mode == "wrap":
                base = self._base_for_mask(cur)
                if base is not None:
                    clip = self._get_object_path(base)
                    if not clip.isEmpty():
                        clips.append(clip)
            cur = self.scene.find_parent(cur)
        return clips

    def _compute_mask_paths(self):
        """Resolve erase/clip mask interactions over the whole hierarchy.

        Returns ``(base_holes, clip_holes)``:
          - ``base_holes[base_id]`` = union of every erase targeting that
            base's unified surface (carved out when the base is drawn);
          - ``clip_holes[mask_id]`` = union of the erases sitting ABOVE that
            clip mask within its run (carved out of the region it clips into).
        """
        order = list(self.scene.iter_objects())
        base_runs: dict[str, list[tuple[int, SceneObject]]] = {}
        for i, obj in enumerate(order):
            if not obj.visible or not obj.is_mask:
                continue
            base = self._base_for_mask(obj)
            if base is not None:
                base_runs.setdefault(base.id, []).append((i, obj))

        base_holes: dict[str, QPainterPath] = {}
        clip_holes: dict[str, QPainterPath] = {}
        for base_id, run in base_runs.items():
            # Higher pre-order index sits on top, so sort top-most first.
            run = sorted(run, key=lambda item: -item[0])
            accum = QPainterPath()
            base_hole = QPainterPath()
            for _, mask in run:
                if mask.mask_mode == "erase":
                    # The eraser's carved shape is its FULL geometry - an
                    # erase mask is never clipped by anything, it only carves.
                    p = self._get_object_raw_path(mask)
                    accum = accum.united(p)
                    base_hole = base_hole.united(p)
                else:  # clip mask
                    clip_holes[mask.id] = accum
            if not base_hole.isEmpty():
                base_holes[base_id] = base_hole
        return base_holes, clip_holes

    def _clip_path_for(self, obj: SceneObject, base_holes, clip_holes) -> QPainterPath | None:
        """World-space clip to apply while drawing ``obj``'s subtree, or None.

        A base object is clipped to its unified surface minus every erase
        targeting it. A wrap mask is additionally clipped to its base's union
        minus the erases above it in its run (which "removes alpha" from the
        clip mask too).
        """
        hole = base_holes.get(obj.id)
        if obj.is_mask and obj.mask_mode == "wrap":
            base = self._base_for_mask(obj)
            if base is None and hole is None:
                return None
            # Clip into the base's VISIBLE silhouette - a folder with masked
            # children clips to its true painted outline, not the hidden
            # full shapes of the children.
            clip = self._get_object_path(base) if base is not None else self._get_object_raw_path(obj)
            eab = clip_holes.get(obj.id)
            if eab is not None and not eab.isEmpty():
                clip = clip.subtracted(eab)
            if hole is not None and not hole.isEmpty():
                clip = clip.subtracted(hole)
            return clip
        if hole is not None and not hole.isEmpty():
            return self._get_object_path(obj).subtracted(hole)
        return None

    def _apply_clip(self, painter: QPainter, obj: SceneObject, clip_world: QPainterPath) -> None:
        """Set ``clip_world`` (canvas/world coordinates) as the painter clip.

        The painter may already carry the transforms of ``obj``'s ancestors
        (when drawing a nested object), so the clip is mapped back into that
        space first - otherwise it would be double-transformed and misalign.

        If a clip is already active (an inner mask inside a symbol that is
        itself a mask inherits the outer symbol's clip), the new clip is
        INTERSECTED into it instead of replacing it - otherwise the inner
        content leaks past the parent symbol's boundary.
        """
        t = self._ancestor_world_transform(obj)
        inv, ok = t.inverted()
        mapped = inv.map(clip_world) if ok and not t.isIdentity() else clip_world
        if painter.hasClipping():
            painter.setClipPath(mapped, Qt.ClipOperation.IntersectClip)
        else:
            painter.setClipPath(mapped)

    def _visible_region(self, obj: SceneObject) -> QPainterPath:
        """The portion of ``obj``'s path that actually renders.

        For ordinary objects this is simply the object's full path. For a
        masked object (``obj.is_mask``) it is the intersection of the
        object's path with the path of whatever clips it - the same
        boolean-path approach previously used to compute occlusion by
        objects drawn on top, now repurposed for mask clipping instead.
        An erase-mode mask renders nothing of its own, so it has no visible
        region.
        """
        if obj.is_mask and obj.mask_mode == "erase":
            return QPainterPath()
        obj_path = self._get_object_path(obj)
        # Holes carved by sibling erase masks targeting this base must show in
        # the outline too, not just in the rendered pixels.
        parent = self.scene.find_parent(obj)
        siblings = parent.children if parent is not None else self.scene.objects
        for sib in siblings:
            if (
                sib is not obj
                and sib.visible
                and sib.is_mask
                and sib.mask_mode == "erase"
                and self._base_for_mask(sib) is obj
            ):
                hole = self._get_object_raw_path(sib)
                if not hole.isEmpty():
                    obj_path = obj_path.subtracted(hole)
        clip_source = self._mask_clip_source(obj)
        if clip_source is None:
            return obj_path
        clip_path = self._get_object_path(clip_source)
        return obj_path.intersected(clip_path)

    def _animate_selection(self):
        self._selection_dash_offset += 1.0
        if self._selection_dash_offset > 10000:
            self._selection_dash_offset = 0.0
        if SelectionState.selected():
            # A selected wrap mask's striped clipped zone is baked into the
            # body snapshot (it is drawn during the object pass so it sits
            # behind its clip source), so its stripes only advance when that
            # snapshot is rebuilt. Nudge the cache each tick - including
            # mid-transform, where the erase mask's live-drawn zone already
            # advances - so they animate in sync with the selection.
            if any(
                o.is_mask and o.mask_mode == "wrap" for o in SelectionState.selected()
            ):
                self._cache_key = None
            self.update()

    def _stroke_selection_border(self, painter: QPainter, path, solid_w: float, dashed_w: float):
        """Stroke ``path`` with the selection border: solid black underneath,
        animated accent dashes on top.
        """
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0), solid_w))
        painter.drawPath(path)

        pen = QPen(Theme.ACCENT, dashed_w)
        pen.setStyle(Qt.DashLine)
        pen.setDashOffset(self._selection_dash_offset)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _draw_selection(self, painter: QPainter, obj: SceneObject):
        painter.save()

        inv_zoom = 1.0 / self.zoom if self.zoom > 0 else 1.0
        solid_w = 2.5 * inv_zoom
        dashed_w = 1.5 * inv_zoom

        # A wrap mask clips its whole subtree, so a selected child inside one
        # must have its border wrap only the visibly-unclipped region. Masks
        # do this via painter clipping (robust at any angle) rather than a
        # boolean intersection, so use the same technique here when the
        # object inherits clips from its masked ancestors.
        clips = self._effective_clip_paths(obj)
        if clips:
            obj_path = self._visible_region(obj)
            painter.save()
            painter.setClipPath(clips[0], Qt.ClipOperation.ReplaceClip)
            for clip_path in clips[1:]:
                painter.setClipPath(clip_path, Qt.ClipOperation.IntersectClip)
            self._stroke_selection_border(painter, obj_path, solid_w, dashed_w)
            painter.restore()
            for clip_path in clips:
                painter.save()
                painter.setClipPath(obj_path, Qt.ClipOperation.ReplaceClip)
                for other in clips:
                    if other is clip_path:
                        continue
                    painter.setClipPath(other, Qt.ClipOperation.IntersectClip)
                self._stroke_selection_border(painter, clip_path, solid_w, dashed_w)
                painter.restore()
        else:
            path = self._visible_region(obj)
            self._stroke_selection_border(painter, path, solid_w, dashed_w)

        pivot = 4 * inv_zoom
        painter.setPen(QPen(Theme.ACCENT, 1 * inv_zoom))
        painter.setBrush(Theme.ACCENT)
        painter.drawEllipse(self._world_position(obj), pivot, pivot)

        painter.restore()

    def _draw_clipped_zone(self, painter: QPainter, obj: SceneObject, color: QColor | None = None) -> None:
        """Zone marking the part of a selected masked object that is cut off
        by its mask.

        Drawn over the object's full shape with no clipping, in the object
        pass right before the mask-source object, so it naturally sits behind
        that object in the stack. Defaults to the purple used for wrap-mode
        masks; erase-mode masks reuse it in red to mark their (invisible)
        carving shape.
        """
        if color is None:
            color = QColor(148, 0, 211)
        painter.save()
        # Called both from the top-level selection pass (painter already in
        # canvas space) and from inside the recursive object pass, where the
        # painter may carry ancestor transforms. The zone path is in world
        # coordinates, so reset to canvas space to keep it aligned.
        if self._paint_canvas_transform is not None:
            painter.setTransform(self._paint_canvas_transform)
        # The zone marks the masked object's own (full) carving/boundary
        # shape - what its mask hides - so raw geometry, not the visible
        # silhouette.
        obj_path = self._get_object_raw_path(obj)
        inv_zoom = 1.0 / self.zoom if self.zoom > 0 else 1.0

        zone_fill = QColor(color)
        zone_fill.setAlphaF(0.25)
        zone_stripe = QColor(color)
        zone_stripe.setAlphaF(0.25)

        painter.setPen(Qt.NoPen)
        painter.setBrush(zone_fill)
        painter.drawPath(obj_path)

        shader = StripeShader(color=zone_stripe)
        shader.paint(painter, obj_path, zoom=self.zoom)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 2.5 * inv_zoom))
        painter.drawPath(obj_path)

        # Pivot dot, so an erase-mode mask (whose selection uses this zone
        # instead of the normal border path) still shows its transform pivot.
        painter.setPen(QPen(Theme.ACCENT, 1 * inv_zoom))
        painter.setBrush(Theme.ACCENT)
        painter.drawEllipse(self._world_position(obj), 4 * inv_zoom, 4 * inv_zoom)

        painter.restore()

    def _draw_masked_selection(self, painter: QPainter, obj: SceneObject) -> None:
        """Selection highlight for a masked (``is_mask``) object.

        The purple clipped zone (fill + stripes + outline) is drawn in the
        object pass behind the mask source by ``_draw_clipped_zone``. Here we
        only draw the animated selection border wrapping whatever is actually
        visible, plus the pivot dot.

        The visible region's border is NOT computed with boolean path
        intersection (``QPainterPath::intersected`` is unreliable at angles).
        Instead the painter's own clipping - which the rasterizer evaluates
        robustly - is used: the border of ``obj ∩ clip`` is the union of
        ``∂obj`` inside the clip and ``∂clip`` inside ``obj``.
        """
        painter.save()
        inv_zoom = 1.0 / self.zoom if self.zoom > 0 else 1.0
        # `obj` here is the mask ITSELF, so its border traces its own full
        # boundary (∂obj), clipped to the clip source's VISIBLE silhouette -
        # plus any wrap-mask clips inherited from ancestors when this mask
        # sits inside a symbol that is itself a mask.
        obj_path = self._get_object_raw_path(obj)

        solid_w = 2.5 * inv_zoom
        dashed_w = 1.5 * inv_zoom

        clips = self._effective_clip_paths(obj)

        # -- Selection border wrapping the visible (un-clipped) region -------
        if clips:
            # ∂obj inside the clips
            painter.save()
            painter.setClipPath(clips[0], Qt.ClipOperation.ReplaceClip)
            for extra in clips[1:]:
                painter.setClipPath(extra, Qt.ClipOperation.IntersectClip)
            self._stroke_selection_border(painter, obj_path, solid_w, dashed_w)
            painter.restore()
            # ∂clip inside obj (for every clip, incl. the outer ones)
            for clip_path in clips:
                painter.save()
                painter.setClipPath(obj_path, Qt.ClipOperation.ReplaceClip)
                for other in clips:
                    if other is clip_path:
                        continue
                    painter.setClipPath(other, Qt.ClipOperation.IntersectClip)
                self._stroke_selection_border(painter, clip_path, solid_w, dashed_w)
                painter.restore()
        else:
            self._stroke_selection_border(painter, obj_path, solid_w, dashed_w)

        # -- Pivot dot -----------------------------------------------------
        pivot = 4 * inv_zoom
        painter.setPen(QPen(Theme.ACCENT, 1 * inv_zoom))
        painter.setBrush(Theme.ACCENT)
        painter.drawEllipse(self._world_position(obj), pivot, pivot)

        painter.restore()

    def _draw_transform_gizmo(self, painter: QPainter):
        if not SelectionState.selected():
            return
        obj = SelectionState.selected()[0]
        inv_zoom = 1.0 / self.zoom if self.zoom > 0 else 1.0

        if self.transform_mode != TransformMode.ROTATE:
            return

        painter.save()

        pen = QPen(QColor(0, 200, 100), 1 * inv_zoom)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        radius = 50 * inv_zoom
        center = self._avg_pivot if SelectionState.selected() else QPointF(obj.transform.x, obj.transform.y)
        painter.drawEllipse(center, radius, radius)

        painter.restore()

    def _animate_hover_highlight(self):
        if self.hovered_object:
            self.update()
        else:
            self._hover_anim_timer.stop()

    def _draw_hover_highlight(self, painter: QPainter, obj: SceneObject):
        """Hover highlight: a subtle 25% fill + 25% stripes + hairline
        outline over the object's full visible region, always on top of
        everything else.

        For a masked object the visible region is ``obj ∩ clip``. Like the
        selection border, this is evaluated with painter clipping (robust at
        any angle) instead of boolean path intersection: the fill/stripes are
        clipped to ``obj ∩ clip`` and the outline is ``∂obj`` inside the clip
        plus ``∂clip`` inside ``obj``.
        """
        painter.save()
        if obj.is_mask and obj.mask_mode == "erase":
            # Erase-mode mask: show the animated striped filling over the
            # mask's own carving shape (the part that gets erased), with no
            # stroke - matching the wrap-mode mask's hover filling. Both the
            # solid gray underpinning and the stripes are drawn within the
            # exact same clip-path boundary so they never disagree at edges.
            obj_path = self._get_object_raw_path(obj)

            erase_fill = QColor(190, 190, 190)
            erase_fill.setAlphaF(0.25)
            erase_stripe = QColor(190, 190, 190)
            erase_stripe.setAlphaF(0.25)

            painter.save()
            painter.setClipPath(obj_path)
            inv_zoom = 1.0 / self.zoom if self.zoom > 0 else 1.0
            pad = 12.0 * inv_zoom
            paint_rect = obj_path.boundingRect().adjusted(-pad, -pad, pad, pad)
            painter.setPen(Qt.NoPen)
            painter.setBrush(erase_fill)
            painter.drawRect(paint_rect)
            painter.restore()

            shader = StripeShader(color=erase_stripe)
            shader.paint(painter, obj_path, zoom=self.zoom)
            painter.restore()
            return
        inv_zoom = 1.0 / self.zoom if self.zoom > 0 else 1.0
        # A mask traces its own (raw) carving/boundary shape, clipped to its
        # clip source's visible silhouette below. A plain object/container
        # instead highlights its mask-aware visible silhouette - erase masks
        # inside a group are invisible and must not show as solid layers.
        obj_path = (
            self._get_object_raw_path(obj)
            if obj.is_mask
            else self._visible_region(obj)
        )

        fill = QColor(190, 190, 190)
        fill.setAlphaF(0.25)
        stripe = QColor(190, 190, 190)
        stripe.setAlphaF(0.25)

        clips = self._effective_clip_paths(obj)

        if clips:
            # -- Visible region fill + animated stripes (obj ∩ ⋂clips) -------
            painter.save()
            painter.setClipPath(obj_path, Qt.ClipOperation.ReplaceClip)
            for clip_path in clips:
                painter.setClipPath(clip_path, Qt.ClipOperation.IntersectClip)

            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawPath(obj_path)

            shader = StripeShader(color=stripe)
            shader.paint(painter, obj_path, zoom=self.zoom)
            painter.restore()

            # -- Hairline outline: ∂obj inside clips + ∂clip inside obj ------
            painter.save()
            painter.setClipPath(clips[0], Qt.ClipOperation.ReplaceClip)
            for clip_path in clips[1:]:
                painter.setClipPath(clip_path, Qt.ClipOperation.IntersectClip)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0), 1.0 * inv_zoom))
            painter.drawPath(obj_path)
            painter.restore()

            for clip_path in clips:
                painter.save()
                painter.setClipPath(obj_path, Qt.ClipOperation.ReplaceClip)
                for other in clips:
                    if other is clip_path:
                        continue
                    painter.setClipPath(other, Qt.ClipOperation.IntersectClip)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(0, 0, 0), 1.0 * inv_zoom))
                painter.drawPath(clip_path)
                painter.restore()
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawPath(obj_path)

            shader = StripeShader(color=stripe)
            shader.paint(painter, obj_path, zoom=self.zoom)

            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0), 1.0 * inv_zoom))
            painter.drawPath(obj_path)

        painter.restore()

    def viewport_to_canvas(self, pos: QPointF) -> QPointF:
        cw = self.CANVAS_SIZE * self.zoom
        ch = self.CANVAS_SIZE * self.zoom
        cx = (self.width() - cw) / 2 + self._pan.x()
        cy = (self.height() - ch) / 2 + self._pan.y()
        return QPointF((pos.x() - cx) / self.zoom, (pos.y() - cy) / self.zoom)

    def hit_test(self, canvas_pos: QPointF) -> SceneObject | None:
        # Fast path: with no masks anywhere, nothing needs clipping.
        if not self._any_masks():
            return self._hit_test_plain(canvas_pos)
        return self._hit_test_masked(canvas_pos)

    def _hit_test_plain(self, canvas_pos: QPointF) -> SceneObject | None:
        def walk(objs, local_point):
            for o in reversed(objs):
                if not o.visible or o.locked:
                    continue
                child_local = self._transform_point_to_local(local_point, o.transform)
                # Children render above the parent, so test them first.
                hit = walk(o.children, child_local)
                if hit is not None:
                    return hit
                if self._local_point_in_object(child_local, o):
                    return o
                if o.is_mask and o.mask_mode == "wrap":
                    raw = self._subtree_raw_union(o)
                    if not raw.isEmpty() and raw.contains(child_local):
                        return o
            return None

        return walk(self.scene.objects, canvas_pos)

    def _hit_test_masked(self, canvas_pos: QPointF) -> SceneObject | None:
        def walk(objs, local_point):
            for o in reversed(objs):
                if not o.visible or o.locked:
                    continue
                child_local = self._transform_point_to_local(local_point, o.transform)
                hit = walk(o.children, child_local)
                if hit is not None:
                    return hit
                if o.is_mask:
                    region = self._mask_hit_region_local(o)
                    if not region.isEmpty() and region.contains(child_local):
                        return o
                elif self._local_point_in_object(child_local, o):
                    region = self._visible_hit_region_local(o)
                    if not region.isEmpty() and region.contains(child_local):
                        return o
            return None

        return walk(self.scene.objects, canvas_pos)

    def _mask_hit_region_local(self, mask: SceneObject) -> QPainterPath:
        """Local-space surface of a mask object that is actually clickable.

        A wrap mask's clickable surface is the geometry it clips with (its
        whole rendered subtree - a symbol mask has shape only through its
        children), confined to the visible silhouette of the base it targets.
        An erase mask has NO clickable surface: it is invisible and its hole
        is empty space, so the erased object's hitbox truly ends at the hole
        (hovering/clicking there must react to nothing, not to the base nor
        to the mask itself). Erase masks are still selected from the outliner,
        like containers."""
        if mask.mask_mode == "erase":
            return QPainterPath()
        region = self._subtree_raw_union(mask)
        base = self._base_for_mask(mask)
        inv, ok = self._world_transform(mask).inverted()
        if base is not None and not region.isEmpty():
            base_region = self._get_object_path(base)
            if not base_region.isEmpty():
                if ok:
                    region = region.intersected(inv.map(base_region))
        # A mask nested under an erased base is clipped by the ancestor's
        # carved holes just like everything else in that base's subtree.
        for hole_world in self._ancestor_erase_holes(mask):
            hole = inv.map(hole_world) if ok else hole_world
            if not hole.isEmpty():
                region = region.subtracted(hole)
        return region

    def _ancestor_erase_holes(self, obj: SceneObject) -> list:
        """World-space erase holes that clip ``obj``'s painted region.

        A base object's whole subtree is clipped to the base surface minus
        every erase mask targeting it (matching ``_draw_object``'s clip), so
        holes sink down to every descendant - not just the base itself."""
        holes: list = []
        cur = obj
        while cur is not None:
            parent = self.scene.find_parent(cur)
            siblings = parent.children if parent is not None else self.scene.objects
            for sib in siblings:
                if (
                    sib is not cur
                    and sib.visible
                    and sib.is_mask
                    and sib.mask_mode == "erase"
                    and self._base_for_mask(sib) is cur
                ):
                    p = self._get_object_raw_path(sib)
                    if not p.isEmpty():
                        holes.append(p)
            cur = parent
        return holes

    def _visible_hit_region_local(self, obj: SceneObject) -> QPainterPath:
        """Local-space portion of ``obj`` that actually renders and is
        clickable: its mask-aware path, with every erase hole that clips it
        (from its own siblings AND from the bases it is nested under) punched
        out, then clipped by wrap masks inherited from its ancestors."""
        inv, ok = self._world_transform(obj).inverted()
        world = None if (ok and inv.isIdentity()) else self._world_transform(obj)
        path = self._get_object_path(obj)
        if world is not None:
            path = inv.map(path)
        # Erase masks targeting this object or any of its ancestors carve
        # holes out of its rendered region, so they carve its hitbox too.
        for hole_world in self._ancestor_erase_holes(obj):
            hole = inv.map(hole_world) if world is not None else hole_world
            if not hole.isEmpty():
                path = path.subtracted(hole)
        # Wrap masks inherited from ancestors clip the hit region too.
        for clip_world in self._effective_clip_paths(obj):
            clip = inv.map(clip_world) if world is not None else clip_world
            if not clip.isEmpty():
                path = path.intersected(clip)
        return path

    def _local_point_in_object(self, local: QPointF, obj: SceneObject) -> bool:
        """Test a point already expressed in `obj`'s local space against the
        object's own geometry (its scene-children are handled by the caller)."""
        lx = local.x()
        ly = local.y()

        if obj.shape_type == "rect":
            w = obj.shape_data.get("width", 100)
            h = obj.shape_data.get("height", 80)
            return abs(lx) <= w / 2 and abs(ly) <= h / 2
        elif obj.shape_type == "circle":
            r = obj.shape_data.get("radius", 50)
            return lx ** 2 + ly ** 2 <= r ** 2
        elif obj.shape_type == "polygon":
            points = obj.shape_data.get("points", [])
            if not points:
                return False
            return self._local_path(obj).contains(local)
        # Containers (Symbols) have no own geometry, so they are never hit on
        # the stage directly - only the shapes they contain are. Select a
        # container from the outliner to move the whole group.
        return False

    def focusOutEvent(self, event):
        # Do NOT cancel transform on simple focus changes caused by mouse grabbing/warping.
        # Only cancel if explicit focus policy requires it, or handle cleanly.
        if self.transform_mode != TransformMode.NONE and not self.underMouse():
            # Keep transform intact unless deliberately destroyed
            pass
        super().focusOutEvent(event)

    def _wrap_mouse(self, current_global_pos: QPoint) -> tuple[float, float]:
        if self._last_global is None:
            self._last_global = current_global_pos
            return 0.0, 0.0

        # Calculate raw screen pixel movement
        raw_dx = float(current_global_pos.x() - self._last_global.x())
        raw_dy = float(current_global_pos.y() - self._last_global.y())

        screen = QGuiApplication.screenAt(current_global_pos) or QGuiApplication.primaryScreen()
        rect = screen.geometry()

        left, top = rect.left(), rect.top()
        right, bottom = rect.right() - 1, rect.bottom() - 1

        target_x = current_global_pos.x()
        target_y = current_global_pos.y()
        wrapped = False

        margin = 10
        if current_global_pos.x() <= left:
            target_x = right - margin
            wrapped = True
        elif current_global_pos.x() >= right:
            target_x = left + margin
            wrapped = True

        if current_global_pos.y() <= top:
            target_y = bottom - margin
            wrapped = True
        elif current_global_pos.y() >= bottom:
            target_y = top + margin
            wrapped = True

        if wrapped:
            new_pos = QPoint(int(target_x), int(target_y))
            # NEVER call releaseMouse() here - keep the grab active!
            QCursor.setPos(new_pos)
            self._last_global = new_pos
        else:
            self._last_global = current_global_pos

        # Divide by zoom scale so movement speed is invariant to view scale
        zoom_level = getattr(self, "zoom", 1.0)
        if zoom_level <= 0:
            zoom_level = 1.0

        canvas_dx = raw_dx / zoom_level
        canvas_dy = raw_dy / zoom_level

        return canvas_dx, canvas_dy

    def mouseMoveEvent(self, event):
        global_pos = event.globalPosition().toPoint()
        is_transforming = getattr(self, "transform_mode", TransformMode.NONE) != TransformMode.NONE

        if is_transforming or getattr(self, "_panning", False):
            dx, dy = self._wrap_mouse(global_pos)

            if dx == 0 and dy == 0:
                event.accept()
                return

            if getattr(self, "_panning", False):
                self._pan += QPointF(dx * getattr(self, "zoom", 1.0), dy * getattr(self, "zoom", 1.0))
            else:
                self._transform_delta += QPointF(dx, dy)
                self._update_transform()

            self.update()
            event.accept()
            return

        self._last_global = global_pos
        canvas_pos = self.viewport_to_canvas(event.position())
        self._last_canvas_pos = canvas_pos
        if hasattr(self, "_update_hover"):
            self._update_hover(canvas_pos)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        global_pos = event.globalPosition().toPoint()

        # Middle-Click / Shift+Middle-Click Pan
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._last_global = global_pos
            self.setCursor(Qt.ClosedHandCursor)
            self.grabMouse()
            event.accept()
            return

        # Left-Click confirmation or selection toggle
        if event.button() == Qt.LeftButton:
            self.setFocus()

            # Confirm transform on click if active
            if getattr(self, "transform_mode", TransformMode.NONE) != TransformMode.NONE:
                self._confirm_transform()
                event.accept()
                return

            # Selection handling
            canvas_pos = self.viewport_to_canvas(event.position())
            additive = bool(event.modifiers() & Qt.ShiftModifier)
            if hasattr(self, "hit_test") and hasattr(self, "_update_selection"):
                obj = self.hit_test(canvas_pos)
                self._update_selection(obj, additive=additive)

            self._last_global = global_pos
            self.grabMouse()
            event.accept()
            return

        super().mousePressEvent(event)
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and getattr(self, "_panning", False):
            self._panning = False
            self._last_global = None
            self.releaseMouse()
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return

        if event.button() == Qt.LeftButton and getattr(self, "transform_mode", TransformMode.NONE) == TransformMode.NONE:
            self.releaseMouse()
            self._last_global = None

        super().mouseReleaseEvent(event)


    def wheelEvent(self, event):
        if event.modifiers() & Qt.ShiftModifier:
            delta = event.angleDelta().x() or event.angleDelta().y()
            if delta == 0:
                delta = event.pixelDelta().x() or event.pixelDelta().y()
            if delta == 0:
                return
            factor = 1.15 if delta > 0 else 1 / 1.15

            # Mark active zoom state to scale the bitmap instantly
            self._is_zooming = True
            self._zoom_at(event.position(), factor)

            # Reset timer; fires 150ms after the wheel stops moving
            self._sharpen_timer.start()

            event.accept()
            return
        super().wheelEvent(event)

    def _zoom_at(self, view_pos: QPointF, factor: float):
        old_zoom = self.zoom
        new_zoom = max(0.05, min(old_zoom * factor, 8.0))
        if new_zoom == old_zoom:
            return
        anchor = self.viewport_to_canvas(view_pos)
        self.zoom = new_zoom
        cw = self.CANVAS_SIZE * self.zoom
        ch = self.CANVAS_SIZE * self.zoom
        cx = (self.width() - cw) / 2 + self._pan.x()
        cy = (self.height() - ch) / 2 + self._pan.y()
        new_x = cx + anchor.x() * self.zoom
        new_y = cy + anchor.y() * self.zoom
        self._pan += QPointF(view_pos.x() - new_x, view_pos.y() - new_y)
        self.update()

    def handle_key_press(self, event):
        key = event.key()

        if self.transform_mode != TransformMode.NONE:
            if key == Qt.Key_Escape:
                self._cancel_transform()
                return
            if key == Qt.Key_X:
                self._constraint_axis = "x"
                self._update_status_for_constraint()
                self._update_transform()
                self.update()
                return
            elif key == Qt.Key_Y:
                self._constraint_axis = "y"
                self._update_status_for_constraint()
                self._update_transform()
                self.update()
                return
            return

        if key == Qt.Key_M and self.selected_object and self._cursor_over_viewport():
            self._toggle_mask()
            return

        if key == Qt.Key_G and SelectionState.selected() and self._cursor_over_viewport():
            self._start_transform(TransformMode.MOVE)
        elif key == Qt.Key_R and SelectionState.selected() and self._cursor_over_viewport():
            self._start_transform(TransformMode.ROTATE)
        elif key == Qt.Key_S and SelectionState.selected() and self._cursor_over_viewport():
            self._start_transform(TransformMode.SCALE)
        elif key == Qt.Key_I and SelectionState.selected():
            self._insert_keyframe()
        if key == Qt.Key_Delete and SelectionState.selected():
            entries = []
            for obj in list(SelectionState.selected()):
                parent_list = self.scene.objects
                parent = self.scene.find_parent(obj)
                if parent is not None:
                    parent_list = parent.children
                idx = parent_list.index(obj) if obj in parent_list else len(parent_list)
                entries.append((obj, parent_list, idx))
                parent_list.remove(obj)
            cmd = DeleteObjectsCommand(entries)
            self.history.push(cmd)
            SelectionState.clear_selected()
            # The deselect is part of the same operation, not a second edit:
            # sync it in place so the implicit save_selection() the emit below
            # triggers doesn't push an extra undo step (which would make the
            # deleted object only come back after *two* undos).
            self.history.sync_selection(None)
            self.selection_changed.emit(None)
            self.update()
            for stage in self._all_stages():
                stage.update()
            # Deleting is the outliner's one structural change that doesn't
            # originate inside it, so it never self-invalidates. Without this
            # the removed row stays painted until the cursor next happens to
            # move over the outliner. Thumbnail cache is dropped too: a
            # remaining parent's thumbnail may have contained the deleted
            # subtree.
            for outliner in self._all_outliners():
                outliner.invalidate_thumbnails()
            return

    def keyPressEvent(self, event):
        self.handle_key_press(event)
        if event.key() == Qt.Key_Alt and self.hovered_object:
            self._update_outline_target()

    def keyReleaseEvent(self, event):
        super().keyReleaseEvent(event)
        if event.key() == Qt.Key_Alt and self.hovered_object:
            self._update_outline_target()

    def focusOutEvent(self, event):
        # Losing focus mid-transform (e.g. alt-tabbing away) would otherwise
        # leave the mouse grab held indefinitely, since neither
        # _confirm_transform nor _cancel_transform would run to release it.
        if self.transform_mode != TransformMode.NONE:
            self._cancel_transform()
        super().focusOutEvent(event)

    def _update_status_for_constraint(self):
        names = {
            TransformMode.MOVE: "Move",
            TransformMode.ROTATE: "Rotate",
            TransformMode.SCALE: "Scale",
        }
        name = names.get(self.transform_mode, "")
        axis = self._constraint_axis.upper() if self._constraint_axis else ""
        self.status_message.emit(f"{name} ({axis}): move mouse, click to confirm, Esc to cancel")

    def _start_transform(self, mode: TransformMode):
        self._clear_hover()
        self.transform_mode = mode
        self.transform_started.emit()
        g = self.cursor().pos()
        self._last_global = QPointF(g.x(), g.y())
        self._just_wrapped = False
        self._transform_delta = QPointF(0, 0)
        self._start_mouse = self.viewport_to_canvas(
            self.mapFromGlobal(g)
        )
        selected = SelectionState.selected()
        self._start_obj_states = {}
        self._avg_pivot = QPointF(0, 0)
        if selected:
            for obj in selected:
                world = self._world_position(obj)
                self._start_obj_states[obj.id] = (
                    world,
                    obj.transform.rotation,
                    (obj.transform.scale_x, obj.transform.scale_y),
                )
                self._avg_pivot += world
            self._avg_pivot /= len(selected)
        self._start_pos = self._avg_pivot
        self._start_rotation = 0.0
        self._start_scale = (1.0, 1.0)
        self._constraint_axis = None
        self.setCursor(Qt.CrossCursor)

        # Grab the mouse for the duration of the transform so this widget
        # keeps receiving mouseMoveEvents even if a fast flick sends the
        # cursor clean off the widget (or off-screen) between native events.
        # Without this, a large single delta can jump past the wrap margin
        # entirely - the widget simply stops getting move events until the
        # cursor wanders back over it, so `_check_cursor_wrap` never runs and
        # the "wrap" is missed. Every code path that ends a transform must
        # release this grab (see _confirm_transform, _cancel_transform, and
        # focusOutEvent as a safety net).
        self.grabMouse()

        self._end_live_drag()
        if mode == TransformMode.MOVE and not self._any_masks():
            # Axis-aligned + un-scaled objects can be redrawn as a cached
            # sprite during the drag (blit-only per move); anything rotated or
            # scaled falls back to the full-snapshot reraster each move.
            self._live_ids = {
                obj.id
                for obj in selected
                if (
                    not obj.is_mask
                    and obj.transform.rotation == 0
                    and obj.transform.scale_x == 1.0
                    and obj.transform.scale_y == 1.0
                )
            }
        # Whatever path we take, the snapshot must be rebuilt on the first
        # paint after a transform starts/ends: live sprites must be baked out
        # of (or back into) it.
        self._cache_key = None

        names = {
            TransformMode.MOVE: "Move",
            TransformMode.ROTATE: "Rotate",
            TransformMode.SCALE: "Scale",
        }
        count_label = f" ({len(selected)} objs)" if len(selected) > 1 else ""
        self.status_message.emit(
            f"{names[mode]}{count_label}: move mouse, click to confirm, Esc to cancel, X/Y to constrain"
        )

    def _update_transform(self):
        selected = SelectionState.selected()
        if not selected:
            return

        dx = self._transform_delta.x()
        dy = self._transform_delta.y()
        avg_pivot = self._avg_pivot

        if self.transform_mode == TransformMode.MOVE:
            if self._constraint_axis == "x":
                for obj in selected:
                    state = self._start_obj_states.get(obj.id)
                    if state:
                        world = QPointF(state[0].x() + dx, state[0].y())
                        self._set_local_position(obj, world)
            elif self._constraint_axis == "y":
                for obj in selected:
                    state = self._start_obj_states.get(obj.id)
                    if state:
                        world = QPointF(state[0].x(), state[0].y() + dy)
                        self._set_local_position(obj, world)
            else:
                for obj in selected:
                    state = self._start_obj_states.get(obj.id)
                    if state:
                        world = QPointF(state[0].x() + dx, state[0].y() + dy)
                        self._set_local_position(obj, world)

        elif self.transform_mode == TransformMode.ROTATE:
            cursor_canvas = QPointF(
                self._start_mouse.x() + dx,
                self._start_mouse.y() + dy,
            )
            current_angle = math.atan2(
                cursor_canvas.y() - avg_pivot.y(),
                cursor_canvas.x() - avg_pivot.x(),
            )
            initial_angle = math.atan2(
                self._start_mouse.y() - avg_pivot.y(),
                self._start_mouse.x() - avg_pivot.x(),
            )
            delta = math.degrees(current_angle - initial_angle)
            for obj in selected:
                state = self._start_obj_states.get(obj.id)
                if state:
                    obj_pos = state[0]
                    rel_x = obj_pos.x() - avg_pivot.x()
                    rel_y = obj_pos.y() - avg_pivot.y()
                    new_angle_rad = math.atan2(rel_y, rel_x) + math.radians(delta)
                    dist = math.hypot(rel_x, rel_y)
                    self._set_local_position(
                        obj,
                        QPointF(
                            avg_pivot.x() + dist * math.cos(new_angle_rad),
                            avg_pivot.y() + dist * math.sin(new_angle_rad),
                        ),
                    )
                    obj.transform.rotation = state[1] + delta

        elif self.transform_mode == TransformMode.SCALE:
            cursor_canvas = QPointF(
                self._start_mouse.x() + dx,
                self._start_mouse.y() + dy,
            )
            rel_start = QPointF(
                self._start_mouse.x() - avg_pivot.x(),
                self._start_mouse.y() - avg_pivot.y(),
            )
            rel_cur = QPointF(
                cursor_canvas.x() - avg_pivot.x(),
                cursor_canvas.y() - avg_pivot.y(),
            )
            if self._constraint_axis == "x":
                ratio = rel_cur.x() / rel_start.x() if abs(rel_start.x()) > 1 else 1.0
            elif self._constraint_axis == "y":
                ratio = rel_cur.y() / rel_start.y() if abs(rel_start.y()) > 1 else 1.0
            else:
                sd = rel_start.x() ** 2 + rel_start.y() ** 2
                ratio = (
                    (rel_start.x() * rel_cur.x() + rel_start.y() * rel_cur.y()) / sd
                    if sd > 1
                    else 1.0
                )
            ratio = math.copysign(max(abs(ratio), 0.01), ratio)
            for obj in selected:
                state = self._start_obj_states.get(obj.id)
                if state:
                    obj_pos = state[0]
                    rel_x = obj_pos.x() - avg_pivot.x()
                    rel_y = obj_pos.y() - avg_pivot.y()
                    if self._constraint_axis == "x":
                        obj.transform.scale_x = state[2][0] * ratio
                        obj.transform.scale_y = state[2][1]
                    elif self._constraint_axis == "y":
                        obj.transform.scale_x = state[2][0]
                        obj.transform.scale_y = state[2][1] * ratio
                    else:
                        obj.transform.scale_x = state[2][0] * ratio
                        obj.transform.scale_y = state[2][1] * ratio
                    self._set_local_position(
                        obj,
                        QPointF(
                            avg_pivot.x() + rel_x * ratio,
                            avg_pivot.y() + rel_y * ratio,
                        ),
                    )

        self.update()

    def _confirm_transform(self):
        changes: dict[str | None, dict[str, tuple]] = {}
        for obj in SelectionState.selected():
            state = self._start_obj_states.get(obj.id)
            if state is None:
                continue
            obj_attrs: dict[str, tuple] = {}
            if state[0].x() != obj.transform.x or state[0].y() != obj.transform.y:
                obj_attrs["transform.x"] = (state[0].x(), obj.transform.x)
                obj_attrs["transform.y"] = (state[0].y(), obj.transform.y)
            if state[1] != obj.transform.rotation:
                obj_attrs["transform.rotation"] = (state[1], obj.transform.rotation)
            if state[2][0] != obj.transform.scale_x or state[2][1] != obj.transform.scale_y:
                obj_attrs["transform.scale_x"] = (state[2][0], obj.transform.scale_x)
                obj_attrs["transform.scale_y"] = (state[2][1], obj.transform.scale_y)
            if obj_attrs:
                changes[obj.id] = obj_attrs
        if changes:
            self.history.push(PropertyCommand(changes))
        self.transform_mode = TransformMode.NONE
        self._constraint_axis = None
        self._end_live_drag()
        self.releaseMouse()
        self.setCursor(Qt.ArrowCursor)
        self.setFocus()
        self.status_message.emit("Ready")
        self.transform_ended.emit()
        self.update()

    def _cancel_transform(self):
        selected = SelectionState.selected()
        for obj in selected:
            state = self._start_obj_states.get(obj.id)
            if state:
                obj.transform.x = state[0].x()
                obj.transform.y = state[0].y()
                obj.transform.rotation = state[1]
                obj.transform.scale_x = state[2][0]
                obj.transform.scale_y = state[2][1]
        self.transform_mode = TransformMode.NONE
        self._constraint_axis = None
        self._end_live_drag()
        self.releaseMouse()
        self.setCursor(Qt.ArrowCursor)
        self.setFocus()
        self.status_message.emit("Ready")
        self.transform_ended.emit()
        self.update()

    def _insert_keyframe(self):
        selected = SelectionState.selected()
        if not selected:
            return
        frame = self.scene.current_frame
        changes = []
        for obj in selected:
            # Capture pre-existing keyframes at this frame *before*
            # set_keyframe_at_current_frame overwrites them, so undoing a
            # "replace" can restore the replaced keyframes.
            victims = {}
            for channel, kfs in obj.keyframes.items():
                for kf in kfs:
                    if kf.frame == frame:
                        victims[channel] = kf
                        break
            set_keyframe_at_current_frame(obj, frame)
            for channel, kfs in obj.keyframes.items():
                for kf in kfs:
                    if kf.frame == frame:
                        if channel in victims:
                            changes.append(
                                (obj, channel, "replace", (victims[channel], kf))
                            )
                        else:
                            changes.append((obj, channel, "insert", kf))
                        break
        if not changes:
            return
        self.history.push(KeyframeCommand(changes))
        count_label = f" ({len(selected)} objs)" if len(selected) > 1 else ""
        self.status_message.emit(
            f"Keyframe inserted at frame {self.scene.current_frame}{count_label}"
        )
        self.keyframe_created.emit()
        self.update()

    def _toggle_mask(self):
        obj = self.selected_object
        if not obj:
            return
        old = obj.is_mask
        obj.is_mask = not old
        self.history.push(
            PropertyCommand({obj.id: {"is_mask": (old, obj.is_mask)}})
        )
        if obj.is_mask:
            state = f"ON ({obj.mask_mode})"
        else:
            state = "OFF"
        self.status_message.emit(f"Mask {state} on {obj.name}")
        self.update()

    def _check_cursor_wrap(self):
        if self.transform_mode == TransformMode.NONE:
            return
        from PySide6.QtGui import QCursor

        local = self.mapFromGlobal(self.cursor().pos())
        # Widened from 10px: with grabMouse() held during the whole transform
        # we now reliably get every move event even when the cursor lands
        # off-widget, but a slightly bigger margin still gives a large single
        # delta (very fast flick / high-polling-rate mouse) more room to
        # register as "reached the edge" rather than skating past it.
        margin = 20
        new_local = QPoint(local)
        did_wrap = False

        if local.x() <= margin:
            new_local.setX(self.width() - margin)
            did_wrap = True
        elif local.x() >= self.width() - margin:
            new_local.setX(margin)
            did_wrap = True

        if local.y() <= margin:
            new_local.setY(self.height() - margin)
            did_wrap = True
        elif local.y() >= self.height() - margin:
            new_local.setY(margin)
            did_wrap = True

        if did_wrap:
            QCursor.setPos(self.mapToGlobal(new_local))
            g = QCursor.pos()
            self._last_global = QPointF(g.x(), g.y())
            self._just_wrapped = True
