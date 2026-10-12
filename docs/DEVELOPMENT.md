# Development

Use Node 24, Python 3.12, and PostgreSQL 17. Lockfiles pin dependencies.
AI remains OFF. All seeded people and accounts are fictional.

```sh
make demo
```

Setup installs dependencies, applies migrations, seeds 5,000 people and eight
identities, provisions separate SQL roles, builds Next.js, and starts five apps.
Open http://127.0.0.1:3017. Generated passwords stay in ignored, mode-0600
`.local/demo-credentials.json`.

On macOS, setup finds Homebrew PostgreSQL. On Linux, set `MERIDIAN_PG_BIN` to the
folder containing `initdb` and `pg_ctl`. Ports: PostgreSQL 54329, identity 4017,
API 8017, web 3017. Servers bind to loopback.

```sh
make test
make analytics
npx playwright install chromium firefox webkit
npm run e2e
```

`make test` runs lint, strict types, PostgreSQL API tests, identity adapter tests,
and Vitest. Analytics uses real DuckDB/dbt and source reconciliation. Browsers
use the running demo and generated credentials.

```sh
.venv/bin/python scripts/gates.py --phase release-check --browsers
.venv/bin/python scripts/worker_kill_drill.py
```

The runner stops on failure and records observed results. The crash drill uses
a new isolated database, actual SIGKILL after object write, and idempotent retry.
Browser fault helpers control project-owned local processes only.

## CI

Each pull request change runs one workflow. Main runs the full workflow after a
merge. Static checks and dependency security run first, so a lint or dependency
failure does not start the expensive suites.

| Suite | Scope |
| --- | --- |
| Checks | Python and TypeScript checks, frontend units, dependencies, history scan |
| Domain | Financial engines, domain APIs, authorization, replay, migrations, identity, worker crash recovery |
| Analytics | PostgreSQL exports, real DuckDB/dbt, drift repair, storage transport and crash recovery |
| Browsers | All workflows on Chromium, Firefox and WebKit, each with a fresh database |
| Native setup | The documented clean-clone command and real OIDC browser flow |

The two backend suites include every discovered test module exactly once.
They keep separate PostgreSQL services. Each browser also gets a separate
stack and retains one Playwright worker; fault tests cannot interfere with a
second browser. There are no automatic retries. The final `verify` check fails
if any required suite fails, is cancelled, or does not run. Browser evidence
must include every declared case without skips or retries.

Python reports its twenty slowest cases. Browser case timings and bounded
synthetic artifacts show where time goes. `make analytics` remains an
independent local check; CI uses the numerical pipeline tests as its business
analytics gate rather than running that same source reconciliation again.

`make down` retains data. `make demo-reset` deletes the owned synthetic state;
use it only when that reset is intended. `make fixtures` generates the defect
feed and correction. Fresh seeds use varied fictional names; existing immutable
versions retain their original names and hashes.

```sh
make compose-demo
docker compose --env-file .local/compose.env -f infra/compose.yaml stop
```

Compose is an alternative local stack with private volumes and separate users.
[Hosted configuration](DEPLOYMENT_READINESS.md) uses managed server secrets.
Runtime services must never receive operator credentials. Apply migrations with
`scripts.operator_db` and reapply `scripts.provision_db` after schema changes.
The browser must never query Meridian business tables through Supabase.
