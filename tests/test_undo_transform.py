"""Regression tests for undo/redo bugs.

Bug 1: moving a non-symbol object that is a CHILD of a transformed container,
then undoing the move, used to teleport the child to the bottom-right: the
undo command recorded the WORLD position at drag start as if it were the
object's LOCAL position.

Bug 2: undoing a transform/keyframe action on a MULTI-selection used to reset
the selection back to only the first (primary) selected object, because the
history only stored a single object id per entry.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF

from core.model import SceneObject, Transform, Scene
from core.selection import SelectionState
from ui.stage import TransformMode


def rect(name, w=100, h=80, x=0.0, y=0.0, color="#cccccc"):
    return SceneObject(
        name=name,
        shape_type="rect",
        shape_data={"width": w, "height": h},
        transform=Transform(x=x, y=y),
        color=color,
    )


def container(name, children=None, x=0.0, y=0.0, rotation=0.0, scale=(1.0, 1.0)):
    obj = SceneObject(
        name=name,
        shape_type="container",
        transform=Transform(x=x, y=y, rotation=rotation,
                            scale_x=scale[0], scale_y=scale[1]),
    )
    obj.children = list(children or [])
    return obj


class TestUndoChildMove:
    def test_undo_move_child_under_transformed_parent(self, stage):
        """A child whose parent carries a transform must return to its exact
        original world position on undo (not slip toward the parent's origin)."""
        parent = container("Parent", x=100, y=150)
        child = rect("Child", x=10, y=20)
        parent.children = [child]
        stage.scene.objects = [parent]

        world_before = stage._world_position(child)
        local_before = (child.transform.x, child.transform.y)

        SelectionState.set_selected([child])
        stage._start_transform(TransformMode.MOVE)
        stage._transform_delta = QPointF(37, -22)
        stage._update_transform()

        assert child.transform.x == 47 and child.transform.y == -2

        stage._confirm_transform()
        stage.history.undo()

        assert (child.transform.x, child.transform.y) == local_before
        assert stage._world_position(child) == world_before

    def test_undo_move_child_under_rotated_scaled_parent(self, stage):
        """Same as above but the parent is rotated and scaled, so local and
        world space differ in direction and magnitude."""
        parent = container(
            "Parent", x=120, y=90, rotation=33.0, scale=(1.5, 0.8)
        )
        child = rect("Child", x=8, y=14)
        parent.children = [child]
        stage.scene.objects = [parent]

        world_before = stage._world_position(child)

        SelectionState.set_selected([child])
        stage._start_transform(TransformMode.MOVE)
        stage._transform_delta = QPointF(25, -30)
        stage._update_transform()
        stage._confirm_transform()
        stage.history.undo()

        assert stage._world_position(child) == world_before

    def test_undo_move_child_together_with_transformed_parent(self, stage):
        """Selecting BOTH a child and its transformed parent, then moving the
        group, must undo cleanly: the child's own local offset is unchanged by
        the move (it rides along with the parent), so only the parent's
        transform needs restoring."""
        parent = container("Parent", x=100, y=150)
        child = rect("Child", x=10, y=20)
        parent.children = [child]
        stage.scene.objects = [parent]

        parent_world_before = stage._world_position(parent)
        child_world_before = stage._world_position(child)

        SelectionState.set_selected([parent, child])
        stage._start_transform(TransformMode.MOVE)
        stage._transform_delta = QPointF(37, -22)
        stage._update_transform()
        # The child rides along with the parent: its local offset is untouched.
        assert child.transform.x == 10 and child.transform.y == 20
        stage._confirm_transform()
        stage.history.undo()

        assert stage._world_position(parent) == parent_world_before
        assert stage._world_position(child) == child_world_before
        assert (child.transform.x, child.transform.y) == (10, 20)

    def test_cancel_move_child_under_transformed_parent(self, stage):
        """Esc (cancel) must also restore the child to its original position."""
        parent = container("Parent", x=100, y=150)
        child = rect("Child", x=10, y=20)
        parent.children = [child]
        stage.scene.objects = [parent]

        world_before = stage._world_position(child)

        SelectionState.set_selected([child])
        stage._start_transform(TransformMode.MOVE)
        stage._transform_delta = QPointF(60, 70)
        stage._update_transform()
        stage._cancel_transform()

        assert (child.transform.x, child.transform.y) == (10, 20)
        assert stage._world_position(child) == world_before

    def test_undo_rotate_child_under_transformed_parent(self, stage):
        """Rotating a child of a transformed parent and undoing restores its
        exact local transform, not a world-space value."""
        parent = container(
            "Parent", x=120, y=90, rotation=33.0, scale=(1.5, 0.8)
        )
        child = rect("Child", x=8, y=14)
        parent.children = [child]
        stage.scene.objects = [parent]

        local_before = (
            child.transform.x, child.transform.y,
            child.transform.rotation, child.transform.scale_x, child.transform.scale_y,
        )
        world_before = stage._world_position(child)

        SelectionState.set_selected([child])
        stage._start_transform(TransformMode.ROTATE)
        pivot = stage._avg_pivot
        stage._start_mouse = QPointF(pivot.x() + 50, pivot.y())
        stage._transform_delta = QPointF(0, 50)  # 45 degrees around the pivot
        stage._update_transform()
        stage._confirm_transform()
        stage.history.undo()

        now = (
            child.transform.x, child.transform.y,
            child.transform.rotation, child.transform.scale_x, child.transform.scale_y,
        )
        assert now == local_before
        assert stage._world_position(child) == world_before

    def test_undo_scale_child_under_transformed_parent(self, stage):
        """Scaling a child of a transformed parent and undoing restores its
        exact local transform."""
        parent = container(
            "Parent", x=120, y=90, rotation=33.0, scale=(1.5, 0.8)
        )
        child = rect("Child", x=8, y=14, color="#ffffff")
        parent.children = [child]
        stage.scene.objects = [parent]

        local_before = (
            child.transform.x, child.transform.y,
            child.transform.rotation, child.transform.scale_x, child.transform.scale_y,
        )
        world_before = stage._world_position(child)

        SelectionState.set_selected([child])
        stage._start_transform(TransformMode.SCALE)
        pivot = stage._avg_pivot
        stage._start_mouse = QPointF(pivot.x() + 50, pivot.y())
        stage._transform_delta = QPointF(25, 0)  # ratio 1.5
        stage._update_transform()
        stage._confirm_transform()
        stage.history.undo()

        now = (
            child.transform.x, child.transform.y,
            child.transform.rotation, child.transform.scale_x, child.transform.scale_y,
        )
        assert now == local_before
        assert stage._world_position(child) == world_before


class TestUndoMultiSelection:
    def _select(self, *objs):
        SelectionState.set_selected(list(objs))
        self.stage.history.save_selection([o.id for o in objs])

    def _after_undo(self):
        objs = [
            self.stage.scene.get_object_by_id(oid)
            for oid in self.stage.history.selected_ids
        ]
        SelectionState.set_selected([o for o in objs if o is not None])

    def test_undo_transform_restores_full_selection(self, stage):
        a = rect("A", x=0, y=0)
        b = rect("B", x=40, y=0)
        c = rect("C", x=0, y=40)
        stage.scene.objects = [a, b, c]
        self.stage = stage
        self._select(a, b, c)

        stage._start_transform(TransformMode.MOVE)
        stage._transform_delta = QPointF(15, 15)
        stage._update_transform()
        stage._confirm_transform()

        assert stage.history.can_undo()
        stage.history.undo()
        self._after_undo()

        selected = [o.id for o in SelectionState.selected()]
        assert selected == [a.id, b.id, c.id]

    def test_undo_keyframe_restores_full_selection(self, stage):
        a = rect("A", x=0, y=0)
        b = rect("B", x=40, y=0)
        stage.scene.objects = [a, b]
        stage.history.sync_keyframe_selection()
        self.stage = stage
        self._select(a, b)

        stage._insert_keyframe()
        assert stage.history.can_undo()
        stage.history.undo()
        self._after_undo()

        selected = [o.id for o in SelectionState.selected()]
        assert selected == [a.id, b.id]
        # keyframes must actually be gone after undo
        assert not a.has_keyframes()
        assert not b.has_keyframes()

    def test_undo_delete_restores_full_selection(self, stage):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent, QKeySequence

        a = rect("A", x=0, y=0)
        b = rect("B", x=40, y=0)
        c = rect("C", x=0, y=40)
        stage.scene.objects = [a, b, c]
        self.stage = stage
        self._select(a, b, c)

        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key_Delete, Qt.NoModifier
        )
        stage.handle_key_press(event)

        assert a not in stage.scene.objects and b not in stage.scene.objects
        stage.history.undo()
        self._after_undo()

        selected = [o.id for o in SelectionState.selected()]
        assert selected == [a.id, b.id, c.id]
        assert a in stage.scene.objects and b in stage.scene.objects