select distinct version_id, snapshot_id, org_id, parent_id, name, cost_center, region, valid_from, valid_to, row_sha256
from {{ export_relation("orgs") }}
