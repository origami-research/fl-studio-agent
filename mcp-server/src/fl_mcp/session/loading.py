"""Load a stock sample or preset, or audio the model made, into a channel, through the Bridge.

FL's documented host call ``FHD_LoadInChannel`` (plugin SDK id 43) sends a file
to the selected channels; the Bridge's ``load_in_channel`` command makes it with
a file name the Bridge owns (native/bridge/BRIDGE_FINDINGS.md). FL loads into
every selected channel, so the channel is selected alone over the control wire
and FL's selection is read back before the command.

What FL does with the file, live on FL 2026 (docs/research/load-sample-preset-live-2026-11-02.md):

- a sample loads into a sampler channel, which FL renames to the sample's name
  (``"808 Cowbell"``, or ``"808 Kick #2"`` when another channel has that name);
  into a plugin channel it only renames it, so it is refused before FL is asked;
- an instrument preset (``Plugin presets/Generators/<plugin>/...``) puts that
  plugin on the channel, replacing its instrument (a sampler too), and FL names
  the channel after the preset (both are checked);
- an effect preset (``Plugin presets/Effects/<plugin>/...``) adds that effect to
  the next free slot of the mixer track the channel plays through.

Each is confirmed by FL's readback: the channel's type, name and plugin, and
for an effect the mixer track's slots before and after.

Audio the model made (a chop, a stretched loop) arrives as the file's bytes, never
as a path: the server does not open files in the kernel's workspace, where a planted
link could point anywhere. The server decodes the bytes and writes a WAV of its own
under the engine's data folder, named after the model's file so FL names the channel
after it, and loads that file like a stock sample.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from fl_mcp import errors
from fl_mcp import sounds as stock_sounds
from fl_mcp.audio import decode_audio
from fl_mcp.errors import WriteRejected
from fl_mcp.fl.paths import app_data_root
from fl_mcp.fl.process import fl_process_running
from fl_mcp.schemas import ChannelSound, LoadKind, MixerEffect

from .controls import GENERATOR_SLOT, _live_int, _live_read, _live_write
from .notes import select_only
from .routing import MIXER_SLOTS

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

NOTHING_LOADED = "Nothing was loaded."
# channels.getChannelType's answer for a sampler (FL's CT_Sampler).
CT_SAMPLER = 0
# FL acknowledges after it loaded the file; a plugin took up to 3.2 s live.
LOAD_RESPONSE_TIMEOUT_SECONDS = 30.0
# What a Bridge built before load_in_channel answers it with (bridge_core.cpp, the unknown-op refusal).
OUTDATED_PLUGIN = "unknown op: load_in_channel"

# A file name FL can name a channel after, on any OS: no folders, drive or stream
# separators, wildcards or control characters, and not a Windows device name.
_SAMPLE_NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{1,100}")
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(10)), *(f"LPT{n}" for n in range(10))}
)

Role = Literal["sample", "instrument", "effect"]


def _library_file(sound: str, kind: LoadKind) -> tuple[Path, Role, str | None]:
    """The library file, what loading it does, and its plugin; checked before FL is touched."""
    lib = stock_sounds.library()
    if not lib.stock.is_dir():
        raise errors.stock_sounds_missing(lib.stock)
    try:
        path = stock_sounds.resolve(lib, sound, kind)
        if kind == "sample":
            return path, "sample", None
        role, plugin = stock_sounds.preset_role(sound)
    except ValueError as exc:
        raise errors.invalid_argument(f"fl.load_{kind}", str(exc)) from None
    return path, role, plugin


def _require_bridge(session: FLSession) -> None:
    """Only the Bridge can load a file into a channel; refuse before selecting anything."""
    caps = session.caps
    if caps.bridge_live and session.breaker.admit(caps.bridge_heartbeat_at):
        return
    raise errors.ToolError(
        "note_plane_unavailable",
        f"FL Studio cannot load a sound right now. {NOTHING_LOADED}",
        "FL may be busy or still starting. Retry in a moment.",
        retryable=True,
    )


def _instrument(session: FLSession, channel: int) -> tuple[str, str | None, bool]:
    """The channel's name, its instrument plugin (None when it has none), and whether it is a sampler."""
    name = str(_live_read(session, "channels.getChannelName", [channel]))
    if _live_int(session, "channels.getChannelType", [channel]) == CT_SAMPLER:
        return name, None, True
    if not _live_read(session, "plugins.isValid", [channel, GENERATOR_SLOT]):
        return name, None, False
    return name, str(_live_read(session, "plugins.getPluginName", [channel, GENERATOR_SLOT, False])), False


def _effects(session: FLSession, track: int) -> list[str | None]:
    """Each effect slot of a mixer track: its plugin's name, or None when empty."""
    return [
        str(_live_read(session, "plugins.getPluginName", [track, slot, False]))
        if _live_read(session, "mixer.isTrackPluginValid", [track, slot])
        else None
        for slot in range(MIXER_SLOTS)
    ]


def _send_load(session: FLSession, path: Path, channel: int) -> None:
    """One load_in_channel command; a refusal or a timeout raises, and the breaker learns either way."""
    with session._transport_use():
        command = session.transport.send("load_in_channel", path=str(path))
        try:
            response = session.transport.await_response(command, timeout=LOAD_RESPONSE_TIMEOUT_SECONDS)
        except errors.ToolError:
            if fl_process_running():
                session.breaker.record_soft_failure()
            else:
                session.breaker.record_hard_failure(f"load_in_channel command {command.id} unacked; FL not running")
            raise
    if not response.ok:
        session.breaker.record_soft_failure()
        # The Bridge's own words stay in the log: they name internals the model cannot act on.
        log.warning("the Bridge refused load_in_channel %d: %s", command.id, response.err)
        if OUTDATED_PLUGIN in (response.err or ""):
            raise WriteRejected(
                f"FL Studio could not load {path.name}: the FL plugin this tool installs is older than this "
                f"tool, so it cannot load sounds. {NOTHING_LOADED}",
                fix="Ask the user to update the tool's FL plugin and restart FL Studio.",
                retryable=False,
            )
        raise WriteRejected(
            f"FL Studio refused to load {path.name} into channel {channel}. {NOTHING_LOADED}",
            fix="Retry; if it refuses again, pick another sound from fl.sounds().",
        )
    session.breaker.record_success()


def _named_after(name: str, stem: str) -> bool:
    """FL names a channel after what it loaded, numbering it when another channel has that name."""
    return re.fullmatch(re.escape(stem) + r"( #\d+)?", name) is not None


def _same_plugin(fl_name: str | None, folder: str | None) -> bool:
    """Whether FL's name for a channel's plugin is the plugin of a preset folder. FL's names differ
    from its folders in case ("Fruity parametric EQ 2") and may add a word: SimSynth's presets
    load as "SimSynth Live" (FL 26.1.6.5639, all 73 refused before this)."""
    if fl_name is None or folder is None:
        return False
    fl_name, folder = fl_name.casefold(), folder.casefold()
    return fl_name == folder or fl_name.startswith(folder + " ")


def _ready(session: FLSession, channel: int, role: Role) -> tuple[int, list[str | None]] | None:
    """Refuse a load FL would not do, before anything is selected. For an effect,
    the mixer track it will land on and that track's slots now."""
    if role == "sample":
        name, instrument, sampler = _instrument(session, channel)
        if not sampler:
            raise errors.invalid_argument(
                "fl.load_sample",
                f"channel {channel} ({name}) plays {instrument or 'no instrument'}, not samples; load a sample "
                f"into a sampler channel, or change this channel's instrument with fl.load_preset",
            )
        return None
    if role == "effect":
        track = _live_int(session, "channels.getTargetFxTrack", [channel])
        slots = _effects(session, track)
        if None not in slots:
            raise errors.invalid_argument(
                "fl.load_preset", f"mixer track {track}, which channel {channel} plays through, has no free effect slot"
            )
        return track, slots
    return None


def _confirm(
    session: FLSession,
    loaded: ChannelSound,
    path: Path,
    role: Role,
    plugin: str | None,
    effect_track: tuple[int, list[str | None]] | None,
    sampler: bool,
) -> ChannelSound:
    """Raise unless FL's readback shows the load; return it with the effect FL added, if any."""
    channel = loaded.channel
    if role == "sample" and not (sampler and _named_after(loaded.name, path.stem)):
        raise WriteRejected(
            f"Channel {channel} shows {loaded.name!r} after loading {path.name}, so FL did not confirm the sample.",
            fix="Load it again; if FL keeps the old sound, pick another sampler channel.",
        )
    # FL names the channel after an instrument preset (live), so the name is checked too: the plugin
    # alone cannot tell a load from nothing when the channel already plays that plugin.
    if role == "instrument" and not (_same_plugin(loaded.plugin, plugin) and _named_after(loaded.name, path.stem)):
        raise WriteRejected(
            f"Channel {channel} plays {loaded.plugin or 'no plugin'} as {loaded.name!r} after loading "
            f"{path.name}, so FL did not confirm the preset.",
            fix="Load the preset again, or pick another preset from fl.sounds().",
        )
    if effect_track is None:
        return loaded
    track, before = effect_track
    added = [(slot, now) for slot, (then, now) in enumerate(zip(before, _effects(session, track))) if then != now]
    if len(added) != 1 or not _same_plugin(added[0][1], plugin):
        raise WriteRejected(
            f"Mixer track {track} did not gain one {plugin} after loading {path.name} (changed slots: {added}).",
            fix="Check the mixer track's effects before loading the preset again.",
            retryable=False,
        )
    slot, effect = added[0]
    return loaded.model_copy(update={"effect": MixerEffect(mixer_track=track, slot=slot, plugin=str(effect))})


def load_sound(session: FLSession, channel: int, sound: str, kind: LoadKind) -> ChannelSound:
    """Load the library ``sound`` (a path from fl.sounds()) into ``channel``; return what FL now has."""
    path, role, plugin = _library_file(sound, kind)
    return _load(session, channel, path, sound, kind, role, plugin)


def load_audio(session: FLSession, channel: int, name: str, audio: bytes) -> ChannelSound:
    """Load ``audio``, an audio file's bytes named ``name``, into the sampler ``channel``."""
    path = _imported_sample(name, audio)
    return _load(session, channel, path, name, "sample", "sample", None)


def _imported_sample(name: str, audio: bytes) -> Path:
    """The engine's own WAV of ``audio``: decoded, checked and written once per content,
    in a folder of its own so the file keeps the model's name."""
    stem = PurePosixPath(name).stem
    if not _SAMPLE_NAME.fullmatch(name) or stem.rstrip(" .") != stem or not stem or stem.upper() in _WINDOWS_DEVICES:
        raise errors.invalid_argument("fl.load_sample", f"{name!r} is not a file name FL can name a channel after")
    try:
        samples, rate = decode_audio(audio)
    except ValueError as exc:
        raise errors.invalid_argument("fl.load_sample", f"{name}: {exc}") from None
    folder = app_data_root() / "samples" / hashlib.sha256(audio).hexdigest()[:24]
    path = folder / f"{stem}.wav"
    if not path.is_file():
        import soundfile

        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f".{stem}.{os.getpid()}.partial"
        # Float keeps whatever level the model wrote; FL reads 32-bit float WAVs.
        soundfile.write(partial, samples, rate, subtype="FLOAT", format="WAV")
        partial.replace(path)
    return path


def _load(
    session: FLSession, channel: int, path: Path, sound: str, kind: LoadKind, role: Role, plugin: str | None
) -> ChannelSound:
    _require_bridge(session)
    effect_track = _ready(session, channel, role)
    select_only(session, channel, unchanged=NOTHING_LOADED)
    _send_load(session, path, channel)
    name, instrument, sampler = _instrument(session, channel)
    loaded = ChannelSound(channel=channel, kind=kind, sound=sound, name=name, plugin=instrument)
    loaded = _confirm(session, loaded, path, role, plugin, effect_track, sampler)
    log.info("loaded %s into channel %d: %s", PurePosixPath(sound).name, channel, loaded)
    return loaded


def name_channel(session: FLSession, channel: int, name: str) -> str:
    """Name ``channel`` (``channels.setChannelName``) and return FL's readback, which must match.

    Loading renames a channel after its file; the audition channel is found by name, so
    it is named back after each load."""
    _live_write(session, "channels.setChannelName", [channel, name])
    now = str(_live_read(session, "channels.getChannelName", [channel]))
    if now != name:
        raise WriteRejected(
            f"Channel {channel} is named {now!r} after naming it {name!r}, so FL did not confirm the name.",
            fix="Read fl.channels and retry.",
        )
    return now


def channels_named(session: FLSession, names: tuple[str, ...], call: str) -> dict[str, int]:
    """Each of ``names``' channel index, by FL's live channel names (any case); raises
    naming every one this project lacks, before anything changes."""
    live = {c.name.casefold(): c.index for c in session.snapshot().channels}
    missing = [name for name in names if name.casefold() not in live]
    if missing:
        raise errors.invalid_argument(
            call,
            f"this project has no channel named {', '.join(missing)}: {call} needs the agent project's "
            f"{', '.join(names)} channel{'s' if len(names) > 1 else ''}",
        )
    return {name: live[name.casefold()] for name in names}
