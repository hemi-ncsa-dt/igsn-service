"""Durable publication queue.

FastAPI's ``BackgroundTasks`` is in-memory: a restart mid-publish would silently
lose the work, and a DataCite outage would need retries the process can't
remember. Rather than pull Celery and a broker into this service, publication
attempts are rows in ``publication_attempts`` drained by a single asyncio loop.
Claiming is an atomic ``find_one_and_update``, so running more than one replica
is safe.
"""

import asyncio
import datetime
import logging

from bson import ObjectId
from pymongo import ReturnDocument

from ..config import get_settings
from ..db import ATTEMPTS, RECORDS, TENANTS, get_db
from . import datacite
from .allocator import MetadataError, validate_metadata

logger = logging.getLogger(__name__)

# An attempt claimed but not finished within this window is presumed orphaned by
# a crash or restart and is requeued.
STALE_CLAIM_SECONDS = 600


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def enqueue(
    igsn: str, tenant: dict, target: str, recurse: bool = False, db=None
) -> list[str]:
    """Queue ``igsn`` (and optionally its children) for publication."""
    db = db if db is not None else get_db()
    igsns = [igsn]
    if recurse:
        cursor = db[RECORDS].find(
            {"parent_igsn": igsn, "status": {"$ne": "revoked"}}, {"igsn": 1}
        )
        # Publish the parent first: DataCite resolves the children's IsPartOf
        # relation against it.
        igsns += [doc["igsn"] async for doc in cursor]

    now = _now()
    attempt_ids = []
    for item in igsns:
        doc = {
            "igsn": item,
            "tenant": tenant["slug"],
            "target": target,
            "status": "queued",
            "attempts": 0,
            "next_attempt_at": now,
            "last_error": None,
            "created": now,
            "updated": now,
        }
        result = await db[ATTEMPTS].insert_one(doc)
        attempt_ids.append(str(result.inserted_id))
    return attempt_ids


async def _claim(db) -> dict | None:
    return await db[ATTEMPTS].find_one_and_update(
        {"status": "queued", "next_attempt_at": {"$lte": _now()}},
        {"$set": {"status": "running", "claimed_at": _now()}},
        sort=[("next_attempt_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def requeue_stale(db=None) -> int:
    """Return attempts orphaned by a crash to the queue."""
    db = db if db is not None else get_db()
    cutoff = _now() - datetime.timedelta(seconds=STALE_CLAIM_SECONDS)
    result = await db[ATTEMPTS].update_many(
        {"status": "running", "claimed_at": {"$lt": cutoff}},
        {"$set": {"status": "queued", "next_attempt_at": _now(), "updated": _now()}},
    )
    if result.modified_count:
        logger.warning("Requeued %s stale publication attempt(s)", result.modified_count)
    return result.modified_count


async def _finish(
    db, attempt, status: str, error: str | None = None, request: dict | None = None
) -> None:
    updates = {"status": status, "last_error": error, "updated": _now()}
    if request is not None:
        # Keep what was sent alongside what came back; a DataCite validation
        # error names a field but never its value.
        updates["last_request"] = request
    await db[ATTEMPTS].update_one({"_id": attempt["_id"]}, {"$set": updates})


async def _retry_later(db, attempt, error: str, request: dict | None = None) -> None:
    settings = get_settings()
    attempts = int(attempt.get("attempts", 0)) + 1
    if attempts >= settings.publisher_max_attempts:
        logger.error(
            "Giving up on %s after %s attempts: %s", attempt["igsn"], attempts, error
        )
        await db[ATTEMPTS].update_one(
            {"_id": attempt["_id"]},
            {
                "$set": {
                    "status": "failed",
                    "attempts": attempts,
                    "last_error": error,
                    "last_request": request,
                    "updated": _now(),
                }
            },
        )
        await db[RECORDS].update_one(
            {"igsn": attempt["igsn"]},
            {
                "$set": {
                    "datacite.last_error": error,
                    "datacite.last_request": request,
                    "updated": _now(),
                }
            },
        )
        return

    delay = get_settings().publisher_backoff_base_seconds * (2 ** (attempts - 1))
    await db[ATTEMPTS].update_one(
        {"_id": attempt["_id"]},
        {
            "$set": {
                "status": "queued",
                "attempts": attempts,
                "last_error": error,
                "last_request": request,
                "next_attempt_at": _now() + datetime.timedelta(seconds=delay),
                "updated": _now(),
            }
        },
    )


async def process_attempt(attempt: dict, db=None) -> str:
    """Run one publication attempt. Returns its terminal-or-requeued status."""
    db = db if db is not None else get_db()
    record = await db[RECORDS].find_one({"igsn": attempt["igsn"]})
    if record is None:
        await _finish(db, attempt, "failed", "Record no longer exists")
        return "failed"
    if record["status"] == "revoked":
        await _finish(db, attempt, "failed", "Record is revoked")
        return "failed"

    tenant = await db[TENANTS].find_one({"slug": attempt["tenant"]})
    if tenant is None:
        await _finish(db, attempt, "failed", f"Tenant {attempt['tenant']} no longer exists")
        return "failed"

    # Refuse to push an incomplete record rather than letting DataCite 422 on it.
    try:
        validate_metadata(record.get("metadata"))
        if not record.get("metadata"):
            raise MetadataError("Record has no metadata; nothing to publish")
    except MetadataError as exc:
        await _finish(db, attempt, "failed", str(exc))
        await db[RECORDS].update_one(
            {"igsn": record["igsn"]},
            {"$set": {"status": "failed", "datacite.last_error": str(exc), "updated": _now()}},
        )
        return "failed"

    try:
        result = await datacite.submit(record, tenant, attempt["target"])
    except datacite.DataCiteError as exc:
        # datacite.attempts counts tries, not successes -- a record stuck at 0
        # after repeated failures tells you nothing about how hard we tried.
        await db[RECORDS].update_one(
            {"igsn": record["igsn"]}, {"$inc": {"datacite.attempts": 1}}
        )
        if exc.retryable:
            await _retry_later(db, attempt, str(exc), request=exc.request)
            return "queued"
        await _finish(db, attempt, "failed", str(exc), request=exc.request)
        await db[RECORDS].update_one(
            {"igsn": record["igsn"]},
            {
                "$set": {
                    "status": "failed",
                    "datacite.last_error": str(exc),
                    "datacite.last_request": exc.request or None,
                    "updated": _now(),
                }
            },
        )
        return "failed"

    now = _now()
    await db[RECORDS].update_one(
        {"igsn": record["igsn"]},
        {
            "$set": {
                "status": result["status"],
                "published_at": record.get("published_at") or now,
                "datacite.last_sync": result["synced_at"],
                "datacite.last_error": None,
                "datacite.last_request": result.get("request"),
                "updated": now,
            },
            "$inc": {"datacite.attempts": 1},
        },
    )
    await _finish(db, attempt, "done", request=result.get("request"))
    logger.info("Published %s as %s", record["igsn"], result["status"])
    return "done"


async def drain_once(db=None) -> int:
    """Process every attempt that is currently due. Returns how many ran."""
    db = db if db is not None else get_db()
    processed = 0
    while True:
        attempt = await _claim(db)
        if attempt is None:
            return processed
        processed += 1
        try:
            await process_attempt(attempt, db=db)
        except Exception:  # noqa: BLE001 - a bad attempt must not kill the loop
            logger.exception("Unhandled error processing attempt %s", attempt["_id"])
            await _retry_later(db, attempt, "Unhandled internal error")


async def run_forever() -> None:
    settings = get_settings()
    db = get_db()
    await requeue_stale(db)
    logger.info("Publisher loop started (poll=%ss)", settings.publisher_poll_seconds)
    while True:
        try:
            await drain_once(db)
        except asyncio.CancelledError:
            logger.info("Publisher loop stopped")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Publisher loop iteration failed")
        await asyncio.sleep(settings.publisher_poll_seconds)


async def attempts_for(igsn: str, db=None, limit: int = 20) -> list[dict]:
    """Most recent publication attempts for one IGSN, newest first.

    This is the debugging surface: each attempt carries the error DataCite
    returned *and* the request summary that produced it.
    """
    db = db if db is not None else get_db()
    cursor = (
        db[ATTEMPTS].find({"igsn": igsn.upper()}).sort("created", -1).limit(limit)
    )
    out = []
    async for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        out.append(doc)
    return out


async def get_attempt(attempt_id: str, db=None) -> dict | None:
    db = db if db is not None else get_db()
    try:
        oid = ObjectId(attempt_id)
    except Exception:  # noqa: BLE001
        return None
    return await db[ATTEMPTS].find_one({"_id": oid})
