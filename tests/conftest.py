"""Headless test harness for the animation software.

The StageWidget forces ``QT_QPA_PLATFORM=xcb`` at import time, but there is no
X server in CI.  Qt reads the platform env var when ``QApplication`` is *first
constructed*, so we import ``ui.stage`` first (which sets xcb) and then flip the
env var to ``offscreen`` right before creating the app.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def app():
    import ui.stage  # noqa: F401  (forces QT_QPA_PLATFORM=xcb at import)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication

    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    instance.setStyle("Fusion")
    return instance


@pytest.fixture()
def stage(app):
    from core.model import Scene
    from core.history import History
    from ui.stage import StageWidget

    scene = Scene()
    widget = StageWidget(scene, History(scene))
    widget._selection_timer.stop()
    widget._hover_anim_timer.stop()
    widget.resize(700, 700)
    yield widget
    widget.close()