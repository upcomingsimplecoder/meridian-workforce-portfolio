# Deployment and verification

## Hosted layout

The production frontend is [Meridian](https://meridian-workforce.vercel.app),
deployed from main commit
`76c10ebeb7d7170cc47d36bca8bfe8d763f123fc`. The API is
[meridian-api](https://meridian-api-icc5.onrender.com/ready), and the separate
synthetic OIDC issuer is
[meridian-identity](https://meridian-identity-3g3y.onrender.com/.well-known/openid-configuration).
Both Render services run on the free plan and are configured to deploy only
after GitHub checks pass.

Supabase project `recdxudyiqjpdmcqtgtg` hosts PostgreSQL and the private
`meridian-artifacts` bucket. API, worker, identity, and operator roles are
separate. Browser roles cannot query business tables or download stored
artifacts directly.

The free Render plan does not provide separate background-worker services.
The outbox/export and analytics consumers run as supervised processes beside
the API. They use separate runtime database accounts, but share the API
container's memory and availability. DuckDB/dbt is the hosted analytics engine.
Snowflake is not required and is not live.

## Checks completed

The Vercel production deployment reports Ready. The public homepage, API
readiness endpoint, OIDC discovery endpoint, and JWKS endpoint returned HTTP
200 in the latest release check. The outbox and analytics heartbeat rows were
updated within seconds of a read-only database check. This confirms both hosted
worker processes were alive at that moment; it does not verify workflow recovery.
The OIDC issuer and endpoints use the hosted HTTPS identity service.

Supabase reached Alembic schema 0011. The three application database accounts
were checked over verified TLS. The private bucket has a 32 MiB object limit,
and its write, checksum, duplicate-write, and corruption checks passed. The
main workflow's product jobs passed 807 Python tests, 18 PostgreSQL identity
tests, 60 frontend tests, 117 browser cases across three engines, 12 OIDC/demo
setup checks, DuckDB/dbt tests, and the worker-kill recovery drill.

## Open release gates

GitHub did not start the main workflow's final aggregate verification job
because the account billing check blocked runner startup. Main CI is not green.
No payment setting was changed and no test gate was bypassed.

Endpoint health does not establish the hosted workflows. Ingestion, planning,
compensation, talent, analytics drift repair, Decision Replay, worker restart,
database reconnect, and current-grant artifact denial still need a complete
hosted acceptance run. A fresh reviewer must also complete the critical browser
journey and keyboard-only flow. These results remain NOT-TESTED.

The services use generated synthetic accounts. Passwords are not published as
shared demo credentials. AI is OFF. No paid resource, enterprise MFA, always-on
availability, compliance certification, or real payroll integration is claimed.

See the [reviewer guide](REVIEWER_GUIDE.md), [system design](SYSTEM_DESIGN_CASE_STUDY.md),
and [audit summary](PUBLIC_AUDIT_SUMMARY.md) for product behavior and recorded
test boundaries.
