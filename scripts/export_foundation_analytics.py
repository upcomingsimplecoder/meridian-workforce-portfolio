"""Export measured runtime source counts for adapter connectivity."""

import csv
from pathlib import Path

from meridian.config import settings
from meridian.db import SessionLocal
from meridian.models import Principal, now
from sqlalchemy import func, select

path = Path(settings().local_data_dir) / "foundation.csv"
with SessionLocal() as db, path.open("w") as file:
    writer = csv.writer(file)
    writer.writerow(["seed_version", "principal_count", "as_of"])
    writer.writerow(
        ["synthetic-v1", db.scalar(select(func.count()).select_from(Principal)), now().isoformat()]
    )
print("Wrote synthetic identity count for local dbt foundation check.")
