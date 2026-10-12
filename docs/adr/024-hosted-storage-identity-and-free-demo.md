# Hosted storage, identity and the free demo

Status: hosted services are deployed; end-to-end hosted acceptance is not complete.

The transactional schema stays in PostgreSQL. Hosted artifacts use a private
Supabase bucket; local development keeps the filesystem adapter. FastAPI checks
the current principal and scope before reading any artifact. Links expire and
remain bound to a principal, purpose and immutable version. The browser receives
neither a service key nor an unrestricted Storage URL.

Objects use their SHA-256 as their key. Every read checks that hash. Writes use
`x-upsert: false`, then verify the stored bytes before acknowledgement. A lost
write acknowledgement or process crash can leave an unreferenced object. The
outbox retry safely reuses identical bytes; a conflicting or corrupt object
fails. Uploads cap at 8 MiB and stored artifacts at 32 MiB. Metadata and download
streams have byte and time limits. A public or unbounded bucket is rejected.

The synthetic OIDC service runs separately with an exact HTTPS issuer and one
registered callback. Hosted configuration requires secure cookies, PKCE,
persistent signing and cookie keys, and a dedicated identity database account
with TLS certificate validation. API sessions still resolve current database
grants on each request. No role selection exists in the browser. The operator
generates a different password for each fictional account. This is demo
authentication; it makes no enterprise MFA claim.

Render's free plan has no background-worker service. The smallest free demo
option runs the API, outbox consumer and analytics consumer as separate supervised
processes in one web container. They use the existing worker entry points. Each
child receives only its own database account; no operator or identity key reaches
those processes. A required child's exit stops the service and its subprocesses.
Render can restart the container, then the persisted outbox resumes. The API
never substitutes a synchronous or fake worker action.

This option shares memory and availability. It is unsuitable for an always-on
service: the free service can sleep, workspace hours are shared, and a large dbt
run can exhaust container memory. Container files are temporary; PostgreSQL and
private storage are durable. The services are deployed, but hosted memory,
restart, cold-start and workflow recovery remain unverified. The two workers
remain independently runnable for a separately approved paid topology. No paid
resource has been created.

Costs and migration: this introduces no business-schema change. New Supabase
provisioning must apply the existing Alembic migrations, seed fictional identities
through an operator-only task, and apply explicit API/worker/identity grants.
Supabase browser roles receive no business-table or function privileges. RLS
allows the trusted SQL runtime roles only; object and field checks remain in
FastAPI. Existing private audit history is preserved.

Validation: storage contract tests and persisted PostgreSQL workflows cover
duplicate writes, hash mismatch, byte limits, lost acknowledgements, export
retry, current-grant denial and real DuckDB/dbt repair. Process tests exercise
actual child termination and forced shutdown. These tests do not establish
Supabase, Render or Vercel deployment success.
