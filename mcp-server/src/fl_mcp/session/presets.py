"""A plugin channel's presets: list them, and step the plugin to one.

Documented controller calls only, live-verified on FL 2026 (controller API 45,
docs/research/routing-presets-live-2026-11-02.md): ``plugins.getPresetCount``
and ``plugins.getName(channel, -1, FPN_PRESET, i)`` list a plugin's presets, and
``plugins.nextPreset`` / ``prevPreset`` step it one preset at a time.

FL has no getter for the current preset. Stepping renames the channel to the
preset's name, and that name is the only readback: ``set_preset`` steps from
the preset the channel's name shows and confirms by the name it ends on.
A sampler channel is not a plugin, and FL answers its plugin calls with
"Plugin not valid".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_mcp import errors
from fl_mcp.errors import WriteRejected
from fl_mcp.schemas import ChannelPreset

from .controls import GENERATOR_SLOT, _live_int, _live_read, _live_write

if TYPE_CHECKING:
    from .facade import FLSession

# plugins.getName's flag for a preset name (FL's FPN_Preset).
FPN_PRESET = 6
# FL's answer to a plugins.* call on a channel that has no plugin (a sampler).
PLUGIN_NOT_VALID = "plugin not valid"
# How many presets an unmatched name lists back.
LISTED_PRESETS = 20


def _channel_name(session: FLSession, channel: int) -> str:
    return str(_live_read(session, "channels.getChannelName", [channel]))


def _preset_count(session: FLSession, channel: int) -> int:
    try:
        return _live_int(session, "plugins.getPresetCount", [channel, GENERATOR_SLOT])
    except errors.ToolError as exc:
        if exc.category == "fl_api_error" and PLUGIN_NOT_VALID in exc.message.lower():
            raise errors.not_a_plugin(_channel_name(session, channel)) from exc
        raise


def presets(session: FLSession, channel: int) -> list[str]:
    """The names of the channel plugin's presets, in FL's order."""
    count = _preset_count(session, channel)
    return [
        str(_live_read(session, "plugins.getName", [channel, GENERATOR_SLOT, FPN_PRESET, index]))
        for index in range(count)
    ]


def _choose(names: list[str], preset: int | str, channel: int) -> int:
    """The index of ``preset``: an index, a name (any case), or a unique part of one."""
    if isinstance(preset, int):
        if 0 <= preset < len(names):
            return preset
        raise errors.invalid_argument(
            "fl.set_preset", f"channel {channel} has presets 0 to {len(names) - 1}, not {preset}"
        )
    wanted = preset.casefold()
    exact = [index for index, name in enumerate(names) if name.casefold() == wanted]
    if exact:
        return exact[0]  # presets sharing a name read back the same, so any of them is it
    partial = [index for index, name in enumerate(names) if wanted in name.casefold()]
    if len(partial) == 1:
        return partial[0]
    candidates = [names[index] for index in partial] or names
    listed = ", ".join(candidates[:LISTED_PRESETS]) + ("..." if len(candidates) > LISTED_PRESETS else "")
    raise errors.ToolError(
        "unknown_target",
        f"Channel {channel} has {'several presets matching' if partial else 'no preset matching'} {preset!r}.",
        f"Pass a preset's exact name or index from fl.presets(). Presets: {listed}",
        candidates=candidates[:LISTED_PRESETS],
    )


def _place(names: list[str], shown: str) -> int | None:
    """Where in the list the channel's name puts FL, when exactly one preset has that name."""
    return names.index(shown) if names.count(shown) == 1 else None


def _step(session: FLSession, channel: int, steps: int, *, taken: int = 0) -> None:
    """Step ``steps`` presets (back when negative); ``taken`` steps already landed in this call."""
    op = "plugins.nextPreset" if steps > 0 else "plugins.prevPreset"
    for done in range(abs(steps)):
        try:
            _live_write(session, op, [channel, GENERATOR_SLOT])
        except errors.ToolError as exc:
            raise WriteRejected(
                f"FL Studio stopped changing channel {channel}'s preset after {taken + done} of "
                f"{taken + abs(steps)} steps: {exc.message}",
                fix="Call set_preset again; it steps from the preset the channel shows now.",
            ) from exc


def set_preset(session: FLSession, channel: int, preset: int | str) -> ChannelPreset:
    """Step the channel's plugin to ``preset`` and confirm it by the channel's new name."""
    names = presets(session, channel)
    if not names:
        raise errors.invalid_argument("fl.set_preset", f"the plugin on channel {channel} has no presets")
    index = _choose(names, preset, channel)
    wanted = names[index]
    shown = _channel_name(session, channel)
    taken = 0
    if shown != wanted:
        place = _place(names, shown)
        if place is None:
            # The name shows no single preset (a fresh or renamed channel, or a
            # name two presets share): one step makes FL name where it is.
            _step(session, channel, 1)
            taken = 1
            shown = _channel_name(session, channel)
            place = _place(names, shown)
            if place is None:
                raise WriteRejected(
                    f"FL Studio moved channel {channel} to its next preset, but the channel's name "
                    f"{shown!r} does not tell which of its presets that is, so it cannot be confirmed.",
                    fix="Pick a preset with set_preset by another name, or shape the sound with set_param.",
                    retryable=False,
                )
        _step(session, channel, index - place, taken=taken)
        shown = _channel_name(session, channel)
    if shown != wanted:
        raise WriteRejected(
            f"Channel {channel} now shows the preset {shown!r}, not {wanted!r}.",
            fix="Call set_preset again; it steps from the preset the channel shows now.",
        )
    return ChannelPreset(channel=channel, index=index, name=shown)
