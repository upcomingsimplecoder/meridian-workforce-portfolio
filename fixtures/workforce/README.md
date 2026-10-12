# Reproducible synthetic source files

Run `make fixtures` from the repository root. The deterministic generator is `scripts/generate_workforce_fixtures.py`; generated CSV/JSON copies are ignored to keep code reviews focused on the source and tests. Setup and browser test commands regenerate them automatically.

- `quarantine-40.csv`: 5,000 rows, exactly 40 intentional errors (10 FTE, 10 unsupported currency, 10 unknown department, 10 missing manager).
- `corrected.csv` and `corrected.json`: the same 5,000 synthetic people with valid inputs.
- Effective start: 2027-07-01. Register against **June workforce · corrected**, watermark **2027-07-31**. Expected publication: 5,000 active people / 4900.0000 FTE / 10,002 effective versions.

All values are synthetic. File names have no authorization meaning; the API independently checks session, grants, byte size/hash, schema and every record. See `docs/DEVELOPMENT.md` for the complete correction journey.
