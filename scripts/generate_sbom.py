"""Create machine-readable installed-Python and locked-Node supply-chain evidence."""

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / "docs/evidence"
    prefix = os.environ.get("EVIDENCE_PHASE", "p10-supply-chain")
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is required for the actual locked dependency inventory")
    node = subprocess.run(
        [npm, "sbom", "--package-lock-only", "--sbom-format=cyclonedx"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    python = subprocess.run(
        [
            str(ROOT / ".venv/bin/pip-audit"),
            "--local",
            "--skip-editable",
            "--format=cyclonedx-json",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    counts = {}
    for name, result in (("node", node), ("python", python)):
        document = json.loads(result.stdout)
        if document.get("bomFormat") != "CycloneDX" or not document.get("components"):
            raise SystemExit("SBOM generation did not produce a real component inventory")
        if document.get("vulnerabilities"):
            raise SystemExit("Dependency audit reported an unresolved vulnerability")
        (target / f"{prefix}-{name}-sbom.json").write_text(json.dumps(document, indent=2) + "\n")
        counts[name] = len(document["components"])
    print(
        json.dumps(
            {
                "format": "CycloneDX",
                "components": counts,
                "python_scope": "installed locked environment excluding editable application",
                "node_scope": "package-lock dependency graph including optional platform packages",
            }
        )
    )


if __name__ == "__main__":
    main()
