from __future__ import annotations

import time, math

from PySide6.QtGui import QColor, QImage, QTransform, QBrush, QPainter
from PySide6.QtCore import Qt


def _shared_clock() -> float:
    """A single monotonic clock (seconds) shared by every stripe shader.

    All shaders read this same value (multiplied by their own ``speed``) as
    their scroll phase, so any stripe pattern that appears - a new selection,
    a hover, a mask highlight, a drag preview - is always phase-aligned with
    every other one instead of each widget keeping its own counter and
    drifting out of sync.
    """
    return time.monotonic()



class StripeShader:
    """A reusable, seamless 45-degree scrolling stripe pattern.

    The pattern lives in a small seamless tile whose size equals the stripe
    period. Because it is filled as a repeated texture (rather than drawn as
    individual lines), it never pops regardless of how small or oddly shaped
    the clipped region is.

    The tile is cached by (color, thickness, gap), so it is built exactly
    once and reused everywhere.

    Class-level defaults: change them once and every instance (every dock,
    every script) is affected.
    """

    THICKNESS = 12
    GAP = 12
    PAD = 12.0
    SPEED = 30.0

    _TILE_CACHE: dict[tuple[int, int, int, int, int, int], QBrush] = {}

    def __init__(self, color: QColor | str = QColor(190, 190, 190),
                 thickness: int | None = None,
                 gap: int | None = None,
                 pad: float | None = None,
                 speed: float | None = None):
        """Create a stripe shader. All args default to the class-level values
        (``THICKNESS``, ``GAP``, ``PAD``, ``SPEED``). Override once on the
        class to change every instance at once, or pass per-instance here.
        """
        self.thickness = max(1, int(thickness)) if thickness is not None else self.THICKNESS
        self.gap = max(1, int(gap)) if gap is not None else self.GAP
        self.color = QColor(color)
        self.pad = float(pad) if pad is not None else self.PAD
        self.speed = float(speed) if speed is not None else self.SPEED

    def _get_tile(self) -> QBrush:
        period = int(self.thickness + self.gap)
        r, g, b = self.color.red(), self.color.green(), self.color.blue()
        key = (period, self.thickness, r, g, b, int(self.color.alpha()))
        brush = self._TILE_CACHE.get(key)
        if brush is not None:
            return brush

        thickness = self.thickness
        img = QImage(period, period, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(0, 0, 0, 0))
        for y in range(period):
            for x in range(period):
                if (x + y) % period < thickness:
                    img.setPixelColor(x, y, self.color)
        brush = QBrush(img)
        self._TILE_CACHE[key] = brush
        return brush

    def brush(self, phase: float, zoom: float = 1.0) -> QBrush:
        inv_zoom = 1.0 / zoom if zoom > 0 else 1.0
        brush = QBrush(self._get_tile())

        period = float(self.thickness + self.gap)

        # 1. Continuous sub-pixel wrapping inside [0, period)
        wrapped_phase = phase % period

        # 2. Continuous sub-pixel translation (NO math.floor or int casting)
        bt = QTransform(
            inv_zoom, 0, 0, inv_zoom,
            -wrapped_phase * inv_zoom,  # Smooth float offset
            0,
        )
        brush.setTransform(bt)
        return brush

    def paint(self, painter: QPainter, clip_path, phase: float | None = None, zoom: float = 1.0) -> None:
        if phase is None:
            period = float(self.thickness + self.gap)
            phase = (_shared_clock() * self.speed) % period

        inv_zoom = 1.0 / zoom if zoom > 0 else 1.0
        brush = self.brush(phase, zoom)

        painter.save()
        painter.setClipPath(clip_path)

        # Enable smooth interpolation for sub-pixel texture offsets
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.Antialiasing, True)

        pad = self.pad * inv_zoom
        rect = clip_path.boundingRect().adjusted(-pad, -pad, pad, pad)
        painter.setPen(Qt.NoPen)
        painter.setBrush(brush)
        painter.drawRect(rect)
        painter.restore()
