# Meridian interview brief

[Live demo](https://meridian-workforce.vercel.app) ·
[README](README.md) ·
[System design](docs/SYSTEM_DESIGN_CASE_STUDY.md) ·
[Reviewer guide](docs/REVIEWER_GUIDE.md)

## Introduction

Meridian plans workforce costs and runs versioned compensation and talent
reviews. Decision Replay connects a financial result to the source snapshot,
effective dates, scenario operations, FX and policy versions, event history,
and human approvals that produced it.

The February 29, 2028 hire example moves the twelve-month cost from
USD 777,147,041.76 to USD 777,287,524.52, a difference of USD 140,482.76.
The calculation uses pinned inputs and decimal arithmetic.

## Design decisions

- Source-recording time and business effective time are separate. A late
  correction does not change the version an earlier reviewer used.
- Money uses Decimal with explicit currencies, FX assumptions, and rounding.
- Returning a proposal creates a new revision and invalidates old approvals.
  Reviewers cannot approve their own proposal or edit locked awards.
- Content-addressed objects survive a crash before SQL acknowledgement. The
  outbox retries safely and verifies the existing bytes.
- Historical evidence still requires current authorization. Auditor views omit
  individual pay and review narratives.
- The browser calls Next.js and FastAPI. API, worker, and identity services use
  separate database roles. DuckDB/dbt derives and reconciles analytics.

## Evidence and limits

The three-award example charges USD 15,200.00. Drift repair restores all 32
comparisons and the original result hash. Local workflow tests, PostgreSQL
permission checks, worker termination/retry, database reconnect, restore,
financial-oracle, and reconciliation tests passed.

On the main merge, product jobs passed 807 Python tests, 18 PostgreSQL identity
tests, 60 frontend tests, 117 browser cases across three engines, and 12
OIDC/demo setup checks. DuckDB/dbt and the worker-kill drill also passed.
The public main workflow passed every required job.

Hosted services are live, but the six hosted workflows and the independent
manual and keyboard-only reviews have not been completed. AI is OFF. DuckDB/dbt
powers the hosted analytics configuration. Snowflake is not required or live.
The demo uses synthetic data only and makes no enterprise MFA, certification,
penetration-testing, payroll, or production-scale claim.

Meridian is an independent portfolio project, not affiliated with any
employer. All people, organizations, compensation
figures, and workflows are synthetic.
