select distinct cycle_id, pool_id, org_id, currency, cost_center, reporting_currency, budget_minor, holdback_minor, row_sha256
from {{ export_relation("pools") }}
