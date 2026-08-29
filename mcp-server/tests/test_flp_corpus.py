"""G1's fast half: differential oracle - our reader vs pyflp over the corpus.

The corpus is FL's OWN 164 bundled projects (docs/21 addendum A1), read in
place from the installed app and never copied; every test here skips when FL
is absent. The 4 fetched tests/corpus files join the set when downloaded.

Parity target (A2): tempo, channel count, channel names, note tuples on every
project pyflp reads. Measured on this machine (FL 2026, 164 files): our reader
parses 164/164 with zero crashes; pyflp parses 163 (StringError on Astes -
Bien Duro). 143/163 match exactly; the 20 divergent files fall into three
classes where the ORACLE is wrong or cannot testify, each verified from the
file's own bytes at runtime rather than trusted from a filename list:

1. Mixer-name leak (18 files): pyflp attributes a mixer SLOT's plugin name
   (e.g. 'Fruity Balance', 'Love Philter' - effects, not generators) to a rack
   channel. Byte evidence: the claimed name's text event sits after the first
   InsertID.Flags (236), i.e. inside the mixer section - 'Fruity Balance.flp'
   has it at stream offset 2925, two 236 events deep, next to the slot's
   'Fruity Wrapper' internal name. Our reader stops channel attribution at 236.
2. Fabricated note (Remix a song.flp): pyflp decodes the size-34 notes blob
   (not a multiple of 24; A9) into one garbage note - rack_channel 2817, raw
   key 12500 (pyflp documents its own key range as 0-131). We skip the blob
   with a warning.
3. Channel enumeration crash (Basic with limiter.flp): pyflp's .channels
   raises IndexError on the PRISTINE file (its display-group handling), so it
   cannot testify about channels there. parse() alone succeeding is not enough.
"""

from __future__ import annotations

from pathlib import Path

import flpkit as flp
import pytest
from flpkit import FlpError
from harness.flp_compat import parse

# pyflp spells keys like "C5"; its octave system differs from ours by design.
_PYFLP_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pyflp_key_to_midi(key: str) -> int:
    letter = key.rstrip("0123456789")
    return _PYFLP_NOTE_NAMES.index(letter) + 12 * int(key[len(letter) :])

from fl_mcp.flpaths import fl_app

_FL_DATA = fl_app() / "Contents/Resources/FL/Data"
_FETCHED = Path(__file__).resolve().parents[1] / "corpus" / "data"

requires_fl = pytest.mark.skipif(
    not _FL_DATA.is_dir(), reason="FL Studio is not installed on this machine"
)


def _corpus() -> list[Path]:
    bundled = sorted(_FL_DATA.rglob("*.flp")) if _FL_DATA.is_dir() else []
    fetched = sorted(_FETCHED.glob("*.flp")) if _FETCHED.is_dir() else []
    return bundled + [p for p in fetched if not p.name.startswith("corrupt_")]


CORPUS = _corpus()


def _oracle_parse(path: Path):
    """pyflp through the compat shim; None when the oracle itself fails."""
    try:
        return parse(path)
    except Exception:
        return None


def _channel_own_name_events(path: Path) -> list[set[str]]:
    """Every name-event string inside each channel's OWN block (before the
    mixer section) - the byte evidence for divergence class 1. Same
    attribution rule as the reader: channel 0 is implicit, New(64) switches,
    output ordered by channel index."""
    data = path.read_bytes()
    _, stream = flp._chunks(data)
    unicode_text = False
    blocks: dict[int, set[str]] = {}
    current: set[str] | None = None
    for event_id, off, size in flp._events(stream):
        payload = bytes(stream[off : off + size])
        if event_id == flp.EVENT_VERSION:
            unicode_text = flp._version_is_unicode(payload, off)
        elif event_id == flp.EVENT_CHANNEL_NEW and size == 2:
            current = blocks.setdefault(int.from_bytes(payload, "little"), set())
        elif event_id == flp.EVENT_MIXER_FLAGS:
            break
        elif event_id in (
            flp.EVENT_NAME_USER, flp.EVENT_NAME_LEGACY, flp.EVENT_NAME_INTERNAL
        ):
            if current is None:
                current = blocks.setdefault(0, set())
            current.add(flp._text(payload, unicode_text, off))
        elif current is None and flp._is_channel_scoped(event_id):
            current = blocks.setdefault(0, set())
    return [blocks[iid] for iid in sorted(blocks)]


def _has_nonconforming_notes_blob(path: Path) -> bool:
    """True when any PatternID.Notes event is not a multiple of 24 bytes -
    the byte evidence for divergence class 2."""
    data = path.read_bytes()
    _, stream = flp._chunks(data)
    return any(
        event_id == flp.EVENT_PATTERN_NOTES and size % flp.NOTE_SIZE
        for event_id, size in ((e, s) for e, _o, s in flp._events(stream))
    )


@requires_fl
@pytest.mark.parametrize(
    "path", CORPUS, ids=[str(p.relative_to(p.parents[2])) for p in CORPUS]
)
def test_reader_matches_pyflp_or_divergence_is_evidenced(path):
    try:
        ours = flp.read(path)
    except FlpError:
        # A clean typed failure is acceptable ONLY where the oracle fails too;
        # any other exception propagating out of read() fails this test (the
        # never-crash contract).
        assert _oracle_parse(path) is None, f"pyflp parses {path.name} but our reader refused"
        return

    oracle = _oracle_parse(path)
    if oracle is None:
        return  # we out-parse the oracle; the Astes test below pins the known case

    # -- tempo (strict) -------------------------------------------------------
    tempo = oracle.tempo
    if tempo is None:
        assert ours.tempo is None
    else:
        assert ours.tempo == pytest.approx(float(tempo), abs=1e-9)

    # -- channels -------------------------------------------------------------
    # The FLhd header's nChannels field is the free ground truth for channel
    # COUNT - stronger than pyflp, which drops the implicit channel 0 (the
    # G4-found bug this invariant would have caught immediately).
    header = path.read_bytes()[8:14]
    assert len(ours.channels) == int.from_bytes(header[2:4], "little")

    try:
        oracle_names = [(c.display_name or "") for c in oracle.channels]
    except Exception:
        oracle_names = None
    if oracle_names is not None and len(oracle_names) == len(ours.channels):
        own_names = _channel_own_name_events(path)
        for i, (our_channel, oracle_name) in enumerate(
            zip(ours.channels, oracle_names, strict=True)
        ):
            if our_channel.name == oracle_name:
                continue
            # Divergence class 1: allowed only when the oracle's claimed name
            # is provably foreign to the channel's own event block.
            assert oracle_name not in own_names[i], (
                f"channel {i}: ours={our_channel.name!r} oracle={oracle_name!r} "
                f"and the oracle's name IS in the channel's own events - real mismatch"
            )

    # -- notes ----------------------------------------------------------------
    try:
        oracle_notes = sorted(
            (n.position, pyflp_key_to_midi(n.key), n.rack_channel, n.length, n.velocity, n.pan)
            for pat in oracle.patterns
            for n in pat.notes
        )
    except Exception:
        return
    our_notes = sorted(
        (n.position, n.key, n.channel, n.length, n.velocity, n.pan) for n in ours.notes
    )
    if our_notes != oracle_notes:
        # Divergence class 2: pyflp inventing notes from a non-conforming blob
        # we deliberately skip. Ours must be a subset and the blob must exist.
        assert _has_nonconforming_notes_blob(path), (
            f"note mismatch without a non-conforming blob: "
            f"ours={len(our_notes)} oracle={len(oracle_notes)}"
        )
        extras = [n for n in oracle_notes if n not in our_notes]
        assert all(n in oracle_notes for n in our_notes)
        assert extras, "oracle has fewer notes than us despite the bad blob"


@requires_fl
def test_astes_bien_duro_parses_where_pyflp_cannot():
    """The 164th file (A2): pyflp dies on a utf-16 text event; our reader must
    parse it or raise FlpError - it parses, fully."""
    path = _FL_DATA / "Demo projects/Demo songs/Astes - Bien Duro.flp"
    assert _oracle_parse(path) is None, "pyflp learned to parse Astes; update A2"
    project = flp.read(path)
    assert project.tempo == 95.0
    header = path.read_bytes()[8:14]
    assert len(project.channels) == int.from_bytes(header[2:4], "little") == 308
    assert len(project.notes) > 1000


def test_corrupt_fetched_files_raise_flp_error():
    corrupt = sorted(_FETCHED.glob("corrupt_*.flp")) if _FETCHED.is_dir() else []
    if not corrupt:
        pytest.skip("corrupt corpus files not fetched")
    for path in corrupt:
        with pytest.raises(FlpError):
            flp.read(path)
