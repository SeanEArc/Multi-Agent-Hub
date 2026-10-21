import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.engine import Engine

from app.api import health, sessions
from app.config import get_settings
from app.db import init_db, make_engine
from app.orchestrator import AdapterFactory, Orchestrator, default_adapter_factory


def create_app(engine: Engine | None = None, adapter_factory: AdapterFactory = default_adapter_factory) -> FastAPI:
    settings = get_settings()
    engine = engine or make_engine(settings.database_url)
    orchestrator = Orchestrator(engine, adapter_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(engine)
        orchestrator.recover_interrupted()
        yield
        await orchestrator.shutdown()
        engine.dispose()

    app = FastAPI(title="Multi-Agent Hub", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.orchestrator = orchestrator

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
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
