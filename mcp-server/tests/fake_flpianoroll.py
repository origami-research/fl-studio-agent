"""Minimal stand-in for FL Studio's `flpianoroll` module.

Mirrors the official API stubs (IL-Group/FL-Studio-API-Stubs) closely enough to
exercise the real .pyscript in tests: Note property defaults, score.PPQ,
addNote/clearNotes/noteCount, and Utils.ShowMessage.

This verifies our executor's logic (unit conversion, spool handling, error
paths). It cannot verify FL's own behavior - that needs the manual smoke test
in docs/10.
"""

from __future__ import annotations


class Note:
    def __init__(self) -> None:
        self.number = 60
        self.time = 0
        self.length = 0
        self.group = 0
        self.pan = 0.5
        self.velocity = 0.8
        self.release = 0.5
        self.color = 0
        self.fcut = 0.5
        self.fres = 0.5
        self.pitchofs = 0
        self.slide = False
        self.porta = False
        self.muted = False
        self.selected = False


class _Score:
    def __init__(self, ppq: int = 96) -> None:
        self.PPQ = ppq
        self._notes: list[Note] = []

    @property
    def noteCount(self) -> int:
        return len(self._notes)

    def addNote(self, note: Note) -> None:
        self._notes.append(note)

    def getNote(self, index: int) -> Note:
        return self._notes[index]

    def deleteNote(self, index: int) -> None:
        del self._notes[index]

    def clearNotes(self, all: bool = False) -> None:
        self._notes.clear()

    def clear(self, all: bool = False) -> None:
        self._notes.clear()


class _Utils:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def ShowMessage(self, message: str) -> None:
        self.messages.append(message)


score = _Score()
Utils = _Utils()


def reset(ppq: int = 96) -> None:
    """Restore module state between tests."""
    score.PPQ = ppq
    score._notes.clear()
    Utils.messages.clear()
