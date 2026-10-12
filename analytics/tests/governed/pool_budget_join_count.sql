{{ config(tags=['governed']) }}
select b.cycle_id, b.currency from (select cycle_id, currency, sum(budget_minor) as budget_minor, sum(holdback_minor) as holdback_minor from {{ ref('stg_pools') }} group by cycle_id, currency) b
 join {{ ref('mart_comp_cycle_budget') }} m on b.cycle_id = m.cycle_id and b.currency = m.currency
 where b.budget_minor <> m.budget_minor or b.holdback_minor <> m.holdback_minor
