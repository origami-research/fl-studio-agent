"""The workspace tools' operations, run inside the confined process (docs/39).

Each operation takes the session's Workspace and plain arguments, touches
files or runs a command with the confined process's own access, and returns
its result as a plain dict that the server validates. Stdlib only: the op
process starts once per call, so it imports nothing it does not need.

Paths are relative to the workspace (``work``) or absolute; results show
workspace-relative paths with forward slashes. Nothing here is a security
check: the OS boundary decides what is reachable, and the path checks only
turn a denial into a helpful message.

Every result must fit the harness's 20,000-character cap on a tool result
once the MCP layer prints it as indented JSON, so each operation cuts its own
output against ``RESULT_CHARS`` measured that way and says how to get the rest.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from fl_mcp.kernel.launch import Workspace
from fl_mcp.kernel.probe import probe as boundary_probe

# An op's result as indented JSON; the rest of the harness's 20,000 is the
# server's actions and the MCP envelope.
RESULT_CHARS = 18_000
SHELL_COMMAND_BYTES = 16 << 10
SHELL_TIMEOUT_DEFAULT = 120
SHELL_TIMEOUT_MAX = 600
# stdout and stderr together, each cut to its first 40% and last 60%.
SHELL_OUTPUT_CHARS = 16_000
SHELL_HEAD_SHARE = 0.4
# The full output of a cut command goes to a log in the workspace, up to this much.
SHELL_LOG_BYTES = 64 << 20
LOGS_DIR = ".logs"
# A path, glob pattern or regular expression; results and errors echo them.
NAME_CHARS = 4096
READ_LINES = 2000
READ_LINE_CHARS = 2000
READ_CHARS = 16_000
WRITE_BYTES = 1 << 20
EDIT_STRING_BYTES = 256 << 10
GLOB_PATHS = 500
GREP_RESULTS = 200
GREP_FILE_BYTES = 20 << 20
GREP_LINE_CHARS = 300
# A search walks at most this many folder entries, so a huge tree cannot use the whole call.
WALK_ENTRIES = 200_000
# The workspace's own plumbing (kernel_env): skipped by glob and grep unless searched directly.
PLUMBING = frozenset({".packages", ".cache", ".tmp", ".appdata"})

# Bytes kept of each stream while a command runs: generous for multi-byte text.
_HEAD_BYTES = 32 << 10
_TAIL_BYTES = 48 << 10
_PIPE_CHUNK = 64 << 10
# After the command exits, how long its output may still drain: a background
# process it started can hold the pipes open, and is stopped when the call ends.
_DRAIN_SECONDS = 1.0
_SNIFF_BYTES = 8192
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF8_BOM = b"\xef\xbb\xbf"
_GLOB_MAGIC = re.compile(r"[*?[]")


class OpError(Exception):
    """A refusal the model can act on: what went wrong and what to do instead."""

    def __init__(self, message: str, fix: str) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


def json_chars(value: Any) -> int:
    """How many characters ``value`` takes as the MCP layer prints it (indented JSON)."""
    return len(json.dumps(value, indent=2, ensure_ascii=False))


# -- paths ------------------------------------------------------------------------


def resolve(workspace: Workspace, path: str) -> Path:
    if not path.strip():
        raise OpError("path is empty.", "Pass a path relative to your workspace, like notes/plan.md.")
    given = Path(path)
    return Path(os.path.normpath(given if given.is_absolute() else workspace.work / given))


def show(workspace: Workspace, path: Path) -> str:
    """Workspace-relative with forward slashes when inside it, else the absolute path.

    A name that is not valid Unicode (bytes on Linux, an unpaired UTF-16 half
    on NTFS) shows with "?" in its place, so the answer is always valid UTF-8.
    """
    try:
        relative = os.path.relpath(path, workspace.work)
    except ValueError:  # another drive
        relative = None
    if relative is None or relative == os.pardir or relative.startswith(os.pardir + os.sep):
        text = str(path)
    else:
        text = Path(relative).as_posix()
    return text.encode("utf-8", "replace").decode("utf-8")


def _echo(text: str, limit: int = 200) -> str:
    """A caller's path in a note, short enough that it cannot crowd out the result."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _inside(path: Path, folder: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(
            folder
        )
    except ValueError:
        return False


def _refuse_renders(workspace: Workspace, path: Path, shown: str) -> None:
    if _inside(path, workspace.renders):
        raise OpError(
            f"{shown} is in the renders folder, which is read-only: the engine writes renders there.",
            "Write to your workspace instead, for example notes/ or exports/.",
        )


def _not_a_folder(workspace: Workspace, path: Path, shown: str) -> None:
    """Windows reports opening a folder as a permission error; say what it is instead."""
    if _is(path.is_dir, workspace, path, shown):
        raise OpError(f"{shown} is a folder, not a file.", "List it with glob.")


def _is(check: Any, workspace: Workspace, path: Path, shown: str) -> bool:
    """``path.is_dir``/``is_file``, with a denial (Python 3.12+ raises it) told plainly."""
    try:
        return bool(check())
    except OSError as exc:
        raise _os_error(exc, workspace, path, shown) from exc


def _os_error(exc: OSError, workspace: Workspace, path: Path, shown: str) -> OpError:
    if isinstance(exc, FileNotFoundError):
        return OpError(f"{shown} does not exist.", "Check the path; glob finds files by name.")
    if isinstance(exc, IsADirectoryError):
        return OpError(f"{shown} is a folder, not a file.", "List it with glob.")
    if isinstance(exc, NotADirectoryError):
        return OpError(
            f"Part of {shown} is a file, not a folder.", "Check the path; glob finds files by name."
        )
    if isinstance(exc, PermissionError):
        if _inside(path, workspace.renders):
            return OpError(f"{shown} is in the read-only renders folder.", "Write to your workspace instead.")
        return OpError(
            f"Permission denied: {shown}.",
            "You can read and write only your workspace (paths relative to it, like notes/plan.md) "
            "and read the renders folder.",
        )
    return OpError(f"{shown}: {exc.strerror or exc}.", "Check the path and retry.")


# -- read_file -------------------------------------------------------------------

_KIND_BY_MAGIC = (
    (b"MThd", "midi"),
    (b"RIFF", "audio"),  # WAV; an AVI would be RIFF too, and the hint still holds
    (b"fLaC", "audio"),
    (b"OggS", "audio"),
    (b"ID3", "audio"),
    (b"FORM", "audio"),
    (b"\x89PNG", "image"),
    (b"\xff\xd8\xff", "image"),
    (b"GIF8", "image"),
    (b"FLhd", "fl_song"),
    (b"PK\x03\x04", "archive"),
)
_KIND_BY_SUFFIX = {
    ".mid": "midi",
    ".midi": "midi",
    ".wav": "audio",
    ".mp3": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".aif": "audio",
    ".aiff": "audio",
    ".m4a": "audio",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".flp": "fl_song",
    ".zip": "archive",
}
HINTS = {
    "midi": "MIDI: parse it with Python in shell or run (pip install mido).",
    "audio": "Audio: measure or hear it with your audio tools, or read the samples with Python (wave, numpy).",
    "image": "Image: read_file returns text only.",
    "fl_song": "An FL Studio song: work on it in FL Studio, not as text.",
    "archive": "Archive: list or extract it with Python's zipfile in shell.",
    "binary": "Binary file: inspect it with Python in shell.",
}


def _binary_kind(head: bytes, suffix: str) -> str | None:
    """None for text. UTF-16 text (PowerShell's ``>`` writes it) has NULs but is text."""
    if head.startswith(_UTF16_BOMS) or b"\0" not in head:
        return None
    for magic, kind in _KIND_BY_MAGIC:
        if head.startswith(magic):
            return kind
    return _KIND_BY_SUFFIX.get(suffix.lower(), "binary")


def _lines(text: IO[str]) -> Iterator[tuple[str, bool]]:
    """Each line without its newline, cut at READ_LINE_CHARS; the flag says it was cut.

    A line is read in bounded slices, so a huge file with no newlines cannot
    fill the process's memory.
    """
    while line := text.readline(READ_LINE_CHARS + 1):
        if line.endswith("\n"):
            yield line[:-1], False
            continue
        if len(line) <= READ_LINE_CHARS:
            yield line, False  # the last line, without a newline
            continue
        while (rest := text.readline(_PIPE_CHUNK)) and not rest.endswith("\n"):
            pass
        yield line[:READ_LINE_CHARS], True


def read_file(workspace: Workspace, path: str, offset: int = 1, limit: int = READ_LINES) -> dict[str, Any]:
    full = resolve(workspace, path)
    shown = show(workspace, full)
    _not_a_folder(workspace, full, shown)
    try:
        with full.open("rb") as raw:
            size = os.fstat(raw.fileno()).st_size
            head = raw.read(_SNIFF_BYTES)
            kind = _binary_kind(head, full.suffix)
            if kind is not None:
                return {"path": shown, "kind": kind, "bytes": size, "hint": HINTS[kind]}
            raw.seek(0)
            encoding = "utf-16" if head.startswith(_UTF16_BOMS) else "utf-8-sig"
            text = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline=None)
            budget = READ_CHARS - json_chars(shown)
            return {"path": shown, "kind": "text", "bytes": size, **_numbered(text, offset, limit, budget)}
    except OSError as exc:
        raise _os_error(exc, workspace, full, shown) from exc


def _numbered(text: IO[str], offset: int, limit: int, budget: int) -> dict[str, Any]:
    shown: list[str] = []
    used = 0
    end = offset - 1
    total = 0
    cut_line = full = False
    for total, (line, cut) in enumerate(_lines(text), 1):
        if total < offset or full or total >= offset + limit:
            continue
        entry = f"{total:>6}\t{line}{' [line cut]' if cut else ''}\n"
        cost = json_chars(entry) - 2
        if used + cost > budget:
            full = True
            continue
        shown.append(entry)
        used += cost
        end = total
        cut_line = cut_line or cut
    notes = []
    if total == 0:
        notes.append("The file is empty.")
    elif offset > total:
        notes.append(f"The file has {total} lines; offset {offset} is past its end.")
    elif end < total:
        notes.append(
            f"Showing lines {offset}-{end} of {total}. Call read_file with offset={end + 1} to read on."
        )
    if cut_line:
        notes.append(
            f"Lines longer than {READ_LINE_CHARS} characters were cut; grep or shell shows them whole."
        )
    return {
        "content": "".join(shown).removesuffix("\n"),
        "start_line": offset,
        "end_line": end,
        "total_lines": total,
        "truncated": end < total or cut_line,
        "note": " ".join(notes) or None,
    }


# -- write_file / edit_file ----------------------------------------------------------


def write_file(workspace: Workspace, path: str, content: str) -> dict[str, Any]:
    full = resolve(workspace, path)
    shown = show(workspace, full)
    _refuse_renders(workspace, full, shown)
    _not_a_folder(workspace, full, shown)
    data = content.encode("utf-8")
    try:
        created = not full.exists()
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
    except OSError as exc:
        raise _os_error(exc, workspace, full, shown) from exc
    return {"path": shown, "bytes": len(data), "created": created}


def edit_file(
    workspace: Workspace, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> dict[str, Any]:
    full = resolve(workspace, path)
    shown = show(workspace, full)
    _refuse_renders(workspace, full, shown)
    if not old_string:
        raise OpError("old_string is empty.", "To create or overwrite a whole file, use write_file.")
    if old_string == new_string:
        raise OpError(
            "old_string and new_string are the same, so nothing would change.", "Pass the new text."
        )
    _not_a_folder(workspace, full, shown)
    try:
        raw = full.read_bytes()
    except OSError as exc:
        raise _os_error(exc, workspace, full, shown) from exc
    bom = _UTF8_BOM if raw.startswith(_UTF8_BOM) else b""
    try:
        text = raw[len(bom) :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpError(
            f"{shown} is not UTF-8 text, so it cannot be edited as text.",
            "Rewrite it with write_file, or change it with Python in shell.",
        ) from exc
    found = _found(text, old_string)
    if found == 0 and "\r\n" in text and "\n" in old_string and "\r" not in old_string:
        # read_file shows Windows line endings as plain newlines: try the file's own.
        translated = old_string.replace("\n", "\r\n")
        if found := _found(text, translated):
            old_string = translated
    if found == 0:
        crlf = "\r\n" in text and not _windows_endings(text) and "\n" in old_string
        raise OpError(
            f"old_string was not found in {shown}."
            + (" The file mixes Windows (\\r\\n) and plain (\\n) line endings." if crlf else ""),
            "Read the file again and copy the exact text, including spaces and indentation.",
        )
    if found > 1 and not replace_all:
        times = text.count(old_string)
        raise OpError(
            f"old_string appears {f'{times} times' if times > 1 else 'more than once (its matches overlap)'} "
            f"in {shown}.",
            "Include more of the surrounding lines so it matches once, or set replace_all to change every one.",
        )
    if "\r" not in new_string and ("\r\n" in old_string or _windows_endings(text)):
        new_string = new_string.replace("\n", "\r\n")  # new lines keep the file's endings
    updated = text.replace(old_string, new_string, -1 if replace_all else 1)
    try:
        full.write_bytes(bom + updated.encode("utf-8"))
    except OSError as exc:
        raise _os_error(exc, workspace, full, shown) from exc
    return {"path": shown, "replacements": text.count(old_string) if replace_all else 1}


def _found(text: str, old: str) -> int:
    """0, 1, or 2 for "more than one": overlapping matches count ("aa" is in "aaa" twice).

    It stops at two, so a long repetitive file cannot make the check slow.
    """
    first = text.find(old)
    if first == -1:
        return 0
    return 1 if text.find(old, first + 1) == -1 else 2


def _windows_endings(text: str) -> bool:
    """Every line break in the file is \r\n."""
    return "\r\n" in text and "\n" not in text.replace("\r\n", "")


# -- glob / grep ----------------------------------------------------------------------


def _parts(pattern: str) -> list[str]:
    parts: list[str] = []
    for part in re.split(r"[\\/]", pattern):
        if part in ("", ".") or (part == "**" and parts[-1:] == ["**"]):
            continue  # "**/**" is "**"
        parts.append(part)
    return parts


def _matches(pattern: list[str], parts: tuple[str, ...]) -> bool:
    """Component-wise glob: ``**`` spans any folders, the rest is fnmatch (case-insensitive on Windows)."""
    return _match_from(tuple(pattern), parts, 0, 0, {})


def _match_from(
    pattern: tuple[str, ...], parts: tuple[str, ...], p: int, i: int, seen: dict[tuple[int, int], bool]
) -> bool:
    """Pattern from index p against parts from index i, each pair decided once (no blow-up on many **)."""
    key = (p, i)
    if key not in seen:
        if p == len(pattern):
            seen[key] = i == len(parts)
        elif pattern[p] == "**":
            seen[key] = any(_match_from(pattern, parts, p + 1, j, seen) for j in range(i, len(parts) + 1))
        else:
            seen[key] = (
                i < len(parts)
                and fnmatch.fnmatch(parts[i], pattern[p])
                and _match_from(pattern, parts, p + 1, i + 1, seen)
            )
    return seen[key]


class _Walk:
    """The files under a folder, plumbing and linked folders skipped, at most WALK_ENTRIES entries."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.stopped = False

    def __iter__(self) -> Iterator[tuple[tuple[str, ...], os.DirEntry[str]]]:
        seen = 0
        stack: list[tuple[Path, tuple[str, ...]]] = [(self.base, ())]
        while stack:
            folder, parts = stack.pop()
            try:
                with os.scandir(folder) as scan:
                    entries = sorted(scan, key=lambda e: e.name)
            except OSError:
                continue  # unreadable folders are skipped, like an rg search
            subfolders = []
            for entry in entries:
                seen += 1
                if seen > WALK_ENTRIES:
                    self.stopped = True
                    return
                try:
                    if entry.is_symlink() or _is_junction(entry):
                        continue  # never walk a link: no loops, no second copy of a tree
                    if entry.is_dir():
                        if entry.name not in PLUMBING:
                            subfolders.append((Path(entry.path), (*parts, entry.name)))
                    elif entry.is_file():
                        yield (*parts, entry.name), entry
                except OSError:
                    continue
            stack.extend(reversed(subfolders))


def _is_junction(entry: os.DirEntry[str]) -> bool:
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+; 3.11 cannot see junctions
    return bool(is_junction and is_junction())


def _mtime(entry: os.DirEntry[str]) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _within_budget(items: list[str], cap: int) -> list[str]:
    """The leading items that fit both ``cap`` and the result budget as a JSON list."""
    kept: list[str] = []
    used = 0
    for item in items[:cap]:
        cost = json_chars(item) + 6  # indent, comma, newline
        if used + cost > RESULT_CHARS - 1000:
            break
        kept.append(item)
        used += cost
    return kept


def glob(workspace: Workspace, pattern: str, path: str | None = None) -> dict[str, Any]:
    if not pattern.strip():
        raise OpError("pattern is empty.", 'Pass a pattern like "**/*.mid" or "notes/*.md".')
    base = resolve(workspace, path or ".")
    parts = _parts(pattern)
    if Path(pattern).is_absolute():
        base = Path(Path(pattern).anchor)
        parts = _parts(pattern[len(Path(pattern).anchor) :])
    while len(parts) > 1 and not _GLOB_MAGIC.search(parts[0]):
        base = base / parts.pop(0)  # a literal leading folder narrows the walk
    shown_base = show(workspace, base)
    if not _is(base.is_dir, workspace, base, shown_base):
        return {"paths": [], "truncated": False, "note": f"{_echo(shown_base)} is not a folder you can read."}
    walk = _Walk(base)
    found = [(entry, _mtime(entry)) for rel, entry in walk if _matches(parts, rel)]
    found.sort(key=lambda item: (-item[1], item[0].path))
    paths = _within_budget([show(workspace, Path(entry.path)) for entry, _ in found], GLOB_PATHS)
    notes = []
    if not found:
        notes.append(f"No files match the pattern under {_echo(shown_base)}.")
    elif len(paths) < len(found):
        notes.append(
            f"Showing {len(paths)} of {len(found)} matches, newest first. Narrow the pattern or path."
        )
    if walk.stopped:
        notes.append(f"The search stopped after {WALK_ENTRIES:,} entries; narrow path.")
    return {
        "paths": paths,
        "truncated": len(paths) < len(found) or walk.stopped,
        "note": " ".join(notes) or None,
    }


def _read_lines(path: Path) -> list[str] | None:
    """A file's lines for grep, numbered as read_file numbers them; None when it is binary."""
    data = path.read_bytes()
    if data.startswith(_UTF16_BOMS):
        text = data.decode("utf-16", "replace")
    elif b"\0" in data[:_SNIFF_BYTES]:
        return None
    else:
        text = data.decode("utf-8-sig", "replace")
    return [line.removesuffix("\n") for line in io.StringIO(text, newline=None)]


def grep(
    workspace: Workspace,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    case_insensitive: bool = False,
    output_mode: str = "files",
) -> dict[str, Any]:
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        raise OpError(
            f"pattern is not a valid Python regular expression: {exc}.",
            "Escape special characters such as ( [ . * with a backslash.",
        ) from exc
    base = resolve(workspace, path or ".")
    shown_base = show(workspace, base)
    name_filter = _parts(glob) if glob else None
    walk = _Walk(base)
    if _is(base.is_file, workspace, base, shown_base):
        files: Any = [((base.name,), base)]
    elif _is(base.is_dir, workspace, base, shown_base):
        files = ((rel, Path(entry.path)) for rel, entry in walk)
    else:
        raise OpError(f"{shown_base} does not exist.", "Check the path; glob finds files by name.")
    hits: list[tuple[str, float, int]] = []  # (shown path, mtime, matching lines)
    lines: list[str] = []
    too_big = 0
    for rel, file in files:
        if name_filter and not _matches(name_filter, rel if len(name_filter) > 1 else rel[-1:]):
            continue
        try:
            stat = file.stat()
            if stat.st_size > GREP_FILE_BYTES:
                too_big += 1
                continue
            text = _read_lines(file)
        except OSError:
            continue
        if text is None:
            continue
        shown = show(workspace, file)
        count = 0
        for number, line in enumerate(text, 1):
            if regex.search(line):
                count += 1
                if output_mode == "lines":
                    lines.append(f"{shown}:{number}: {line[:GREP_LINE_CHARS]}")
        if count:
            hits.append((shown, stat.st_mtime, count))
        if output_mode == "lines" and len(lines) > GREP_RESULTS:
            break  # enough: the rest would be cut anyway
    if output_mode == "files":
        hits.sort(key=lambda hit: (-hit[1], hit[0]))
        results = [hit[0] for hit in hits]
    elif output_mode == "count":
        results = [f"{hit[0]}: {hit[2]}" for hit in hits]
    else:
        results = lines
    matches = _within_budget(results, GREP_RESULTS)
    truncated = len(matches) < len(results) or walk.stopped
    notes = []
    if not results:
        notes.append(f"No matches under {_echo(shown_base)}.")
    elif truncated:
        notes.append(f"Showing the first {len(matches)} results. Narrow the pattern, path or glob.")
    if too_big:
        notes.append(f"Skipped {too_big} file(s) over {GREP_FILE_BYTES >> 20} MB.")
    if walk.stopped:
        notes.append(f"The search stopped after {WALK_ENTRIES:,} entries; narrow path.")
    return {"mode": output_mode, "matches": matches, "truncated": truncated, "note": " ".join(notes) or None}


# -- shell --------------------------------------------------------------------------


def shell_argv(command: str, *, windows: bool, comspec: str | None) -> str | list[str]:
    """How the command runs. Windows takes the line verbatim, quoted the way
    ``subprocess`` quotes ``shell=True`` (cmd /s strips exactly the outer quotes);
    /d skips AutoRun commands from the registry."""
    if windows:
        return f'{comspec or "cmd.exe"} /d /s /c "{command}"'
    return ["/bin/sh", "-c", command]


def one_line(command: str) -> None:
    """cmd.exe silently runs only the first line, so a multi-line command would half-run."""
    if "\n" in command.strip():
        raise OpError(
            "cmd.exe runs only the first line of a command with line breaks.",
            "Chain the steps on one line with &&, or write a script file and run it.",
        )


class _Capture:
    """One output stream: its first and last bytes, and how many there were in all."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = _HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        if len(self.tail) > 2 * _TAIL_BYTES:
            del self.tail[:-_TAIL_BYTES]

    def text(self) -> tuple[str, str | None]:
        """(the text, None) when whole; (its start, its end) when bytes between were dropped."""
        tail = bytes(self.tail[-_TAIL_BYTES:])
        if self.total == len(self.head) + len(tail):
            return (bytes(self.head) + tail).decode("utf-8", "replace"), None
        return bytes(self.head).decode("utf-8", "replace"), tail.decode("utf-8", "replace")


class _Log:
    """The full output of one command, written as it arrives; None-safe when it cannot be written."""

    def __init__(self, path: Path, command: str) -> None:
        self.path = path
        self.written = 0
        self.lock = threading.Lock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file: IO[bytes] | None = path.open("wb")
            self.file.write(f"$ {command}\n".encode())
        except OSError:
            self.file = None

    def write(self, chunk: bytes) -> None:
        with self.lock:
            if self.file is None or self.written >= SHELL_LOG_BYTES:
                return
            self.file.write(chunk[: SHELL_LOG_BYTES - self.written])
            self.written += len(chunk)
            if self.written >= SHELL_LOG_BYTES:
                self.file.write(f"\n[log stopped at {SHELL_LOG_BYTES >> 20} MB]\n".encode())

    def close(self, keep: bool) -> bool:
        """Close; keep the file only when asked. True when a kept log exists."""
        with self.lock:
            if self.file is None:
                return False
            try:
                self.file.close()
                if not keep:
                    self.path.unlink()
            except OSError:
                return False
            finally:
                self.file = None
            return keep


def _pump(
    stream: IO[bytes], capture: _Capture, log: _Log, lock: threading.Lock, done: threading.Event
) -> None:
    while chunk := stream.read1(_PIPE_CHUNK):  # type: ignore[attr-defined]
        with lock:
            if done.is_set():
                return
            capture.add(chunk)
        log.write(chunk)


def _clip(text: tuple[str, str | None], keep: int, marker: str) -> tuple[str, bool]:
    whole, end = text
    if end is None and len(whole) <= keep:
        return whole, False
    head = int(keep * SHELL_HEAD_SHARE)
    tail_source = whole if end is None else end
    return whole[:head] + marker + tail_source[len(tail_source) - (keep - head) :], True


def _fit_output(
    out: tuple[str, str | None], err: tuple[str, str | None], log_name: str | None, result: dict[str, Any]
) -> tuple[str, str, bool]:
    """stdout and stderr cut to share SHELL_OUTPUT_CHARS, and fewer if escapes make the JSON longer."""
    where = f"the full output is in {log_name}" if log_name else "the full output could not be saved"
    marker = f"\n[... output cut here; {where} ...]\n"

    def size(text: tuple[str, str | None]) -> int:
        return len(text[0]) if text[1] is None else 10**9

    budget = SHELL_OUTPUT_CHARS
    while True:
        err_share = min(size(err), max(budget // 4, budget - size(out)))
        stdout, cut_out = _clip(out, budget - err_share, marker)
        stderr, cut_err = _clip(err, err_share, marker)
        over = json_chars({**result, "stdout": stdout, "stderr": stderr}) - RESULT_CHARS
        if over <= 0 or budget <= 500:
            return stdout, stderr, cut_out or cut_err
        budget = max(500, budget * RESULT_CHARS // (RESULT_CHARS + over) - 200)


def shell(workspace: Workspace, command: str, timeout_s: int, log_number: int) -> dict[str, Any]:
    windows = os.name == "nt"
    if windows:
        one_line(command)
    log_name = f"{LOGS_DIR}/shell-{log_number}.txt"
    log = _Log(workspace.work / LOGS_DIR / f"shell-{log_number}.txt", command)
    argv = shell_argv(command, windows=windows, comspec=os.environ.get("COMSPEC"))
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, cwd=workspace.work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        log.close(keep=False)
        raise OpError(
            f"The shell could not start: {exc}.", "Retry once; if it fails again, report it."
        ) from exc
    process.stdin.close()  # an empty input: a command that reads it sees its end at once
    out, err = _Capture(), _Capture()
    lock, done = threading.Lock(), threading.Event()
    readers = [
        threading.Thread(target=_pump, args=(process.stdout, out, log, lock, done), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, err, log, lock, done), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        exit_code: int | None = process.wait(timeout_s)
    except subprocess.TimeoutExpired:
        exit_code = None
        process.kill()  # the shell itself; the server stops the rest of its tree when the call ends
    duration = time.monotonic() - started
    deadline = time.monotonic() + _DRAIN_SECONDS
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
    lingering = exit_code is not None and any(reader.is_alive() for reader in readers)
    with lock:
        done.set()
        texts = out.text(), err.text()
    notes = []
    if exit_code is None:
        notes.append(
            f"The command ran past its {timeout_s} s limit and was stopped; output up to then is shown."
        )
    if lingering:
        notes.append(
            "It left a process running, which was stopped; that process's later output is not shown."
        )
    result: dict[str, Any] = {
        "exit_code": exit_code,
        "timed_out": exit_code is None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "full_output": log_name,
        "duration_s": round(duration, 2),
        "note": " ".join(notes) or None,
    }
    stdout, stderr, cut = _fit_output(*texts, log_name if log.file is not None else None, result)
    kept = log.close(keep=cut)
    return {
        **result,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": cut,
        "full_output": log_name if kept else None,
    }


# -- the boundary probe -----------------------------------------------------------------


def probe(workspace: Workspace, home: str, loopback_port: int) -> dict[str, bool]:
    return boundary_probe(workspace.canary, Path(home), loopback_port)


OPS = {
    "probe": probe,
    "shell": shell,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "glob": glob,
    "grep": grep,
}
