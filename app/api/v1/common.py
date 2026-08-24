"""Shared serialization helpers for the v1 API."""

from ...config import get_settings
from ...schemas import RecordOut


def serialize_record(doc: dict) -> RecordOut:
    settings = get_settings()
    data = dict(doc)
    data.pop("_id", None)
    data["resolver_url"] = f"{settings.public_url.rstrip('/')}/{doc['igsn']}"
    return RecordOut(**data)
