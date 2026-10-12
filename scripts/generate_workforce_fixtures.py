"""Reproducible synthetic files; defects change exactly forty of five thousand rows."""

import csv
import io
import json
from pathlib import Path

from meridian.ingestion_parser import FIELDS
from meridian.people_seed import workforce_rows


def fixture_rows(defects: bool = False) -> list[dict[str, str]]:
    result = []
    for index, row in enumerate(workforce_rows(), 1):
        item = {field: str(row[field]) if row.get(field) is not None else "" for field in FIELDS}
        item["valid_from"] = "2027-07-01"
        if defects:
            if 4961 <= index <= 4970:
                item["fte"] = "0.0000"
            elif 4971 <= index <= 4980:
                item["currency"] = "XXX"
            elif 4981 <= index <= 4990:
                item["org_id"] = "D99"
            elif index >= 4991:
                item["manager_id"] = "SYN-999999"
        result.append(item)
    return result


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return target.getvalue().encode()


if __name__ == "__main__":
    folder = Path("fixtures/workforce")
    folder.mkdir(parents=True, exist_ok=True)
    for label, defects in [("quarantine-40", True), ("corrected", False)]:
        (folder / f"{label}.csv").write_bytes(csv_bytes(fixture_rows(defects)))
    (folder / "corrected.json").write_text(
        json.dumps({"schema_version": 1, "records": fixture_rows()}, indent=2) + "\n"
    )
    print(
        "Generated synthetic 5,000-record source fixtures; defective file has exactly 40 invalid rows."
    )
