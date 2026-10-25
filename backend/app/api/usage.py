from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlmodel import Session, SQLModel, col, select

from app.adapters.base import EventType
from app.db import get_session
from app.models import AgentKind, SessionEvent

router = APIRouter(prefix="/usage", tags=["usage"])

DbSession = Annotated[Session, Depends(get_session)]

# Claude reports a reached limit as LIMIT_REACHED with the same usage-window data.
USAGE_EVENTS = [EventType.RATE_LIMIT, EventType.LIMIT_REACHED]


class AgentUsage(SQLModel):
    status: str | None
    windows: dict[str, Any]
    plan_type: str | None
    # When the agent reported it; limits only update when that agent runs a turn in the hub.
    reported_at: datetime


@router.get("", response_model=dict[AgentKind, AgentUsage])
def get_usage(db: DbSession) -> dict[AgentKind, AgentUsage]:
    """Each agent's latest plan usage windows from any session, since limits belong to the account."""
    usage = {}
    for agent in AgentKind:
        rows = db.exec(
            select(SessionEvent)
            .where(SessionEvent.agent == agent, col(SessionEvent.type).in_(USAGE_EVENTS))
            .order_by(col(SessionEvent.id).desc())
        )
        # A limit found from error text carries no windows; skip to the last report that has them.
        row = next((r for r in rows if (r.payload or {}).get("windows")), None)
        if row is not None:
            usage[agent] = AgentUsage(
                status=row.payload.get("status"),
                windows=row.payload["windows"],
                plan_type=row.payload.get("plan_type"),
                reported_at=row.created_at,
            )
    return usage
