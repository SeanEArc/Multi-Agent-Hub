import asyncio
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.adapters.base import EventType
from app.adapters.claude import ClaudeAdapter
from app.adapters.cli import HEADLESS_GIT_ENV
from app.adapters.codex import CodexAdapter
from app.adapters.runner import HostRunner, RunningProcess

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fake_codex.py"


class RecordingRunner(HostRunner):
    """A host runner that records each launch, standing in for another runner (such as Docker)."""

    def __init__(self) -> None:
        self.launches: list[tuple[list[str], Path, dict[str, str]]] = []

    async def start(self, command: Sequence[str], cwd: Path, env: Mapping[str, str]) -> RunningProcess:
        self.launches.append((list(command), cwd, dict(env)))
        return await super().start(command, cwd, env)


def run_turns(adapter, *prompts: str) -> list[list]:
    async def run() -> list[list]:
        turns = [[event async for event in adapter.start_task(prompts[0])]]
        for reply in prompts[1:]:
            turns.append([event async for event in adapter.send_reply(reply)])
        return turns

    return asyncio.run(run())


def test_claude_launches_go_through_the_runner(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), "ok"], runner=runner)

    turns = run_turns(adapter, "hello", "again")

    assert [turn[-1].type for turn in turns] == [EventType.COMPLETED, EventType.COMPLETED]
    assert len(runner.launches) == 2
    command, cwd, env = runner.launches[1]
    assert command[:3] == [sys.executable, str(FAKE_CLAUDE), "ok"]
    assert "--resume" in command
    assert cwd == tmp_path
    assert env == HEADLESS_GIT_ENV


def test_codex_launches_go_through_the_runner(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = CodexAdapter(
        tmp_path, command=[sys.executable, str(FAKE_CODEX), "ok"], runner=runner, codex_home=tmp_path / "codex-home"
    )

    turns = run_turns(adapter, "hello")

    assert turns[0][-1].type is EventType.COMPLETED
    assert [launch[1] for launch in runner.launches] == [tmp_path]


def test_stop_kills_through_the_runner(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), "hang"], runner=runner)

    async def run() -> list:
        events = []
        async for event in adapter.start_task("wait"):
            events.append(event)
            if event.type is EventType.STARTED:
                asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(adapter.stop()))
        return events

    events = asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert events[-1].data.get("stopped") is True
