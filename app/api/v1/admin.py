"""Admin API: tenants, vocabularies, counters.

Gated on ``IGSN_ADMIN_TOKEN``, separate from tenant service-account tokens. An
unset admin token disables these routes.
"""

import datetime

from fastapi import APIRouter, Body, HTTPException, Query

from ...auth import AdminDep, generate_token, hash_token
from ...db import COUNTERS, RECORDS, TENANTS, get_db
from ...schemas import TenantIn, TenantOut
from ...services.vocab import get_vocabularies, set_vocabularies

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[AdminDep])


def _tenant_out(doc: dict, token: str | None = None) -> TenantOut:
    return TenantOut(
        slug=doc["slug"],
        name=doc.get("name", doc["slug"]),
        allowed_institutions=doc.get("allowed_institutions", []),
        datacite=doc["datacite"],
        landing_template=doc.get("landing_template"),
        active=doc.get("active", True),
        token=token,
    )


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants():
    cursor = get_db()[TENANTS].find({}).sort("slug", 1)
    return [_tenant_out(doc) async for doc in cursor]


@router.post("/tenants", response_model=TenantOut, status_code=201)
async def create_tenant(body: TenantIn):
    """Create a service account. The token is returned once and never again."""
    db = get_db()
    if await db[TENANTS].find_one({"slug": body.slug}):
        raise HTTPException(status_code=409, detail=f"Tenant {body.slug} already exists")
    token = generate_token()
    doc = body.model_dump()
    doc["token_sha256"] = hash_token(token)
    doc["created"] = datetime.datetime.now(datetime.UTC)
    await db[TENANTS].insert_one(doc)
    return _tenant_out(doc, token=token)


@router.post("/tenants/{slug}/rotate-token", response_model=TenantOut)
async def rotate_token(slug: str):
    db = get_db()
    token = generate_token()
    doc = await db[TENANTS].find_one_and_update(
        {"slug": slug},
        {"$set": {"token_sha256": hash_token(token)}},
        return_document=True,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No tenant {slug}")
    return _tenant_out(doc, token=token)


@router.put("/tenants/{slug}", response_model=TenantOut)
async def update_tenant(slug: str, body: TenantIn):
    db = get_db()
    updates = body.model_dump()
    updates.pop("slug", None)
    doc = await db[TENANTS].find_one_and_update(
        {"slug": slug}, {"$set": updates}, return_document=True
    )
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No tenant {slug}")
    return _tenant_out(doc)


@router.get("/vocabularies")
async def read_vocabularies():
    doc = await get_vocabularies()
    doc.pop("_id", None)
    return doc


@router.put("/vocabularies")
async def write_vocabularies(
    institutions: dict = Body(...), materials: dict = Body(...)
):
    doc = await set_vocabularies(institutions, materials)
    doc.pop("_id", None)
    return doc


@router.get("/counters")
async def list_counters():
    """Counter values next to the highest IGSN actually recorded per prefix.

    A ``seq`` below ``highest_record`` means the counter is behind reality. The
    allocator recovers from that on its own via the unique index, but it points
    at a bad backfill and is worth knowing about.
    """
    db = get_db()
    counters = {doc["_id"]: int(doc["seq"]) async for doc in db[COUNTERS].find({})}
    pipeline = [{"$group": {"_id": "$prefix", "highest": {"$max": "$igsn"}}}]
    observed = {}
    async for row in db[RECORDS].aggregate(pipeline):
        highest = row["highest"] or ""
        suffix = highest[len(row["_id"] or "") :][:5]
        observed[row["_id"]] = int(suffix) if suffix.isdigit() else 0

    prefixes = sorted(set(counters) | set(observed))
    return [
        {
            "prefix": prefix,
            "seq": counters.get(prefix, 0),
            "highest_record": observed.get(prefix, 0),
            "behind": counters.get(prefix, 0) < observed.get(prefix, 0),
        }
        for prefix in prefixes
    ]


@router.put("/counters/{prefix}")
async def seed_counter(prefix: str, seq: int = Query(..., ge=0)):
    """Raise a counter to a high-water mark. Refuses to lower it.

    Lowering a counter is how you mint a duplicate identifier, so the API will
    not do it even when asked.
    """
    db = get_db()
    prefix = prefix.upper()
    existing = await db[COUNTERS].find_one({"_id": prefix})
    if existing and int(existing["seq"]) > seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Counter {prefix} is at {existing['seq']}; refusing to lower it to "
                f"{seq}, which would re-issue existing identifiers"
            ),
        )
    await db[COUNTERS].update_one({"_id": prefix}, {"$set": {"seq": seq}}, upsert=True)
    return {"prefix": prefix, "seq": seq}
