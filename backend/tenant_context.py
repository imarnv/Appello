"""
Appello Tenant Context
───────────────────────
Turns an incoming HTTP request or WebSocket connection into a tenant id.

Resolution order (first hit wins):
  1. Explicit ``X-Tenant-Id`` header or ``tenant_id`` query param — the testing
     bypass, gated on ALLOW_TENANT_HEADER_OVERRIDE.
  2. A ``tenant_id`` custom claim on the bearer token.
  3. An ``orgId`` custom claim, matched against tenants.external_org_id — this is
     what the dashboard's createCustomerAccount function already writes.
  4. The token's email, matched against tenant_users.
  5. The default tenant, so pre-tenancy clients keep working.

⚠️  SECURITY — KNOWN GAP, DELIBERATE
    Bearer tokens are decoded but *not verified*, matching the existing
    behaviour in api_routes.get_user_email_from_request. Anyone can forge a
    token and claim any tenant. This is kept on purpose so the tenancy work can
    be tested without a Firebase service account in the loop.

    To close it, add ``firebase-admin`` to requirements.txt and replace the body
    of :func:`decode_token_claims` with a call to
    ``firebase_admin.auth.verify_id_token``. Nothing else needs to change —
    every caller goes through that one function.
"""

import base64
import json
import logging
import os
from typing import Any, Dict, Optional

from tenancy import DEFAULT_TENANT_ID, is_valid_uuid

logger = logging.getLogger("appello")

# Lets a caller name its own tenant via header or query param. Required for the
# current test flow; turn it off once tokens are verified for real.
ALLOW_TENANT_HEADER_OVERRIDE = os.getenv(
    "ALLOW_TENANT_HEADER_OVERRIDE", "true"
).strip().lower() in ("1", "true", "yes", "on")

ANONYMOUS_EMAIL = "anonymous@local"

# Set by main.py at startup so resolution can hit the database.
_tenant_store = None


def init(tenant_store):
    global _tenant_store
    _tenant_store = tenant_store


def decode_token_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying its signature.

    See the module docstring — this is the single seam where real verification
    gets added later.
    """
    if not token:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return {}


def _bearer_from_headers(headers) -> str:
    auth = ""
    try:
        auth = headers.get("Authorization") or headers.get("authorization") or ""
    except Exception:
        return ""
    return auth[7:] if auth.startswith("Bearer ") else ""


def claims_from_request(request) -> Dict[str, Any]:
    return decode_token_claims(_bearer_from_headers(request.headers))


def email_from_claims(claims: Dict[str, Any]) -> str:
    return (
        claims.get("email")
        or claims.get("sub")
        or ANONYMOUS_EMAIL
    )


async def _resolve(
    claims: Dict[str, Any],
    header_tenant: Optional[str] = None,
) -> str:
    """Shared resolution used by both the HTTP and WebSocket entry points."""
    # 1. Explicit override (testing).
    if ALLOW_TENANT_HEADER_OVERRIDE and header_tenant:
        candidate = str(header_tenant).strip()
        if is_valid_uuid(candidate):
            return candidate
        # Also accept a slug, which is far easier to type by hand.
        if _tenant_store:
            tenant = await _tenant_store.get_tenant_by_slug(candidate)
            if tenant:
                return tenant["id"]
        logger.warning(f"[tenancy] Ignoring unresolvable tenant override: {candidate!r}")

    # 2. Direct tenant_id claim.
    claim_tenant = claims.get("tenant_id")
    if claim_tenant and is_valid_uuid(claim_tenant):
        return str(claim_tenant)

    if _tenant_store is None:
        return DEFAULT_TENANT_ID

    # 3. orgId claim → tenants.external_org_id (written by the dashboard).
    org_id = claims.get("orgId") or claims.get("org_id")
    if org_id:
        try:
            from tenancy import admin_scope

            pool = _tenant_store.pool
            if pool:
                async with admin_scope(pool) as conn:
                    row = await conn.fetchrow(
                        "SELECT id FROM tenants WHERE external_org_id = $1;",
                        str(org_id),
                    )
                if row:
                    return str(row["id"])
        except Exception as e:
            logger.error(f"[tenancy] orgId lookup failed for {org_id!r}: {e}")

    # 4. Firebase uid, then email.
    uid = claims.get("user_id") or claims.get("uid")
    if uid:
        try:
            resolved = await _tenant_store.resolve_tenant_for_uid(str(uid))
            if resolved:
                return resolved
        except Exception as e:
            logger.error(f"[tenancy] uid lookup failed: {e}")

    email = email_from_claims(claims)
    if email and email != ANONYMOUS_EMAIL:
        try:
            resolved = await _tenant_store.resolve_tenant_for_email(email)
            if resolved:
                return resolved
        except Exception as e:
            logger.error(f"[tenancy] email lookup failed: {e}")

    # 5. Pre-tenancy clients land here.
    return DEFAULT_TENANT_ID


async def resolve_tenant_id(request) -> str:
    """Tenant for an HTTP request. Always returns an id, never raises."""
    claims = claims_from_request(request)
    header_tenant = (
        request.headers.get("X-Tenant-Id")
        or request.headers.get("x-tenant-id")
        or request.query_params.get("tenant_id")
    )
    return await _resolve(claims, header_tenant)


async def resolve_tenant_context(request) -> Dict[str, Any]:
    """Tenant id plus the caller's identity, for routes that need both."""
    claims = claims_from_request(request)
    header_tenant = (
        request.headers.get("X-Tenant-Id")
        or request.headers.get("x-tenant-id")
        or request.query_params.get("tenant_id")
    )
    tenant_id = await _resolve(claims, header_tenant)
    email = email_from_claims(claims)
    member = None
    if _tenant_store and email != ANONYMOUS_EMAIL:
        try:
            member = await _tenant_store.get_member(email)
        except Exception:
            member = None
    return {
        "tenant_id": tenant_id,
        "email": email,
        "uid": claims.get("user_id") or claims.get("uid"),
        "role": (member or {}).get("role", "customer_admin"),
        "claims": claims,
    }


async def resolve_tenant_id_ws(ws) -> str:
    """Tenant for a WebSocket handshake.

    Browsers cannot set headers on a WebSocket, so the token and tenant arrive as
    query params here (``?tenant_id=…&token=…``). Falls back to the header path
    for server-to-server callers such as Exotel.
    """
    params = ws.query_params
    token = params.get("token") or params.get("access_token") or ""
    claims = decode_token_claims(token) if token else {}
    if not claims:
        claims = decode_token_claims(_bearer_from_headers(ws.headers))

    header_tenant = (
        params.get("tenant_id")
        or params.get("tenant")
        or ws.headers.get("x-tenant-id")
    )
    return await _resolve(claims, header_tenant)
