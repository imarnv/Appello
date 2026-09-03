"""
Appello Tenancy Layer
──────────────────────
Multi-tenant primitives that live in the *same* Postgres database as the rest of
the bridge (no new datastore, no per-tenant database).

Isolation model: a single set of shared tables, every tenant-owned row carrying a
``tenant_id``, with Postgres Row-Level Security enforcing the boundary. Queries
run inside :func:`tenant_scope`, which sets a transaction-local ``app.tenant_id``
GUC that the RLS policies read. A query that forgets to filter by tenant returns
nothing rather than another tenant's rows — the boundary fails closed.

The policies use FORCE ROW LEVEL SECURITY because the bridge normally connects as
the table owner (Neon hands you an owner role), and owners bypass ordinary RLS.

One tenant owns many agents. An agent is a row in ``agents`` that points at a
scenario template from scenarios.py and layers tenant-specific overrides on top
(display name, voice, language, greeting, system prompt).
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger("appello")

# ─── Configuration ──────────────────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Turning this off leaves the tenant_id columns and data in place but stops
# Postgres enforcing the boundary. Useful when debugging a migration; never in
# production.
RLS_ENFORCED = _env_flag("TENANT_RLS_ENFORCED", True)

# Every row that existed before tenancy was introduced belongs to this tenant, and
# any code path that has not yet been threaded with a tenant falls back to it. The
# UUID is fixed so the backfill is reproducible across environments.
DEFAULT_TENANT_ID = os.getenv(
    "DEFAULT_TENANT_ID", "00000000-0000-0000-0000-000000000001"
)
DEFAULT_TENANT_SLUG = os.getenv("DEFAULT_TENANT_SLUG", "default")
DEFAULT_TENANT_NAME = os.getenv("DEFAULT_TENANT_NAME", "Default Workspace")

# The GUC the RLS policies read.
TENANT_GUC = "app.tenant_id"

# ── The application role ────────────────────────────────────────────────
# RLS is bypassed entirely by any role holding BYPASSRLS, and managed Postgres
# providers hand you exactly such a role by default (Neon's `neondb_owner` has
# rolbypassrls = true). FORCE ROW LEVEL SECURITY does not help — it closes the
# *owner* loophole, not the BYPASSRLS one.
#
# So the bridge must connect as a role that has neither attribute. This role owns
# no tables and cannot bypass anything; it just reads and writes rows, subject to
# the policies. Provision it with provision_app_role(), then point DATABASE_URL at
# it. The privileged URL stays in ADMIN_DATABASE_URL for migrations.
APP_DB_ROLE = os.getenv("APP_DB_ROLE", "appello_app")

# Tables that gain a tenant_id and come under RLS. Order matters only for
# readability — the migration is idempotent.
# Pre-tenancy tables: these need a tenant_id added and backfilled. They get a
# column DEFAULT so legacy INSERTs that predate tenancy keep working, landing on
# the default tenant rather than failing.
LEGACY_TENANT_TABLES: List[str] = [
    "leads",
    "calls",
    "transcripts",
    "availability_slots",
    "bookings",
    "knowledge_files",
    "restaurant_reservations",
    "restaurant_pre_orders",
    "restaurant_booking_logs",
    "feedback_agent_logs",
    "reminder_contacts",
]

# Born tenant-aware. No column DEFAULT on purpose: an INSERT that omits tenant_id
# here is a bug and should fail loudly.
NATIVE_TENANT_TABLES: List[str] = [
    "tenant_users",
    "agents",
    "usage_events",
]

# Everything RLS applies to.
TENANT_SCOPED_TABLES: List[str] = LEGACY_TENANT_TABLES + NATIVE_TENANT_TABLES

# Roles a member of a tenant can hold. Mirrors the roles the dashboard already
# derives in the dashboard's rbac module so the two agree.
TENANT_ROLES = ("customer_admin", "customer_user")


class TenantResolutionError(Exception):
    """Raised when a request cannot be attributed to a tenant."""


def is_valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def slugify(value: str) -> str:
    """Lowercase, hyphen-separated, safe for use as a tenant or agent key."""
    cleaned = []
    prev_dash = False
    for ch in (value or "").strip().lower():
        if ch.isalnum():
            cleaned.append(ch)
            prev_dash = False
        elif not prev_dash:
            cleaned.append("-")
            prev_dash = True
    slug = "".join(cleaned).strip("-")
    return slug[:63] or "tenant"


# ─── Connection scoping ─────────────────────────────────────────────────

@asynccontextmanager
async def tenant_scope(pool, tenant_id: Optional[str] = None):
    """Acquire a connection bound to one tenant for the life of a transaction.

    ``set_config(..., is_local => true)`` scopes the GUC to the transaction, so a
    connection returned to the pool never carries one tenant's identity into the
    next request. Everything inside the block therefore runs under exactly one
    tenant's RLS policies.
    """
    resolved = str(tenant_id or DEFAULT_TENANT_ID)
    if not is_valid_uuid(resolved):
        raise TenantResolutionError(f"Invalid tenant_id: {resolved!r}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config($1, $2, true);", TENANT_GUC, resolved
            )
            yield conn


@asynccontextmanager
async def admin_scope(pool):
    """Acquire a connection that can see across tenants.

    Only for platform-level work: the migration itself, cross-tenant billing
    rollups, and the tenant/user lookups that must happen *before* a tenant is
    known (signup, login). Never reachable from a tenant-facing route.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.bypass_rls', 'on', true);"
            )
            yield conn


# ─── Schema ─────────────────────────────────────────────────────────────

TENANCY_DDL = """
CREATE TABLE IF NOT EXISTS tenants (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug             VARCHAR(63) UNIQUE NOT NULL,
    name             VARCHAR(200) NOT NULL,
    plan             VARCHAR(32)  NOT NULL DEFAULT 'trial',
    status           VARCHAR(20)  NOT NULL DEFAULT 'active',
    -- Links this tenant to the Firestore organizations/{orgId} document the
    -- dashboard already writes, so the two systems can be reconciled.
    external_org_id  VARCHAR(128) UNIQUE,
    billing_email    VARCHAR(255),
    settings         JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tenant_users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email         VARCHAR(255) NOT NULL,
    -- Firebase Auth uid, when the account was created through the dashboard.
    external_uid  VARCHAR(128),
    role          VARCHAR(32) NOT NULL DEFAULT 'customer_admin',
    status        VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- An email resolves to exactly one tenant, which is what makes JWT-based tenant
-- resolution unambiguous. Revisit if a user ever needs to belong to two tenants.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_users_email_key
    ON tenant_users (lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS tenant_users_external_uid_key
    ON tenant_users (external_uid) WHERE external_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS tenant_users_tenant_idx ON tenant_users (tenant_id);

CREATE TABLE IF NOT EXISTS agents (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Tenant-scoped slug used in the ?agent= query param on the voice socket.
    agent_key      VARCHAR(64) NOT NULL,
    display_name   VARCHAR(200) NOT NULL,
    -- Template this agent is built from; must be a key in scenarios.SCENARIOS.
    scenario_key   VARCHAR(64) NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'draft',
    voice          VARCHAR(64),
    language       VARCHAR(32),
    greeting       TEXT,
    -- Full prompt override. NULL means "use the scenario template as-is".
    system_prompt  TEXT,
    config         JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, agent_key)
);

CREATE INDEX IF NOT EXISTS agents_tenant_status_idx ON agents (tenant_id, status);

-- Append-only usage ledger. One row per billable event; rollups are computed on
-- read so a correction never has to rewrite history.
CREATE TABLE IF NOT EXISTS usage_events (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id            UUID REFERENCES agents(id) ON DELETE SET NULL,
    session_id          VARCHAR(100),
    -- call_started | call_ended | tokens | tts | stt
    event_type          VARCHAR(32) NOT NULL,
    -- web | exotel | measurement
    channel             VARCHAR(20),
    scenario_key        VARCHAR(64),
    provider            VARCHAR(32),
    model               VARCHAR(64),
    duration_seconds    INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    input_audio_tokens  INTEGER NOT NULL DEFAULT 0,
    output_audio_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens       INTEGER NOT NULL DEFAULT 0,
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB,
    occurred_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS usage_events_tenant_time_idx
    ON usage_events (tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_tenant_agent_idx
    ON usage_events (tenant_id, agent_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_session_idx ON usage_events (session_id);
"""


async def _ensure_tenant_column(conn, table: str):
    """Add tenant_id to a pre-tenancy table, backfill it, then constrain it.

    Split into three steps because the column has to exist and be populated
    before NOT NULL can be applied to a table that already holds rows.
    """
    exists = await conn.fetchval(
        "SELECT to_regclass($1) IS NOT NULL;", f"public.{table}"
    )
    if not exists:
        logger.debug(f"[tenancy] Table {table} does not exist yet; skipping.")
        return

    await conn.execute(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id UUID;"
    )
    # Backfill pre-tenancy rows onto the default tenant.
    await conn.execute(
        f"UPDATE {table} SET tenant_id = $1::uuid WHERE tenant_id IS NULL;",
        DEFAULT_TENANT_ID,
    )
    # The DEFAULT reads the same GUC the RLS policy checks. That means every
    # pre-existing INSERT statement in the codebase — none of which mention
    # tenant_id — automatically writes into whichever tenant the surrounding
    # `tenant_scope` opened, and still satisfies the policy's WITH CHECK. It is
    # what makes this migration possible without rewriting ~30 query sites.
    await conn.execute(
        f"""
        ALTER TABLE {table} ALTER COLUMN tenant_id SET DEFAULT COALESCE(
            NULLIF(current_setting('{TENANT_GUC}', true), '')::uuid,
            '{DEFAULT_TENANT_ID}'::uuid
        );
        """
    )
    await conn.execute(f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL;")

    # The FK is added separately so a pre-existing constraint doesn't abort the
    # migration (Postgres has no ADD CONSTRAINT IF NOT EXISTS).
    await conn.execute(
        f"""
        DO $$
        BEGIN
            ALTER TABLE {table}
                ADD CONSTRAINT {table}_tenant_id_fkey
                FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
            WHEN duplicate_table THEN NULL;
        END $$;
        """
    )
    await conn.execute(
        f"CREATE INDEX IF NOT EXISTS {table}_tenant_id_idx ON {table} (tenant_id);"
    )


async def _apply_rls(conn, table: str):
    """Put one table under a fail-closed tenant policy.

    The policy admits a row when it belongs to the tenant named by the
    transaction-local GUC. With no GUC set, ``current_setting`` returns NULL, the
    comparison is NULL, and no rows qualify — an un-scoped query sees an empty
    table instead of everybody's data.

    ``app.bypass_rls`` is the escape hatch used by :func:`admin_scope` for
    platform-level work such as this migration and signup.
    """
    exists = await conn.fetchval(
        "SELECT to_regclass($1) IS NOT NULL;", f"public.{table}"
    )
    if not exists:
        return

    policy = f"{table}_tenant_isolation"
    await conn.execute(f"DROP POLICY IF EXISTS {policy} ON {table};")

    if not RLS_ENFORCED:
        await conn.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        return

    await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    # Without FORCE, the owner role the bridge connects as would bypass the
    # policy entirely and the isolation would be decorative.
    await conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    # NULLIF matters: current_setting(..., true) yields NULL only when the GUC was
    # never set at all. Once a transaction has set it to the empty string, the
    # bare cast ''::uuid raises instead of returning NULL, so the query errors out
    # rather than failing closed. NULLIF collapses both cases to NULL, which makes
    # the comparison NULL and admits no rows — the intended behaviour.
    predicate = (
        "current_setting('app.bypass_rls', true) = 'on' "
        f"OR tenant_id = NULLIF(current_setting('{TENANT_GUC}', true), '')::uuid"
    )
    await conn.execute(
        f"""
        CREATE POLICY {policy} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate});
        """
    )


# ─── Application role provisioning ──────────────────────────────────────

async def role_bypasses_rls(conn) -> bool:
    """True when the connected role can see through RLS policies."""
    row = await conn.fetchrow(
        """
        SELECT rolsuper, rolbypassrls
          FROM pg_roles WHERE rolname = current_user;
        """
    )
    return bool(row and (row["rolsuper"] or row["rolbypassrls"]))


async def provision_app_role(conn, password: str, role: str = None) -> str:
    """Create (or update) the non-privileged role the bridge connects as.

    Idempotent. Grants exactly what the bridge needs on the public schema and
    nothing more: no ownership, no BYPASSRLS, no CREATEROLE. Run this once with a
    privileged connection, then move DATABASE_URL onto the returned role.
    """
    role = role or APP_DB_ROLE
    if not password:
        raise ValueError("A password is required to provision the app role")

    exists = await conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = $1;", role
    )
    if exists:
        await conn.execute(
            f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOBYPASSRLS "
            f"NOCREATEROLE NOCREATEDB PASSWORD $${password}$$;"
        )
    else:
        await conn.execute(
            f"CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOBYPASSRLS "
            f"NOCREATEROLE NOCREATEDB PASSWORD $${password}$$;"
        )

    await conn.execute(f"GRANT USAGE ON SCHEMA public TO {role};")
    await conn.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {role};"
    )
    await conn.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role};"
    )
    # Tables created later (a new scenario table, say) are covered automatically.
    await conn.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role};"
    )
    await conn.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role};"
    )
    logger.info(f"[tenancy] Provisioned application role '{role}' (NOBYPASSRLS).")
    return role


async def _retenant_unique_constraints(conn):
    """Re-scope global UNIQUE constraints that two tenants would collide on.

    ``leads.phone_number`` and ``availability_slots (service_type, date,
    time_slot)`` were unique across the whole table. Under multi-tenancy that
    means the second tenant to add a customer on a given phone number, or to open
    the same appointment slot, gets a constraint violation caused by data they
    cannot even see. Both become unique *per tenant* instead.

    Left global on purpose: the session_id keys on calls, restaurant_booking_logs
    and feedback_agent_logs. Those are generated identifiers that must stay
    globally unique so a session id can be resolved without knowing its tenant.
    """
    rescopes = [
        ("leads", "leads_phone_number_key", "(tenant_id, phone_number)"),
        (
            "availability_slots",
            "availability_slots_service_type_date_time_slot_key",
            "(tenant_id, service_type, date, time_slot)",
        ),
    ]
    for table, old_constraint, new_cols in rescopes:
        exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL;", f"public.{table}"
        )
        if not exists:
            continue
        await conn.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old_constraint};"
        )
        await conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_tenant_unique_idx "
            f"ON {table} {new_cols};"
        )


async def initialize_tenancy(conn):
    """Create the tenancy tables, migrate existing tables, and apply RLS.

    Idempotent: safe to run on every boot. Runs inside the caller's transaction.
    """
    await conn.execute(TENANCY_DDL)

    # Bootstrap tenant must exist before anything can reference it.
    await conn.execute(
        """
        INSERT INTO tenants (id, slug, name, plan, status)
        VALUES ($1::uuid, $2, $3, 'internal', 'active')
        ON CONFLICT (id) DO NOTHING;
        """,
        DEFAULT_TENANT_ID,
        DEFAULT_TENANT_SLUG,
        DEFAULT_TENANT_NAME,
    )

    for table in LEGACY_TENANT_TABLES:
        await _ensure_tenant_column(conn, table)

    await _retenant_unique_constraints(conn)

    for table in TENANT_SCOPED_TABLES:
        await _apply_rls(conn, table)

    logger.info(
        f"[tenancy] Schema ready — {len(TENANT_SCOPED_TABLES)} tenant-scoped "
        f"tables, RLS {'enforced' if RLS_ENFORCED else 'DISABLED'}."
    )

    # A policy that is silently bypassed is worse than no policy, because it
    # looks like isolation. Say so at every boot until the role is fixed.
    if RLS_ENFORCED and await role_bypasses_rls(conn):
        current = await conn.fetchval("SELECT current_user;")
        logger.warning(
            f"[tenancy] ⚠️  RLS IS NOT IN FORCE: the bridge is connected as "
            f"'{current}', which holds SUPERUSER or BYPASSRLS and therefore "
            f"ignores every tenant policy. Tenant data is NOT isolated. "
            f"Provision the '{APP_DB_ROLE}' role and point DATABASE_URL at it "
            f"(see provision_app_role / scripts/provision_tenant_role.py)."
        )
