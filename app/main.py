"""FastAPI application entry point.

Route order matters: the resolver claims ``GET /{igsn}``, which would otherwise
swallow ``/api/...``, ``/docs`` and the ``/healthz`` and ``/readyz`` probes. The
API router is included first and the resolver's path pattern is constrained to
the IGSN shape, so both hold.
"""

import asyncio
import contextlib
import logging

from fastapi import FastAPI, HTTPException

from . import resolver
from .api import v1
from .config import get_settings
from .db import connect, disconnect, ensure_indexes, get_db
from .services import publisher

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await connect()
    await ensure_indexes()

    task: asyncio.Task | None = None
    # Both flags must be on: publisher_enabled says this process is the one that
    # drains, datacite_enabled says draining is allowed at all.
    if settings.publisher_enabled and settings.datacite_enabled:
        task = asyncio.create_task(publisher.run_forever(), name="igsn-publisher")
    elif settings.publisher_enabled:
        logger.warning(
            "Publisher loop not started: IGSN_DATACITE_ENABLED is false. "
            "Nothing will be sent to DataCite."
        )

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
        """Liveness: is the process running? Deliberately does not touch Mongo --
        a database outage should not get every pod killed and restarted."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        """Readiness: can this pod actually serve a request?

        Separate from /healthz because every route but the two probes needs
        Mongo. A pod that reports ready on process start alone joins the Service
        before it can answer anything and 500s the traffic it is handed.
        """
        try:
            await get_db().client.admin.command("ping")
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            raise HTTPException(
                status_code=503, detail=f"MongoDB unavailable: {type(exc).__name__}"
            ) from exc
        return {"status": "ready"}

    app.include_router(v1.router)
    # Must come last: its `/{igsn}` pattern is a catch-all at the root.
    app.include_router(resolver.router)
    return app


app = create_app()
