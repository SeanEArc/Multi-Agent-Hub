from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlmodel import Session

from app.adapters.ollama import OllamaUnavailable, list_models, pick_model
from app.config import get_settings
from app.db import get_session
from app.models import load_hub_settings

router = APIRouter(prefix="/local", tags=["local"])

DbSession = Annotated[Session, Depends(get_session)]


class LocalModel(BaseModel):
    name: str
    size: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    # None when the server doesn't report capabilities.
    tools: bool | None = None
    thinking: bool | None = None


class LocalModels(BaseModel):
    url: str
    # False when the server isn't running; `error` then says so.
    available: bool
    models: list[LocalModel]
    # The model a session gets when it doesn't name one, or None if there is none to use.
    default: str | None = None
    error: str | None = None


@router.get("/models", response_model=LocalModels)
async def local_models(request: Request, db: DbSession) -> LocalModels:
    """The models installed in Ollama, for picking a session's local model."""
    settings = get_settings()
    url = request.app.state.ollama_url or settings.ollama_url
    try:
        models = await list_models(url, transport=request.app.state.ollama_transport)
    except OllamaUnavailable as exc:
        return LocalModels(url=url, available=False, models=[], error=str(exc))
    chosen = load_hub_settings(db).local_model
    try:
        default = pick_model(models, chosen, settings.ollama_model)
        error = None
    except OllamaUnavailable as exc:
        default, error = None, str(exc)
    return LocalModels(url=url, available=True, models=[LocalModel(**m) for m in models], default=default, error=error)
