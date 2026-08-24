"""FastAPI application entry point.

Route order matters: the resolver claims ``GET /{igsn}``, which would otherwise
swallow ``/api/...``, ``/docs`` and ``/healthz``. The API router is included
first and the resolver's path pattern is constrained to the IGSN shape, so both
hold.
"""

import asyncio
import contextlib
import logging

from fastapi import FastAPI

from . import resolver
from .api import v1
from .config import get_settings
from .db import connect, disconnect, ensure_indexes
from .services import publisher

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await connect()
    await ensure_indexes()

    task: asyncio.Task | None = None
    if settings.publisher_enabled:
        task = asyncio.create_task(publisher.run_forever(), name="igsn-publisher")

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await disconnect()


def create_app() -> FastAPI:
    app = FastAPI(
        title="IGSN Registry",
        description=(
            "Centralized allocation and DataCite publication of IGSNs "
            "(persistent identifiers for physical samples)."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    app.include_router(v1.router)
    # Must come last: its `/{igsn}` pattern is a catch-all at the root.
    app.include_router(resolver.router)
    return app


app = create_app()
