"""Complete, isolated CI suites and fail-closed release aggregation."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSERS = {"chromium", "firefox", "webkit"}
REQUIRED_JOBS = {"checks", "native-setup", "backend", "browsers"}
EXPECTED_BROWSER_CASES = 39
EXPECTED_BROWSER_SHARDS = {browser: {"1", "2"} for browser in BROWSERS}


def partitions(root: Path) -> dict[str, list[str]]:
    """Every discovered test module belongs to exactly one backend suite."""
    suites: dict[str, list[str]] = {"domain": [], "analytics": []}
    for directory in ("apps/api/tests", "tests/tools"):
        for path in sorted((root / directory).rglob("test_*.py")):
            name = path.relative_to(root).as_posix()
            suite = (
                "analytics"
                if path.name.startswith("test_analytics_")
                or path.name == "test_storage_workflows.py"
                else "domain"
            )
            suites[suite].append(name)
    if any(not paths for paths in suites.values()):
        raise ValueError("Both complete backend suites must contain test modules")
    return suites


def browser_gate(root: Path, browser: str) -> int:
    if browser not in BROWSERS:
        raise ValueError("Unknown browser suite")
    incremental = json.loads((root / ".local/e2e-case-results.json").read_text())
    report = json.loads((root / "test-results/results.json").read_text())
    cases = incremental["cases"]
    declared = incremental["declared_cases"]
    stats = report["stats"]
    if (
        incremental.get("complete") is not True
        or incremental.get("result") != "passed"
        or type(declared) is not int
        or declared <= 0
        or len(cases) != declared
        or stats["expected"] != declared
        or any(stats[key] != 0 for key in ("unexpected", "skipped", "flaky"))
        or report.get("errors")
        or any(
            case["browser"] != browser or case["status"] != "passed" or case["retry"] != 0
            for case in cases
        )
    ):
        raise ValueError(
            "Browser suite must finish every declared case once without skips or retries"
        )
    return declared


def browser_aggregate(artifact_root: Path) -> dict[str, int]:
    """Require every unique browser case across the two isolated shards."""
    expected_artifacts = {
        f"browser-{browser}-shard-{shard}-evidence"
        for browser, shards in EXPECTED_BROWSER_SHARDS.items()
        for shard in shards
    }
    entries = list(artifact_root.iterdir())
    if any(path.is_symlink() for path in entries):
        raise ValueError("Browser evidence artifacts cannot be symlinks")
    actual_artifacts = {path.name for path in entries if path.is_dir()}
    if actual_artifacts != expected_artifacts:
        raise ValueError("Every browser shard evidence artifact must be present exactly once")

    totals: dict[str, int] = {}
    for browser, shards in EXPECTED_BROWSER_SHARDS.items():
        seen: set[str] = set()
        for shard in sorted(shards):
            path = (
                artifact_root
                / f"browser-{browser}-shard-{shard}-evidence"
                / "e2e-case-results.json"
            )
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                raise ValueError("Browser shard evidence is missing, unsafe, or too large")
            report = json.loads(path.read_text())
            cases = report.get("cases")
            declared = report.get("declared_cases")
            if (
                report.get("complete") is not True
                or report.get("result") != "passed"
                or type(declared) is not int
                or declared <= 0
                or not isinstance(cases, list)
                or len(cases) != declared
            ):
                raise ValueError("Browser shard did not complete its full declared suite")
            for case in cases:
                digest = case.get("case_sha256") if isinstance(case, dict) else None
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                    or case.get("browser") != browser
                    or case.get("status") != "passed"
                    or type(case.get("retry")) is not int
                    or case["retry"] != 0
                ):
                    raise ValueError("Browser shard includes a failed, retried, or foreign case")
                if digest in seen:
                    raise ValueError("Browser case was duplicated across shards")
                seen.add(digest)
        if len(seen) != EXPECTED_BROWSER_CASES:
            raise ValueError("Combined browser shards must cover every declared case exactly once")
        totals[browser] = len(seen)
    return totals


def release_gate(needs: dict) -> None:
    if set(needs) != REQUIRED_JOBS or any(row.get("result") != "success" for row in needs.values()):
        raise ValueError("Every required CI suite must complete successfully")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    tests = commands.add_parser("tests")
    tests.add_argument("suite", choices=["domain", "analytics"])
    tests.add_argument("--list", action="store_true")
    browser = commands.add_parser("browser-gate")
    browser.add_argument("browser", choices=sorted(BROWSERS))
    aggregate = commands.add_parser("browser-aggregate")
    aggregate.add_argument("artifact_root", type=Path)
    commands.add_parser("gate")
    args = parser.parse_args()
    if args.command == "tests":
        paths = partitions(ROOT)[args.suite]
        if args.list:
            print("\n".join(paths))
            return
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--durations=20", *paths], cwd=ROOT, check=False
        )
        print(
            json.dumps(
                {
                    "suite": args.suite,
                    "test_modules": len(paths),
                    "seconds": round(time.monotonic() - started, 2),
                    "exit_code": result.returncode,
                }
            ),
            flush=True,
        )
        raise SystemExit(result.returncode)
    if args.command == "browser-gate":
        count = browser_gate(ROOT, args.browser)
        print(
            f"{args.browser} shard: {count}/{count} passed; zero skipped, retried or incomplete cases"
        )
        return
    if args.command == "browser-aggregate":
        counts = browser_aggregate(args.artifact_root.resolve())
        print(
            "Complete unique browser coverage: "
            + ", ".join(f"{name} {count}/{count}" for name, count in sorted(counts.items()))
        )
        return
    needs = json.loads(os.environ["MERIDIAN_CI_NEEDS"])
    release_gate(needs)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(
            "All required suites passed. Each backend module ran in one suite, and each browser "
            "used its own database.\n\n| Suite | Result |\n| --- | --- |\n"
            + "".join(f"| {name} | success |\n" for name in sorted(needs))
        )
    print("All required CI suites passed")


if __name__ == "__main__":
    main()
