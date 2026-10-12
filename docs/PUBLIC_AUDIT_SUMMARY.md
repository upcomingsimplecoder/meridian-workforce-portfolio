# Verification notes

## Release revision and CI

PR #1 was merged to `main` with regular merge commit
`76c10ebeb7d7170cc47d36bca8bfe8d763f123fc`. Its product jobs passed 807
Python tests, 18 PostgreSQL identity tests, 60 frontend tests, 117 browser
cases across Chromium, Firefox and WebKit, and 12 OIDC/demo setup checks.
DuckDB/dbt passed one model and five data tests. The worker-kill drill recovered
one artifact and one receipt after a consumer stopped between the object write
and database acknowledgement.

GitHub did not start the final aggregate job because its billing check blocked
runner startup. Product tests passed, but the main workflow is not green. The
local gate evaluation passed against the downloaded run evidence. It does not
change the GitHub workflow result.

## Hosted status

Vercel serves the production frontend at
[meridian-workforce.vercel.app](https://meridian-workforce.vercel.app).
Render API and synthetic OIDC services are live from the same main revision.
The API readiness, OIDC discovery, and JWKS endpoints returned HTTP 200. A
read-only database check found fresh outbox and analytics heartbeats. That
confirms both worker processes were alive during the check, but does not verify
workflow recovery. Render auto-deploy waits for passing GitHub checks.

Supabase project `recdxudyiqjpdmcqtgtg` holds the transactional database and
private `meridian-artifacts` bucket. The database has separate API, worker,
and identity roles. The bucket has a 32 MiB limit and no browser object
policies. The free Render configuration runs the outbox/export and analytics
consumers as supervised processes beside the API. DuckDB/dbt is the hosted
demo's configured analytics engine.

These checks confirm service reachability and configuration. They do not
confirm authenticated hosted workflows, worker recovery or manual browser
acceptance. Those gates have not been run yet.

## Scope

AI is OFF. All demo people, organizations, pay and decisions are synthetic.
Snowflake is not required and has not been verified live. The application does
not claim enterprise MFA, compliance certification, penetration testing,
payroll integration, or production scale.
