import json

import pytest

from scripts.ci_evidence import publish


def test_public_ci_evidence_omits_private_failure_values_headers_reports_and_symlinks(tmp_path):
    private = tmp_path / "test-results"
    private.mkdir()
    evidence = tmp_path / "docs/evidence"
    evidence.mkdir(parents=True)
    sentinel = "private-cookie-token-and-response-sentinel"
    report = {
        "stats": {
            "startTime": "2026-10-10T00:00:00Z",
            "expected": 1,
            "unexpected": 1,
            "skipped": 0,
            "flaky": 0,
            "duration": 50,
        },
        "suites": [
            {
                "specs": [
                    {
                        "file": "security.spec.ts",
                        "title": sentinel,
                        "tests": [
                            {
                                "projectName": "chromium",
                                "status": "unexpected",
                                "results": [
                                    {
                                        "duration": 50,
                                        "error": sentinel,
                                        "headers": {"cookie": sentinel},
                                        "body": sentinel,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ],
        "errors": [{"message": sentinel}],
    }
    (private / "results.json").write_text(json.dumps(report))
    (private / "error-context.md").write_text(sentinel)
    outside = tmp_path / "credentials.png"
    outside.write_text(sentinel)
    (evidence / "linked.png").symlink_to(outside)
    result = publish(tmp_path)
    target = tmp_path / ".local/public-evidence"
    assert result["browser"]["unexpected"] == 1
    assert result["cases"][0]["status"] == "unexpected"
    assert result["report_error_count"] == 1
    assert result["synthetic_product_screenshots"] == []
    assert list(target.iterdir()) == [target / "verification.json"]
    assert sentinel not in (target / "verification.json").read_text()
    assert (private / "results.json").read_text() == json.dumps(report)


def test_no_report_does_not_falsely_claim_no_browser_ran_and_unsafe_destination_is_rejected(
    tmp_path,
):
    assert publish(tmp_path)["browser"] == "UNVERIFIED_NO_REPORT"
    target = tmp_path / ".local/public-evidence"
    target.rmdir() if not list(target.iterdir()) else (target / "verification.json").unlink()
    if target.exists():
        target.rmdir()
    target.symlink_to(tmp_path / "test-results")
    with pytest.raises(ValueError, match="project-owned"):
        publish(tmp_path)
    assert not (tmp_path / "test-results").exists()


def test_interrupted_ci_keeps_actual_case_states_without_error_values(tmp_path):
    local = tmp_path / ".local"
    local.mkdir()
    sentinel = "private-cookie-and-response-sentinel"
    (local / "e2e-case-results.json").write_text(
        json.dumps(
            {
                "complete": False,
                "cases": [
                    {
                        "case_sha256": "a" * 64,
                        "browser": "webkit",
                        "status": "failed",
                        "headers": {"cookie": sentinel},
                        "error": sentinel,
                        "diagnostic": [
                            {
                                "category": "assertion",
                                "message": sentinel,
                                "frames": [
                                    {
                                        "file": "tests/e2e/analytics.spec.ts",
                                        "line": 521,
                                        "column": 8,
                                        "input": sentinel,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )
    report = publish(tmp_path)
    assert report["browser"] == "INCOMPLETE"
    assert report["incremental"]["complete"] is False
    assert report["incremental"]["cases"][0]["status"] == "failed"
    assert report["incremental"]["cases"][0]["source_frames"] == [
        {"file": "tests/e2e/analytics.spec.ts", "line": 521, "column": 8}
    ]
    assert sentinel not in (local / "public-evidence/verification.json").read_text()


def test_old_green_report_cannot_mask_a_new_incomplete_browser_run(tmp_path):
    private = tmp_path / "test-results"
    private.mkdir()
    (private / "results.json").write_text(
        json.dumps(
            {
                "stats": {"expected": 96, "unexpected": 0, "startTime": "2026-10-10T00:00:00Z"},
                "suites": [],
            }
        )
    )
    local = tmp_path / ".local"
    local.mkdir()
    (local / "e2e-case-results.json").write_text(
        json.dumps({"complete": False, "started_at": "2026-10-10T01:00:00Z", "cases": []})
    )
    result = publish(tmp_path)
    assert result["browser"] == "INCOMPLETE"
    assert "cases" not in result


def test_actual_drill_projection_excludes_arbitrary_private_values_and_old_native_evidence(
    tmp_path,
):
    evidence = tmp_path / "docs/evidence"
    evidence.mkdir(parents=True)
    sentinel = "private-driver-cookie-sentinel"
    native = evidence / "p10-worker-sigkill.json"
    native.write_text(json.dumps({"scope": sentinel, "pass": True}))
    assert publish(tmp_path)["worker_sigkill"] == "UNVERIFIED_NO_REPORT"
    current = {
        "scope": sentinel,
        "signal": 9,
        "observed_child_exit": -9,
        "after_kill": {
            "artifact_rows": 0,
            "receipt_rows": 0,
            "event_state": "PENDING",
            "bytes_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "headers": {"cookie": sentinel},
        },
        "after_retry": {
            "artifact_rows": 1,
            "receipt_rows": 1,
            "signature_valid": True,
            "pool_charge_usd_minor": "1520000",
            "body": sentinel,
        },
    }
    (evidence / "ci-worker-sigkill.json").write_text(json.dumps(current))
    result = publish(tmp_path)
    assert result["worker_sigkill"]["observed_child_exit"] == -9
    assert result["worker_sigkill"]["counts"]["retried_artifacts"] == 1
    assert sentinel not in (tmp_path / ".local/public-evidence/verification.json").read_text()


def test_drill_evidence_symlink_cannot_publish_a_private_file(tmp_path):
    evidence = tmp_path / "docs/evidence"
    evidence.mkdir(parents=True)
    private = tmp_path / "private.json"
    private.write_text('{"secret":"private-sentinel"}')
    (evidence / "ci-worker-sigkill.json").symlink_to(private)
    with pytest.raises(ValueError, match="Drill evidence cannot be a symlink"):
        publish(tmp_path)
