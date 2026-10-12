select cycle_id, count(*) as decision_count,
 sum(case when action = 'APPROVE' then 1 else 0 end) as approval_count,
 sum(case when action = 'RETURN' then 1 else 0 end) as return_count,
 cast({{ elapsed_seconds('min(created_at)', 'max(created_at)') }} as decimal(20,3)) as observed_elapsed_seconds
 from {{ ref('fct_approval_decision') }} group by cycle_id
