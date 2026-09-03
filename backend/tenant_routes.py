"""
Appello Tenant API
───────────────────
HTTP surface for tenant registration, membership, agent management and usage
reporting. Mounted by main.py alongside the existing routers.

Route groups:
  /tenants   — registration and tenant administration
  /agents    — the agents a tenant has deployed (one tenant, many agents)
  /usage     — per-tenant usage, which is what billing reads
  /platform  — cross-tenant views, for platform admins only

Every tenant-facing route resolves its tenant through tenant_context, which reads
the bearer token. Nothing takes a tenant id from the request body — a caller
cannot name a tenant it does not belong to unless the testing override is on.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import tenant_context
from tenancy import DEFAULT_TENANT_ID, TENANT_ROLES

logger = logging.getLogger("appello")

router = APIRouter()

_tenant_store = None
_db_store = None


def init(tenant_store, db_store=None):
    global _tenant_store, _db_store
    _tenant_store = tenant_store
    _db_store = db_store


# Scenario templates an agent may be built from. The first three come from
# scenarios.py; the rest are branches implemented directly in the Gemini
# pipeline, so they are valid targets even though they have no template file.
PIPELINE_SCENARIOS = {
    "payment_followup",
    "ggs_support",
    "fsecure_support",
}


def _valid_scenarios() -> set:
    try:
        from scenarios import SCENARIOS

        return set(SCENARIOS.keys()) | PIPELINE_SCENARIOS
    except Exception:
        return set(PIPELINE_SCENARIOS)


def _require_store():
    if _tenant_store is None or _tenant_store.pool is None:
        raise HTTPException(503, "Tenant store unavailable — database not connected")


async def _ctx(request: Request) -> Dict[str, Any]:
    return await tenant_context.resolve_tenant_context(request)


def _require_admin(ctx: Dict[str, Any]):
    """Platform-admin gate for cross-tenant routes.

    Reads the role claim, which is unverified while the token bypass is in place —
    so this is a guard against accident, not against a determined caller. It
    becomes a real boundary the moment token verification is switched on in
    tenant_context.decode_token_claims.
    """
    claims = ctx.get("claims") or {}
    if claims.get("role") != "platform_admin":
        raise HTTPException(403, "Platform admin role required")


# ─── Schemas ────────────────────────────────────────────────────────────

class TenantRegistration(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    admin_email: str
    plan: str = "trial"
    slug: Optional[str] = None
    external_org_id: Optional[str] = None
    external_uid: Optional[str] = None
    billing_email: Optional[str] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    plan: Optional[str] = None
    status: Optional[str] = None
    billing_email: Optional[str] = None
    external_org_id: Optional[str] = None


class MemberCreate(BaseModel):
    email: str
    role: str = "customer_user"
    external_uid: Optional[str] = None


class AgentCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    scenario_key: str
    agent_key: Optional[str] = None
    voice: Optional[str] = None
    language: Optional[str] = None
    greeting: Optional[str] = None
    system_prompt: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    status: str = "draft"


class AgentUpdate(BaseModel):
    display_name: Optional[str] = None
    scenario_key: Optional[str] = None
    status: Optional[str] = None
    voice: Optional[str] = None
    language: Optional[str] = None
    greeting: Optional[str] = None
    system_prompt: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


# ─── Tenants ────────────────────────────────────────────────────────────

@router.post("/tenants/register", status_code=201)
async def register_tenant(body: TenantRegistration):
    """Sign up a new tenant along with its first admin user.

    Open by design so the dashboard can drive self-serve signup. The caller
    supplies the admin email; an email that already belongs to a tenant is
    rejected, which is what keeps email→tenant resolution unambiguous.
    """
    _require_store()
    try:
        tenant = await _tenant_store.register_tenant(
            name=body.name,
            admin_email=body.admin_email,
            plan=body.plan,
            slug=body.slug,
            external_org_id=body.external_org_id,
            external_uid=body.external_uid,
            billing_email=body.billing_email,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.error(f"[tenants] Registration failed: {e}")
        raise HTTPException(500, "Registration failed")
    return {"tenant": tenant}


@router.get("/tenants/me")
async def current_tenant(request: Request):
    """The tenant, role and identity behind the current bearer token."""
    _require_store()
    ctx = await _ctx(request)
    tenant = await _tenant_store.get_tenant(ctx["tenant_id"])
    return {
        "tenant": tenant,
        "email": ctx["email"],
        "role": ctx["role"],
        "is_default_tenant": ctx["tenant_id"] == DEFAULT_TENANT_ID,
    }


@router.get("/tenants")
async def list_tenants(request: Request, limit: int = 100):
    _require_store()
    _require_admin(await _ctx(request))
    return {"tenants": await _tenant_store.list_tenants(limit=limit)}


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, body: TenantUpdate, request: Request):
    """Update a tenant. A tenant admin may edit only its own tenant."""
    _require_store()
    ctx = await _ctx(request)
    claims = ctx.get("claims") or {}
    if claims.get("role") != "platform_admin" and tenant_id != ctx["tenant_id"]:
        raise HTTPException(403, "Cannot modify another tenant")
    updated = await _tenant_store.update_tenant(
        tenant_id, body.model_dump(exclude_none=True)
    )
    if not updated:
        raise HTTPException(404, "Tenant not found")
    return {"tenant": updated}


# ─── Members ────────────────────────────────────────────────────────────

@router.get("/tenants/me/members")
async def list_members(request: Request):
    _require_store()
    ctx = await _ctx(request)
    return {"members": await _tenant_store.list_members(ctx["tenant_id"])}


@router.post("/tenants/me/members", status_code=201)
async def add_member(body: MemberCreate, request: Request):
    _require_store()
    ctx = await _ctx(request)
    if ctx["role"] not in ("customer_admin",) and (
        (ctx.get("claims") or {}).get("role") != "platform_admin"
    ):
        raise HTTPException(403, "Tenant admin role required")
    if body.role not in TENANT_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(TENANT_ROLES)}")
    try:
        member = await _tenant_store.add_member(
            ctx["tenant_id"], body.email, body.role, body.external_uid
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"member": member}


# ─── Agents ─────────────────────────────────────────────────────────────

@router.get("/agents")
async def list_agents(request: Request, status: Optional[str] = None):
    """Every agent this tenant has, optionally filtered by status."""
    _require_store()
    ctx = await _ctx(request)
    agents = await _tenant_store.list_agents(ctx["tenant_id"], status=status)
    return {"agents": agents, "tenant_id": ctx["tenant_id"]}


@router.get("/agents/scenarios")
async def list_scenarios():
    """Scenario templates a new agent can be built from."""
    return {"scenarios": sorted(_valid_scenarios())}


@router.post("/agents", status_code=201)
async def create_agent(body: AgentCreate, request: Request):
    _require_store()
    ctx = await _ctx(request)
    valid = _valid_scenarios()
    if body.scenario_key not in valid:
        raise HTTPException(
            400, f"scenario_key must be one of {sorted(valid)}"
        )
    if body.status not in ("draft", "deployed", "disabled"):
        raise HTTPException(400, "status must be draft, deployed or disabled")
    try:
        agent = await _tenant_store.create_agent(
            tenant_id=ctx["tenant_id"],
            display_name=body.display_name,
            scenario_key=body.scenario_key,
            agent_key=body.agent_key,
            voice=body.voice,
            language=body.language,
            greeting=body.greeting,
            system_prompt=body.system_prompt,
            config=body.config,
            status=body.status,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"agent": agent}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    _require_store()
    ctx = await _ctx(request)
    agent = await _tenant_store.get_agent(ctx["tenant_id"], agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return {"agent": agent}


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate, request: Request):
    _require_store()
    ctx = await _ctx(request)
    fields = body.model_dump(exclude_none=True)
    if "scenario_key" in fields and fields["scenario_key"] not in _valid_scenarios():
        raise HTTPException(400, "Unknown scenario_key")
    if "status" in fields and fields["status"] not in (
        "draft",
        "deployed",
        "disabled",
    ):
        raise HTTPException(400, "status must be draft, deployed or disabled")
    agent = await _tenant_store.update_agent(ctx["tenant_id"], agent_id, fields)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return {"agent": agent}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    _require_store()
    ctx = await _ctx(request)
    if not await _tenant_store.delete_agent(ctx["tenant_id"], agent_id):
        raise HTTPException(404, "Agent not found")
    return {"deleted": agent_id}


# ─── Usage ──────────────────────────────────────────────────────────────

@router.get("/usage/summary")
async def usage_summary(request: Request, days: int = 30):
    """Totals for the calling tenant over a trailing window."""
    _require_store()
    ctx = await _ctx(request)
    return await _tenant_store.usage_summary(ctx["tenant_id"], days=days)


@router.get("/usage/by-agent")
async def usage_by_agent(request: Request, days: int = 30):
    _require_store()
    ctx = await _ctx(request)
    return {"agents": await _tenant_store.usage_by_agent(ctx["tenant_id"], days=days)}


@router.get("/usage/daily")
async def usage_daily(request: Request, days: int = 30):
    _require_store()
    ctx = await _ctx(request)
    return {"days": await _tenant_store.usage_daily(ctx["tenant_id"], days=days)}


@router.get("/platform/usage")
async def platform_usage(request: Request, days: int = 30):
    """Usage for every tenant — the billing rollup."""
    _require_store()
    _require_admin(await _ctx(request))
    return {"tenants": await _tenant_store.platform_usage(days=days)}
