"""Controlled vocabularies and prefix validation.

This is the centralized replacement for the per-instance
``jsonforms.igsn_institutions`` / ``jsonforms.igsn_materials`` settings, which
were free to drift between Girder instances.

The validation rules replicate ``PrefixCounter.validate`` in girder-jsonforms
(``girder_jsonforms/models/deposition.py:44-66``) exactly, including its
quirk that a material with no ``subcategories`` accepts only the submaterial
letter ``X``. Diverging here would reject prefixes the existing instances have
already minted under.
"""

import json
from pathlib import Path

from ..db import VOCABULARIES, get_db

SEED_PATH = Path(__file__).parent.parent / "static" / "vocabularies.json"

# Girder's implicit default for materials that declare no subcategories.
_DEFAULT_SUBCATEGORIES = {"X": "empty"}


class PrefixError(ValueError):
    """A prefix is malformed, unknown, or not permitted for this tenant."""


def load_seed() -> dict:
    with open(SEED_PATH) as fh:
        return json.load(fh)


async def get_vocabularies(db=None) -> dict:
    db = db if db is not None else get_db()
    doc = await db[VOCABULARIES].find_one({"_id": "current"})
    if doc is None:
        seed = load_seed()
        doc = {"_id": "current", "version": 1, **seed}
        await db[VOCABULARIES].update_one(
            {"_id": "current"}, {"$setOnInsert": doc}, upsert=True
        )
        doc = await db[VOCABULARIES].find_one({"_id": "current"})
    return doc


async def set_vocabularies(institutions: dict, materials: dict, db=None) -> dict:
    db = db if db is not None else get_db()
    current = await get_vocabularies(db)
    doc = {
        "institutions": institutions,
        "materials": materials,
        "version": int(current.get("version", 1)) + 1,
    }
    await db[VOCABULARIES].update_one({"_id": "current"}, {"$set": doc}, upsert=True)
    return await db[VOCABULARIES].find_one({"_id": "current"})


def validate_prefix(prefix: str, vocabularies: dict, allowed_institutions=None) -> str:
    """Validate a 6-character IGSN prefix. Returns the canonical (upper) form.

    Layout: 2-letter institution + 1-letter lab + 2-letter material + 1-letter
    submaterial, e.g. ``JHAMAL``.
    """
    if not isinstance(prefix, str) or len(prefix) != 6:
        raise PrefixError("Prefix must be 6 characters long")
    prefix = prefix.upper()
    if not prefix.isalpha():
        raise PrefixError("Prefix must be alphabetic")

    institutions = vocabularies.get("institutions", {})
    materials = vocabularies.get("materials", {})

    inst = prefix[:2]
    if inst not in institutions:
        raise PrefixError(f"Invalid institution {inst}")

    subinst = prefix[2]
    if subinst not in institutions[inst].get("labs", {}):
        raise PrefixError(f"Invalid subinstitution {subinst}")

    material = prefix[3:5]
    if material not in materials:
        raise PrefixError(f"Invalid material {material}")

    submaterial = prefix[5]
    subcategories = materials[material].get("subcategories", _DEFAULT_SUBCATEGORIES)
    if submaterial not in subcategories:
        raise PrefixError(f"Invalid submaterial {submaterial}")

    if allowed_institutions and inst not in allowed_institutions:
        raise PrefixError(f"Institution {inst} is not permitted for this tenant")

    return prefix
