import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from app.db import get_session
from app.model_list import ModelCatalog, catalog, model_ids
from app.models import AgentKind
from app.orchestrator import AgentUnavailable, Orchestrator, SessionBusy

router = APIRouter(prefix="/models", tags=["models"])

DbSession = Annotated[Session, Depends(get_session)]


class ModelCheck(BaseModel):
    agent: AgentKind


@router.get("", response_model=ModelCatalog)
def list_models(db: DbSession, request: Request) -> ModelCatalog:
    """Claude's and Codex's models, with what the hub knows about whether the user's plan can use each."""
    return catalog(db, request.app.state.codex_home)


@router.post("/check", response_model=ModelCatalog)
async def check_models(body: ModelCheck, db: DbSession, request: Request) -> ModelCatalog:
    """Send each of the agent's models a tiny prompt (a little of the user's usage) and record the results.

    Waits until every model has answered, up to a few minutes.
    """
    orchestrator: Orchestrator = request.app.state.orchestrator
    codex_home: Path = request.app.state.codex_home
    # An empty folder of its own, outside any repository, so no project's CLAUDE.md or files are loaded.
    cwd = Path(tempfile.mkdtemp(prefix="hub-model-check-"))
    try:
        await orchestrator.check_models(body.agent, model_ids(body.agent, codex_home), cwd)
    except AgentUnavailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except SessionBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    db.expire_all()
    return catalog(db, codex_home)
