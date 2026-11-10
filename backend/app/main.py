from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx2 import AsyncBaseTransport
from sqlalchemy.engine import Engine

from app.api import folders, health, local, sessions, settings as hub_settings, usage
from app.auth import HostOriginMiddleware, load_or_create_token, require_caller
from app.config import get_settings
from app.db import init_db, make_engine
from app.orchestrator import AdapterFactory, Orchestrator, default_adapter_factory


def create_app(
    engine: Engine | None = None,
    adapter_factory: AdapterFactory = default_adapter_factory,
    token: str | None = None,
    worktrees_dir: Path | None = None,
    chats_dir: Path | None = None,
    local_dir: Path | None = None,
    ollama_url: str | None = None,
    ollama_transport: AsyncBaseTransport | None = None,
) -> FastAPI:
    """Tests pass `token`, the folders and a fake Ollama transport; otherwise they come from the settings."""
    settings = get_settings()
    engine = engine or make_engine(settings.database_url)
    orchestrator = Orchestrator(engine, adapter_factory, simulate_limit=set(settings.simulate_limit))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.token = (
            token
            or (settings.token.get_secret_value() if settings.token else None)
            or load_or_create_token(settings.token_file)
        )
        init_db(engine)
        orchestrator.recover_interrupted()
        yield
        await orchestrator.shutdown()
        engine.dispose()

    app = FastAPI(title="Multi-Agent Hub", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.orchestrator = orchestrator
    app.state.worktrees_dir = worktrees_dir or settings.worktrees_dir
    app.state.chats_dir = chats_dir or settings.chats_dir
    app.state.local_dir = local_dir or settings.local_dir
    app.state.ollama_url = ollama_url
    app.state.ollama_transport = ollama_transport

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last, so it runs first: bad Host/Origin is rejected before anything else.
    app.add_middleware(HostOriginMiddleware, allowed_origins=settings.cors_origins)

    authenticated = [Depends(require_caller)]
    app.include_router(health.router, prefix="/api", dependencies=authenticated)
    app.include_router(sessions.router, prefix="/api", dependencies=authenticated)
    app.include_router(folders.router, prefix="/api", dependencies=authenticated)
    app.include_router(hub_settings.router, prefix="/api", dependencies=authenticated)
    app.include_router(usage.router, prefix="/api", dependencies=authenticated)
    app.include_router(local.router, prefix="/api", dependencies=authenticated)
    return app


app = create_app()


if __name__ == "__main__":
    from app.server import run

    run()
