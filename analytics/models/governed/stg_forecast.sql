select distinct scenario_id, revision_id, basis_revision_id, basis, month, year, currency, exponent, base_minor, benefits_minor, variable_minor, employer_tax_minor, total_minor, fx_set_id, policy_id, engine_version, model_version, result_sha256, row_sha256
from {{ export_relation("forecast") }}
