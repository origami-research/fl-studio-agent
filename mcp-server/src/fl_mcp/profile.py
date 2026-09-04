"""The capability profile: every version-specific FL fact, in one loadable artifact.

One discovery run against one FL install emits one JSON document (docs/research/
r4-streamline-and-audit.md §A1) describing that exact build: struct layouts read
from FL's own RTTI, the dispatcher surface, the live-probe verdicts, the file
format facts. This module is the runtime loader for that document, and the
single home the hardcoded constants migrate into (docs/27 §4).

Loader precedence (each step logged when it loses):

    FLMCP_PROFILE env var           - explicit override, fails loudly if bad
    <spool>/profile.json            - the per-install profile discovery wrote
    packaged profiles/ matching     - the checked-in baseline for the detected
      the installed FL version        FL version
    BUILTIN_FALLBACK                - the constants compiled into this tree
                                      (gathered from bridge_core.h,
                                      probe_safety.h, flpaths.py, make_fst.py,
                                      flpkit), with a logged warning naming
                                      every fact that fell back

Everything is a frozen dataclass: a profile is evidence, not state.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from fl_mcp.fl import paths as flpaths

log = logging.getLogger(__name__)

PROFILE_VERSION = 1
PROFILE_FILENAME = "profile.json"


# --- the facts ---------------------------------------------------------------


@dataclass(frozen=True)
class ProfileMeta:
    fl_version: str  # "26.1.5"
    fl_build: int  # 5384
    arch: str  # "arm64"
    os: str  # "macOS 26.5.1"
    generated: str  # ISO-8601 timestamp of the discovery run
    engine_dylib_sha256: str  # of the shipped (fat) FLEngine_x64.dylib
    sources: tuple[str, ...]  # e.g. ("rtti", "vmt", "live-probe")


@dataclass(frozen=True)
class UnsafeFact:
    """A dispatcher id that must never be called blind, with its evidence."""

    hazard: str  # "crashes_host" | "hangs_host"
    evidence: str  # pointer into the evidence tree


@dataclass(frozen=True)
class DispatcherFacts:
    vmt_first_method_offset: int  # 200: the FPC VMT header size
    host_dispatcher_addr: int  # TFruityHost.Dispatcher in the arm64 slice
    jump_table_addr: int  # the id jump table the dispatcher indexes
    handled_max_id: int  # ids 0..this are the whole handled surface
    inert_in_range: tuple[int, ...]  # in-range ids that hit the default branch
    unsafe: dict[int, UnsafeFact] = field(default_factory=dict)


@dataclass(frozen=True)
class StructField:
    name: str  # SDK label (RTTI carries no field names; order is declaration order)
    offset: int  # scraped from FL's record RTTI
    type: str  # RTTI type name: "LongInt" | "Single" | "SmallInt" | "PChar" | "array"


@dataclass(frozen=True)
class StructLayout:
    size: int  # RecSize from the RTTI (for TNotesParams: with one note)
    fields: tuple[StructField, ...]


@dataclass(frozen=True)
class GetterFact:
    id: int
    name: str
    convention: str  # "guarded_buffer" | "index_only" | "returns_pointer"
    median_ns: int


@dataclass(frozen=True)
class FlpFacts:
    event_size_overrides: dict[int, int] = field(default_factory=dict)
    playlist_stride: int = 88
    note_flags_default: int = 0x4000


@dataclass(frozen=True)
class FstFieldFact:
    offset: int
    value: int


@dataclass(frozen=True)
class FstFacts:
    version_string: str  # "11.5.5"
    wrapper_bytes: int  # 52-byte plugin-wrapper record (event 212)
    wrapper_kind: FstFieldFact  # 2 @ offset 8 marks a third-party native plugin
    wrapper_generator: FstFieldFact  # 16 @ offset 16 marks a generator (0 = effect)


@dataclass(frozen=True)
class ProfileCapabilities:
    """The machine form of the live-probe verdicts (PROBE_VERDICT_2026-08-30)."""

    note_readback_live: bool
    pattern_targeting_live: bool
    channel_targeting_live: bool
    score_edit_push: bool


@dataclass(frozen=True)
class TickFacts:
    mean_ms: float
    max_ms: float
    frontmost_required: bool


@dataclass(frozen=True)
class CapabilityProfile:
    meta: ProfileMeta
    dispatcher: DispatcherFacts
    structs: dict[str, StructLayout]
    getters: tuple[GetterFact, ...]
    push_events: frozenset[str]
    flp: FlpFacts
    fst: FstFacts
    ppq_default: int
    capabilities: ProfileCapabilities
    tick: TickFacts
    spool_roots: tuple[str, ...]


# --- JSON <-> dataclass ------------------------------------------------------


class ProfileError(ValueError):
    """A profile document that cannot be trusted: unreadable, wrong version, malformed."""


def _addr(value: object) -> int:
    """Addresses are hex strings in JSON ("0x427bc0") and ints in memory."""
    if isinstance(value, str):
        return int(value, 16)
    if isinstance(value, int):
        return value
    raise ProfileError(f"not an address: {value!r}")


def parse_profile(doc: dict) -> CapabilityProfile:
    """Validate + freeze one profile document (the §A1 schema)."""
    version = doc.get("profile_version") if isinstance(doc, dict) else None
    if version != PROFILE_VERSION:
        raise ProfileError(f"profile_version {version!r} != {PROFILE_VERSION}")
    try:
        meta = ProfileMeta(
            fl_version=str(doc["meta"]["fl_version"]),
            fl_build=int(doc["meta"]["fl_build"]),
            arch=str(doc["meta"]["arch"]),
            os=str(doc["meta"]["os"]),
            generated=str(doc["meta"]["generated"]),
            engine_dylib_sha256=str(doc["meta"]["engine_dylib_sha256"]),
            sources=tuple(doc["meta"]["sources"]),
        )
        d = doc["dispatcher"]
        dispatcher = DispatcherFacts(
            vmt_first_method_offset=int(d["vmt_first_method_offset"]),
            host_dispatcher_addr=_addr(d["host_dispatcher_addr"]),
            jump_table_addr=_addr(d["jump_table_addr"]),
            handled_max_id=int(d["handled_max_id"]),
            inert_in_range=tuple(int(i) for i in d["inert_in_range"]),
            unsafe={
                int(u["id"]): UnsafeFact(hazard=str(u["hazard"]), evidence=str(u["evidence"]))
                for u in d["unsafe"]
            },
        )
        structs = {
            name: StructLayout(
                size=int(s["size"]),
                fields=tuple(
                    StructField(name=str(f["name"]), offset=int(f["offset"]), type=str(f["type"]))
                    for f in s["fields"]
                ),
            )
            for name, s in doc["structs"].items()
        }
        getters = tuple(
            GetterFact(
                id=int(g["id"]),
                name=str(g["name"]),
                convention=str(g["convention"]),
                median_ns=int(g["median_ns"]),
            )
            for g in doc["getters"]
        )
        flp = FlpFacts(
            event_size_overrides={int(k): int(v) for k, v in doc["flp"]["event_size_overrides"].items()},
            playlist_stride=int(doc["flp"]["playlist_stride"]),
            note_flags_default=int(doc["flp"]["note_flags_default"]),
        )
        fst = FstFacts(
            version_string=str(doc["fst"]["version_string"]),
            wrapper_bytes=int(doc["fst"]["wrapper_bytes"]),
            wrapper_kind=FstFieldFact(
                offset=int(doc["fst"]["wrapper_kind"]["offset"]),
                value=int(doc["fst"]["wrapper_kind"]["value"]),
            ),
            wrapper_generator=FstFieldFact(
                offset=int(doc["fst"]["wrapper_generator"]["offset"]),
                value=int(doc["fst"]["wrapper_generator"]["value"]),
            ),
        )
        capabilities = ProfileCapabilities(
            note_readback_live=bool(doc["capabilities"]["note_readback_live"]),
            pattern_targeting_live=bool(doc["capabilities"]["pattern_targeting_live"]),
            channel_targeting_live=bool(doc["capabilities"]["channel_targeting_live"]),
            score_edit_push=bool(doc["capabilities"]["score_edit_push"]),
        )
        tick = TickFacts(
            mean_ms=float(doc["tick"]["mean_ms"]),
            max_ms=float(doc["tick"]["max_ms"]),
            frontmost_required=bool(doc["tick"]["frontmost_required"]),
        )
        return CapabilityProfile(
            meta=meta,
            dispatcher=dispatcher,
            structs=structs,
            getters=getters,
            push_events=frozenset(doc["push_events"]),
            flp=flp,
            fst=fst,
            ppq_default=int(doc["ppq"]["default"]),
            capabilities=capabilities,
            tick=tick,
            spool_roots=tuple(doc["spool_roots"]),
        )
    except ProfileError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfileError(f"malformed profile document: {exc!r}") from exc


def profile_to_dict(profile: CapabilityProfile) -> dict:
    """The inverse of parse_profile: the §A1 JSON document (round-trip exact)."""
    return {
        "profile_version": PROFILE_VERSION,
        "meta": {
            "fl_version": profile.meta.fl_version,
            "fl_build": profile.meta.fl_build,
            "arch": profile.meta.arch,
            "os": profile.meta.os,
            "generated": profile.meta.generated,
            "engine_dylib_sha256": profile.meta.engine_dylib_sha256,
            "sources": list(profile.meta.sources),
        },
        "ppq": {"source": "flp_header_u16_at_4", "default": profile.ppq_default},
        "dispatcher": {
            "vmt_first_method_offset": profile.dispatcher.vmt_first_method_offset,
            "host_dispatcher_addr": hex(profile.dispatcher.host_dispatcher_addr),
            "jump_table_addr": hex(profile.dispatcher.jump_table_addr),
            "handled_max_id": profile.dispatcher.handled_max_id,
            "inert_in_range": list(profile.dispatcher.inert_in_range),
            "unsafe": [
                {"id": uid, "hazard": u.hazard, "evidence": u.evidence}
                for uid, u in sorted(profile.dispatcher.unsafe.items())
            ],
        },
        "getters": [
            {"id": g.id, "name": g.name, "convention": g.convention, "median_ns": g.median_ns}
            for g in profile.getters
        ],
        "push_events": sorted(profile.push_events),
        "tick": {
            "mean_ms": profile.tick.mean_ms,
            "max_ms": profile.tick.max_ms,
            "frontmost_required": profile.tick.frontmost_required,
        },
        "structs": {
            name: {
                "size": s.size,
                "fields": [{"name": f.name, "offset": f.offset, "type": f.type} for f in s.fields],
            }
            for name, s in profile.structs.items()
        },
        "flp": {
            "event_size_overrides": {str(k): v for k, v in profile.flp.event_size_overrides.items()},
            "playlist_stride": profile.flp.playlist_stride,
            "note_flags_default": profile.flp.note_flags_default,
        },
        "fst": {
            "version_string": profile.fst.version_string,
            "wrapper_bytes": profile.fst.wrapper_bytes,
            "wrapper_kind": {
                "offset": profile.fst.wrapper_kind.offset,
                "value": profile.fst.wrapper_kind.value,
            },
            "wrapper_generator": {
                "offset": profile.fst.wrapper_generator.offset,
                "value": profile.fst.wrapper_generator.value,
            },
        },
        "spool_roots": list(profile.spool_roots),
        "capabilities": {
            "note_readback_live": profile.capabilities.note_readback_live,
            "pattern_targeting_live": profile.capabilities.pattern_targeting_live,
            "channel_targeting_live": profile.capabilities.channel_targeting_live,
            "score_edit_push": profile.capabilities.score_edit_push,
        },
    }


# --- BUILTIN_FALLBACK: the constants compiled into this tree -----------------
#
# Verbatim from where each fact is hardcoded today; the drift-alarm test
# (test_profile.py::test_builtin_fallback_matches_packaged) pins this block
# against the packaged FL 2026 profile until the source constants are deleted.
# When load_profile() lands here, none of these facts were verified against the
# INSTALLED FL - that is what the warning says.

_FALLBACK_FACTS = {
    # fact name -> (value summary, where the constant lives today)
    "dispatcher.handled_max_id": ("109", "native/abi/fl_dispatch_ledger.h kHandledMaxId"),
    "dispatcher.unsafe": ("ids 94 (crashes), 99 (hangs)", "native/abi/fl_dispatch_ledger.h kLedger"),
    "dispatcher.vmt_first_method_offset": ("200", "native/abi/fl_host_abi.h kFpcVmtHeaderBytes"),
    "structs.TNoteParams/TNotesParams": ("32/52 bytes, Vol=Single@12", "native/bridge/src/bridge_core.cpp static_asserts"),
    "ppq.default": ("96", "native/bridge/src/bridge_core.h kDefaultPPQ"),
    "flp.event_size_overrides": ("{172: 1}", "flpkit _EVENT_SIZE_OVERRIDES"),
    "flp.playlist_stride": ("88", "flpkit stride detector's FL 2026 answer"),
    "flp.note_flags_default": ("0x4000", "flpkit NOTE_FLAGS_DEFAULT"),
    "fst": ("11.5.5, 52-byte wrapper", "git 2ad0d74: native/probe/make_fst.py"),
    "spool_roots": ("Piano roll scripts[/FLMCP]", "fl_mcp/fl/paths.py SPOOL_SUBDIRS"),
    "capabilities+tick": ("probe verdicts of 2026-08-30", "native/probe/PROBE_VERDICT_2026-08-30.md"),
}

_SDK_NOTE_FIELDS = (
    # (name, offset, rtti type) - offsets/types as FL 2026's own record RTTI
    # states them; names are the SDK declaration-order labels (fp_plugclass.h).
    ("Position", 0, "LongInt"),
    ("Length", 4, "LongInt"),
    ("Pan", 8, "LongInt"),
    ("Vol", 12, "Single"),  # the header says int; FL's RTTI says Single (BRIDGE_FINDINGS.md)
    ("Note", 16, "SmallInt"),
    ("Color", 18, "SmallInt"),
    ("Pitch", 20, "LongInt"),
    ("FCut", 24, "Single"),
    ("FRes", 28, "Single"),
)

_SDK_NOTES_FIELDS = (
    ("Target", 0, "LongInt"),
    ("Flags", 4, "LongInt"),
    ("PatNum", 8, "LongInt"),
    ("ChanNum", 12, "LongInt"),
    ("Count", 16, "LongInt"),
    ("NoteParams", 20, "array"),
)

_SDK_PLUGINFO_FIELDS = (
    ("SDKVersion", 0, "LongInt"),
    ("LongName", 4, "PChar"),
    ("ShortName", 12, "PChar"),
    ("Flags", 20, "LongInt"),
    ("NumParams", 24, "LongInt"),
    ("DefPoly", 28, "LongInt"),
    ("NumOutCtrls", 32, "LongInt"),
    ("NumOutVoices", 36, "LongInt"),
    ("Reserved", 40, "array"),
)


def _layout(size: int, triples: tuple[tuple[str, int, str], ...]) -> StructLayout:
    return StructLayout(size=size, fields=tuple(StructField(n, o, t) for n, o, t in triples))


BUILTIN_FALLBACK = CapabilityProfile(
    meta=ProfileMeta(
        fl_version="26.1.5",
        fl_build=5384,
        arch="arm64",
        os="macOS 26.5.1",
        generated="2026-08-30T00:00:00Z",
        engine_dylib_sha256="68f08f4b533ce2c9b74ba118e040f3c3ae7ec647fb3bb6835b89df9cd25377fb",
        sources=("builtin-fallback",),
    ),
    dispatcher=DispatcherFacts(
        vmt_first_method_offset=200,
        host_dispatcher_addr=0x427BC0,
        jump_table_addr=0x42DA00,
        handled_max_id=109,
        inert_in_range=(39, 62, 63, 64, 102),
        unsafe={
            94: UnsafeFact(
                hazard="crashes_host",
                evidence="native/probe/evidence-2026-08-30/crash-id94.log",
            ),
            99: UnsafeFact(
                hazard="hangs_host",
                evidence="native/probe/PROBE_VERDICT_2026-08-30.md",
            ),
        },
    ),
    structs={
        "TNoteParams": _layout(32, _SDK_NOTE_FIELDS),
        "TNotesParams": _layout(52, _SDK_NOTES_FIELDS),
        "TFruityPlugInfo": _layout(160, _SDK_PLUGINFO_FIELDS),
    },
    getters=(
        GetterFact(36, "FHD_GetMixingTime", "guarded_buffer", 0),
        GetterFact(37, "FHD_GetPlaybackTime", "guarded_buffer", 0),
        GetterFact(38, "FHD_GetSelTime", "guarded_buffer", 0),
        GetterFact(39, "FHD_GetTimeMul", "index_only", 0),
        GetterFact(61, "FHD_GetProjectInfo", "returns_pointer", 0),
        GetterFact(76, "FHD_GetContextInfo", "index_only", 0),
        GetterFact(84, "FHD_GetColorRGBValue", "index_only", 0),
    ),
    push_events=frozenset(
        {
            "FPD_SetPlaying",
            "FPD_SongPosChanged",
            "FPD_SetFocus",
            "FPD_ContextInfoChanged",
            "FPD_RoutingChanged",
            "FPD_ProjLoaded",
            "FPD_PluginLoaded",
            "FPD_ColorsHaveChanged",
            "FPD_SetTimeSig",
            "FPD_SetSampleRate",
            "FPD_SetBlockSize",
        }
    ),
    flp=FlpFacts(event_size_overrides={172: 1}, playlist_stride=88, note_flags_default=0x4000),
    fst=FstFacts(
        version_string="11.5.5",
        wrapper_bytes=52,
        wrapper_kind=FstFieldFact(offset=8, value=2),
        wrapper_generator=FstFieldFact(offset=16, value=16),
    ),
    ppq_default=96,
    capabilities=ProfileCapabilities(
        note_readback_live=False,
        pattern_targeting_live=True,
        channel_targeting_live=True,
        score_edit_push=False,
    ),
    tick=TickFacts(mean_ms=8.7, max_ms=30.5, frontmost_required=True),
    spool_roots=flpaths.SPOOL_SUBDIRS,
)


# --- the loader --------------------------------------------------------------


def _load_file(path: Path) -> CapabilityProfile:
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read profile {path}: {exc}") from exc
    return parse_profile(doc)


def _spool_profile_path() -> Path | None:
    """<spool>/profile.json in the preferred existing spool root, if present."""
    for settings in flpaths.fl_settings_dirs():
        for sub in flpaths.SPOOL_SUBDIRS:
            candidate = settings / sub / PROFILE_FILENAME
            if candidate.is_file():
                return candidate
    return None


def detect_fl_version() -> tuple[str, int] | None:
    """(fl_version, fl_build) of the installed FL app, from its Info.plist.

    CFBundleShortVersionString is "26.1.5.5384": three version components and
    the build number. Returns None when there is no readable FL install -
    never guesses.
    """
    plist = flpaths.fl_app() / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as fh:
            doc = plistlib.load(fh)
        parts = str(doc["CFBundleShortVersionString"]).split(".")
        return ".".join(parts[:3]), int(parts[3])
    except (OSError, KeyError, ValueError, IndexError, plistlib.InvalidFileException):
        return None


def packaged_profiles() -> dict[str, dict]:
    """All checked-in profile documents, keyed by resource filename."""
    out: dict[str, dict] = {}
    root = resources.files(__package__) / "profiles"
    for entry in root.iterdir():
        if entry.name.endswith(".json"):
            out[entry.name] = json.loads(entry.read_text())
    return out


def _packaged_match(version: str, build: int) -> CapabilityProfile | None:
    for name, doc in sorted(packaged_profiles().items()):
        try:
            profile = parse_profile(doc)
        except ProfileError as exc:
            log.warning("packaged profile %s is malformed, skipping: %s", name, exc)
            continue
        if profile.meta.fl_version == version and profile.meta.fl_build == build:
            log.info("capability profile: packaged %s (FL %s build %d)", name, version, build)
            return profile
    return None


def load_profile() -> CapabilityProfile:
    """FLMCP_PROFILE env > <spool>/profile.json > packaged profile matching the
    installed FL version > BUILTIN_FALLBACK with a warning naming each fallback."""
    override = os.environ.get("FLMCP_PROFILE")
    if override:
        # An explicit override that does not load is an error, never a fallback:
        # the operator asked for THIS profile.
        profile = _load_file(Path(override))
        log.info("capability profile: FLMCP_PROFILE=%s", override)
        return profile

    spool_path = _spool_profile_path()
    if spool_path is not None:
        try:
            profile = _load_file(spool_path)
        except ProfileError as exc:
            log.warning("spool profile %s unusable (%s); trying packaged profiles", spool_path, exc)
        else:
            log.info("capability profile: spool %s", spool_path)
            return profile

    detected = detect_fl_version()
    if detected is not None:
        profile = _packaged_match(*detected)
        if profile is not None:
            return profile

    for fact, (value, home) in _FALLBACK_FACTS.items():
        log.warning("capability profile fallback: %s = %s (hardcoded in %s, unverified on this install)",
                    fact, value, home)
    log.warning(
        "capability profile: BUILTIN_FALLBACK (FL %s build %d constants); run discovery to generate "
        "a real profile for this install",
        BUILTIN_FALLBACK.meta.fl_version,
        BUILTIN_FALLBACK.meta.fl_build,
    )
    return BUILTIN_FALLBACK


__all__ = [
    "BUILTIN_FALLBACK",
    "CapabilityProfile",
    "DispatcherFacts",
    "FlpFacts",
    "FstFacts",
    "FstFieldFact",
    "GetterFact",
    "ProfileCapabilities",
    "ProfileError",
    "ProfileMeta",
    "StructField",
    "StructLayout",
    "TickFacts",
    "UnsafeFact",
    "detect_fl_version",
    "load_profile",
    "packaged_profiles",
    "parse_profile",
    "profile_to_dict",
]
