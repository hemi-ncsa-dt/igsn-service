"""Public IGSN resolver.

``GET /{igsn}`` is the only unauthenticated read path. It is what a DataCite DOI
ultimately points at, so its behavior is a published contract:

- a record with a tenant ``landing_page`` redirects there (302), which is what
  keeps DOIs already published against ``data.htmdec.org/#igsn/...`` working;
- otherwise the service renders the record from its own metadata, so a tenant
  with no web presence still gets a landing page;
- ``Accept: application/json`` (or the DataCite media type) returns metadata
  instead of HTML, for machine clients.
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import RECORDS, get_db

logger = logging.getLogger(__name__)

# Matches the canonical IGSN shape: 6-char prefix + 5-digit sequence, plus any
# number of batch suffixes. Kept deliberately in step with IGSN_REGEX in
# girder-jsonforms' settings.py.
IGSN_PATH = re.compile(r"^[A-Za-z]{6}[0-9]{5}[A-Za-z0-9\-]*$")

JSON_TYPES = (
    "application/json",
    "application/vnd.api+json",
    "application/vnd.datacite.datacite+json",
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter(tags=["resolver"])


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if any(media in accept for media in JSON_TYPES):
        # An HTML-first browser Accept header also lists */*; prefer HTML there.
        return "text/html" not in accept
    return False


@router.get("/{igsn}", include_in_schema=False)
async def resolve(igsn: str, request: Request):
    if not IGSN_PATH.match(igsn):
        raise HTTPException(status_code=404, detail="Not an IGSN")

    record = await get_db()[RECORDS].find_one({"igsn": igsn.upper()})
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown IGSN {igsn.upper()}")

    if record["status"] == "revoked":
        # Tombstone: say so rather than 404, so a citation of a withdrawn sample
        # gets an explanation instead of a dead end.
        if _wants_json(request):
            return JSONResponse(
                {"igsn": record["igsn"], "status": "revoked"}, status_code=410
            )
        return templates.TemplateResponse(
            request=request,
            name="tombstone.html",
            context={"record": record},
            status_code=410,
        )

    if _wants_json(request):
        return JSONResponse(
            {
                "igsn": record["igsn"],
                "doi": record["doi"],
                "status": record["status"],
                "landing_page": record.get("landing_page"),
                "attributes": record.get("metadata", {}),
            }
        )

    if record.get("landing_page"):
        return RedirectResponse(record["landing_page"], status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={"record": record, "metadata": record.get("metadata", {})},
    )


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})
