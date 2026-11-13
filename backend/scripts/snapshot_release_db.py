"""Save a sample database at the current schema as tests/release_dbs/v<version>.sql.

Run this once per release, before tagging (see RELEASING.md):

    uv run python scripts/snapshot_release_db.py 0.2.0

The file is a plain SQL dump of a small but complete history (a project session with a worktree and a
handoff, a chat, events, a summary and settings). `tests/test_release_upgrades.py` loads every dump,
runs the migrations to head and checks that nothing was lost, so a migration that would damage an
existing user's database fails CI instead of reaching their PC.
"""

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from app.db import init_db, make_engine
from app.models import (AgentKind, AgentSession, AgentSummary, HubSettings, SessionEvent, SessionStatus, Stint,
                        StintReason, Worktree, save_hub_settings)

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "release_dbs"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def fill(db: Session) -> None:
    project = AgentSession(id="project-session", title="Fix the parser", project_path=r"C:\work\repo",
                           status=SessionStatus.COMPLETED, chain=[{"agent": "claude"}, {"agent": "codex"}],
                           created_at=T0, updated_at=T0)
    chat = AgentSession(id="chat-session", title="Question", project_path=r"C:\hub\chats\chat-session",
                        chat=True, created_at=T0, updated_at=T0)
    db.add_all([project, chat])
    db.flush()

    claude = Stint(session_id=project.id, seq=1, agent=AgentKind.CLAUDE, model="claude-opus-5-5", effort="high",
                   native_session_id="claude-native", reason=StintReason.NEW, started_at=T0,
                   ended_at=T0 + timedelta(minutes=5))
    codex = Stint(session_id=project.id, seq=2, agent=AgentKind.CODEX, native_session_id="codex-thread",
                  reason=StintReason.LIMIT, brief="Task: fix the parser\n\ncontinue", started_at=T0)
    chat_stint = Stint(session_id=chat.id, seq=1, agent=AgentKind.OLLAMA, model="qwen3.5:9b-q4_K_M",
                       native_session_id="local-uuid", reason=StintReason.NEW, started_at=T0)
    db.add_all([claude, codex, chat_stint])
    db.flush()

    db.add(Worktree(session_id=project.id, repo_path=r"C:\work\repo", path=r"C:\hub\worktrees\repo-12345678",
                    branch="hub/fix-the-parser-12345678", base_commit="0" * 40, created_at=T0))
    events = [
        (project, claude, "user_message", {"text": "Fix the parser"}),
        (project, claude, "message", {"text": "Looking at it."}),
        (project, claude, "tool_call", {"name": "Edit", "input": {"file_path": "parser.py"}}),
        (project, claude, "limit_reached", {"message": "usage limit"}),
        (project, codex, "handoff", {"from": "claude", "to": "codex", "brief": True}),
        (project, codex, "completed", {}),
        (chat, chat_stint, "user_message", {"text": "What is a worktree?"}),
        (chat, chat_stint, "message", {"text": "A second checkout of the same repository."}),
    ]
    for i, (session, stint, kind, payload) in enumerate(events):
        db.add(SessionEvent(session_id=session.id, stint_id=stint.id, agent=stint.agent, type=kind,
                            payload=payload, created_at=T0 + timedelta(seconds=i)))
    db.add(AgentSummary(session_id=project.id, agent=AgentKind.CODEX, text="Parser fixed.", tokens=120,
                        last_event_id=6, created_at=T0))
    save_hub_settings(db, HubSettings())
    db.commit()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    version = sys.argv[1].removeprefix("v")
    engine = make_engine("sqlite://", poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as db:
        fill(db)
    raw = engine.raw_connection()
    try:
        dump = "\n".join(raw.driver_connection.iterdump())
    finally:
        raw.close()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"v{version}.sql"
    header = f"-- Multi-Agent Hub {version} sample database (scripts/snapshot_release_db.py). Do not edit.\n"
    target.write_text(header + dump + "\n", encoding="utf-8", newline="\n")
    # Sanity check: the dump loads back.
    sqlite3.connect(":memory:").executescript(target.read_text(encoding="utf-8"))
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
