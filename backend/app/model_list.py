"""The models each CLI agent can run, as close to the user's own subscription as the CLIs allow.

Claude Code has no command or cache that lists a plan's models, so its list is the current full
names (the aliases such as `opus` are left out: they only repeat the newest of each family), trimmed by what the hub
learns: a turn that fails because the plan can't use a model marks it unavailable, one that
works marks it available, and the optional check sends each model a tiny prompt.

Codex keeps the models of the signed-in account in `$CODEX_HOME/models_cache.json` (an
undocumented format, read best effort). Only the `models` entries are read, never `auth.json`
or the account details in the same file. Without it, a built-in list is shown.

Results are stored per user in the `settings` table under `model_availability`.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from sqlmodel import Session

from app.models import EFFORTS, AgentKind, Setting, utcnow

log = logging.getLogger(__name__)

AVAILABILITY_KEY = "model_availability"
CHECKS_KEY = "model_checks"

# Current full names (Anthropic's model list, 2026-11-03). Update with app releases; retired ones drop off.
CLAUDE_VERSIONS = [
    ("claude-fable-5-2", "Fable 5.2"),
    ("claude-fable-5-1", "Fable 5.1"),
    ("claude-fable-5", "Fable 5"),
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-opus-5", "Opus 5"),
    ("claude-opus-4-8", "Opus 4.8"),
    ("claude-opus-4-7", "Opus 4.7"),
    ("claude-opus-4-6", "Opus 4.6"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-sonnet-5", "Sonnet 5"),
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-haiku-4-5", "Haiku 4.5"),
]
# Shown when Codex's account cache can't be read (codex-cli 0.164.0).
CODEX_FALLBACK = [
    ("gpt-6.1-sol", "GPT-6.1-Sol"),
    ("gpt-6-sol", "GPT-6-Sol"),
    ("gpt-6-luna", "GPT-6-Luna"),
]

# How the CLIs say a model can't be used: Claude Code's "There's an issue with the selected model (x).
# It may not exist or you may not have access to it", and the usual API wordings.
UNAVAILABLE_MODEL = re.compile(
    r"selected model|may not exist|not have access to (it|this model)|model[^.\n]{0,60}"
    r"(not found|not available|isn't available|does not exist|doesn't exist|not supported|unsupported)"
    r"|(unknown|invalid|unsupported) model",
    re.IGNORECASE,
)

Group = Literal["latest", "version", "account"]


class ModelOption(BaseModel):
    id: str
    label: str
    group: Group
    # True: worked on this user's plan; False: the plan can't use it; None: not known yet.
    available: bool | None = None
    note: str | None = None
    checked_at: str | None = None
    # The reasoning efforts the hub can pass for this model, lowest first (empty: none), and the CLI's default.
    efforts: list[str] = list(EFFORTS)
    default_effort: str | None = None


class ModelCatalog(BaseModel):
    claude: list[ModelOption]
    codex: list[ModelOption]
    # Whether Codex's list came from the user's account cache or the built-in list.
    codex_source: Literal["account", "builtin"]
    # When "Check models" last ran, per agent.
    checked_at: dict[str, str | None]


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_entries(codex_home: Path) -> list[dict[str, Any]] | None:
    """The listed model entries of the signed-in Codex account, by priority, or None if unreadable."""
    try:
        data = json.loads((codex_home / "models_cache.json").read_text(encoding="utf-8"))
        entries = data["models"]
        listed = sorted(
            (m for m in entries if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"),
            key=lambda m: m.get("priority", 1_000),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return listed or None


def read_codex_models(codex_home: Path) -> list[tuple[str, str]] | None:
    """The listed models of the signed-in Codex account, or None if the cache is missing or unreadable."""
    entries = _codex_entries(codex_home)
    return [(str(m["slug"]), str(m.get("display_name") or m["slug"])) for m in entries] if entries else None


def read_codex_efforts(codex_home: Path) -> dict[str, tuple[list[str], str | None]]:
    """Each listed Codex model's reasoning efforts (only those the hub offers, lowest first) and its default.

    Levels beyond max (such as "ultra", which also delegates on its own) are left out.
    """
    efforts: dict[str, tuple[list[str], str | None]] = {}
    for m in _codex_entries(codex_home) or []:
        try:
            listed = {
                str(level["effort"] if isinstance(level, dict) else level)
                for level in m.get("supported_reasoning_levels") or []
            }
        except (KeyError, TypeError):
            continue
        if not listed:
            continue
        default = m.get("default_reasoning_level")
        efforts[str(m["slug"])] = ([e for e in EFFORTS if e in listed], default if default in EFFORTS else None)
    return efforts


def is_unavailable_model_error(message: str | None) -> bool:
    return bool(message) and UNAVAILABLE_MODEL.search(message) is not None


def _load(db: Session, key: str) -> dict[str, Any]:
    row = db.get(Setting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _save(db: Session, key: str, value: dict[str, Any]) -> None:
    db.merge(Setting(key=key, value=value))
    db.commit()


def record_availability(
    db: Session, agent: AgentKind, model: str, available: bool | None, note: str | None = None
) -> None:
    """Remember whether `model` works on the user's plan (None: unknown, e.g. out of usage during a check)."""
    stored = _load(db, AVAILABILITY_KEY)
    per_agent = dict(stored.get(agent.value) or {})
    current = per_agent.get(model) or {}
    if available is None and current.get("available") is not None:
        return  # an inconclusive result doesn't erase a known one
    if current.get("available") == available and current.get("note") == note and available is not None:
        return  # nothing new: skip the write after every turn
    per_agent[model] = {"available": available, "note": note, "checked_at": utcnow().isoformat()}
    stored[agent.value] = per_agent
    _save(db, AVAILABILITY_KEY, stored)


def record_check(db: Session, agent: AgentKind) -> None:
    checks = _load(db, CHECKS_KEY)
    checks[agent.value] = utcnow().isoformat()
    _save(db, CHECKS_KEY, checks)


def model_ids(agent: AgentKind, codex_home: Path) -> list[str]:
    """Every model the check tries for `agent`."""
    if agent is AgentKind.CLAUDE:
        return [m for m, _ in CLAUDE_VERSIONS]
    return [m for m, _ in (read_codex_models(codex_home) or CODEX_FALLBACK)]


def catalog(db: Session, codex_home: Path) -> ModelCatalog:
    stored = _load(db, AVAILABILITY_KEY)
    checks = _load(db, CHECKS_KEY)

    codex_efforts = read_codex_efforts(codex_home)

    def options(agent: AgentKind, entries: list[tuple[str, str]], group: Group) -> list[ModelOption]:
        known = stored.get(agent.value) or {}
        result = []
        for model, label in entries:
            option = ModelOption(id=model, label=label, group=group, **{
                k: (known.get(model) or {}).get(k) for k in ("available", "note", "checked_at")
            })
            # Claude Code takes every level for any model; Codex lists them per model in its cache.
            if agent is AgentKind.CODEX and model in codex_efforts:
                option.efforts, option.default_effort = codex_efforts[model]
            result.append(option)
        return result

    account = read_codex_models(codex_home)
    return ModelCatalog(
        claude=options(AgentKind.CLAUDE, CLAUDE_VERSIONS, "version"),
        codex=options(AgentKind.CODEX, account, "account") if account else options(AgentKind.CODEX, CODEX_FALLBACK, "version"),
        codex_source="account" if account else "builtin",
        checked_at={agent.value: checks.get(agent.value) for agent in (AgentKind.CLAUDE, AgentKind.CODEX)},
    )
