"""Regression tests for the live-drag sprite placement.

During a MOVE drag of an axis-aligned, un-scaled object, the object body is
rendered from a cached sprite blitted over the snapshot. The sprite must land
at the object's CURRENT world position, i.e. its local->world->viewport
transform must be composed BEFORE the canvas->widget viewport transform. A
previous version composed them the other way round, so the object's position
was applied in widget-pixel space: the body drifted away from its outline by
an amount that grew with distance from the world origin.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QTransform

from core.model import SceneObject, Transform
from core.selection import SelectionState
from ui.stage import TransformMode
from tests.test_undo_transform import rect, container


def _body_bbox(widget, color):
    """Widget-space bounding box (as (x0, y0, x1, y1)) of pixels matching
    `color`, or None when nothing was painted in that colour."""
    target = QColor(color)
    image = widget.grab().toImage()
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            c = QColor(image.pixel(x, y))
            if (
                abs(c.red() - target.red()) <= 8
                and abs(c.green() - target.green()) <= 8
                and abs(c.blue() - target.blue()) <= 8
            ):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _expect_widget_pos(stage, x, y):
    """Canvas->widget mapping for a world/canvas point, mirroring StageWidget's
    paintEvent math exactly."""
    cw = stage.CANVAS_SIZE * stage.zoom
    ch = stage.CANVAS_SIZE * stage.zoom
    cx = (stage.width() - cw) / 2 + stage._pan.x()
    cy = (stage.height() - ch) / 2 + stage._pan.y()
    return (cx + x * stage.zoom, cy + y * stage.zoom)


BODY = "#4f8fd2"


class TestLiveSpritePlacement:
    def test_top_level_object_follows_drag(self, stage):
        stage.zoom = 1.3
        stage._pan = QPointF(25, -15)
        stage.scene.objects = [rect("R", x=150, y=120, color=BODY)]
        obj = stage.scene.objects[0]

        SelectionState.set_selected([obj])
        stage._start_transform(TransformMode.MOVE)
        assert obj.id in stage._live_ids, "test must exercise the live-sprite path"

        for delta, expected_xy in [((0, 0), (150, 120)), ((-60, -40), (90, 80)), ((50, -60), (200, 60))]:
            stage._transform_delta = QPointF(*delta)
            stage._update_transform()
            bb = _body_bbox(stage, BODY)
            assert bb, "sprite body was not rendered"
            center = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
            exp = _expect_widget_pos(stage, *expected_xy)
            assert abs(center[0] - exp[0]) < 1.5, f"x drift: {center} vs {exp}"
            assert abs(center[1] - exp[1]) < 1.5, f"y drift: {center} vs {exp}"

    def test_child_under_rotated_parent_follows_drag(self, stage):
        stage.zoom = 1.3
        stage._pan = QPointF(25, -15)
        parent = container("Parent", x=150, y=170, rotation=30)
        child = rect("Child", x=60, y=30, color=BODY)
        parent.children = [child]
        stage.scene.objects = [parent]

        SelectionState.set_selected([child])
        stage._start_transform(TransformMode.MOVE)
        assert child.id in stage._live_ids, "test must exercise the live-sprite path"
        start_world = ((child.transform.x, child.transform.y), QTransform().translate(150, 170).rotate(30).map(
            QPointF(child.transform.x, child.transform.y)
        ))
        start_world = start_world[1].x(), start_world[1].y()

        for delta in [(0, 0), (-40, 30), (30, -60)]:
            stage._transform_delta = QPointF(*delta)
            stage._update_transform()
            expected = (start_world[0] + delta[0], start_world[1] + delta[1])
            bb = _body_bbox(stage, BODY)
            assert bb, "sprite body was not rendered"
            center = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
            exp = _expect_widget_pos(stage, expected[0], expected[1])
            assert abs(center[0] - exp[0]) < 2.0, f"x drift: {center} vs {exp} (delta {delta})"
            assert abs(center[1] - exp[1]) < 2.0, f"y drift: {center} vs {exp} (delta {delta})"


TOP = "#1da7e0"  # higher z (painted last)
BOTTOM = "#e0b01d"  # lower z (painted first)


class TestLiveSpriteZSafety:
    def test_buried_object_disables_live_path(self, stage):
        """A lower-z object under an overlapping higher-z sibling must NOT be
        live-dragged as a sprite (it would be blitted on top of its occluder);
        it falls back to the z-correct reraster instead."""
        bottom = rect("Bottom", x=270, y=270, w=100, h=80, color=BOTTOM)
        top = rect("Top", x=260, y=260, w=160, h=160, color=TOP)
        stage.scene.objects = [bottom, top]

        SelectionState.set_selected([bottom])
        stage._start_transform(TransformMode.MOVE)
        assert bottom.id not in stage._live_ids, "buried object must not use the sprite path"

    def test_topmost_object_keeps_live_path(self, stage):
        """The last-painted object is never occludable, so it still gets the
        fast sprite path."""
        bottom = rect("Bottom", x=270, y=270, w=100, h=80, color=BOTTOM)
        top = rect("Top", x=260, y=260, w=160, h=160, color=TOP)
        stage.scene.objects = [bottom, top]

        SelectionState.set_selected([top])
        stage._start_transform(TransformMode.MOVE)
        assert top.id in stage._live_ids, "topmost object should keep the sprite path"

    def test_mixed_selection_disables_live_all_or_nothing(self, stage):
        """A multi-drag mixing a buried object and a topmost one cannot use
        sprites (the sprite would separate them in z), so it rerasters."""
        bottom = rect("Bottom", x=270, y=270, w=100, h=80, color=BOTTOM)
        top = rect("Top", x=260, y=260, w=160, h=160, color=TOP)
        stage.scene.objects = [bottom, top]

        SelectionState.set_selected([bottom, top])
        stage._start_transform(TransformMode.MOVE)
        assert not stage._live_ids, "mixed-depth drag must not use the sprite path"

    def test_buried_object_stays_covered_while_dragging(self, stage):
        """Visually: dragging a covered object must keep it covered by the
        higher-z sibling, and once dragged clear of it the sprite appears."""
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)
        bottom = rect("Bottom", x=270, y=270, w=100, h=80, color=BOTTOM)
        top = rect("Top", x=260, y=260, w=160, h=160, color=TOP)
        stage.scene.objects = [bottom, top]

        SelectionState.set_selected([bottom])
        stage._start_transform(TransformMode.MOVE)
        assert not stage._live_ids

        # Dragged while still fully under the top object: it must stay hidden.
        stage._transform_delta = QPointF(15, 10)
        stage._update_transform()
        covered = _body_bbox(stage, BOTTOM)
        assert covered is None, f"buried object leaked above its occluder: {covered}"

        # Dragged clear of the top object: now visible at its world position.
        stage._transform_delta = QPointF(140, 90)
        stage._update_transform()
        visible = _body_bbox(stage, BOTTOM)
        assert visible, "object should be visible after being dragged clear"
        center = ((visible[0] + visible[2]) / 2, (visible[1] + visible[3]) / 2)
        exp = _expect_widget_pos(stage, 410, 360)
        assert abs(center[0] - exp[0]) < 1.5, f"x drift: {center} vs {exp}"
        assert abs(center[1] - exp[1]) < 1.5, f"y drift: {center} vs {exp}"