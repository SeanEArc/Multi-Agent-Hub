"""scripts/notices.py: the third-party notices the installer ships, and the release check that keeps them complete."""

import importlib.util
import json

import pytest

from support.paths import ROOT

spec = importlib.util.spec_from_file_location("notices", ROOT / "scripts" / "notices.py")
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)
Entry = notices.Entry


@pytest.mark.parametrize("expr, ok", [
    ("MIT", True),
    ("MIT OR Apache-2.0", True),
    ("MIT/Apache-2.0", True),  # Cargo's older spelling of OR
    ("Apache-2.0 / MIT", True),
    ("(MIT OR Apache-2.0) AND Unicode-3.0", True),
    ("GPL-3.0-only OR MIT", True),  # one allowed branch is enough
    ("MIT AND GPL-3.0-only", False),  # every AND part must be allowed
    ("GPL-3.0-only", False),
    ("AGPL-3.0-or-later", False),
    ("Apache-2.0 WITH LLVM-exception", True),
    ("(MIT", False),
    ("", False),
])
def test_allowed_license_expressions(expr, ok):
    assert notices.allowed(expr) is ok


def test_exceptions_apply_only_to_their_package():
    expr = "GPL-2.0-or-later WITH Bootloader-exception"
    assert not notices.allowed(expr)
    assert notices.allowed(expr, ("other", "PyInstaller bootloader"))


def test_two_shipped_versions_with_different_licenses_both_count(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    merged = notices.merge_versions([
        Entry("rust", "foo", "0.9.0", "MIT", files={"LICENSE": a}),
        Entry("rust", "foo", "1.0.0", "GPL-3.0-only", files={"LICENSE": b}),
        Entry("rust", "bar", "1.0.0", "MIT", files={"LICENSE": a}),
        Entry("rust", "bar", "1.1.0", "MIT", files={"LICENSE": b}),
    ])
    foo, bar = merged
    assert (foo.version, foo.license) == ("0.9.0, 1.0.0", "(MIT) AND (GPL-3.0-only)")
    assert not notices.allowed(foo.license)
    assert foo.files == {"0.9.0/LICENSE": a, "1.0.0/LICENSE": b}
    assert (bar.version, bar.license, bar.files) == ("1.0.0, 1.1.0", "MIT", {"LICENSE": a})


def test_prereleases_sort_before_their_release():
    assert sorted(["1.0.0", "1.0.0-rc.1", "0.9.10", "0.9.2"], key=notices.version_key) == [
        "0.9.2", "0.9.10", "1.0.0-rc.1", "1.0.0"]


@pytest.mark.parametrize("value, expected", [
    ("MIT", "MIT"), ({"type": "MIT", "url": "x"}, "MIT"), ([{"type": "MIT"}, {"type": "Apache-2.0"}], "MIT OR Apache-2.0"),
    (None, ""),
])
def test_old_npm_license_fields(value, expected):
    assert notices.npm_license(value) == expected


def test_license_folders_are_collected_at_the_top_level(tmp_path):
    (tmp_path / "LICENSES").mkdir()
    (tmp_path / "LICENSES" / "MIT.txt").write_text("x", encoding="utf-8")
    (tmp_path / "LICENSE-APACHE").write_text("x", encoding="utf-8")
    (tmp_path / "license.js").write_text("x", encoding="utf-8")  # code, not a license text
    assert sorted(notices.license_files(tmp_path, depth=1)) == ["LICENSE-APACHE", "LICENSES/MIT.txt"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with a BetterOffice NOTICE and three shipped packages, notices generated for them."""
    notice = tmp_path / "NOTICE"
    notice.write_text("BetterOffice\nCopyright 2026 Elia Hilse, The OpenOOXML Project\n", encoding="utf-8")
    lic = tmp_path / "LICENSE"
    lic.write_text("license text", encoding="utf-8")
    shipped = [
        Entry("python", "betteroffice-xlsx", "0.2.2", "Apache-2.0", files={"NOTICE": notice, "LICENSE": lic}),
        Entry("npm", "react", "19.3.1", "MIT", "Meta | Platforms", files={"LICENSE": lic}),
        Entry("rust", "serde", "1.0.229", "MIT OR Apache-2.0", files={"LICENSE-MIT": lic}),
    ]
    monkeypatch.setattr(notices, "ROOT", tmp_path)
    monkeypatch.setattr(notices, "NOTICES", tmp_path / "THIRD-PARTY-NOTICES.md")
    monkeypatch.setattr(notices, "LICENSES", tmp_path / "licenses")
    monkeypatch.setattr(notices, "collect", lambda download: [Entry(**{**e.__dict__}) for e in shipped])
    assert notices.generate() == 0
    return tmp_path, shipped


def test_generate_writes_rows_license_files_and_the_betteroffice_notice(repo):
    root, _ = repo
    text = (root / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright 2026 Elia Hilse, The OpenOOXML Project" in text
    assert notices.parse(text) == {
        ("python", "betteroffice-xlsx"): ("0.2.2", "Apache-2.0"),
        ("npm", "react"): ("19.3.1", "MIT"),
        ("rust", "serde"): ("1.0.229", "MIT OR Apache-2.0"),
    }
    assert "Meta \\| Platforms" in text  # a | in a cell doesn't split the row
    assert (root / "licenses" / "python" / "betteroffice-xlsx" / "NOTICE").exists()
    assert (root / "licenses" / "rust" / "serde" / "LICENSE-MIT").read_text(encoding="utf-8") == "license text"


def test_check_passes_when_complete(repo, capsys):
    assert notices.check() == 0
    assert "covers all 3" in capsys.readouterr().out


def test_check_fails_when_an_entry_is_removed(repo, capsys):
    root, _ = repo
    path = root / "THIRD-PARTY-NOTICES.md"
    path.write_text("\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                              if not l.startswith("| react |")), encoding="utf-8")
    assert notices.check() == 1
    assert "npm react 19.3.1 ships but has no entry" in capsys.readouterr().err


def test_check_fails_on_a_new_package_or_a_changed_license(repo, monkeypatch, capsys):
    _, shipped = repo
    lic = shipped[1].files["LICENSE"]
    now = [Entry(**{**e.__dict__}) for e in shipped]
    now[2].license = "MIT"
    now.append(Entry("npm", "left-pad", "1.3.0", "WTFPL", files={"LICENSE": lic}))
    monkeypatch.setattr(notices, "collect", lambda download: now)
    assert notices.check() == 1
    err = capsys.readouterr().err
    assert "rust serde: license is now 'MIT'" in err
    assert "npm left-pad: license 'WTFPL' isn't allowed" in err
    assert "npm left-pad 1.3.0 ships but has no entry" in err


def test_check_only_warns_about_a_version_bump(repo, monkeypatch, capsys):
    _, shipped = repo
    now = [Entry(**{**e.__dict__}) for e in shipped]
    now[1].version = "19.4.0"
    monkeypatch.setattr(notices, "collect", lambda download: now)
    assert notices.check() == 0
    assert "npm react: 19.3.1 -> 19.4.0" in capsys.readouterr().err


def test_committed_notices_cover_the_backends_python_packages():
    """Catches a new Python dependency before release; the release check covers npm and Rust too."""
    rows = notices.parse((ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8"))
    for entry in notices.python_entries():
        assert ("python", entry.name) in rows, f"{entry.name}: run `scripts/notices.py generate`"
        assert rows[("python", entry.name)][1] == notices.LICENSE_FIXES.get(entry.key, entry.license)
        assert any((ROOT / entry.folder).iterdir())
    for name in ("Python", "PyInstaller bootloader", "NSIS"):
        assert ("other", name) in rows


def test_installer_ships_the_notices():
    conf = json.loads((ROOT / "frontend" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    resources = conf["bundle"]["resources"]
    assert resources["../../THIRD-PARTY-NOTICES.md"] == "THIRD-PARTY-NOTICES.md"
    assert resources["../../licenses/"] == "licenses/"
    assert resources["../../LICENSE"] == "LICENSE"
