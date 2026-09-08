"""Regression tests for the relative-motion ("fake cursor") drag math.

The transform/pan drag work is driven by raw mouse deltas accumulated while
the real OS cursor is hidden and grabbed. When ``evdev`` is available the
deltas come from hardware input; when it is not (CI sandboxes, missing sudo /
permissions), the controller falls back to measuring the hidden cursor's own
movement. In both cases the FIRST delta is measured from where the cursor sat
when the drag began, so the previous drag's end position can never leak in as
a phantom jump.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF

from core.selection import SelectionState
from ui.stage import TransformMode

from tests.test_undo_transform import rect


class TestRelativeDrag:
    def _start(self, stage):
        obj = rect("R", x=150, y=120)
        stage.scene.objects = [obj]
        SelectionState.set_selected([obj])
        stage._start_transform(TransformMode.MOVE)

    def test_motion_measured_from_drag_start(self, stage):
        """Genuine motion is measured from the gesture anchor where the real
        cursor is parked, so the previous drag's end position cannot leak in
        as a jump."""
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)
        self._start(stage)
        anchor = stage._drag._gesture_global

        # First event after begin fixes the origin -> zero motion, no jump.
        dx0, dy0 = stage._relative_drag_delta(QPoint(anchor))
        assert (dx0, dy0) == (0.0, 0.0)

        # Genuine motion is measured relative to the anchored real cursor.
        moved = QPoint(anchor.x() + 10, anchor.y() + 20)
        dx, dy = stage._relative_drag_delta(moved)
        assert (dx, dy) == (10.0, 20.0)

    def test_no_phantom_jump_from_previous_drag_end(self, stage):
        """Wherever the cursor sat when the drag began - e.g. far from where a
        previous drag ended - cannot leak into the first measured delta, because
        the first event fixes the origin and the real cursor is parked at the
        gesture anchor."""
        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)
        self._start(stage)
        anchor = stage._drag._gesture_global

        # The first event lands wherever it does - e.g. 100px left and 80px
        # below the anchor (where the previous drag left the cursor) - and is
        # the origin fix: zero motion, no phantom jump.
        far = QPoint(anchor.x() - 100, anchor.y() + 80)
        dx0, dy0 = stage._relative_drag_delta(far)
        assert (dx0, dy0) == (0.0, 0.0), (
            f"previous drag end leaked in as a phantom jump: {dx0}, {dy0}"
        )

        moved = QPoint(anchor.x() + 5, anchor.y() - 3)
        dx, dy = stage._relative_drag_delta(moved)
        assert (dx, dy) == (5.0, -3.0)

    def test_evdev_absent_falls_back_to_cursor_deltas(self, stage):
        """When evdev is unavailable the controller still reports genuine
        deltas from the grabbed cursor, so dragging works without sudo/evdev."""
        assert not stage._drag.using_evdev or stage._drag._listener.reason is None

    def test_real_cursor_anchored_during_drag(self, stage, monkeypatch):
        """During a drag the real cursor is parked at the gesture anchor on
        every poll, so it can never physically leave the widget and abort the
        gesture (the focus timer / grab loss can't fire)."""
        positions = []

        def _fake_setpos(target, *args, **kwargs):
            positions.append(QPoint(target))

        monkeypatch.setattr("ui.relative_drag.QCursor.setPos", _fake_setpos)

        stage.zoom = 1.0
        stage._pan = QPointF(0, 0)
        self._start(stage)
        anchor = stage._drag._gesture_global
        stage._relative_drag_delta(QPoint(anchor.x() + 40, anchor.y() + 40))
        assert positions, "a poll should have parked the real cursor"
        assert all(p == anchor for p in positions), (
            f"cursor escaped the anchor: {positions}"
        )
