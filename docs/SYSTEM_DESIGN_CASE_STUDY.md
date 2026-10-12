# Meridian system design

Meridian connects workforce planning, compensation, and qualitative talent
review around one question: can a reviewer reconstruct this decision?
It is an independent synthetic portfolio project with no employer affiliation
or proprietary employer data. AI is OFF.

## Data and trust

Browser → Next.js → FastAPI → PostgreSQL. PostgreSQL owns editable state,
immutable revisions, grants, decisions, audit, and jobs. There is no browser
business-table connection.

Separate non-owner SQL accounts constrain the API, workers, and identity.
The API cannot write worker receipts; workers cannot mint sessions or human
approvals; identity cannot read workforce or web sessions. Only operator tasks
migrate and bootstrap. Supabase browser roles have no business-table privileges.
RLS admits trusted SQL roles, while FastAPI checks object and field scope.

## Versioned inputs and review

Ingestion retains bytes, quarantines errors, and seals a valid snapshot once.
Source date and effective date are separate. A late correction changes a current
interpretation while preserving an earlier published view.

Scenarios pin snapshot, policy, FX, model, and engine. Decimal calculations
handle explicit currencies, rounding, and leap-day proration. Approval references
the reviewed revision. Returned compensation proposals create new revisions and
invalidate old approvals. Budgets reserve exact annualized charges. Exceptions
and locks require independent, current signatures. Locked awards cannot change.

Talent reviews record qualitative observations and reasoned calibration without
ranking or scoring people. Reopening retains earlier locks and exports. Current
permissions govern historical evidence.

## Workers, storage, and analytics

Business mutations and outbox events commit together. Consumers validate event
pins, write evidence, and commit idempotent receipts. Retry and dead-letter state
are visible. Artifacts use SHA-256 keys and verified bytes. Hosted configuration
uses a private bounded Supabase bucket. A crash between write and SQL commit can
leave an orphan object, but retry safely reuses identical content. Corruption
fails closed. Downloads require a live session, current scope, and expiring link.

The free Render option supervises the API and two real consumers in one
container, with a separate HTTPS identity service. Child failure stops remaining
processes; pending outbox work resumes after restart. This shares memory and
availability and requires hosted validation. Workers remain independently
runnable for an approved paid topology.

DuckDB/dbt reads versioned Parquet exports. Reconciliation compares counts,
exact sums, and hashes. A dropped delivery produces drift and no publication;
repair retains the original pins. Snowflake is optional source code, not live.

Replay freezes inputs, event fingerprints, and human decisions. Each attempt
reproduces the expected output or records a visible mismatch. Governed viewer
projections withhold restricted pay and narratives. There is no chatbot.

## Evidence and limits

API tests use PostgreSQL. Browser journeys use real synthetic OIDC. Security
cases cover scope, field leaks, self-approval, stale revisions, locks, and exports.
Recovery checks include duplicate delivery, actual SIGKILL, database reconnect,
and analytics outage. Storage transport tests do not establish cloud execution.

Hosted and prescribed fresh manual/keyboard gates remain incomplete. No cloud
reliability, enterprise MFA, compliance certification, or real personnel outcome
is claimed. The private archive preserves original reports and failures.
[Audit summary](PUBLIC_AUDIT_SUMMARY.md) separates results from assumptions.
