select distinct snapshot_id, version_id, employee_id, position_id, org_id, manager_id, status, fte, valid_from, valid_to, source_batch_id, recorded_at, row_sha256
from {{ export_relation("workforce") }}
