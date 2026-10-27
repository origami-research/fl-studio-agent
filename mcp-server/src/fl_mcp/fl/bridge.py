"""Install the native Bridge plugin where FL loads it.

Two sources, one placement:

* the prebuilt Bridge from this package version's GitHub release (the default
  for ``fl-mcp-install``): the release workflow builds it for macOS (universal)
  and Windows x64 and publishes ``SHA256SUMS`` beside it; the download is
  installed only when its SHA-256 matches. The checksum catches a corrupt or
  truncated download; authenticity rests on GitHub's HTTPS, as for any release
  asset (the binaries are not code-signed).
* a library you built yourself (``fl-mcp-install --bridge PATH``).

FL loads native plugins only from inside its own install: the app bundle on
macOS (group writable; FL ships with library validation disabled, so it loads
an ad-hoc-signed dylib) and Program Files on Windows (an elevated terminal).
FL must be closed: it holds the library open while running.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import ssl
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

import truststore

from fl_mcp import __version__
from fl_mcp.schemas import BridgeInstall

from .paths import bridge_binary_name, bridge_plugin_dir
from .process import fl_process_running
from .steps import InstallStep

RELEASE_DOWNLOAD = "https://github.com/origami-research/fl-studio-agent/releases/download"
# Asset names the release workflow (.github/workflows/release.yml) publishes.
RELEASE_ASSETS = {"darwin": "fl-mcp-bridge-macos-universal.dylib", "win32": "fl-mcp-bridge-windows-x64.dll"}
CHECKSUMS_ASSET = "SHA256SUMS"
# Beside the library: where it came from and its hash, for fl-mcp-doctor.
SOURCE_RECORD = "bridge_source.txt"
DOWNLOAD_TIMEOUT_SECONDS = 60
# A download can fail as an OSError (URLError, SSL, timeouts) or as an http.client error
# (IncompleteRead on a cut connection), which is not an OSError.
DOWNLOAD_ERRORS = (OSError, http.client.HTTPException)

Fetch = Callable[[str], bytes]


def release_tag() -> str:
    """The release this package version's Bridge comes from (a local version suffix is dropped)."""
    return "v" + __version__.split("+", 1)[0]


def _fetch(url: str) -> bytes:
    """Download over HTTPS verified by the OS, as a browser does. Python's own check sees
    only the roots already in Windows' store, which Windows fills on demand: a machine
    that never opened github.com in Edge (Chrome keeps its own roots) lacks GitHub's
    root and failed with CERTIFICATE_VERIFY_FAILED (2026-10-26, a fresh Windows user)."""
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, context=context) as response:
        return response.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def installed() -> BridgeInstall:
    """What is in FL's plugin folder now; never changes anything."""
    from fl_mcp.profile import installed_bridge_build

    library = bridge_plugin_dir() / bridge_binary_name()
    if not library.is_file():
        return BridgeInstall(installed=False, path=str(library))
    try:
        source = (bridge_plugin_dir() / SOURCE_RECORD).read_text(encoding="utf-8").partition("\n")[0].strip() or None
    except OSError:
        source = None
    return BridgeInstall(
        installed=True,
        path=str(library),
        source=source,
        sha256=_sha256(library.read_bytes()),
        installed_for=installed_bridge_build(),
    )


def _needs_user(detail: str, *, explicit: bool) -> InstallStep:
    """A step the user must act on: a failure when they asked for this install, advice otherwise."""
    return InstallStep(status="FAIL" if explicit else "WARN", detail=detail)


def _place(data: bytes, source: str, *, explicit: bool) -> InstallStep:
    dest_dir = bridge_plugin_dir()
    library = dest_dir / bridge_binary_name()
    digest = _sha256(data)
    current = library.is_file() and _sha256(library.read_bytes()) == digest
    if not current and fl_process_running():
        return _needs_user("FL Studio is running and holds the Bridge open; close it, then rerun fl-mcp-install",
                           explicit=explicit)
    if not dest_dir.parent.is_dir():
        return _needs_user(f"FL Studio's native Effects folder not found: {dest_dir.parent} "
                           "(set FL_APP to FL Studio's app or FL64.exe if it is installed elsewhere)", explicit=explicit)
    from fl_mcp.profile import BRIDGE_BUILD_RECORD, detect_fl_version

    try:
        dest_dir.mkdir(exist_ok=True)
        if not current:
            # A new file swapped in whole: a partial write never sits where FL loads it, and
            # macOS never sees a signed library rewritten in place (it kills such a load).
            staged = dest_dir / f".{bridge_binary_name()}.partial"
            staged.write_bytes(data)
            staged.replace(library)
        # Capital V matches the field in stock plugins' Plugin.nfo (e.g. Fruity Balance).
        (dest_dir / "Plugin.nfo").write_bytes(b"ps_Vendorname=FL MCP\n")
        (dest_dir / SOURCE_RECORD).write_text(f"{source}\nsha256 {digest}\n", encoding="utf-8")
        fl_build = detect_fl_version()  # macOS only; the doctor compares it with the running FL
        if fl_build is not None:
            (dest_dir / BRIDGE_BUILD_RECORD).write_text(f"{fl_build.label()}\n", encoding="utf-8")
    except OSError as exc:
        with contextlib.suppress(OSError):
            (dest_dir / f".{bridge_binary_name()}.partial").unlink(missing_ok=True)
        if sys.platform == "win32":
            hint = "run fl-mcp-install from a terminal opened as administrator (FL's plugins live in Program Files)"
        else:
            hint = ("allow your terminal app under System Settings > Privacy & Security > App Management "
                    "(macOS guards changes inside other apps), then rerun")
        return _needs_user(f"cannot write {dest_dir} ({exc}); {hint}", explicit=explicit)
    if sys.platform == "darwin":
        # A library that came through a browser keeps the quarantine flag; FL would refuse it.
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(library)], capture_output=True, check=False)
    if current:
        return InstallStep(status="OK", detail=f"{library} ({source}, already current)")
    return InstallStep(status="OK", detail=f"{library} ({source}, installed); restart FL Studio to load it")


def install_built(built: Path) -> InstallStep:
    """Install a Bridge library you built (``--bridge PATH``)."""
    if not built.is_file():
        return InstallStep(status="FAIL", detail=f"no Bridge library at {built}")
    if built.name != bridge_binary_name():
        return InstallStep(status="FAIL", detail=f"{built.name} is not this OS's Bridge library ({bridge_binary_name()})")
    return _place(built.read_bytes(), f"local build {built}", explicit=True)


def install_release(fetch: Fetch = _fetch) -> InstallStep:
    """Download this version's prebuilt Bridge, verify it against the release's SHA256SUMS, install it."""
    asset = RELEASE_ASSETS.get(sys.platform)
    if asset is None:
        return InstallStep(status="WARN", detail=f"no prebuilt Bridge for {sys.platform}; FL Studio runs on macOS and Windows")
    tag = release_tag()
    base = f"{RELEASE_DOWNLOAD}/{tag}"
    build_hint = "rerun later, or build it yourself and use --bridge PATH (SETUP.md, 'Build the Bridge yourself')"
    try:
        sums = fetch(f"{base}/{CHECKSUMS_ASSET}").decode("utf-8")
    except (*DOWNLOAD_ERRORS, UnicodeDecodeError) as exc:
        return InstallStep(status="WARN", detail=f"could not download {base}/{CHECKSUMS_ASSET} ({exc}); {build_hint}")
    expected = next((line.split()[0].lower() for line in sums.splitlines()
                     if len(line.split()) == 2 and line.split()[1].lstrip("*") == asset), None)
    if expected is None:
        return InstallStep(status="WARN", detail=f"release {tag} lists no {asset}; {build_hint}")
    library = bridge_plugin_dir() / bridge_binary_name()
    if library.is_file():
        if _sha256(library.read_bytes()) == expected:
            return _place(library.read_bytes(), f"fl-studio-agent {tag} release", explicit=False)
        mine = installed().source or ""
        if mine.startswith("local build"):
            # A contributor's own build is kept: rerunning the install to refresh the scripts
            # must not swap it for the release.
            return InstallStep(status="OK", detail=f"{library} ({mine}, kept; delete {library.parent} "
                               "and rerun to switch to the release build)")
    try:
        data = fetch(f"{base}/{asset}")
    except DOWNLOAD_ERRORS as exc:
        return InstallStep(status="WARN", detail=f"could not download {base}/{asset} ({exc}); {build_hint}")
    if _sha256(data) != expected:
        return InstallStep(status="FAIL", detail=f"{asset} from {tag} has SHA-256 {_sha256(data)}, "
                           f"but the release's {CHECKSUMS_ASSET} says {expected}; nothing was installed")
    return _place(data, f"fl-studio-agent {tag} release", explicit=False)
