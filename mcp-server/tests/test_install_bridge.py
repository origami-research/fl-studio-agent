"""fl-mcp-install's Bridge step: the checksum-verified release download and
--bridge PATH, both placing the library where FL loads it (fl/bridge.py)."""

import hashlib
import http.client
import urllib.error

import pytest

from fl_mcp.fl import bridge, install

DYLIB = "FL MCP Bridge_x64.dylib"
ASSET = bridge.RELEASE_ASSETS["darwin"]


@pytest.fixture
def fl(tmp_path, monkeypatch):
    """A macOS FL install under tmp_path, FL closed; returns the Bridge folder."""
    effects = tmp_path / "FL Studio.app" / "Contents" / "Resources" / "FL" / "Plugins" / "Fruity" / "Effects"
    effects.mkdir(parents=True)
    monkeypatch.setattr(bridge.sys, "platform", "darwin")
    monkeypatch.setattr(bridge, "bridge_plugin_dir", lambda: effects / "FL MCP Bridge")
    monkeypatch.setattr(bridge, "bridge_binary_name", lambda: DYLIB)
    monkeypatch.setattr(bridge, "fl_process_running", lambda: False)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr("fl_mcp.profile.detect_fl_version", lambda: None)
    monkeypatch.setattr(bridge, "__version__", "0.1.2")
    return effects / "FL MCP Bridge"


class Release:
    """A fake GitHub release: SHA256SUMS plus assets, recording every download."""

    def __init__(self, assets: dict[str, bytes], sums: dict[str, bytes] | None = None):
        self.assets = assets
        listed = sums if sums is not None else assets
        self.files = {"SHA256SUMS": "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in listed.items()).encode()}
        self.files.update(assets)
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return self.files[name]


def test_release_bridge_is_downloaded_verified_and_installed_from_the_matching_tag(fl):
    release = Release({ASSET: b"universal bridge"})

    step = bridge.install_release(fetch=release)

    assert step.status == "OK" and "(fl-studio-agent v0.1.2 release, installed)" in step.detail
    assert release.fetched == [f"{bridge.RELEASE_DOWNLOAD}/v0.1.2/SHA256SUMS", f"{bridge.RELEASE_DOWNLOAD}/v0.1.2/{ASSET}"]
    assert (fl / DYLIB).read_bytes() == b"universal bridge"
    assert (fl / "Plugin.nfo").read_text() == "ps_Vendorname=FL MCP\n"
    report = bridge.installed()
    assert report.installed and report.source == "fl-studio-agent v0.1.2 release"
    assert report.sha256 == hashlib.sha256(b"universal bridge").hexdigest()


def test_rerun_with_the_release_already_installed_downloads_only_the_checksums(fl):
    release = Release({ASSET: b"universal bridge"})
    bridge.install_release(fetch=release)
    release.fetched.clear()

    step = bridge.install_release(fetch=release)

    assert step.status == "OK" and "already current" in step.detail
    assert release.fetched == [f"{bridge.RELEASE_DOWNLOAD}/v0.1.2/SHA256SUMS"]


def test_a_current_bridge_reruns_without_writing_anything(fl, monkeypatch):
    """A rerun from a terminal that is not elevated (Windows Program Files is read-only
    there) must report the installed Bridge already current, not Permission denied:
    0.1.3 rewrote Plugin.nfo on every run and said "bridge needs you" (2026-10-27)."""
    release = Release({ASSET: b"universal bridge"})
    bridge.install_release(fetch=release)

    def read_only(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    record = fl / bridge.SOURCE_RECORD  # as Windows' text mode wrote it before this fix
    record.write_bytes(record.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(bridge.Path, "write_bytes", read_only)
    monkeypatch.setattr(bridge.Path, "write_text", read_only)
    step = bridge.install_release(fetch=release)

    assert step.status == "OK" and "already current" in step.detail
    assert (fl / "Plugin.nfo").read_bytes() == b"ps_Vendorname=FL MCP\n"  # byte-identical to before


def test_a_download_that_does_not_match_the_checksum_is_never_installed(fl):
    """A corrupt or truncated download must not reach FL's plugin folder."""
    release = Release({ASSET: b"truncated"}, sums={ASSET: b"the real bridge"})

    step = bridge.install_release(fetch=release)

    assert step.status == "FAIL" and "nothing was installed" in step.detail
    assert not (fl / DYLIB).exists()


def test_no_release_or_no_network_is_advice_with_the_build_yourself_path(fl):
    def offline(url):
        raise urllib.error.URLError("no network")

    for fetch in (offline, Release({})):
        step = bridge.install_release(fetch=fetch)
        assert step.status == "WARN" and "--bridge PATH" in step.detail
    assert not fl.exists()


def test_a_local_version_suffix_maps_to_its_release_tag(monkeypatch):
    monkeypatch.setattr(bridge, "__version__", "0.1.2+g1234abc")
    assert bridge.release_tag() == "v0.1.2"


def test_running_fl_is_advice_for_the_default_install_and_a_failure_for_an_explicit_one(fl, tmp_path, monkeypatch):
    """FL holds the library open. A default install says close FL and rerun; an
    install the user asked for with --bridge exits nonzero instead."""
    monkeypatch.setattr(bridge, "fl_process_running", lambda: True)
    assert bridge.install_release(fetch=Release({ASSET: b"x"})).status == "WARN"
    built = tmp_path / DYLIB
    built.write_bytes(b"mine")
    assert bridge.install_built(built).status == "FAIL"
    assert not fl.exists()


def test_install_built_places_your_library_and_refuses_the_wrong_one(fl, tmp_path):
    built = tmp_path / "build" / DYLIB
    built.parent.mkdir()
    built.write_bytes(b"bridge v1")

    first = bridge.install_built(built)
    again = bridge.install_built(built)

    assert first.status == "OK" and "installed" in first.detail
    assert again.status == "OK" and "already current" in again.detail
    assert bridge.installed().source == f"local build {built}"
    wrong = tmp_path / "FL MCP Bridge_x64.dll"
    wrong.write_bytes(b"dll")
    assert bridge.install_built(wrong).status == "FAIL"
    assert bridge.install_built(tmp_path / "missing" / DYLIB).status == "FAIL"


def test_unsupported_os_gets_no_download(monkeypatch):
    monkeypatch.setattr(bridge.sys, "platform", "linux")
    release = Release({})
    assert bridge.install_release(fetch=release).status == "WARN"
    assert release.fetched == []


def test_install_main_routes_the_bridge_flags(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(install, "install_agent_project", lambda: install.InstallStep(status="OK", detail="p"))
    monkeypatch.setattr(install, "install_note_script", lambda: install.InstallStep(status="OK", detail="n"))
    monkeypatch.setattr(install, "install_controller", lambda: install.InstallStep(status="OK", detail="c"))
    monkeypatch.setattr(install, "configure_midi", lambda: install.InstallStep(status="OK", detail="m"))
    monkeypatch.setattr(install.sys, "platform", "darwin")
    monkeypatch.setattr(install.bridge, "install_release", lambda: calls.append("release") or install.InstallStep(status="OK", detail="r"))
    monkeypatch.setattr(install.bridge, "install_built", lambda p: calls.append(p) or install.InstallStep(status="OK", detail="b"))

    assert "bridge" in install.install_all()
    assert "bridge" not in install.install_all(with_bridge=False)
    install.install_all(tmp_path / DYLIB)

    assert calls == ["release", tmp_path / DYLIB]


def test_a_cut_connection_is_advice_not_a_traceback(fl):
    """IncompleteRead is an http.client error, not an OSError; it must still become a WARN."""
    release = Release({ASSET: b"x"})

    def cut(url):
        if url.endswith(ASSET):
            raise http.client.IncompleteRead(b"par")
        return release(url)

    step = bridge.install_release(fetch=cut)

    assert step.status == "WARN"
    assert not fl.exists()


def test_an_upgrade_swaps_the_library_whole_and_leaves_no_partial_file(fl):
    bridge.install_release(fetch=Release({ASSET: b"v1"}))
    step = bridge.install_release(fetch=Release({ASSET: b"v2"}))

    assert step.status == "OK" and "installed" in step.detail
    assert (fl / DYLIB).read_bytes() == b"v2"
    assert sorted(p.name for p in fl.iterdir()) == [DYLIB, "Plugin.nfo", bridge.SOURCE_RECORD]


def test_a_contributors_local_build_is_kept_by_a_plain_rerun(fl, tmp_path):
    built = tmp_path / DYLIB
    built.write_bytes(b"my build")
    bridge.install_built(built)

    step = bridge.install_release(fetch=Release({ASSET: b"release build"}))

    assert step.status == "OK" and "kept" in step.detail
    assert (fl / DYLIB).read_bytes() == b"my build"


def test_an_already_current_bridge_needs_no_fl_restart(fl, monkeypatch):
    release = Release({ASSET: b"bridge"})
    bridge.install_release(fetch=release)
    monkeypatch.setattr(bridge, "fl_process_running", lambda: True)

    step = bridge.install_release(fetch=release)

    assert step.status == "OK" and "already current" in step.detail


def test_the_download_is_verified_by_the_os_trust_store(monkeypatch):
    """Windows fills its root store on demand, so Python's own check failed on a fresh
    user's machine (CERTIFICATE_VERIFY_FAILED for github.com, 2026-10-26 box run) and the
    Bridge was never installed. truststore asks the OS, which fetches the missing root."""
    import truststore

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"sums"

    def urlopen(url, *, timeout, context):
        seen.update(url=url, timeout=timeout, context=context)
        return Response()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)
    assert bridge._fetch("https://example.invalid/SHA256SUMS") == b"sums"
    assert isinstance(seen["context"], truststore.SSLContext)
    assert seen["timeout"] == bridge.DOWNLOAD_TIMEOUT_SECONDS


def test_a_bridge_left_for_the_user_is_not_reported_as_done(monkeypatch, capsys):
    """A WARN step (no Bridge: no render) must not end with "Next: open FL Studio"."""
    steps = {
        "controller_script": install.InstallStep(status="OK", detail="c"),
        "bridge": install.InstallStep(status="WARN", detail="could not download"),
    }
    monkeypatch.setattr(install, "install_all", lambda *a, **k: steps)
    install.main([])
    out = capsys.readouterr().out
    assert "Not finished: bridge needs you" in out and "Next:" not in out

    steps["bridge"] = install.InstallStep(status="OK", detail="installed")
    install.main([])
    assert install.NEXT_STEPS in capsys.readouterr().out
