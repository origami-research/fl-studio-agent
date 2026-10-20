"""The store: content hashing, structured diffs, the session's note state and
the durable action log.

Ported from v1's test_records/test_actions/test_representation, rewritten to
the v2 schemas (NoteMove carries the full ``from_`` note, NoteDelta.removed is
notes not hashes). The note state is per session by design (a later server
must not inherit claims about a reset FL); the action log is durable.
"""

from __future__ import annotations

import pytest

from fl_mcp import store as store_mod
from fl_mcp.clock import now
from fl_mcp.schemas import Note, Target
from fl_mcp.store import Store, diff_notes, note_hash, noteset_hash


def make_notes(*keys):
    return [Note(key=k, start=i, length=1) for i, k in enumerate(keys)]


def target(pattern=1, channel=0) -> Target:
    return Target(pattern=pattern, channel=channel)


# -- hashing -----------------------------------------------------------------


def test_note_hash_is_stable_and_value_based():
    a = Note(key=60, start=0, length=1)
    b = Note(key=60, start=0, length=1)
    assert note_hash(a) == note_hash(b)


def test_note_hash_distinguishes_value():
    assert note_hash(Note(key=60, start=0, length=1)) != note_hash(Note(key=61, start=0, length=1))


def test_note_hash_is_spelling_independent():
    # "C4" and MIDI 60 resolve to the same canonical note -> same hash.
    assert note_hash(Note(key="C4", start=0, length=1)) == note_hash(Note(key=60, start=0, length=1))


def test_noteset_hash_is_order_independent():
    n1, n2 = make_notes("C4", "E4")
    assert noteset_hash([n1, n2]) == noteset_hash([n2, n1])


def test_noteset_hash_differs_for_different_content():
    assert noteset_hash(make_notes("C4", "E4")) != noteset_hash(make_notes("C4", "G4"))


# -- diff --------------------------------------------------------------------


def test_diff_added_and_removed():
    old = [Note(key="C4", start=0, length=1)]
    new = [Note(key="C4", start=0, length=1), Note(key="G4", start=2, length=1)]
    d = diff_notes(old, new)
    assert len(d.added) == 1 and d.added[0].key == 67
    assert d.removed == [] and d.moved == []


def test_diff_pairs_a_dragged_note_as_moved():
    old = [Note(key="C4", start=0, length=1)]
    new = [Note(key="E4", start=0, length=1)]  # same spot, up a third
    d = diff_notes(old, new)
    assert not d.added and not d.removed
    assert len(d.moved) == 1
    assert d.moved[0].from_.key == 60
    assert d.moved[0].to.key == 64
    assert 0 < d.moved[0].confidence <= 1


def test_diff_far_edit_is_delete_plus_insert_not_move():
    old = [Note(key="C2", start=0, length=1)]
    new = [Note(key="C6", start=30, length=1)]  # far in pitch and time
    d = diff_notes(old, new)
    assert not d.moved
    assert len(d.added) == 1 and len(d.removed) == 1


def test_diff_at_the_window_edge_still_pairs_with_positive_confidence():
    # Exactly MOVE_START_WINDOW_BEATS + MOVE_PITCH_WINDOW_SEMITONES away:
    # still a move, and confidence stays > 0 (the schema demands it).
    old = [Note(key=60, start=0, length=1)]
    new = [Note(key=60 + store_mod.MOVE_PITCH_WINDOW_SEMITONES,
                start=store_mod.MOVE_START_WINDOW_BEATS, length=1)]
    d = diff_notes(old, new)
    assert len(d.moved) == 1
    assert d.moved[0].confidence > 0


def test_diff_greedy_pairing_prefers_the_closest_candidate():
    old = [Note(key=60, start=0, length=1)]
    near = Note(key=61, start=0, length=1)
    far = Note(key=65, start=0.5, length=1)
    d = diff_notes(old, [near, far])
    assert len(d.moved) == 1
    assert d.moved[0].to.key == 61
    assert len(d.added) == 1 and d.added[0].key == 65


def test_identical_groups_produce_empty_delta():
    notes = [Note(key="C4", start=0, length=1), Note(key="E4", start=1, length=1)]
    assert diff_notes(notes, list(reversed(notes))).is_empty()


# -- Store: note state ---------------------------------------------------------


def test_record_write_sets_the_state_and_returns_the_delta(tmp_path):
    store = Store("proj", root=tmp_path)
    delta = store.record_write(target(), [], make_notes("C4"), "replace", "project-write", 1)
    assert len(delta.added) == 1
    assert store.notes_at(target())[0].key == 60


def test_the_recorded_state_is_what_the_caller_confirmed(tmp_path):
    """The store never re-derives a merge: the lane hands it FL's confirmed state."""
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [], make_notes("C4"), "replace", "bridge", 1)
    store.record_write(target(), make_notes("C4"), make_notes("C4", "E4"), "merge", "bridge", 2)
    assert [n.key for n in store.notes_at(target())] == [60, 64]


def test_clear_via_empty_replace_empties_the_target(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [], make_notes("C4"), "replace", "live", 1)
    delta = store.record_write(target(), make_notes("C4"), [], "replace", "live", 2)
    assert store.notes_at(target()) == []
    assert len(delta.removed) == 1


def test_unknown_target_returns_none(tmp_path):
    store = Store("proj", root=tmp_path)
    assert store.notes_at(target(9, 9)) is None


def test_written_lists_every_target_this_session_wrote(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(1, 0), [], make_notes("C4"), "replace", "bridge", 1)
    store.record_write(target(2, 3), [], [], "replace", "bridge", 2)
    assert {key: [n.key for n in notes] for key, notes in store.written().items()} == {
        (1, 0): [60],
        (2, 3): [],
    }


def test_returned_note_lists_are_copies(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [], make_notes("C4"), "replace", "live", 1)
    store.notes_at(target()).append(Note(key=61, start=0, length=1))
    store.written()[(1, 0)].append(Note(key=61, start=0, length=1))
    assert len(store.notes_at(target())) == 1


def test_a_later_save_supersedes_the_records_it_postdates(tmp_path):
    """A save of FL's live state holds everything FL had then, so for a record
    older than that save the saved project is the newer evidence."""
    store = Store("proj", root=tmp_path)
    store.record_write(target(1, 0), [], make_notes("C4"), "replace", "bridge", 1)
    saved = now()
    store.record_write(target(2, 0), [], make_notes("E4"), "replace", "bridge", 2)

    assert store.notes_at(target(1, 0), saved) is None
    assert [n.key for n in store.notes_at(target(2, 0), saved)] == [64]
    assert set(store.written(saved)) == {(2, 0)}
    assert set(store.written()) == {(1, 0), (2, 0)}


def test_note_state_is_not_inherited_by_a_later_store(tmp_path):
    """A later server drives an FL that may have been reset since: it must not
    present an earlier session's writes as what FL has now."""
    Store("proj", root=tmp_path).record_write(target(), [], make_notes("C4"), "replace", "bridge", 1)
    assert Store("proj", root=tmp_path).notes_at(target()) is None


def test_state_is_set_even_when_the_action_log_cannot_be_written(tmp_path):
    """The write already landed in FL: a failing log append must not leave reads stale."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    store = Store("proj", root=blocker)
    with pytest.raises(OSError):
        store.record_write(target(), [], make_notes("C4"), "replace", "bridge", 1)
    assert [n.key for n in store.notes_at(target())] == [60]


# -- Store: action log ---------------------------------------------------------


def test_history_starts_empty(tmp_path):
    store = Store("proj", root=tmp_path)
    assert list(store.history()) == []
    assert store.last_action() is None


def test_history_is_oldest_first_and_last_action_is_newest(tmp_path):
    store = Store("proj", root=tmp_path)
    for i in range(3):
        store.record_write(target(), [], make_notes(f"C{i + 2}"), "replace", "live", i)
    assert [e.id for e in store.history()] == [0, 1, 2]
    assert store.last_action().id == 2


def test_action_entry_carries_hashes_backend_mode_and_delta(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(3, 4), [], make_notes("C4"), "replace", "project-write", 5)

    (entry,) = store.history()

    assert entry.op == "add_notes"
    assert entry.target == target(3, 4)
    assert entry.mode == "replace"
    assert entry.backend == "project-write"
    assert entry.before_hash == noteset_hash([])
    assert entry.after_hash == noteset_hash(make_notes("C4"))
    assert len(entry.delta.added) == 1
    assert "1 notes at 3:4" in entry.summary


def test_clear_is_logged_as_clear_notes(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [], make_notes("C4"), "replace", "live", 1)
    store.record_write(target(), make_notes("C4"), [], "replace", "live", 2)
    assert store.last_action().op == "clear_notes"


def test_action_log_is_append_only_across_instances(tmp_path):
    Store("proj", root=tmp_path).record_write(target(), [], make_notes("C4"), "replace", "live", 0)
    store2 = Store("proj", root=tmp_path)
    store2.record_write(target(), [], make_notes("E4"), "merge", "live", 1)
    assert [e.id for e in store2.history()] == [0, 1]


def test_action_entries_with_moves_round_trip_through_the_log(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [], [Note(key=60, start=0, length=1)], "replace", "live", 1)
    store.record_write(target(), [Note(key=60, start=0, length=1)], [Note(key=62, start=0, length=1)], "replace", "live", 2)

    entry = store.last_action()
    assert len(entry.delta.moved) == 1
    assert entry.delta.moved[0].from_.key == 60
    assert entry.delta.moved[0].to.key == 62
