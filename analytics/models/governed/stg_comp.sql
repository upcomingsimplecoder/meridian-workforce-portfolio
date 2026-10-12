select distinct cycle_id, lock_id, event_id, proposal_id, revision_id, employee_id, org_id, pool_id, currency, exponent, annualized_merit_minor, merit_first_year_minor, bonus_payable_minor, pool_charge_minor, reporting_currency, reporting_pool_charge_minor, guideline_id, fx_set_id, signature_count, row_sha256
from {{ export_relation("comp") }}
