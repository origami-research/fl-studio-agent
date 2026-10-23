"""The workspace operations themselves, called in-process on a temp workspace.

These are the functions the confined op process runs (fl_mcp.workspace.ops).
They pin the model-facing rules: exact-match edits, caps that say how to get
the rest, binary files described instead of dumped, plumbing skipped by
searches, and every result small enough for the harness's 20,000-character
cap once the MCP layer prints it as indented JSON.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from fl_mcp.kernel.launch import Workspace
from fl_mcp.workspace import ops
from fl_mcp.workspace.ops import OpError

# What the harness keeps of one tool result (its MAX_RESULT_CHARS).
HARNESS_RESULT_CHARS = 20_000


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace.create(tmp_path / "root")


def _write(ws: Workspace, rel: str, data: str | bytes) -> None:
    path = ws.work / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8", newline="")
    else:
        path.write_bytes(data)


def _fits(result: dict) -> bool:
    return ops.json_chars(result) <= ops.RESULT_CHARS < HARNESS_RESULT_CHARS


# -- paths ------------------------------------------------------------------------------


def test_paths_are_workspace_relative_in_results_and_absolute_outside(ws, tmp_path):
    assert ops.show(ws, ops.resolve(ws, "notes/plan.md")) == "notes/plan.md"
    assert ops.show(ws, ops.resolve(ws, str(ws.work / "a" / "b.txt"))) == "a/b.txt"
    outside = ops.resolve(ws, "../renders/mix.wav")
    assert outside == ws.renders / "mix.wav"
    assert ops.show(ws, outside) == str(ws.renders / "mix.wav")
    with pytest.raises(OpError, match="path is empty"):
        ops.resolve(ws, "  ")


# -- write_file / edit_file ------------------------------------------------------------------


def test_write_creates_parents_and_reports_created_then_replaced(ws):
    first = ops.write_file(ws, "notes/deep/plan.md", "a\nb\n")
    assert first == {"path": "notes/deep/plan.md", "bytes": 4, "created": True}
    second = ops.write_file(ws, "notes/deep/plan.md", "é")
    assert second == {"path": "notes/deep/plan.md", "bytes": 2, "created": False}
    assert (ws.work / "notes/deep/plan.md").read_bytes() == "é".encode()


def test_write_keeps_content_exactly_without_newline_translation(ws):
    ops.write_file(ws, "crlf.txt", "a\r\nb\n")
    assert (ws.work / "crlf.txt").read_bytes() == b"a\r\nb\n"


def test_writes_into_renders_are_refused_with_a_helpful_message(ws):
    """renders is the engine's; on Windows the OS denies it, everywhere the message explains it."""
    for call in (
        lambda: ops.write_file(ws, "../renders/x.wav", "x"),
        lambda: ops.edit_file(ws, str(ws.renders / "x.wav"), "a", "b"),
    ):
        with pytest.raises(OpError, match="read-only") as caught:
            call()
        assert "workspace" in caught.value.fix
    assert not (ws.renders / "x.wav").exists()


def test_a_folder_is_named_as_a_folder(ws):
    (ws.work / "notes").mkdir()
    for call in (
        lambda: ops.read_file(ws, "notes"),
        lambda: ops.write_file(ws, "notes", "x"),
        lambda: ops.edit_file(ws, "notes", "a", "b"),
    ):
        with pytest.raises(OpError, match="is a folder"):
            call()


def test_edit_replaces_a_unique_match(ws):
    _write(ws, "plan.md", "intro\ngroove: TODO\noutro\n")
    assert ops.edit_file(ws, "plan.md", "TODO", "four on the floor") == {"path": "plan.md", "replacements": 1}
    assert (ws.work / "plan.md").read_text() == "intro\ngroove: four on the floor\noutro\n"


def test_edit_refuses_an_ambiguous_match_and_leaves_the_file(ws):
    _write(ws, "plan.md", "x = 1\nx = 1\nx = 1\n")
    with pytest.raises(OpError, match="appears 3 times") as caught:
        ops.edit_file(ws, "plan.md", "x = 1", "x = 2")
    assert "replace_all" in caught.value.fix
    assert (ws.work / "plan.md").read_text() == "x = 1\nx = 1\nx = 1\n"


def test_edit_replace_all_changes_every_occurrence(ws):
    _write(ws, "plan.md", "x = 1\nx = 1\n")
    assert ops.edit_file(ws, "plan.md", "1", "2", replace_all=True)["replacements"] == 2
    assert (ws.work / "plan.md").read_text() == "x = 2\nx = 2\n"


def test_edit_matches_a_windows_line_ending_file_with_plain_newlines(ws):
    """read_file shows \r\n as plain newlines, and Python text writes on the Windows host
    are \r\n: the model's copied old_string must still match, and the file keeps \r\n."""
    _write(ws, "win.txt", "a\r\nb\r\nc\r\n")
    assert ops.edit_file(ws, "win.txt", "a\nb", "x\ny")["replacements"] == 1
    assert (ws.work / "win.txt").read_bytes() == b"x\r\ny\r\nc\r\n"
    with pytest.raises(OpError, match="was not found"):
        ops.edit_file(ws, "win.txt", "zzz", "c")


def test_a_mixed_ending_file_matches_its_plain_newline_part_exactly(ws):
    """An LF file with one line appended by cmd (echo x >> f) is mixed; its LF text must still match."""
    _write(ws, "f.py", b"def f():\n    return 1\nx\r\n")
    ops.edit_file(ws, "f.py", "def f():\n    return 1", "def f():\n    return 2")
    assert (ws.work / "f.py").read_bytes() == b"def f():\n    return 2\nx\r\n"


def test_new_lines_in_a_windows_ending_file_get_windows_endings(ws):
    _write(ws, "win.txt", "a\r\nb\r\n")
    ops.edit_file(ws, "win.txt", "a", "a1\na2")
    assert (ws.work / "win.txt").read_bytes() == b"a1\r\na2\r\nb\r\n"


def test_the_uniqueness_check_stays_fast_on_a_repetitive_file(ws):
    import time as clock

    _write(ws, "a.txt", "a" * (2 << 20))
    started = clock.monotonic()
    with pytest.raises(OpError, match="appears"):
        ops.edit_file(ws, "a.txt", "a" * 2000, "b")
    assert clock.monotonic() - started < 2.0


def test_overlapping_matches_are_not_unique(ws):
    _write(ws, "a.txt", "aaa")
    with pytest.raises(OpError, match="more than once"):
        ops.edit_file(ws, "a.txt", "aa", "b")


def test_edit_refuses_empty_or_identical_strings_and_non_utf8(ws):
    _write(ws, "plan.md", "abc")
    with pytest.raises(OpError, match="old_string is empty"):
        ops.edit_file(ws, "plan.md", "", "x")
    with pytest.raises(OpError, match="the same"):
        ops.edit_file(ws, "plan.md", "abc", "abc")
    _write(ws, "latin1.txt", "caf\xe9".encode("latin-1"))
    with pytest.raises(OpError, match="not UTF-8"):
        ops.edit_file(ws, "latin1.txt", "caf", "bar")


def test_edit_keeps_a_utf8_bom(ws):
    _write(ws, "bom.txt", b"\xef\xbb\xbfhello")
    ops.edit_file(ws, "bom.txt", "hello", "bye")
    assert (ws.work / "bom.txt").read_bytes() == b"\xef\xbb\xbfbye"


def test_missing_files_say_how_to_find_them(ws):
    with pytest.raises(OpError, match="does not exist") as caught:
        ops.read_file(ws, "nope.txt")
    assert "glob" in caught.value.fix


# -- read_file -----------------------------------------------------------------------


def test_read_numbers_lines_like_cat_n(ws):
    _write(ws, "a.txt", "one\ntwo\nthree")
    result = ops.read_file(ws, "a.txt")
    assert result["content"] == "     1\tone\n     2\ttwo\n     3\tthree"
    assert (result["start_line"], result["end_line"], result["total_lines"]) == (1, 3, 3)
    assert result["truncated"] is False and result["note"] is None
    assert result["kind"] == "text" and result["bytes"] == 13


def test_read_offset_and_limit_page_through_and_say_where_to_resume(ws):
    _write(ws, "a.txt", "".join(f"line {n}\n" for n in range(1, 101)))
    result = ops.read_file(ws, "a.txt", offset=10, limit=5)
    assert result["content"].splitlines()[0] == "    10\tline 10"
    assert (result["start_line"], result["end_line"], result["total_lines"]) == (10, 14, 100)
    assert result["truncated"] is True
    assert "offset=15" in result["note"]


def test_read_caps_lines_at_2000(ws, monkeypatch):
    """The character cap usually binds first (2000 numbered lines rarely fit 16,000
    characters); with it lifted, the line cap still holds."""
    monkeypatch.setattr(ops, "READ_CHARS", 10**6)
    _write(ws, "many.txt", "x\n" * 5000)
    result = ops.read_file(ws, "many.txt")
    assert result["end_line"] == ops.READ_LINES == 2000
    assert result["total_lines"] == 5000 and "offset=2001" in result["note"]


def test_read_caps_characters_and_fits_the_harness(ws):
    _write(ws, "wide.txt", ("y" * 150 + "\n") * 1000)
    result = ops.read_file(ws, "wide.txt")
    assert result["truncated"] and result["end_line"] < 1000
    assert f"offset={result['end_line'] + 1}" in result["note"]
    assert len(result["content"]) <= ops.READ_CHARS
    assert _fits(result)


def test_read_escapes_count_against_the_cap(ws):
    """Tabs and quotes double in JSON; the cap is on what the model is sent."""
    _write(ws, "tabs.tsv", ('\t"' * 70 + "\n") * 1000)
    assert _fits(ops.read_file(ws, "tabs.tsv"))


def test_read_cuts_long_lines_and_says_so(ws):
    _write(ws, "long.txt", "a" * 5000 + "\nshort\n")
    result = ops.read_file(ws, "long.txt")
    first, second = result["content"].split("\n")
    assert first.endswith("[line cut]") and len(first) < 2100
    assert second == "     2\tshort"
    assert result["total_lines"] == 2 and "cut" in result["note"]


def test_read_offset_past_the_end_and_empty_files_say_so(ws):
    _write(ws, "a.txt", "one\n")
    assert "past its end" in ops.read_file(ws, "a.txt", offset=5)["note"]
    _write(ws, "empty.txt", "")
    empty = ops.read_file(ws, "empty.txt")
    assert empty["content"] == "" and empty["total_lines"] == 0 and "empty" in empty["note"]


def test_read_decodes_utf16_text_powershell_writes(ws):
    _write(ws, "ps.txt", "hello\r\nworld\r\n".encode("utf-16"))
    result = ops.read_file(ws, "ps.txt")
    assert result["kind"] == "text"
    assert result["content"] == "     1\thello\n     2\tworld"


@pytest.mark.parametrize(
    ("name", "data", "kind", "hint_word"),
    [
        ("riff.mid", b"MThd\x00\x00\x00\x06\x00\x01", "midi", "mido"),
        ("take.wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio", "audio"),
        ("cover.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image", "text only"),
        ("song.flp", b"FLhd\x06\x00\x00\x00", "fl_song", "FL Studio"),
        ("pack.zip", b"PK\x03\x04\x00\x00", "archive", "zipfile"),
        ("blob.bin", b"\x00\x01\x02", "binary", "Python"),
        ("mystery.mid", b"\x00\x00junk", "midi", "mido"),  # no magic: the extension decides
    ],
)
def test_binary_files_return_size_kind_and_hint_not_content(ws, name, data, kind, hint_word):
    _write(ws, name, data)
    result = ops.read_file(ws, name)
    assert result == {"path": name, "kind": kind, "bytes": len(data), "hint": ops.HINTS[kind]}
    assert hint_word in result["hint"]


# -- glob --------------------------------------------------------------------------------


def test_glob_matches_by_component_newest_first(ws):
    for rel in ("notes/a.md", "notes/sub/b.md", "c.md", "notes/x.txt"):
        _write(ws, rel, "x")
    now = time.time()
    for age, rel in enumerate(("notes/sub/b.md", "c.md", "notes/a.md")):
        os.utime(ws.work / rel, (now - age * 10, now - age * 10))
    assert ops.glob(ws, "**/*.md")["paths"] == ["notes/sub/b.md", "c.md", "notes/a.md"]
    assert ops.glob(ws, "notes/*.md")["paths"] == ["notes/a.md"]
    assert ops.glob(ws, "*.md", path="notes/sub")["paths"] == ["notes/sub/b.md"]


def test_glob_skips_plumbing_unless_asked_and_never_follows_links(ws):
    _write(ws, "song.py", "x")
    _write(ws, ".packages/numpy/core.py", "x")
    _write(ws, ".cache/pip/x.py", "x")
    (ws.work / "loop").symlink_to(ws.work, target_is_directory=True)
    assert ops.glob(ws, "**/*.py")["paths"] == ["song.py"]
    assert ops.glob(ws, "**/*.py", path=".packages")["paths"] == [".packages/numpy/core.py"]


def test_glob_caps_paths_and_says_so(ws):
    for n in range(600):
        _write(ws, f"f/{n:04d}.txt", "")
    result = ops.glob(ws, "**/*.txt")
    assert len(result["paths"]) == ops.GLOB_PATHS == 500
    assert result["truncated"] and "500 of 600" in result["note"]
    assert _fits(result)


def test_glob_with_long_paths_stays_within_the_harness_cap(ws):
    for n in range(500):
        _write(ws, f"{'deep_folder_name/' * 6}{n:04d}_{'x' * 60}.txt", "")
    result = ops.glob(ws, "**/*.txt")
    assert result["truncated"] and len(result["paths"]) < 500
    assert _fits(result)


def test_glob_with_no_match_or_missing_folder_says_so(ws):
    assert "No files match" in ops.glob(ws, "*.mid")["note"]
    assert "not a folder" in ops.glob(ws, "*.mid", path="nowhere")["note"]
    with pytest.raises(OpError, match="pattern is empty"):
        ops.glob(ws, "")


def test_glob_takes_an_absolute_pattern(ws):
    _write(ws, "notes/a.md", "x")
    assert ops.glob(ws, str(ws.work / "notes" / "*.md"))["paths"] == ["notes/a.md"]


# -- grep --------------------------------------------------------------------------------


def test_grep_modes(ws):
    _write(ws, "a.py", "tempo = 120\nkey = 'C'\ntempo2 = 1\n")
    _write(ws, "b.txt", "no match here\n")
    _write(ws, "c.py", "TEMPO = 90\n")
    now = time.time()
    os.utime(ws.work / "c.py", (now, now))
    os.utime(ws.work / "a.py", (now - 100, now - 100))
    assert ops.grep(ws, r"tempo")["matches"] == ["a.py"]
    assert ops.grep(ws, r"tempo", case_insensitive=True)["matches"] == ["c.py", "a.py"]
    assert ops.grep(ws, r"tempo", output_mode="lines")["matches"] == [
        "a.py:1: tempo = 120",
        "a.py:3: tempo2 = 1",
    ]
    assert ops.grep(ws, r"tempo", output_mode="count")["matches"] == ["a.py: 2"]
    assert ops.grep(ws, r"match", glob="*.py")["matches"] == []
    assert ops.grep(ws, r"key", path="a.py", output_mode="lines")["matches"] == ["a.py:2: key = 'C'"]


def test_grep_skips_binary_plumbing_and_huge_files(ws, monkeypatch):
    _write(ws, "bin.dat", b"needle\x00")
    _write(ws, ".packages/lib.py", "needle")
    _write(ws, "big.txt", "needle" + "x" * 10)
    _write(ws, "ok.txt", "needle")
    monkeypatch.setattr(ops, "GREP_FILE_BYTES", 7)
    result = ops.grep(ws, "needle")
    assert result["matches"] == ["ok.txt"]
    assert "Skipped 1 file(s)" in result["note"]
    assert ops.grep(ws, "needle", path=".packages")["matches"] == [".packages/lib.py"]


def test_grep_caps_results_and_fits_the_harness(ws):
    _write(ws, "many.txt", ("needle " + "z" * 290 + "\n") * 1000)
    result = ops.grep(ws, "needle", output_mode="lines")
    assert result["truncated"] and len(result["matches"]) <= ops.GREP_RESULTS
    assert "Showing the first" in result["note"]
    assert _fits(result)


def test_grep_bad_regex_is_a_plain_refusal(ws):
    with pytest.raises(OpError, match="regular expression") as caught:
        ops.grep(ws, "(unclosed")
    assert "backslash" in caught.value.fix


# -- shell (in-process; the runner tests cover timeouts and process trees) ---------------------


def test_shell_argv_on_windows_quotes_like_subprocess_shell_true():
    line = ops.shell_argv('echo "a b" & dir', windows=True, comspec=r"C:\Windows\system32\cmd.exe")
    assert line == r'C:\Windows\system32\cmd.exe /d /s /c "echo "a b" & dir"'
    assert ops.shell_argv("ls", windows=False, comspec=None) == ["/bin/sh", "-c", "ls"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_shell_splits_streams_and_reports_the_exit_code(ws):
    result = ops.shell(ws, "echo out; echo err >&2; exit 4", timeout_s=10, log_number=1)
    assert (result["exit_code"], result["stdout"], result["stderr"]) == (4, "out\n", "err\n")
    assert result["timed_out"] is False and result["truncated"] is False
    assert result["full_output"] is None and not (ws.work / ".logs" / "shell-1.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_shell_runs_in_the_workspace_with_an_empty_input(ws):
    result = ops.shell(ws, "pwd; cat; echo done", timeout_s=10, log_number=1)
    assert result["stdout"] == f"{ws.work}\ndone\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_long_output_keeps_its_start_and_end_and_the_full_log(ws):
    result = ops.shell(ws, "seq 1 100000; seq 1 3000 >&2", timeout_s=30, log_number=7)
    assert result["truncated"] is True and result["full_output"] == ".logs/shell-7.txt"
    assert result["stdout"].startswith("1\n2\n3\n") and result["stdout"].endswith("99999\n100000\n")
    assert "output cut here; the full output is in .logs/shell-7.txt" in result["stdout"]
    assert result["stderr"].endswith("3000\n")
    assert len(result["stdout"]) + len(result["stderr"]) <= ops.SHELL_OUTPUT_CHARS + 200
    log = (ws.work / ".logs" / "shell-7.txt").read_text()
    assert log.startswith("$ seq 1 100000") and "\n55555\n" in log
    assert _fits(result)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_output_full_of_escapes_still_fits(ws):
    """Control characters print as \\u0001 in JSON: six characters each."""
    command = f'{sys.executable} -c "import sys; sys.stdout.write(chr(1) * 50000)"'
    result = ops.shell(ws, command, timeout_s=30, log_number=2)
    assert result["truncated"] and _fits(result)
    assert json.loads(json.dumps(result))["stdout"].count("\x01") > 2000  # cut to fit, not to nothing


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_short_stdout_leaves_room_for_long_stderr(ws):
    result = ops.shell(ws, "echo ok; seq 1 20000 >&2", timeout_s=30, log_number=3)
    assert result["stdout"] == "ok\n"
    assert len(result["stderr"]) > ops.SHELL_OUTPUT_CHARS * 0.8


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")
def test_a_missing_log_folder_does_not_lose_the_result(ws):
    (ws.work / ".logs").write_text("a file where the folder should be")
    result = ops.shell(ws, "seq 1 100000", timeout_s=30, log_number=1)
    assert result["truncated"] and result["full_output"] is None
    assert "could not be saved" in result["stdout"]


# -- found in review (2026-10-22) -----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions")
def test_a_denied_path_is_a_plain_permission_error_not_a_crash(ws):
    """Python 3.12+ raises from is_dir() on a denied path; that must read as a denial."""
    locked = ws.work / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0)
    try:
        for call in (
            lambda: ops.read_file(ws, "locked/inner/x.txt"),
            lambda: ops.write_file(ws, "locked/inner/x.txt", "x"),
            lambda: ops.edit_file(ws, "locked/inner/x.txt", "a", "b"),
            lambda: ops.grep(ws, "x", path="locked/inner"),
            lambda: ops.glob(ws, "*", path="locked/inner"),
        ):
            with pytest.raises(OpError, match="Permission denied"):
                call()
    finally:
        locked.chmod(0o755)


def test_notes_never_echo_a_long_pattern_or_path(ws):
    pattern = "\x01" * 4096
    for result in (
        ops.grep(ws, pattern),
        ops.glob(ws, pattern),
        ops.glob(ws, "*", path="/".join(["\u200b" * 60] * 4)),
    ):
        assert _fits(result) and len(result["note"]) < 300


def test_a_long_path_counts_against_read_files_budget(ws):
    deep = "/".join(["d" * 200] * 4) + "/f.txt"
    _write(ws, deep, ("y" * 150 + "\n") * 1000)
    assert _fits(ops.read_file(ws, deep))


@pytest.mark.skipif(sys.platform != "linux", reason="only Linux file names can be any bytes")
def test_a_file_name_that_is_not_unicode_still_answers(ws):
    os.close(os.open(os.fsencode(ws.work) + b"/bad\xff.txt", os.O_CREAT | os.O_WRONLY))
    json.dumps(ops.glob(ws, "*"), ensure_ascii=False).encode("utf-8")


def test_shown_paths_are_always_valid_utf8(ws):
    assert ops.show(ws, ws.work / "bad\udcff.txt") == "bad?.txt"


def test_grep_numbers_lines_as_read_file_does(ws):
    _write(ws, "a.txt", "\ufeffTARGET\nform\x0cfeed\nTARGET\n")
    assert ops.grep(ws, "^TARGET", output_mode="lines")["matches"] == ["a.txt:1: TARGET", "a.txt:3: TARGET"]
    assert ops.read_file(ws, "a.txt")["content"].split("\n")[2] == "     3\tTARGET"


def test_many_double_stars_cost_little():
    import time as clock

    started = clock.monotonic()
    assert ops._matches(ops._parts("/".join(["**"] * 1200) + "/*.txt"), ("a",) * 12 + ("f.txt",))
    assert not ops._matches(ops._parts("**/a/**/b/**/c/**/d/**/e/**/*.zz"), ("a",) * 30 + ("f.txt",))
    assert clock.monotonic() - started < 1.0
    assert ops._parts("a/**/**/b") == ["a", "**", "b"]


def test_a_multi_line_command_is_refused_where_cmd_would_run_only_its_first_line():
    ops.one_line("dir && echo done\n")
    with pytest.raises(OpError, match="first line") as caught:
        ops.one_line("cd notes\npython build.py")
    assert "&&" in caught.value.fix
