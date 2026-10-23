"""Public-docs rules for the OSS mirror: history redaction and dead-link checks.

One module, three users:
- build-public-mirror.sh imports redact_file_info() as git-filter-repo's
  --file-info-callback, so every historical version of every *.md file loses
  the lines docs/oss/private/history-redactions.txt names;
- build-public-mirror.sh runs `check-ref` first: public HEAD must already be
  clean by hand, so the redaction only ever rewrites history, never what the
  owner reviewed;
- verify-public-mirror.sh runs `scan-history` and `dead-links` on the mirror.

Exit codes: 0 clean, 1 findings (printed), 2 usage or scan error.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

_RULES: list[re.Pattern[str]] | None = None


def load_rules(path: str | os.PathLike[str]) -> list[re.Pattern[str]]:
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    return [re.compile(line) for line in lines if line.strip() and not line.startswith("#")]


def _rules() -> list[re.Pattern[str]]:
    global _RULES
    if _RULES is None:
        _RULES = load_rules(os.environ["FLMCP_REDACTIONS"])
    return _RULES


def is_public_doc(path: str) -> bool:
    return path.endswith(".md")


def matching_lines(text: str, rules: list[re.Pattern[str]]) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), 1)
        if any(rule.search(line) for rule in rules)
    ]


def redact(data: bytes, rules: list[re.Pattern[str]]) -> bytes:
    kept = [
        line
        for line in data.splitlines(keepends=True)
        if not any(rule.search(line.decode("utf-8", "surrogateescape")) for rule in rules)
    ]
    return b"".join(kept)


def redact_file_info(filename, mode, blob_id, value):
    """git-filter-repo --file-info-callback: drop redacted lines from Markdown blobs."""
    if blob_id is None or not is_public_doc(filename.decode("utf-8", "surrogateescape")):
        return (filename, mode, blob_id)
    data = value.get_contents_by_identifier(blob_id)
    redacted = redact(data, _rules())
    if redacted != data:
        blob_id = value.insert_file_with_contents(redacted)
    return (filename, mode, blob_id)


def _git(repo: str, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", repo, *args], input=stdin, check=True, capture_output=True).stdout


def _blobs(repo: str, revs: list[str]) -> dict[str, str]:
    """Blob id -> one path it appears under, for Markdown blobs reachable from revs."""
    listing = _git(repo, "rev-list", "--objects", *revs).decode("utf-8", "surrogateescape")
    blobs: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if path and is_public_doc(path):
            blobs.setdefault(sha, path)
    return blobs


def _contents(repo: str, shas: list[str]):
    """Yield (sha, bytes) for each object via one `git cat-file --batch`."""
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{s}\n" for s in shas).encode())
    pos = 0
    for _ in shas:
        header_end = out.index(b"\n", pos)
        sha, kind, size = out[pos:header_end].split()
        start = header_end + 1
        end = start + int(size)
        if kind == b"blob":
            yield sha.decode(), out[start:end]
        pos = end + 1


def scan(repo: str, revs: list[str], rules: list[re.Pattern[str]]) -> list[str]:
    blobs = _blobs(repo, revs)
    findings = []
    for sha, data in _contents(repo, list(blobs)):
        for number, line in matching_lines(data.decode("utf-8", "surrogateescape"), rules):
            findings.append(f"{blobs[sha]}:{number} ({sha[:12]}): {line[:160]}")
    return sorted(findings)


def dead_links(root: pathlib.Path) -> list[str]:
    link = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    found = []
    for md in sorted(root.rglob("*.md")):
        if ".git" in md.relative_to(root).parts:
            continue
        for number, line in enumerate(md.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for target in link.findall(line):
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                path = target.split("#", 1)[0]
                if path and not (md.parent / path).exists():
                    found.append(f"{md.relative_to(root)}:{number}: {target}")
    return found


def main(argv: list[str]) -> int:
    usage = (
        "usage: public_docs.py check-ref <repo> <ref> <rules>\n"
        "       public_docs.py scan-history <repo> <rules>\n"
        "       public_docs.py dead-links <checkout>"
    )
    try:
        if len(argv) == 4 and argv[0] == "check-ref":
            findings = scan(argv[1], [argv[2], "--no-walk"], load_rules(argv[3]))
        elif len(argv) == 3 and argv[0] == "scan-history":
            findings = scan(argv[1], ["--all"], load_rules(argv[2]))
        elif len(argv) == 2 and argv[0] == "dead-links":
            findings = dead_links(pathlib.Path(argv[1]).resolve())
        else:
            print(usage, file=sys.stderr)
            return 2
    except (OSError, subprocess.CalledProcessError, re.error, ValueError) as error:
        print(f"public_docs.py: {error}", file=sys.stderr)
        return 2
    if findings:
        print("\n".join(findings))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
