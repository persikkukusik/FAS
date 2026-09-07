from __future__ import annotations

from PySide6.QtGui import QColor


class Theme:
    """Global UI accent color.

    Change ACCENT to retheme the selection outline, the outliner selection
    effect, and the timeline keyframe diamonds.
    """

    ACCENT = QColor(255, 128, 0)
