"""Service-account authentication.

Deliberately coarse: one bearer token per tenant (a Girder instance or an
external project), not per user. Authorization is limited to (a) which
institution codes a tenant may mint under and (b) tenant-scoping every read.
Fine-grained ACLs stay in Girder, which is the only thing that has users.

Tokens are stored only as SHA-256; the plaintext is returned once, at creation
or rotation, and cannot be recovered afterwards.
"""

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status

from .config import get_settings
from .db import TENANTS, get_db

TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


async def current_tenant(authorization: str | None = Header(default=None)) -> dict:
    """Resolve the calling tenant, or 401."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    tenant = await get_db()[TENANTS].find_one({"token_sha256": hash_token(token)})
    if tenant is None or not tenant.get("active", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive service account",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tenant


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Gate the admin API on a separately-configured token.

    An unset admin token disables the admin API rather than leaving it open.
    """
    configured = get_settings().admin_token
    token = _bearer(authorization)
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin API is disabled (IGSN_ADMIN_TOKEN is not set)",
        )
    if not token or not secrets.compare_digest(token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )


TenantDep = Depends(current_tenant)
AdminDep = Depends(require_admin)
