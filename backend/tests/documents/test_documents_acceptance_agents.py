"""Acceptance tests for revision 4, attachments PR 2: what each agent receives, the outputs folder in the API, and
PR 1 regressions. Written before the feature from `docs/plan.md` (Revision 4, PR 2, "Decided 2026-11-23" bullets
first) and the live probe (`sub-agents/runs/2026-11-21-rev4-attachments-pr2/probe.md`).

"What each agent receives" goes through the hub's REAL wiring: `create_app` with `default_adapter_factory` (the
production factory), and only the program at the end swapped for the fake CLI by a `Runner` (the documented seam
every CLI launch goes through, `app/adapters/runner.py`). So these tests see exactly the command line and
environment the real `claude`/`codex` would get, without either CLI installed. The local model talks HTTP to a
loopback server that serves `FakeOllama`. Nothing here reads the user's real Claude, Codex or Ollama setup.

Seams assumed (see the report): the outputs folder is `<attachments dir>/<session id>/outputs` (plan); hub-doc
learns it from `HUB_DOC_OUTPUTS` in the agent's environment; optional `create_app(tools_bin=, office_command=)`
are passed only when `create_app` has them (so these tests never write to the real app-data folder or start real
Office once those exist); the outputs routes are in `OutputsApi`.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.attachments import make_read_only, mark_from_elsewhere
from app.extract import make_text_copy

from support import attachment_samples
from support import document_samples as samples
from support import fake_ollama
from support.attachments import upload
from support.hub import opened_files
from support.wired import (
    Launch,
    OutputsApi,
    WiredHub,
    allowed_tools,
    make_chat_with_original,
    outputs_existed,
    same_path,
    writable_roots,
)

CLAUDE_RULES = {"Bash(hub-doc *)", "PowerShell(hub-doc *)"}


# --- Checks shared by the agents --------------------------------------------------------------------------------


def hub_doc_dir(launch: Launch) -> Path:
    """The PATH folder holding hub-doc, which must be first to resolve `hub-doc` and hold nothing that shadows."""
    path = launch.effective_env.get("PATH") or launch.effective_env.get("Path") or ""
    for folder in path.split(os.pathsep):
        if folder and any((Path(folder) / name).is_file() for name in ("hub-doc.exe", "hub-doc")):
            return Path(folder)
    raise AssertionError(f"no folder on the agent's PATH holds hub-doc: {path[:600]}")


def assert_toolkit_on_path(launch: Launch, outputs: Path) -> None:
    """Plan: Decided 2026-11-23 ("One hub-doc command ... in a bin folder of its own ... put on the agent's PATH")."""
    folder = hub_doc_dir(launch)
    # A folder of its own: the venv's python.exe must not shadow the user's Python for the agent.
    assert not (folder / "python.exe").exists(), f"{folder} also holds python.exe"
    assert same_path(launch.effective_env.get("HUB_DOC_OUTPUTS", ""), outputs), launch.env


def mentions(text: str, outputs: Path) -> bool:
    forms = {str(outputs), outputs.as_posix(), str(outputs).replace("\\", "\\\\")}
    return "hub-doc" in text and any(form in text for form in forms)


# --- Claude ---------------------------------------------------------------------------------------------------------


def test_claude_gets_hub_doc_pre_approved_on_every_turn(wired: WiredHub) -> None:
    """Plan: Decided 2026-11-23 ("Claude gets it pre-approved with --allowedTools "Bash(hub-doc *)"
    "PowerShell(hub-doc *)": under the hub's settings Claude's shell otherwise runs no program") and "Outputs folder:
    Claude writes through the existing --add-dir ... The folder exists before each turn". Probe s.2: under
    acceptEdits + --permission-prompts none, Bash running any program is denied without the rule."""
    session_id = wired.chat("claude")
    outputs = wired.outputs(session_id)
    for text in ("Fix the errors in my workbook", "Now the other sheet"):
        body = wired.send(session_id, text)
        assert body["status"] == "completed", body
        launch = wired.launches[-1]
        args = launch.command[1:]
        assert ("--session-id" in args) if text.startswith("Fix") else ("--resume" in args)
        rules = allowed_tools(args)
        assert CLAUDE_RULES <= set(rules), args
        # The first tool the hub pre-approves, and the only one: nothing broader than hub-doc by its bare name.
        assert set(rules) == CLAUDE_RULES, rules
        add_dirs = [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
        assert any(Path(outputs).resolve().is_relative_to(Path(d).resolve()) for d in add_dirs), add_dirs
        assert outputs_existed(launch, outputs)
        assert_toolkit_on_path(launch, outputs)
        echoed = wired.echo(session_id)
        assert mentions(" ".join(args) + echoed["prompt"], outputs), "Claude isn't told about hub-doc and the folder"


def test_claude_plain_turns_are_unchanged(wired: WiredHub) -> None:
    """Regression (task brief): plain messages and turns still work through the real wiring; the user's text is
    what the transcript keeps."""
    session_id = wired.chat("claude")
    body = wired.send(session_id, "hello there")
    assert body["status"] == "completed", body
    events = wired.events(session_id)
    assert events[0]["type"] == "user_message" and events[0]["payload"]["text"] == "hello there"
    assert wired.echo(session_id)["prompt"].startswith("hello there")


# --- Codex ----------------------------------------------------------------------------------------------------------


def test_codex_may_write_only_the_outputs_folder_on_new_and_resumed_turns(wired: WiredHub) -> None:
    """Plan: Decided 2026-11-23 ("Codex needs -c sandbox_workspace_write.writable_roots=["<outputs>"], which must be
    the outputs folder only (with the whole session folder, the sandbox could clear an original's read-only flag)").
    Probe s.3: without it Codex's sandbox denies writes to outputs; the override parses on `exec resume` too."""
    session_id = wired.chat("codex")
    outputs = wired.outputs(session_id)
    for text in ("Fix the workbook", "And the chart"):
        body = wired.send(session_id, text)
        assert body["status"] == "completed", body  # the fake exits with a config error on a malformed value
        launch = wired.launches[-1]
        args = launch.command[1:]
        assert args[:2] == (["exec", "--json"] if text.startswith("Fix") else ["exec", "resume"]), args
        roots = writable_roots(args)
        assert len(roots) == 1, args
        assert len(roots[0]) == 1 and same_path(roots[0][0], outputs), roots
        assert not any(same_path(root, outputs.parent) for root in roots[0])  # never the session folder
        assert outputs_existed(launch, outputs)
        assert_toolkit_on_path(launch, outputs)
        echoed = wired.echo(session_id)
        assert mentions(" ".join(args) + echoed["prompt"], outputs), "Codex isn't told about hub-doc and the folder"


def test_codex_with_images_still_gets_the_writable_root(wired: WiredHub) -> None:
    """Plan: Decided 2026-11-23 (writable root on every turn) together with PR 1's `--image` flags, which are greedy
    (codex-cli 0.164.0): the override must not be swallowed as an image path."""
    session_id = wired.chat("codex")
    image = upload(wired.client, session_id, "shot.png", attachment_samples.png())
    body = wired.send(session_id, "What is this?", [image["id"]])
    assert body["status"] == "completed", body
    echoed = wired.echo(session_id)
    assert echoed["images"] == [image["path"]] and echoed["image_errors"] == []
    roots = writable_roots(wired.launches[-1].command[1:])
    assert roots and same_path(roots[0][0], wired.outputs(session_id))


# --- Local model ----------------------------------------------------------------------------------------------------


def tool_messages(call: dict) -> list[str]:
    return [m["content"] for m in call["messages"] if m["role"] == "tool"]


def test_local_model_writes_outputs_but_not_originals(wired: WiredHub) -> None:
    """Plan: Decided 2026-11-23 ("the local model needs a LocalTools write root. The folder exists before each
    turn"). Probe s.3: today `LocalTools.resolve(writing=True)` refuses anything outside the working folder."""
    session_id = wired.chat("ollama", model=fake_ollama.MODEL)
    outputs = wired.outputs(session_id)
    note = upload(wired.client, session_id, "notes.txt", b"the original notes\n")
    original = Path(note["path"])
    before = original.read_bytes()
    wired.ollama.watch = outputs
    wired.ollama.fake = fake_ollama.FakeOllama(script=[
        {"content": "Writing the summary.", "tool_calls": [
            ("write_file", {"path": str(outputs / "summary.txt"), "content": "summary from the local model\n"}),
            ("write_file", {"path": str(original), "content": "overwritten\n"}),
        ]},
        "Wrote outputs/summary.txt.",
    ])
    body = wired.send(session_id, "Summarize my notes into a new file", [note["id"]])
    assert body["status"] == "completed", body

    assert (outputs / "summary.txt").read_text(encoding="utf-8") == "summary from the local model\n"
    assert original.read_bytes() == before
    assert not (original.stat().st_mode & stat.S_IWRITE)  # still read-only
    results = tool_messages(wired.ollama.fake.calls[-1])
    assert results[0].startswith("Created"), results
    assert not results[1].startswith(("Created", "Replaced")), results
    assert wired.ollama.outputs_at_first_chat == [True]
    system = " ".join(m["content"] for m in wired.ollama.fake.calls[0]["messages"] if m["role"] == "system")
    assert mentions(system, outputs), "the local model isn't told about hub-doc and the folder"


def test_local_model_finds_hub_doc_on_its_path(wired: WiredHub) -> None:
    """Plan: Decided 2026-11-23 (hub-doc "put on the agent's PATH", every agent). The local model's `run_command` is
    real PowerShell here, so this asks PowerShell itself where `hub-doc` is."""
    session_id = wired.chat("ollama", model=fake_ollama.MODEL)
    outputs = wired.outputs(session_id)
    wired.ollama.fake = fake_ollama.FakeOllama(script=[
        {"tool_calls": [("run_command", {"command": "(Get-Command hub-doc).Source; $env:HUB_DOC_OUTPUTS"})]},
        "Found it.",
    ])
    body = wired.send(session_id, "Where is hub-doc?")
    assert body["status"] == "completed", body
    result = tool_messages(wired.ollama.fake.calls[-1])[0]
    assert "hub-doc" in result.lower() and str(outputs).lower() in result.lower(), result


# --- Outputs in the API ---------------------------------------------------------------------------------------------


def test_outputs_are_listed_opened_and_saved_elsewhere(client: TestClient, tmp_path: Path) -> None:
    """Plan: PR 2 > Outputs folder ("each shows as a file row with Open and Save as...")."""
    session_id, _, outputs = make_chat_with_original(client)
    api = OutputsApi(client, session_id)
    assert api.list().status_code == 200
    (outputs / "report (edited).docx").write_bytes(samples.formatted_docx())
    (outputs / "fix.ps1").write_text("Remove-Item C:\\ -Recurse\n", encoding="utf-8")

    listed = api.list()
    assert listed.status_code == 200, listed.text
    rows = {row["name"]: row for row in listed.json()}
    assert set(rows) == {"report (edited).docx", "fix.ps1"}
    assert rows["report (edited).docx"]["size"] == (outputs / "report (edited).docx").stat().st_size

    opened_files.clear()
    assert api.open("report (edited).docx").json() == {"action": "opened"}
    # An agent's script is shown in Explorer, never run (as PR 1 does for attachments).
    assert api.open("fix.ps1").json() == {"action": "revealed"}
    assert [(Path(p).name, reveal) for p, reveal in opened_files] == [("report (edited).docx", False), ("fix.ps1", True)]

    destination = tmp_path / "chosen" / "my report.docx"
    destination.parent.mkdir()
    saved = api.save("report (edited).docx", destination)
    assert saved.status_code == 200, saved.text
    assert destination.read_bytes() == (outputs / "report (edited).docx").read_bytes()
    assert (outputs / "report (edited).docx").exists()  # a copy, not a move


def test_outputs_routes_refuse_anything_outside_outputs(client: TestClient, tmp_path: Path) -> None:
    """Plan: PR 2 > Outputs folder, with the task brief's rule: the routes refuse paths outside outputs (an
    original, another folder, a junction out), and never let a copy land on an original."""
    session_id, note, outputs = make_chat_with_original(client)
    api = OutputsApi(client, session_id)
    original = Path(note["path"])
    attachment_folder = original.parent.name
    (outputs / "ok.txt").write_text("fine", encoding="utf-8")
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("secret", encoding="utf-8")
    junction = outputs / "link"
    if os.name == "nt":
        assert subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(secret_dir)],
                              capture_output=True).returncode == 0
    try:
        bad_names = [f"../{attachment_folder}/notes.txt", f"..\\{attachment_folder}\\notes.txt", str(original),
                     str(secret_dir / "secret.txt"), "", "nope.txt"]
        if os.name == "nt":
            bad_names.append("link/secret.txt")
        opened_files.clear()
        copy = tmp_path / "copy.txt"
        for name in bad_names:
            assert api.open(name).status_code in (400, 403, 404, 422), name
            assert api.save(name, copy).status_code in (400, 403, 404, 422), name
            assert api.file(name).status_code in (400, 403, 404, 422), name
        assert opened_files == [] and not copy.exists()
        listed = {row["name"] for row in api.list().json()}
        assert "secret.txt" not in json.dumps(sorted(listed)) and "notes.txt" not in listed

        # Save as... never writes over an original.
        before = original.read_bytes()
        assert api.save("ok.txt", original).status_code in (400, 403, 409, 422)
        assert original.read_bytes() == before
    finally:
        if junction.exists():
            os.rmdir(junction)


def test_outputs_routes_need_the_token_and_a_real_session(client: TestClient) -> None:
    """Plan: PR 2 > Outputs folder; AGENTS.md (every /api router is behind the token)."""
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "ok.txt").write_text("fine", encoding="utf-8")
    anonymous = TestClient(client.app, base_url=str(client.base_url))
    base = f"/api/sessions/{session_id}/outputs"
    opened_files.clear()
    assert anonymous.get(base).status_code in (401, 403)
    assert anonymous.post(f"{base}/open", json={"name": "ok.txt"}).status_code in (401, 403)
    assert anonymous.post(f"{base}/save", json={"name": "ok.txt", "destination": "C:/x.txt"}).status_code in (401, 403)
    assert anonymous.get(f"{base}/file", params={"name": "ok.txt"}).status_code in (401, 403)
    assert opened_files == []
    assert client.get("/api/sessions/00000000-0000-4000-8000-000000000000/outputs").status_code == 404


def test_output_files_are_served_for_the_preview_and_nothing_runs_as_a_page(client: TestClient) -> None:
    """Plan: PR 3 > preview pane. Output files come as blobs: the exact bytes, `nosniff`, never cached (an agent
    overwrites under the same name), images and PDFs as themselves, Office files as downloads, and pages, SVG and
    XML only ever as downloads. hub-doc's staging folders and missing files are refused."""
    session_id, _, outputs = make_chat_with_original(client)
    api = OutputsApi(client, session_id)
    files = {
        "report.docx": (samples.formatted_docx(), "application/octet-stream", "attachment"),
        "sales.xlsx": (samples.sales_xlsx(), "application/octet-stream", "attachment"),
        "paper.pdf": (attachment_samples.pdf(), "application/pdf", "inline"),
        "chart.png": (attachment_samples.png(), "image/png", "inline"),
        "notes.md": (b"# Notes", "text/plain; charset=utf-8", "inline"),
        "page.html": (b"<script>alert(1)</script>", "application/octet-stream", "attachment"),
        "art.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "application/octet-stream", "attachment"),
        "feed.xml": (b"<?xml version='1.0'?><x/>", "application/octet-stream", "attachment"),
        "tool.exe": (b"MZ", "application/octet-stream", "attachment"),
    }
    for name, (data, _, _) in files.items():
        (outputs / name).write_bytes(data)
    for name, (data, content_type, disposition) in files.items():
        served = api.file(name)
        assert served.status_code == 200, (name, served.text)
        assert served.content == data, name
        assert served.headers["content-type"] == content_type, name
        assert served.headers["content-disposition"].startswith(disposition), name
        assert served.headers["x-content-type-options"] == "nosniff"
        assert served.headers["cache-control"] == "no-store"

    # The pane fetches only the start of a large text file.
    start = client.get(f"{api.base}/file", params={"name": "notes.md"}, headers={"Range": "bytes=0-2"})
    assert start.status_code == 206 and start.content == b"# N"

    staging = outputs / ".~hub-doc-0123abcd"
    staging.mkdir()
    (staging / "half.docx").write_bytes(b"PK")
    assert api.file(".~hub-doc-0123abcd/half.docx").status_code in (400, 404)
    assert api.file("gone.docx").status_code == 404


def test_document_links_open_in_the_browser_only_when_they_are_web_links(client: TestClient) -> None:
    """Plan: PR 3 > links inside a previewed document open in the system browser, never the app's window: only
    http(s) and mailto are handed to Windows, behind the token."""
    opened_files.clear()
    # Characters such as | ^ { } \ and ' are fine in a real link: os.startfile runs no shell.
    good = ["https://example.com/a?b=1", "mailto:someone@example.com", "https://example.com/search?q={a|b}^c",
            r"https://example.com/path\to", "https://en.wikipedia.org/wiki/O'Brien"]
    for url in good:
        assert client.post("/api/links/open", json={"url": url}).status_code == 204, url
    for bad in ("file:///C:/Windows/System32/calc.exe", r"C:\Windows\notepad.exe", r"\\server\share\x",
                "javascript:alert(1)", "ms-settings:", "https://", "https://example.com/a b", 'http://x" & calc',
                "http://[abc]/", "https://example.com/\x07"):
        assert client.post("/api/links/open", json={"url": bad}).status_code == 400, bad
    anonymous = TestClient(client.app, base_url=str(client.base_url))
    assert anonymous.post("/api/links/open", json={"url": "https://example.com"}).status_code in (401, 403)
    assert opened_files == [(url, False) for url in good]


def test_an_agents_new_file_shows_in_the_outputs_list(wired: WiredHub) -> None:
    """Plan: PR 2 > Outputs folder: a file an agent writes during a turn is what the list shows afterwards."""
    session_id = wired.chat("ollama", model=fake_ollama.MODEL)
    outputs = wired.outputs(session_id)
    wired.ollama.fake = fake_ollama.FakeOllama(script=[
        {"tool_calls": [("write_file", {"path": str(outputs / "answer.md"), "content": "# Answer\n"})]}, "Done.",
    ])
    assert wired.send(session_id, "Write the answer to a file")["status"] == "completed"
    names = [row["name"] for row in OutputsApi(wired.client, session_id).list().json()]
    assert names == ["answer.md"]


def test_deleting_a_session_deletes_its_outputs(client: TestClient) -> None:
    """Plan: PR 1 storage ("deleted with the session") applied to `<attachments>/<session id>/outputs`."""
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "made.txt").write_text("x", encoding="utf-8")
    make_read_only(outputs / "made.txt")
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert not outputs.exists()


# --- Regression: PR 1's text copies ---------------------------------------------------------------------------------


def test_xlsx_text_copy_still_works_under_the_512_mb_cap(tmp_path: Path) -> None:
    """Regression (Decided 2026-11-23, "No pandas"; probe s.6): with numpy importable, `import openpyxl` loads
    OpenBLAS, which commits ~757 MB on a 24-thread PC, and PR 1's extract worker (512 MB cap) then crashed on every
    Excel file with no text copy. Run unmocked, with the default cap, on a workbook with formulas and a chart."""
    original = tmp_path / "sales.xlsx"
    original.write_bytes(samples.sales_xlsx())
    mark_from_elsewhere(original)
    copy = asyncio.run(make_text_copy("excel", original))
    assert copy.error is None and copy.path is not None, copy.error
    text = copy.path.read_text(encoding="utf-8")
    assert "North" in text and "Revenue" in text


def test_pandas_isnt_shipped() -> None:
    """Plan: Decided 2026-11-23 ("No pandas": +49 MB and the OpenBLAS memory problem above)."""
    assert importlib.util.find_spec("pandas") is None


def test_the_extract_worker_doesnt_load_numpy(tmp_path: Path) -> None:
    """Regression (probe s.6): whatever the engineer adds, the worker's `import openpyxl` must not pull numpy (and its
    OpenBLAS reservation) into the capped process, unless OPENBLAS_NUM_THREADS=1 is set first."""
    probe = subprocess.run([sys.executable, "-I", "-c",
                            "import sys, os, openpyxl; print('numpy' in sys.modules, os.environ.get('OPENBLAS_NUM_THREADS'))"],
                           capture_output=True, text=True, timeout=60)
    loaded, threads = probe.stdout.split()
    assert loaded == "False" or threads == "1", probe.stdout
