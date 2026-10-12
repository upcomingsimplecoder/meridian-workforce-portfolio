"""Measure compressed production assets against declared budgets, no sample metrics."""

import gzip
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
assets = root / "apps/web/.next/static"
rows = [
    {
        "path": str(path.relative_to(root)),
        "gzip_bytes": len(gzip.compress(path.read_bytes(), mtime=0)),
    }
    for path in sorted(assets.rglob("*.js"))
]
if not rows:
    raise SystemExit("Build production assets before checking the bundle")
largest = max(row["gzip_bytes"] for row in rows)
total = sum(row["gzip_bytes"] for row in rows)
limits = {"largest_chunk_gzip_bytes": 250 * 1024, "all_js_gzip_bytes": 512 * 1024}
output = {
    "scope": "measured local production JavaScript assets; includes all routes, not a per-navigation payload or LCP measurement",
    "assets": rows,
    "largest_chunk_gzip_bytes": largest,
    "all_js_gzip_bytes": total,
    "limits": limits,
}
destination = root / "docs/evidence/bundle-budget.json"
destination.write_text(json.dumps(output, indent=2) + "\n")
print(
    json.dumps({"largest_chunk_gzip_bytes": largest, "all_js_gzip_bytes": total, "limits": limits})
)
if largest > limits["largest_chunk_gzip_bytes"] or total > limits["all_js_gzip_bytes"]:
    raise SystemExit("Production bundle exceeds the declared budget")
