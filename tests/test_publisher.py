"""The durable publication queue."""

import datetime

import httpx
import pytest
import respx

from app.services import allocator, publisher


@pytest.fixture
async def reserved(database, tenant, metadata):
    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    return docs[0]


def _dois_route(status_code=201, **kwargs):
    return respx.post("https://api.test.datacite.org/dois").mock(
        return_value=httpx.Response(status_code, json=kwargs.get("json", {}))
    )


class TestEnqueue:
    async def test_single_record(self, database, tenant, reserved):
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        assert len(ids) == 1
        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "queued"
        assert attempt["target"] == "findable"

    async def test_recurse_includes_children_parent_first(self, database, tenant, reserved):
        await allocator.allocate_children(reserved["igsn"], tenant, indices=["001", "002"])
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable", recurse=True)
        assert len(ids) == 3
        attempts = [await publisher.get_attempt(i) for i in ids]
        # The parent must go first: children carry an IsPartOf pointing at it.
        assert attempts[0]["igsn"] == reserved["igsn"]

    async def test_recurse_skips_revoked_children(self, database, tenant, reserved):
        await allocator.allocate_children(reserved["igsn"], tenant, indices=["001", "002"])
        await database["records"].update_one(
            {"igsn": f"{reserved['igsn']}-002"}, {"$set": {"status": "revoked"}}
        )
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable", recurse=True)
        assert len(ids) == 2


class TestProcessAttempt:
    @respx.mock
    async def test_success_marks_record_findable(self, database, tenant, reserved):
        _dois_route()
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        assert await publisher.drain_once(database) == 1

        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["status"] == "findable"
        assert record["published_at"] is not None
        assert record["datacite"]["last_sync"] is not None
        assert record["datacite"]["last_error"] is None
        assert (await publisher.get_attempt(ids[0]))["status"] == "done"

    @respx.mock
    async def test_registered_target(self, database, tenant, reserved):
        _dois_route()
        await publisher.enqueue(reserved["igsn"], tenant, "registered")
        await publisher.drain_once(database)
        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["status"] == "registered"

    @respx.mock
    async def test_terminal_error_fails_without_retry(self, database, tenant, reserved):
        route = _dois_route(422, json={"errors": [{"title": "bad publisher"}]})
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)

        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "failed"
        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["status"] == "failed"
        assert "bad publisher" in record["datacite"]["last_error"]

        # A failed attempt must not be picked up again -- no retry storm.
        assert await publisher.drain_once(database) == 0
        assert route.call_count == 1

    @respx.mock
    async def test_transient_error_is_requeued_with_backoff(self, database, tenant, reserved):
        _dois_route(503)
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)

        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "queued"
        assert attempt["attempts"] == 1
        assert attempt["next_attempt_at"] > datetime.datetime.now(datetime.UTC)
        # Not due yet, so a second drain does nothing.
        assert await publisher.drain_once(database) == 0
        # The record itself stays reserved rather than being marked failed.
        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["status"] == "reserved"

    @respx.mock
    async def test_gives_up_after_max_attempts(self, database, tenant, reserved, monkeypatch):
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "publisher_max_attempts", 2)
        monkeypatch.setattr(settings, "publisher_backoff_base_seconds", 0)
        _dois_route(503)

        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        for _ in range(3):
            await publisher.drain_once(database)

        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "failed"
        assert attempt["attempts"] == 2

    async def test_record_without_metadata_is_refused(self, database, tenant):
        """Don't make DataCite tell us the record is incomplete."""
        docs = await allocator.allocate("JHAMAA", 1, tenant)
        ids = await publisher.enqueue(docs[0]["igsn"], tenant, "findable")
        await publisher.drain_once(database)

        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "failed"
        assert "no metadata" in attempt["last_error"]

    async def test_revoked_record_is_refused(self, database, tenant, reserved):
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await database["records"].update_one(
            {"igsn": reserved["igsn"]}, {"$set": {"status": "revoked"}}
        )
        await publisher.drain_once(database)
        assert (await publisher.get_attempt(ids[0]))["status"] == "failed"

    @respx.mock
    async def test_drain_processes_batch(self, database, tenant, reserved):
        _dois_route()
        await allocator.allocate_children(reserved["igsn"], tenant, indices=["001", "002"])
        await publisher.enqueue(reserved["igsn"], tenant, "findable", recurse=True)
        assert await publisher.drain_once(database) == 3
        assert await database["records"].count_documents({"status": "findable"}) == 3


class TestStaleClaims:
    async def test_orphaned_running_attempt_is_requeued(self, database, tenant, reserved):
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            seconds=publisher.STALE_CLAIM_SECONDS + 60
        )
        await database["publication_attempts"].update_one(
            {"igsn": reserved["igsn"]},
            {"$set": {"status": "running", "claimed_at": stale}},
        )
        assert await publisher.requeue_stale(database) == 1
        assert (await publisher.get_attempt(ids[0]))["status"] == "queued"

    async def test_recent_claim_is_left_alone(self, database, tenant, reserved):
        await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await database["publication_attempts"].update_one(
            {"igsn": reserved["igsn"]},
            {
                "$set": {
                    "status": "running",
                    "claimed_at": datetime.datetime.now(datetime.UTC),
                }
            },
        )
        assert await publisher.requeue_stale(database) == 0


class TestPersistedDiagnostics:
    """A failed publish has to leave behind enough to explain itself."""

    @respx.mock
    async def test_failure_records_what_was_sent(self, database, tenant, reserved):
        _dois_route(422, json={"errors": [{"source": "url", "title": "URL is not valid"}]})
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)

        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "failed"
        assert "URL is not valid" in attempt["last_error"]
        # The request summary is the part that makes it actionable.
        assert attempt["last_request"]["url"] == f"https://igsn.example.org/{reserved['igsn']}"
        assert attempt["last_request"]["method"] == "POST"
        assert attempt["last_request"]["doi"] == reserved["doi"]

        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["datacite"]["last_request"]["url"]

    @respx.mock
    async def test_attempts_are_counted_even_when_they_fail(
        self, database, tenant, reserved
    ):
        """A record stuck at attempts=0 after repeated failures tells you nothing."""
        _dois_route(422, json={"errors": [{"source": "publisher"}]})
        await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)
        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["datacite"]["attempts"] == 1

    @respx.mock
    async def test_transient_failure_records_the_request_too(
        self, database, tenant, reserved
    ):
        _dois_route(503)
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)
        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "queued"
        assert attempt["last_request"]["method"] == "POST"

    @respx.mock
    async def test_success_records_the_request(self, database, tenant, reserved):
        _dois_route()
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)
        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "done"
        assert attempt["last_request"]["method"] == "POST"
        record = await database["records"].find_one({"igsn": reserved["igsn"]})
        assert record["datacite"]["last_error"] is None
        assert record["datacite"]["last_request"]["url"]

    async def test_invalid_landing_page_fails_without_contacting_datacite(
        self, database, tenant, reserved
    ):
        """respx is not mocking anything here: any HTTP call would error."""
        await database["records"].update_one(
            {"igsn": reserved["igsn"]},
            {"$set": {"landing_page": "#igsn/" + reserved["igsn"]}},
        )
        ids = await publisher.enqueue(reserved["igsn"], tenant, "findable")
        await publisher.drain_once(database)

        attempt = await publisher.get_attempt(ids[0])
        assert attempt["status"] == "failed"
        assert "#igsn/" in attempt["last_error"]
        assert "not an absolute http" in attempt["last_error"]

    @respx.mock
    async def test_attempts_for_returns_newest_first(self, database, tenant, reserved):
        _dois_route(503)
        await publisher.enqueue(reserved["igsn"], tenant, "registered")
        await publisher.enqueue(reserved["igsn"], tenant, "findable")
        attempts = await publisher.attempts_for(reserved["igsn"])
        assert len(attempts) == 2
        assert attempts[0]["created"] >= attempts[1]["created"]
        assert all("id" in a and "_id" not in a for a in attempts)

    async def test_attempts_for_is_empty_for_an_unpublished_record(
        self, database, tenant, reserved
    ):
        assert await publisher.attempts_for(reserved["igsn"]) == []
