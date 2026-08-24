"""DataCite REST client.

Ported from the hand-run scripts that did this job before centralization
(``htmdec/datacite/publish_prod.py`` and ``update_prod.py``), including the
non-obvious bit: DataCite does not accept ``IGSN``-typed related identifiers,
so every ``IsPartOf``/``HasPart`` edge -- which girder-jsonforms emits as
``relatedIdentifierType: "IGSN"`` for batches -- has to be rewritten to a
lowercase DOI before submission.
"""

import datetime
import logging
from typing import Any

import httpx

from ..config import datacite_password, get_settings

logger = logging.getLogger(__name__)

JSONAPI = "application/vnd.api+json"

# Target state -> DataCite `event`. Omitting `event` leaves the DOI a draft.
EVENT_FOR_TARGET = {"draft": None, "registered": "register", "findable": "publish"}

# Metadata keys DataCite derives itself; sending ours would be ignored at best.
_STRIPPED_KEYS = ("suffix",)


class DataCiteError(RuntimeError):
    """A DataCite call failed.

    ``retryable`` distinguishes a transient failure (429, 5xx, timeout) from a
    terminal one (422 validation, 401/403 credentials). Terminal failures must
    not be retried in a loop -- they need a human.
    """

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class DataCiteNotConfigured(DataCiteError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False)


def rewrite_related_identifiers(
    related: list[dict[str, Any]], doi_prefix: str
) -> list[dict[str, Any]]:
    """Convert IGSN-typed related identifiers to lowercase DOIs.

    ``update_prod.py`` did exactly this by hand. Without it, DataCite rejects
    every batch parent and child.
    """
    out = []
    for item in related or []:
        item = dict(item)
        if item.get("relatedIdentifierType") == "IGSN":
            identifier = str(item.get("relatedIdentifier", ""))
            # Already a DOI (has a slash)? Leave the value, just fix the type.
            if "/" not in identifier:
                identifier = f"{doi_prefix}/{identifier}"
            item["relatedIdentifier"] = identifier.lower()
            item["relatedIdentifierType"] = "DOI"
        out.append(item)
    return out


def build_attributes(record: dict, tenant: dict, target: str) -> dict[str, Any]:
    """Build the ``data.attributes`` payload for a record."""
    settings = get_settings()
    doi_prefix = tenant["datacite"]["doi_prefix"]

    attributes: dict[str, Any] = dict(record.get("metadata") or {})
    for key in _STRIPPED_KEYS:
        attributes.pop(key, None)

    attributes["prefix"] = doi_prefix
    attributes["doi"] = record["doi"]
    # Central resolver is canonical; a tenant-supplied landing page wins when set,
    # which is what keeps already-published DOIs pointing where they always did.
    attributes["url"] = record.get("landing_page") or (
        f"{settings.public_url.rstrip('/')}/{record['igsn']}"
    )

    if attributes.get("relatedIdentifiers"):
        attributes["relatedIdentifiers"] = rewrite_related_identifiers(
            attributes["relatedIdentifiers"], doi_prefix
        )

    event = EVENT_FOR_TARGET.get(target)
    if event:
        attributes["event"] = event
    else:
        attributes.pop("event", None)

    return attributes


def _auth(tenant: dict) -> tuple[str, str]:
    config = tenant.get("datacite") or {}
    repository_id = config.get("repository_id")
    password = datacite_password(config.get("password_env", ""))
    if not repository_id or not password:
        raise DataCiteNotConfigured(
            f"Tenant {tenant.get('slug')} has no usable DataCite credentials "
            f"(repository_id={repository_id!r}, password_env="
            f"{config.get('password_env')!r} is unset in the environment)"
        )
    return repository_id, password


def _classify(response: httpx.Response) -> DataCiteError:
    body = response.text[:2000]
    status = response.status_code
    retryable = status == 429 or status >= 500
    return DataCiteError(
        f"DataCite returned {status}: {body}", retryable=retryable, status_code=status
    )


def _already_exists(response: httpx.Response) -> bool:
    """DataCite reports a duplicate DOI as a 422 on the `doi` field."""
    if response.status_code not in (409, 422):
        return False
    return "already been taken" in response.text or "already exists" in response.text


async def submit(record: dict, tenant: dict, target: str) -> dict:
    """Create or update a DOI. Returns ``{status, doi, response}``.

    Reserved records are POSTed; anything already known to DataCite is PUT. A
    POST that collides with an existing DOI falls back to PUT, so re-running a
    publish is safe.
    """
    settings = get_settings()
    repository_id, password = _auth(tenant)
    api_base = (tenant["datacite"].get("api_base") or "https://api.datacite.org").rstrip("/")
    attributes = build_attributes(record, tenant, target)
    payload = {"data": {"type": "dois", "attributes": attributes}}
    headers = {"accept": JSONAPI, "content-type": "application/json"}

    async with httpx.AsyncClient(
        auth=(repository_id, password), timeout=settings.datacite_timeout_seconds
    ) as client:
        try:
            if record.get("status") == "reserved":
                response = await client.post(
                    f"{api_base}/dois", json=payload, headers=headers
                )
                if _already_exists(response):
                    logger.info(
                        "DOI %s already exists at DataCite; updating instead", record["doi"]
                    )
                    response = await client.put(
                        f"{api_base}/dois/{record['doi']}", json=payload, headers=headers
                    )
            else:
                response = await client.put(
                    f"{api_base}/dois/{record['doi']}", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise DataCiteError(f"DataCite request failed: {exc}", retryable=True) from exc

    if response.status_code >= 400:
        raise _classify(response)

    return {
        "status": target,
        "doi": record["doi"],
        "response": response.json() if response.content else {},
        "synced_at": datetime.datetime.now(datetime.UTC),
    }


async def fetch(doi: str, tenant: dict) -> dict | None:
    """Read a DOI back from DataCite. Used by the backfill to learn real status."""
    settings = get_settings()
    repository_id, password = _auth(tenant)
    api_base = (tenant["datacite"].get("api_base") or "https://api.datacite.org").rstrip("/")
    async with httpx.AsyncClient(
        auth=(repository_id, password), timeout=settings.datacite_timeout_seconds
    ) as client:
        response = await client.get(
            f"{api_base}/dois/{doi}", headers={"accept": JSONAPI}
        )
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise _classify(response)
    return response.json()
