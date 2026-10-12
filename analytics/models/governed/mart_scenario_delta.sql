select scenario_id, revision_id, year, currency, exponent, fx_set_id, policy_id,
 cast(sum(case when basis = 'CURRENT' then total_minor else 0 end) as decimal(38,0)) as annual_minor,
 cast(sum(case when basis = 'BASELINE' then total_minor else 0 end) as decimal(38,0)) as baseline_minor,
 cast(sum(case when basis = 'CURRENT' then total_minor else -total_minor end) as decimal(38,0)) as delta_minor
 from {{ ref('fct_workforce_forecast_month') }} group by scenario_id, revision_id, year, currency, exponent, fx_set_id, policy_id
