"""Backfill logic.

``seed_counters`` is the one step in the migration that can cause a duplicate
IGSN, so it gets the most attention here.
"""

import httpx
import pytest
import respx

from app import cli
from app.cli import parse_igsn


class TestParseIgsn:
    @pytest.mark.parametrize(
        "igsn,expected",
        [
            ("JHAMAA00001", ("JHAMAA", 1, None)),
            ("JHAMAA00042", ("JHAMAA", 42, None)),
            ("JHAMAA99999", ("JHAMAA", 99999, None)),
            ("JHAMAA00001-001", ("JHAMAA", 1, "JHAMAA00001")),
            ("JHAMAA00001-001-002", ("JHAMAA", 1, "JHAMAA00001-001")),
            ("JHAMAA00001-S1R0C0", ("JHAMAA", 1, "JHAMAA00001")),
        ],
    )
    def test_valid(self, igsn, expected):
        assert parse_igsn(igsn) == expected

    def test_lowercase_is_accepted(self):
        assert parse_igsn("jhamaa00001") == ("JHAMAA", 1, None)

    @pytest.mark.parametrize(
        "igsn", ["", "JHAMAA", "JHAMAA0001", "JHAM00001", "JH-AMAA00001", "not an igsn"]
    )
    def test_malformed(self, igsn):
        assert parse_igsn(igsn) is None

    def test_childs_sequence_is_its_parents(self):
        """A child's digits are its parent's sequence, not a new one.

        Reading 003 out of JHAMAA00007-003 as a top-level sequence would be
        harmless here but catastrophic in _highest_top_level.
        """
        assert parse_igsn("JHAMAA00007-003") == ("JHAMAA", 7, "JHAMAA00007")


class TestSeedCounters:
    async def test_raises_counters_to_observed_high_water_mark(self, database):
        await database["records"].insert_many(
            [
                {"igsn": "JHAMAA00001", "doi": "10.1/a", "prefix": "JHAMAA"},
                {"igsn": "JHAMAA00007", "doi": "10.1/b", "prefix": "JHAMAA"},
                {"igsn": "NWXCRA00003", "doi": "10.1/c", "prefix": "NWXCRA"},
            ]
        )
        rows = await cli.seed_counters(db=database)
        assert {r["prefix"]: r["to"] for r in rows} == {"JHAMAA": 7, "NWXCRA": 3}
        assert (await database["counters"].find_one({"_id": "JHAMAA"}))["seq"] == 7
        assert (await database["counters"].find_one({"_id": "NWXCRA"}))["seq"] == 3

    async def test_children_do_not_inflate_the_counter(self, database):
        """A child suffix must never be read as a top-level sequence."""
        await database["records"].insert_many(
            [
                {"igsn": "JHAMAA00002", "doi": "10.1/a", "prefix": "JHAMAA"},
                {"igsn": "JHAMAA00002-001", "doi": "10.1/b", "prefix": "JHAMAA"},
                {"igsn": "JHAMAA00002-999", "doi": "10.1/c", "prefix": "JHAMAA"},
                {"igsn": "JHAMAA00002-001-005", "doi": "10.1/d", "prefix": "JHAMAA"},
            ]
        )
        await cli.seed_counters(db=database)
        assert (await database["counters"].find_one({"_id": "JHAMAA"}))["seq"] == 2

    async def test_never_lowers_a_counter(self, database):
        """Lowering a counter is how you re-issue an existing identifier."""
        await database["counters"].insert_one({"_id": "JHAMAA", "seq": 500})
        await database["records"].insert_one(
            {"igsn": "JHAMAA00003", "doi": "10.1/a", "prefix": "JHAMAA"}
        )
        rows = await cli.seed_counters(db=database)
        assert rows[0]["raised"] is False
        assert (await database["counters"].find_one({"_id": "JHAMAA"}))["seq"] == 500

    async def test_dry_run_writes_nothing(self, database):
        await database["records"].insert_one(
            {"igsn": "JHAMAA00009", "doi": "10.1/a", "prefix": "JHAMAA"}
        )
        rows = await cli.seed_counters(db=database, dry_run=True)
        assert rows == [{"prefix": "JHAMAA", "from": 0, "to": 9, "raised": True}]
        assert await database["counters"].count_documents({}) == 0

    async def test_explicit_marks_let_operator_take_cross_instance_max(self, database):
        """The marks from every instance must be merged before any are written."""
        await cli.seed_counters(observed={"JHAMAA": 120, "NWXCRA": 44}, db=database)
        assert (await database["counters"].find_one({"_id": "JHAMAA"}))["seq"] == 120
        assert (await database["counters"].find_one({"_id": "NWXCRA"}))["seq"] == 44

    async def test_malformed_igsns_are_ignored(self, database):
        await database["records"].insert_many(
            [
                {"igsn": "JHAMAA00004", "doi": "10.1/a", "prefix": "JHAMAA"},
                {"igsn": "GARBAGE", "doi": "10.1/b"},
            ]
        )
        rows = await cli.seed_counters(db=database)
        assert rows == [{"prefix": "JHAMAA", "from": 0, "to": 4, "raised": True}]

    async def test_seeded_counter_prevents_duplicate_allocation(self, database, tenant):
        """The end-to-end point of the whole exercise.

        After a backfill, the next allocation must continue past the imported
        records rather than colliding with them.
        """
        from app.services import allocator

        await database["records"].insert_one(
            {
                "igsn": "JHAMAA00042",
                "doi": "10.83961/jhamaa00042",
                "prefix": "JHAMAA",
                "tenant": tenant["slug"],
                "status": "findable",
            }
        )
        await cli.seed_counters(db=database)
        docs = await allocator.allocate("JHAMAA", 1, tenant, db=database)
        assert docs[0]["igsn"] == "JHAMAA00043"


class TestVerify:
    async def test_reports_ok(self, database):
        await database["records"].insert_one(
            {"igsn": "JHAMAA00003", "doi": "10.1/a", "prefix": "JHAMAA",
             "status": "reserved"}
        )
        await database["counters"].insert_one({"_id": "JHAMAA", "seq": 3})
        result = await cli.verify(db=database)
        assert result["behind"] == []
        assert result["prefixes"] == [
            {
                "prefix": "JHAMAA",
                "seq": 3,
                "highest_record": 3,
                "records": 1,
                "behind": False,
            }
        ]

    async def test_flags_a_counter_behind_reality(self, database):
        """This is the pre-flight check before switching an instance over."""
        await database["records"].insert_one(
            {"igsn": "JHAMAA00009", "doi": "10.1/a", "prefix": "JHAMAA",
             "status": "reserved"}
        )
        await database["counters"].insert_one({"_id": "JHAMAA", "seq": 2})
        result = await cli.verify(db=database)
        assert result["behind"] == ["JHAMAA"]

    async def test_reports_status_breakdown(self, database):
        await database["records"].insert_many(
            [
                {"igsn": "JHAMAA00001", "doi": "10.1/a", "prefix": "JHAMAA",
                 "status": "findable"},
                {"igsn": "JHAMAA00002", "doi": "10.1/b", "prefix": "JHAMAA",
                 "status": "reserved"},
            ]
        )
        await database["counters"].insert_one({"_id": "JHAMAA", "seq": 2})
        result = await cli.verify(db=database)
        assert result["by_status"] == {"findable": 1, "reserved": 1}


class TestCreateTenant:
    async def test_creates_and_returns_token_once(self, database):
        token = await cli.create_tenant(
            slug="htmdec-girder",
            name="HTMDEC Girder",
            doi_prefix="10.82581",
            repository_id="jhu.hemi-igsn",
            password_env="DATACITE_PW_HTMDEC",
            institutions=["JH"],
            landing_template="https://data.htmdec.org/#igsn/{igsn}",
            db=database,
        )
        assert token
        stored = await database["tenants"].find_one({"slug": "htmdec-girder"})
        assert stored["allowed_institutions"] == ["JH"]
        assert stored["landing_template"] == "https://data.htmdec.org/#igsn/{igsn}"
        assert stored["datacite"]["doi_prefix"] == "10.82581"
        # Only the hash is persisted.
        assert "token" not in stored
        assert stored["token_sha256"] != token

    async def test_duplicate_slug_is_rejected(self, database, tenant):
        with pytest.raises(ValueError, match="already exists"):
            await cli.create_tenant(
                slug=tenant["slug"],
                name="dup",
                doi_prefix="10.1",
                repository_id="x",
                password_env="X",
                db=database,
            )


class FakeGirderClient:
    """Stands in for girder_client.GirderClient over the /deposition endpoints."""

    urlBase = "https://data.htmdec.org/api/v1"

    def __init__(self, depositions):
        self._depositions = depositions

    def get(self, path, parameters=None):
        if path == "deposition":
            offset = (parameters or {}).get("offset", 0)
            limit = (parameters or {}).get("limit", 500)
            # Mimic Girder: the list view omits ACLed relatedIdentifiers.
            page = self._depositions[offset : offset + limit]
            return [
                {k: v for k, v in d.items() if k != "metadata"}
                | {"metadata": {k: v for k, v in d["metadata"].items()
                                if k != "relatedIdentifiers"}}
                for d in page
            ]
        if path.startswith("deposition/"):
            wanted = path.split("/", 1)[1]
            return next(d for d in self._depositions if str(d["_id"]) == wanted)
        raise AssertionError(f"unexpected path {path}")


def _deposition(igsn, _id, **metadata):
    return {
        "_id": _id,
        "igsn": igsn,
        "state": "draft",  # Girder never advances this
        "created": "2025-01-01T00:00:00+00:00",
        "metadata": {
            "titles": [{"title": f"Sample {igsn}"}],
            "relatedIdentifiers": [
                {
                    "relationType": "IsPartOf",
                    "relatedIdentifier": "JHAMAA00001",
                    "relatedIdentifierType": "IGSN",
                }
            ],
            **metadata,
        },
    }


class TestImportGirder:
    @pytest.fixture
    async def girder(self):
        return FakeGirderClient(
            [
                _deposition("JHAMAA00001", "aaa"),
                _deposition("JHAMAA00001-001", "bbb"),
                _deposition("JHAMAA00005", "ccc"),
                _deposition("NOTANIGSN", "ddd"),
            ]
        )

    async def test_imports_records(self, database, tenant, girder):
        result = await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        assert result["imported"] == 3
        assert result["malformed"] == 1
        assert await database["records"].count_documents({}) == 3

    async def test_reports_high_water_marks_excluding_children(
        self, database, tenant, girder
    ):
        result = await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        assert result["observed"] == {"JHAMAA": 5}

    async def test_derives_parent_and_doi(self, database, tenant, girder):
        await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        child = await database["records"].find_one({"igsn": "JHAMAA00001-001"})
        assert child["parent_igsn"] == "JHAMAA00001"
        assert child["doi"] == "10.83961/jhamaa00001-001"
        assert child["prefix"] == "JHAMAA"

    async def test_fetches_full_record_for_related_identifiers(
        self, database, tenant, girder
    ):
        """The list endpoint hides them; a metadata copy missing them is wrong."""
        await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        record = await database["records"].find_one({"igsn": "JHAMAA00001"})
        assert record["metadata"]["relatedIdentifiers"]

    async def test_everything_arrives_reserved(self, database, tenant, girder):
        """Girder's state is meaningless; sync_status establishes the truth."""
        await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        statuses = {
            doc["status"] async for doc in database["records"].find({}, {"status": 1})
        }
        assert statuses == {"reserved"}

    async def test_landing_template_is_applied(self, database, tenant, girder):
        await cli.import_girder(
            girder_client=girder,
            tenant_slug=tenant["slug"],
            landing_template="https://data.htmdec.org/#igsn/{igsn}",
            db=database,
        )
        record = await database["records"].find_one({"igsn": "JHAMAA00005"})
        assert record["landing_page"] == "https://data.htmdec.org/#igsn/JHAMAA00005"

    async def test_records_the_girder_backpointer(self, database, tenant, girder):
        await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        record = await database["records"].find_one({"igsn": "JHAMAA00005"})
        assert record["external_ref"] == {
            "system": "girder",
            "instance": "https://data.htmdec.org/api/v1",
            "id": "ccc",
        }

    async def test_rerun_is_idempotent(self, database, tenant, girder):
        await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        again = await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], db=database
        )
        assert again["imported"] == 0
        assert again["skipped"] == 3
        assert await database["records"].count_documents({}) == 3

    async def test_dry_run_writes_nothing(self, database, tenant, girder):
        result = await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], dry_run=True, db=database
        )
        assert result["imported"] == 3
        assert await database["records"].count_documents({}) == 0

    async def test_pagination(self, database, tenant):
        girder = FakeGirderClient(
            [_deposition(f"JHAMAA{n:05d}", f"id{n}") for n in range(1, 26)]
        )
        result = await cli.import_girder(
            girder_client=girder, tenant_slug=tenant["slug"], page_size=10, db=database
        )
        assert result["imported"] == 25
        assert result["observed"] == {"JHAMAA": 25}

    async def test_unknown_tenant_is_rejected(self, database, girder):
        with pytest.raises(ValueError, match="No tenant"):
            await cli.import_girder(
                girder_client=girder, tenant_slug="nope", db=database
            )


class TestSyncStatus:
    @pytest.fixture
    async def imported(self, database, tenant):
        await database["records"].insert_many(
            [
                {
                    "igsn": "JHAMAA00001",
                    "doi": "10.83961/jhamaa00001",
                    "prefix": "JHAMAA",
                    "tenant": tenant["slug"],
                    "status": "reserved",
                    "created": None,
                },
                {
                    "igsn": "JHAMAA00002",
                    "doi": "10.83961/jhamaa00002",
                    "prefix": "JHAMAA",
                    "tenant": tenant["slug"],
                    "status": "reserved",
                    "created": None,
                },
            ]
        )

    @respx.mock
    async def test_published_dois_are_detected(self, database, tenant, imported):
        """Girder says draft; DataCite says findable. DataCite wins."""
        respx.get("https://api.test.datacite.org/dois/10.83961/jhamaa00001").mock(
            return_value=httpx.Response(
                200, json={"data": {"attributes": {"state": "findable"}}}
            )
        )
        respx.get("https://api.test.datacite.org/dois/10.83961/jhamaa00002").mock(
            return_value=httpx.Response(404, json={})
        )

        result = await cli.sync_status(tenant_slug=tenant["slug"], db=database)
        assert result == {"published": 1, "unpublished": 1, "errors": 0}

        first = await database["records"].find_one({"igsn": "JHAMAA00001"})
        assert first["status"] == "findable"
        second = await database["records"].find_one({"igsn": "JHAMAA00002"})
        assert second["status"] == "reserved"

    @respx.mock
    async def test_registered_state_is_mapped(self, database, tenant, imported):
        respx.get(url__startswith="https://api.test.datacite.org/dois/").mock(
            return_value=httpx.Response(
                200, json={"data": {"attributes": {"state": "registered"}}}
            )
        )
        await cli.sync_status(tenant_slug=tenant["slug"], db=database)
        record = await database["records"].find_one({"igsn": "JHAMAA00001"})
        assert record["status"] == "registered"

    @respx.mock
    async def test_datacite_draft_stays_reserved(self, database, tenant, imported):
        """A DataCite draft was never really published; don't claim it was."""
        respx.get(url__startswith="https://api.test.datacite.org/dois/").mock(
            return_value=httpx.Response(
                200, json={"data": {"attributes": {"state": "draft"}}}
            )
        )
        result = await cli.sync_status(tenant_slug=tenant["slug"], db=database)
        assert result["published"] == 0
        record = await database["records"].find_one({"igsn": "JHAMAA00001"})
        assert record["status"] == "reserved"

    @respx.mock
    async def test_lookup_errors_are_counted_not_fatal(self, database, tenant, imported):
        respx.get(url__startswith="https://api.test.datacite.org/dois/").mock(
            return_value=httpx.Response(503, text="down")
        )
        result = await cli.sync_status(tenant_slug=tenant["slug"], db=database)
        assert result["errors"] == 2
        record = await database["records"].find_one({"igsn": "JHAMAA00001"})
        assert record["status"] == "reserved"

    @respx.mock
    async def test_dry_run_writes_nothing(self, database, tenant, imported):
        respx.get(url__startswith="https://api.test.datacite.org/dois/").mock(
            return_value=httpx.Response(
                200, json={"data": {"attributes": {"state": "findable"}}}
            )
        )
        await cli.sync_status(tenant_slug=tenant["slug"], dry_run=True, db=database)
        record = await database["records"].find_one({"igsn": "JHAMAA00001"})
        assert record["status"] == "reserved"
