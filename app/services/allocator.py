"""Identifier allocation.

This module replaces girder-jsonforms' per-instance ``PrefixCounter``. Two
properties carry the whole design:

1. ``counters.find_one_and_update({_id: prefix}, {$inc: {seq: count}}, upsert=True)``
   is atomic, so concurrent callers -- including callers on different Girder
   instances -- never receive overlapping blocks. This works only because there
   is now exactly one writer database.

2. The unique index on ``records.igsn`` is the actual guarantee. If a counter is
   ever behind reality (an imperfect backfill, a restored dump), ``insert_many``
   fails with a duplicate key and we increment again rather than handing out an
   IGSN that already exists. That makes a stale counter self-healing instead of
   an incident.

Never "optimize" either of those away.
"""

import datetime
import logging
import re

import jsonschema
from pymongo import ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError

from ..db import AUDIT, COUNTERS, RECORDS, get_db
from .vocab import PrefixError, get_vocabularies, validate_prefix

__all__ = [
    "AllocationError",
    "MetadataError",
    "PrefixError",
    "allocate",
    "allocate_children",
    "derive_child_metadata",
    "doi_for",
    "landing_page_for",
    "next_numeric_indices",
    "validate_metadata",
]

logger = logging.getLogger(__name__)

MAX_ALLOCATION_RETRIES = 10
SEQUENCE_WIDTH = 5
NUMERIC_INDEX = re.compile(r"^\d+$")

_schema_validator: jsonschema.Draft201909Validator | None = None


class AllocationError(RuntimeError):
    """Allocation could not be completed."""


class MetadataError(ValueError):
    """Metadata failed DataCite schema validation."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _validator() -> jsonschema.Draft201909Validator:
    global _schema_validator
    if _schema_validator is None:
        import json
        from pathlib import Path

        path = Path(__file__).parent.parent / "static" / "datacite-v4.5.json"
        with open(path) as fh:
            _schema_validator = jsonschema.Draft201909Validator(json.loads(fh.read()))
    return _schema_validator


def validate_metadata(metadata: dict | None) -> dict:
    """Validate against DataCite kernel-4, the same file Girder validates against.

    An empty dict is allowed: an identifier may be reserved before its metadata
    exists. Publication validates again and refuses to push an incomplete record.
    """
    metadata = metadata or {}
    if not metadata:
        return {}
    try:
        _validator().validate(metadata)
    except jsonschema.ValidationError as exc:
        raise MetadataError(f"Metadata validation failed: {exc.message}") from exc
    return metadata


def doi_for(igsn: str, doi_prefix: str) -> str:
    """DataCite folds DOIs to lowercase; store them that way so lookups match."""
    return f"{doi_prefix}/{igsn}".lower()


def landing_page_for(igsn: str, tenant: dict, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    template = tenant.get("landing_template")
    if not template:
        return None
    return template.replace("{igsn}", igsn)


def _record_doc(
    igsn: str,
    *,
    prefix: str,
    tenant: dict,
    metadata: dict,
    landing_page: str | None,
    external_ref: dict | None,
    parent_igsn: str | None = None,
) -> dict:
    now = _now()
    doi = doi_for(igsn, tenant["datacite"]["doi_prefix"])
    metadata = dict(metadata or {})
    if metadata:
        # Keep the stored copy self-consistent. The authoritative values are the
        # record's own fields; publication re-derives these from them regardless.
        metadata["doi"] = doi
        if landing_page:
            metadata["url"] = landing_page
    return {
        "igsn": igsn,
        "doi": doi,
        "prefix": prefix,
        "parent_igsn": parent_igsn,
        "tenant": tenant["slug"],
        "status": "reserved",
        "metadata": metadata,
        "landing_page": landing_page,
        "external_ref": external_ref,
        "created": now,
        "updated": now,
        "published_at": None,
        "datacite": {"last_sync": None, "last_error": None, "attempts": 0},
    }


def _inserted_count(exc: Exception) -> int:
    """How many documents an ordered insert_many managed before it failed."""
    details = getattr(exc, "details", None) or {}
    return int(details.get("nInserted") or details.get("insertedCount") or 0)


async def _audit(action: str, tenant: dict, igsns: list[str], detail: dict | None = None) -> None:
    await get_db()[AUDIT].insert_one(
        {
            "action": action,
            "tenant": tenant["slug"],
            "igsn": igsns[0] if len(igsns) == 1 else None,
            "igsns": igsns,
            "count": len(igsns),
            "detail": detail or {},
            "created": _now(),
        }
    )


async def allocate(
    prefix: str,
    count: int,
    tenant: dict,
    *,
    metadata: dict | None = None,
    landing_page: str | None = None,
    external_ref: dict | None = None,
    db=None,
) -> list[dict]:
    """Allocate ``count`` top-level IGSNs under ``prefix``.

    Returns the created record documents. Raises AllocationError if a contiguous
    block could not be claimed after bounded retries.
    """
    db = db if db is not None else get_db()
    vocabularies = await get_vocabularies(db)
    prefix = validate_prefix(prefix, vocabularies, tenant.get("allowed_institutions"))
    metadata = validate_metadata(metadata)

    last_error: Exception | None = None
    for attempt in range(MAX_ALLOCATION_RETRIES):
        counter = await db[COUNTERS].find_one_and_update(
            {"_id": prefix},
            {"$inc": {"seq": count}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        high = int(counter["seq"])
        igsns = [
            f"{prefix}{n:0{SEQUENCE_WIDTH}d}" for n in range(high - count + 1, high + 1)
        ]
        docs = [
            _record_doc(
                igsn,
                prefix=prefix,
                tenant=tenant,
                metadata=metadata,
                landing_page=landing_page_for(igsn, tenant, landing_page),
                external_ref=external_ref,
            )
            for igsn in igsns
        ]
        try:
            await db[RECORDS].insert_many(docs, ordered=True)
        except (BulkWriteError, DuplicateKeyError) as exc:
            # The counter was behind the records that already exist. Increment
            # again rather than reusing an identifier. See module docstring.
            last_error = exc
            logger.warning(
                "Counter %s was stale (attempt %s/%s): %s",
                prefix,
                attempt + 1,
                MAX_ALLOCATION_RETRIES,
                igsns[0],
            )
            continue
        await _audit("allocate", tenant, igsns, {"prefix": prefix, "count": count})
        return docs

    raise AllocationError(
        f"Could not allocate {count} identifier(s) under {prefix} after "
        f"{MAX_ALLOCATION_RETRIES} attempts: {last_error}"
    )


async def next_numeric_indices(parent_igsn: str, count: int, db=None) -> list[str]:
    """Pick the next free zero-padded numeric child indices.

    Replaces the local ``^{igsn}-(\\d+)$`` max-scan that girder-jsonforms did in
    ``rest/deposition.py``, which was only ever correct while a parent's children
    could not be created from more than one instance.
    """
    db = db if db is not None else get_db()
    highest = 0
    cursor = db[RECORDS].find({"parent_igsn": parent_igsn}, {"igsn": 1})
    async for doc in cursor:
        suffix = doc["igsn"][len(parent_igsn) + 1 :]
        if NUMERIC_INDEX.match(suffix):
            highest = max(highest, int(suffix))
    return [f"{n:03d}" for n in range(highest + 1, highest + 1 + count)]


def derive_child_metadata(
    parent_metadata: dict,
    index: str,
    local_identifier: str | None = None,
    override: dict | None = None,
) -> dict:
    """Build a child's metadata from its parent's.

    Mirrors ``Deposition.create_batch`` in girder-jsonforms: title gains the
    index, an ``IsPartOf`` relation to the parent is added, and ``HasPart``
    relations are dropped so children don't claim their siblings. Girder sends
    the metadata it computed, so in practice this is the path external
    (non-Girder) clients take.
    """
    if override:
        return dict(override)

    metadata = {k: v for k, v in (parent_metadata or {}).items()}
    metadata.pop("doi", None)
    metadata.pop("url", None)

    related = [
        r
        for r in metadata.get("relatedIdentifiers", [])
        if r.get("relationType") != "HasPart"
    ]
    metadata["relatedIdentifiers"] = related

    titles = metadata.get("titles") or [{"title": ""}]
    metadata["titles"] = [{"title": f"{titles[0].get('title', '')} - {index}"}]

    if local_identifier:
        metadata["alternateIdentifiers"] = [
            {"alternateIdentifier": local_identifier, "alternateIdentifierType": "Local"}
        ]
    return metadata


async def allocate_children(
    parent_igsn: str,
    tenant: dict,
    *,
    indices: list[str] | None = None,
    count: int | None = None,
    metadata: dict | None = None,
    local_identifiers: dict[str, str] | None = None,
    db=None,
) -> list[dict]:
    """Register children of ``parent_igsn``.

    Children are suffixes on an already-unique parent, so no counter is
    involved -- but the insert is still ``ordered=True`` against the unique
    index, so a batch that collides fails whole rather than half-registering.
    That matters to the caller: Girder aborts its local batch on rejection, and
    would otherwise be left holding children the registry doesn't know about.
    """
    db = db if db is not None else get_db()
    parent = await db[RECORDS].find_one({"igsn": parent_igsn})
    if parent is None:
        raise AllocationError(f"Parent {parent_igsn} is not registered")
    if parent["tenant"] != tenant["slug"]:
        raise AllocationError(f"Parent {parent_igsn} belongs to another tenant")
    if parent["status"] == "revoked":
        raise AllocationError(f"Parent {parent_igsn} is revoked")

    # Explicit indices are the caller's choice and a collision is their error.
    # Count-based indices are ours to pick, and picking them is a read-then-write
    # that two concurrent callers can lose -- so retry those rather than making
    # the caller handle a race it didn't create.
    if indices is None:
        for attempt in range(MAX_ALLOCATION_RETRIES):
            picked = await next_numeric_indices(parent_igsn, count or 0, db=db)
            try:
                return await allocate_children(
                    parent_igsn,
                    tenant,
                    indices=picked,
                    metadata=metadata,
                    local_identifiers=local_identifiers,
                    db=db,
                )
            except AllocationError as exc:
                if "already exist" not in str(exc) or attempt == MAX_ALLOCATION_RETRIES - 1:
                    raise
                logger.warning(
                    "Child index collision under %s (attempt %s/%s); re-picking",
                    parent_igsn,
                    attempt + 1,
                    MAX_ALLOCATION_RETRIES,
                )

    if not indices:
        raise AllocationError("No child indices to register")

    local_identifiers = local_identifiers or {}
    parent_related = {
        "relationType": "IsPartOf",
        "relatedIdentifier": parent_igsn,
        "relatedIdentifierType": "IGSN",
    }

    docs = []
    for index in indices:
        child_igsn = f"{parent_igsn}-{index}"
        child_metadata = derive_child_metadata(
            parent.get("metadata", {}),
            index,
            local_identifier=local_identifiers.get(index),
            override=metadata,
        )
        if child_metadata:
            child_metadata.setdefault("relatedIdentifiers", [])
            if parent_related not in child_metadata["relatedIdentifiers"]:
                child_metadata["relatedIdentifiers"] = [
                    *child_metadata["relatedIdentifiers"],
                    parent_related,
                ]
            validate_metadata(child_metadata)
        docs.append(
            _record_doc(
                child_igsn,
                prefix=parent["prefix"],
                tenant=tenant,
                metadata=child_metadata,
                landing_page=landing_page_for(child_igsn, tenant),
                external_ref=None,
                parent_igsn=parent_igsn,
            )
        )

    try:
        await db[RECORDS].insert_many(docs, ordered=True)
    except (BulkWriteError, DuplicateKeyError) as exc:
        # `ordered=True` stops at the first duplicate, which leaves the children
        # before it inserted. Roll those back so the batch really is
        # all-or-nothing: callers (Girder's create_batch) abort their own insert
        # on this error, and a half-registered batch would leave the registry
        # holding children nothing else knows about, with the parent's HasPart
        # list disagreeing about which children exist.
        inserted = _inserted_count(exc)
        if inserted:
            await db[RECORDS].delete_many(
                {"igsn": {"$in": [doc["igsn"] for doc in docs[:inserted]]}}
            )
        raise AllocationError(
            f"One or more children of {parent_igsn} already exist: {exc}"
        ) from exc

    # Record the HasPart edges on the parent so its DataCite metadata lists them.
    has_parts = [
        {
            "relationType": "HasPart",
            "relatedIdentifier": doc["igsn"],
            "relatedIdentifierType": "IGSN",
        }
        for doc in docs
    ]
    await db[RECORDS].update_one(
        {"igsn": parent_igsn},
        {
            "$addToSet": {"metadata.relatedIdentifiers": {"$each": has_parts}},
            "$set": {"updated": _now()},
        },
    )

    await _audit(
        "allocate_children",
        tenant,
        [d["igsn"] for d in docs],
        {"parent": parent_igsn},
    )
    return docs
