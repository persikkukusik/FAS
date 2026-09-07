from __future__ import annotations

from PySide6.QtCore import Qt, QPointF, QRectF, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QImage
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QStackedWidget,
    QLabel,
    QComboBox,
)

from core.commands import PropertyCommand
from core.model import Scene
from core.selection import SelectionState
from core.history import History
from ui.theme import Theme
from ui.number_field import NumericField
from ui.render_settings import get_render_settings

_BG = "#2e2e2e"


# --------------------------------------------------------------------------- #
# Icons (drawn programmatically, cached per device-pixel-ratio)
# --------------------------------------------------------------------------- #

_ICON_CACHE: dict[tuple[str, float], QImage] = {}


def _draw_gear(painter: QPainter, size: float) -> None:
    center = QPointF(size / 2, size / 2)
    tooth = QColor(200, 200, 200)
    for k in range(8):
        painter.save()
        painter.translate(center)
        painter.rotate(k * 45)
        painter.fillRect(QRectF(-2.5, -size / 2 + 1, 5, 4), tooth)
        painter.restore()
    painter.setBrush(tooth)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(center, size * 0.38, size * 0.38)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
    painter.drawEllipse(center, size * 0.23, size * 0.23)


def _draw_move(painter: QPainter, size: float) -> None:
    c = size / 2
    pen = QPen(QColor(200, 200, 200), 2)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(6, c), QPointF(size - 6, c))
    painter.drawLine(QPointF(c, 6), QPointF(c, size - 6))
    painter.drawLine(QPointF(6, c), QPointF(11, c - 4))
    painter.drawLine(QPointF(6, c), QPointF(11, c + 4))
    painter.drawLine(QPointF(size - 6, c), QPointF(size - 11, c - 4))
    painter.drawLine(QPointF(size - 6, c), QPointF(size - 11, c + 4))
    painter.drawLine(QPointF(c, 6), QPointF(c - 4, 11))
    painter.drawLine(QPointF(c, 6), QPointF(c + 4, 11))
    painter.drawLine(QPointF(c, size - 6), QPointF(c - 4, size - 11))
    painter.drawLine(QPointF(c, size - 6), QPointF(c + 4, size - 11))


def _draw_render(painter: QPainter, size: float) -> None:
    """A small frame/clapperboard icon for the Render tab."""
    pen = QPen(QColor(200, 200, 200), 2)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    x0, y0, w, h = 7, 10, 26, 20
    painter.drawRect(QRectF(x0, y0, w, h))
    painter.drawLine(QPointF(x0, y0 + 6), QPointF(x0 + w, y0))
    painter.drawLine(QPointF(x0, y0 + 6), QPointF(x0, y0 + 6))
    pen2 = QPen(QColor(200, 200, 200), 2)
    pen2.setCapStyle(Qt.RoundCap)
    painter.setPen(pen2)
    painter.drawLine(QPointF(17, y0 + 6), QPointF(x0 + w - 3, y0 + 6))


def _tab_icon(name: str, dpr: float) -> QImage:
    key = (name, dpr)
    img = _ICON_CACHE.get(key)
    if img is not None:
        return img
    size = 40
    img = QImage(int(size * dpr), int(size * dpr), QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    img.setDevicePixelRatio(dpr)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(dpr, dpr)
    if name == "gear":
        _draw_gear(painter, size)
    elif name == "move":
        _draw_move(painter, size)
    elif name == "render":
        _draw_render(painter, size)
    painter.end()
    _ICON_CACHE[key] = img
    return img


# --------------------------------------------------------------------------- #
# Left icon tab strip
# --------------------------------------------------------------------------- #

class _TabIconButton(QWidget):
    clicked = Signal(int)

    def __init__(self, index: int, tooltip: str, icon: str):
        super().__init__()
        self._index = index
        self._icon = icon
        self._active = False
        self._hovered = False
        self.setFixedSize(40, 40)
        self.setToolTip(tooltip)
        self.setCursor(Qt.PointingHandCursor)

    def set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._active:
            painter.fillRect(self.rect(), QColor("#414141"))
            painter.fillRect(0, 0, 3, self.height(), Theme.ACCENT)
        elif self._hovered:
            painter.fillRect(self.rect(), QColor("#3b3b3b"))
        else:
            painter.fillRect(self.rect(), QColor("#333333"))
        painter.drawImage(
            QRectF(2, 2, 36, 36),
            _tab_icon(self._icon, self.devicePixelRatioF() or 1.0),
        )
        painter.setPen(QPen(QColor("#292929"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
            event.accept()
            return
        super().mousePressEvent(event)


class _TabStrip(QWidget):
    current_changed = Signal(int)

    def __init__(self, tabs):
        super().__init__()
        self.setFixedWidth(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(2)
        self._buttons: list[_TabIconButton] = []
        self._active = 0
        for index, tooltip, icon in tabs:
            button = _TabIconButton(index, tooltip, icon)
            button.clicked.connect(self._select)
            layout.addWidget(button)
            self._buttons.append(button)
        layout.addStretch(1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#2b2b2b"))
        painter.setPen(QPen(QColor("#262626"), 1))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        painter.end()

    def _sync(self) -> None:
        for button in self._buttons:
            button.set_active(button._index == self._active)

    def _select(self, index: int) -> None:
        if index == self._active:
            return
        self._active = index
        self._sync()
        self.current_changed.emit(index)

    def set_current(self, index: int) -> None:
        if index == self._active:
            return
        self._active = index
        self._sync()
        self.current_changed.emit(index)


# --------------------------------------------------------------------------- #
# Shared row / heading helpers
# --------------------------------------------------------------------------- #

def _field_row(label_text: str, box: QWidget, label_width: int = 96) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 2, 0, 2)
    layout.setSpacing(8)
    label = QLabel(label_text)
    label.setFixedWidth(label_width)
    label.setProperty("class", "propLabel")
    layout.addWidget(label)
    layout.addWidget(box, 1)
    return row


def _header(text: str) -> QLabel:
    header = QLabel(text)
    header.setProperty("class", "propHeader")
    return header


def _make_page(bg_css: str = _BG) -> QWidget:
    page = QWidget()
    page.setStyleSheet(
        f"QWidget {{ background-color: {bg_css}; }}\n"
        f".propLabel {{ color: #c0c0c0; font-size: 11px; }}\n"
        f".propHeader {{ color: #8a8a8a; font-size: 10px; font-weight: bold; }}\n"
    )
    return page


# --------------------------------------------------------------------------- #
# Project page
# --------------------------------------------------------------------------- #

class _ProjectPage(QWidget):
    """Global animation settings: fps plus the playhead's start/end range."""

    def __init__(self, scene: Scene, owner):
        super().__init__()
        self.scene = scene
        self.owner = owner

        page = _make_page()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        layout.addWidget(_header("PROJECT"))
        layout.addSpacing(4)

        self.fps_box = NumericField(
            variant="int", minimum=1, maximum=240, step=1, scrub_step=1
        )
        self.fps_box.setValue(scene.fps)
        self.fps_box.valueChanged.connect(self._on_fps)

        self.start_box = NumericField(
            variant="int", minimum=None, maximum=None, step=1, scrub_step=1
        )
        self.start_box.setValue(scene.start_frame)
        self.start_box.valueChanged.connect(self._on_start)

        self.end_box = NumericField(
            variant="int", minimum=None, maximum=None, step=1, scrub_step=1
        )
        self.end_box.setValue(scene.end_frame)
        self.end_box.valueChanged.connect(self._on_end)
        for field in (self.fps_box, self.start_box, self.end_box):
            field.scrub_started.connect(self.owner._on_scrub_started)
            field.scrub_finished.connect(self.owner._on_scrub_finished)

        layout.addWidget(_field_row("FPS", self.fps_box, label_width=96))
        layout.addWidget(_field_row("Start Frame", self.start_box, label_width=96))
        layout.addWidget(_field_row("End Frame", self.end_box, label_width=96))
        layout.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page)

    def _quiet(self, box: NumericField, value: int) -> None:
        box.blockSignals(True)
        box.setValue(value)
        box.blockSignals(False)

    def _on_fps(self, value: float) -> None:
        self.owner.begin_edit()
        self.scene.fps = max(1, int(value))
        self.owner.object_changed.emit()

    def _on_start(self, value: float) -> None:
        self.owner.begin_edit()
        start = int(value)
        self.scene.start_frame = start
        if self.scene.end_frame < start:
            self.scene.end_frame = start
            self._quiet(self.end_box, start)
        if self.scene.current_frame < start:
            self.scene.current_frame = start
        self.owner.object_changed.emit()

    def _on_end(self, value: float) -> None:
        self.owner.begin_edit()
        end = int(value)
        self.scene.end_frame = end
        if self.scene.start_frame > end:
            self.scene.start_frame = end
            self._quiet(self.start_box, end)
        if self.scene.current_frame > end:
            self.scene.current_frame = end
        self.owner.object_changed.emit()

    def _pull(self) -> None:
        for box, value in (
            (self.fps_box, self.scene.fps),
            (self.start_box, self.scene.start_frame),
            (self.end_box, self.scene.end_frame),
        ):
            if box.hasFocus():
                continue
            self._quiet(box, value)


# --------------------------------------------------------------------------- #
# Object page
# --------------------------------------------------------------------------- #

_TRANSFORM_FIELDS = {
    "pos_x": "x",
    "pos_y": "y",
    "rotation": "rotation",
    "scale_x": "scale_x",
    "scale_y": "scale_y",
}


class _ObjectPage(QWidget):
    """Transform + opacity of the last selected object (grouped selections are
    ignored; only the most recently selected object is shown)."""

    def __init__(self, scene: Scene, owner):
        super().__init__()
        self.scene = scene
        self.owner = owner
        self._obj = None

        page = _make_page()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        layout.addWidget(_header("OBJECT"))

        self._name_label = QLabel("Nothing selected")
        self._name_label.setProperty("class", "propLabel")
        layout.addWidget(self._name_label)
        layout.addSpacing(6)

        self._boxes: dict[str, NumericField] = {}

        def add_spin(key: str, text: str, minimum: float, maximum: float,
                     decimals: int, step: float, scrub: float):
            box = NumericField(
                variant="float",
                minimum=minimum,
                maximum=maximum,
                decimals=decimals,
                step=step,
                scrub_step=scrub,
            )
            box.valueChanged.connect(lambda v, k=key: self._on_field(k, v))
            box.scrub_started.connect(self.owner._on_scrub_started)
            box.scrub_finished.connect(self.owner._on_scrub_finished)
            self._boxes[key] = box
            layout.addWidget(_field_row(text, box, label_width=96))
            return box

        add_spin("pos_x", "Position X", None, None, 1, 1, 1)
        add_spin("pos_y", "Position Y", None, None, 1, 1, 1)
        add_spin("rotation", "Rotation", None, None, 1, 1, 1)
        add_spin("scale_x", "Scale X", None, None, 3, 0.1, 0.01)
        add_spin("scale_y", "Scale Y", None, None, 3, 0.1, 0.01)
        add_spin("opacity", "Opacity", 0.0, 1.0, 2, 0.05, 0.01)

        layout.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page)

        self._set_target(None)

    def _set_target(self, obj) -> None:
        self._obj = obj
        enabled = obj is not None
        self._name_label.setText(obj.name if obj is not None else "Nothing selected")
        for box in self._boxes.values():
            box.setEnabled(enabled)

    def _on_field(self, key: str, value: float) -> None:
        obj = self.owner.current_target()
        if obj is None:
            return
        self.owner.begin_edit()
        if key == "opacity":
            obj.opacity = min(max(float(value), 0.0), 1.0)
        else:
            setattr(
                obj.transform,
                _TRANSFORM_FIELDS[key],
                float(value),
            )
        self.owner.object_changed.emit()

    def _pull(self) -> None:
        self._set_target(self.owner.current_target())
        obj = self._obj
        for key, box in self._boxes.items():
            if box.hasFocus():
                continue
            value = (
                obj.opacity
                if key == "opacity" and obj is not None
                else (
                    getattr(obj.transform, _TRANSFORM_FIELDS[key])
                    if obj is not None
                    else 0.0
                )
            )
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)


# --------------------------------------------------------------------------- #
# Render settings page
# --------------------------------------------------------------------------- #

_IMAGE_FORMAT_NAMES = {
    "png": "PNG",
    "jpg": "JPEG",
    "bmp": "BMP",
    "webp": "WebP",
}

_VIDEO_CODEC_NAMES = {
    "h264": "H.264 (MP4)",
    "h265": "H.265 (MP4)",
    "vp9": "VP9 (WebM)",
    "av1": "AV1 (MP4)",
}


def _dark_combo(items=None) -> QComboBox:
    combo = QComboBox()
    combo.setStyleSheet(
        """
        QComboBox {
            background-color: #3a3a3a;
            color: #d0d0d0;
            border: 1px solid #4a4a4a;
            border-radius: 3px;
            padding: 2px 6px;
            font-size: 11px;
        }
        QComboBox:hover { background-color: #424242; }
        QComboBox::drop-down { border: none; width: 18px; }
        QComboBox QAbstractItemView {
            background-color: #2b2b2b;
            color: #d0d0d0;
            selection-background-color: #3a5a8a;
            border: 1px solid #4a4a4a;
        }
        """
    )
    if items:
        combo.addItems(items)
    return combo


class _RenderPage(QWidget):
    """Export settings: image format/quality and video codec/bitrate/scale."""

    def __init__(self, scene: Scene, owner):
        super().__init__()
        self.scene = scene
        self.owner = owner

        page = _make_page()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        layout.addWidget(_header("RENDER"))
        layout.addSpacing(4)

        layout.addWidget(_header("IMAGE"))
        self.image_format_combo = _dark_combo(list(_IMAGE_FORMAT_NAMES.values()))
        self.image_format_combo.currentIndexChanged.connect(self._on_image_format)
        layout.addWidget(_field_row("Format", self.image_format_combo, label_width=96))

        self.image_quality_box = NumericField(
            variant="int", minimum=0, maximum=100, step=1, scrub_step=1
        )
        self.image_quality_box.valueChanged.connect(self._on_image_quality)
        layout.addWidget(_field_row("Quality", self.image_quality_box, label_width=96))

        layout.addSpacing(8)
        layout.addWidget(_header("VIDEO"))
        self.video_codec_combo = _dark_combo(list(_VIDEO_CODEC_NAMES.values()))
        self.video_codec_combo.currentIndexChanged.connect(self._on_video_codec)
        layout.addWidget(_field_row("Codec", self.video_codec_combo, label_width=96))

        self.video_bitrate_box = NumericField(
            variant="int", minimum=100, maximum=120000, step=500, scrub_step=100
        )
        self.video_bitrate_box.valueChanged.connect(self._on_video_bitrate)
        layout.addWidget(_field_row("Bitrate (kb/s)", self.video_bitrate_box, label_width=96))

        self.video_scale_combo = _dark_combo(["1x (512x512)", "2x (1024x1024)", "4x (2048x2048)"])
        self.video_scale_combo.currentIndexChanged.connect(self._on_video_scale)
        layout.addWidget(_field_row("Scale", self.video_scale_combo, label_width=96))

        layout.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page)

        self._pull()

    def _settings(self):
        return get_render_settings(self.scene)

    def _on_image_format(self, index: int) -> None:
        if index < 0:
            return
        key = list(_IMAGE_FORMAT_NAMES.keys())[index]
        self._settings().image_format = key

    def _on_image_quality(self, value: float) -> None:
        self._settings().image_quality = max(0, min(100, int(value)))

    def _on_video_codec(self, index: int) -> None:
        if index < 0:
            return
        key = list(_VIDEO_CODEC_NAMES.keys())[index]
        self._settings().video_codec = key

    def _on_video_bitrate(self, value: float) -> None:
        self._settings().video_bitrate_kbps = max(100, int(value))

    def _on_video_scale(self, index: int) -> None:
        self._settings().video_scale = [1, 2, 4][max(0, min(index, 2))]

    def _quiet_combo(self, combo: QComboBox, index: int) -> None:
        if combo.currentIndex() != index:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _quiet_box(self, box: NumericField, value) -> None:
        box.blockSignals(True)
        box.setValue(value)
        box.blockSignals(False)

    def _pull(self) -> None:
        s = self._settings()
        self._quiet_combo(
            self.image_format_combo,
            list(_IMAGE_FORMAT_NAMES.keys()).index(s.image_format)
            if s.image_format in _IMAGE_FORMAT_NAMES else 0,
        )
        if not self.image_quality_box.hasFocus():
            self._quiet_box(self.image_quality_box, s.image_quality)
        self._quiet_combo(
            self.video_codec_combo,
            list(_VIDEO_CODEC_NAMES.keys()).index(s.video_codec)
            if s.video_codec in _VIDEO_CODEC_NAMES else 0,
        )
        if not self.video_bitrate_box.hasFocus():
            self._quiet_box(self.video_bitrate_box, s.video_bitrate_kbps)
        self._quiet_combo(self.video_scale_combo, max(0, min(s.video_scale - 1, 2)))


# --------------------------------------------------------------------------- #
# Properties dock
# --------------------------------------------------------------------------- #

class PropertiesWidget(QWidget):
    """A "Properties" dock: a left icon strip switching between a project
    page (fps, start/end frame) and an object page (transform + opacity)."""

    object_changed = Signal()

    REFRESH_MS = 40
    SNAPSHOT_SETTLE_MS = 700

    def __init__(self, scene: Scene, history: History):
        super().__init__()
        self.scene = scene
        self.history = history
        self._target_id: str | None = None
        self._snapshotted = False
        self._scrubbing = False  # True while a numeric field drag is in flight
        self._edit_baseline: dict[str | None, dict[str, tuple]] = {}
        self.setMinimumWidth(220)

        self._build_ui()

        # An edit (typing, stepping, dragging a spinbox) pushes a single
        # history snapshot for the whole burst, not one per keystroke/tick.
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setSingleShot(True)
        self._snapshot_timer.setInterval(self.SNAPSHOT_SETTLE_MS)
        self._snapshot_timer.timeout.connect(self._release_snapshot)

        # Pull model state into the widgets on a timer, so the panel always
        # stays in sync with selection changes, scrub/playback interpolation,
        # stage drags, undo/redo, etc. - without needing to wire every signal.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(self.REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._strip = _TabStrip(
            [
                (0, "Project Settings", "gear"),
                (1, "Object Properties", "move"),
                (2, "Render Settings", "render"),
            ]
        )
        root.addWidget(self._strip)

        self._stack = QStackedWidget()
        self._project = _ProjectPage(self.scene, self)
        self._object = _ObjectPage(self.scene, self)
        self._render = _RenderPage(self.scene, self)
        self._stack.addWidget(self._project)
        self._stack.addWidget(self._object)
        self._stack.addWidget(self._render)
        root.addWidget(self._stack, 1)

        self._strip.current_changed.connect(self._stack.setCurrentIndex)
        self._stack.setCurrentIndex(0)

    # ------------------------------------------------------------------ #
    # Public API used by the pages
    # ------------------------------------------------------------------ #
    def current_target(self):
        """The last selected object; grouped selections are ignored."""
        objects = SelectionState.selected()
        return objects[-1] if objects else None

    def begin_edit(self) -> None:
        """Capture the current values of every editable panel field once per
        burst, so the whole edit (typing / stepping / scrubbing) becomes a
        single undo step."""
        if self._snapshotted:
            return
        self._snapshotted = True
        self._edit_baseline = self._capture_fields()
        self._snapshot_timer.start()

    def _capture_fields(self) -> dict[str | None, dict[str, tuple]]:
        """Capture the pre-edit value of every editable panel property.

        The returned dict maps ``obj_id_or_None`` -> ``{attr: (old, None)}``
        (``new`` filled in at commit time), so a single PropertyCommand can
        describe every change made during the burst."""
        baseline: dict[str | None, dict[str, tuple]] = {}
        # Project page: fps / start / end
        for attr in ("fps", "start_frame", "end_frame"):
            baseline[None] = baseline.get(None, {})
            baseline[None][attr] = (getattr(self.scene, attr), None)
        # Object page: the current target's transform + opacity
        obj = self.current_target()
        if obj is not None:
            tr = obj.transform
            obj_attrs = {
                "transform.x": (tr.x, None),
                "transform.y": (tr.y, None),
                "transform.rotation": (tr.rotation, None),
                "transform.scale_x": (tr.scale_x, None),
                "transform.scale_y": (tr.scale_y, None),
                "opacity": (obj.opacity, None),
            }
            baseline[obj.id] = obj_attrs
        return baseline

    def _commit_edit(self) -> None:
        """Diff the current values against the captured baseline and push a
        single PropertyCommand for the whole burst."""
        if not self._snapshotted:
            return
        changes: dict[str | None, dict[str, tuple]] = {}
        baseline = self._edit_baseline
        for obj_id, attrs in baseline.items():
            target = self.scene if obj_id is None else self.scene.get_object_by_id(obj_id)
            if target is None:
                continue
            diff = {}
            for attr, (old, _) in attrs.items():
                new = self._read_attr(target, attr)
                if old != new:
                    diff[attr] = (old, new)
            if diff:
                changes[obj_id] = diff
        if changes:
            self.history.push(PropertyCommand(changes))

    @staticmethod
    def _read_attr(target, attr: str):
        for part in attr.split("."):
            target = getattr(target, part)
        return target

    def _on_scrub_started(self) -> None:
        # A numeric drag is a single edit: snapshot now and don't let the
        # settle-timer close the burst while the user is still dragging.
        self._scrubbing = True
        self.begin_edit()

    def _on_scrub_finished(self) -> None:
        self._scrubbing = False
        self._commit_edit()
        self._snapshotted = False
        self._edit_baseline = {}
        self._snapshot_timer.stop()

    def _release_snapshot(self) -> None:
        # Only close the burst when nothing is being scrubbed; a long drag
        # would otherwise spill into several undo steps.
        if self._scrubbing:
            return
        self._commit_edit()
        self._snapshotted = False
        self._edit_baseline = {}

    # ------------------------------------------------------------------ #
    # Model -> widgets
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        self._project._pull()
        self._object._pull()
        self._render._pull()
