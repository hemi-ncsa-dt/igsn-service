from fastapi import APIRouter

from . import admin, identifiers, records, vocabularies

router = APIRouter(prefix="/api/v1")
router.include_router(identifiers.router)
router.include_router(records.router)
router.include_router(vocabularies.router)
router.include_router(admin.router)
