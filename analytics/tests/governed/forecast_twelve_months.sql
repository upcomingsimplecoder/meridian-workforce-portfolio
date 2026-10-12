{{ config(tags=['governed']) }}
select revision_id from {{ ref('fct_workforce_forecast_month') }} group by revision_id
 having sum(case when basis = 'CURRENT' then 1 else 0 end) <> 12 or sum(case when basis = 'BASELINE' then 1 else 0 end) <> 12
 union all select revision_id from {{ ref('fct_workforce_forecast_month') }} where base_minor + benefits_minor + variable_minor + employer_tax_minor <> total_minor
