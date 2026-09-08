from __future__ import annotations

import time, math

from PySide6.QtGui import QColor, QImage, QTransform, QBrush, QPainter
from PySide6.QtCore import Qt


def _shared_clock() -> float:
    return time.monotonic()


class StripeShader:
    """A reusable, seamless 45-degree scrolling sine-wave pattern."""

    THICKNESS = 12
    GAP = 12
    PAD = 12.0
    SPEED = 60.0  # Pixels per second

    _TILE_CACHE: dict[tuple[int, int, int, int, int, int], QBrush] = {}

    def __init__(self, color: QColor | str = QColor(190, 190, 190),
                 thickness: int | None = None,
                 gap: int | None = None,
                 pad: float | None = None,
                 speed: float | None = None):
        self.thickness = max(1, int(thickness)) if thickness is not None else self.THICKNESS
        self.gap = max(1, int(gap)) if gap is not None else self.GAP
        self.color = QColor(color)
        self.pad = float(pad) if pad is not None else self.PAD
        self.speed = float(speed) if speed is not None else self.SPEED

    def _get_tile(self) -> QBrush:
        period = int(self.thickness + self.gap)
        r, g, b, base_alpha = self.color.red(), self.color.green(), self.color.blue(), self.color.alpha()
        key = (period, self.thickness, r, g, b, base_alpha)

        brush = self._TILE_CACHE.get(key)
        if brush is not None:
            return brush

        img = QImage(period, period, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor(0, 0, 0, 0))

        # Two-PI scalar for one complete wavelength across `period`
        freq = (2.0 * math.pi) / period

        for y in range(period):
            for x in range(period):
                # Calculate smooth sine wave value ranging from 0.0 to 1.0 along diagonals
                sine_val = (math.sin((x + y) * freq) + 1.0) / 2.0

                # Scale base alpha by the sine factor
                pixel_alpha = int(base_alpha * sine_val)

                # Multiply color components by alpha for Premultiplied ARGB32
                px_color = QColor.fromRgbF(
                    (r / 255.0) * (pixel_alpha / 255.0),
                    (g / 255.0) * (pixel_alpha / 255.0),
                    (b / 255.0) * (pixel_alpha / 255.0),
                    pixel_alpha / 255.0
                )
                img.setPixelColor(x, y, px_color)

        brush = QBrush(img)
        self._TILE_CACHE[key] = brush
        return brush

    def brush(self, phase: float, zoom: float = 1.0) -> QBrush:
        inv_zoom = 1.0 / zoom if zoom > 0 else 1.0
        brush = QBrush(self._get_tile())

        period = float(self.thickness + self.gap)
        wrapped_phase = phase % period

        bt = QTransform(
            inv_zoom, 0, 0, inv_zoom,
            -wrapped_phase * inv_zoom,
            0,
        )
        brush.setTransform(bt)
        return brush

    def paint(self, painter: QPainter, clip_path, phase: float | None = None, zoom: float = 1.0) -> None:
        if phase is None:
            # Calculate pixel distance traveled: speed (px/s) * elapsed time (s)
            phase = _shared_clock() * self.speed

        inv_zoom = 1.0 / zoom if zoom > 0 else 1.0
        brush = self.brush(phase, zoom)

        painter.save()
        painter.setClipPath(clip_path)

        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.Antialiasing, True)

        pad = self.pad * inv_zoom
        rect = clip_path.boundingRect().adjusted(-pad, -pad, pad, pad)
        painter.setPen(Qt.NoPen)
        painter.setBrush(brush)
        painter.drawRect(rect)
        painter.restore()
