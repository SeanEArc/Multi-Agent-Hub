"""Write THIRD-PARTY-NOTICES.md and licenses/ for everything the installer ships, and check them at release.

The installer bundles the backend (PyInstaller: Python and the backend's runtime packages), the UI (npm packages
built by Vite), the desktop shell (Rust crates) and the NSIS installer stub. Most of those licenses ask for their
text and copyright notices to go with every copy, and Apache-2.0 also asks for each NOTICE file's text, so this
script lists every shipped package with its version, license, authors and source, and copies each package's own
license files into licenses/<section>/<name>/.

    uv run --project backend python scripts/notices.py generate   # rewrite THIRD-PARTY-NOTICES.md and licenses/
    uv run --project backend python scripts/notices.py check      # fail if a shipped package is missing or not allowed

Run it from the backend's environment (it reads the installed Python packages' metadata), after `npm ci` in
frontend/, with `cargo` on PATH (the shell's crate list comes from `cargo metadata`). `generate` also downloads the
license files of Rust crates compiled into other packages' binaries (BetterOffice's WebAssembly and Python modules,
pydantic-core, ...) from crates.io, cached in the system temp folder.

`check` (the release workflow runs it) compares the shipped packages with the committed THIRD-PARTY-NOTICES.md by
name and license, so a dependency update that only bumps versions passes with a warning, while a new package, a
changed license, a removed entry or a license outside ALLOWED fails.
"""

import argparse
import importlib.metadata as md
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SHELL = FRONTEND / "src-tauri"
DATA = Path(__file__).resolve().parent / "notices_data"
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"
LICENSES = ROOT / "licenses"
TARGET = "x86_64-pc-windows-msvc"

# Licenses an MIT app can ship (each OR branch or AND part is checked separately).
ALLOWED = {
    "0BSD", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "BSL-1.0", "CC0-1.0", "CC-BY-4.0", "ISC", "MIT", "MIT-0",
    "OFL-1.1", "PSF-2.0", "Python-2.0", "Unicode-3.0", "Unicode-DFS-2016", "Unlicense", "Zlib", "bzip2-1.0.6",
    # File-level copyleft: fine to ship unmodified, with the license text and a link to the source (both given).
    "MPL-2.0",
}
# Licenses accepted only for one package, with the reason.
EXCEPTIONS = {
    ("other", "PyInstaller bootloader"): "the bootloader exception lets programs built with it use any license",
    ("other", "NSIS"): "LZMA module: its special exception lets installers link it without the CPL's terms",
}
# Package metadata that isn't an SPDX expression, rewritten to one (the license files are still copied as shipped).
LICENSE_FIXES = {
    ("python", "xlrd"): "BSD-3-Clause",
    ("python", "pypdfium2"): "BSD-3-Clause AND Apache-2.0 AND CC-BY-4.0",
}
# Packages with no license file of their own: use a sibling package's (same project, same license).
BORROW = {
    ("rust", "alloc-stdlib"): ("rust", "alloc-no-stdlib"),
    ("rust", "selectors"): ("rust", "cssparser"),  # both Servo, MPL-2.0 (the text is the standard one)
    ("other", "Tauri NSIS plugin"): ("rust", "tauri"),  # same project and license
}
# Packages with no license file of their own whose upstream license is kept in scripts/notices_data/.
VENDORED = {
    ("rust", "webview2-com"): "webview2-rs-LICENSE.txt",
    ("rust", "webview2-com-sys"): "webview2-rs-LICENSE.txt",
}
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|third[-_]party[-_]notices|authors|copyright)"
                          r"([-_.][\w.-]*)?$", re.I)
CODE_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".map", ".json", ".rs", ".py", ".html", ".d"}

SECTIONS = {
    "python": "Python packages (backend)",
    "npm": "npm packages (user interface)",
    "rust": "Rust crates (desktop shell)",
    "embedded": "Rust crates compiled into other packages",
    "other": "Other",
}


@dataclass
class Entry:
    section: str
    name: str
    version: str
    license: str
    authors: str = ""
    url: str = ""
    found_in: str = ""  # embedded crates: which binaries
    files: dict[str, Path] = field(default_factory=dict)  # name under licenses/<section>/<name>/ -> source
    texts: dict[str, str] = field(default_factory=dict)  # generated files (MIT template)

    @property
    def key(self) -> tuple[str, str]:
        return self.section, self.name

    @property
    def folder(self) -> str:
        return f"licenses/{self.section}/{safe(self.name)}"


def safe(name: str) -> str:
    return re.sub(r"[^\w@.+-]", "-", name).replace("@", "")


# ---- license expressions -------------------------------------------------------------------------------------

def normalize(expr: str) -> str:
    """Cargo's older `MIT/Apache-2.0` means OR."""
    return re.sub(r"\s*/\s*", " OR ", expr.strip())


def allowed(expr: str, key: tuple[str, str] | None = None) -> bool:
    """True when the expression can be met with ALLOWED licenses (any OR branch, every AND part)."""
    if key in EXCEPTIONS:
        return True
    tokens = re.findall(r"\(|\)|[^\s()]+", normalize(expr))
    pos = 0

    def parse_or() -> bool:
        nonlocal pos
        ok = parse_and()
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            pos += 1
            ok = parse_and() or ok
        return ok

    def parse_and() -> bool:
        nonlocal pos
        ok = parse_atom()
        while pos < len(tokens) and tokens[pos].upper() == "AND":
            pos += 1
            ok = parse_atom() and ok
        return ok

    def parse_atom() -> bool:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError(f"unfinished license expression: {expr!r}")
        token = tokens[pos]
        pos += 1
        if token == "(":
            ok = parse_or()
            if pos >= len(tokens) or tokens[pos] != ")":
                raise ValueError(f"unbalanced license expression: {expr!r}")
            pos += 1
            return ok
        if pos < len(tokens) and tokens[pos].upper() == "WITH":
            pos += 2  # an exception only adds permissions
        return token.removesuffix("+") in ALLOWED

    try:
        ok = parse_or()
        return ok and pos == len(tokens)
    except ValueError:
        return False


# ---- collecting what ships -----------------------------------------------------------------------------------

def license_files(folder: Path, depth: int = 2) -> dict[str, Path]:
    """License, notice and authors files in a package folder (and LICENSES/ folders, and e.g. pdf.js's wasm/)."""
    found: dict[str, Path] = {}

    def walk(path: Path, rel: str, level: int) -> None:
        for item in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            name = f"{rel}{item.name}"
            if item.is_dir():
                if item.name.lower() in ("licenses", "license", "build_licenses"):  # at any level walked
                    for sub in sorted(item.rglob("*")):
                        if sub.is_file():
                            found[f"{name}/{sub.relative_to(item).as_posix()}"] = sub
                elif item.name not in ("node_modules", ".git", "src", "tests", "test") and level < depth:
                    walk(item, f"{name}/", level + 1)
            elif LICENSE_FILE.match(item.name) and item.suffix.lower() not in CODE_SUFFIXES:
                found[name] = item

    walk(folder, "", 1)
    return found


def mit_text(authors: str) -> str:
    template = (DATA / "MIT.txt").read_text(encoding="utf-8")
    return ("This package ships no license file; this is the MIT license it declares, with its authors as the "
            "copyright holders.\n\n" + template.replace("{holder}", authors))


def python_entries() -> list[Entry]:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    seen: dict[str, md.Distribution] = {}
    extras_done: set[tuple[str, str]] = set()

    def walk(name: str, extras: tuple[str, ...] = ()) -> None:
        dist = md.distribution(name)
        key = canonicalize_name(dist.metadata["Name"])
        seen.setdefault(key, dist)
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker and not any(req.marker.evaluate({"extra": e}) for e in (extras or ("",))):
                continue
            child = canonicalize_name(req.name)
            todo = [e for e in req.extras if (child, e) not in extras_done]
            if child in seen and not todo:
                continue
            extras_done.update((child, e) for e in req.extras)
            walk(req.name, tuple(req.extras))

    walk("multi-agent-hub-backend")
    entries = []
    for key, dist in sorted(seen.items()):
        if key == "multi-agent-hub-backend":
            continue
        meta = dist.metadata
        lic = meta.get("License-Expression") or meta.get("License") or ""
        authors = meta.get("Author") or ""
        if not authors and meta.get("Author-email"):
            authors = re.sub(r"\s*<[^>]*>", "", meta["Author-email"])
        if not authors:
            authors = re.sub(r"\s*<[^>]*>", "", meta.get("Maintainer") or meta.get("Maintainer-email") or "")
        urls = dict(u.split(", ", 1) for u in meta.get_all("Project-URL") or [] if ", " in u)
        url = next((urls[k] for k in urls if k.lower() in ("source", "source code", "repository", "homepage",
                                                            "home", "github")), "") or meta.get("Home-page") or ""
        files: dict[str, Path] = {}
        for f in dist.files or []:
            parts = Path(str(f)).parts
            if not parts[0].endswith(".dist-info") or len(parts) < 2:
                continue
            inside_licenses = parts[1] == "licenses"
            if inside_licenses or (len(parts) == 2 and LICENSE_FILE.match(parts[1])):
                rel = "/".join(parts[2:] if inside_licenses else parts[1:])
                files[rel] = Path(dist.locate_file(f))
        entries.append(Entry("python", meta["Name"], dist.version, lic, authors, url, files=files))
    return entries


def npm_license(value) -> str:
    """package.json's license: an SPDX string, or in older packages {type} or a `licenses` list."""
    if isinstance(value, dict):
        return str(value.get("type", ""))
    if isinstance(value, list):
        return " OR ".join(npm_license(v) for v in value if npm_license(v))
    return value if isinstance(value, str) else ""


def npm_paths(lock: dict) -> list[tuple[str, str, dict]]:
    """(path, name, lock entry) of every package that reaches the UI build."""
    found = []
    for path, info in sorted(lock["packages"].items()):
        # Dev tools, optional platform binaries (pdf.js's Node-only canvas) and type packages never reach the build.
        if not path or info.get("dev") or info.get("devOptional") or info.get("optional"):
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        if not name.startswith("@types/"):
            found.append((path, name, info))
    return found


def npm_entries(lock: dict) -> list[Entry]:
    entries = []
    for path, name, info in npm_paths(lock):
        folder = FRONTEND / path
        if not folder.is_dir():
            raise SystemExit(f"{path} isn't installed: run `npm ci` in frontend/ first")
        pkg = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        author = pkg.get("author") or ""
        if isinstance(author, dict):
            author = author.get("name", "")
        author = re.sub(r"\s*[<(][^>)]*[>)]", "", author)
        repo = pkg.get("repository") or ""
        if isinstance(repo, dict):
            repo = repo.get("url", "")
        repo = re.sub(r"^git\+|\.git$", "", repo).replace("git://", "https://")
        if repo and "://" not in repo:
            repo = "https://github.com/" + repo.removeprefix("github:")
        url = repo or pkg.get("homepage") or f"https://www.npmjs.com/package/{name}"
        version = info.get("version", pkg.get("version", ""))
        lic = npm_license(info.get("license")) or npm_license(pkg.get("license")) or npm_license(pkg.get("licenses"))
        entries.append(Entry("npm", name, version, lic, author, url, files=license_files(folder)))
    return merge_versions(entries)  # the same package nested at another version


def cargo_metadata() -> dict:
    out = subprocess.run(["cargo", "metadata", "--format-version", "1", "--filter-platform", TARGET, "--locked"],
                         cwd=SHELL, capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise SystemExit(f"cargo metadata failed:\n{out.stderr}")
    return json.loads(out.stdout)


def rust_entries() -> list[Entry]:
    meta = cargo_metadata()
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root = meta["resolve"]["root"]
    shipped, stack = set(), [root]
    while stack:  # normal dependencies only; proc macros and build scripts run at compile time and don't ship
        pid = stack.pop()
        if pid in shipped:
            continue
        pkg = packages[pid]
        if pid != root and any("proc-macro" in t["kind"] for t in pkg["targets"]):
            continue
        shipped.add(pid)
        for dep in nodes[pid]["deps"]:
            if any(k["kind"] is None for k in dep["dep_kinds"]):
                stack.append(dep["pkg"])
    entries = []
    for pid in sorted(shipped - {root}, key=lambda i: (packages[i]["name"], packages[i]["version"])):
        pkg = packages[pid]
        folder = Path(pkg["manifest_path"]).parent
        files = license_files(folder, depth=1)
        if pkg.get("license_file") and (folder / pkg["license_file"]).is_file():
            lf = folder / pkg["license_file"]
            files.setdefault(lf.name, lf)
        authors = ", ".join(re.sub(r"\s*<[^>]*>", "", a) for a in pkg["authors"][:4])
        if len(pkg["authors"]) > 4:
            authors += " and others"
        entries.append(Entry("rust", pkg["name"], pkg["version"], pkg["license"] or "", authors,
                             pkg.get("repository") or pkg.get("homepage") or f"https://crates.io/crates/{pkg['name']}",
                             files=files))
    return merge_versions(entries)


def merge_versions(entries: list[Entry]) -> list[Entry]:
    """One row per name. When the shipped versions' licenses differ, every one must be allowed (joined with AND)
    and each version's license files are kept under a folder named by its version."""
    groups: dict[str, list[Entry]] = {}
    for e in entries:
        groups.setdefault(e.name, []).append(e)
    merged = []
    for group in groups.values():
        first = group[0]
        licenses = list(dict.fromkeys(e.license for e in group))
        if len(licenses) > 1:
            first.license = " AND ".join(f"({lic})" for lic in licenses)
            first.files = {f"{e.version}/{n}": p for e in group for n, p in e.files.items()}
            first.texts = {f"{e.version}/{n}": t for e in group for n, t in e.texts.items()}
        first.version = ", ".join(dict.fromkeys(e.version for e in group))
        merged.append(first)
    return merged


CRATE_PATH = re.compile(rb"(?:index\.crates\.io-[0-9a-f]+|registry[/\\]src[/\\][^/\\]+)[/\\]"
                        rb"([A-Za-z0-9_-]+?)-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)[/\\]")


def binaries(python: list[Entry], lock: dict) -> list[tuple[str, Path]]:
    """Compiled files of shipped packages that may hold Rust code: Python extension modules and WebAssembly."""
    found = []
    for e in python:
        dist = md.distribution(e.name)
        for f in dist.files or []:
            if str(f).endswith(".pyd"):
                found.append((f"{e.name} ({Path(str(f)).name})", Path(dist.locate_file(f))))
    for path, name, _ in npm_paths(lock):
        for wasm in sorted((FRONTEND / path).rglob("*.wasm")):
            found.append((f"{name} ({wasm.name})", wasm))
    return found


def embedded_crates(python: list[Entry], lock: dict) -> dict[str, dict]:
    """Rust crates whose source paths appear in shipped binaries (from panic messages, so a lower bound)."""
    crates: dict[str, dict] = {}
    for label, path in binaries(python, lock):
        for name, version in set(CRATE_PATH.findall(path.read_bytes())):
            info = crates.setdefault(name.decode(), {"versions": set(), "in": set()})
            info["versions"].add(version.decode())
            info["in"].add(label.split(" (")[0])
    return crates


def version_key(v: str) -> tuple:
    """Sorts 1.0.0-rc.1 before 1.0.0."""
    release, _, pre = v.partition("-")
    return tuple(int(x) for x in release.split(".")), not pre, pre


def crate_folder(name: str, version: str) -> Path:
    """The crate's source: the local cargo registry, else a download from crates.io (cached)."""
    home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    for src in sorted((home / "registry" / "src").glob("*")):
        if (src / f"{name}-{version}").is_dir():
            return src / f"{name}-{version}"
    cache = Path(tempfile.gettempdir()) / "MultiAgentHub-notices" / f"{name}-{version}"
    if not (cache / "Cargo.toml").exists():
        url = f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "notices-script"}),
                                    timeout=60) as resp:
            data = resp.read()
        cache.parent.mkdir(parents=True, exist_ok=True)
        archive = cache.parent / f"{name}-{version}.crate"
        archive.write_bytes(data)
        with tarfile.open(archive) as tar:
            wanted = [m for m in tar.getmembers() if m.isfile() and m.name.count("/") == 1
                      and (LICENSE_FILE.match(m.name.split("/")[1]) or m.name.endswith("/Cargo.toml"))]
            tar.extractall(cache.parent, members=wanted, filter="data")
        archive.unlink()
    return cache


def embedded_entries(found: dict[str, dict]) -> list[Entry]:
    entries = []
    for name, version in sorted((n, v) for n in found for v in found[n]["versions"]):
        folder = crate_folder(name, version)
        pkg = tomllib.loads((folder / "Cargo.toml").read_text(encoding="utf-8"))["package"]
        authors = pkg.get("authors", [])
        authors = authors if isinstance(authors, list) else []
        files = license_files(folder, depth=1)
        if isinstance(pkg.get("license-file"), str) and (folder / pkg["license-file"]).exists():
            files.setdefault(Path(pkg["license-file"]).name, folder / pkg["license-file"])
        lic = pkg.get("license") if isinstance(pkg.get("license"), str) else ""
        repo = pkg.get("repository") if isinstance(pkg.get("repository"), str) else ""
        entries.append(Entry("embedded", name, version, lic,
                             ", ".join(re.sub(r"\s*<[^>]*>", "", a) for a in authors[:4]),
                             repo or f"https://crates.io/crates/{name}", ", ".join(sorted(found[name]["in"])),
                             files=files))
    entries.sort(key=lambda e: (e.name, version_key(e.version)))
    return merge_versions(entries)


def other_entries() -> list[Entry]:
    base = Path(sys.base_prefix)
    pyinstaller = md.distribution("pyinstaller")
    copying = next(Path(pyinstaller.locate_file(f)) for f in pyinstaller.files or [] if str(f).endswith("COPYING.txt"))
    return [
        # The runtime's own LICENSE.txt (python-build-standalone) lacks the texts of the libraries it bundles.
        Entry("other", "Python", platform.python_version(),
              "PSF-2.0 AND Apache-2.0 AND MIT AND Zlib AND BSD-2-Clause AND bzip2-1.0.6",
              "Python Software Foundation", "https://www.python.org",
              files={"LICENSE.txt": base / "LICENSE.txt", "LIBRARIES.txt": DATA / "python-libraries.txt"}),
        Entry("other", "PyInstaller bootloader", pyinstaller.version,
              "GPL-2.0-or-later WITH Bootloader-exception", "PyInstaller Development Team",
              "https://github.com/pyinstaller/pyinstaller", files={"COPYING.txt": copying}),
        Entry("other", "NSIS", "3", "Zlib AND bzip2-1.0.6 AND CPL-1.0", "NSIS contributors",
              "https://nsis.sourceforge.io", files={"COPYING.txt": DATA / "NSIS-COPYING.txt"}),
        # nsis_tauri_utils.dll, which Tauri's NSIS template puts in every installer.
        Entry("other", "Tauri NSIS plugin", "bundled by the Tauri CLI", "MIT OR Apache-2.0",
              "Tauri Programme within The Commons Conservancy", "https://github.com/tauri-apps/nsis-tauri-utils"),
    ]


def collect(download: bool) -> list[Entry]:
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    python, npm = python_entries(), npm_entries(lock)
    found = embedded_crates(python, lock)
    embedded = (embedded_entries(found) if download else
                [Entry("embedded", n, ", ".join(sorted(i["versions"], key=version_key)), "") for n, i in found.items()])
    entries = python + npm + rust_entries() + embedded + other_entries()
    for e in entries:
        e.license = LICENSE_FIXES.get(e.key, e.license)  # the only place fixes apply
    if download:
        by_key = {e.key: e for e in entries}
        for e in entries:
            if e.files:
                continue
            if e.key in VENDORED:
                e.files = {"LICENSE.txt": DATA / VENDORED[e.key]}
            elif e.key in BORROW:
                e.files = dict(by_key[BORROW[e.key]].files)
            elif re.search(r"\bMIT\b", e.license) and e.authors:
                e.texts["LICENSE-MIT.txt"] = mit_text(e.authors)
    return entries


# ---- writing and checking ------------------------------------------------------------------------------------

def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render(entries: list[Entry]) -> str:
    out = [
        "# Third-party notices",
        "",
        "Multi-Agent Hub is released under the MIT license (`LICENSE`). Its installer also ships the open-source "
        "software listed here: the backend's Python packages and the Python runtime (bundled with PyInstaller), the "
        "user interface's npm packages, the desktop shell's Rust crates and the NSIS installer. Each entry's license "
        "files, copied as the package ships them, are in the folder named in its row under `licenses/`. Packages "
        "are used unmodified.",
        "",
        "Generated by `scripts/notices.py` from `backend/uv.lock`, `frontend/package-lock.json` and "
        "`frontend/src-tauri/Cargo.lock`; don't edit it by hand.",
        "",
        "## BetterOffice",
        "",
        "Document support (the Excel engine for recalculating and editing workbooks, the Word structure export behind "
        "the formatting report, and the preview pane's Word and Excel viewers) comes from "
        "[BetterOffice](https://github.com/openooxml/betteroffice) by Elia Hilse and The OpenOOXML Project, "
        "licensed under the Apache License 2.0; its fonts package is under the Apache License 2.0 and the SIL Open "
        "Font License 1.1. BetterOffice doesn't endorse Multi-Agent Hub. Its `NOTICE` file reads:",
        "",
    ]
    notice = next((p for e in entries if e.name == "betteroffice-xlsx" for n, p in e.files.items()
                   if n == "NOTICE"), None)
    if not notice:
        raise SystemExit("betteroffice-xlsx's NOTICE file wasn't found")
    notice_text = notice.read_text(encoding="utf-8").strip()
    out += ["```", notice_text, "```", ""]
    notices = [(e, n, p) for e in entries for n, p in e.files.items()
               if re.match(r"notice", Path(n).name, re.I)
               and p.read_text(encoding="utf-8", errors="replace").strip() != notice_text]
    if notices:
        out += ["## NOTICE files", "", "The text of every other `NOTICE` file the shipped packages include.", ""]
        for e, n, p in notices:
            out += [f"### {e.name} ({n})", "", "```", p.read_text(encoding="utf-8", errors="replace").strip(), "```", ""]
    for section, title in SECTIONS.items():
        rows = sorted((e for e in entries if e.section == section), key=lambda e: e.name.lower())
        if not rows:
            continue
        out += [f"## {title}", ""]
        if section == "embedded":
            out += ["Rust crates whose code is compiled into a shipped Python module or WebAssembly file, found from "
                    "the source paths those binaries contain. Crates that leave no path behind can't be found this "
                    "way, so the packages' own notices (listed above) also apply.", ""]
        found_col = section == "embedded"
        out.append("| Name | Version | License | Authors | Source |" + (" Found in |" if found_col else "")
                   + " License files |")
        out.append("|---|---|---|---|---|" + ("---|" if found_col else "") + "---|")
        for e in rows:
            out.append(f"| {cell(e.name)} | {cell(e.version)} | {cell(e.license)} | {cell(e.authors)} | "
                       f"{cell(e.url)} |" + (f" {cell(e.found_in)} |" if found_col else "") + f" `{e.folder}/` |")
        out.append("")
    return "\n".join(out)


def problems(entries: list[Entry], need_files: bool) -> list[str]:
    errors = []
    for e in entries:
        if not e.license:
            errors.append(f"{e.section} {e.name}: no license declared")
        elif not allowed(e.license, e.key):
            errors.append(f"{e.section} {e.name}: license {e.license!r} isn't allowed")
        if need_files and not (e.files or e.texts):
            errors.append(f"{e.section} {e.name}: no license file found (add its upstream license to VENDORED, or a "
                          "sibling package to BORROW, in scripts/notices.py)")
    return errors


def generate() -> int:
    entries = collect(download=True)
    errors = problems(entries, need_files=True)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    # Build the new folder beside the old one and swap at the end, so a failure leaves licenses/ as it was.
    staging = LICENSES.with_name("licenses.new")
    if staging.exists():
        shutil.rmtree(staging)
    for e in entries:
        folder = staging / e.folder.removeprefix("licenses/")
        for name, src in e.files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, folder / name)
        for name, text in e.texts.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8", newline="\n")
    text = render(entries) + "\n"
    if LICENSES.exists():
        shutil.rmtree(LICENSES)
    staging.rename(LICENSES)
    NOTICES.write_text(text, encoding="utf-8", newline="\n")
    counts = {s: sum(e.section == s for e in entries) for s in SECTIONS}
    print(f"Wrote {NOTICES.name} and licenses/: " + ", ".join(f"{n} {s}" for s, n in counts.items()))
    return 0


def parse(text: str) -> dict[tuple[str, str], tuple[str, str]]:
    """Rows of the committed THIRD-PARTY-NOTICES.md: (section, name) -> (version, license)."""
    titles = {t: s for s, t in SECTIONS.items()}
    rows, section = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            section = titles.get(line[3:].strip())
        elif section and line.startswith("| ") and not line.startswith("| Name |"):
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            rows[(section, cells[0])] = (cells[1], cells[2])
    return rows


def check() -> int:
    if not NOTICES.exists():
        print(f"{NOTICES.name} is missing: run `scripts/notices.py generate`", file=sys.stderr)
        return 1
    committed = parse(NOTICES.read_text(encoding="utf-8"))
    entries = collect(download=False)
    errors = problems([e for e in entries if e.section != "embedded"], need_files=False)
    stale = []
    for e in entries:
        row = committed.get(e.key)
        if row is None:
            errors.append(f"{e.section} {e.name} {e.version} ships but has no entry in {NOTICES.name}")
            continue
        version, lic = row
        if e.section == "embedded":  # its license comes from crates.io, which check doesn't read
            if not allowed(lic):
                errors.append(f"embedded {e.name}: license {lic!r} isn't allowed")
        elif lic != e.license:
            errors.append(f"{e.section} {e.name}: license is now {e.license!r}, {NOTICES.name} says {lic!r}")
        if version != e.version and e.section != "other":
            stale.append(f"{e.section} {e.name}: {version} -> {e.version}")
        if not any((ROOT / e.folder).iterdir() if (ROOT / e.folder).is_dir() else ()):
            errors.append(f"{e.section} {e.name}: {e.folder}/ is missing or empty")
    gone = sorted(set(committed) - {e.key for e in entries})
    if stale or gone:
        print("Warning: versions changed or packages left since the notices were generated; run "
              "`scripts/notices.py generate` to refresh:", file=sys.stderr)
        for line in stale + [f"{s} {n}: no longer shipped" for s, n in gone]:
            print(f"  {line}", file=sys.stderr)
    if errors:
        print("\n".join(errors) + f"\nFix: run `scripts/notices.py generate` (and commit the result), or replace the "
              "dependency if its license isn't allowed.", file=sys.stderr)
        return 1
    print(f"{NOTICES.name} covers all {len(entries)} shipped packages.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["generate", "check"])
    args = parser.parse_args()
    return generate() if args.command == "generate" else check()


if __name__ == "__main__":
    sys.exit(main())
