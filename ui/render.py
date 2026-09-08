from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath, QImage, QTransform
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QMainWindow,
    QProgressBar,
    QSlider,
    QFileDialog,
    QMessageBox,
    QApplication,
)

from core.model import Scene
from core.animation import apply_interpolation
from ui.docks import DockManager, DockWidget
from ui.render_settings import RenderSettings, get_render_settings


def render_scene_image(scene: Scene, frame: int | None = None) -> QImage:
    """Rasterise the whole scene at canvas resolution (1 scene unit = 1 px).

    Mirrors StageWidget's cached body render: white canvas, light grid, then
    every visible object honouring masks and group opacity. No selection or
    hover overlays are drawn - this is a pure scene snapshot.

    Interpolation mutates the shared object transforms in place, so after
    rendering the scene is always restored to its current frame.
    """
    restore = None
    if frame is not None:
        restore = scene.current_frame
        if frame != restore:
            apply_interpolation(scene, frame)
    size = StageCanvas.CANVAS
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(50, 50, 50))

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.fillRect(QRectF(0, 0, size, size), QColor(255, 255, 255))

    grid_pen = QPen(QColor(235, 235, 235), 0.5)
    painter.setPen(grid_pen)
    for gx in range(0, size + 1, 50):
        painter.drawLine(gx, 0, gx, size)
    for gy in range(0, size + 1, 50):
        painter.drawLine(0, gy, size, gy)

    base_holes, clip_holes = _compute_mask_paths(scene)
    for obj in scene.objects:
        _draw_object(painter, scene, obj, base_holes, clip_holes)

    painter.setPen(QPen(QColor(120, 120, 120), 1))
    painter.drawRect(QRectF(0.5, 0.5, size - 1, size - 1))
    painter.end()

    if restore is not None and restore != frame:
        apply_interpolation(scene, restore)
    return img


class StageCanvas:
    CANVAS = 512


# --------------------------------------------------------------------------- #
# Offscreen scene rasterising (a standalone copy of StageWidget's pipeline so
# exports don't depend on a live viewport widget).
# --------------------------------------------------------------------------- #

def _local_path(obj) -> QPainterPath:
    path = QPainterPath()
    points = obj.shape_data.get("points", [])
    if len(points) >= 2:
        path.moveTo(points[0][0], points[0][1])
        for p in points[1:]:
            path.lineTo(p[0], p[1])
        path.closeSubpath()
    return path


def _flat_brush(obj):
    from PySide6.QtGui import QBrush
    if not obj.color or obj.color.lower() == "none":
        return Qt.NoBrush
    return QBrush(QColor(obj.color))


def _get_local_shape(obj, include_children=True) -> QPainterPath:
    local = QPainterPath()
    cx = obj.transform.content_x
    cy = obj.transform.content_y
    if obj.shape_type == "rect":
        w = obj.shape_data.get("width", 100)
        h = obj.shape_data.get("height", 80)
        local.addRect(-w / 2 + cx, -h / 2 + cy, w, h)
    elif obj.shape_type == "circle":
        r = obj.shape_data.get("radius", 50)
        local.addEllipse(QPointF(cx, cy), r, r)
    elif obj.shape_type == "polygon":
        if obj.shape_data.get("points"):
            t = QTransform()
            t.translate(cx, cy)
            local.addPath(t.map(_local_path(obj)))
    if include_children:
        for child in obj.children:
            t = QTransform()
            t.translate(child.transform.x, child.transform.y)
            t.rotate(child.transform.rotation)
            t.scale(child.transform.scale_x, child.transform.scale_y)
            local.addPath(t.map(_get_local_shape(child)))
    return local


def _ancestors(scene: Scene, obj):
    chain = []
    cur = scene.find_parent(obj)
    while cur is not None:
        chain.append(cur)
        cur = scene.find_parent(cur)
    chain.reverse()
    return chain


def _ancestor_world_transform(scene: Scene, obj) -> QTransform:
    t = QTransform()
    for o in _ancestors(scene, obj):
        tr = o.transform
        t.translate(tr.x, tr.y)
        t.rotate(tr.rotation)
        t.scale(tr.scale_x, tr.scale_y)
    return t


def _world_transform(scene: Scene, obj) -> QTransform:
    t = _ancestor_world_transform(scene, obj)
    tr = obj.transform
    t.translate(tr.x, tr.y)
    t.rotate(tr.rotation)
    t.scale(tr.scale_x, tr.scale_y)
    return t


def _paint_own_shape(painter: QPainter, obj) -> None:
    painter.setPen(Qt.NoPen)
    painter.setBrush(_flat_brush(obj))
    painter.save()
    painter.translate(obj.transform.content_x, obj.transform.content_y)
    if obj.shape_type == "rect":
        w = obj.shape_data.get("width", 100)
        h = obj.shape_data.get("height", 80)
        painter.drawRect(-w / 2, -h / 2, w, h)
    elif obj.shape_type == "circle":
        r = obj.shape_data.get("radius", 50)
        painter.drawEllipse(QPointF(0, 0), r, r)
    elif obj.shape_type == "polygon":
        if obj.shape_data.get("points"):
            painter.drawPath(_local_path(obj))
    painter.restore()


def _subtree_raw_union(scene: Scene, obj) -> QPainterPath:
    union = QPainterPath()

    def child_transform(t: QTransform, o) -> QTransform:
        c = QTransform(t)
        c.translate(o.transform.x, o.transform.y)
        c.rotate(o.transform.rotation)
        c.scale(o.transform.scale_x, o.transform.scale_y)
        return c

    def walk(o, t: QTransform):
        nonlocal union
        if not o.visible:
            return
        local = _get_local_shape(o, include_children=False)
        p = t.map(local)
        union = union.united(p) if not union.isEmpty() else p
        for c in o.children:
            walk(c, child_transform(t, c))

    walk(obj, QTransform())
    return union


def _subtree_visible_union(scene: Scene, obj) -> QPainterPath:
    union = QPainterPath()
    root_world = _world_transform(scene, obj)
    inv_root, ok_root = root_world.inverted()

    def child_transform(t: QTransform, o) -> QTransform:
        c = QTransform(t)
        c.translate(o.transform.x, o.transform.y)
        c.rotate(o.transform.rotation)
        c.scale(o.transform.scale_x, o.transform.scale_y)
        return c

    def base_for_mask(o):
        parent = scene.find_parent(o)
        if parent is not None:
            children = parent.children
            try:
                idx = children.index(o)
            except ValueError:
                return parent
            j = idx - 1
            while j >= 0 and children[j].is_mask:
                j -= 1
            return children[j] if j >= 0 else parent
        try:
            idx = scene.objects.index(o)
        except ValueError:
            return None
        j = idx - 1
        while j >= 0 and scene.objects[j].is_mask:
            j -= 1
        return scene.objects[j] if j >= 0 else None

    def get_object_path(o) -> QPainterPath:
        return _world_transform(scene, o).map(_subtree_visible_union(scene, o))

    def has_mask(o) -> bool:
        for oo in o.iter_subtree():
            if oo.is_mask:
                return True
        return False

    def piece_of(o) -> QPainterPath:
        raw = _subtree_raw_union(scene, o)
        if not has_mask(o):
            return raw
        holes = QPainterPath()
        def collect_erase(o2, t2):
            nonlocal holes
            if not o2.visible:
                return
            if o2.is_mask and o2.mask_mode == "erase":
                # An erase mask may itself be a symbol (container): its whole
                # rendered subtree carves the hole, not just the container's
                # own (empty) local geometry.
                p = t2.map(_subtree_raw_union(scene, o2))
                if not p.isEmpty():
                    holes = holes.united(p) if not holes.isEmpty() else p
            for c in o2.children:
                collect_erase(c, child_transform(t2, c))
        collect_erase(o, QTransform())
        if holes.isEmpty():
            return raw
        return raw.subtracted(holes)

    def walk(o, t: QTransform):
        nonlocal union
        if not o.visible:
            return
        if o.is_mask and o.mask_mode == "erase":
            hole = t.map(_subtree_raw_union(scene, o))
            if not hole.isEmpty() and not union.isEmpty():
                union = union.subtracted(hole)
            return
        if o.is_mask:
            own = t.map(piece_of(o))
            clip = None
            base = base_for_mask(o)
            if base is not None and base is not obj and ok_root:
                base_sil = get_object_path(base)
                clip = inv_root.map(base_sil)
            if clip is not None and not clip.isEmpty():
                piece = own.intersected(clip)
            else:
                piece = own
            if not piece.isEmpty():
                union = union.united(piece) if not union.isEmpty() else piece
            return
        p = t.map(_get_local_shape(o, include_children=False))
        if not p.isEmpty():
            union = union.united(p) if not union.isEmpty() else p
        for c in o.children:
            if c.visible:
                walk(c, child_transform(t, c))

    walk(obj, QTransform())
    return union


def _get_object_path(scene: Scene, obj) -> QPainterPath:
    return _world_transform(scene, obj).map(_subtree_visible_union(scene, obj))


def _get_object_raw_path(scene: Scene, obj) -> QPainterPath:
    return _world_transform(scene, obj).map(_subtree_raw_union(scene, obj))


def _compute_mask_paths(scene: Scene):
    order = list(scene.iter_objects())
    base_runs: dict[str, list[tuple[int, object]]] = {}
    for i, obj in enumerate(order):
        if not obj.visible or not obj.is_mask:
            continue
        base = _mask_base(scene, obj)
        if base is not None:
            base_runs.setdefault(base.id, []).append((i, obj))

    base_holes: dict[str, QPainterPath] = {}
    clip_holes: dict[str, QPainterPath] = {}
    for base_id, run in base_runs.items():
        run = sorted(run, key=lambda item: -item[0])
        accum = QPainterPath()
        base_hole = QPainterPath()
        for _, mask in run:
            if mask.mask_mode == "erase":
                p = _get_object_raw_path(scene, mask)
                accum = accum.united(p)
                base_hole = base_hole.united(p)
            else:
                clip_holes[mask.id] = accum
        if not base_hole.isEmpty():
            base_holes[base_id] = base_hole
    return base_holes, clip_holes


def _mask_base(scene: Scene, obj):
    parent = scene.find_parent(obj)
    if parent is not None:
        children = parent.children
        try:
            idx = children.index(obj)
        except ValueError:
            return parent
        j = idx - 1
        while j >= 0 and children[j].is_mask:
            j -= 1
        return children[j] if j >= 0 else parent
    try:
        idx = scene.objects.index(obj)
    except ValueError:
        return None
    j = idx - 1
    while j >= 0 and scene.objects[j].is_mask:
        j -= 1
    return scene.objects[j] if j >= 0 else None


def _clip_path_for(scene: Scene, obj, base_holes, clip_holes):
    hole = base_holes.get(obj.id)
    if obj.is_mask and obj.mask_mode == "wrap":
        base = _mask_base(scene, obj)
        if base is None and hole is None:
            return None
        clip = _get_object_path(scene, base) if base is not None else _get_object_raw_path(scene, obj)
        eab = clip_holes.get(obj.id)
        if eab is not None and not eab.isEmpty():
            clip = clip.subtracted(eab)
        if hole is not None and not hole.isEmpty():
            clip = clip.subtracted(hole)
        return clip
    if hole is not None and not hole.isEmpty():
        return _get_object_path(scene, obj).subtracted(hole)
    return None


def _apply_clip(painter: QPainter, scene: Scene, obj, clip_world: QPainterPath) -> None:
    t = _ancestor_world_transform(scene, obj)
    inv, ok = t.inverted()
    mapped = inv.map(clip_world) if ok and not t.isIdentity() else clip_world
    # Nested masks stack: an inner mask inside a symbol that is itself a mask
    # inherits the outer symbol's clip. Intersect rather than replace, or the
    # inner content leaks past the parent symbol's boundary.
    if painter.hasClipping():
        painter.setClipPath(mapped, Qt.ClipOperation.IntersectClip)
    else:
        painter.setClipPath(mapped)


def _draw_grouped(painter: QPainter, scene: Scene, obj, base_holes, clip_holes, opacity: float) -> None:
    local_union = _subtree_raw_union(scene, obj)
    if local_union.isEmpty():
        return
    world_bounds = _world_transform(scene, obj).mapRect(local_union.boundingRect())
    if world_bounds.isEmpty():
        return

    s = 1.0
    margin = 2.0
    left = world_bounds.left() - margin
    top = world_bounds.top() - margin
    w_img = max(1, math.ceil((world_bounds.width() + 2 * margin) * s))
    h_img = max(1, math.ceil((world_bounds.height() + 2 * margin) * s))

    img = QImage(w_img, h_img, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))

    bp = QPainter(img)
    bp.setRenderHint(QPainter.Antialiasing)
    bp.translate(-left * s, -top * s)
    bp.scale(s, s)

    bp.save()
    for o in _ancestors(scene, obj):
        bp.translate(o.transform.x, o.transform.y)
        bp.rotate(o.transform.rotation)
        bp.scale(o.transform.scale_x, o.transform.scale_y)
    bp.translate(obj.transform.x, obj.transform.y)
    bp.rotate(obj.transform.rotation)
    bp.scale(obj.transform.scale_x, obj.transform.scale_y)
    _paint_own_shape(bp, obj)
    for child in obj.children:
        if child.visible:
            _draw_object(bp, scene, child, base_holes, clip_holes)
    bp.restore()
    bp.end()

    painter.save()
    painter.setOpacity(max(0.0, min(opacity, 1.0)))
    a_inv, ok = _ancestor_world_transform(scene, obj).inverted()
    dest = QRectF(left, top, w_img / s, h_img / s)
    if ok:
        dest = a_inv.mapRect(dest)
    painter.drawImage(dest, img, QRectF(0, 0, w_img, h_img))
    painter.restore()


def _draw_object(painter: QPainter, scene: Scene, obj, base_holes, clip_holes):
    if not obj.visible:
        return
    if obj.is_mask and obj.mask_mode == "erase":
        return

    clip = _clip_path_for(scene, obj, base_holes, clip_holes)
    painter.save()
    if clip is not None:
        _apply_clip(painter, scene, obj, clip)
    _draw_subtree(painter, scene, obj, base_holes, clip_holes)
    painter.restore()


def _draw_subtree(painter: QPainter, scene: Scene, obj, base_holes, clip_holes):
    painter.save()
    painter.translate(obj.transform.x, obj.transform.y)
    painter.rotate(obj.transform.rotation)
    painter.scale(obj.transform.scale_x, obj.transform.scale_y)

    combined = painter.opacity() * obj.opacity
    if obj.children and combined < 0.999:
        painter.restore()
        _draw_grouped(painter, scene, obj, base_holes, clip_holes, combined)
        return

    painter.setOpacity(combined)
    _paint_own_shape(painter, obj)

    for child in obj.children:
        if child.visible:
            _draw_object(painter, scene, child, base_holes, clip_holes)

    painter.restore()


# --------------------------------------------------------------------------- #
# Export helpers
# --------------------------------------------------------------------------- #

_IMAGE_FORMATS = {
    "png": ("PNG", "PNG image (*.png)"),
    "jpg": ("JPEG", "JPEG image (*.jpg *.jpeg)"),
    "bmp": ("BMP", "BMP image (*.bmp)"),
    "webp": ("WEBP", "WebP image (*.webp)"),
}

_VIDEO_CODECS = {
    "h264": ("libx264", ".mp4", "H.264 / MP4"),
    "h265": ("libx265", ".mp4", "H.265 / HEVC / MP4"),
    "vp9": ("libvpx-vp9", ".webm", "VP9 / WebM"),
    "av1": ("libsvtav1", ".mp4", "AV1 / MP4"),
}


def save_image(scene: Scene, settings: RenderSettings, parent=None) -> bool:
    image = render_scene_image(scene, scene.current_frame)
    fmt_key = settings.image_format
    qt_fmt, file_filter = _IMAGE_FORMATS.get(fmt_key, _IMAGE_FORMATS["png"])
    default = f"render.{qt_fmt.lower()}"
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save Image", default, file_filter
    )
    if not path:
        return False
    ok = image.save(path, qt_fmt, settings.image_quality)
    if not ok:
        QMessageBox.warning(parent, "Export Error", f"Could not save image to:\n{path}")
        return False
    return True


def copy_image_to_clipboard(scene: Scene) -> None:
    image = render_scene_image(scene, scene.current_frame)
    QApplication.clipboard().setImage(image)


def encode_frame_dir(frame_dir: Path, scene: Scene, settings: RenderSettings,
                     parent=None) -> bool:
    """Encode an already-rendered PNG frame series with ffmpeg.

    The expensive scene-rendering pass has already happened (frames live in a
    temp dir), so saving only costs the ffmpeg encode itself.
    """
    frame_glob = frame_dir / "frame_%06d.png"
    if not any(frame_dir.glob("frame_*.png")):
        return False

    codec, ext, _ = _VIDEO_CODECS.get(settings.video_codec, _VIDEO_CODECS["h264"])
    default = f"render{ext}"
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save Video", default,
        f"Video file (*{ext})",
    )
    if not path:
        return False

    rate_mult = max(1, settings.video_scale)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        QMessageBox.warning(
            parent, "Export Error",
            "ffmpeg was not found on this system.\n"
            "Install ffmpeg and make sure it is on PATH.",
        )
        return False

    fps = max(1, scene.fps)
    crf = {
        "h264": 18, "h265": 20, "vp9": 28, "av1": 30,
    }.get(settings.video_codec, 18)
    vf = f"scale=trunc(iw*{rate_mult}/2)*2:trunc(ih*{rate_mult}/2)*2"
    cmd = [
        ffmpeg,
        "-y",
        "-framerate", str(fps),
        "-i", str(frame_glob),
        "-vf", vf,
        "-c:v", codec,
        "-crf", str(crf),
        "-b:v", f"{settings.video_bitrate_kbps}k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        path,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(frame_dir)
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-2000:]
        QMessageBox.warning(
            parent, "Export Error",
            f"ffmpeg failed:\n{tail}",
        )
        return False
    if not Path(path).exists():
        QMessageBox.warning(parent, "Export Error", f"Output file was not written:\n{path}")
        return False
    return True


def _scene_fingerprint(scene: Scene) -> int:
    """Cheap content fingerprint used to decide whether a cached video render
    is still up to date. When the animation changes, the render re-runs on the
    next show; otherwise the cached frames play back instantly."""
    items = [scene.start_frame, scene.end_frame, scene.fps]
    for o in scene.iter_objects():
        t = o.transform
        items += [
            id(o), o.visible, o.is_mask, o.mask_mode, o.opacity,
            t.x, t.y, t.rotation, t.scale_x, t.scale_y,
            hash(o.color), id(o.shape_data),
        ]
        for channel, keyframes in o.keyframes.items():
            items.append(channel)
            items.append(tuple((k.frame, k.value, k.interpolation)
                               for k in keyframes))
    return hash(tuple(items))


# --------------------------------------------------------------------------- #
# Preview widget + render window
# --------------------------------------------------------------------------- #

class _RenderView(QWidget):
    """Zoomable / pannable frame display mirroring the Stage viewport:
    Shift+wheel zooms around the cursor, middle-drag pans."""

    MIN_ZOOM = 0.05
    MAX_ZOOM = 8.0

    def __init__(self):
        super().__init__()
        self._image: QImage | None = None
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self._panning = False
        self._last_pos = QPointF()
        self.setMinimumSize(320, 240)
        self.setFocusPolicy(Qt.StrongFocus)

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#2a2a2a"))
        img = self._image
        if img is not None:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            w = img.width() * self.zoom
            h = img.height() * self.zoom
            x = (self.width() - w) / 2 + self._pan.x()
            y = (self.height() - h) / 2 + self._pan.y()
            painter.drawImage(QRectF(x, y, w, h), img)
            painter.setPen(QPen(QColor(60, 60, 60), 1))
            painter.drawRect(QRectF(x, y, w, h))
        painter.end()

    def wheelEvent(self, event):
        if not (event.modifiers() & Qt.ShiftModifier):
            super().wheelEvent(event)
            return
        delta = (event.angleDelta().y() or event.angleDelta().x()
                 or event.pixelDelta().y())
        factor = 1.15 if delta > 0 else 1 / 1.15
        old = self.zoom
        new = max(self.MIN_ZOOM, min(old * factor, self.MAX_ZOOM))
        if new == old:
            return
        pos = event.position()
        img = self._image
        if img is not None:
            w_old = img.width() * old
            h_old = img.height() * old
            x_old = (self.width() - w_old) / 2 + self._pan.x()
            y_old = (self.height() - h_old) / 2 + self._pan.y()
            ix = (pos.x() - x_old) / old
            iy = (pos.y() - y_old) / old
        self.zoom = new
        if img is not None:
            w_new = img.width() * new
            h_new = img.height() * new
            self._pan.setX(pos.x() - ix * new - (self.width() - w_new) / 2)
            self._pan.setY(pos.y() - iy * new - (self.height() - h_new) / 2)
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._last_pos = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            self._pan += event.position() - self._last_pos
            self._last_pos = event.position()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.releaseMouse()
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class RenderPreview(QWidget):
    """Shows the rendered frame(s). Video renders are pre-rendered into a temp
    PNG series when the window opens, so playback and export never touch the
    scene renderer again. The export control lives in the dock's header (see
    RenderHeader), exactly like the timeline's transport bar."""

    RENDER_BATCH = 8

    def __init__(self, scene: Scene, settings_getter, mode: str):
        super().__init__()
        self.scene = scene
        self.settings_getter = settings_getter
        self.mode = mode  # "image" or "video"
        self._image: QImage | None = None
        self._export_enabled = True
        self._export_button: QPushButton | None = None

        self._frame_dir: Path | None = None   # temp PNG series for video mode
        self._frame_paths: list[Path] = []
        self._frame_index = 0
        self._render_index = 0
        self._rendering = False
        self._fp: int | None = None

        self.setMinimumSize(320, 240)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self._view = _RenderView()
        self._layout.addWidget(self._view, 1)

        # Bottom bar: a plain progress bar while rendering, then the video
        # player (frame/time info on top, play button + timeline handle below).
        self._bottom = QWidget()
        self._bottom_layout = QVBoxLayout(self._bottom)
        self._bottom_layout.setContentsMargins(8, 6, 8, 6)
        self._bottom_layout.setSpacing(4)

        self._info = QLabel("")
        self._info.setStyleSheet("color: #b0b0b0; font-size: 11px;")

        self._timeline_row = QWidget()
        _row = QHBoxLayout(self._timeline_row)
        _row.setContentsMargins(0, 0, 0, 0)
        _row.setSpacing(6)
        self._play_button = QPushButton("Play")
        self._play_button.setFixedSize(56, 22)
        self._play_button.setFocusPolicy(Qt.NoFocus)
        self._play_button.clicked.connect(self._toggle_playback)
        _row.addWidget(self._play_button)
        self._timeline = QSlider(Qt.Horizontal)
        self._timeline.setFocusPolicy(Qt.NoFocus)
        self._timeline.sliderPressed.connect(self._begin_scrub)
        self._timeline.sliderReleased.connect(self._end_scrub)
        self._timeline.valueChanged.connect(self._seek_video)
        _row.addWidget(self._timeline, 1)
        self._timeline_row.hide()

        self._progress = QProgressBar()
        self._progress.setMaximumHeight(16)
        self._progress.setVisible(False)

        self._bottom_layout.addWidget(self._info)
        self._bottom_layout.addWidget(self._timeline_row)
        self._bottom_layout.addWidget(self._progress)
        self._layout.addWidget(self._bottom)

        if mode != "video":
            self._bottom.hide()

        self._scrubbing = False
        self._was_playing = False

        self._render_timer = QTimer(self)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_tick)

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(1000 // max(1, scene.fps))
        self._play_timer.timeout.connect(self._next_video_frame)

    # -- export --------------------------------------------------------------
    def show_export_menu(self, anchor: QPushButton) -> None:
        if not self._export_enabled:
            return
        from ui.menus import StripeMenu
        menu = StripeMenu()
        if self.mode == "image":
            menu.add_action("Save Image...", callback=lambda checked: self._save_image())
            menu.add_action("Copy", callback=lambda checked: self._copy_image())
        else:
            menu.add_action("Save Video...", callback=lambda checked: self._save_video())
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def set_export_enabled(self, enabled: bool) -> None:
        """Disable the header export button while a video render is running."""
        self._export_enabled = enabled
        if self._export_button is not None:
            self._export_button.setEnabled(enabled)

    # -- frame update --------------------------------------------------------
    def set_frame_image(self, image: QImage) -> None:
        self._image = image
        self._view.set_image(image)

    def show_current_image(self) -> None:
        self.set_frame_image(render_scene_image(self.scene, self.scene.current_frame))

    # -- video render + playback ---------------------------------------------
    def on_shown(self) -> None:
        """Called whenever the window is (re)shown. Re-renders the animation
        when the temp frame cache is missing or stale, else replays it."""
        if self._rendering or self._render_timer.isActive():
            return
        fp = _scene_fingerprint(self.scene)
        if self._frame_paths and fp == self._fp:
            self.start_video_playback()
        else:
            self.render_animation()

    def render_animation(self) -> None:
        if self._rendering or self._render_timer.isActive():
            return
        self._fp = _scene_fingerprint(self.scene)
        self._clear_frames()
        self._rendering = True
        self.set_export_enabled(False)
        self._show_render_bar()

        total = self.scene.end_frame - self.scene.start_frame + 1
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._render_timer.start()

    def _show_render_bar(self) -> None:
        """During the render the bottom bar collapses to a plain progress bar."""
        self._progress.setVisible(True)
        self._info.hide()
        self._timeline_row.hide()

    def _show_player_bar(self) -> None:
        """Once the animation is rendered, the bar becomes the player controls:
        current second/frame info above, play button + timeline handle below."""
        self._progress.setVisible(False)
        self._timeline.setRange(0, max(0, len(self._frame_paths) - 1))
        self._update_player_info()
        self._info.show()
        self._timeline_row.show()

    def _update_player_info(self) -> None:
        total = len(self._frame_paths)
        if not total:
            return
        fps = max(1, self.scene.fps)
        self._info.setText(
            f"Frame {self._frame_index + 1}/{total} · "
            f"{self._frame_index / fps:.1f}s / {total / fps:.1f}s"
        )
        self._timeline.blockSignals(True)
        self._timeline.setValue(self._frame_index)
        self._timeline.blockSignals(False)

    def _seek_video(self, index: int) -> None:
        if not self._frame_paths:
            return
        self._frame_index = max(0, min(index, len(self._frame_paths) - 1))
        self.set_frame_image(QImage(str(self._frame_paths[self._frame_index])))
        self._update_player_info()

    def _begin_scrub(self) -> None:
        self._scrubbing = True
        self._was_playing = self._play_timer.isActive()
        self.stop_video_playback()

    def _end_scrub(self) -> None:
        self._scrubbing = False
        if self._was_playing:
            self.start_video_playback()

    def _toggle_playback(self) -> None:
        if self._play_timer.isActive():
            self.stop_video_playback()
        else:
            self.start_video_playback()

    def _render_tick(self) -> None:
        scene = self.scene
        start = scene.start_frame
        total = scene.end_frame - start + 1
        for _ in range(self.RENDER_BATCH):
            if self._render_index >= total:
                self._finish_animation_render()
                return
            frame = start + self._render_index
            image = render_scene_image(scene, frame)
            path = self._frame_dir / f"frame_{self._render_index:06d}.png"
            image.save(str(path), "PNG")
            self._frame_paths.append(path)
            self._render_index += 1
        self._progress.setValue(self._render_index)
        self.set_frame_image(QImage(str(self._frame_paths[-1])))

    def _finish_animation_render(self) -> None:
        self._render_timer.stop()
        self._rendering = False
        self.set_export_enabled(True)
        self.start_video_playback()
        QTimer.singleShot(300, self._save_video)

    def _clear_frames(self) -> None:
        self._frame_paths = []
        self._render_index = 0
        self._frame_index = 0
        if self._frame_dir is not None:
            shutil.rmtree(self._frame_dir, ignore_errors=True)
        self._frame_dir = Path(tempfile.mkdtemp(prefix="fas_render_"))

    def start_video_playback(self) -> None:
        if not self._frame_paths:
            return
        self._frame_index = 0
        self._show_player_bar()
        self.set_frame_image(QImage(str(self._frame_paths[0])))
        self._play_timer.start()
        self._play_button.setText("Pause")

    def stop_video_playback(self) -> None:
        self._play_timer.stop()
        self._play_button.setText("Play")

    def _next_video_frame(self) -> None:
        if not self._frame_paths:
            return
        self._frame_index += 1
        if self._frame_index >= len(self._frame_paths):
            self._frame_index = 0
        self.set_frame_image(QImage(str(self._frame_paths[self._frame_index])))
        self._update_player_info()

    # -- export handlers -----------------------------------------------------
    def _save_image(self):
        save_image(self.scene, self.settings_getter(), parent=self)

    def _copy_image(self):
        copy_image_to_clipboard(self.scene)

    def _save_video(self):
        if not self._frame_paths or self._frame_dir is None:
            return
        self.set_export_enabled(False)
        try:
            encode_frame_dir(
                self._frame_dir, self.scene, self.settings_getter(), parent=self
            )
        finally:
            self.set_export_enabled(True)


class RenderHeader(QWidget):
    """Export control that lives in the dock's header bar (mirroring how the
    timeline puts its transport bar there). Renders as:
        [hamburger]  Render  ........  [Export]  Image/Animation
    """

    def __init__(self, preview: RenderPreview):
        super().__init__()
        self.preview = preview

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.export_button = QPushButton("Export")
        self.export_button.setFixedSize(70, 20)
        self.export_button.clicked.connect(self._on_export)
        layout.addWidget(self.export_button)

        mode_label = "Image" if preview.mode == "image" else "Animation"
        self.info_label = QLabel(mode_label)
        self.info_label.setStyleSheet("color: #c0c0c0; font-size: 11px;")
        layout.addWidget(self.info_label)

        # Let the preview disable this button while an export is running.
        preview._export_button = self.export_button

    def _on_export(self):
        self.preview.show_export_menu(self.export_button)


class RenderWindow(QMainWindow):
    """A separate top-level window whose single dock holds the RenderPreview.
    The dock is a real DockWidget slot (same system as the main window's
    docks), so the render can be treated like any other dock - its Export
    control lives in the dock header bar."""

    def __init__(self, scene: Scene, settings_getter, mode: str = "image",
                 title: str = "Render"):
        super().__init__()
        self.scene = scene
        self.settings_getter = settings_getter
        self.resize(560, 560)
        self.setWindowTitle(title)

        self.preview = RenderPreview(scene, settings_getter, mode)
        manager = DockManager(SimpleNamespace(scene=scene))
        manager.register(
            "render",
            "Render",
            lambda ctx: self.preview,
            header_factory=lambda ctx: RenderHeader(self.preview),
        )
        self.slot = DockWidget(manager, "render")
        self.setCentralWidget(self.slot)

    def refresh(self) -> None:
        """(Re)render or replay the current content. Called from showEvent and
        also directly when the window is refocused, because show() does not
        fire showEvent again for an already-visible window."""
        if self.preview.mode == "image":
            self.preview.show_current_image()
        else:
            self.preview.on_shown()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def closeEvent(self, event):
        self.preview.stop_video_playback()
        super().closeEvent(event)


def show_render_window(parent, scene: Scene, mode: str) -> RenderWindow | None:
    """Open (or refocus) the export window for the given mode. The window's
    showEvent handles the first-frame display and the immediate video render."""
    settings_getter = lambda: get_render_settings(scene)
    win = RenderWindow(
        scene, settings_getter, mode=mode,
        title="Render - Image" if mode == "image" else "Render - Animation",
    )
    win.show()
    win.raise_()
    win.activateWindow()
    return win