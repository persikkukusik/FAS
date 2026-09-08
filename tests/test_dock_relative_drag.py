"""Integration tests: every dock that supports an endless relative drag must
run it through the shared ``RelativeDrag`` controller (hidden + grabbed real
cursor, per-move relative deltas, fake-cursor glyph or "none"), rather than
reimplementing the fragile per-dock absolute-cursor logic.

Stage and timeline use the visible cross glyph; the outliner reorder and the
number-field scrub hide + grab the cursor but draw their own feedback
(ghost row / number + fill bar), so their controller kind is ``"none"``.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt, QEvent
from PySide6.QtGui import QMouseEvent

from core.history import History
from core.model import Scene, SceneObject, Keyframe
from core.selection import KeyframeSelection
from ui.relative_drag import RelativeDrag
from ui.stage import TransformMode, StageWidget
from ui.timeline import TimelineWidget
from ui.outliner import OutlinerWidget
from ui.number_field import NumericField

from tests.test_undo_transform import rect


class TestSharedControllerWiring:
    def test_stage_owns_shared_controller(self, stage):
        assert isinstance(stage._drag, RelativeDrag)
        stage._begin_relative_drag("cross")
        assert stage._drag.active and stage._drag.kind == "cross"
        stage._end_relative_drag()
        assert not stage._drag.active and stage._drag.kind is None

    def test_timeline_move_enters_shared_controller(self, app):
        scene = Scene()
        obj = SceneObject(
            name="A", shape_type="rect", shape_data={"width": 10, "height": 10}
        )
        obj.keyframes["x"] = [Keyframe(frame=10, value=1.0)]
        scene.objects = [obj]
        tl = TimelineWidget(scene, History(scene))
        tl.resize(600, 200)

        sel = KeyframeSelection(obj, 10)
        tl.selected = [sel]
        tl._enter_move([10], [list(sel.iterate())])

        assert tl._transform_mode == "move"
        assert isinstance(tl._drag, RelativeDrag)
        assert tl._drag.active and tl._drag.kind == "cross"
        assert tl._accum_dx == 0.0

        # The first event fixes the measurement origin; genuine motion after
        # that is delivered by the shared controller relative to the anchored
        # real cursor.
        anchor = tl._drag._gesture_global
        assert tl._drag.delta(QPoint(anchor.x() + 99, anchor.y() - 99)) is None
        dx, dy = tl._drag.delta(QPoint(anchor.x() + 12, anchor.y() - 9))
        assert (dx, dy) == (12.0, -9.0)

        tl._cancel_transform()
        assert not tl._drag.active and tl._drag.kind is None
        tl.close()

    def test_outliner_reorder_enters_shared_controller(self, app):
        scene = Scene()
        a = rect("A", x=10, y=10)
        b = rect("B", x=40, y=40)
        scene.objects = [a, b]
        ol = OutlinerWidget(scene, History(scene))
        ol.resize(200, 200)

        ol._start_drag(a, mode="lmb", grab_y=15.0)
        assert isinstance(ol._drag, RelativeDrag)
        assert ol._drag.active and ol._drag.kind == "none"
        assert ol._drag_grab_y == 15.0 and ol._accum_dy == 0.0

        anchor = ol._drag._gesture_global
        assert ol._drag.delta(QPoint(anchor.x() - 99, anchor.y() + 99)) is None
        dx, dy = ol._drag.delta(QPoint(anchor.x() + 7, anchor.y() - 8))
        assert (dx, dy) == (7.0, -8.0)

        ol._cancel_drag()
        assert not ol._drag.active and ol._drag.kind is None
        ol.close()

    def test_number_field_scrub_enters_shared_controller(self, app):
        nf = NumericField(variant="float", step=1.0, scrub_step=1.0)
        nf.resize(120, 22)
        nf._value = 5.0
        nf._pressed = True
        nf._press_global_x = 40.0

        # Cross the 2px scrub threshold with a first move -> begins the
        # controller-owned drag (hidden + grabbed cursor, no glyph).
        begin_ev = QMouseEvent(
            QEvent.MouseMove, QPointF(10, 10), QPointF(43.0, 40.0),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        nf.mouseMoveEvent(begin_ev)
        assert isinstance(nf._drag, RelativeDrag)
        assert nf._scrubbing and nf._drag.active
        assert nf._drag.kind == "none"

        # The first move fixes the measurement origin; the next one drives the
        # value through the controller's anchor-relative delta (the real
        # cursor is parked at the gesture anchor).
        anchor = nf._drag._gesture_global
        settle_ev = QMouseEvent(
            QEvent.MouseMove, QPointF(10, 10),
            QPointF(anchor.x() + 3, anchor.y() - 2),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        nf.mouseMoveEvent(settle_ev)
        assert nf._value == 5.0  # origin-fixing event: zero motion, no fling

        move_ev = QMouseEvent(
            QEvent.MouseMove, QPointF(10, 10),
            QPointF(anchor.x() + 6, anchor.y()),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        nf.mouseMoveEvent(move_ev)
        assert nf._value == 5.0 + 6.0

        release_ev = QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(10, 10),
            QPointF(anchor.x() + 6, anchor.y()),
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        nf.mouseReleaseEvent(release_ev)
        assert not nf._scrubbing and not nf._drag.active
        nf.close()

    def test_number_field_restores_cursor_to_drag_start(self, app, monkeypatch):
        """The scrub hides the real cursor; on release it must come back to
        exactly where it was when the drag began, NOT to the (wrapped) end
        position of the invisible phantom."""
        nf = NumericField(variant="float", step=1.0, scrub_step=1.0)
        nf.resize(120, 22)
        nf._value = 5.0
        nf._pressed = True
        nf._press_global_x = 40.0

        begin_ev = QMouseEvent(
            QEvent.MouseMove, QPointF(10, 10), QPointF(43.0, 40.0),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        nf.mouseMoveEvent(begin_ev)
        gesture_start = QPoint(nf._drag._gesture_global)
        assert nf._drag.active

        anchor = nf._drag._gesture_global
        settle_ev = QMouseEvent(
            QEvent.MouseMove, QPointF(10, 10),
            QPointF(anchor.x() - 2, anchor.y() + 4),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        nf.mouseMoveEvent(settle_ev)
        move_ev = QMouseEvent(
            QEvent.MouseMove, QPointF(10, 10),
            QPointF(anchor.x() - 2 + 6, anchor.y() + 4),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        nf.mouseMoveEvent(move_ev)

        placed = []
        def _fake_setpos(target, *args, **kwargs):
            placed.append(QPoint(target))

        monkeypatch.setattr("ui.relative_drag.QCursor.setPos", _fake_setpos)

        release_ev = QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(10, 10),
            QPointF(anchor.x() + 6, anchor.y()),
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        nf.mouseReleaseEvent(release_ev)

        assert placed, "release should position the cursor somewhere"
        # The final placement is back at the gesture start - not the phantom's
        # end position the default end() would use.
        assert placed[-1] == gesture_start, (
            f"cursor restored to {placed[-1]}, expected drag start {gesture_start}"
        )
        nf.close()

    def test_all_docks_share_the_same_class(self, app, stage):
        from core.model import Scene as S
        from core.history import History as H

        scene = S()
        docks = [
            stage,
            TimelineWidget(scene, H(scene)),
            OutlinerWidget(scene, H(scene)),
            NumericField(),
        ]
        for dock in docks:
            assert isinstance(dock._drag, RelativeDrag)
        for dock in docks[1:]:
            dock.close()
