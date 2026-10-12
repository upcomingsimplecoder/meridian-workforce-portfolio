{% macro export_relation(name) -%}
{% if target.type == 'snowflake' %}
(select * from {{ source('meridian_raw', name) }} where run_id = '{{ var("run_id", "") }}')
{% else %}
read_parquet('{{ var("export_dir", "") | replace("'", "''") }}/{{ name }}.parquet')
{% endif %}
{%- endmacro %}
{% macro previous_day(expression) -%}
{% if target.type == 'snowflake' %}dateadd(day, -1, {{ expression }}){% else %}({{ expression }} - interval '1 day'){% endif %}
{%- endmacro %}
{% macro elapsed_seconds(start, finish) -%}
{% if target.type == 'snowflake' %}datediff('millisecond', {{ start }}, {{ finish }}) / 1000.000{% else %}date_diff('millisecond', {{ start }}, {{ finish }}) / 1000.000{% endif %}
{%- endmacro %}
