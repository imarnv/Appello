"""End-to-end: sign two tenants up through the API, give each agents and usage,
and prove neither can reach the other. Cleans up after itself."""
import asyncio, base64, json, os, sys, uuid
BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
os.chdir(BRIDGE)
from dotenv import load_dotenv; load_dotenv(os.path.join(BRIDGE, ".env"))
import logging; logging.basicConfig(level=logging.ERROR)

import httpx  # asyncpg pools are bound to the loop that created them, so the
              # client must share this loop rather than spawn its own thread.
from fastapi import FastAPI
from postgres_store import PostgresStore
from tenant_store import TenantStore
import tenant_context, tenant_routes

db = PostgresStore()
ts = TenantStore(lambda: db.pool)
FAILS = []

def check(label, cond, detail=""):
    print(("✓ " if cond else "✗ ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)

def hdr(email, role=None):
    """A Firebase-shaped unsigned token — exactly what the bridge accepts today."""
    payload = {"email": email}
    if role:
        payload["role"] = role
    b = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer hdr.{b}.sig"}

async def main():
    await db.connect()
    tenant_context.init(ts)
    tenant_routes.init(ts, db)
    app = FastAPI(); app.include_router(tenant_routes.router)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    stamp = uuid.uuid4().hex[:8]
    a_mail, b_mail = f"admin@acme-{stamp}.test", f"admin@globex-{stamp}.test"
    created = []
    try:
        # ── Signup ─────────────────────────────────────────────────────
        ra = await c.post("/tenants/register", json={"name": f"Acme {stamp}", "admin_email": a_mail})
        rb = await c.post("/tenants/register", json={"name": f"Globex {stamp}", "admin_email": b_mail})
        check("two tenants register", ra.status_code == 201 and rb.status_code == 201,
              f"{ra.status_code}/{rb.status_code} {ra.text[:160]}")
        if ra.status_code != 201:
            return
        A, B = ra.json()["tenant"]["id"], rb.json()["tenant"]["id"]
        created = [A, B]
        check("tenants get distinct ids", A != B)
        check("slugs are derived and unique",
              ra.json()["tenant"]["slug"] != rb.json()["tenant"]["slug"])

        dup = await c.post("/tenants/register", json={"name": "Dup", "admin_email": a_mail})
        check("duplicate admin email rejected", dup.status_code == 409, str(dup.status_code))

        # ── Token → tenant resolution ──────────────────────────────────
        me = await c.get("/tenants/me", headers=hdr(a_mail))
        check("email in token resolves to its own tenant",
              me.status_code == 200 and me.json()["tenant"]["id"] == A, me.text[:160])

        # ── One tenant, many agents ────────────────────────────────────
        for n in ("Support Bot", "Sales Bot", "Booking Bot"):
            r = await c.post("/agents", headers=hdr(a_mail),
                             json={"display_name": n, "scenario_key": "fsecure_support",
                                   "status": "deployed", "voice": "Charon"})
            check(f"tenant A deploys '{n}'", r.status_code == 201, r.text[:160])
        rb2 = await c.post("/agents", headers=hdr(b_mail),
                           json={"display_name": "Globex Only", "scenario_key": "restaurant_booking"})
        check("tenant B deploys its own agent", rb2.status_code == 201, rb2.text[:160])
        b_agent = rb2.json()["agent"]["id"]

        la = (await c.get("/agents", headers=hdr(a_mail))).json()["agents"]
        lb = (await c.get("/agents", headers=hdr(b_mail))).json()["agents"]
        check("one tenant holds many agents", len(la) == 3, f"got {len(la)}")
        check("tenant A cannot see tenant B's agent",
              all(x["display_name"] != "Globex Only" for x in la),
              str([x["display_name"] for x in la]))
        check("tenant B sees only its own", len(lb) == 1, str([x["display_name"] for x in lb]))

        # ── Cross-tenant attempts ──────────────────────────────────────
        check("A fetching B's agent by id → 404",
              (await c.get(f"/agents/{b_agent}", headers=hdr(a_mail))).status_code == 404)
        check("A editing B's agent → 404",
              (await c.patch(f"/agents/{b_agent}", headers=hdr(a_mail),
                             json={"display_name": "pwned"})).status_code == 404)
        check("A deleting B's agent → 404",
              (await c.delete(f"/agents/{b_agent}", headers=hdr(a_mail))).status_code == 404)
        still = await c.get(f"/agents/{b_agent}", headers=hdr(b_mail))
        check("B's agent survived unmodified",
              still.status_code == 200 and still.json()["agent"]["display_name"] == "Globex Only")

        bad = await c.post("/agents", headers=hdr(a_mail),
                           json={"display_name": "X", "scenario_key": "nope"})
        check("unknown scenario_key rejected", bad.status_code == 400, str(bad.status_code))

        # ── Usage recorded per tenant ──────────────────────────────────
        for i in range(3):
            await ts.record_usage(tenant_id=A, event_type="call_ended", session_id=f"s-a-{stamp}-{i}",
                                  agent_id=la[0]["id"], channel="web", scenario_key="fsecure_support",
                                  provider="gemini", duration_seconds=60, total_tokens=1000)
        await ts.record_usage(tenant_id=B, event_type="call_ended", session_id=f"s-b-{stamp}",
                              agent_id=b_agent, channel="exotel", scenario_key="restaurant_booking",
                              provider="gemini", duration_seconds=999, total_tokens=77)

        ua = (await c.get("/usage/summary", headers=hdr(a_mail))).json()
        ub = (await c.get("/usage/summary", headers=hdr(b_mail))).json()
        check("tenant A usage counts only its own",
              ua["calls"] == 3 and ua["duration_seconds"] == 180 and ua["total_tokens"] == 3000, str(ua))
        check("tenant B usage counts only its own",
              ub["calls"] == 1 and ub["duration_seconds"] == 999 and ub["total_tokens"] == 77, str(ub))

        ba = (await c.get("/usage/by-agent", headers=hdr(a_mail))).json()["agents"]
        check("per-agent breakdown attributes correctly",
              len(ba) == 1 and ba[0]["duration_seconds"] == 180, str(ba))
        da = (await c.get("/usage/daily", headers=hdr(a_mail))).json()["days"]
        check("daily series returns data", len(da) >= 1, str(da))

        # ── Platform admin ─────────────────────────────────────────────
        check("non-admin blocked from cross-tenant usage",
              (await c.get("/platform/usage", headers=hdr(a_mail))).status_code == 403)
        adm = await c.get("/platform/usage", headers=hdr("root@appello.test", role="platform_admin"))
        check("platform admin sees the rollup", adm.status_code == 200, adm.text[:160])
        if adm.status_code == 200:
            rows = {r["tenant_id"]: r for r in adm.json()["tenants"]}
            check("rollup keeps tenants separate",
                  rows.get(A, {}).get("duration_seconds") == 180
                  and rows.get(B, {}).get("duration_seconds") == 999)
    except Exception:
        import traceback; traceback.print_exc()
        FAILS.append("exception")
    finally:
        if created:
            async with db.acquire_admin() as conn:
                await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[]);", created)
            print(f"\n🧹 removed {len(created)} test tenants (cascade)")
        await c.aclose()
        await db.close()

    print("\nRESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
    sys.exit(0 if not FAILS else 1)

asyncio.run(main())
