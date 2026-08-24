"""Management commands, chiefly the Girder backfill.

Backfill order matters and the commands are deliberately separate so each step
can be inspected before the next:

    1. import-girder   -- copy depositions in as records (repeat per instance)
    2. seed-counters   -- set each prefix's counter to the observed high-water
                          mark, only ever raising it
    3. sync-status     -- learn real publication status from DataCite, because
                          Girder's `state` says "draft" even for DOIs that were
                          published by hand
    4. verify          -- report drift before any instance is switched over

Step 2 is the one that must not be wrong: a counter below reality is how you
mint a duplicate IGSN. The allocator's duplicate-key retry is a backstop, not a
substitute for getting this right, and ``seed_counters`` refuses to lower a
counter for the same reason.

The ``async def`` functions below hold the actual logic and are what the tests
exercise; the click commands are a thin shell that connects to Mongo and calls
them.
"""

import asyncio
import datetime
import json
import logging
import re
import sys

import click

from .auth import generate_token, hash_token
from .db import COUNTERS, RECORDS, TENANTS, connect, disconnect, ensure_indexes, get_db
from .services import datacite
from .services.allocator import doi_for

logger = logging.getLogger(__name__)

# A full IGSN: 6-char prefix, 5-digit sequence, then any number of batch
# suffixes. Kept in step with IGSN_REGEX in girder-jsonforms' settings.py.
IGSN_RE = re.compile(r"^([A-Z]{6})(\d{5})((?:-[A-Za-z0-9]+)*)$")


def parse_igsn(igsn: str) -> tuple[str, int, str | None] | None:
    """Split an IGSN into (prefix, sequence, parent), or None if malformed.

    The sequence of a child is its *parent's* sequence -- ``JHAMAA00007-003``
    parses as sequence 7, not 3. Getting that wrong would inflate counters.
    """
    match = IGSN_RE.match(igsn.upper())
    if not match:
        return None
    prefix, sequence, suffixes = match.groups()
    parent = igsn.upper().rsplit("-", 1)[0] if suffixes else None
    return prefix, int(sequence), parent


def _highest_top_level(igsns) -> dict[str, int]:
    """Observed high-water mark per prefix, ignoring children and junk."""
    observed: dict[str, int] = {}
    for igsn in igsns:
        parsed = parse_igsn(igsn)
        if parsed is None:
            continue
        prefix, sequence, parent = parsed
        if parent is not None:
            continue
        observed[prefix] = max(observed.get(prefix, 0), sequence)
    return observed


# --------------------------------------------------------------------------- #
# Logic
# --------------------------------------------------------------------------- #


async def create_tenant(
    *,
    slug: str,
    name: str,
    doi_prefix: str,
    repository_id: str,
    password_env: str,
    api_base: str = "https://api.datacite.org",
    institutions: list[str] | None = None,
    landing_template: str | None = None,
    db=None,
) -> str:
    """Create a service account and return its plaintext token (shown once)."""
    db = db if db is not None else get_db()
    if await db[TENANTS].find_one({"slug": slug}):
        raise ValueError(f"Tenant {slug} already exists")
    token = generate_token()
    await db[TENANTS].insert_one(
        {
            "slug": slug,
            "name": name,
            "allowed_institutions": list(institutions or []),
            "datacite": {
                "repository_id": repository_id,
                "doi_prefix": doi_prefix,
                "api_base": api_base,
                "password_env": password_env,
            },
            "landing_template": landing_template,
            "active": True,
            "token_sha256": hash_token(token),
            "created": datetime.datetime.now(datetime.UTC),
        }
    )
    return token


async def import_girder(
    *,
    girder_client,
    tenant_slug: str,
    landing_template: str | None = None,
    page_size: int = 500,
    dry_run: bool = False,
    db=None,
) -> dict:
    """Copy a Girder instance's depositions in as records.

    Safe to re-run: existing records are left alone rather than overwritten, so
    a second pass only picks up what appeared since the first.
    """
    db = db if db is not None else get_db()
    tenant = await db[TENANTS].find_one({"slug": tenant_slug})
    if tenant is None:
        raise ValueError(f"No tenant {tenant_slug}; create it first")

    template = landing_template or tenant.get("landing_template")
    doi_prefix = tenant["datacite"]["doi_prefix"]

    imported = skipped = malformed = 0
    seen: list[str] = []
    offset = 0

    while True:
        page = girder_client.get(
            "deposition", parameters={"limit": page_size, "offset": offset}
        )
        if not page:
            break
        offset += len(page)

        for deposition in page:
            igsn = (deposition.get("igsn") or "").upper()
            if parse_igsn(igsn) is None:
                logger.warning("Skipping malformed IGSN %r", igsn)
                malformed += 1
                continue
            seen.append(igsn)
            prefix, _sequence, parent = parse_igsn(igsn)

            if await db[RECORDS].find_one({"igsn": igsn}):
                skipped += 1
                continue

            # The list endpoint drops metadata.relatedIdentifiers (they are
            # ACLed), so fetch the record itself for a complete metadata copy.
            full = girder_client.get(f"deposition/{deposition['_id']}")
            doc = {
                "igsn": igsn,
                "doi": doi_for(igsn, doi_prefix),
                "prefix": prefix,
                "parent_igsn": parent,
                "tenant": tenant_slug,
                # Girder never advanced `state`, so everything arrives reserved.
                # sync_status corrects this from DataCite itself.
                "status": "reserved",
                "metadata": full.get("metadata") or {},
                "landing_page": template.replace("{igsn}", igsn) if template else None,
                "external_ref": {
                    "system": "girder",
                    "instance": getattr(girder_client, "urlBase", None),
                    "id": str(deposition["_id"]),
                },
                "created": deposition.get("created"),
                "updated": datetime.datetime.now(datetime.UTC),
                "published_at": None,
                "datacite": {"last_sync": None, "last_error": None, "attempts": 0},
                "imported": True,
            }
            if not dry_run:
                await db[RECORDS].insert_one(doc)
            imported += 1

    return {
        "imported": imported,
        "skipped": skipped,
        "malformed": malformed,
        "observed": _highest_top_level(seen),
    }


async def seed_counters(
    *, observed: dict[str, int] | None = None, dry_run: bool = False, db=None
) -> list[dict]:
    """Raise each prefix's counter to its high-water mark. Never lowers one.

    Pass ``observed`` to supply marks computed across *every* instance; omit it
    to derive them from the records already imported.
    """
    db = db if db is not None else get_db()
    if observed is None:
        igsns = [doc["igsn"] async for doc in db[RECORDS].find({}, {"igsn": 1})]
        observed = _highest_top_level(igsns)

    results = []
    for prefix in sorted(observed):
        target = int(observed[prefix])
        existing = await db[COUNTERS].find_one({"_id": prefix})
        current = int(existing["seq"]) if existing else 0
        raised = current < target
        if raised and not dry_run:
            await db[COUNTERS].update_one(
                {"_id": prefix}, {"$set": {"seq": target}}, upsert=True
            )
        results.append(
            {"prefix": prefix, "from": current, "to": target if raised else current,
             "raised": raised}
        )
    return results


async def sync_status(
    *, tenant_slug: str, prefix: str | None = None, dry_run: bool = False, db=None
) -> dict:
    """Learn each reserved record's real publication status from DataCite.

    Girder's `state` reads "draft" for every deposition, including ones the
    hand-run publish scripts already pushed. Trusting it would mean re-POSTing
    live DOIs, so status is read back from DataCite instead of guessed.
    """
    db = db if db is not None else get_db()
    tenant = await db[TENANTS].find_one({"slug": tenant_slug})
    if tenant is None:
        raise ValueError(f"No tenant {tenant_slug}")

    query: dict = {"tenant": tenant_slug, "status": "reserved"}
    if prefix:
        query["prefix"] = prefix.upper()

    found = unpublished = errors = 0
    async for record in db[RECORDS].find(query):
        try:
            remote = await datacite.fetch(record["doi"], tenant)
        except datacite.DataCiteError as exc:
            logger.error("%s: %s", record["igsn"], exc)
            errors += 1
            continue

        state = None
        if remote:
            state = ((remote.get("data") or {}).get("attributes") or {}).get("state")
        status = {"findable": "findable", "registered": "registered"}.get(state)
        if status is None:
            unpublished += 1
            continue

        found += 1
        logger.info("%s is %s at DataCite", record["igsn"], status)
        if not dry_run:
            await db[RECORDS].update_one(
                {"igsn": record["igsn"]},
                {
                    "$set": {
                        "status": status,
                        "published_at": record.get("created"),
                        "datacite.last_sync": datetime.datetime.now(datetime.UTC),
                    }
                },
            )

    return {"published": found, "unpublished": unpublished, "errors": errors}


async def verify(*, db=None) -> dict:
    """Compare counters against the records that exist. Pre-flight for cutover."""
    db = db if db is not None else get_db()
    counters = {doc["_id"]: int(doc["seq"]) async for doc in db[COUNTERS].find({})}

    igsns = []
    totals: dict[str, int] = {}
    async for record in db[RECORDS].find({}, {"igsn": 1}):
        igsns.append(record["igsn"])
        parsed = parse_igsn(record["igsn"])
        if parsed:
            totals[parsed[0]] = totals.get(parsed[0], 0) + 1
    observed = _highest_top_level(igsns)

    rows = []
    for prefix in sorted(set(counters) | set(observed)):
        counter = counters.get(prefix, 0)
        highest = observed.get(prefix, 0)
        rows.append(
            {
                "prefix": prefix,
                "seq": counter,
                "highest_record": highest,
                "records": totals.get(prefix, 0),
                "behind": counter < highest,
            }
        )

    by_status: dict[str, int] = {}
    async for row in db[RECORDS].aggregate(
        [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ):
        by_status[row["_id"]] = row["n"]

    return {
        "prefixes": rows,
        "by_status": by_status,
        "behind": [row["prefix"] for row in rows if row["behind"]],
    }


# --------------------------------------------------------------------------- #
# CLI shell
# --------------------------------------------------------------------------- #


def _run(factory):
    """Connect, run one coroutine factory, disconnect."""

    async def wrapper():
        await connect()
        await ensure_indexes()
        try:
            return await factory()
        finally:
            await disconnect()

    return asyncio.run(wrapper())


@click.group()
@click.option("--verbose", is_flag=True)
def cli(verbose):
    """IGSN registry management."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)


@cli.command("create-tenant")
@click.option("--slug", required=True)
@click.option("--name", required=True)
@click.option("--doi-prefix", required=True, help="e.g. 10.82581")
@click.option("--repository-id", required=True, help="DataCite repository, e.g. jhu.hemi-igsn")
@click.option("--password-env", required=True, help="Env var holding the Fabrica password")
@click.option("--api-base", default="https://api.datacite.org", show_default=True)
@click.option(
    "--institution",
    "institutions",
    multiple=True,
    help="Institution code this tenant may mint under (repeatable). Empty means any.",
)
@click.option(
    "--landing-template",
    default=None,
    help="e.g. 'https://data.htmdec.org/#igsn/{igsn}'. Omit to use the central resolver.",
)
def cli_create_tenant(**kwargs):
    """Create a service account and print its token (shown once)."""
    kwargs["institutions"] = list(kwargs.pop("institutions"))

    async def inner():
        return await create_tenant(**kwargs)

    try:
        token = _run(inner)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Created tenant {kwargs['slug']}")
    click.echo(f"Token (store it now, it cannot be recovered): {token}")


@cli.command("import-girder")
@click.option("--api-url", required=True, help="e.g. https://data.htmdec.org/api/v1")
@click.option("--tenant", "tenant_slug", required=True)
@click.option("--api-key", default=None, help="Girder API key, for non-public depositions")
@click.option(
    "--landing-template",
    default=None,
    help="Overrides the tenant's template for imported records.",
)
@click.option("--page-size", default=500, show_default=True)
@click.option("--dry-run", is_flag=True, help="Report what would be imported and stop.")
def cli_import_girder(api_url, tenant_slug, api_key, landing_template, page_size, dry_run):
    """Copy a Girder instance's depositions in as records."""
    try:
        from girder_client import GirderClient
    except ImportError as exc:  # pragma: no cover
        raise click.ClickException(
            "girder-client is required: pip install 'igsn-service[backfill]'"
        ) from exc

    client = GirderClient(apiUrl=api_url)
    if api_key:
        client.authenticate(apiKey=api_key)

    async def inner():
        return await import_girder(
            girder_client=client,
            tenant_slug=tenant_slug,
            landing_template=landing_template,
            page_size=page_size,
            dry_run=dry_run,
        )

    try:
        result = _run(inner)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{'Would import' if dry_run else 'Imported'}: {result['imported']}")
    click.echo(f"Already present: {result['skipped']}")
    if result["malformed"]:
        click.echo(f"Malformed IGSNs skipped: {result['malformed']}")
    click.echo(
        "\nObserved high-water marks. Take the max of these across every "
        "instance before running seed-counters:"
    )
    click.echo(json.dumps(result["observed"], sort_keys=True))


@cli.command("seed-counters")
@click.option(
    "--from-json",
    is_flag=True,
    help="Read a {prefix: high_water_mark} JSON map on stdin instead of deriving it.",
)
@click.option("--dry-run", is_flag=True)
def cli_seed_counters(from_json, dry_run):
    """Raise each prefix's counter to its high-water mark. Never lowers one."""
    observed = {k: int(v) for k, v in json.load(sys.stdin).items()} if from_json else None

    async def inner():
        return await seed_counters(observed=observed, dry_run=dry_run)

    for row in _run(inner):
        if row["raised"]:
            click.echo(f"  {row['prefix']}: {row['from']} -> {row['to']}")
        else:
            click.echo(
                f"  {row['prefix']}: already at {row['from']} "
                f"(>= {row['to']}), unchanged"
            )
    if dry_run:
        click.echo("\n(dry run; nothing written)")


@cli.command("sync-status")
@click.option("--tenant", "tenant_slug", required=True)
@click.option("--prefix", default=None, help="Limit to one 6-char IGSN prefix.")
@click.option("--dry-run", is_flag=True)
def cli_sync_status(tenant_slug, prefix, dry_run):
    """Learn each record's real publication status from DataCite."""

    async def inner():
        return await sync_status(tenant_slug=tenant_slug, prefix=prefix, dry_run=dry_run)

    try:
        result = _run(inner)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Already at DataCite: {result['published']}")
    click.echo(f"Not yet published: {result['unpublished']}")
    if result["errors"]:
        click.echo(f"Lookup errors: {result['errors']}", err=True)


@cli.command("verify")
def cli_verify():
    """Report counter drift and record counts. Run before switching instances over."""
    result = _run(verify)

    click.echo(f"{'prefix':<8} {'counter':>8} {'highest':>8} {'records':>8}  status")
    for row in result["prefixes"]:
        click.echo(
            f"{row['prefix']:<8} {row['seq']:>8} {row['highest_record']:>8} "
            f"{row['records']:>8}  {'BEHIND' if row['behind'] else 'ok'}"
        )
    click.echo("\nRecords by status: " + json.dumps(result["by_status"], sort_keys=True))

    if result["behind"]:
        click.echo(
            f"\n{len(result['behind'])} prefix(es) have a counter behind their "
            "highest record. Run seed-counters before switching any instance to "
            "remote mode.",
            err=True,
        )
        raise SystemExit(1)
    click.echo("\nAll counters are at or above their highest record.")


if __name__ == "__main__":  # pragma: no cover
    cli()
