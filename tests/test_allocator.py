"""Allocation correctness -- the reason this service exists."""

import asyncio

import pytest

from app.services import allocator
from app.services.vocab import PrefixError, validate_prefix


async def test_allocate_sequences_from_one(database, tenant, metadata):
    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    assert [d["igsn"] for d in docs] == ["JHAMAA00001"]
    assert docs[0]["doi"] == "10.83961/jhamaa00001"
    assert docs[0]["status"] == "reserved"
    assert docs[0]["prefix"] == "JHAMAA"
    assert docs[0]["parent_igsn"] is None


async def test_allocate_block_is_contiguous(database, tenant, metadata):
    docs = await allocator.allocate("JHAMAA", 5, tenant, metadata=metadata)
    assert [d["igsn"] for d in docs] == [f"JHAMAA{n:05d}" for n in range(1, 6)]
    counter = await database["counters"].find_one({"_id": "JHAMAA"})
    assert counter["seq"] == 5


async def test_concurrent_allocation_never_collides(database, tenant, metadata):
    """The whole point: two instances minting the same prefix at once.

    Previously each Girder had its own counter, so this produced duplicates.
    """
    concurrency = 40
    results = await asyncio.gather(
        *(allocator.allocate("JHAMAA", 1, tenant, metadata=metadata) for _ in range(concurrency))
    )
    igsns = [docs[0]["igsn"] for docs in results]
    assert len(set(igsns)) == concurrency, "duplicate IGSN handed out"
    assert sorted(igsns) == [f"JHAMAA{n:05d}" for n in range(1, concurrency + 1)]

    stored = await database["records"].count_documents({"prefix": "JHAMAA"})
    assert stored == concurrency


async def test_concurrent_block_allocation_never_overlaps(database, tenant, metadata):
    results = await asyncio.gather(
        *(allocator.allocate("JHAMAA", 3, tenant, metadata=metadata) for _ in range(10))
    )
    igsns = [d["igsn"] for docs in results for d in docs]
    assert len(set(igsns)) == 30


async def test_stale_counter_self_heals(database, tenant, metadata):
    """A counter behind reality must not re-issue an existing identifier.

    This is the migration safety net: if the backfilled high-water mark is too
    low, the unique index rejects the insert and the allocator increments again.
    """
    await allocator.allocate("JHAMAA", 3, tenant, metadata=metadata)
    # Simulate a bad backfill by winding the counter back behind the records.
    await database["counters"].update_one({"_id": "JHAMAA"}, {"$set": {"seq": 0}})

    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    assert docs[0]["igsn"] == "JHAMAA00004", "should skip past the existing records"
    assert await database["records"].count_documents({"prefix": "JHAMAA"}) == 4


async def test_allocation_gives_up_rather_than_duplicating(database, tenant, metadata, monkeypatch):
    await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    await database["counters"].update_one({"_id": "JHAMAA"}, {"$set": {"seq": 0}})
    monkeypatch.setattr(allocator, "MAX_ALLOCATION_RETRIES", 1)

    with pytest.raises(allocator.AllocationError):
        await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    # And crucially, no duplicate was written.
    assert await database["records"].count_documents({"igsn": "JHAMAA00001"}) == 1


async def test_prefix_must_be_permitted_for_tenant(database, tenant, metadata):
    with pytest.raises(PrefixError, match="not permitted"):
        await allocator.allocate("TMAMAA", 1, tenant, metadata=metadata)


async def test_metadata_is_validated(database, tenant):
    with pytest.raises(allocator.MetadataError):
        await allocator.allocate("JHAMAA", 1, tenant, metadata={"titles": "not a list"})


async def test_reserve_without_metadata_is_allowed(database, tenant):
    docs = await allocator.allocate("JHAMAA", 1, tenant)
    assert docs[0]["metadata"] == {}
    assert docs[0]["status"] == "reserved"


async def test_landing_template_is_applied(database, metadata):
    tenant = {
        "slug": "t",
        "allowed_institutions": [],
        "datacite": {"doi_prefix": "10.82581"},
        "landing_template": "https://data.htmdec.org/#igsn/{igsn}",
    }
    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    assert docs[0]["landing_page"] == "https://data.htmdec.org/#igsn/JHAMAA00001"
    assert docs[0]["metadata"]["url"] == "https://data.htmdec.org/#igsn/JHAMAA00001"


class TestPrefixValidation:
    """Must accept exactly what girder-jsonforms' PrefixCounter.validate accepted."""

    @pytest.fixture
    def vocabularies(self):
        from app.services.vocab import load_seed

        return load_seed()

    def test_valid(self, vocabularies):
        assert validate_prefix("JHAMAA", vocabularies) == "JHAMAA"

    def test_lowercase_is_canonicalized(self, vocabularies):
        assert validate_prefix("jhamaa", vocabularies) == "JHAMAA"

    def test_material_without_subcategories_requires_x(self, vocabularies):
        """BO (biological) declares no subcategories, so only X is valid.

        Girder implements this by defaulting to {"X": "empty"}; diverging would
        reject prefixes already in use.
        """
        assert validate_prefix("JHXBOX", vocabularies) == "JHXBOX"
        with pytest.raises(PrefixError, match="Invalid submaterial A"):
            validate_prefix("JHXBOA", vocabularies)

    @pytest.mark.parametrize(
        "prefix,message",
        [
            ("foo", "6 characters"),
            ("ZZCDEF", "Invalid institution ZZ"),
            ("JHZDEF", "Invalid subinstitution Z"),
            ("JHXZZZ", "Invalid material ZZ"),
            ("JHXMAZ", "Invalid submaterial Z"),
        ],
    )
    def test_rejections_match_girder(self, vocabularies, prefix, message):
        with pytest.raises(PrefixError, match=message):
            validate_prefix(prefix, vocabularies)
