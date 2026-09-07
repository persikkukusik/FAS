from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class Transform:
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0

    def copy(self) -> Transform:
        return Transform(self.x, self.y, self.rotation, self.scale_x, self.scale_y)


INTERPOLATION_MODES = ("constant", "linear", "adaptive")
MASK_MODES = ("wrap", "erase")

SHAPE_TYPES = ("rect", "circle", "polygon")
CONTAINER_TYPE = "container"


@dataclass
class Keyframe:
    frame: int
    value: float
    interpolation: str = "adaptive"


@dataclass(eq=False)
class SceneObject:
    """A node in the scene tree.

    Two kinds of nodes exist:

    - A **shape** (``shape_type`` in ``rect``/``circle``/``polygon``) is a
      real, drawable path with geometry in ``shape_data``. Shapes are the
      leaf objects on the stage.
    - A **container** (``shape_type == "container"``, i.e. a Symbol) groups
      shapes and other containers under ``children`` for organisation. It has
      no geometry of its own, but it carries a transform, so moving/rotating/
      scaling (or keyframing) a container moves its whole subtree as a unit.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "Object"
    shape_type: str = "rect"
    shape_data: dict = field(default_factory=dict)
    transform: Transform = field(default_factory=Transform)
    color: str = "#cccccc"
    opacity: float = 1.0
    visible: bool = True
    locked: bool = False
    is_mask: bool = False
    mask_mode: str = "wrap"
    keyframes: dict = field(default_factory=dict)
    children: list[SceneObject] = field(default_factory=list)
    expanded: bool = True

    @property
    def is_container(self) -> bool:
        return self.shape_type == CONTAINER_TYPE

    def iter_subtree(self):
        """Yield this object and all of its descendants, parent before child
        (pre-order). This is also the draw order for rendering."""
        yield self
        for child in self.children:
            yield from child.iter_subtree()

    def get_keyframes(self, channel: str) -> list[Keyframe]:
        return self.keyframes.get(channel, [])

    def has_keyframes(self) -> bool:
        return any(len(v) > 0 for v in self.keyframes.values())

    def set_keyframe(self, frame: int, channel: str, value: float) -> None:
        if channel not in self.keyframes:
            self.keyframes[channel] = []
        ch = self.keyframes[channel]
        ch[:] = [kf for kf in ch if kf.frame != frame]
        ch.append(Keyframe(frame=frame, value=value))
        ch.sort(key=lambda kf: kf.frame)

    def remove_keyframe(self, frame: int, channel: str) -> None:
        if channel in self.keyframes:
            self.keyframes[channel] = [kf for kf in self.keyframes[channel] if kf.frame != frame]


@dataclass
class Scene:
    objects: list[SceneObject] = field(default_factory=list)
    start_frame: int = 0
    end_frame: int = 60
    fps: int = 24
    current_frame: int = 0

    def add_object(self, obj: SceneObject) -> SceneObject:
        self.objects.append(obj)
        return obj

    def add_symbol(self, name: str = "Symbol") -> SceneObject:
        """Create a Symbol (a container) and add it at the scene root.

        A Symbol has no geometry of its own; it groups shapes and nested
        Symbols for organisation. Use its ``children`` list to hold them.
        """
        container = SceneObject(name=name, shape_type=CONTAINER_TYPE)
        self.objects.append(container)
        return container

    def remove_object(self, obj: SceneObject) -> bool:
        if obj in self.objects:
            self.objects.remove(obj)
            return True
        parent = self.find_parent(obj)
        if parent is not None:
            parent.children.remove(obj)
            return True
        return False

    def iter_objects(self):
        """Yield every object in the scene (children included), in draw order
        (parent before its children, siblings in list order)."""
        stack = list(reversed(self.objects))
        while stack:
            obj = stack.pop()
            yield obj
            stack.extend(reversed(obj.children))

    def display_objects(self) -> list[SceneObject]:
        """Top-most-first flattening used by the outliner and timeline:
        each object followed by its children, siblings shown top-most first."""
        out: list[SceneObject] = []

        def walk(objs: list[SceneObject]) -> None:
            for o in reversed(objs):
                out.append(o)
                walk(o.children)

        walk(self.objects)
        return out

    def get_object_by_id(self, obj_id: str) -> SceneObject | None:
        for obj in self.iter_objects():
            if obj.id == obj_id:
                return obj
        return None

    def find_parent(self, obj: SceneObject) -> SceneObject | None:
        for cand in self.iter_objects():
            if obj in cand.children:
                return cand
        return None