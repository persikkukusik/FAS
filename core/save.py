from __future__ import annotations

import gzip
import json
import uuid
from pathlib import Path

from core.model import INTERPOLATION_MODES, Keyframe, Scene, SceneObject, Transform

FAS_VERSION = 1

# gzip streams begin with these magic bytes. Old/simple .fas files are stored
# as plain JSON, so we sniff the header on load to accept both formats.
_GZIP_MAGIC = b"\x1f\x8b"


def _valid_interpolation(name: str) -> str:
    """Accept legacy saved modes and unknown names by normalising them."""
    if name == "cubic":
        return "adaptive"
    if name in INTERPOLATION_MODES:
        return name
    return "adaptive"


def _serialize_transform(t: Transform) -> dict:
    return {
        "x": t.x,
        "y": t.y,
        "rotation": t.rotation,
        "scale_x": t.scale_x,
        "scale_y": t.scale_y,
        "content_offset": {"x": t.content_x, "y": t.content_y},
    }


def _deserialize_transform(d: dict) -> Transform:
    content_offset = d.get("content_offset") or {}
    return Transform(
        x=d.get("x", 0.0),
        y=d.get("y", 0.0),
        rotation=d.get("rotation", 0.0),
        scale_x=d.get("scale_x", 1.0),
        scale_y=d.get("scale_y", 1.0),
        content_x=content_offset.get("x", 0.0),
        content_y=content_offset.get("y", 0.0),
    )


def _serialize_keyframes(keyframes: dict) -> dict:
    out = {}
    for channel, frames in keyframes.items():
        out[channel] = [
            {"frame": kf.frame, "value": kf.value, "interpolation": kf.interpolation}
            for kf in frames
        ]
    return out


def _deserialize_keyframes(d: dict) -> dict:
    out = {}
    for channel, frames in d.items():
        out[channel] = [
            Keyframe(
                frame=f["frame"],
                value=f["value"],
                interpolation=_valid_interpolation(f.get("interpolation", "adaptive")),
            )
            for f in frames
        ]
    return out


def _serialize_object(obj: SceneObject) -> dict:
    return {
        "id": obj.id,
        "name": obj.name,
        "shape_type": obj.shape_type,
        "shape_data": obj.shape_data,
        "transform": _serialize_transform(obj.transform),
        "color": obj.color,
        "opacity": obj.opacity,
        "visible": obj.visible,
        "locked": obj.locked,
        "is_mask": obj.is_mask,
        "mask_mode": obj.mask_mode,
        "keyframes": _serialize_keyframes(obj.keyframes),
        "children": [_serialize_object(c) for c in obj.children],
        "expanded": obj.expanded,
    }


def _deserialize_object(d: dict) -> SceneObject:
    return SceneObject(
        id=d.get("id", ""),
        name=d.get("name", "Object"),
        shape_type=d.get("shape_type", "rect"),
        shape_data=d.get("shape_data", {}),
        transform=_deserialize_transform(d.get("transform", {})),
        color=d.get("color", "#cccccc"),
        opacity=d.get("opacity", 1.0),
        visible=d.get("visible", True),
        locked=d.get("locked", False),
        is_mask=d.get("is_mask", False),
        mask_mode=d.get("mask_mode", "wrap"),
        keyframes=_deserialize_keyframes(d.get("keyframes", {})),
        children=[_deserialize_object(c) for c in d.get("children", [])],
        expanded=d.get("expanded", True),
    )


def serialize_scene(scene: Scene) -> dict:
    return {
        "fas_version": FAS_VERSION,
        "start_frame": scene.start_frame,
        "end_frame": scene.end_frame,
        "fps": scene.fps,
        "current_frame": scene.current_frame,
        "objects": [_serialize_object(obj) for obj in scene.objects],
    }


def deserialize_scene(data: dict) -> Scene:
    scene = Scene(
        start_frame=data.get("start_frame", 0),
        end_frame=data.get("end_frame", 60),
        fps=data.get("fps", 24),
        current_frame=data.get("current_frame", 0),
    )
    scene.objects = [_deserialize_object(obj) for obj in data.get("objects", [])]
    return scene


def save_scene_to_file(scene: Scene, path: str | Path) -> None:
    path = Path(path)
    data = serialize_scene(scene)
    raw = json.dumps(data, indent=2).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)


def load_scene_from_file(path: str | Path) -> Scene:
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(_GZIP_MAGIC):
        raw = gzip.decompress(data).decode("utf-8")
    else:
        raw = data.decode("utf-8")
    parsed = json.loads(raw)
    return deserialize_scene(parsed)


SYMBOL_VERSION = 1


def serialize_symbol(symbol: SceneObject) -> dict:
    """Serialize a single Symbol (a container subtree) for a .sym file."""
    return {
        "sym_version": SYMBOL_VERSION,
        "symbol": _serialize_object(symbol),
    }


def _reassign_ids(obj: SceneObject) -> None:
    """Give every object in a subtree a fresh unique id.

    Symbol files preserve each object's id, so importing a copy of a symbol
    that already lives in the scene would otherwise produce duplicate ids. Many
    systems key data by ``obj.id`` (notably mask bases: an erase mask's hole is
    looked up by the id of the object it clips), so two objects sharing an id
    would mutually interfere - e.g. one circle's mask clipping every rectangle
    that happens to share its id. Fresh ids keep an import fully independent.
    """
    obj.id = uuid.uuid4().hex[:8]
    for child in obj.children:
        _reassign_ids(child)


def deserialize_symbol(data: dict) -> SceneObject:
    """Deserialize a Symbol (a container subtree) from a .sym file."""
    symbol = _deserialize_object(data.get("symbol", {}))
    if not symbol.is_container:
        raise ValueError("Not a valid Symbol: the .sym file root is not a container")
    _reassign_ids(symbol)
    return symbol


def export_symbol_to_file(symbol: SceneObject, path: str | Path) -> None:
    """Write a single Symbol to a gzip-compressed .sym file."""
    path = Path(path)
    raw = json.dumps(serialize_symbol(symbol), indent=2).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)


def import_symbol_from_file(path: str | Path) -> SceneObject:
    """Load a single Symbol from a .sym file."""
    path = Path(path)
    data = path.read_bytes()
    if data.startswith(_GZIP_MAGIC):
        raw = gzip.decompress(data).decode("utf-8")
    else:
        raw = data.decode("utf-8")
    parsed = json.loads(raw)
    return deserialize_symbol(parsed)
