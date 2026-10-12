import hashlib
import json
from pathlib import Path

import pytest

from scripts.ci import (
    BROWSERS,
    EXPECTED_BROWSER_CASES,
    EXPECTED_BROWSER_SHARDS,
    REQUIRED_JOBS,
    browser_aggregate,
    browser_gate,
    partitions,
    release_gate,
)


def test_backend_partitions_include_new_modules_once_without_manual_allowlists(tmp_path):
    names = {
        "apps/api/tests/test_analytics_api.py",
        "apps/api/tests/test_analytics_crash.py",
        "apps/api/tests/test_storage_workflows.py",
        "apps/api/tests/test_finance_engine.py",
        "apps/api/tests/test_future_domain.py",
        "tests/tools/test_future_tool.py",
    }
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    suites = partitions(tmp_path)
    assert set(suites["domain"]) | set(suites["analytics"]) == names
    assert not set(suites["domain"]) & set(suites["analytics"])
    assert len(suites["domain"]) == len(suites["analytics"]) == 3


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", "pending"])
def test_release_gate_rejects_any_required_suite_that_did_not_pass(state):
    needs = {name: {"result": "success"} for name in REQUIRED_JOBS}
    release_gate(needs)
    needs["backend"]["result"] = state
    with pytest.raises(ValueError):
        release_gate(needs)


def test_release_gate_rejects_a_missing_suite():
    with pytest.raises(ValueError):
        release_gate({name: {"result": "success"} for name in REQUIRED_JOBS - {"browsers"}})


def reports(root: Path):
    incremental = {
        "complete": True,
        "result": "passed",
        "declared_cases": 1,
        "cases": [{"browser": "chromium", "status": "passed", "retry": 0}],
    }
    report = {"stats": {"expected": 1, "unexpected": 0, "skipped": 0, "flaky": 0}, "errors": []}
    (root / ".local").mkdir()
    (root / "test-results").mkdir()
    return incremental, report


@pytest.mark.parametrize("defect", ["incomplete", "missing", "skipped", "retry", "other-browser"])
def test_browser_gate_rejects_incomplete_or_substituted_evidence(tmp_path, defect):
    incremental, report = reports(tmp_path)
    if defect == "incomplete":
        incremental["complete"] = False
    elif defect == "missing":
        incremental["cases"] = []
    elif defect == "skipped":
        report["stats"]["skipped"] = 1
    elif defect == "retry":
        incremental["cases"][0]["retry"] = 1
    else:
        incremental["cases"][0]["browser"] = "webkit"
    (tmp_path / ".local/e2e-case-results.json").write_text(json.dumps(incremental))
    (tmp_path / "test-results/results.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        browser_gate(tmp_path, "chromium")


def test_browser_gate_accepts_complete_single_engine_evidence(tmp_path):
    incremental, report = reports(tmp_path)
    (tmp_path / ".local/e2e-case-results.json").write_text(json.dumps(incremental))
    (tmp_path / "test-results/results.json").write_text(json.dumps(report))
    assert browser_gate(tmp_path, "chromium") == 1


def write_browser_shard_artifacts(root: Path):
    for browser in BROWSERS:
        for shard in EXPECTED_BROWSER_SHARDS[browser]:
            artifact = root / f"browser-{browser}-shard-{shard}-evidence"
            artifact.mkdir(parents=True)
            cases = [
                {
                    "case_sha256": hashlib.sha256(f"{browser}-{index}".encode()).hexdigest(),
                    "browser": browser,
                    "status": "passed",
                    "retry": 0,
                }
                for index in range(EXPECTED_BROWSER_CASES)
                if str(index % 2 + 1) == shard
            ]
            (artifact / "e2e-case-results.json").write_text(
                json.dumps(
                    {
                        "complete": True,
                        "result": "passed",
                        "declared_cases": len(cases),
                        "cases": cases,
                    }
                )
            )


def test_browser_aggregate_requires_every_unique_case_from_all_three_browsers(tmp_path):
    write_browser_shard_artifacts(tmp_path)
    assert browser_aggregate(tmp_path) == {browser: EXPECTED_BROWSER_CASES for browser in BROWSERS}


@pytest.mark.parametrize(
    "defect",
    ["missing-shard", "missing-case", "duplicate", "skipped", "retry", "foreign-browser"],
)
def test_browser_aggregate_rejects_incomplete_or_substituted_shard_evidence(tmp_path, defect):
    write_browser_shard_artifacts(tmp_path)
    chromium_first = tmp_path / "browser-chromium-shard-1-evidence" / "e2e-case-results.json"
    chromium_second = tmp_path / "browser-chromium-shard-2-evidence" / "e2e-case-results.json"
    first = json.loads(chromium_first.read_text())
    second = json.loads(chromium_second.read_text())
    if defect == "missing-shard":
        (tmp_path / "browser-webkit-shard-2-evidence").rename(
            tmp_path / "missing-webkit-shard-2-evidence"
        )
    elif defect == "missing-case":
        second["cases"].pop()
        chromium_second.write_text(json.dumps(second))
    elif defect == "duplicate":
        second["cases"][0]["case_sha256"] = first["cases"][0]["case_sha256"]
        chromium_second.write_text(json.dumps(second))
    elif defect == "skipped":
        first["cases"][0]["status"] = "skipped"
        chromium_first.write_text(json.dumps(first))
    elif defect == "retry":
        first["cases"][0]["retry"] = 1
        chromium_first.write_text(json.dumps(first))
    else:
        first["cases"][0]["browser"] = "webkit"
        chromium_first.write_text(json.dumps(first))
    with pytest.raises(ValueError):
        browser_aggregate(tmp_path)
