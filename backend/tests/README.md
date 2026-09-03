# Tenancy tests

Run against the database in `bridge/.env`. Each test cleans up after itself.

```bash
cd bridge
./venv/bin/python tests/test_tenant_rls.py         # RLS isolation (rolls back; writes nothing)
./venv/bin/python tests/test_tenant_api.py         # signup, agents, usage, cross-tenant denial
./venv/bin/python tests/test_legacy_regression.py  # pre-tenancy call paths still work
```

`test_tenant_rls.py` is the one that matters most. It provisions the
`appello_app` role inside a transaction, switches to it with `SET ROLE`, and
proves the policies actually filter — then rolls the whole thing back. Run it
after any change to `tenancy.py`.

Note that it deliberately does **not** test against `neondb_owner`: that role
holds `BYPASSRLS`, so every isolation assertion would fail. If the suite starts
failing on the isolation checks, check which role `DATABASE_URL` points at
before suspecting the policies.
