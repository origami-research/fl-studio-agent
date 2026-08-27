from __future__ import annotations

import threading

from fl_mcp import records, schemas


def make_notes(*keys):
    return [schemas.Note(key=k, start=i, length=1) for i, k in enumerate(keys)]


def test_note_set_id_is_stable_for_identical_content():
    a = records.note_set_id(make_notes("C4", "E4"))
    b = records.note_set_id(make_notes("C4", "E4"))
    assert a == b


def test_note_set_id_differs_for_different_content():
    a = records.note_set_id(make_notes("C4", "E4"))
    b = records.note_set_id(make_notes("C4", "G4"))
    assert a != b


def test_note_set_id_uses_canonical_form_not_raw_key_spelling():
    # "C4" and MIDI 60 resolve to the same canonical note -> same id.
    a = records.note_set_id([schemas.Note(key="C4", start=0, length=1)])
    b = records.note_set_id([schemas.Note(key=60, start=0, length=1)])
    assert a == b


def test_write_stores_and_returns_a_shadow_ref(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    target = schemas.Target(pattern=0, channel=1)
    ref = store.write(target, make_notes("C4"), mode="replace")
    assert ref.provenance == "shadow"
    assert ref.stale is False
    assert store.notes_at(target)[0].key == "C4"


def test_merge_appends_replace_overwrites(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    target = schemas.Target(pattern=0, channel=1)
    store.write(target, make_notes("C4"), mode="replace")
    store.write(target, make_notes("E4"), mode="merge")
    assert len(store.notes_at(target)) == 2

    store.write(target, make_notes("G4"), mode="replace")
    notes = store.notes_at(target)
    assert len(notes) == 1
    assert notes[0].key == "G4"


def test_clear_empties_the_target(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    target = schemas.Target(pattern=0, channel=1)
    store.write(target, make_notes("C4"), mode="replace")
    store.clear(target)
    assert store.notes_at(target) == []


def test_old_note_set_stays_addressable_by_hash_after_target_moves_on(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    target = schemas.Target(pattern=0, channel=1)
    ref1 = store.write(target, make_notes("C4"), mode="replace")
    store.write(target, make_notes("G4"), mode="replace")

    assert store.ref_at(target).id != ref1.id
    old_notes = store.notes_by_id(ref1.id)
    assert old_notes is not None
    assert old_notes[0].key == "C4"


def test_mark_stale_flags_only_the_given_target(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    t1, t2 = schemas.Target(pattern=0, channel=1), schemas.Target(pattern=0, channel=2)
    store.write(t1, make_notes("C4"), mode="replace")
    store.write(t2, make_notes("D4"), mode="replace")

    store.mark_stale(t1)

    assert store.ref_at(t1).stale is True
    assert store.ref_at(t2).stale is False


def test_mark_stale_with_no_target_flags_everything(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    t1, t2 = schemas.Target(pattern=0, channel=1), schemas.Target(pattern=0, channel=2)
    store.write(t1, make_notes("C4"), mode="replace")
    store.write(t2, make_notes("D4"), mode="replace")

    store.mark_stale()

    assert store.ref_at(t1).stale is True
    assert store.ref_at(t2).stale is True


def test_persistence_round_trips_across_instances(tmp_path):
    target = schemas.Target(pattern=2, channel=3)
    store = records.ShadowStore("proj", root=tmp_path)
    store.write(target, make_notes("C4", "E4", "G4"), mode="replace", provenance="model")

    reloaded = records.ShadowStore("proj", root=tmp_path)
    ref = reloaded.ref_at(target)
    assert ref is not None
    assert ref.provenance == "model"
    notes = reloaded.notes_at(target)
    assert [n.key for n in notes] == ["C4", "E4", "G4"]


def test_get_on_unknown_target_returns_none(tmp_path):
    store = records.ShadowStore("proj", root=tmp_path)
    assert store.ref_at(schemas.Target(pattern=9, channel=9)) is None
    assert store.notes_at(schemas.Target(pattern=9, channel=9)) is None


def test_concurrent_merge_writes_to_the_same_target_do_not_lose_updates(tmp_path):
    """An MCP server dispatches tool calls concurrently (one thread per call).

    Without a lock around merge's read-then-append-then-save, two threads can
    both read the same base, append their own note, and the second save()
    silently overwrites the first thread's note even though nothing errored.
    """
    store = records.ShadowStore("proj", root=tmp_path)
    target = schemas.Target(pattern=0, channel=0)
    store.write(target, [], mode="replace")

    barrier = threading.Barrier(20)

    def writer(i: int) -> None:
        barrier.wait()
        store.write(target, make_notes(f"C{i % 4}"), mode="merge")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.notes_at(target)) == 20
