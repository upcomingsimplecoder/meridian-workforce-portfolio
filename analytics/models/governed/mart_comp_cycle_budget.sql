with budgets as (
 select cycle_id, currency, max(reporting_currency) as reporting_currency,
 sum(budget_minor) as budget_minor, sum(holdback_minor) as holdback_minor
 from {{ ref('stg_pools') }} group by cycle_id, currency
), awards as (
 select cycle_id, currency, max(exponent) as exponent, count(*) as row_count,
 sum(annualized_merit_minor) as annualized_merit_minor, sum(merit_first_year_minor) as merit_first_year_minor,
 sum(bonus_payable_minor) as bonus_payable_minor, sum(pool_charge_minor) as pool_charge_minor,
 max(reporting_currency) as reporting_currency, sum(reporting_pool_charge_minor) as reporting_pool_charge_minor
 from {{ ref('fct_comp_proposal_revision') }} group by cycle_id, currency
)
select b.cycle_id, b.currency, coalesce(a.exponent, case b.currency when 'JPY' then 0 when 'KWD' then 3 else 2 end) as exponent,
 coalesce(a.row_count, 0) as row_count,
 cast(coalesce(a.annualized_merit_minor, 0) as decimal(38,0)) as annualized_merit_minor,
 cast(coalesce(a.merit_first_year_minor, 0) as decimal(38,0)) as merit_first_year_minor,
 cast(coalesce(a.bonus_payable_minor, 0) as decimal(38,0)) as bonus_payable_minor,
 cast(coalesce(a.pool_charge_minor, 0) as decimal(38,0)) as pool_charge_minor,
 cast(b.budget_minor as decimal(38,0)) as budget_minor, cast(b.holdback_minor as decimal(38,0)) as holdback_minor,
 cast(b.budget_minor - b.holdback_minor - coalesce(a.pool_charge_minor, 0) as decimal(38,0)) as remaining_minor,
 b.reporting_currency,
 cast(coalesce(a.reporting_pool_charge_minor, 0) as decimal(38,0)) as reporting_pool_charge_minor
from budgets b left join awards a on a.cycle_id = b.cycle_id and a.currency = b.currency
