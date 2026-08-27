"""Structured edits: diff two note groups into a delta, and fold deltas.

Instead of recording edits as a replay of imperative action strings, every
change - the model's writes and the user's hand-edits alike - is a
content-addressed NoteDelta. Deltas are compact (only what changed), reversible
(swap added/removed), and foldable (reconstruct any state from a bootstrap plus
a delta list). This is git-style diff/merge applied to notes.

diff_notes(old, new):
  1. exact-hash pass: notes present in both are unchanged and ignored;
  2. leftovers become removed (old-only) and added (new-only);
  3. greedy proximity pairing turns close removed/added pairs into `moved`,
     with a confidence that falls off with distance - a dragged note stays the
     "same" note to the model, a low-confidence pair is flagged not asserted.
"""

from __future__ import annotations

from .hashing import index_by_hash, note_hash
from .schemas import Note, NoteDelta, NoteMove, _resolve_key

# A moved note must land within this window to be paired rather than counted as
# a separate delete+insert. Beats for time, semitones for pitch.
_MOVE_START_WINDOW = 1.0  # beats
_MOVE_PITCH_WINDOW = 12  # one octave


def _distance(a: Note, b: Note) -> float | None:
    """Pairing distance between a removed and an added note, or None if too far.

    Normalized so 0 = identical position/pitch and 1 = at the window edge.
    """
    ds = abs(a.start - b.start)
    dp = abs(_resolve_key(a.key) - _resolve_key(b.key))
    if ds > _MOVE_START_WINDOW or dp > _MOVE_PITCH_WINDOW:
        return None
    return (ds / _MOVE_START_WINDOW + dp / _MOVE_PITCH_WINDOW) / 2


def diff_notes(old: list[Note], new: list[Note]) -> NoteDelta:
    """Content-addressed diff from `old` to `new`."""
    old_by_hash = index_by_hash(old)
    new_by_hash = index_by_hash(new)

    # 1. Exact matches (same hash) are unchanged - drop from both sides.
    removed_hashes = [h for h in old_by_hash if h not in new_by_hash]
    added_hashes = [h for h in new_by_hash if h not in old_by_hash]

    removed_notes = {h: old_by_hash[h] for h in removed_hashes}
    added_notes = {h: new_by_hash[h] for h in added_hashes}

    # 2/3. Greedy nearest pairing of leftovers into moves.
    candidates: list[tuple[float, str, str]] = []
    for rh, rn in removed_notes.items():
        for ah, an in added_notes.items():
            d = _distance(rn, an)
            if d is not None:
                candidates.append((d, rh, ah))
    candidates.sort(key=lambda c: c[0])  # closest first

    moved: list[NoteMove] = []
    paired_removed: set[str] = set()
    paired_added: set[str] = set()
    for d, rh, ah in candidates:
        if rh in paired_removed or ah in paired_added:
            continue
        paired_removed.add(rh)
        paired_added.add(ah)
        moved.append(NoteMove(from_hash=rh, to=added_notes[ah], confidence=round(1.0 - d, 3)))

    delta = NoteDelta(
        added=[n for h, n in added_notes.items() if h not in paired_added],
        removed=[h for h in removed_notes if h not in paired_removed],
        moved=moved,
    )
    return delta


def apply_delta(base: list[Note], delta: NoteDelta) -> list[Note]:
    """Fold one delta onto a note group, returning the new group.

    Order within the result is not meaningful (note groups are content sets);
    hashing is order-independent, so callers compare by noteset_hash, not order.
    """
    by_hash = index_by_hash(base)
    for h in delta.removed:
        by_hash.pop(h, None)
    for move in delta.moved:
        by_hash.pop(move.from_hash, None)
        by_hash[note_hash(move.to)] = move.to
    for note in delta.added:
        by_hash[note_hash(note)] = note
    return list(by_hash.values())


def fold(bootstrap: list[Note], deltas: list[NoteDelta]) -> list[Note]:
    """Reconstruct a note group from a starting snapshot and a delta series."""
    state = list(bootstrap)
    for delta in deltas:
        state = apply_delta(state, delta)
    return state


def invert(delta: NoteDelta, base: list[Note]) -> NoteDelta:
    """The delta that undoes `delta` (given the pre-image `base` for removed notes)."""
    base_by_hash = index_by_hash(base)
    return NoteDelta(
        added=[base_by_hash[h] for h in delta.removed if h in base_by_hash],
        removed=[note_hash(n) for n in delta.added],
        moved=[
            NoteMove(from_hash=note_hash(m.to), to=base_by_hash[m.from_hash], confidence=m.confidence)
            for m in delta.moved
            if m.from_hash in base_by_hash
        ],
    )
