"""hub-doc reads only the session's folders (`HUB_DOC_READ_ROOTS`, `app.doctool.paths`): every spelling of a path
inside or outside (short names, junctions, prefixes, other spellings), what each session's agents get, an empty
or missing limit, and Save as… never reaching the attachments folder by another route.
"""

import ctypes
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.files.attachments import AttachmentStore
from app.doctool import cli
from app.doctool.contract import READ_ROOTS_ENV, Refused, read_roots
from app.doctool.paths import within
from app.files.documents import DocumentAccess
from app.files.outputs import OutputsStore

from support import document_samples as samples
from support.documents import HubDoc, junction, make_session
from support.sessions import create, wait_until_idle

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows file locks, junctions and processes")


def short_name(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value


@pytest.fixture
def folders(tmp_path: Path) -> dict[str, Path]:
    inside = tmp_path / "Session Files"
    sibling = tmp_path / "Session Files-other"  # same prefix, a different folder
    outside = tmp_path / "elsewhere"
    for folder in (inside, sibling, outside):
        folder.mkdir()
        (folder / "book.xlsx").write_bytes(samples.sales_xlsx())
    return {"inside": inside, "sibling": sibling, "outside": outside}


def gate(monkeypatch: pytest.MonkeyPatch, raw: str | None, file: Path | str) -> None:
    """hub-doc's read check for one input, with HUB_DOC_READ_ROOTS set to `raw` (None: unset)."""
    if raw is None:
        monkeypatch.delenv(READ_ROOTS_ENV, raising=False)
    else:
        monkeypatch.setenv(READ_ROOTS_ENV, raw)
    cli.check(cli.parse(["xlsx", "check", str(file)]))


@windows_only
def test_every_spelling_of_a_file_inside_is_allowed(folders: dict[str, Path], monkeypatch) -> None:
    inside = folders["inside"]
    target = inside / "book.xlsx"
    spellings = [str(target), str(target).upper(), "\\\\?\\" + str(target), str(inside / "sub" / ".." / "book.xlsx")]
    short = short_name(target)
    if short and short.upper() != str(target).upper():
        spellings.append(short)
    for spelling in spellings:
        gate(monkeypatch, str(inside), spelling)  # no Refused


@windows_only
@pytest.mark.parametrize("which", ["outside", "sibling prefix", "dotdot", "verbatim", "stream", "trailing dot",
                                   "short name", "junction in"])
def test_every_spelling_of_a_file_outside_is_refused(folders: dict[str, Path], monkeypatch, tmp_path: Path,
                                                      which: str) -> None:
    inside, outside = folders["inside"], folders["outside"]
    target = outside / "book.xlsx"
    spelling = {
        "outside": str(target),
        "sibling prefix": str(folders["sibling"] / "book.xlsx"),
        "dotdot": str(inside / ".." / "elsewhere" / "book.xlsx"),
        "verbatim": "\\\\?\\" + str(target),
        "stream": str(target) + ":Zone.Identifier",
        "trailing dot": str(outside) + ".\\book.xlsx",
        "short name": short_name(target),
        "junction in": "",
    }[which]
    if which == "junction in":
        junction(inside / "link", outside)
        spelling = str(inside / "link" / "book.xlsx")
    if not spelling:
        pytest.skip("no 8.3 names on this volume")
    try:
        with pytest.raises(Refused, match="outside the folders"):
            gate(monkeypatch, str(inside), spelling)
    finally:
        if (inside / "link").exists():
            os.rmdir(inside / "link")


@windows_only
def test_roots_given_by_other_spellings_or_through_a_junction(folders: dict[str, Path], monkeypatch,
                                                              tmp_path: Path) -> None:
    """The hub's roots may themselves be junctions (a user's profile moved to another drive), short names or
    `\\\\?\\` paths: the files really inside are allowed by any spelling, the rest still refused."""
    inside = folders["inside"]
    via = tmp_path / "via-junction"
    junction(via, inside)
    try:
        roots = [str(via), "\\\\?\\" + str(inside).lower() + "\\", short_name(inside) or str(inside)]
        for root in roots:
            gate(monkeypatch, root, inside / "book.xlsx")
            gate(monkeypatch, root, via / "book.xlsx")
            with pytest.raises(Refused):
                gate(monkeypatch, root, folders["outside"] / "book.xlsx")
    finally:
        os.rmdir(via)


def test_an_empty_or_malformed_read_limit_reads_nothing(folders: dict[str, Path], monkeypatch) -> None:
    """Decided here: only an unset HUB_DOC_READ_ROOTS (hub-doc run by hand) allows any file. Set but empty, only
    separators, or only relative parts (which would depend on the agent's current folder) allows nothing; a root
    that doesn't exist allows nothing outside it. Before the fix, an empty value allowed every file."""
    target = folders["outside"] / "book.xlsx"
    gate(monkeypatch, None, target)  # by hand: any file
    assert read_roots() is None
    for raw in ["", " ", os.pathsep * 3, "relative" + os.pathsep + ".", str(folders["inside"] / "missing")]:
        with pytest.raises(Refused):
            gate(monkeypatch, raw, target)
    monkeypatch.chdir(folders["outside"])
    with pytest.raises(Refused):
        gate(monkeypatch, ".", "book.xlsx")  # "." can't name the current folder
    gate(monkeypatch, os.pathsep.join(["relative", str(folders["outside"]), ""]), target)  # the absolute part counts


@windows_only
def test_hub_doc_itself_refuses_with_an_empty_read_limit(hub_doc_exe: str, folders: dict[str, Path]) -> None:
    reply = HubDoc(hub_doc_exe, None).run("xlsx", "check", folders["outside"] / "book.xlsx",
                                          env={READ_ROOTS_ENV: ""})
    assert reply.code == 1 and "outside the folders" in reply.data["error"], reply


def test_a_missing_read_limit_under_the_hub_reads_only_the_session(hub_doc_exe: str, tmp_path: Path) -> None:
    """An agent that unsets HUB_DOC_READ_ROOTS can't widen reads: with an outputs folder set, only the session's own
    folder (the outputs folder's parent) can be read. Run by hand with neither variable, any file."""
    session = make_session(tmp_path / "attachments")
    inside = session.original("sales.xlsx", samples.sales_xlsx())
    elsewhere = tmp_path / "private" / "secret.xlsx"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(samples.sales_xlsx())
    tool = HubDoc(hub_doc_exe, session.outputs)  # sets HUB_DOC_OUTPUTS and clears every other HUB_DOC_ variable
    assert tool.xlsx_check(inside).code == 0
    refused = tool.xlsx_check(elsewhere)
    assert refused.code == 1 and "HUB_DOC_READ_ROOTS isn't set" in refused.data["error"]
    assert tool.run("docx", "diff", inside, elsewhere).code in (1, 2)  # a second input outside is refused too
    assert HubDoc(hub_doc_exe, None).xlsx_check(elsewhere).code == 0


@windows_only
def test_hub_doc_reads_only_inside_its_read_roots(hub_doc_exe: str, tmp_path: Path) -> None:
    session = make_session(tmp_path / "attachments")
    inside = session.original("sales file.xlsx", samples.sales_xlsx())
    elsewhere = tmp_path / "private"
    elsewhere.mkdir()
    secret = elsewhere / "secret.xlsx"
    secret.write_bytes(samples.sales_xlsx())
    tool = HubDoc(hub_doc_exe, session.outputs)
    roots = {"HUB_DOC_READ_ROOTS": os.pathsep.join([str(session.root), str(session.outputs)])}

    def check(path: object) -> int:
        return tool.run("xlsx", "check", path, env=roots).code

    assert check(inside) == 0
    short = short_name(inside)
    if short and short != str(inside):
        assert check(short) == 0  # an 8.3 spelling of a file inside
    assert check(secret) == 1
    assert check(f"\\\\?\\{secret}") == 1
    assert check(elsewhere / "missing.xlsx") == 1  # refused before saying whether it exists
    assert check(session.root / ".." / "private" / "secret.xlsx") == 1
    link = session.root / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], capture_output=True, check=True)
    try:
        assert check(link / "secret.xlsx") == 1  # a junction inside that leads out
    finally:
        os.rmdir(link)
    share = "\\\\localhost\\" + str(secret).replace(":", "$", 1)
    if os.path.exists(share):  # the admin share needs an elevated token: checked when it's reachable
        assert check(share) == 1
        assert check("\\\\localhost\\" + str(inside).replace(":", "$", 1)) == 0
    reply = tool.run("docx", "report", secret, env=roots)
    assert reply.code == 1 and "outside the folders hub-doc may read" in reply.data["error"]
    # Under the hub (outputs set) with the read limit unset, only the session's folder: unsetting it can't widen reads.
    assert tool.xlsx_check(secret).code == 1 and tool.xlsx_check(inside).code == 0
    assert HubDoc(hub_doc_exe, None).xlsx_check(secret).code == 0  # run by hand, neither variable set: any file


@windows_only
def test_within_matches_short_names_junctions_and_prefixes(tmp_path: Path) -> None:
    root = tmp_path / "a long folder name"
    (root / "sub").mkdir(parents=True)
    assert within(root / "sub" / "x.txt", root)
    assert within(f"\\\\?\\{root}\\sub\\x.txt", root)
    short = short_name(root)
    if short and short != str(root):
        assert within(Path(short) / "x.txt", root)
    other = tmp_path / "other"
    other.mkdir()
    link = other / "into"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root)], capture_output=True, check=True)
    try:
        assert within(link / "x.txt", root)
        assert not within(other / "x.txt", root)
    finally:
        os.rmdir(link)


def test_agents_get_the_sessions_folders_as_read_roots(client, tmp_path: Path) -> None:
    session_id = client.post("/api/sessions", json={"title": "ok", "agent": "claude"}).json()["id"]
    orchestrator = client.app.state.orchestrator

    class Stub:
        cwd = tmp_path / "work"
        documents = None

    stub = Stub()
    orchestrator._equip(session_id, stub)
    outputs = orchestrator.outputs.folder(session_id)
    assert stub.documents.read_roots == (outputs.parent, outputs, tmp_path / "work")
    roots = stub.documents.env()["HUB_DOC_READ_ROOTS"].split(os.pathsep)
    assert roots == [str(outputs.parent), str(outputs), str(tmp_path / "work")]


def test_read_roots_are_left_out_when_there_are_none(tmp_path: Path) -> None:
    assert "HUB_DOC_READ_ROOTS" not in DocumentAccess(outputs=tmp_path).env()


def roots_of(client: TestClient, session_id: str) -> tuple[Path, ...]:
    return client.app.state.orchestrator._adapters[session_id].documents.read_roots


def test_roots_for_a_chat(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={"title": "ok", "agent": "claude"}).json()["id"]
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
    wait_until_idle(client, session_id)
    session_folder = client.app.state.orchestrator.attachments.session_dir(session_id)
    chat_folder = Path(client.get(f"/api/sessions/{session_id}").json()["project_path"])
    assert roots_of(client, session_id) == (session_folder, session_folder / "outputs", chat_folder)


def test_roots_for_a_worktree_subfolder_and_after_a_handoff(client: TestClient, repo: Path) -> None:  # noqa: F811
    """A session on `<repo>/src` runs in `<worktree>/src`: that is the working folder hub-doc may read (never the
    user's own checkout), and a handoff to another agent keeps the same roots."""
    session_id = create(client, repo / "src", "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "where are you"})
    wait_until_idle(client, session_id)
    session = client.get(f"/api/sessions/{session_id}").json()
    worktree = Path(session["worktree"]["path"])
    ran_in = Path(json.loads(client.get(f"/api/sessions/{session_id}/events").json()[-1]["payload"]["text"])["cwd"])
    roots = roots_of(client, session_id)
    assert Path(roots[2]).resolve() == (worktree / "src").resolve() == ran_in.resolve()
    assert not any(Path(r).resolve().is_relative_to(repo.resolve()) for r in roots)

    assert client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"}).status_code == 202
    wait_until_idle(client, session_id)
    assert client.get(f"/api/sessions/{session_id}").json()["agent"] == "codex"
    assert roots_of(client, session_id) == roots


@windows_only
def test_save_as_through_a_junction_into_attachments_is_refused(tmp_path: Path) -> None:
    store = OutputsStore(AttachmentStore(tmp_path / "attachments"))
    session = uuid.uuid4().hex
    (store.folder(session, create=True) / "r.txt").write_text("copy")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = elsewhere / "looks-harmless"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "attachments")], capture_output=True,
                   check=True)
    try:
        from app.files.attachments import AttachmentError

        with pytest.raises(AttachmentError) as refused:
            store.save_copy(session, "r.txt", str(link / "copy.txt"))
        assert refused.value.status == 403
    finally:
        os.rmdir(link)
