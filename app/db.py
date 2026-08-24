"""Mongo wiring and index creation.

The unique index on ``records.igsn`` is not an optimization -- it is the
correctness backstop for identifier allocation. If a counter is ever behind the
records that already exist (an imperfect backfill, a restored dump), the
allocator's insert fails with a duplicate key and it re-increments instead of
handing the same IGSN out twice. Never drop it.
"""

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, IndexModel

from .config import get_settings

logger = logging.getLogger(__name__)

TENANTS = "tenants"
COUNTERS = "counters"
RECORDS = "records"
VOCABULARIES = "vocabularies"
ATTEMPTS = "publication_attempts"
AUDIT = "audit"

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def set_db(db: AsyncIOMotorDatabase) -> None:
    """Install a database handle. Used at startup and by tests."""
    global _db
    _db = db


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database is not initialized; call connect() or set_db() first")
    return _db


async def connect() -> AsyncIOMotorDatabase:
    global _client
    settings = get_settings()
    _client = AsyncIOMotorClient(settings.mongo_uri, tz_aware=True)
    set_db(_client[settings.mongo_db])
    return get_db()


async def disconnect() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


async def ensure_indexes(db: AsyncIOMotorDatabase | None = None) -> None:
    db = db if db is not None else get_db()

    await db[TENANTS].create_indexes(
        [
            IndexModel([("slug", ASCENDING)], unique=True),
            IndexModel([("token_sha256", ASCENDING)], unique=True, sparse=True),
        ]
    )
    await db[RECORDS].create_indexes(
        [
            # The allocation guarantee. See module docstring.
            IndexModel([("igsn", ASCENDING)], unique=True),
            IndexModel([("doi", ASCENDING)], unique=True),
            IndexModel([("parent_igsn", ASCENDING)]),
            IndexModel([("prefix", ASCENDING)]),
            IndexModel([("tenant", ASCENDING), ("status", ASCENDING)]),
        ]
    )
    await db[ATTEMPTS].create_indexes(
        [
            IndexModel([("status", ASCENDING), ("next_attempt_at", ASCENDING)]),
            IndexModel([("igsn", ASCENDING)]),
        ]
    )
    await db[AUDIT].create_indexes(
        [
            IndexModel([("tenant", ASCENDING), ("created", ASCENDING)]),
            IndexModel([("igsn", ASCENDING)]),
        ]
    )
    logger.info("Mongo indexes ensured")
