from datetime import date

import pytest
from meridian.ingestion_parser import SourceRow
from meridian.people_seed import workforce_rows
from meridian.synthetic_names import fictional_name
from pydantic import ValidationError


def test_fictional_names_are_varied_deterministic_and_keep_financial_inputs():
    rows = workforce_rows()
    assert len(rows) == 5000
    assert len({row["display_name"] for row in rows}) > 4000
    assert rows[0]["display_name"] == "Aisha Adler"
    assert all(row["display_name"] == fictional_name(i) for i, row in enumerate(rows, 1))
    assert not any("Synthetic Worker" in row["display_name"] for row in rows)
    assert rows == workforce_rows()
    # Monetary golden outputs are also checked by the existing finance suite.
    assert {row["currency"] for row in rows} == {"USD", "EUR", "JPY", "KWD"}
    assert sum(row["fte"] == "0.8000" for row in rows) == 500


def source_row():
    row = workforce_rows()[0].copy()
    row["valid_from"] = date(2027, 1, 1).isoformat()
    return row


def test_generated_and_legacy_names_keep_the_synthetic_identity_boundary():
    row = source_row()
    assert SourceRow.model_validate(row).display_name == "Aisha Adler"
    assert (
        SourceRow.model_validate(row | {"display_name": "Synthetic Worker 000001"}).employee_id
        == "SYN-000001"
    )


@pytest.mark.security
@pytest.mark.parametrize(
    "name", ["Actual Person", "Alex Clarke", "Synthetic Worker 000002", "=HYPERLINK(unsafe)"]
)
def test_arbitrary_real_or_other_generated_names_are_rejected(name):
    with pytest.raises(ValidationError):
        SourceRow.model_validate(source_row() | {"display_name": name})
