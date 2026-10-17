"""Operational surface: readiness probing and the DataCite master switch.

The switch is what makes it safe to run this service in production before
anyone is willing to spend money at DataCite, so it is tested at every layer
that is supposed to hold it -- the endpoint, the drain loop, and the client
itself -- rather than only at the top.
"""

import httpx
import pytest
import respx

from app.config import get_settings
from app.services import allocator, datacite, publisher


@pytest.fixture
async def record(database, tenant, metadata):
    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    return docs[0]


@pytest.fixture
def datacite_off(monkeypatch):
    """Turn publication off the way the deployment does."""
    monkeypatch.setattr(get_settings(), "datacite_enabled", False)


class TestHealthAndReadiness:
    async def test_healthz_does_not_need_mongo(self, client):
        # Liveness must not depend on the database, or an outage restarts
        # every pod instead of just failing requests.
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_readyz_ok_when_mongo_is_reachable(self, client, database):
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    async def test_readyz_is_503_when_mongo_is_gone(self, client, database, monkeypatch):
        from app import db as db_module

        class Unreachable:
            class admin:
                @staticmethod
                async def command(_):
                    raise ConnectionError("no server")

        class FakeDb:
            client = Unreachable

        monkeypatch.setattr(db_module, "get_db", lambda: FakeDb)
        monkeypatch.setattr("app.main.get_db", lambda: FakeDb)

        response = await client.get("/readyz")
        assert response.status_code == 503
        assert "MongoDB unavailable" in response.json()["detail"]

    async def test_readyz_is_not_shadowed_by_the_resolver(self, client, database):
        # The resolver owns /{igsn}; a probe path that fell through to it would
        # 404 and take the pod out of service.
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200


class TestDataCiteDisabled:
    async def test_publish_is_503_and_queues_nothing(
        self, client, auth, record, database, datacite_off
    ):
        response = await client.post(
            f"/api/v1/records/{record['igsn']}/publish", headers=auth, json={}
        )
        assert response.status_code == 503
        assert "IGSN_DATACITE_ENABLED" in response.json()["detail"]
        # The point of refusing rather than queueing: no backlog to fire off
        # the moment publication is switched on.
        assert await database["publication_attempts"].count_documents({}) == 0

    async def test_allocation_still_works(self, client, auth, metadata, datacite_off):
        # Allocation never touches DataCite, so the switch must not affect it.
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={"prefix": "JHAMAA", "metadata": metadata},
        )
        assert response.status_code == 201
        assert response.json()["records"][0]["status"] == "reserved"

    async def test_resolver_still_works(self, client, record, datacite_off):
        response = await client.get(f"/{record['igsn']}")
        assert response.status_code == 200

    async def test_updating_a_published_record_does_not_queue(
        self, client, auth, database, record, metadata, datacite_off
    ):
        await database["records"].update_one(
            {"igsn": record["igsn"]}, {"$set": {"status": "findable"}}
        )
        response = await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"metadata": {**metadata, "titles": [{"title": "Corrected"}]}},
        )
        assert response.status_code == 200
        # The local edit lands; only the push to DataCite is withheld.
        assert response.json()["metadata"]["titles"] == [{"title": "Corrected"}]
        assert await database["publication_attempts"].count_documents({}) == 0

    async def test_drain_claims_nothing(self, database, tenant, record, datacite_off):
        # Queued directly, bypassing the endpoint, to prove the loop holds the
        # line on a backlog that predates the switch being turned off.
        await publisher.enqueue(record["igsn"], tenant, "findable", db=database)
        assert await publisher.drain_once(database) == 0
        attempt = await database["publication_attempts"].find_one({})
        # Still queued, not claimed-and-abandoned: a "running" attempt would sit
        # untouched until requeue_stale noticed it ten minutes later.
        assert attempt["status"] == "queued"

    async def test_run_forever_returns_immediately(self, database, datacite_off):
        # Would otherwise loop until cancelled.
        await publisher.run_forever()

    @respx.mock
    async def test_submit_refuses_even_when_called_directly(
        self, tenant, record, datacite_off
    ):
        route = respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        with pytest.raises(datacite.DataCiteNotConfigured, match="disabled"):
            await datacite.submit(record, tenant, "findable")
        assert not route.called

    async def test_fetch_is_not_gated(self, tenant, record, datacite_off):
        # sync-status reads DataCite to learn what is already published. Reads
        # are free and are the prerequisite for turning publication on safely,
        # so the switch must not block them.
        with respx.mock:
            route = respx.get(
                f"https://api.test.datacite.org/dois/{record['doi']}"
            ).mock(
                return_value=httpx.Response(
                    200, json={"data": {"attributes": {"state": "findable"}}}
                )
            )
            result = await datacite.fetch(record["doi"], tenant)
        assert route.called
        assert result["data"]["attributes"]["state"] == "findable"
