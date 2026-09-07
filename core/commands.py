from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.model import Scene, SceneObject, Keyframe


class Command:
    """Base class for undoable commands.

    Each command captures the minimal before/after state needed to reverse
    itself. The scene is mutated on ``redo()``; ``undo()`` restores it.
    """

    def undo(self, scene: Scene) -> None:
        raise NotImplementedError

    def redo(self, scene: Scene) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Generic property mutations
# --------------------------------------------------------------------------- #

def _set_attr(obj: Any, attr: str, value: Any) -> None:
    """Set *attr* on *obj*, walking dotted paths like ``"transform.x"``."""
    parts = attr.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


class PropertyCommand(Command):
    """Change one or more simple attributes on objects or the scene.

    ``changes`` maps an object id (or ``None`` for scene-level props) to a
    dict of ``{attr: (old_value, new_value)}``.  ``attr`` may be dotted
    (e.g. ``"transform.x"``).
    """

    def __init__(
        self,
        changes: dict[str | None, dict[str, tuple[Any, Any]]],
    ) -> None:
        self.changes = changes

    def undo(self, scene: Scene) -> None:
        for obj_id, attrs in self.changes.items():
            target = scene if obj_id is None else scene.get_object_by_id(obj_id)
            if target is None:
                continue
            for attr, (old, _new) in attrs.items():
                _set_attr(target, attr, old)

    def redo(self, scene: Scene) -> None:
        for obj_id, attrs in self.changes.items():
            target = scene if obj_id is None else scene.get_object_by_id(obj_id)
            if target is None:
                continue
            for attr, (_old, new) in attrs.items():
                _set_attr(target, attr, new)


# --------------------------------------------------------------------------- #
# Structural: add / remove objects
# --------------------------------------------------------------------------- #

class AddObjectCommand(Command):
    """Add an object to a parent list (scene root or a container's children)."""

    def __init__(self, obj: SceneObject, parent_list: list[SceneObject]) -> None:
        self.obj = obj
        self.parent_list = parent_list

    def undo(self, scene: Scene) -> None:
        if self.obj in self.parent_list:
            self.parent_list.remove(self.obj)

    def redo(self, scene: Scene) -> None:
        if self.obj not in self.parent_list:
            self.parent_list.append(self.obj)


class DeleteObjectsCommand(Command):
    """Remove objects from the scene tree, remembering where they came from.

    Each entry is ``(obj, parent_list, index_in_parent_list)``.
    """

    def __init__(
        self,
        entries: list[tuple[SceneObject, list[SceneObject], int]],
    ) -> None:
        self.entries = entries

    def undo(self, scene: Scene) -> None:
        for obj, parent_list, index in self.entries:
            insert_at = min(index, len(parent_list))
            parent_list.insert(insert_at, obj)

    def redo(self, scene: Scene) -> None:
        for obj, parent_list, _index in self.entries:
            if obj in parent_list:
                parent_list.remove(obj)


# --------------------------------------------------------------------------- #
# Reorder (drag-drop in outliner)
# --------------------------------------------------------------------------- #

class ReorderCommand(Command):
    """Move objects between parent lists.

    ``moves`` is a list of ``(obj, old_parent_list, old_index,
    new_parent_list, new_index)``.  On undo the objects go back to their
    old positions; on redo they move to the new ones.
    """

    def __init__(
        self,
        moves: list[
            tuple[
                SceneObject,
                list[SceneObject],
                int,
                list[SceneObject],
                int,
            ]
        ],
    ) -> None:
        self.moves = moves

    def _remove(self, obj: SceneObject, parent_list: list[SceneObject]) -> None:
        if obj in parent_list:
            parent_list.remove(obj)

    def _insert(
        self,
        obj: SceneObject,
        parent_list: list[SceneObject],
        index: int,
    ) -> None:
        insert_at = min(index, len(parent_list))
        parent_list.insert(insert_at, obj)

    def undo(self, scene: Scene) -> None:
        for obj, old_parent, old_idx, new_parent, _new_idx in self.moves:
            self._remove(obj, new_parent)
            self._insert(obj, old_parent, old_idx)

    def redo(self, scene: Scene) -> None:
        for obj, old_parent, _old_idx, new_parent, new_idx in self.moves:
            self._remove(obj, old_parent)
            self._insert(obj, new_parent, new_idx)


# --------------------------------------------------------------------------- #
# Keyframe mutations
# --------------------------------------------------------------------------- #

class KeyframeCommand(Command):
    """Record and reverse keyframe mutations.

    ``changes`` is a list of action tuples:

    * ``(obj, channel, "insert", kf)`` — insert keyframe *kf*
    * ``(obj, channel, "delete", kf)`` — delete keyframe *kf*
    * ``(obj, channel, "move", (kf, old_frame, new_frame))`` — move *kf*
    * ``(obj, channel, "set_interp", (kf, old_interp, new_interp))`` — change interpolation
    * ``(obj, channel, "replace", (old_kf, new_kf))`` — replace *old_kf* with *new_kf*
    """

    Action = tuple  # rough typing; kept simple for readability

    def __init__(self, changes: list[tuple]) -> None:
        self.changes = list(changes)

    # -- helpers used by the UI to build the command incrementally --

    @staticmethod
    def record_insert(obj: SceneObject, channel: str, kf: Keyframe) -> tuple:
        return (obj, channel, "insert", kf)

    @staticmethod
    def record_delete(obj: SceneObject, channel: str, kf: Keyframe) -> tuple:
        return (obj, channel, "delete", kf)

    @staticmethod
    def record_move(obj: SceneObject, channel: str, kf: Keyframe, old_frame: int, new_frame: int) -> tuple:
        return (obj, channel, "move", (kf, old_frame, new_frame))

    @staticmethod
    def record_set_interp(obj: SceneObject, channel: str, kf: Keyframe, old_interp: str, new_interp: str) -> tuple:
        return (obj, channel, "set_interp", (kf, old_interp, new_interp))

    @staticmethod
    def record_replace(obj: SceneObject, channel: str, old_kf: Keyframe, new_kf: Keyframe) -> tuple:
        return (obj, channel, "replace", (old_kf, new_kf))

    # -- undo / redo --

    def _apply(self, scene: Scene, forward: bool) -> None:
        for obj, channel, action, data in self.changes:
            ch = obj.keyframes.setdefault(channel, [])
            if action == "insert":
                if forward:
                    ch.append(data)
                    ch.sort(key=lambda k: k.frame)
                else:
                    ch[:] = [k for k in ch if k is not data]
            elif action == "delete":
                if forward:
                    ch[:] = [k for k in ch if k is not data]
                else:
                    ch.append(data)
                    ch.sort(key=lambda k: k.frame)
            elif action == "move":
                kf, old_frame, new_frame = data
                kf.frame = new_frame if forward else old_frame
            elif action == "set_interp":
                kf, old_interp, new_interp = data
                kf.interpolation = new_interp if forward else old_interp
            elif action == "replace":
                old_kf, new_kf = data
                if forward:
                    ch[:] = [k for k in ch if k is not old_kf]
                    ch.append(new_kf)
                    ch.sort(key=lambda k: k.frame)
                else:
                    ch[:] = [k for k in ch if k is not new_kf]
                    ch.append(old_kf)
                    ch.sort(key=lambda k: k.frame)

    def undo(self, scene: Scene) -> None:
        self._apply(scene, forward=False)

    def redo(self, scene: Scene) -> None:
        self._apply(scene, forward=True)


# --------------------------------------------------------------------------- #
# Compound (groups sub-commands as one undo step)
# --------------------------------------------------------------------------- #

class CompoundCommand(Command):
    """A single undo step composed of several sub-commands.

    Sub-commands are undone in reverse order and redone in forward order.
    """

    def __init__(self, commands: list[Command]) -> None:
        self.commands = list(commands)

    def undo(self, scene: Scene) -> None:
        for cmd in reversed(self.commands):
            cmd.undo(scene)

    def redo(self, scene: Scene) -> None:
        for cmd in self.commands:
            cmd.redo(scene)
