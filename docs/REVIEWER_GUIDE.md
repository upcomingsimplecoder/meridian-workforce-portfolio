# Reviewer guide

[Open the hosted demo](https://meridian-workforce.vercel.app). It runs from
main commit `76c10ebeb7d7170cc47d36bca8bfe8d763f123fc`. The sign-in page is
live, but passwords for generated synthetic accounts are not published. Use
the local walkthrough below to explore the full workflows. Hosted acceptance
has not yet been completed.

## Run the local demo

Run `make demo`, open http://127.0.0.1:3017, and read the generated passwords
from the ignored, mode-0600 `.local/demo-credentials.json` file. Sign out
between personas. The server controls each account's role and data scope.

| Account | Access |
| --- | --- |
| steward | Imports, quarantine, corrections, and publication |
| hr | Workforce, planning review, and review-cycle controls |
| finance | Planning, financial sign-off, and governed exports |
| manager-a | Research reporting scope |
| manager-b | Operations reporting scope |
| approver | Independent Research division review |
| executive | Governed aggregates only |
| auditor | Redacted decisions, provenance, and evidence |

## Six workflows

1. **Ingestion.** As steward, import `fixtures/workforce/quarantine-40.csv`.
   Expect 4,960 accepted and 40 quarantined rows. Inspect redacted errors.
   Upload the linked `corrected.csv`, then publish 5,000 valid rows once.
   Earlier quarantine and snapshots remain available.
2. **Planning.** As Finance, choose the June snapshot and branch two scenarios.
   Model a February 29, 2028 hire with USD 120,000 pay, 1 FTE, and 8% bonus.
   The verified plan totals USD 777,147,041.76 at baseline and
   USD 777,287,524.52 as planned. Submit it to a separate HR reviewer with a
   reason, then inspect the reviewed revision before approval or return.
3. **Compensation.** HR launches a cycle pinned to its source and guideline.
   `manager-a` proposes awards, triggers a guideline exception, and submits.
   The approver returns a proposal with a reason; the manager revises and
   resubmits. Division, HR, and Finance sign the current version. HR locks and
   exports it. The three-award example charges USD 15,200.00.
4. **Talent.** HR creates a cycle. `manager-a` records qualitative
   observations; the approver calibrates with a reason. Complete acknowledgement
   and HR signature, lock, and download. Reopening retains the earlier revision.
5. **Analytics.** Request a run from pinned source, scenario, and cycle
   versions. Wait for DuckDB/dbt completion and inspect 32 comparisons. Simulate
   a dropped delivery, observe drift, and repair from the same pins.
6. **Replay.** Open a financial delta. Inspect inputs, FX, policy, event hashes,
   and human decisions. Start a replay and compare exact outputs. As auditor,
   inspect the redacted view. As executive, confirm raw replay is denied.

## Access and keyboard checks

As `manager-b`, request a Research object and export. Expect denial. Check
self-approval, stale revisions, concurrent edits, expired downloads, grant
revocation, and locked awards. The local test and browser suites cover these
cases against persisted state; they are not hosted acceptance.

A fresh manual review must repeat critical workflows through the hosted browser.
Complete the critical flow using only Tab, Shift+Tab, Enter, Space, Escape, and
arrow keys. Check visible focus, table scrolling, validation, dialog focus
return, and saved server state. Automated browser tests do not substitute for
that review.

See the [audit summary](PUBLIC_AUDIT_SUMMARY.md) and
[deployment record](DEPLOYMENT_READINESS.md) for current verification.
