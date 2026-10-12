"""Publish bounded browser evidence without credentials, headers, bodies or raw traces."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--scope", choices=["native", "compose"], default="native")
    args = parser.parse_args()
    report = json.loads((args.source / "results.json").read_text())
    tests = []

    def visit(suite):
        for spec in suite.get("specs", []):
            for test in spec["tests"]:
                tests.append(
                    {
                        "file": spec["file"],
                        "title": spec["title"],
                        "browser": test["projectName"],
                        "status": test["status"],
                        "attempts": len(test["results"]),
                        "duration_ms": sum(row["duration"] for row in test["results"]),
                    }
                )
        for child in suite.get("suites", []):
            visit(child)

    for suite in report["suites"]:
        visit(suite)
    traces = []
    for path in sorted(args.source.rglob("trace.zip")):
        counts = Counter()
        omitted = 0
        with ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Browser trace failed its ZIP integrity check")
            for member in archive.namelist():
                if not member.endswith(".network"):
                    continue
                for line in archive.read(member).splitlines():
                    row = json.loads(line).get("snapshot", {})
                    request, response = row.get("request", {}), row.get("response", {})
                    url = urlsplit(request.get("url", ""))
                    if url.port == 4017:
                        route = "[OIDC path redacted]"
                    elif url.port in (8017, 3017) and (
                        url.path.startswith("/api/v1/") or url.path in {"/login", "/logout"}
                    ):
                        route = url.path
                    else:
                        omitted += 1
                        continue
                    counts[(request.get("method", ""), route, response.get("status", 0))] += 1
        traces.append(
            {
                "private_trace": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "zip_crc_verified": True,
                "requests": [
                    {"method": method, "path": route, "status": status, "count": count}
                    for (method, route, status), count in sorted(counts.items())
                ],
                "omitted_asset_requests": omitted,
            }
        )
    destination = Path("docs/evidence")
    summary = {
        "claim_scope": "actual local Docker Compose browser run; hosted execution UNVERIFIED"
        if args.scope == "compose"
        else "actual native local browser run; hosted/container execution UNVERIFIED",
        "stats": report["stats"],
        "report_errors": len(report["errors"]),
        "tests": tests,
        "private_trace_count": len(traces),
    }
    (destination / f"{args.phase}-browser-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    (destination / f"{args.phase}-trace-review.json").write_text(
        json.dumps(
            {
                "privacy": "method/path/status/count only; all query strings, headers, bodies and OIDC opaque paths omitted; raw traces remain private",
                "traces": traces,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"stats": report["stats"], "private_trace_count": len(traces)}))


if __name__ == "__main__":
    main()
