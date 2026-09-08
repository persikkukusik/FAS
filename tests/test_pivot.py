"""Tests for the pivot behaviour.

The pivot point IS the object's origin: one dot per object. Dragging it
RELOCATES the reference frame while the object's contents (its own shape and
every descendant) STAY in world space - each piece re-bases its local offset
against the new origin so nothing moves on screen. Every selected object
keeps its own individual pivot (they are never unified into a shared point);
ROTATE/SCALE still spin around the average of the selected objects' pivots.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from core.model import Scene
from core.selection import SelectionState
from ui.stage import TransformMode

from tests.test_undo_transform import container, rect


def _local_for_canvas(stage, p: QPointF) -> QPointF:
    """Inverse of ``viewport_to_canvas`` so tests can build mouse events at a
    known canvas position."""
    zoom = stage.zoom
    cw = stage.CANVAS_SIZE * zoom
    ch = stage.CANVAS_SIZE * zoom
    cx = (stage.width() - cw) / 2 + stage._pan.x()
    cy = (stage.height() - ch) / 2 + stage._pan.y()
    return QPointF(cx + p.x() * zoom, cy + p.y() * zoom)


def _setup(stage):
    obj = rect("R", x=150, y=120)
    stage.scene.objects = [obj]
    SelectionState.set_selected([obj])
    stage.zoom = 1.0
    stage._pan = QPointF(0, 0)
    return obj


class TestPivotDrag:
    def test_dragging_pivot_relocates_origin_keeps_content(self, stage):
        obj = _setup(stage)

        stage._pivot_drag_obj = obj
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(40, 30)
        stage._update_transform()

        # The origin (the dot being dragged) moves ...
        assert (obj.transform.x, obj.transform.y) == (190, 150)
        assert stage._world_position(obj) == QPointF(190, 150)
        # ... but the object's own shape re-bases against the new origin so
        # it stays in world space (unrotated: offset = -delta).
        assert (obj.transform.content_x, obj.transform.content_y) == (-40, -30)
        assert stage._get_local_shape(obj).boundingRect().center() == QPointF(-40, -30)

    def test_dragging_pivot_keeps_children_in_world_space(self, stage):
        """Moving the origin leaves children visually untouched: their local
        offsets adapt to the new origin (they do NOT follow it)."""
        child = rect("Child", x=10, y=20)
        parent = container("Parent", children=[child], x=150, y=120)
        stage.scene.objects = [parent]
        SelectionState.set_selected([parent])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        child_world_before = stage._world_position(child)
        child_local_before = (child.transform.x, child.transform.y)
        stage._pivot_drag_obj = parent
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(40, 0)
        stage._update_transform()

        assert (parent.transform.x, parent.transform.y) == (190, 120)
        # Child compensated by -delta in parent's local axes.
        assert (child.transform.x, child.transform.y) == (
            child_local_before[0] - 40, child_local_before[1]
        )
        assert stage._world_position(child) == child_world_before

    def test_dragging_pivot_compensates_rotated_children(self, stage):
        """Under a rotated parent the compensation uses the inverse of the
        parent's rotate/scale, so children still stay EXACTLY in world space."""
        child = rect("Child", x=10, y=0)
        parent = container("Parent", children=[child], x=150, y=120, rotation=90)
        stage.scene.objects = [parent]
        SelectionState.set_selected([parent])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        child_world_before = stage._world_position(child)
        stage._pivot_drag_obj = parent
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(0, 40)
        stage._update_transform()

        origin = stage._world_position(parent)
        assert (origin.x(), origin.y()) == (150, 160)
        assert stage._world_position(child) == child_world_before

    def test_pivot_drag_is_undoable(self, stage):
        obj = _setup(stage)

        stage._pivot_drag_obj = obj
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(40, 30)
        stage._update_transform()
        stage._confirm_transform()

        stage.history.undo()
        assert (obj.transform.x, obj.transform.y) == (150.0, 120.0)
        assert (obj.transform.content_x, obj.transform.content_y) == (0.0, 0.0)
        stage.history.redo()
        assert (obj.transform.x, obj.transform.y) == (190, 150)
        assert (obj.transform.content_x, obj.transform.content_y) == (-40, -30)

    def test_pivot_drag_with_children_is_undoable(self, stage):
        """Undo restores the parent origin AND every child's compensated
        local offset in one step."""
        child = rect("Child", x=10, y=20)
        parent = container("Parent", children=[child], x=150, y=120)
        stage.scene.objects = [parent]
        SelectionState.set_selected([parent])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        stage._pivot_drag_obj = parent
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(40, 0)
        stage._update_transform()
        stage._confirm_transform()

        assert (child.transform.x, child.transform.y) == (-30, 20)
        stage.history.undo()
        assert (parent.transform.x, parent.transform.y) == (150.0, 120.0)
        assert (child.transform.x, child.transform.y) == (10, 20)
        stage.history.redo()
        assert (parent.transform.x, parent.transform.y) == (190.0, 120.0)
        assert (child.transform.x, child.transform.y) == (-30, 20)

    def test_cancel_restores_origin_and_content(self, stage):
        obj = _setup(stage)

        stage._pivot_drag_obj = obj
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(40, 30)
        stage._update_transform()
        stage._cancel_transform()

        assert (obj.transform.x, obj.transform.y) == (150.0, 120.0)
        assert (obj.transform.content_x, obj.transform.content_y) == (0.0, 0.0)

    def test_pivot_does_not_compound_across_frames(self, stage):
        """The delta is TOTAL from drag start, so a second move must land on
        start + total, not keep accumulating the already-moved position."""
        obj = _setup(stage)

        stage._pivot_drag_obj = obj
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(10, 0)
        stage._update_transform()
        assert (obj.transform.x, obj.transform.y) == (160, 120)
        assert (obj.transform.content_x, obj.transform.content_y) == (-10, 0)

        stage._transform_delta = QPointF(20, 0)
        stage._update_transform()
        assert (obj.transform.x, obj.transform.y) == (170, 120)
        assert (obj.transform.content_x, obj.transform.content_y) == (-20, 0)

    def test_multi_selection_moves_only_grabbed_object(self, stage):
        """Grabbing one object's dot moves only that object's origin/pivot.
        The others keep their own individual pivots - no unification."""
        a = rect("A", x=150, y=120)
        b = rect("B", x=300, y=150)
        stage.scene.objects = [a, b]
        SelectionState.set_selected([a, b])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        assert stage._hit_test_pivot(stage._world_position(a)) is a

        stage._pivot_drag_obj = a
        stage._start_transform(TransformMode.PIVOT)
        stage._transform_delta = QPointF(20, 30)
        stage._update_transform()

        assert (a.transform.x, a.transform.y) == (170, 150)
        assert (a.transform.content_x, a.transform.content_y) == (-20, -30)
        assert (b.transform.x, b.transform.y) == (300, 150)
        assert (b.transform.content_x, b.transform.content_y) == (0, 0)


class TestPivotHitTest:
    def test_hit_test_pivot_returns_grabbed_object(self, stage):
        obj = _setup(stage)
        assert stage._hit_test_pivot(QPointF(151, 121)) is obj

    def test_hit_test_miss_returns_none(self, stage):
        _setup(stage)
        assert stage._hit_test_pivot(QPointF(250, 250)) is None

    def test_no_pivot_hit_without_selection(self, stage):
        assert stage._hit_test_pivot(QPointF(10, 10)) is None

    def test_overlapping_dots_pick_closest(self, stage):
        a = rect("A", x=150, y=120)
        b = rect("B", x=152, y=120)
        stage.scene.objects = [a, b]
        SelectionState.set_selected([a, b])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        assert stage._hit_test_pivot(QPointF(151.5, 120)) is b
        assert stage._hit_test_pivot(QPointF(150.2, 120)) is a

    def test_unselected_object_origin_is_not_grabbable(self, stage):
        """Only SELECTED objects expose their origin dot. An unselected
        object's origin must not grab the pivot (or swallow hover)."""
        a = rect("A", x=150, y=120)
        b = rect("B", x=300, y=150)
        stage.scene.objects = [a, b]
        SelectionState.set_selected([b])

        assert stage._hit_test_pivot(QPointF(150, 120)) is None
        assert stage._hit_test_pivot(QPointF(300, 150)) is b

    def test_hover_over_pivot_highlights_nothing_beneath(self, stage):
        """Hovering a selected object's dot must NOT light up whatever object
        is underneath it - not even an object drawn ON TOP of the dot's owner
        (the dot's hitbox outranks every shape hitbox for hover too)."""
        a = rect("A", x=150, y=120)
        b = rect("B", x=150, y=120, w=140, h=100, color="#ffffff")
        stage.scene.objects = [a, b]
        SelectionState.set_selected([a])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        assert stage.hit_test(QPointF(150, 120)) is b  # topmost body
        stage._update_hover(QPointF(150, 120))
        assert stage.hovered_object is None


class TestContentRendering:
    """The content offset shifts an object's geometry away from its origin,
    and every geometry consumer (hit-test, unions, serialization) honours it."""

    def test_local_shape_centered_on_content_offset(self, stage):
        obj = _setup(stage)
        obj.transform.content_x = -40
        obj.transform.content_y = -30
        assert stage._get_local_shape(obj).boundingRect().center() == QPointF(-40, -30)

    def test_hit_test_respects_content_offset(self, stage):
        obj = _setup(stage)
        obj.transform.content_x = 50
        obj.transform.content_y = 0
        # The 100x80 rect's center is now (50, 0), spanning x in [0, 100].
        assert stage._local_point_in_object(QPointF(51, 0), obj)
        assert not stage._local_point_in_object(QPointF(-60, 0), obj)
        assert stage._local_point_in_object(QPointF(60, 30), obj)

    def test_content_offset_round_trips_save(self):
        from core.save import deserialize_scene, serialize_scene
        obj = rect("R", x=150, y=120)
        obj.transform.content_x = 12.5
        obj.transform.content_y = -7.25
        scene = Scene()
        scene.objects = [obj]
        reloaded = deserialize_scene(serialize_scene(scene))
        out = reloaded.objects[0]
        assert (out.transform.content_x, out.transform.content_y) == (12.5, -7.25)


class TestRotationScaleUseAverageOfPivots:
    def test_rotation_pivot_is_average_of_selected_origins(self, stage):
        a = rect("A", x=100, y=100)
        b = rect("B", x=300, y=100)
        stage.scene.objects = [a, b]
        SelectionState.set_selected([a, b])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        stage._start_transform(TransformMode.ROTATE)
        assert stage._avg_pivot == QPointF(200.0, 100.0)

        # Rotate 45 degrees about the average: A orbits from (100,100) to
        # around (129.3, 29.3).
        stage._start_mouse = QPointF(250, 100)
        stage._transform_delta = QPointF(0, 50)
        stage._update_transform()

        world = stage._world_position(a)
        expected = QPointF(
            200 + (-100 * math.cos(math.radians(45))),
            100 + (-100 * math.sin(math.radians(45))),
        )
        assert abs(world.x() - expected.x()) < 1e-6
        assert abs(world.y() - expected.y()) < 1e-6


class TestPivotHoldAndRelease:
    """Dragging the pivot is a press-and-hold gesture: LMB press on an
    object's dot enters PIVOT mode, and LMB release applies it - no second
    click."""

    def test_press_starts_release_applies(self, stage):
        obj = _setup(stage)
        local = _local_for_canvas(stage, stage._world_position(obj))

        press = QMouseEvent(
            QEvent.MouseButtonPress, local, local,
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        stage.mousePressEvent(press)
        assert stage.transform_mode == TransformMode.PIVOT

        # Drag while holding: the object's origin follows.
        stage._transform_delta = QPointF(25, -10)
        stage._update_transform()
        assert (obj.transform.x, obj.transform.y) == (175, 110)
        assert (obj.transform.content_x, obj.transform.content_y) == (-25, 10)
        assert stage.transform_mode == TransformMode.PIVOT

        # Releasing LMB commits - mode exits with no second click.
        release = QMouseEvent(
            QEvent.MouseButtonRelease, local, local,
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        stage.mouseReleaseEvent(release)
        assert stage.transform_mode == TransformMode.NONE
        assert (obj.transform.x, obj.transform.y) == (175, 110)

        # And the commit is undoable.
        stage.history.undo()
        assert (obj.transform.x, obj.transform.y) == (150.0, 120.0)
        assert (obj.transform.content_x, obj.transform.content_y) == (0.0, 0.0)

    def test_plain_click_on_pivot_is_a_noop(self, stage):
        """Press+release with no motion must not move the object and must not
        leave the widget stuck in PIVOT mode."""
        obj = _setup(stage)
        local = _local_for_canvas(stage, stage._world_position(obj))

        press = QMouseEvent(
            QEvent.MouseButtonPress, local, local,
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        stage.mousePressEvent(press)
        release = QMouseEvent(
            QEvent.MouseButtonRelease, local, local,
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        stage.mouseReleaseEvent(release)

        assert stage.transform_mode == TransformMode.NONE
        assert (obj.transform.x, obj.transform.y) == (150.0, 120.0)

    def test_press_release_moves_only_grabbed_object(self, stage):
        a = rect("A", x=150, y=120)
        b = rect("B", x=300, y=150)
        stage.scene.objects = [a, b]
        SelectionState.set_selected([a, b])
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)

        local = _local_for_canvas(stage, stage._world_position(a))
        press = QMouseEvent(
            QEvent.MouseButtonPress, local, local,
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        stage.mousePressEvent(press)
        assert stage.transform_mode == TransformMode.PIVOT

        stage._transform_delta = QPointF(20, 0)
        stage._update_transform()
        assert (a.transform.x, a.transform.y) == (170, 120)
        assert (b.transform.x, b.transform.y) == (300, 150)

        release = QMouseEvent(
            QEvent.MouseButtonRelease, local, local,
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        stage.mouseReleaseEvent(release)
        assert stage.transform_mode == TransformMode.NONE

    def test_move_outside_pivot_still_selects(self, stage):
        """A click that misses every pivot dot must NOT enter PIVOT mode."""
        obj = _setup(stage)
        pivot = stage._world_position(obj)
        local = _local_for_canvas(stage, QPointF(pivot.x() + 60, pivot.y() + 60))

        press = QMouseEvent(
            QEvent.MouseButtonPress, local, local,
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        stage.mousePressEvent(press)
        assert stage.transform_mode == TransformMode.NONE