# Workforce history and reporting scope

Status: implemented and verified locally.

A published snapshot owns a sealed set of workforce and organization versions.
Effective intervals are start-inclusive and end-exclusive. PostgreSQL rejects
overlaps and changes to published versions. A correction creates another
snapshot with its own source hash and recording time. It cannot change the
facts used by an earlier plan.

This copies history between snapshots. For the 5,000-person demo, that cost buys
simple reconstruction and predictable permissions. A larger system could share
immutable version references, provided publication and replay keep the same
contract.

Managers are bound to workforce identities on the server. A recursive SQL query
derives their reporting scope for the requested snapshot and effective date.
Each historical row must satisfy its own reporting graph. Capability grants are
checked separately: broad workforce access cannot enlarge a narrower pay or
export grant.

Pages have a 100-row limit and retain their snapshot/date boundary. Executive
queries expose a fixed aggregate surface; any department cohort below five
suppresses the entire breakdown to avoid residual subtraction. This is a
bounded portfolio privacy control, not a claim of universal anonymity.
