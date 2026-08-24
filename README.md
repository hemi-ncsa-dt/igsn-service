# igsn-service

Centralized allocation and DataCite publication of **IGSNs** — persistent
identifiers for physical samples, following the DataCite metadata kernel.

## Why this exists

IGSN minting started inside the `girder-jsonforms` Girder plugin, where each
Girder instance kept its own sequence counter in its own MongoDB. That works for
exactly one instance. With more than one, two instances sharing a prefix would
hand out the same IGSN; collisions were avoided only by keeping their prefixes
disjoint, which does not scale and breaks the moment two labs want the same
institution/material code. Publication to DataCite was not automated at all — it
was a set of hand-run scripts.

This service owns:

- **The identifier namespace.** One database, one atomic counter per prefix.
- **The DataCite metadata of record**, validated against DataCite kernel-4.
- **Publication**, with retries, so nobody runs a script by hand.
- **The controlled vocabularies** (institutions, materials) that define a valid
  prefix, so validation can no longer drift between instances.
- **A resolver**, so every IGSN has a landing page even for tenants with no web
  presence.

Girder keeps its own local record of every IGSN — it needs one for its ACLs,
search, sample tracking, and project wiring — but negotiates the identifier here
and delegates publication here.

## Design notes worth knowing before you change anything

**The unique index on `records.igsn` is load-bearing.** Allocation increments a
counter atomically, but the index is what actually guarantees uniqueness: if a
counter is ever behind reality (an imperfect backfill, a restored dump), the
insert fails with a duplicate key and the allocator increments again rather than
re-issuing an identifier. That makes a stale counter self-healing instead of an
incident. Don't drop the index, and don't "simplify" the retry away.

**Identifiers are never reused.** Revoking a record tombstones it; it does not
roll a counter back. `seed-counters` refuses to lower a counter for the same
reason, and so does the admin API.

**DataCite rejects IGSN-typed related identifiers.** Every batch relation Girder
emits (`IsPartOf` / `HasPart` with `relatedIdentifierType: "IGSN"`) is rewritten
to a lowercase DOI before submission. This was discovered the hard way in the
scripts this service replaces; see `app/services/datacite.py`.

**Authentication is coarse on purpose.** One bearer token per tenant — a Girder
instance or an external project — not per user. A tenant is limited to the
institution codes it may mint under, and every read is tenant-scoped. Real ACLs
stay in Girder, which is the only thing that has users. Tokens are stored as
SHA-256 only; the plaintext is shown once.

## Running

```bash
python -m venv venv && ./venv/bin/pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Configuration is environment-driven with an `IGSN_` prefix (see `app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `IGSN_MONGO_URI` | `mongodb://localhost:27017` | |
| `IGSN_MONGO_DB` | `igsn` | |
| `IGSN_ADMIN_TOKEN` | *(unset)* | Admin API token. Unset **disables** the admin API. |
| `IGSN_PUBLIC_URL` | `http://localhost:8000` | Used to build resolver URLs registered with DataCite. |
| `IGSN_PUBLISHER_ENABLED` | `true` | Background publication loop. |
| `IGSN_PUBLISHER_MAX_ATTEMPTS` | `8` | |

DataCite passwords are **not** stored in Mongo. A tenant names the environment
variable holding its Fabrica password (`datacite.password_env`), and the value is
read from the process environment at publication time — so a database dump is
not a credential leak.

## Tests

```bash
docker run -d --name igsn-test-mongo -p 27117:27017 mongo:7
./venv/bin/python -m pytest
```

They run against a real MongoDB rather than a mock, because the two behaviors
that matter most — atomic `$inc` and unique-index rejection — are exactly what a
mock is most likely to fake. Point `IGSN_TEST_MONGO_URI` elsewhere if you like.
A missing server **fails** the suite rather than skipping it: skipping produced
a run that read as green while testing nothing.

## API

`GET /docs` for the generated reference. In brief:

```
POST /api/v1/identifiers                     allocate top-level IGSNs
POST /api/v1/identifiers/{igsn}/children     register children of one
GET  /api/v1/records/{igsn}                  read a record
GET  /api/v1/records                         list, tenant-scoped
PUT  /api/v1/records/{igsn}                  replace metadata / landing page
POST /api/v1/records/{igsn}/publish          queue DataCite publication
GET  /api/v1/records/{igsn}/attempts         publication attempts + diagnostics
POST /api/v1/records/{igsn}/revoke           tombstone a reserved identifier
GET  /api/v1/vocabularies                    institutions + materials
GET  /api/v1/prefixes/validate?prefix=       check a prefix, consuming nothing
GET  /{igsn}                                 public resolver (HTML or JSON)
```

`POST /api/v1/identifiers` is **not idempotent** and must never be blindly
retried — a retry consumes another block of sequence numbers. On an ambiguous
failure, look the allocation up with `GET /api/v1/records?prefix=…` instead.

## Debugging a failed publish

A record that fails to publish records why, and what it sent:

```bash
# The error, plus a summary of the request that produced it
curl -sH "Authorization: Bearer $TOKEN" \
    https://igsn.example.org/api/v1/records/JHAMAA00001/attempts | jq

# Same diagnostics on the record itself
curl -sH "Authorization: Bearer $TOKEN" \
    https://igsn.example.org/api/v1/records/JHAMAA00001 | jq .datacite
```

`datacite.last_request` / `attempts[].last_request` carry the `url`, `doi`,
`prefix` and `event` actually submitted. This matters because DataCite's
validation errors name a field but never echo its value — `{"source":"url",
"title":"URL is not valid"}` says nothing about *which* URL. The service also
appends it to the message: `... (we sent url='#igsn/JHAMAA00001')`.

The publisher logs every submission at INFO (`DataCite POST … igsn=… url=…`)
and rejections at WARNING with the response body; set `IGSN_LOG_LEVEL=DEBUG` to
log full payloads. Credentials never appear in logs or in stored diagnostics.

**Invalid landing pages** are the most common cause. A publishing system whose
server-root setting is empty sends a relative value like `#igsn/JHAMAA00001`;
these are now rejected before DataCite sees them, at allocation and on
`PUT /records/{igsn}`, with an error naming the value. To repair records that
already have one:

```bash
igsn-service fix-landing-pages --dry-run   # then without --dry-run
```

Clearing the field is the repair, not guessing a replacement: the record falls
back to this service's own resolver, which is always valid. Already-published
records are left alone — their URL is registered at DataCite and changing it is
a deliberate act.

## Migration from Girder

Each step is a separate command so it can be inspected before the next:

```bash
# 1. one service account per Girder instance / external project
igsn-service create-tenant --slug htmdec-girder --name 'HTMDEC Girder' \
    --doi-prefix 10.82581 --repository-id jhu.hemi-igsn \
    --password-env DATACITE_PW_HTMDEC --institution JH \
    --landing-template 'https://data.htmdec.org/#igsn/{igsn}'

# 2. import every instance (repeat per instance; safe to re-run)
igsn-service import-girder --api-url https://data.htmdec.org/api/v1 \
    --tenant htmdec-girder --dry-run

# 3. raise counters -- only after EVERY instance is imported, using the max of
#    the high-water marks each import reported
igsn-service seed-counters --dry-run

# 4. learn real publication status from DataCite, not from Girder
igsn-service sync-status --tenant htmdec-girder --dry-run

# 5. pre-flight; exits non-zero if any counter is behind its records
igsn-service verify
```

Step 3 is the one that must not be wrong: a counter below reality is how you
mint a duplicate. Compute the marks across *all* instances before writing any of
them. `verify` is the gate — do not point a Girder instance at this service
until it passes.

Girder-side rollout is behind the `jsonforms.igsn_service_url` setting: unset
keeps the existing local behavior untouched, set switches that instance to
remote allocation.
