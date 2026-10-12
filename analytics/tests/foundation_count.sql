{{ config(tags=['foundation']) }}
select * from {{ ref('stg_foundation') }} where principal_count <= 0
