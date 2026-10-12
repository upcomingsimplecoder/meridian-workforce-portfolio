from pathlib import Path

import pytest
from public_release import selected, validate_public_text


def test_private_artifacts_and_history_are_excluded_even_inside_source_roots():
    for path in (
        ".env",
        ".local/demo-credentials.json",
        "docs/history/report.md",
        "docs/evidence/trace.zip",
        "apps/api/.local/operator.json",
        "apps/web/node_modules/module.js",
        "apps/api/private.log",
        "docs/portfolio/MERIDIAN_LOCAL_GUIDE.mp4",
        "fixtures/workforce/corrected.csv",
    ):
        assert not selected(path)
    for path in (
        "apps/api/meridian/main.py",
        "apps/api/migrations/versions/0011_operational_traces.py",
        "tests/e2e/replay.spec.ts",
        "docs/adr/024-hosted-storage-identity-and-free-demo.md",
        "SECURITY.md",
    ):
        assert selected(path)
    assert not selected("docs/PUBLIC_IMPLEMENTATION_STATUS.md")


def test_public_metadata_check_refuses_machine_account_paths_and_em_dashes():
    values = ["/" + "Users/private-owner/project", "srv-" + "a" * 24, "copy" + chr(0x2014) + "copy"]
    for value in values:
        with pytest.raises(RuntimeError):
            validate_public_text("README.md", value.encode())
    validate_public_text(
        "README.md", b"Independent synthetic project. Hosted acceptance UNVERIFIED."
    )


def test_binary_screenshots_are_not_treated_as_text():
    validate_public_text(str(Path("docs/portfolio/screens/workforce.png")), b"\x89PNG\r\n\x1a\n")
