"""Test fixtures.

These run against a real MongoDB rather than a mock on purpose: the two things
this service must get right -- atomic counter increments and unique-index
rejection of duplicate IGSNs -- are exactly the semantics a mock is most likely
to fake. Point ``IGSN_TEST_MONGO_URI`` at any instance; the default matches

    docker run -d --name igsn-test-mongo -p 27117:27017 mongo:7
"""

import os

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from app import db as db_module
from app.auth import generate_token, hash_token
from app.config import get_settings

TEST_MONGO_URI = os.environ.get("IGSN_TEST_MONGO_URI", "mongodb://localhost:27117")

# Set before Settings is ever constructed so cached settings pick these up.
os.environ.setdefault("IGSN_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("IGSN_PUBLIC_URL", "https://igsn.example.org")
os.environ.setdefault("IGSN_PUBLISHER_ENABLED", "false")
os.environ.setdefault("DATACITE_PW_TEST", "s3cret")


def pytest_configure(config):
    get_settings.cache_clear()


TEST_DB_NAME = "igsn_test"

# Collections are emptied between tests rather than the database being dropped
# and recreated: rebuilding six collections' indexes per test churns enough
# WiredTiger files to panic the server, and index creation is idempotent anyway.
_indexes_ready = False


@pytest_asyncio.fixture
async def database():
    global _indexes_ready

    client = AsyncIOMotorClient(TEST_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    try:
        await client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        client.close()
        pytest.skip(f"No MongoDB at {TEST_MONGO_URI}: {exc}")

    db = client[TEST_DB_NAME]
    if not _indexes_ready:
        await db_module.ensure_indexes(db)
        _indexes_ready = True

    await _clear(db)
    db_module.set_db(db)
    try:
        yield db
    finally:
        await _clear(db)
        client.close()


async def _clear(db):
    for name in (
        db_module.TENANTS,
        db_module.COUNTERS,
        db_module.RECORDS,
        db_module.VOCABULARIES,
        db_module.ATTEMPTS,
        db_module.AUDIT,
    ):
        await db[name].delete_many({})


@pytest_asyncio.fixture
async def tenant(database):
    """A tenant permitted to mint under JH and NW, on the DataCite test API."""
    token = generate_token()
    doc = {
        "slug": "test-tenant",
        "name": "Test Tenant",
        "allowed_institutions": ["JH", "NW"],
        "datacite": {
            "repository_id": "jhu.igsn-test",
            "doi_prefix": "10.83961",
            "api_base": "https://api.test.datacite.org",
            "password_env": "DATACITE_PW_TEST",
        },
        "landing_template": None,
        "active": True,
    }
    await database["tenants"].insert_one({**doc, "token_sha256": hash_token(token)})
    doc["_token"] = token
    return doc


@pytest_asyncio.fixture
async def other_tenant(database):
    token = generate_token()
    doc = {
        "slug": "other-tenant",
        "name": "Other Tenant",
        "allowed_institutions": ["TM"],
        "datacite": {
            "repository_id": "tamu.igsn-test",
            "doi_prefix": "10.99999",
            "api_base": "https://api.test.datacite.org",
            "password_env": "DATACITE_PW_TEST",
        },
        "active": True,
    }
    await database["tenants"].insert_one({**doc, "token_sha256": hash_token(token)})
    doc["_token"] = token
    return doc


@pytest.fixture
def metadata():
    """Minimal metadata that satisfies datacite-v4.5.json's required fields."""
    return {
        "titles": [{"title": "Test Sample"}],
        "creators": [{"name": "Doe, Jane", "nameType": "Personal"}],
        "publisher": {"name": "Test Institute"},
        "publicationYear": "2026",
        "types": {"resourceType": "material sample", "resourceTypeGeneral": "PhysicalObject"},
        "schemaVersion": "http://datacite.org/schema/kernel-4",
    }


@pytest_asyncio.fixture
async def client(database):
    """HTTP client bound to the ASGI app, bypassing the network."""
    import httpx

    from app.main import create_app

    app = create_app()
    # The app's lifespan would reconnect to the configured Mongo and start the
    # publisher; tests drive both explicitly, so it is skipped here.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://igsn.example.org"
    ) as http_client:
        yield http_client


@pytest.fixture
def auth(tenant):
    return {"Authorization": f"Bearer {tenant['_token']}"}


@pytest.fixture
def admin_auth():
    return {"Authorization": f"Bearer {os.environ['IGSN_ADMIN_TOKEN']}"}
