"""Dry-run the tenancy migration against the live DB, then roll it back.

Proves the DDL applies and that RLS actually isolates, without persisting
anything.
"""
import asyncio, os, sys
BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
os.chdir(BRIDGE)
from dotenv import load_dotenv
load_dotenv(os.path.join(BRIDGE, ".env"))
import asyncpg
from tenancy import initialize_tenancy, provision_app_role, role_bypasses_rls, DEFAULT_TENANT_ID, TENANT_GUC

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"

async def scope(conn, tid):
    await conn.execute("SELECT set_config($1,$2,true);", TENANT_GUC, tid)

async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
    tx = conn.transaction()
    await tx.start()
    ok = True
    try:
        print("→ running initialize_tenancy …")
        await initialize_tenancy(conn)
        print("  migration applied")
        await provision_app_role(conn, "test-only-password")
        # SET ROLE reproduces exactly what connecting as the app role would do,
        # while staying inside the transaction we are going to roll back.
        # PG16+ gives the creator ADMIN but not SET on a role it made, so the
        # test has to grant itself SET before it can impersonate.
        await conn.execute(
            "GRANT appello_app TO current_user WITH SET TRUE, INHERIT FALSE;")
        await conn.execute("SET ROLE appello_app;")
        print("  now acting as:", await conn.fetchval("SELECT current_user;"),
              "| bypasses RLS:", await role_bypasses_rls(conn), "\n")

        await conn.execute("SELECT set_config('app.bypass_rls','on',true);")
        for tid, slug in ((A, "acme-test"), (B, "globex-test")):
            await conn.execute(
                "INSERT INTO tenants (id,slug,name) VALUES ($1::uuid,$2,$3) "
                "ON CONFLICT DO NOTHING;", tid, slug, slug)
        await conn.execute("SELECT set_config('app.bypass_rls','',true);")

        # Insert one call per tenant, relying on the GUC-driven column DEFAULT —
        # the INSERT never mentions tenant_id, exactly like the existing code.
        await scope(conn, A)
        await conn.execute(
            "INSERT INTO calls (session_id, phone_number, scenario) "
            "VALUES ('rls-test-a','+100','x') ON CONFLICT DO NOTHING;")
        await scope(conn, B)
        await conn.execute(
            "INSERT INTO calls (session_id, phone_number, scenario) "
            "VALUES ('rls-test-b','+200','x') ON CONFLICT DO NOTHING;")

        # 1. Default lands rows in the right tenant.
        await conn.execute("SELECT set_config('app.bypass_rls','on',true);")
        got = dict(await conn.fetchrow(
            "SELECT (SELECT tenant_id FROM calls WHERE session_id='rls-test-a') a,"
            "       (SELECT tenant_id FROM calls WHERE session_id='rls-test-b') b;"))
        assert str(got["a"]) == A and str(got["b"]) == B, got
        print("✓ GUC-driven column DEFAULT routes INSERTs to the open tenant")
        await conn.execute("SELECT set_config('app.bypass_rls','',true);")

        # 2. A cannot see B's rows.
        await scope(conn, A)
        rows = [r["session_id"] for r in await conn.fetch(
            "SELECT session_id FROM calls WHERE session_id LIKE 'rls-test-%';")]
        assert rows == ["rls-test-a"], rows
        print("✓ tenant A reads only its own rows:", rows)

        await scope(conn, B)
        rows = [r["session_id"] for r in await conn.fetch(
            "SELECT session_id FROM calls WHERE session_id LIKE 'rls-test-%';")]
        assert rows == ["rls-test-b"], rows
        print("✓ tenant B reads only its own rows:", rows)

        # 3. A cannot UPDATE or DELETE across the boundary.
        await scope(conn, A)
        res = await conn.execute(
            "UPDATE calls SET summary='pwned' WHERE session_id='rls-test-b';")
        assert res.endswith(" 0"), res
        res = await conn.execute("DELETE FROM calls WHERE session_id='rls-test-b';")
        assert res.endswith(" 0"), res
        print("✓ cross-tenant UPDATE and DELETE affect 0 rows")

        # 4. Forging a tenant_id on INSERT is rejected by WITH CHECK.
        # Wrapped in a savepoint: the rejection aborts its subtransaction, and
        # without one the rest of the suite could not run.
        sp = conn.transaction()
        await sp.start()
        try:
            await conn.execute(
                "INSERT INTO calls (session_id,phone_number,scenario,tenant_id) "
                "VALUES ('rls-test-forge','+300','x',$1::uuid);", B)
            await sp.commit()
            print("✗ FAIL: forged cross-tenant INSERT was allowed")
            ok = False
        except asyncpg.exceptions.InsufficientPrivilegeError:
            await sp.rollback()
            print("✓ forged cross-tenant INSERT rejected by WITH CHECK")

        # 5. No GUC at all → fail closed.
        await conn.execute("SELECT set_config($1,'',true);", TENANT_GUC)
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM calls WHERE session_id LIKE 'rls-test-%';")
        assert n == 0, n
        print("✓ un-scoped query sees 0 rows (fails closed)")

        # 6. Legacy rows are readable under the default tenant.
        await scope(conn, DEFAULT_TENANT_ID)
        n = await conn.fetchval("SELECT COUNT(*) FROM calls;")
        print(f"✓ default tenant sees {n} backfilled legacy call rows")

        # 7. Tenant-scoped uniqueness: both tenants can hold the same phone.
        await scope(conn, A)
        await conn.execute("INSERT INTO leads (phone_number,name) VALUES ('9990001111','A');")
        await scope(conn, B)
        await conn.execute("INSERT INTO leads (phone_number,name) VALUES ('9990001111','B');")
        print("✓ two tenants can hold the same lead phone number")

    except Exception as e:
        ok = False
        print(f"\n✗ FAILED: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
    finally:
        await tx.rollback()
        await conn.close()
        print("\n↩ rolled back — database unchanged")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

asyncio.run(main())
