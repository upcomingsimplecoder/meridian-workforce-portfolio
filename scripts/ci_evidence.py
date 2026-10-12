"""Publish an explicit evidence allowlist; raw browser reports/traces never leave CI."""

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile


def publish(root: Path) -> dict:
    destination = root / ".local/public-evidence"
    if destination.is_symlink():
        raise ValueError("Evidence destination must be a project-owned directory")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, mode=0o700)
    report_path = root / "test-results/results.json"
    summary = {
        "scope": "synthetic CI evidence; credentials, headers, bodies and raw reports excluded"
    }
    # This filename is generated only by the CI drill, never committed native evidence.
    drill = root / "docs/evidence/ci-worker-sigkill.json"
    if drill.is_symlink():
        raise ValueError("Drill evidence cannot be a symlink")
    if drill.is_file():
        if drill.stat().st_size > 1_000_000:
            raise ValueError("Drill evidence is too large")
        observed = json.loads(drill.read_text())
        killed, retried = observed["after_kill"], observed["after_retry"]
        hashes = {key: killed[key] for key in ("bytes_sha256", "manifest_sha256")}
        counts = {
            "killed_artifacts": killed["artifact_rows"],
            "killed_receipts": killed["receipt_rows"],
            "retried_artifacts": retried["artifact_rows"],
            "retried_receipts": retried["receipt_rows"],
        }
        if (
            any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in hashes.values())
            or any(type(value) is not int or value < 0 for value in counts.values())
            or type(observed["signal"]) is not int
            or type(observed["observed_child_exit"]) is not int
            or killed["event_state"] not in {"PENDING", "DONE", "DEAD"}
            or type(retried["signature_valid"]) is not bool
            or not re.fullmatch(r"[0-9]{1,24}", str(retried["pool_charge_usd_minor"]))
        ):
            raise ValueError("Invalid bounded drill evidence")
        summary["worker_sigkill"] = {
            "signal": observed["signal"],
            "observed_child_exit": observed["observed_child_exit"],
            "event_after_kill": killed["event_state"],
            "signature_valid": retried["signature_valid"],
            "pool_charge_usd_minor": str(retried["pool_charge_usd_minor"]),
            "counts": counts,
            "hashes": hashes,
        }
    else:
        summary["worker_sigkill"] = "UNVERIFIED_NO_REPORT"
    incremental_path = root / ".local/e2e-case-results.json"
    report_available = report_path.is_file()
    if incremental_path.is_symlink():
        raise ValueError("Incremental evidence cannot be a symlink")
    if incremental_path.is_file():
        partial = json.loads(incremental_path.read_text())
        if partial.get("complete") is not True:
            report_available = False
        elif report_available and partial.get("started_at"):
            prior = json.loads(report_path.read_text())
            newest = datetime.fromisoformat(partial["started_at"].replace("Z", "+00:00"))
            older = datetime.fromisoformat(prior["stats"]["startTime"].replace("Z", "+00:00"))
            if (newest - older).total_seconds() > 5:
                report_available = False
        cases = []
        for row in partial.get("cases", []):
            if not isinstance(row, dict) or not re.fullmatch(
                r"[0-9a-f]{64}", str(row.get("case_sha256", ""))
            ):
                raise ValueError("Invalid incremental case digest")
            if row.get("browser") not in {"chromium", "firefox", "webkit"} or row.get(
                "status"
            ) not in {"passed", "failed", "timedOut", "skipped", "interrupted"}:
                raise ValueError("Invalid incremental case state")
            frames = []
            categories = []
            for error in row.get("diagnostic", []):
                categories.append(
                    error.get("category")
                    if error.get("category") in {"timeout", "assertion", "execution"}
                    else "execution"
                )
                for frame in error.get("frames", [])[:8]:
                    if (
                        re.fullmatch(
                            r"tests/e2e/[a-z0-9_-]+(?:\.spec)?\.ts", str(frame.get("file", ""))
                        )
                        and type(frame.get("line")) is int
                        and type(frame.get("column")) is int
                    ):
                        frames.append({key: frame[key] for key in ("file", "line", "column")})
            cases.append(
                {
                    "case_sha256": row["case_sha256"],
                    "browser": row["browser"],
                    "status": row["status"],
                    "failure_categories": categories,
                    "source_frames": frames,
                }
            )
        summary["incremental"] = {
            "complete": partial.get("complete") is True,
            "observed_cases": len(cases),
            "cases": cases,
        }
    if not report_available:
        summary["browser"] = "INCOMPLETE" if incremental_path.is_file() else "UNVERIFIED_NO_REPORT"
    else:
        report = json.loads(report_path.read_text())
        summary["browser"] = {
            key: report["stats"][key]
            for key in ("expected", "unexpected", "skipped", "flaky", "duration")
        }
        cases = []

        def visit(suite):
            for spec in suite.get("specs", []):
                for case in spec["tests"]:
                    cases.append(
                        {
                            "case_sha256": hashlib.sha256(
                                (spec["file"] + "\0" + spec["title"]).encode()
                            ).hexdigest(),
                            "browser": case["projectName"],
                            "status": case["status"],
                            "attempts": len(case["results"]),
                            "duration_ms": sum(result["duration"] for result in case["results"]),
                        }
                    )
            for child in suite.get("suites", []):
                visit(child)

        for suite in report["suites"]:
            visit(suite)
        summary["cases"] = cases
        summary["report_error_count"] = len(report.get("errors", []))
        traces = []
        for path in sorted((root / "test-results").rglob("trace.zip")):
            if path.is_symlink() or not path.resolve().is_relative_to(
                (root / "test-results").resolve()
            ):
                raise ValueError("Trace must stay inside the private test directory")
            with ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ValueError("Invalid browser trace")
                traces.append(
                    {
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "entries": len(archive.namelist()),
                    }
                )
        summary["valid_private_trace_count"] = len(traces)
        summary["private_trace_checksums"] = traces
        start = datetime.fromisoformat(
            report["stats"]["startTime"].replace("Z", "+00:00")
        ).timestamp()
        screenshots = []
        for path in sorted((root / "docs/evidence").glob("*.png")):
            if path.is_symlink() or path.resolve().parent != (root / "docs/evidence").resolve():
                continue
            if path.stat().st_mtime < start or path.stat().st_size > 20 * 1024 * 1024:
                continue
            if not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Declared screenshot is not PNG")
            # Only explicit synthetic product captures, never failure attachments/DOM snapshots.
            shutil.copyfile(path, destination / path.name)
            screenshots.append(path.name)
        summary["synthetic_product_screenshots"] = screenshots
    dbt = root / "analytics/target/run_results.json"
    if dbt.is_file():
        result = json.loads(dbt.read_text())
        summary["dbt"] = [
            {"node": row["unique_id"], "status": row["status"], "seconds": row["execution_time"]}
            for row in result["results"]
        ]
    (destination / "verification.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = publish(args.root.resolve())
    print(
        json.dumps(
            {
                "browser": result["browser"],
                "public_files": len(list((args.root / ".local/public-evidence").iterdir())),
            }
        )
    )
