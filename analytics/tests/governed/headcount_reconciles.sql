{{ config(tags=['governed']) }}
select * from {{ ref('fct_headcount_month') }}
 where opening_headcount + hires + transfers_in - departures - transfers_out <> closing_headcount
