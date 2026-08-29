"""Tests for the representation layer: hashing, views (compression), diff (deltas)."""

from fl_mcp import diff, hashing, views
from fl_mcp.schemas import (
    Channel,
    Effect,
    MixerTrack,
    Note,
    NoteDelta,
    SessionState,
)

# --- hashing ---------------------------------------------------------------


def test_note_hash_stable_and_value_based():
    a = Note(key="C4", start=0, length=1)
    b = Note(key=60, start=0, length=1)  # same value, different spelling
    assert hashing.note_hash(a) == hashing.note_hash(b)


def test_note_hash_distinguishes_value():
    a = Note(key="C4", start=0, length=1)
    b = Note(key="C4", start=1, length=1)
    assert hashing.note_hash(a) != hashing.note_hash(b)


def test_noteset_hash_is_order_independent():
    n1 = Note(key="C4", start=0, length=1)
    n2 = Note(key="E4", start=1, length=1)
    assert hashing.noteset_hash([n1, n2]) == hashing.noteset_hash([n2, n1])


# --- views (compression) ---------------------------------------------------


def test_render_drops_defaults_and_none():
    ch = Channel(index=1, name="808 Kick")  # all else default
    assert views.render(ch) == {"index": 1, "name": "808 Kick"}


def test_render_keeps_non_default_fields():
    ch = Channel(index=4, name="808 Snare", volume_db=-5.2)
    out = views.render(ch)
    assert out == {"index": 4, "name": "808 Snare", "volume_db": -5.2}


def test_render_session_compacts_nested():
    state = SessionState(
        tempo_bpm=130.0,
        channels=[Channel(index=1, name="Kick"), Channel(index=2, name="Bass", pan=-0.3)],
        mixer=[MixerTrack(index=0, name="Master", effects=[Effect(slot=10, name="Emphasizer")])],
    )
    out = views.render_session(state)
    # tempo present; ppq/playing/current_pattern/source all default -> dropped.
    assert out == {
        "tempo_bpm": 130.0,
        "channels": [{"index": 1, "name": "Kick"}, {"index": 2, "name": "Bass", "pan": -0.3}],
        "mixer": [{"index": 0, "name": "Master", "effects": [{"slot": 10, "name": "Emphasizer"}]}],
    }


def test_note_view_full_when_small():
    notes = [Note(key="C4", start=0, length=1), Note(key="E4", start=1, length=0.5, velocity=0.6)]
    out = views.note_view(notes)
    assert out == [
        {"key": 60, "start": 0.0, "length": 1.0},
        {"key": 64, "start": 1.0, "length": 0.5, "velocity": 0.6},
    ]


def test_note_view_summarizes_when_large():
    notes = [Note(key=48 + (i % 24), start=i * 0.25, length=0.25) for i in range(200)]
    out = views.note_view(notes, threshold=50)
    assert isinstance(out, dict)
    assert out["count"] == 200
    assert out["low"] == "C3" and out["high"] == "B4"
    assert "id" in out and out["density"] is not None


# --- diff (deltas) ---------------------------------------------------------


def test_diff_added_and_removed():
    old = [Note(key="C4", start=0, length=1)]
    new = [Note(key="C4", start=0, length=1), Note(key="G4", start=2, length=1)]
    d = diff.diff_notes(old, new)
    assert len(d.added) == 1 and d.added[0].key == 67
    assert d.removed == [] and d.moved == []


def test_diff_pairs_a_dragged_note_as_moved():
    old = [Note(key="C4", start=0, length=1)]
    new = [Note(key="E4", start=0, length=1)]  # same spot, up a third
    d = diff.diff_notes(old, new)
    assert not d.added and not d.removed
    assert len(d.moved) == 1
    assert d.moved[0].to.key == 64
    assert 0 < d.moved[0].confidence <= 1


def test_diff_far_edit_is_delete_plus_insert_not_move():
    old = [Note(key="C2", start=0, length=1)]
    new = [Note(key="C6", start=30, length=1)]  # far in pitch and time
    d = diff.diff_notes(old, new)
    assert not d.moved
    assert len(d.added) == 1 and len(d.removed) == 1


def test_identical_groups_produce_empty_delta():
    notes = [Note(key="C4", start=0, length=1), Note(key="E4", start=1, length=1)]
    d = diff.diff_notes(notes, list(reversed(notes)))
    assert d.is_empty()


def test_fold_reconstructs_state():
    bootstrap = [Note(key="C4", start=0, length=1)]
    d1 = NoteDelta(added=[Note(key="E4", start=1, length=1)])
    d2 = NoteDelta(added=[Note(key="G4", start=2, length=1)])
    folded = diff.fold(bootstrap, [d1, d2])
    target = [*bootstrap, Note(key="E4", start=1, length=1), Note(key="G4", start=2, length=1)]
    assert hashing.noteset_hash(folded) == hashing.noteset_hash(target)


def test_apply_then_invert_is_identity():
    base = [Note(key="C4", start=0, length=1), Note(key="E4", start=1, length=1)]
    new = [Note(key="C4", start=0, length=1), Note(key="G4", start=3, length=1)]
    d = diff.diff_notes(base, new)
    forward = diff.apply_delta(base, d)
    assert hashing.noteset_hash(forward) == hashing.noteset_hash(new)
    back = diff.apply_delta(forward, diff.invert(d, base))
    assert hashing.noteset_hash(back) == hashing.noteset_hash(base)
