# Decision Replay freezes the explanation of a result

Status: implemented and verified locally.

A replay capsule captures a financial decision's exact source, effective dates,
operation history, currency and FX rules, policy/model versions, human decisions,
and audit/outbox chronology. It also fingerprints the calculator code. Capsule
documents and hashes are immutable.

Scenario verification recalculates the selected revision and its original
comparison baseline. Award verification uses frozen eligibility, terms,
guidelines, and FX. Verification appends an attempt instead of replacing a
previous result. MATCH means the reconstructed output matches its expected hash.
MISSING identifies unavailable dependencies; CORRUPT identifies failed integrity.
Unsupported models or calculator code are reported explicitly.

No newer policy, rate, snapshot, or calculator is substituted to force a match.
Historical calculator code is identified, not downloaded and executed from an
artifact. An incompatible deployment must restore the reviewed implementation
before that capsule can be verified.

Current permission checks apply to capsule reads and downloads. Auditors receive
redacted provenance without individual pay inputs or personnel narratives.
Scenario aggregates require a minimum cohort in both compared versions.

This detects damage against captured evidence. A privileged database and storage
operator with the signing key remains trusted. The project makes no tamper-proof
storage, external attestation, or compliance-certification claim.
