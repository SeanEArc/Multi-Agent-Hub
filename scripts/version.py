"""Keep the app's version the same everywhere, and read release notes from CHANGELOG.md.

The version is written in several files (the backend, the UI, the Tauri shell and their lockfiles).
Releases are tags like v0.2.0 on main, and the release workflow refuses a tag that doesn't match.

    python scripts/version.py check                 # every file has the same version
    python scripts/version.py check --expect 0.2.0  # ... and it is 0.2.0 (used by the release workflow)
    python scripts/version.py set 0.2.0             # write 0.2.0 into every file
    python scripts/version.py notes 0.2.0           # print the CHANGELOG.md section for 0.2.0

Standard library only, so it runs on any Python 3.11+ without installing anything.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

# Semantic version, with an optional prerelease part (0.2.0, 0.2.0-beta.1). No build metadata.
VERSION_RE = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"


class Spot:
    """One place a version is written: a file and a pattern whose group 2 is the version."""

    def __init__(self, path: str, pattern: str, count: int = 1) -> None:
        self.path = ROOT / path
        self.label = path
        self.pattern = re.compile(pattern, re.MULTILINE)
        self.count = count

    def read(self) -> list[str]:
        text = self.path.read_text(encoding="utf-8")
        found = [m.group(2) for m in self.pattern.finditer(text)]
        if len(found) != self.count:
            raise SystemExit(f"{self.label}: expected {self.count} version line(s), found {len(found)}")
        return found

    def write(self, version: str) -> None:
        raw = self.path.read_bytes().decode("utf-8")
        new, n = self.pattern.subn(lambda m: m.group(1) + version + m.group(3), raw)
        if n != self.count:
            raise SystemExit(f"{self.label}: expected {self.count} version line(s), found {n}")
        self.path.write_bytes(new.encode("utf-8"))


SPOTS = [
    Spot("backend/pyproject.toml", rf'(^version = ")({VERSION_RE})(")'),
    Spot("backend/app/main.py", rf'(FastAPI\(title="Multi-Agent Hub", version=")({VERSION_RE})(")'),
    Spot("backend/uv.lock", rf'(^name = "multi-agent-hub-backend"\r?\nversion = ")({VERSION_RE})(")'),
    Spot("frontend/package.json", rf'(^  "version": ")({VERSION_RE})(")'),
    # The lockfile repeats the root package's version under packages[""].
    Spot("frontend/package-lock.json",
         rf'(^\s*"name": "multi-agent-hub-frontend",\r?\n\s*"version": ")({VERSION_RE})(")', count=2),
    Spot("frontend/src-tauri/tauri.conf.json", rf'(^  "version": ")({VERSION_RE})(")'),
    Spot("frontend/src-tauri/Cargo.toml", rf'(^version = ")({VERSION_RE})(")'),
    Spot("frontend/src-tauri/Cargo.lock", rf'(^name = "multi-agent-hub"\r?\nversion = ")({VERSION_RE})(")'),
]


def versions() -> dict[str, str]:
    found = {}
    for spot in SPOTS:
        for i, version in enumerate(spot.read()):
            found[spot.label if i == 0 else f"{spot.label} (#{i + 1})"] = version
    return found


def check(expect: str | None) -> int:
    found = versions()
    distinct = set(found.values())
    if len(distinct) != 1:
        print("Versions differ:", file=sys.stderr)
        for label, version in found.items():
            print(f"  {version:<16} {label}", file=sys.stderr)
        print("Fix with: python scripts/version.py set <version>", file=sys.stderr)
        return 1
    (version,) = distinct
    if expect is not None and version != expect:
        print(f"Files say {version}, but {expect} was expected (does the tag match?).", file=sys.stderr)
        print(f"Fix with: python scripts/version.py set {expect}", file=sys.stderr)
        return 1
    print(f"All {len(found)} version lines say {version}.")
    return 0


def set_version(version: str) -> int:
    if not re.fullmatch(VERSION_RE, version):
        print(f"Not a version like 1.2.3 or 1.2.3-beta.1: {version}", file=sys.stderr)
        return 1
    for spot in SPOTS:
        spot.write(version)
    print(f"Set {version} in {len(SPOTS)} files. Add a '## [{version}]' section to CHANGELOG.md.")
    return check(version)


def changelog_section(version: str) -> str | None:
    """The body of the '## [version]' section of CHANGELOG.md, or None when there isn't one."""
    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    if match is None:
        return None
    # Drop link reference definitions ([0.2.0]: https://...) that trail the last section.
    body = re.sub(r"^\[[^\]]+\]: \S+\s*$", "", match.group(1), flags=re.MULTILINE)
    return body.strip()


def notes(version: str) -> int:
    body = changelog_section(version)
    if not body:
        print(f"CHANGELOG.md has no '## [{version}]' section with notes.", file=sys.stderr)
        return 1
    print(body)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check_cmd = commands.add_parser("check", help="every file has the same version")
    check_cmd.add_argument("--expect", help="the version they must all have")
    commands.add_parser("set", help="write a version into every file").add_argument("version")
    commands.add_parser("notes", help="print a version's CHANGELOG.md section").add_argument("version")
    args = parser.parse_args()
    if args.command == "check":
        return check(args.expect)
    if args.command == "set":
        return set_version(args.version)
    return notes(args.version)


if __name__ == "__main__":
    sys.exit(main())
