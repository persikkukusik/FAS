from __future__ import annotations

from core.model import Scene, SceneObject


class SelectionState:
    """Global selection state shared by all docks (stage, outliner, etc.).

    When any dock changes the selection it updates this single shared state,
    so all other docks see the change immediately without having to ask
    individual docks.
    """

    _selected: list[SceneObject] = []
    _hovered: SceneObject | None = None

    @classmethod
    def selected(cls) -> list[SceneObject]:
        return cls._selected

    @classmethod
    def set_selected(cls, objects: list[SceneObject]) -> None:
        cls._selected = list(objects)

    @classmethod
    def clear_selected(cls) -> None:
        cls._selected = []

    @classmethod
    def toggle(cls, obj: SceneObject) -> None:
        if obj in cls._selected:
            cls._selected.remove(obj)
        else:
            cls._selected.append(obj)

    @classmethod
    def set_hovered(cls, obj: SceneObject | None) -> None:
        cls._hovered = obj

    @classmethod
    def hovered(cls) -> SceneObject | None:
        return cls._hovered


class KeyframeSelection:
    """A logical keyframe on a timeline track.

    In this app an object has multiple animation channels, but a "keyframe"
    (a diamond in the timeline) represents *all* channels sampled at one frame.
    Selecting and transforming a keyframe therefore affects every channel at
    that frame on the same object together.
    """

    __slots__ = ("obj", "frame")

    def __init__(self, obj: SceneObject, frame: int):
        self.obj = obj
        self.frame = frame

    def iterate(self):
        """Yield (channel, Keyframe) pairs for every channel at this frame."""
        for channel, kfs in self.obj.keyframes.items():
            for kf in kfs:
                if kf.frame == self.frame:
                    yield channel, kf

    def mode(self) -> str | None:
        seen = {kf.interpolation for _, kf in self.iterate()}
        return seen.pop() if len(seen) == 1 else None

    def __eq__(self, other):
        return (
            isinstance(other, KeyframeSelection)
            and other.obj is self.obj
            and other.frame == self.frame
        )

    def __hash__(self):
        return hash((id(self.obj), self.frame))


class KeyframeSelectionState:
    """Global keyframe selection shared by all timeline docks.

    Mirrors SelectionState (object selection) but for keyframes on the
    timeline. When any dock changes the keyframe selection it updates this
    shared state, so every dock sees the change immediately.
    """

    _selected: list[KeyframeSelection] = []

    @classmethod
    def selected(cls) -> list[KeyframeSelection]:
        return cls._selected

    @classmethod
    def set_selected(cls, selections: list[KeyframeSelection]) -> None:
        cls._selected = list(selections)

    @classmethod
    def clear_selected(cls) -> None:
        cls._selected = []


def serialize_keyframe_selection(
    selections: list[KeyframeSelection],
) -> list[tuple[str, int]]:
    """Convert live keyframe selections into history-safe (obj_id, frame) pairs."""
    return [(sel.obj.id, sel.frame) for sel in selections]


def deserialize_keyframe_selection(
    scene: Scene,
    pairs: list[tuple[str, int]],
) -> list[KeyframeSelection]:
    """Rebuild live keyframe selections from (obj_id, frame) pairs, resolving
    against `scene`'s current objects (which may have been replaced by an
    undo/redo restore)."""
    result = []
    for obj_id, frame in pairs:
        obj = scene.get_object_by_id(obj_id)
        if obj is not None:
            result.append(KeyframeSelection(obj, frame))
    return result