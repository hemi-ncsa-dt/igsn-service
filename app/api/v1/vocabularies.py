"""Controlled vocabularies.

Girder reads these instead of its own ``jsonforms.igsn_institutions`` /
``jsonforms.igsn_materials`` settings once it is pointed at the service, so
prefix validation and the form dropdowns agree across every instance.
"""

import hashlib
import json

from fastapi import APIRouter, HTTPException, Query, Response

from ...auth import TenantDep
from ...schemas import VocabulariesOut
from ...services.vocab import PrefixError, get_vocabularies, validate_prefix

router = APIRouter(tags=["vocabularies"])


@router.get("/vocabularies", response_model=VocabulariesOut)
async def read_vocabularies(response: Response, tenant: dict = TenantDep):
    doc = await get_vocabularies()
    out = VocabulariesOut(
        institutions=doc.get("institutions", {}),
        materials=doc.get("materials", {}),
        version=int(doc.get("version", 1)),
    )
    # Cheap client-side caching: Girder polls this on a TTL and can skip the body.
    payload = json.dumps(out.model_dump(), sort_keys=True).encode()
    response.headers["ETag"] = hashlib.sha256(payload).hexdigest()[:32]
    return out


@router.get("/prefixes/validate")
async def validate(prefix: str = Query(...), tenant: dict = TenantDep):
    """Check a prefix without consuming a sequence number."""
    vocabularies = await get_vocabularies()
    try:
        canonical = validate_prefix(
            prefix, vocabularies, tenant.get("allowed_institutions")
        )
    except PrefixError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    institutions = vocabularies["institutions"]
    materials = vocabularies["materials"]
    return {
        "prefix": canonical,
        "institution": institutions[canonical[:2]]["name"],
        "lab": institutions[canonical[:2]]["labs"][canonical[2]],
        "material": materials[canonical[3:5]]["name"],
        "submaterial": materials[canonical[3:5]]
        .get("subcategories", {"X": "empty"})
        .get(canonical[5]),
    }
