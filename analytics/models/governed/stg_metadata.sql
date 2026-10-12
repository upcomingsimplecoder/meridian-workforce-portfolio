select distinct snapshot_id, source_batch_id, watermark, data_as_of, captured_at, model_version, row_sha256
from {{ export_relation("metadata") }}
