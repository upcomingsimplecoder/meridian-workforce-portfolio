with active as (
 select w.* from {{ ref('dim_employee_scd2') }} w cross join {{ ref('stg_metadata') }} m
 where w.status = 'ACTIVE' and w.valid_from <= m.data_as_of and (w.valid_to is null or m.data_as_of < w.valid_to)
)
select org_id, count(*) as headcount, cast(sum(fte) as decimal(38,4)) as fte from active group by org_id
union all
select 'ALL' as org_id, count(*) as headcount, cast(coalesce(sum(fte), 0) as decimal(38,4)) as fte from active
