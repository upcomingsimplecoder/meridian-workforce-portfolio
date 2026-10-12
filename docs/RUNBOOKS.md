# Runbooks

Use fictional data only. AI is OFF. Operator credentials stay outside runtime
services. Hosted recovery is UNVERIFIED until it is observed against real services.

## Local start and stop

`make demo` performs setup and starts the owned runtime. `make down` retains
state. Restart with `.venv/bin/python scripts/local.py up`. Do not reset retained
acceptance data just to clear a failure.

## Pending or failed jobs

Inspect Operations and the Data Quality or review detail. Pending work stays in
PostgreSQL. Restore the failed worker or storage connection. Consumers retry
with bounded backoff; after dead-letter, use the authorized retry action with a
reason. Do not edit event payloads, delete receipts, or unlock awards to repair
an export. A post-write/pre-ack retry must reuse identical verified bytes.

For local crash verification, run
`.venv/bin/python scripts/worker_kill_drill.py`. It creates an isolated database,
kills an actual worker after writing export bytes, retries, and checks one
artifact, one receipt, a valid signature, and unchanged monetary totals.

## Storage corruption or exposure

Stop affected downloads and investigate privately. The adapter rejects public
or unbounded bucket settings and hash mismatches. Restore the object under its
original hash from a verified private backup, then retry the pending event.
Do not enable a public bucket, bypass integrity checks, or re-sign changed bytes
as historical evidence. Storage keys belong only in server secret stores.

## Analytics drift

Analytics uses DuckDB/dbt over frozen exports. A mismatched run must not publish.
Inspect count, sum, hash, and delivery comparisons. Restore the consumer, then
use the authorized repair action with the original pins. Approval and source
history must not be rewritten to make analytical totals match.

## Database reconnect and migrations

The API checks live schema readiness; pooled connections pre-ping. A database
outage must produce a bounded unavailable response. Restore PostgreSQL and
verify recovery plus pending work. Migrations and grants run as operator only.
Check with `.venv/bin/python -m scripts.operator_db check`, then reapply the
explicit runtime policy after a schema change. Never alter immutable rows or
disable their triggers to repair state.

## Release and rollback

Deploy a tested main commit and retain its SHA, provider deployment IDs, schema
revision, and acceptance results. Roll back application code only when compatible
with the live schema. Rehearse backup/restore and compare pinned replay outputs
before claiming recovery. Local encrypted restore tooling does not establish
hosted PITR or offsite durability.

Public publication uses `scripts/public_release.py` and a new source-only Git
history. Run Gitleaks on the private archive and the new public history. Keep raw
traces, logs, machine paths, credentials, and forensic screenshots private.
