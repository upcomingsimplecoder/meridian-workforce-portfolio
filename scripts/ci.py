"""Complete, isolated CI suites and fail-closed release aggregation."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSERS = {"chromium", "firefox", "webkit"}
REQUIRED_JOBS = {"checks", "native-setup", "backend", "browsers"}
WORKFLOW_JOBS = REQUIRED_JOBS | {"scope"}
DOCUMENTATION_FILES = {
    "README.md",
    "SECURITY.md",
    "LICENSE",
    "PORTFOLIO_ONE_PAGER.md",
    "INTERVIEW_BRIEF.md",
    ".github/dependabot.yml",
}
DOCUMENTATION_SUFFIXES = {".md", ".png", ".jpg", ".webp", ".svg", ".json"}
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


def requires_full_verification(paths: list[str]) -> bool:
    """Only a bounded documentation allowlist avoids the product suites."""
    if not paths:
        return True
    for path in paths:
        parts = Path(path).parts
        if not parts or ".." in parts or any(ord(char) < 32 for char in path):
            return True
        if path in DOCUMENTATION_FILES:
            continue
        if path.startswith("docs/") and Path(path).suffix in DOCUMENTATION_SUFFIXES:
            continue
        return True
    return False


def change_scope(root: Path, event_name: str, event: dict) -> tuple[bool, int]:
    if event_name == "pull_request":
        base = event.get("pull_request", {}).get("base", {}).get("sha")
    elif event_name == "push":
        base = event.get("before")
    else:
        return True, 0
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        return True, 0
    try:
        # Treat a renamed executable as deletion plus addition, not only its new doc name.
        raw = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"], cwd=root
        )
        paths = [path.decode("utf-8") for path in raw.split(b"\0") if path]
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return True, 0
    return requires_full_verification(paths), len(paths)


def workflow_gate(needs: dict) -> bool:
    if set(needs) != WORKFLOW_JOBS:
        raise ValueError("Every workflow job must report a result")
    if needs["scope"].get("result") != "success":
        raise ValueError("Change classification and documentation checks must pass")
    mode = needs["scope"].get("outputs", {}).get("full")
    if mode not in {"true", "false"}:
        raise ValueError("Unknown verification scope")
    suites = {name: needs[name] for name in REQUIRED_JOBS}
    if mode == "true":
        release_gate(suites)
        return True
    if any(row.get("result") != "skipped" for row in suites.values()):
        raise ValueError("Documentation verification cannot hide a failed product suite")
    return False


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
    commands.add_parser("scope")
    commands.add_parser("workflow-gate")
    args = parser.parse_args()
    if args.command == "scope":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        full, count = change_scope(ROOT, os.environ["GITHUB_EVENT_NAME"], event)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"full={str(full).lower()}\n")
        print(json.dumps({"full_verification": full, "changed_paths": count}))
        return
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
    if args.command == "workflow-gate":
        full = workflow_gate(needs)
    else:
        release_gate(needs)
        full = True
    message = (
        "All required product suites passed."
        if full
        else "Documentation checks passed. Runtime source was unchanged; product suites did not run."
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(
            message
            + "\n\n| Suite | Result |\n| --- | --- |\n"
            + "".join(f"| {name} | {needs[name]['result']} |\n" for name in sorted(needs))
        )
    print(message)


if __name__ == "__main__":
    main()
