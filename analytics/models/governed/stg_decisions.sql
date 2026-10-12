select distinct cycle_id, decision_id, proposal_id, revision_id, action, ordinal, actor_id, created_at, correlation_id, row_sha256
from {{ export_relation("decisions") }}
