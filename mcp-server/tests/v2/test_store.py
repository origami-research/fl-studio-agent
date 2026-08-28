"""v2 store: content addressing, structured diffs, and the durable shadow score.

Ported from v1's test_records/test_actions/test_representation, rewritten to
the v2 schemas (NoteMove carries the full ``from_`` note, NoteDelta.removed is
notes not hashes, NoteSetRef has target + shadow/project provenance and no
stale flag). v1 coverage that v2 drops by design: mark_stale (no stale field),
"model" provenance, diff.fold/invert (undo is FL's own, not delta replay).
"""

from __future__ import annotations

import threading

import pytest

from fl_mcp.v2 import store as store_mod
from fl_mcp.v2.schemas import Note, NoteDelta, Target
from fl_mcp.v2.store import Store, apply_delta, diff_notes, note_hash, noteset_hash


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


def test_apply_delta_reproduces_the_new_set_and_leaves_input_untouched():
    base = [Note(key="C4", start=0, length=1), Note(key="E4", start=1, length=1)]
    new = [Note(key="C4", start=0, length=1), Note(key="G4", start=3, length=1)]
    base_before = [n.model_copy() for n in base]

    result = apply_delta(base, diff_notes(base, new))

    assert noteset_hash(result) == noteset_hash(new)
    assert base == base_before  # input list and its notes untouched


def test_apply_delta_handles_pure_add():
    base = [Note(key="C4", start=0, length=1)]
    delta = NoteDelta(added=[Note(key="E4", start=1, length=1)])
    result = apply_delta(base, delta)
    assert len(result) == 2
    assert len(base) == 1


# -- Store: shadow score ------------------------------------------------------


def test_record_write_stores_notes_and_returns_the_delta(tmp_path):
    store = Store("proj", root=tmp_path)
    delta = store.record_write(target(), make_notes("C4"), "replace", "project-write", 1)
    assert len(delta.added) == 1
    assert store.notes_at(target())[0].key == 60
    assert store.ref_at(target()).provenance == "shadow"


def test_merge_appends_replace_overwrites(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), make_notes("C4"), "replace", "live", 1)
    store.record_write(target(), make_notes("E4"), "merge", "live", 2)
    assert len(store.notes_at(target())) == 2

    store.record_write(target(), make_notes("G4"), "replace", "live", 3)
    notes = store.notes_at(target())
    assert len(notes) == 1 and notes[0].key == 67


def test_clear_via_empty_replace_empties_the_target(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), make_notes("C4"), "replace", "live", 1)
    delta = store.record_write(target(), [], "replace", "live", 2)
    assert store.notes_at(target()) == []
    assert len(delta.removed) == 1


def test_old_note_set_stays_addressable_by_hash_after_target_moves_on(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), make_notes("C4"), "replace", "live", 1)
    ref1 = store.ref_at(target())
    store.record_write(target(), make_notes("G4"), "replace", "live", 2)

    assert store.ref_at(target()).id != ref1.id
    old_notes = store.notes_by_id(ref1.id)
    assert old_notes is not None and old_notes[0].key == 60


def test_mark_ground_truth_labels_provenance_project(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), make_notes("C4"), "replace", "live", 1)
    assert store.ref_at(target()).provenance == "shadow"

    ref = store.mark_ground_truth(target(), make_notes("C4", "E4"))

    assert ref.provenance == "project"
    assert store.ref_at(target()).provenance == "project"
    assert len(store.notes_at(target())) == 2


def test_unknown_target_returns_none(tmp_path):
    store = Store("proj", root=tmp_path)
    assert store.ref_at(target(9, 9)) is None
    assert store.notes_at(target(9, 9)) is None


def test_returned_note_lists_are_copies(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), make_notes("C4"), "replace", "live", 1)
    store.notes_at(target()).append(Note(key=61, start=0, length=1))
    assert len(store.notes_at(target())) == 1


def test_persistence_round_trips_across_instances(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(2, 3), make_notes("C4", "E4", "G4"), "replace", "project-write", 7)
    store.mark_ground_truth(target(2, 4), make_notes("D4"))

    reloaded = Store("proj", root=tmp_path)
    assert [n.key for n in reloaded.notes_at(target(2, 3))] == [60, 64, 67]
    assert reloaded.ref_at(target(2, 3)).provenance == "shadow"
    assert reloaded.ref_at(target(2, 4)).provenance == "project"


def test_corrupt_shadow_file_raises_instead_of_reading_as_empty(tmp_path):
    (tmp_path / "shadow.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        Store("proj", root=tmp_path)


# -- Store: action log ---------------------------------------------------------


def test_history_starts_empty(tmp_path):
    store = Store("proj", root=tmp_path)
    assert list(store.history()) == []
    assert store.last_action() is None


def test_history_is_oldest_first_and_last_action_is_newest(tmp_path):
    store = Store("proj", root=tmp_path)
    for i in range(3):
        store.record_write(target(), make_notes(f"C{i + 2}"), "replace", "live", i)
    assert [e.id for e in store.history()] == [0, 1, 2]
    assert store.last_action().id == 2


def test_action_entry_carries_hashes_backend_mode_and_delta(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(3, 4), make_notes("C4"), "replace", "project-write", 5)

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
    store.record_write(target(), make_notes("C4"), "replace", "live", 1)
    store.record_write(target(), [], "replace", "live", 2)
    assert store.last_action().op == "clear_notes"


def test_action_log_is_append_only_across_instances(tmp_path):
    Store("proj", root=tmp_path).record_write(target(), make_notes("C4"), "replace", "live", 0)
    store2 = Store("proj", root=tmp_path)
    store2.record_write(target(), make_notes("E4"), "merge", "live", 1)
    assert [e.id for e in store2.history()] == [0, 1]


def test_action_entries_with_moves_round_trip_through_the_log(tmp_path):
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [Note(key=60, start=0, length=1)], "replace", "live", 1)
    store.record_write(target(), [Note(key=62, start=0, length=1)], "replace", "live", 2)

    entry = store.last_action()
    assert len(entry.delta.moved) == 1
    assert entry.delta.moved[0].from_.key == 60
    assert entry.delta.moved[0].to.key == 62


# -- concurrency ---------------------------------------------------------------


def test_concurrent_merge_writes_to_the_same_target_do_not_lose_updates(tmp_path):
    """MCP dispatches tool calls concurrently. Without the lock around merge's
    read-then-append-then-save, two threads can both read the same base and the
    second save silently overwrites the first thread's note."""
    store = Store("proj", root=tmp_path)
    store.record_write(target(), [], "replace", "live", 0)

    barrier = threading.Barrier(20)

    def writer(i: int) -> None:
        barrier.wait()
        store.record_write(target(), [Note(key=40 + i, start=i, length=1)], "merge", "live", i + 1)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.notes_at(target())) == 20
    assert len(list(store.history())) == 21
