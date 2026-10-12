select distinct scenario_id, revision_id, operation_id, kind, effective_date, row_sha256
from {{ export_relation("changes") }}
