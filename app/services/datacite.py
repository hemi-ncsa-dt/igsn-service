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
from urllib.parse import urlparse

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

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        request: dict | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        # What we actually sent. DataCite's errors name a field but never echo
        # the value ("URL is not valid" without saying which URL), so without
        # this a 422 is unactionable.
        self.request = request or {}


class DataCiteNotConfigured(DataCiteError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class InvalidLandingUrl(DataCiteError):
    """A record's landing page is not something DataCite will accept."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, status_code=422)


def validate_landing_url(url: str | None, *, source: str = "landing_page") -> str:
    """Reject a URL DataCite would reject, while we can still say why.

    DataCite answers a bad ``url`` with ``{"source":"url","title":"URL is not
    valid"}`` and no echo of the value, which is close to undebuggable. Catching
    it here means the error names the actual string and the reason.
    """
    if not url or not url.strip():
        raise InvalidLandingUrl(f"{source} is empty; DataCite requires a URL")
    url = url.strip()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise InvalidLandingUrl(
            f"{source} {url!r} is not an absolute http(s) URL "
            f"(scheme={parsed.scheme or 'missing'!r}). A bare path or fragment "
            f"like '#igsn/XYZ' usually means the publishing system's server-root "
            f"setting is empty."
        )
    if not parsed.netloc:
        raise InvalidLandingUrl(f"{source} {url!r} has no host")
    host = parsed.hostname or ""
    if "." not in host:
        raise InvalidLandingUrl(
            f"{source} {url!r} has a host DataCite will not accept ({host!r}); "
            f"it must be a publicly resolvable name, not 'localhost' or a bare hostname"
        )
    return url


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


def summarize_request(method: str, url: str, attributes: dict) -> dict:
    """The parts of a submission worth keeping for post-mortem.

    Stored on the record and the attempt, and logged. Deliberately a summary
    rather than the whole payload: metadata can be large, and these are the
    fields DataCite actually rejects. The full payload goes to the log at DEBUG.
    """
    return {
        "method": method,
        "endpoint": url,
        "doi": attributes.get("doi"),
        "url": attributes.get("url"),
        "prefix": attributes.get("prefix"),
        "event": attributes.get("event"),
        "publisher": attributes.get("publisher"),
        "publicationYear": attributes.get("publicationYear"),
        "types": attributes.get("types"),
        "counts": {
            "creators": len(attributes.get("creators") or []),
            "titles": len(attributes.get("titles") or []),
            "relatedIdentifiers": len(attributes.get("relatedIdentifiers") or []),
            "alternateIdentifiers": len(attributes.get("alternateIdentifiers") or []),
        },
    }


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


def _classify(response: httpx.Response, request: dict | None = None) -> DataCiteError:
    body = response.text[:2000]
    status = response.status_code
    retryable = status == 429 or status >= 500
    message = f"DataCite returned {status}: {body}"
    # DataCite names the offending field but never the value, so append the
    # value we sent for whichever field it complained about.
    if request:
        for field in ("url", "doi", "prefix", "event"):
            if f'"source":"{field}"' in response.text or f'"source": "{field}"' in response.text:
                message += f" (we sent {field}={request.get(field)!r})"
    return DataCiteError(
        f"{message}", retryable=retryable, status_code=status, request=request
    )


def _already_exists(response: httpx.Response) -> bool:
    """DataCite reports a duplicate DOI as a 422 on the `doi` field."""
    if response.status_code not in (409, 422):
        return False
    return "already been taken" in response.text or "already exists" in response.text


async def submit(record: dict, tenant: dict, target: str) -> dict:
    """Create or update a DOI. Returns ``{status, doi, request, response}``.

    A record DataCite has never accepted is POSTed; one it has is PUT. The
    deciding field is ``datacite.last_sync``, not ``status`` -- a record whose
    first POST failed has status "failed", and keying off that would PUT a DOI
    that was never created and 404 forever.

    Both directions have a fallback, so a retry is always safe: a POST that
    collides with an existing DOI becomes a PUT, and a PUT for a DOI DataCite
    does not have becomes a POST.
    """
    settings = get_settings()
    repository_id, password = _auth(tenant)
    api_base = (tenant["datacite"].get("api_base") or "https://api.datacite.org").rstrip("/")
    attributes = build_attributes(record, tenant, target)

    # Preflight the URL rather than letting DataCite answer with an unactionable
    # "URL is not valid". This is the single most common cause of a 422 here.
    try:
        validate_landing_url(
            attributes.get("url"),
            source="landing_page" if record.get("landing_page") else "resolver url",
        )
    except InvalidLandingUrl as exc:
        # Attach the offending values so the stored attempt is self-describing,
        # the same as it would be for a rejection that came back from DataCite.
        exc.request = {
            "method": None,
            "endpoint": None,
            "doi": attributes.get("doi"),
            "url": attributes.get("url"),
            "rejected_by": "preflight",
        }
        raise

    payload = {"data": {"type": "dois", "attributes": attributes}}
    headers = {"accept": JSONAPI, "content-type": "application/json"}

    # Never synced means DataCite has no such DOI yet.
    never_synced = not (record.get("datacite") or {}).get("last_sync")
    method = "POST" if never_synced else "PUT"
    endpoint = f"{api_base}/dois" if method == "POST" else f"{api_base}/dois/{record['doi']}"
    request = summarize_request(method, endpoint, attributes)

    logger.info(
        "DataCite %s %s igsn=%s doi=%s target=%s url=%s",
        method,
        endpoint,
        record["igsn"],
        attributes.get("doi"),
        target,
        attributes.get("url"),
    )
    logger.debug("DataCite payload for %s: %s", record["igsn"], payload)

    async with httpx.AsyncClient(
        auth=(repository_id, password), timeout=settings.datacite_timeout_seconds
    ) as client:
        try:
            if method == "POST":
                response = await client.post(endpoint, json=payload, headers=headers)
                if _already_exists(response):
                    logger.info(
                        "DOI %s already exists at DataCite; updating instead",
                        record["doi"],
                    )
                    endpoint = f"{api_base}/dois/{record['doi']}"
                    request = summarize_request("PUT", endpoint, attributes)
                    response = await client.put(endpoint, json=payload, headers=headers)
            else:
                response = await client.put(endpoint, json=payload, headers=headers)
                if response.status_code == 404:
                    logger.info(
                        "DOI %s is not at DataCite despite a previous sync; creating it",
                        record["doi"],
                    )
                    endpoint = f"{api_base}/dois"
                    request = summarize_request("POST", endpoint, attributes)
                    response = await client.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("DataCite request for %s failed: %s", record["igsn"], exc)
            raise DataCiteError(
                f"DataCite request failed: {exc}", retryable=True, request=request
            ) from exc

    if response.status_code >= 400:
        error = _classify(response, request)
        logger.warning(
            "DataCite rejected %s (%s): %s",
            record["igsn"],
            response.status_code,
            response.text[:500],
        )
        raise error

    logger.info(
        "DataCite accepted %s as %s (%s)", record["igsn"], target, response.status_code
    )
    return {
        "status": target,
        "doi": record["doi"],
        "request": request,
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
