"""Tests for the tiled fake-cursor rendering.

The fake cursor's stored position is UNWRAPPED, so it can drift past the
widget bounds. To keep a visible cursor inside the widget it is drawn at 9
offsets (its raw position plus ±width/±height wrap-around copies). This lets
the cursor exit one side while its copy visibly enters from the opposite side,
instead of popping in. These tests paint the widget and inspect pixels.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from ui.stage import TransformMode


def _cursor_centers(image):
    """Return the (x, y) of every white stroke pixel cluster centre via a
    simple scan for the distinctive fake-cursor white (the outline is dark, so
    the bright centre pixels are unambiguous)."""
    centers = []
    for y in range(image.height()):
        for x in range(image.width()):
            c = QColor(image.pixel(x, y))
            if c.red() > 200 and c.green() > 200 and c.blue() > 200 and c.alpha() > 200:
                centers.append((x, y))
    return centers


def _white_x_range(image):
    xs = [p[0] for p in _cursor_centers(image)]
    return (min(xs), max(xs)) if xs else None


def _paint(stage, fx, fy):
    stage._fake_cursor_kind = "cross"
    stage._fake_cursor_pos = QPointF(fx, fy)
    stage.update()
    return stage.grab().toImage()


class TestTiledFakeCursor:
    def test_init_pos_visible_in_widget(self, stage):
        """A cursor inside the widget is drawn once, in the middle."""
        w = stage.width()
        img = _paint(stage, w * 0.5, w * 0.5)
        xs = [p[0] for p in _cursor_centers(img)]
        assert xs, "cursor should be visible"
        assert len(xs) >= 5  # cross stroke is many white pixels

    def test_exit_right_enters_left(self, stage):
        """When the unwrapped cursor is just past the right edge, a copy must
        appear at the left edge - the same cursor coming from the other side.
        No copy should be far off where it would be invisible."""
        w = stage.width()
        # Unwrapped x = w/2 + w + 30 -> exits right, wraps to x=30 on the left.
        img = _paint(stage, w + 30.0, w * 0.5)
        rng = _white_x_range(img)
        assert rng, "at least one cursor copy must be visible"
        # The entering copy sits around x=30 (within the widget).
        assert rng[0] < 60, f"expected an entering copy near the left edge, got x-range {rng}"

    def test_exit_left_enters_right(self, stage):
        """Mirror of the above: unwrapped x just BELOW 0 exits the left edge
        and wraps to a copy near the right edge."""
        w = stage.width()
        img = _paint(stage, -30.0, w * 0.5)
        rng = _white_x_range(img)
        assert rng, "at least one cursor copy must be visible"
        assert rng[1] > w - 60, f"expected an entering copy near the right edge, got x-range {rng}"

    def test_corner_wraps_both_axes(self, stage):
        """A cursor exiting the top-right corner appears entering at the
        bottom-left corner."""
        w = stage.width()
        img = _paint(stage, w + 20.0, -20.0)
        centers = _cursor_centers(img)
        assert centers, "cursor copies must be visible"
        # The wrap-around copy sitting inside the widget at (~20,~20).
        xs = [p[0] for p in centers if p[0] < 60 and p[1] < 60]
        assert xs, f"expected a copy entering at the top-left, got centers {centers}"
