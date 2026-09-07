from __future__ import annotations

from dataclasses import dataclass, field

from core.model import Scene


@dataclass
class RenderSettings:
    """User-configurable export options (persisted on the Scene object)."""

    # Image export
    image_format: str = "png"        # png | jpg | bmp | webp
    image_quality: int = 100         # 0..100

    # Video export
    video_codec: str = "h264"        # h264 | h265 | vp9 | av1
    video_bitrate_kbps: int = 8000   # bitrate in kbps
    video_scale: int = 1             # resolution multiplier (1, 2, 4)


_attr = "_fas_render_settings"


def get_render_settings(scene: Scene) -> RenderSettings:
    settings = getattr(scene, _attr, None)
    if settings is None:
        settings = RenderSettings()
        setattr(scene, _attr, settings)
    return settings


def set_render_settings(scene: Scene, settings: RenderSettings) -> None:
    setattr(scene, _attr, settings)