# Verification notes

## Source and CI

The deployed release is private archive merge
`76c10ebeb7d7170cc47d36bca8bfe8d763f123fc`. The curated public source is based
on that release. Its [main workflow](https://github.com/upcomingsimplecoder/meridian-workforce-portfolio/actions/runs/38257301139)
passed all required jobs at `b0aed945bfcd6f454e7c4923bc448b9df6b4a260`.

That run passed 757 domain and 50 analytics tests, 18 PostgreSQL identity
tests, 60 frontend tests, and 117 browser cases: 39 each on Chromium,
Firefox, and WebKit. The clean demo and OIDC check passed 12 cases. DuckDB/dbt
passed one model and five data tests. The actual worker-kill drill recovered
one artifact and one receipt after termination between object write and
database acknowledgement. Migration, reconnect, restore, financial-oracle,
and reconciliation checks also passed.

The private archive's main run is separately blocked: GitHub refused to start
its final job because of the account payment or spending-limit check. Its
product jobs passed. That blocked run is not represented as a green workflow.

## Hosted observations

[Meridian](https://meridian-workforce.vercel.app), the API, and the separate
HTTPS synthetic identity service are live from the deployed release. A dedicated
Supabase project contains PostgreSQL and the private `meridian-artifacts`
bucket. API, worker, and identity database roles are separate. The bucket has
a 32 MiB limit and no browser object policies. On the free Render plan, the
export and analytics consumers are supervised processes beside the API.

| Workflow | Recorded observation | Still required |
| --- | --- | --- |
| Ingestion | 4,960 accepted and 40 quarantined rows; correction published 5,000 rows; SQL confirmed the snapshot | Retained browser evidence and complete hosted negatives |
| Planning | Two branches; leap-day difference USD 140,482.76; Finance submission and HR return persisted | Revision, resubmission, final approval, and concurrency checks |
| Compensation | HR configured and launched one pinned review cycle | Manager proposal, exception, return/revision, signatures, lock, export |
| Talent | Local and CI workflows passed | Hosted calibration, revisions, signatures, and lock |
| Analytics | Local and CI DuckDB/dbt reconciliation and repair passed | Hosted run, drift repair, outage and recovery |
| Decision Replay | Two hosted MATCH attempts reproduced the same USD 140,482.76 result and expected hash | Auditor/privacy negatives and retained download evidence |

The observed browser sessions displayed screenshots but did not retain image
files or traces. Complete hosted authorization, private-artifact grant rechecks,
worker restart, analytics outage, database reconnect, fresh manual review, and
the critical keyboard-only flow are incomplete. Automated local browser tests
do not substitute for those checks.

## Scope

AI is OFF. All people, organizations, pay, and decisions are synthetic.
DuckDB/dbt is the analytical implementation. Snowflake is not required or
verified live. There is no employer affiliation, compliance certification,
enterprise MFA, penetration-test, payroll integration, or production-scale claim.

The public repository excludes the original blueprint, raw traces, local logs,
and forensic history. Those materials remain in the private engineering archive.
