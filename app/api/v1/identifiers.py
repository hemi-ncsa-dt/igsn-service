"""Allocation endpoints.

Note for clients: ``POST /identifiers`` is **not** idempotent and must never be
blindly retried -- a retry consumes another block of sequence numbers. On an
ambiguous failure (timeout, 502) look the allocation up via
``GET /records?prefix=...`` instead of re-POSTing.
"""

import logging

from fastapi import APIRouter, HTTPException, status

from ...auth import TenantDep
from ...schemas import AllocateRequest, AllocateResponse, ChildrenRequest
from ...services import allocator
from .common import serialize_record

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identifiers"])


@router.post(
    "/identifiers",
    response_model=AllocateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Allocate one or more top-level IGSNs",
)
async def allocate_identifiers(body: AllocateRequest, tenant: dict = TenantDep):
    try:
        docs = await allocator.allocate(
            body.prefix,
            body.count,
            tenant,
            metadata=body.metadata,
            landing_page=body.landing_page,
            external_ref=body.external_ref.model_dump() if body.external_ref else None,
        )
    except allocator.PrefixError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except allocator.MetadataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except allocator.AllocationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AllocateResponse(records=[serialize_record(doc) for doc in docs])


@router.post(
    "/identifiers/{igsn}/children",
    response_model=AllocateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register children of an existing IGSN",
)
async def allocate_children(igsn: str, body: ChildrenRequest, tenant: dict = TenantDep):
    try:
        docs = await allocator.allocate_children(
            igsn.upper(),
            tenant,
            indices=body.indices,
            count=body.count,
            metadata=body.metadata,
            local_identifiers=body.local_identifiers,
        )
    except allocator.MetadataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except allocator.AllocationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AllocateResponse(records=[serialize_record(doc) for doc in docs])
