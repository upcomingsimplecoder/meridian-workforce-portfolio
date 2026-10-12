{{ config(tags=['foundation']) }}
select
    seed_version,
    cast(principal_count as integer) as principal_count,
    cast(as_of as timestamp) as source_as_of
from read_csv('{{ env_var("MERIDIAN_ANALYTICS_SOURCE", ".local/foundation.csv") }}', header = true)
