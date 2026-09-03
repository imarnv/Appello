#!/usr/bin/env python3
"""
Provision the application database role that makes tenant isolation real.

Why this exists
───────────────
Row-Level Security is ignored entirely by any role holding SUPERUSER or
BYPASSRLS, and managed Postgres hands you exactly such a role by default —
Neon's `neondb_owner` has rolbypassrls = true. FORCE ROW LEVEL SECURITY does not
close that hole; it only closes the *table owner* one. So while the bridge
connects as the owner role, every tenant policy is decorative: the queries run,
the policies exist, and nothing is filtered.

This script creates a plain login role with neither attribute, grants it exactly
the table and sequence privileges the bridge needs, and prints the DATABASE_URL
to switch to.

Usage
─────
    # Uses ADMIN_DATABASE_URL if set, else DATABASE_URL, as the privileged
    # connection that does the provisioning.
    python scripts/provision_tenant_role.py --password 'a-strong-password'

    # Then set, in bridge/.env:
    #   DATABASE_URL=<the URL this prints>          ← what the bridge runs as
    #   ADMIN_DATABASE_URL=<the original owner URL> ← migrations only

Re-running is safe; it updates the existing role in place.
"""

import argparse
import asyncio
import os
import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from tenancy import APP_DB_ROLE, provision_app_role, role_bypasses_rls  # noqa: E402


def rewrite_url(url: str, role: str, password: str) -> str:
    """Swap the credentials in a Postgres URL, leaving host and options intact."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{quote(role)}:{quote(password)}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--password",
        help="Password for the app role. Generated if omitted.",
    )
    ap.add_argument("--role", default=APP_DB_ROLE, help=f"Default: {APP_DB_ROLE}")
    args = ap.parse_args()

    admin_url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not admin_url:
        print("✗ Set ADMIN_DATABASE_URL or DATABASE_URL first.", file=sys.stderr)
        return 1

    password = args.password or secrets.token_urlsafe(24)

    conn = await asyncpg.connect(admin_url)
    try:
        if not await role_bypasses_rls(conn):
            print(
                "✗ The connection used for provisioning is itself non-privileged.\n"
                "  Point ADMIN_DATABASE_URL at the owner role and retry.",
                file=sys.stderr,
            )
            return 1

        await provision_app_role(conn, password, args.role)
        print(f"✓ Role '{args.role}' provisioned (NOSUPERUSER, NOBYPASSRLS).")
    finally:
        await conn.close()

    # Verify from a real connection as the new role, rather than trusting the
    # grants went in. A role that still bypasses RLS is the whole failure mode
    # this script exists to prevent.
    app_url = rewrite_url(admin_url, args.role, password)
    try:
        check = await asyncpg.connect(app_url)
    except Exception as e:
        print(f"✗ Could not connect as '{args.role}': {e}", file=sys.stderr)
        return 1
    try:
        if await role_bypasses_rls(check):
            print(
                f"✗ '{args.role}' still bypasses RLS. Isolation would NOT hold.",
                file=sys.stderr,
            )
            return 1
        n = await check.fetchval("SELECT COUNT(*) FROM tenants;")
        print(f"✓ Connected as '{args.role}'; RLS applies; {n} tenants visible.")
    finally:
        await check.close()

    print("\nSet these in bridge/.env:\n")
    print(f"  DATABASE_URL={app_url}")
    print(f"  ADMIN_DATABASE_URL={admin_url}")
    print("\nRestart the bridge, and the startup RLS warning should disappear.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
