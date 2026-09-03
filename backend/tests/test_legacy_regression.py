"""Regression: the pre-tenancy call paths must behave exactly as before, with
their data landing on the default tenant."""
import asyncio, os, sys, uuid
BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
os.chdir(BRIDGE)
from dotenv import load_dotenv; load_dotenv(os.path.join(BRIDGE, ".env"))
import logging; logging.basicConfig(level=logging.ERROR)
from postgres_store import PostgresStore
from tenancy import DEFAULT_TENANT_ID

FAILS=[]
def check(l,c,d=""):
    print(("✓ " if c else "✗ ")+l+(f"   [{d}]" if d and not c else "")); (None if c else FAILS.append(l))

async def main():
    db = PostgresStore(); await db.connect()
    sid = f"regress_{uuid.uuid4().hex[:10]}"
    try:
        # Exactly the pre-tenancy call signature — no tenant argument at all.
        await db.log_call_start(sid, "+919999999999", "real_estate_lead")
        await db.save_transcript_turn(sid, "user", "hello there")
        await db.save_transcript_turn(sid, "assistant", "hi, how can I help")
        await db.log_call_end(sid, "a test summary")

        async with db.acquire_admin() as c:
            row = await c.fetchrow("SELECT * FROM calls WHERE session_id=$1;", sid)
            check("legacy log_call_start still writes", row is not None)
            check("it lands on the default tenant",
                  row and str(row["tenant_id"]) == DEFAULT_TENANT_ID, str(row and row["tenant_id"]))
            check("legacy log_call_end still completes the row",
                  row and row["status"] == "completed" and row["summary"] == "a test summary")
            n = await c.fetchval("SELECT COUNT(*) FROM transcripts WHERE session_id=$1;", sid)
            check("legacy transcripts still persist", n == 2, f"got {n}")

        # Reading back through the default tenant scope sees the same rows.
        async with db.acquire() as c:
            n = await c.fetchval("SELECT COUNT(*) FROM calls WHERE session_id=$1;", sid)
            check("default-tenant scope reads them back", n == 1, f"got {n}")
            total = await c.fetchval("SELECT COUNT(*) FROM calls;")
            check("historic call history is intact", total > 800, f"{total} rows")

        # Other legacy helpers still work untouched.
        slots = await db.get_availability()
        check("get_availability() still returns", isinstance(slots, list))
        files = await db.get_kb_files("anonymous@local")
        check("get_kb_files() still returns", isinstance(files, list))
        contacts = await db.get_reminder_contacts("anonymous@local")
        check("get_reminder_contacts() still returns", isinstance(contacts, list))
        logs = await db.get_restaurant_booking_logs(limit=5)
        check("get_restaurant_booking_logs() still returns", isinstance(logs, list))
    except Exception:
        import traceback; traceback.print_exc(); FAILS.append("exception")
    finally:
        async with db.acquire_admin() as c:
            await c.execute("DELETE FROM transcripts WHERE session_id=$1;", sid)
            await c.execute("DELETE FROM calls WHERE session_id=$1;", sid)
        await db.close()
    print("\nRESULT:", "PASS" if not FAILS else f"FAIL: {FAILS}")
    sys.exit(0 if not FAILS else 1)

asyncio.run(main())
