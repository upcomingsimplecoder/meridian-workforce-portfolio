select distinct month_start, month_end, row_sha256
from {{ export_relation("calendar") }}
