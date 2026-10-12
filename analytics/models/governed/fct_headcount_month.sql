with ordered as (
 select *, lag(status) over (partition by employee_id order by valid_from) as prev_status,
 lag(org_id) over (partition by employee_id order by valid_from) as prev_org,
 lag(valid_to) over (partition by employee_id order by valid_from) as prev_end,
 lead(status) over (partition by employee_id order by valid_from) as next_status,
 lead(org_id) over (partition by employee_id order by valid_from) as next_org,
 lead(valid_from) over (partition by employee_id order by valid_from) as next_start
 from {{ ref('dim_employee_scd2') }}
), departments as (select distinct org_id from ordered)
select c.month_start, c.month_end, d.org_id,
 sum(case when w.status = 'ACTIVE' and w.valid_from <= {{ previous_day('c.month_start') }} and (w.valid_to is null or {{ previous_day('c.month_start') }} < w.valid_to) then 1 else 0 end) as opening_headcount,
 sum(case when w.status = 'ACTIVE' and w.valid_from <= c.month_end and (w.valid_to is null or c.month_end < w.valid_to) then 1 else 0 end) as closing_headcount,
 sum(case when w.status = 'ACTIVE' and w.valid_from between c.month_start and c.month_end and (w.prev_status is null or w.prev_status <> 'ACTIVE' or w.prev_end is null or w.prev_end <> w.valid_from) then 1 else 0 end) as hires,
 sum(case when w.status = 'ACTIVE' and w.valid_to between c.month_start and c.month_end and (w.next_status is null or w.next_status <> 'ACTIVE' or w.next_start <> w.valid_to) then 1 else 0 end) as departures,
 sum(case when w.status = 'ACTIVE' and w.prev_status = 'ACTIVE' and w.prev_end = w.valid_from and w.prev_org <> w.org_id and w.valid_from between c.month_start and c.month_end then 1 else 0 end) as transfers_in,
 sum(case when w.status = 'ACTIVE' and w.next_status = 'ACTIVE' and w.next_start = w.valid_to and w.next_org <> w.org_id and w.valid_to between c.month_start and c.month_end then 1 else 0 end) as transfers_out
 from {{ ref('stg_calendar') }} c cross join departments d left join ordered w on w.org_id = d.org_id
 group by c.month_start, c.month_end, d.org_id
