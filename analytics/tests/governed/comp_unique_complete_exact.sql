{{ config(tags=['governed']) }}
select cycle_id, employee_id from {{ ref('fct_comp_proposal_revision') }} group by cycle_id, employee_id having count(*) <> 1
 union all select cycle_id, employee_id from {{ ref('fct_comp_proposal_revision') }}
 where signature_count <> 3 or annualized_merit_minor + bonus_payable_minor <> pool_charge_minor
 or annualized_merit_minor < 0 or merit_first_year_minor < 0 or bonus_payable_minor < 0
