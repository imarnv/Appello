"""
Appello Tenant Store
─────────────────────
Data access for tenants, their members, their deployed agents, and their usage.

Everything here runs against the same Postgres pool the rest of the bridge uses.
Reads and writes of tenant-owned rows go through ``tenant_scope`` so RLS applies;
the handful of operations that must run before a tenant is known — signup, and
resolving an email to a tenant at login — go through ``admin_scope``.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_ROLES,
    TenantResolutionError,
    admin_scope,
    slugify,
    tenant_scope,
)

logger = logging.getLogger("appello")


def _row_to_dict(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    for key, value in out.items():
        if isinstance(value, uuid.UUID):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _rows_to_list(rows) -> List[Dict[str, Any]]:
    return [_row_to_dict(r) for r in rows]


class TenantStore:
    """Tenant, membership, agent and usage operations on the shared pool.

    Every tenant-owned query carries an explicit ``tenant_id`` predicate *and*
    runs inside ``tenant_scope`` so RLS applies. The redundancy is deliberate:
    RLS is the backstop that catches a query someone forgets to filter, but it is
    silently inert whenever the connected role holds BYPASSRLS — which is the
    default on managed Postgres. The explicit predicate keeps isolation intact
    even then.
    """

    def __init__(self, pool_provider):
        # Takes a callable rather than the pool itself because PostgresStore
        # builds its pool lazily during connect(); holding the object directly
        # would capture None.
        self._pool_provider = pool_provider

    @property
    def pool(self):
        return self._pool_provider()

    # ─── Tenants ────────────────────────────────────────────────────────

    async def _unique_slug(self, conn, desired: str) -> str:
        """Find a free slug, appending -2, -3 … on collision."""
        base = slugify(desired)
        candidate = base
        suffix = 2
        while await conn.fetchval(
            "SELECT 1 FROM tenants WHERE slug = $1;", candidate
        ):
            candidate = f"{base[:58]}-{suffix}"
            suffix += 1
        return candidate

    async def register_tenant(
        self,
        name: str,
        admin_email: str,
        plan: str = "trial",
        slug: Optional[str] = None,
        external_org_id: Optional[str] = None,
        external_uid: Optional[str] = None,
        billing_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a tenant and its first admin member in one transaction.

        This is the signup entry point. Returns the new tenant with its admin
        user attached. Raises ValueError if the email already belongs to a
        tenant, since an email maps to exactly one tenant.
        """
        if not self.pool:
            raise RuntimeError("Postgres pool is not available")
        email = (admin_email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("A valid admin_email is required")
        if not (name or "").strip():
            raise ValueError("A tenant name is required")

        async with admin_scope(self.pool) as conn:
            existing = await conn.fetchrow(
                "SELECT tenant_id FROM tenant_users WHERE lower(email) = $1;", email
            )
            if existing:
                raise ValueError(f"{email} already belongs to a tenant")

            resolved_slug = await self._unique_slug(conn, slug or name)
            tenant = await conn.fetchrow(
                """
                INSERT INTO tenants (slug, name, plan, status, external_org_id,
                                     billing_email)
                VALUES ($1, $2, $3, 'active', $4, $5)
                RETURNING *;
                """,
                resolved_slug,
                name.strip(),
                plan,
                external_org_id,
                billing_email or email,
            )
            user = await conn.fetchrow(
                """
                INSERT INTO tenant_users (tenant_id, email, external_uid, role)
                VALUES ($1, $2, $3, 'customer_admin')
                RETURNING *;
                """,
                tenant["id"],
                email,
                external_uid,
            )

        result = _row_to_dict(tenant)
        result["admin_user"] = _row_to_dict(user)
        logger.info(
            f"[tenancy] Registered tenant '{result['slug']}' ({result['id']}) "
            f"for {email}"
        )
        return result

    async def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        if not self.pool:
            return None
        async with admin_scope(self.pool) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tenants WHERE id = $1::uuid;", str(tenant_id)
            )
        return _row_to_dict(row)

    async def get_tenant_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        if not self.pool:
            return None
        async with admin_scope(self.pool) as conn:
            row = await conn.fetchrow("SELECT * FROM tenants WHERE slug = $1;", slug)
        return _row_to_dict(row)

    async def list_tenants(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Platform-admin view across every tenant."""
        if not self.pool:
            return []
        async with admin_scope(self.pool) as conn:
            rows = await conn.fetch(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM tenant_users u
                         WHERE u.tenant_id = t.id) AS user_count,
                       (SELECT COUNT(*) FROM agents a
                         WHERE a.tenant_id = t.id AND a.status = 'deployed')
                           AS deployed_agent_count
                  FROM tenants t
                 ORDER BY t.created_at DESC
                 LIMIT $1;
                """,
                limit,
            )
        return _rows_to_list(rows)

    async def update_tenant(
        self, tenant_id: str, fields: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        allowed = {"name", "plan", "status", "billing_email", "external_org_id"}
        updates = {k: v for k, v in (fields or {}).items() if k in allowed}
        if not updates or not self.pool:
            return await self.get_tenant(tenant_id)

        assignments = ", ".join(
            f"{col} = ${i + 2}" for i, col in enumerate(updates.keys())
        )
        async with admin_scope(self.pool) as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE tenants SET {assignments},
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1::uuid RETURNING *;
                """,
                str(tenant_id),
                *updates.values(),
            )
        return _row_to_dict(row)

    # ─── Membership & resolution ────────────────────────────────────────

    async def resolve_tenant_for_email(self, email: str) -> Optional[str]:
        """Map a login email to its tenant id. Returns None if unknown."""
        if not self.pool or not email:
            return None
        async with admin_scope(self.pool) as conn:
            row = await conn.fetchrow(
                """
                SELECT tenant_id FROM tenant_users
                 WHERE lower(email) = $1 AND status = 'active';
                """,
                email.strip().lower(),
            )
        return str(row["tenant_id"]) if row else None

    async def resolve_tenant_for_uid(self, uid: str) -> Optional[str]:
        if not self.pool or not uid:
            return None
        async with admin_scope(self.pool) as conn:
            row = await conn.fetchrow(
                """
                SELECT tenant_id FROM tenant_users
                 WHERE external_uid = $1 AND status = 'active';
                """,
                uid,
            )
        return str(row["tenant_id"]) if row else None

    async def get_member(self, email: str) -> Optional[Dict[str, Any]]:
        if not self.pool or not email:
            return None
        async with admin_scope(self.pool) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tenant_users WHERE lower(email) = $1;",
                email.strip().lower(),
            )
        return _row_to_dict(row)

    async def add_member(
        self,
        tenant_id: str,
        email: str,
        role: str = "customer_user",
        external_uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        if role not in TENANT_ROLES:
            raise ValueError(f"role must be one of {TENANT_ROLES}")
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("A valid email is required")

        async with admin_scope(self.pool) as conn:
            existing = await conn.fetchrow(
                "SELECT tenant_id FROM tenant_users WHERE lower(email) = $1;", email
            )
            if existing:
                if str(existing["tenant_id"]) != str(tenant_id):
                    raise ValueError(f"{email} already belongs to another tenant")
                row = await conn.fetchrow(
                    """
                    UPDATE tenant_users
                       SET role = $2, external_uid = COALESCE($3, external_uid),
                           status = 'active'
                     WHERE lower(email) = $1 RETURNING *;
                    """,
                    email,
                    role,
                    external_uid,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO tenant_users (tenant_id, email, role, external_uid)
                    VALUES ($1::uuid, $2, $3, $4) RETURNING *;
                    """,
                    str(tenant_id),
                    email,
                    role,
                    external_uid,
                )
        return _row_to_dict(row)

    async def list_members(self, tenant_id: str) -> List[Dict[str, Any]]:
        if not self.pool:
            return []
        async with tenant_scope(self.pool, tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT * FROM tenant_users WHERE tenant_id = $1::uuid "
                "ORDER BY created_at ASC;",
                str(tenant_id),
            )
        return _rows_to_list(rows)

    # ─── Agents ─────────────────────────────────────────────────────────

    async def create_agent(
        self,
        tenant_id: str,
        display_name: str,
        scenario_key: str,
        agent_key: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        greeting: Optional[str] = None,
        system_prompt: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        status: str = "draft",
    ) -> Dict[str, Any]:
        """Deploy a new agent under a tenant. One tenant may hold many."""
        if not self.pool:
            raise RuntimeError("Postgres pool is not available")
        if not (display_name or "").strip():
            raise ValueError("display_name is required")
        if not (scenario_key or "").strip():
            raise ValueError("scenario_key is required")

        async with tenant_scope(self.pool, tenant_id) as conn:
            key = slugify(agent_key or display_name)
            # agent_key is unique per tenant, so a name collision inside one
            # tenant gets a numeric suffix rather than an error.
            candidate, suffix = key, 2
            while await conn.fetchval(
                "SELECT 1 FROM agents WHERE tenant_id = $1::uuid AND agent_key = $2;",
                str(tenant_id),
                candidate,
            ):
                candidate = f"{key[:58]}-{suffix}"
                suffix += 1

            row = await conn.fetchrow(
                """
                INSERT INTO agents (tenant_id, agent_key, display_name,
                                    scenario_key, status, voice, language,
                                    greeting, system_prompt, config)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                RETURNING *;
                """,
                str(tenant_id),
                candidate,
                display_name.strip(),
                scenario_key.strip(),
                status,
                voice,
                language,
                greeting,
                system_prompt,
                json.dumps(config or {}),
            )
        return _row_to_dict(row)

    async def list_agents(
        self, tenant_id: str, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if not self.pool:
            return []
        async with tenant_scope(self.pool, tenant_id) as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM agents WHERE tenant_id = $1::uuid "
                    "AND status = $2 ORDER BY created_at DESC;",
                    str(tenant_id),
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM agents WHERE tenant_id = $1::uuid "
                    "ORDER BY created_at DESC;",
                    str(tenant_id),
                )
        return _rows_to_list(rows)

    async def get_agent(
        self, tenant_id: str, agent_id: str
    ) -> Optional[Dict[str, Any]]:
        """Look an agent up by uuid or by its tenant-scoped agent_key."""
        if not self.pool or not agent_id:
            return None
        async with tenant_scope(self.pool, tenant_id) as conn:
            try:
                uuid.UUID(str(agent_id))
                row = await conn.fetchrow(
                    "SELECT * FROM agents WHERE tenant_id = $1::uuid AND id = $2::uuid;",
                    str(tenant_id),
                    str(agent_id),
                )
            except (ValueError, AttributeError, TypeError):
                row = await conn.fetchrow(
                    "SELECT * FROM agents WHERE tenant_id = $1::uuid AND agent_key = $2;",
                    str(tenant_id),
                    str(agent_id),
                )
        return _row_to_dict(row)

    async def update_agent(
        self, tenant_id: str, agent_id: str, fields: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "display_name",
            "scenario_key",
            "status",
            "voice",
            "language",
            "greeting",
            "system_prompt",
            "config",
        }
        updates = {k: v for k, v in (fields or {}).items() if k in allowed}
        if not updates or not self.pool:
            return await self.get_agent(tenant_id, agent_id)
        if "config" in updates:
            updates["config"] = json.dumps(updates["config"] or {})

        assignments = ", ".join(
            f"{col} = ${i + 2}" + ("::jsonb" if col == "config" else "")
            for i, col in enumerate(updates.keys())
        )
        # tenant_id goes in as a bind parameter, not an f-string substitution —
        # the column list is generated code, the values never are.
        tenant_ph = f"${len(updates) + 2}"
        async with tenant_scope(self.pool, tenant_id) as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE agents SET {assignments}, updated_at = CURRENT_TIMESTAMP
                 WHERE id = $1::uuid AND tenant_id = {tenant_ph}::uuid
                 RETURNING *;
                """,
                str(agent_id),
                *updates.values(),
                str(tenant_id),
            )
        return _row_to_dict(row)

    async def delete_agent(self, tenant_id: str, agent_id: str) -> bool:
        if not self.pool:
            return False
        async with tenant_scope(self.pool, tenant_id) as conn:
            result = await conn.execute(
                "DELETE FROM agents WHERE id = $1::uuid AND tenant_id = $2::uuid;",
                str(agent_id),
                str(tenant_id),
            )
        return result.endswith("1")

    # ─── Usage ──────────────────────────────────────────────────────────

    async def record_usage(
        self,
        tenant_id: str,
        event_type: str,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        channel: Optional[str] = None,
        scenario_key: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        duration_seconds: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        input_audio_tokens: int = 0,
        output_audio_tokens: int = 0,
        cached_tokens: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one billable event to the tenant's usage ledger.

        Deliberately swallows its own errors: usage accounting must never be the
        reason a live call drops.
        """
        if not self.pool:
            return
        try:
            async with tenant_scope(self.pool, tenant_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO usage_events (
                        tenant_id, agent_id, session_id, event_type, channel,
                        scenario_key, provider, model, duration_seconds,
                        input_tokens, output_tokens, total_tokens,
                        input_audio_tokens, output_audio_tokens, cached_tokens,
                        metadata)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16::jsonb);
                    """,
                    str(tenant_id),
                    str(agent_id) if agent_id else None,
                    session_id,
                    event_type,
                    channel,
                    scenario_key,
                    provider,
                    model,
                    int(duration_seconds or 0),
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(total_tokens or 0),
                    int(input_audio_tokens or 0),
                    int(output_audio_tokens or 0),
                    int(cached_tokens or 0),
                    json.dumps(metadata or {}),
                )
        except Exception as e:
            logger.error(f"[usage] Failed to record {event_type} usage: {e}")

    async def usage_summary(
        self, tenant_id: str, days: int = 30
    ) -> Dict[str, Any]:
        """Totals for one tenant over a trailing window."""
        if not self.pool:
            return {}
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with tenant_scope(self.pool, tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) FILTER (WHERE event_type = 'call_ended')
                           AS calls,
                       COALESCE(SUM(duration_seconds), 0)   AS duration_seconds,
                       COALESCE(SUM(total_tokens), 0)       AS total_tokens,
                       COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                       COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                       COALESCE(SUM(input_audio_tokens), 0) AS input_audio_tokens,
                       COALESCE(SUM(output_audio_tokens), 0) AS output_audio_tokens,
                       COUNT(DISTINCT session_id)           AS sessions
                  FROM usage_events
                 WHERE tenant_id = $2::uuid AND occurred_at >= $1;
                """,
                since,
                str(tenant_id),
            )
        summary = _row_to_dict(row) or {}
        summary["window_days"] = days
        summary["tenant_id"] = str(tenant_id)
        return summary

    async def usage_by_agent(
        self, tenant_id: str, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Per-agent breakdown, so a tenant can see which agent costs what."""
        if not self.pool:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with tenant_scope(self.pool, tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT u.agent_id,
                       COALESCE(a.display_name, u.scenario_key, 'unknown')
                           AS agent_name,
                       a.agent_key,
                       COUNT(*) FILTER (WHERE u.event_type = 'call_ended')
                           AS calls,
                       COALESCE(SUM(u.duration_seconds), 0) AS duration_seconds,
                       COALESCE(SUM(u.total_tokens), 0)     AS total_tokens
                  FROM usage_events u
                  LEFT JOIN agents a ON a.id = u.agent_id
                 WHERE u.tenant_id = $2::uuid AND u.occurred_at >= $1
                 GROUP BY u.agent_id, a.display_name, u.scenario_key, a.agent_key
                 ORDER BY duration_seconds DESC;
                """,
                since,
                str(tenant_id),
            )
        return _rows_to_list(rows)

    async def usage_daily(
        self, tenant_id: str, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Daily time series for usage charts."""
        if not self.pool:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with tenant_scope(self.pool, tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT date_trunc('day', occurred_at)::date AS day,
                       COUNT(*) FILTER (WHERE event_type = 'call_ended') AS calls,
                       COALESCE(SUM(duration_seconds), 0) AS duration_seconds,
                       COALESCE(SUM(total_tokens), 0)     AS total_tokens
                  FROM usage_events
                 WHERE tenant_id = $2::uuid AND occurred_at >= $1
                 GROUP BY day ORDER BY day ASC;
                """,
                since,
                str(tenant_id),
            )
        out = _rows_to_list(rows)
        for r in out:
            if r.get("day") is not None:
                r["day"] = str(r["day"])
        return out

    async def platform_usage(self, days: int = 30) -> List[Dict[str, Any]]:
        """Cross-tenant rollup for platform admins and billing."""
        if not self.pool:
            return []
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with admin_scope(self.pool) as conn:
            rows = await conn.fetch(
                """
                SELECT t.id AS tenant_id, t.slug, t.name, t.plan,
                       COUNT(u.*) FILTER (WHERE u.event_type = 'call_ended')
                           AS calls,
                       COALESCE(SUM(u.duration_seconds), 0) AS duration_seconds,
                       COALESCE(SUM(u.total_tokens), 0)     AS total_tokens
                  FROM tenants t
                  LEFT JOIN usage_events u
                         ON u.tenant_id = t.id AND u.occurred_at >= $1
                 GROUP BY t.id, t.slug, t.name, t.plan
                 ORDER BY duration_seconds DESC;
                """,
                since,
            )
        return _rows_to_list(rows)
