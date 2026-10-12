select m.snapshot_id, m.source_batch_id, m.watermark as source_watermark, m.data_as_of, m.captured_at, m.model_version,
 (select count(*) from {{ ref('dim_employee_scd2') }}) as employee_versions,
 (select count(distinct employee_id) from {{ ref('dim_employee_scd2') }}) as synthetic_people,
 (select count(*) from {{ ref('dim_position') }}) as positions
 from {{ ref('stg_metadata') }} m
