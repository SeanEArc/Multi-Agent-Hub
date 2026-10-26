import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.engine import Engine

from app.api import folders, health, sessions, settings as hub_settings, usage
from app.auth import HostOriginMiddleware, load_or_create_token, require_caller
from app.config import get_settings
from app.db import init_db, make_engine
from app.orchestrator import AdapterFactory, Orchestrator, default_adapter_factory


def create_app(
    engine: Engine | None = None,
    adapter_factory: AdapterFactory = default_adapter_factory,
    token: str | None = None,
    worktrees_dir: Path | None = None,
) -> FastAPI:
    """Tests pass `token` and `worktrees_dir`; otherwise they come from the settings."""
    settings = get_settings()
    engine = engine or make_engine(settings.database_url)
    orchestrator = Orchestrator(engine, adapter_factory, simulate_limit=set(settings.simulate_limit))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.token = token or load_or_create_token(settings.token_file)
        init_db(engine)
        orchestrator.recover_interrupted()
        yield
        await orchestrator.shutdown()
        engine.dispose()

    app = FastAPI(title="Multi-Agent Hub", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.orchestrator = orchestrator
    app.state.worktrees_dir = worktrees_dir or settings.worktrees_dir

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
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_level=settings.log_level,
    )
