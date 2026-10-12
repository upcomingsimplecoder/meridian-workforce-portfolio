{{ config(tags=['governed']) }}
select a.version_id from {{ ref('dim_employee_scd2') }} a join {{ ref('dim_employee_scd2') }} b
 on a.employee_id = b.employee_id and a.version_id < b.version_id
 and a.valid_from < coalesce(b.valid_to, cast('9999-12-31' as date))
 and b.valid_from < coalesce(a.valid_to, cast('9999-12-31' as date))
