"""Run actual gates, fail immediately, retain timestamped local evidence."""

import argparse
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--phase", default="p01")
parser.add_argument("--browsers", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
os.chdir(root)
evidence = root / "docs/evidence"
evidence.mkdir(exist_ok=True)
env = os.environ.copy()
env.pop("NO_COLOR", None)
env["PATH"] = "/opt/homebrew/opt/node@24/bin:" + env["PATH"]
env["NEXT_TELEMETRY_DISABLED"] = "1"
env["EVIDENCE_PHASE"] = args.phase
commands = [
    ["make", "test"],
    [".venv/bin/python", "-m", "scripts.operator_db", "check"],
    ["make", "analytics"],
    ["npm", "audit", "--audit-level=low"],
    [".venv/bin/pip-audit", "--local", "--skip-editable"],
    ["npm", "run", "build"],
    [".venv/bin/python", "scripts/verify_bundle.py"],
]
if args.browsers:
    commands += [
        [".venv/bin/python", "scripts/local.py", "down"],
        [".venv/bin/python", "scripts/local.py", "up"],
        ["npm", "run", "e2e"],
    ]
results = []
for command in commands:
    started = datetime.now(UTC).isoformat()
    begin = time.perf_counter()
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    seconds = round(time.perf_counter() - begin, 3)
    output = result.stdout + result.stderr
    log = evidence / f"{args.phase}-gate-{len(results) + 1}.log"
    clean_output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    log.write_text("\n".join(line.rstrip() for line in clean_output.splitlines()).rstrip() + "\n")
    entry = {
        "command": command,
        "started_at": started,
        "seconds": seconds,
        "exit_code": result.returncode,
        "log": str(log.relative_to(root)),
    }
    results.append(entry)
    (evidence / f"{args.phase}-gates.json").write_text(json.dumps(results, indent=2) + "\n")
    print(
        f"{'PASS' if result.returncode == 0 else 'FAIL'} {' '.join(command)} ({seconds}s); {log.relative_to(root)}",
        flush=True,
    )
    if result.returncode:
        print(output[-6000:])
        raise SystemExit(result.returncode)
