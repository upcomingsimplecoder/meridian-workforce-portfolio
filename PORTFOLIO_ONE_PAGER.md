# Meridian

Meridian helps teams plan workforce costs and review compensation while keeping
the source data, assumptions, revisions, and approvals behind each result.

[Live demo](https://meridian-workforce.vercel.app) ·
[README](README.md) ·
[Architecture](docs/SYSTEM_DESIGN_CASE_STUDY.md) ·
[Demo guide](docs/REVIEWER_GUIDE.md)

## Why it matters

The hard question is whether a decision can be explained later using the same
facts and rules that were available when it was made. Effective dates differ
from correction time. Reviewers may return a proposal and receive a new
revision. A financial result must preserve those distinctions.

## A verified example

A February 29, 2028 hire produces a twelve-month plan of USD 777,287,524.52,
compared with a USD 777,147,041.76 baseline. The difference is USD 140,482.76.
Decision Replay reconstructs it from the pinned workforce snapshot, dates, hire
operation, currency rules, and human decisions.

## How it works

A steward imports a workforce feed, corrects quarantined rows, and publishes
an immutable snapshot. Finance branches scenarios and compares calculated
costs. Managers and reviewers complete versioned compensation and talent
cycles. DuckDB/dbt checks derived exports against source totals.

The browser calls Next.js and FastAPI. PostgreSQL owns transactional state.
Workers consume a transactional outbox at least once and record idempotent
receipts. Private artifacts use content hashes, and current authorization is
checked on download.

## Deployment and verification

The frontend, API, and synthetic OIDC issuer are live from main commit
`76c10ebeb7d7170cc47d36bca8bfe8d763f123fc`. Supabase provides PostgreSQL and
private artifact storage. The free Render setup runs two consumers beside the
API. Hosted journeys and manual keyboard review are not yet verified.

The public main workflow passed 826 Python tests, 18 PostgreSQL identity tests, 60
frontend tests, 117 browser cases across Chromium, Firefox, and WebKit, 12
OIDC/demo setup checks, DuckDB/dbt tests, and a worker-kill recovery drill.
See the
[audit summary](docs/PUBLIC_AUDIT_SUMMARY.md) for the hosted acceptance boundary.

AI is OFF. DuckDB/dbt powers the hosted analytics configuration. Snowflake is
not required or claimed as live.

## About

Meridian is an independent portfolio project. It is not affiliated with any
employer. All people, organizations, compensation figures, and workflows in
the demo are synthetic. No real
employee or employer data is connected.
