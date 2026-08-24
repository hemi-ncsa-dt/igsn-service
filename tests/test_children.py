"""Child/batch registration.

Batches are the common case in practice: one form submission mints a parent and
tens of children. Girder computed child indices from its *local* Mongo before
this, which was only ever correct while a parent's children could not be created
from more than one instance.
"""

import asyncio

import pytest

from app.services import allocator


@pytest.fixture
async def parent(database, tenant, metadata):
    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    return docs[0]


async def test_explicit_indices(database, tenant, parent):
    docs = await allocator.allocate_children(
        parent["igsn"], tenant, indices=["001", "002", "003"]
    )
    assert [d["igsn"] for d in docs] == [
        "JHAMAA00001-001",
        "JHAMAA00001-002",
        "JHAMAA00001-003",
    ]
    assert all(d["parent_igsn"] == "JHAMAA00001" for d in docs)
    assert all(d["prefix"] == "JHAMAA" for d in docs)


async def test_lab_specific_index_labels(database, tenant, parent):
    """The Weihs strategy produces labels like S1R0C0, not numbers."""
    indices = ["S1R0C0", "S1R0C1", "S1R1C0"]
    docs = await allocator.allocate_children(parent["igsn"], tenant, indices=indices)
    assert [d["igsn"] for d in docs] == [f"JHAMAA00001-{i}" for i in indices]


async def test_child_metadata_is_derived_from_parent(database, tenant, parent):
    docs = await allocator.allocate_children(parent["igsn"], tenant, indices=["001"])
    child = docs[0]
    assert child["metadata"]["titles"] == [{"title": "Test Sample - 001"}]
    assert {
        "relationType": "IsPartOf",
        "relatedIdentifier": "JHAMAA00001",
        "relatedIdentifierType": "IGSN",
    } in child["metadata"]["relatedIdentifiers"]
    assert child["doi"] == "10.83961/jhamaa00001-001"


async def test_parent_gains_haspart_edges(database, tenant, parent):
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001", "002"])
    refreshed = await database["records"].find_one({"igsn": parent["igsn"]})
    has_parts = [
        r
        for r in refreshed["metadata"]["relatedIdentifiers"]
        if r["relationType"] == "HasPart"
    ]
    assert {r["relatedIdentifier"] for r in has_parts} == {
        "JHAMAA00001-001",
        "JHAMAA00001-002",
    }


async def test_children_do_not_inherit_haspart(database, tenant, parent):
    """A child must not claim its siblings as parts of itself."""
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001"])
    docs = await allocator.allocate_children(parent["igsn"], tenant, indices=["002"])
    relations = docs[0]["metadata"]["relatedIdentifiers"]
    assert not [r for r in relations if r["relationType"] == "HasPart"]


async def test_local_identifiers(database, tenant, parent):
    docs = await allocator.allocate_children(
        parent["igsn"],
        tenant,
        indices=["001", "002"],
        local_identifiers={"001": "BUILD7_cube_001", "002": "BUILD7_cube_002"},
    )
    assert docs[0]["metadata"]["alternateIdentifiers"] == [
        {"alternateIdentifier": "BUILD7_cube_001", "alternateIdentifierType": "Local"}
    ]


async def test_count_picks_next_free_numeric_indices(database, tenant, parent):
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001", "002"])
    docs = await allocator.allocate_children(parent["igsn"], tenant, count=2)
    assert [d["igsn"] for d in docs] == ["JHAMAA00001-003", "JHAMAA00001-004"]


async def test_count_ignores_non_numeric_indices(database, tenant, parent):
    """Existing S1R0C0-style children must not confuse numeric allocation."""
    await allocator.allocate_children(parent["igsn"], tenant, indices=["S1R0C0"])
    docs = await allocator.allocate_children(parent["igsn"], tenant, count=1)
    assert docs[0]["igsn"] == "JHAMAA00001-001"


async def test_duplicate_batch_fails_whole(database, tenant, parent):
    """A colliding batch must not half-register.

    Girder aborts its local batch when this raises, so anything the registry
    kept would be a child nothing else knows about -- and the parent's HasPart
    list would disagree about which children exist. ``ordered=True`` inserts
    everything before the collision, so those have to be rolled back.
    """
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001"])
    with pytest.raises(allocator.AllocationError, match="already exist"):
        await allocator.allocate_children(
            parent["igsn"], tenant, indices=["002", "001", "003"]
        )

    children = {
        doc["igsn"] async for doc in database["records"].find({"parent_igsn": parent["igsn"]})
    }
    # Only the child from the first, successful call survives. "002" was
    # inserted before the collision and must have been rolled back.
    assert children == {"JHAMAA00001-001"}


async def test_failed_batch_leaves_parent_haspart_consistent(database, tenant, parent):
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001"])
    with pytest.raises(allocator.AllocationError):
        await allocator.allocate_children(
            parent["igsn"], tenant, indices=["002", "001"]
        )
    refreshed = await database["records"].find_one({"igsn": parent["igsn"]})
    has_parts = {
        r["relatedIdentifier"]
        for r in refreshed["metadata"]["relatedIdentifiers"]
        if r["relationType"] == "HasPart"
    }
    assert has_parts == {"JHAMAA00001-001"}


async def test_rolled_back_index_can_be_retried(database, tenant, parent):
    """The point of rolling back: a corrected retry can use the same indices."""
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001"])
    with pytest.raises(allocator.AllocationError):
        await allocator.allocate_children(
            parent["igsn"], tenant, indices=["002", "001", "003"]
        )
    docs = await allocator.allocate_children(
        parent["igsn"], tenant, indices=["002", "003"]
    )
    assert [d["igsn"] for d in docs] == ["JHAMAA00001-002", "JHAMAA00001-003"]


async def test_concurrent_count_allocation_is_not_lost(database, tenant, parent):
    """Concurrent numeric child allocation must neither duplicate nor drop.

    Picking the next index is a read-then-write, so racing callers can choose
    the same one. The unique index makes that a failure rather than a duplicate,
    and the count path re-picks, so every caller still gets its child.
    """
    results = await asyncio.gather(
        *(allocator.allocate_children(parent["igsn"], tenant, count=1) for _ in range(8))
    )
    igsns = [d["igsn"] for docs in results for d in docs]
    assert len(set(igsns)) == 8, "duplicate or lost child IGSN"
    assert sorted(igsns) == [f"JHAMAA00001-{n:03d}" for n in range(1, 9)]


async def test_unknown_parent_is_rejected(database, tenant):
    with pytest.raises(allocator.AllocationError, match="not registered"):
        await allocator.allocate_children("JHAMAA99999", tenant, indices=["001"])


async def test_other_tenants_parent_is_rejected(database, tenant, other_tenant, parent):
    with pytest.raises(allocator.AllocationError, match="another tenant"):
        await allocator.allocate_children(parent["igsn"], other_tenant, indices=["001"])


async def test_grandchildren(database, tenant, parent):
    """IGSNs nest: PREFIX-001-001 is a real shape Project.igsn_query matches."""
    await allocator.allocate_children(parent["igsn"], tenant, indices=["001"])
    docs = await allocator.allocate_children("JHAMAA00001-001", tenant, indices=["001"])
    assert docs[0]["igsn"] == "JHAMAA00001-001-001"
    assert docs[0]["parent_igsn"] == "JHAMAA00001-001"
