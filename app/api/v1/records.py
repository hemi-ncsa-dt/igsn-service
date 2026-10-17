"""Record read/update/publish endpoints. Every query is tenant-scoped."""

import datetime
import logging

from fastapi import APIRouter, HTTPException, Query, status

from ...auth import TenantDep
from ...config import get_settings
from ...db import RECORDS, get_db
from ...schemas import (
    AttemptOut,
    PublishRequest,
    PublishResponse,
    RecordOut,
    RecordUpdate,
    WhoAmI,
)
from ...services import allocator, datacite, publisher
from .common import serialize_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["records"])


async def _load_for_tenant(igsn: str, tenant: dict) -> dict:
    doc = await get_db()[RECORDS].find_one({"igsn": igsn.upper()})
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No record for {igsn}")
    if doc["tenant"] != tenant["slug"]:
        # Deliberately 404, not 403: a tenant should not be able to probe which
        # identifiers another tenant holds.
        raise HTTPException(status_code=404, detail=f"No record for {igsn}")
    return doc


@router.get("/whoami", response_model=WhoAmI)
async def whoami(tenant: dict = TenantDep):
    return WhoAmI(
        slug=tenant["slug"],
        name=tenant.get("name", tenant["slug"]),
        allowed_institutions=tenant.get("allowed_institutions", []),
        doi_prefix=tenant["datacite"]["doi_prefix"],
    )


@router.get("/records", response_model=list[RecordOut])
async def list_records(
    tenant: dict = TenantDep,
    prefix: str | None = Query(default=None),
    record_status: str | None = Query(default=None, alias="status"),
    parent: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {"tenant": tenant["slug"]}
    if prefix:
        query["prefix"] = prefix.upper()
    if record_status:
        query["status"] = record_status
    if parent:
        query["parent_igsn"] = parent.upper()

    cursor = get_db()[RECORDS].find(query).sort("igsn", 1).skip(offset).limit(limit)
    return [serialize_record(doc) async for doc in cursor]


@router.get("/records/{igsn}", response_model=RecordOut)
async def get_record(igsn: str, tenant: dict = TenantDep):
    return serialize_record(await _load_for_tenant(igsn, tenant))


@router.put("/records/{igsn}", response_model=RecordOut)
async def update_record(igsn: str, body: RecordUpdate, tenant: dict = TenantDep):
    """Replace a record's metadata and/or landing page.

    Updating an already-published record queues a DataCite metadata update at its
    current state, so a correction reaches DataCite without a separate publish
    call.
    """
    record = await _load_for_tenant(igsn, tenant)
    if record["status"] == "revoked":
        raise HTTPException(status_code=409, detail=f"{record['igsn']} is revoked")

    updates: dict = {"updated": datetime.datetime.now(datetime.UTC)}
    if body.metadata is not None:
        try:
            allocator.validate_metadata(body.metadata)
        except allocator.MetadataError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        metadata = dict(body.metadata)
        metadata["doi"] = record["doi"]
        updates["metadata"] = metadata
    if body.landing_page is not None:
        # Refuse a landing page DataCite would reject, at the point where the
        # caller can still be told which value was wrong. Storing it and failing
        # later at publish time is how "#igsn/JHBBMX00001" reached DataCite.
        try:
            landing_page = datacite.validate_landing_url(body.landing_page)
        except datacite.InvalidLandingUrl as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        updates["landing_page"] = landing_page
        if "metadata" in updates:
            updates["metadata"]["url"] = landing_page
    if body.external_ref is not None:
        updates["external_ref"] = body.external_ref.model_dump()

    await get_db()[RECORDS].update_one({"igsn": record["igsn"]}, {"$set": updates})
    updated = await get_db()[RECORDS].find_one({"igsn": record["igsn"]})

    if updated["status"] in ("registered", "findable"):
        if get_settings().datacite_enabled:
            await publisher.enqueue(updated["igsn"], tenant, updated["status"])
        else:
            # The local record is updated either way; only the push is skipped.
            # Logged rather than silent, because it leaves DataCite holding
            # stale metadata for a live DOI until someone re-publishes.
            logger.warning(
                "%s updated but not queued for DataCite: IGSN_DATACITE_ENABLED "
                "is false. Re-publish it when publication is turned on.",
                updated["igsn"],
            )

    return serialize_record(updated)


@router.post("/records/{igsn}/publish", response_model=PublishResponse)
async def publish_record(igsn: str, body: PublishRequest, tenant: dict = TenantDep):
    """Queue publication to DataCite.

    Returns as soon as the attempts are durably queued; the background publisher
    does the work with retries. Poll ``GET /records/{igsn}`` for the outcome.
    """
    if not get_settings().datacite_enabled:
        # Refuse rather than queue. A queued attempt with the drain loop off
        # looks accepted and does nothing, then fires as a backlog the moment
        # publication is enabled -- which is real money, unreviewed.
        raise HTTPException(
            status_code=503,
            detail=(
                "DataCite publication is disabled on this deployment "
                "(IGSN_DATACITE_ENABLED is false). The identifier is allocated "
                "and resolvable; nothing was queued."
            ),
        )

    record = await _load_for_tenant(igsn, tenant)
    if record["status"] == "revoked":
        raise HTTPException(status_code=409, detail=f"{record['igsn']} is revoked")
    if not record.get("metadata"):
        raise HTTPException(
            status_code=422,
            detail=f"{record['igsn']} has no metadata; PUT it before publishing",
        )

    attempts = await publisher.enqueue(
        record["igsn"], tenant, body.target, recurse=body.recurse
    )
    return PublishResponse(attempts=attempts, queued=len(attempts))


@router.get("/records/{igsn}/attempts", response_model=list[AttemptOut])
async def list_attempts(igsn: str, tenant: dict = TenantDep, limit: int = 20):
    """Publication attempts for a record, newest first.

    The debugging entry point when a publish fails: each attempt carries the
    error DataCite returned *and* a summary of the request that caused it, so a
    bare "URL is not valid" can be traced to the actual URL sent.
    """
    record = await _load_for_tenant(igsn, tenant)
    return [AttemptOut(**a) for a in await publisher.attempts_for(record["igsn"], limit=limit)]


@router.post(
    "/records/{igsn}/revoke",
    response_model=RecordOut,
    status_code=status.HTTP_200_OK,
)
async def revoke_record(igsn: str, tenant: dict = TenantDep):
    """Tombstone a reserved identifier.

    This never frees the sequence number. IGSNs, like DOIs, must not be reused --
    something may already reference one, and DataCite has no notion of taking an
    identifier back.
    """
    record = await _load_for_tenant(igsn, tenant)
    if record["status"] in ("registered", "findable"):
        raise HTTPException(
            status_code=409,
            detail=f"{record['igsn']} is published and cannot be revoked",
        )
    await get_db()[RECORDS].update_one(
        {"igsn": record["igsn"]},
        {"$set": {"status": "revoked", "updated": datetime.datetime.now(datetime.UTC)}},
    )
    return serialize_record(await get_db()[RECORDS].find_one({"igsn": record["igsn"]}))
