"""Request/response models.

``metadata`` is intentionally a free-form dict rather than a typed model: it is
DataCite kernel-4 metadata, validated against the official JSON Schema
(``app/static/datacite-v4.5.json``) rather than re-modelled in pydantic. Girder
already validates against the same file, so a record accepted here is a record
Girder would accept.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RecordStatus = Literal["reserved", "registered", "findable", "revoked", "failed"]
PublishTarget = Literal["registered", "findable"]


class ExternalRef(BaseModel):
    """Back-pointer to the record's origin, e.g. a Girder deposition."""

    system: str | None = None
    instance: str | None = None
    id: str | None = None

    model_config = ConfigDict(extra="allow")


class AllocateRequest(BaseModel):
    prefix: str = Field(min_length=6, max_length=6)
    count: int = Field(default=1, ge=1, le=1000)
    metadata: dict[str, Any] | None = None
    landing_page: str | None = None
    external_ref: ExternalRef | None = None


class ChildrenRequest(BaseModel):
    """Register children of an existing IGSN.

    Either explicit ``indices`` (the caller computed the labels -- Girder's
    lab-specific batch strategies produce things like ``S1R2C3``) or ``count``,
    in which case the service picks the next free zero-padded numeric indices.
    """

    indices: list[str] | None = None
    count: int | None = Field(default=None, ge=1, le=1000)
    metadata: dict[str, Any] | None = None
    local_identifiers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.indices) == bool(self.count):
            raise ValueError("Provide exactly one of 'indices' or 'count'")
        return self


class RecordUpdate(BaseModel):
    metadata: dict[str, Any] | None = None
    landing_page: str | None = None
    external_ref: ExternalRef | None = None


class PublishRequest(BaseModel):
    target: PublishTarget = "findable"
    recurse: bool = False


class DataCiteState(BaseModel):
    last_sync: datetime | None = None
    last_error: str | None = None
    # What the last submission actually sent -- see AttemptOut.last_request.
    last_request: dict[str, Any] | None = None
    attempts: int = 0


class RecordOut(BaseModel):
    igsn: str
    doi: str
    prefix: str
    parent_igsn: str | None = None
    tenant: str
    status: RecordStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    landing_page: str | None = None
    resolver_url: str | None = None
    external_ref: dict[str, Any] | None = None
    created: datetime | None = None
    updated: datetime | None = None
    published_at: datetime | None = None
    datacite: DataCiteState = Field(default_factory=DataCiteState)


class AllocateResponse(BaseModel):
    records: list[RecordOut]


class PublishResponse(BaseModel):
    attempts: list[str]
    queued: int


class AttemptOut(BaseModel):
    """One publication attempt, with what was sent and what came back."""

    id: str
    igsn: str
    target: str
    status: str
    attempts: int = 0
    last_error: str | None = None
    # Summary of the request that produced last_error. DataCite's validation
    # errors name a field but never echo its value, so this is what makes a 422
    # actionable.
    last_request: dict[str, Any] | None = None
    next_attempt_at: datetime | None = None
    created: datetime | None = None
    updated: datetime | None = None


class VocabulariesOut(BaseModel):
    institutions: dict[str, Any]
    materials: dict[str, Any]
    version: int = 1


class DataCiteConfig(BaseModel):
    repository_id: str
    doi_prefix: str
    api_base: str = "https://api.datacite.org"
    password_env: str


class TenantIn(BaseModel):
    slug: str
    name: str
    allowed_institutions: list[str] = Field(default_factory=list)
    datacite: DataCiteConfig
    landing_template: str | None = None
    active: bool = True


class TenantOut(BaseModel):
    slug: str
    name: str
    allowed_institutions: list[str]
    datacite: DataCiteConfig
    landing_template: str | None = None
    active: bool
    token: str | None = None  # only returned once, at creation/rotation


class WhoAmI(BaseModel):
    slug: str
    name: str
    allowed_institutions: list[str]
    doi_prefix: str
