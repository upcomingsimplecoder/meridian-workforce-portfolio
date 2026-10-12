select batch_id, state, schema_version, accepted_count, rejected_count,
 accepted_count + rejected_count as input_count, watermark, published_at from {{ ref('stg_quality') }}
