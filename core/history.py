from __future__ import annotations

from typing import TYPE_CHECKING

from core.commands import Command
from core.selection import (
    KeyframeSelectionState,
    deserialize_keyframe_selection,
    serialize_keyframe_selection,
)

if TYPE_CHECKING:
    from core.model import Scene


MAX_HISTORY = 100

_Entry = tuple[Command, list[str], list[tuple[str, int]]]


class History:
    """Undo/redo stack of command objects.

    Every entry carries the object selection (a list of object ids, empty
    when nothing is selected) and the keyframe selection (``(obj_id, frame)``
    pairs) as they were *before* the command ran, so undoing a command
    restores the selection that was active when it was performed.  Call
    ``push()`` *before* applying any selection change so the recorded
    selection is the pre-op one.
    """

    def __init__(self, scene: Scene):
        self.scene = scene
        # Selection currently "recorded" with the next command / restored by
        # undo/redo. Kept live by save_*/sync_* calls from the UI.
        self.selected_ids: list[str] = []
        self.keyframe_selection: list[tuple[str, int]] = serialize_keyframe_selection(
            KeyframeSelectionState.selected()
        )
        self._undo_stack: list[_Entry] = []
        self._redo_stack: list[_Entry] = []

    def _capture_selection(self) -> list[tuple[str, int]]:
        return list(self.keyframe_selection)

    def push(self, cmd: Command) -> None:
        """Push a new command onto the undo stack, clearing the redo stack.

        The current ``selected_ids`` / ``keyframe_selection`` are recorded
        alongside the command and are restored by ``undo()``.
        """
        entry: _Entry = (cmd, list(self.selected_ids), self._capture_selection())
        self._undo_stack.append(entry)
        if len(self._undo_stack) > MAX_HISTORY:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def save_selection(self, selected_ids: list[str]) -> None:
        if list(selected_ids) == self.selected_ids:
            return
        self.selected_ids = list(selected_ids)

    def save_keyframe_selection(self, selection: list[tuple[str, int]]) -> None:
        """Record a user-initiated keyframe selection change."""
        if selection == self.keyframe_selection:
            return
        self.keyframe_selection = list(selection)

    def sync_keyframe_selection(self) -> None:
        """Update the recorded keyframe selection *in place* (no history push)."""
        self.keyframe_selection = serialize_keyframe_selection(
            KeyframeSelectionState.selected()
        )

    def sync_selection(self, selected_ids: list[str]) -> None:
        """Update the recorded object selection *in place* (no history push)."""
        self.selected_ids = list(selected_ids)

    def _restore_keyframe_selection(self) -> None:
        """Push the recorded keyframe selection into the live global state."""
        selections = deserialize_keyframe_selection(
            self.scene, self.keyframe_selection
        )
        KeyframeSelectionState.set_selected(selections)

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        cmd, selected_ids, keyframe_selection = self._undo_stack.pop()
        cmd.undo(self.scene)
        # Save the *current* selection with the redo entry so redo() returns
        # to wherever we are right now.
        self._redo_stack.append(
            (cmd, list(self.selected_ids), self._capture_selection())
        )
        # Restore the selection that was live before this command ran.
        self.selected_ids = list(selected_ids)
        self.keyframe_selection = list(keyframe_selection)
        self._restore_keyframe_selection()
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        cmd, selected_ids, keyframe_selection = self._redo_stack.pop()
        cmd.redo(self.scene)
        self._undo_stack.append(
            (cmd, list(self.selected_ids), self._capture_selection())
        )
        if len(self._undo_stack) > MAX_HISTORY:
            self._undo_stack.pop(0)
        # Restore the selection the user had after this command ran.
        self.selected_ids = list(selected_ids)
        self.keyframe_selection = list(keyframe_selection)
        self._restore_keyframe_selection()
        return True

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0
