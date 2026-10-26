"""The interface every agent (Claude, Codex, Ollama) implements.

The orchestrator only talks to this interface, so how an agent is driven
(headless CLI, pseudo-terminal, API) can change without touching handoff,
the UI or history. An adapter instance belongs to one session and is created
with that session's working directory and model.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.models import AgentKind


class EventType(StrEnum):
    USER_MESSAGE = "user_message"  # recorded by the orchestrator, not emitted by adapters
    HANDOFF = "handoff"  # orchestrator: a new agent took over (divider in the transcript)
    HANDOFF_OFFER = "handoff_offer"  # orchestrator: a limit was hit and the user must confirm the handoff
    STARTED = "started"
    MESSAGE = "message"  # assistant text
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    PLAN = "plan"  # the agent's to-do list, whole each time: {items: [{text, status: pending|in_progress|completed}]}
    PERMISSION_DENIED = "permission_denied"  # a tool call needed approval and was refused
    QUESTION = "question"  # turn ended waiting for user input
    RATE_LIMIT = "rate_limit"  # usage-window status; early warning before a limit
    USAGE = "usage"  # token / cost info for the turn
    MODEL = "model"  # the model the agent used, when it is only known after the turn (Codex)
    LIMIT_REACHED = "limit_reached"  # usage limit hit; triggers handoff
    ERROR = "error"
    COMPLETED = "completed"


@dataclass
class AgentEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    # Untouched event from the CLI, stored for debugging and later re-parsing.
    raw: dict[str, Any] | None = None


class AgentAdapter(ABC):
    kind: AgentKind

    @abstractmethod
    def start_task(self, prompt: str) -> AsyncGenerator[AgentEvent]:
        """Start a new task and stream its events until the turn ends."""

    @abstractmethod
    def send_reply(self, reply: str) -> AsyncGenerator[AgentEvent]:
        """Answer the agent's question (or continue the task) and stream the resulting events."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the running turn, including any child processes."""

    @property
    @abstractmethod
    def native_session_id(self) -> str | None:
        """The agent's own session id, once known, so the task can be resumed."""
