import json
from pathlib import Path

from meridian.main import app
from meridian.schemas import EventEnvelope

target = Path("packages/contracts/openapi.json")
target.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(f"Generated {target}")
Path("packages/contracts/events.schema.json").write_text(
    json.dumps(EventEnvelope.model_json_schema(), indent=2, sort_keys=True) + "\n"
)
