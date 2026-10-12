select distinct batch_id, state, schema_version, accepted_count, rejected_count, watermark, published_at, row_sha256
from {{ export_relation("quality") }}
