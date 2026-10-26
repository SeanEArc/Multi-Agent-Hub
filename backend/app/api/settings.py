from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db import get_session
from app.models import HubSettings, load_hub_settings, save_hub_settings

router = APIRouter(prefix="/settings", tags=["settings"])

DbSession = Annotated[Session, Depends(get_session)]


@router.get("", response_model=HubSettings)
def get_settings(db: DbSession) -> HubSettings:
    """The default handoff chain and limit policy (for sessions that don't set their own), and UI preferences."""
    return load_hub_settings(db)


@router.put("", response_model=HubSettings)
def put_settings(body: HubSettings, db: DbSession) -> HubSettings:
    save_hub_settings(db, body)
    return load_hub_settings(db)
