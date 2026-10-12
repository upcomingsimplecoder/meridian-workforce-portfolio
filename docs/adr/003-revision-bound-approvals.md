# Approvals bind to immutable revisions

Status: implemented and verified locally.

Every scenario, compensation proposal, and talent packet edit appends a revision.
An approval records the exact revision, reviewer, reason, and current permission
evidence. Returning work requires a new revision before resubmission. Earlier
signatures remain historical evidence and cannot authorize the revised result.

Scenario owners cannot approve their own plans. Compensation routes separate
manager proposal, exception review, division approval, and HR/Finance sign-off.
Talent routes separate author, calibrator, and HR signer. Frozen assignments
never replace a current grant check.

Row locks and expected versions serialize edits, signatures, and finalization.
Database guards enforce legal transitions and reject locked edits. Idempotency
receipts replay only after current object authority is rechecked.

Compensation budget includes every current draft reservation. Exceeding the total
budget is a hard error. Consuming holdback within that budget requires an explicit
Finance release; it does not invent additional funds. Revised amounts invalidate
decisions tied to stale totals.

A locked talent review can be reopened with a reason into a new revision chain.
The earlier lock and export retain their exact bytes. Qualitative narratives have
no score, rank, automated recommendation, or AI-selected employee outcome.
