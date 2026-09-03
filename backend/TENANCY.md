# Multi-tenancy in the Appello bridge

One tenant owns many agents. Everything lives in the Postgres database the
bridge already uses — no new datastore, no database per tenant.

## The isolation model

Shared tables, a `tenant_id` on every tenant-owned row, and Postgres Row-Level
Security enforcing the boundary. Queries run inside `tenant_scope`, which sets a
transaction-local `app.tenant_id` GUC that the policies read.

```python
async with db_store.acquire(tenant_id) as conn:
    rows = await conn.fetch("SELECT * FROM calls;")   # only this tenant's calls
```

Two properties make this safe:

- **It fails closed.** With no tenant set, `current_setting` yields NULL, the
  policy comparison is NULL, and the query returns nothing. A forgotten filter
  produces an empty result, not a leak.
- **INSERTs need no changes.** Each `tenant_id` column defaults to the same GUC
  the policy checks, so pre-existing INSERT statements — none of which mention
  tenancy — write into whichever tenant is open.

`tenant_store.py` additionally carries an explicit `WHERE tenant_id = $n` on
every query. That redundancy is deliberate; see the warning below.

## ⚠️ RLS does nothing unless you fix the database role

**Any role with `SUPERUSER` or `BYPASSRLS` ignores every policy**, and managed
Postgres hands you exactly such a role: Neon's `neondb_owner` has
`rolbypassrls = true`. `FORCE ROW LEVEL SECURITY` does not help — it closes the
*table owner* loophole, not this one.

So while `DATABASE_URL` points at the owner role, the policies exist, the
queries run, and **nothing is filtered**. The bridge logs a warning at every
startup while this is the case.

To fix it:

```bash
cd bridge
python scripts/provision_tenant_role.py --password '<a-strong-password>'
```

That creates `appello_app` (no superuser, no BYPASSRLS), grants it what the
bridge needs, verifies from a real connection that RLS applies to it, and prints
the `DATABASE_URL` to switch to. Keep the owner URL in `ADMIN_DATABASE_URL` for
migrations.

Until then, isolation rests on the explicit `WHERE tenant_id` predicates in
`tenant_store.py`, which is why they are there.

## Schema

| Table | Purpose |
|---|---|
| `tenants` | The tenant registry. `external_org_id` links to the dashboard's Firestore `organizations/{orgId}`. |
| `tenant_users` | Membership. An email maps to exactly one tenant, which is what makes token→tenant resolution unambiguous. |
| `agents` | A tenant's deployed agents. Points at a scenario template and layers on voice, language, greeting and prompt. |
| `usage_events` | Append-only usage ledger. One row per billable event; rollups computed on read. |

Eleven pre-existing tables gained a `tenant_id`, backfilled onto the default
tenant (`00000000-…-0001`), so all historic data is intact and readable.

Two uniqueness constraints were re-scoped per tenant, because they would
otherwise collide across tenants: `leads.phone_number`, and
`availability_slots (service_type, date, time_slot)`. The `session_id` keys stay
globally unique so a session can be resolved without knowing its tenant.

## Resolving the tenant on a request

`tenant_context.resolve_tenant_id` tries, in order: an `X-Tenant-Id` header or
`?tenant_id=` param (accepts a uuid or a slug); a `tenant_id` token claim; an
`orgId` claim matched against `tenants.external_org_id`; the Firebase uid; the
token email; and finally the default tenant.

WebSockets use `resolve_tenant_id_ws`, since a browser cannot set headers on a
WebSocket — the token and tenant travel as query params instead:

```
wss://<host>/ws/voice-gemini?tenant_id=<uuid-or-slug>&agent_id=<key>&token=<jwt>
```

The agent may also be named in the `config` message the client already sends.

### ⚠️ Bearer tokens are NOT verified

`tenant_context.decode_token_claims` decodes the JWT payload without checking
its signature, matching the behaviour already in `api_routes`. Anyone can forge
a token and claim any tenant. **This is deliberate, so the tenancy work can be
tested without a Firebase service account in the loop.**

To close it: add `firebase-admin` to `requirements.txt` and replace that one
function's body with `firebase_admin.auth.verify_id_token`. Every caller goes
through it, so nothing else changes. Then set
`ALLOW_TENANT_HEADER_OVERRIDE=false`.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/tenants/register` | Signup: creates a tenant and its first admin. |
| GET | `/tenants/me` | Tenant, role and identity behind the token. |
| GET | `/tenants` | Platform admin only. |
| PATCH | `/tenants/{id}` | Own tenant, or platform admin. |
| GET/POST | `/tenants/me/members` | Membership. |
| GET/POST | `/agents` | List and deploy. |
| GET | `/agents/scenarios` | Templates a new agent can be built from. |
| GET/PATCH/DELETE | `/agents/{id}` | By uuid or tenant-scoped `agent_key`. |
| GET | `/usage/summary`, `/usage/by-agent`, `/usage/daily` | This tenant's usage. |
| GET | `/platform/usage` | Cross-tenant billing rollup. Platform admin only. |

## Usage recording

Both live voice paths in `test_realtime_gemini.py` record to `usage_events`: a
`call_started` when the socket opens, and a `call_ended` carrying the billable
duration when it closes. `record_usage` swallows its own errors — usage
accounting must never be why a call drops.

Token-level usage from the Azure path in `main.py` is still only tracked
in-memory and is not yet written to the ledger; wiring it up is a matter of
calling `record_usage(event_type="tokens", …)` where `token_usage_per_turn` is
appended.

## Tests

See `tests/README.md`. `tests/test_tenant_rls.py` proves the policies actually
filter, by provisioning the app role and switching to it inside a transaction it
then rolls back.
