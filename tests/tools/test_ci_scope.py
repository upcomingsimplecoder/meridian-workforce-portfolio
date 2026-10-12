import copy
import subprocess

import pytest

from scripts.ci import REQUIRED_JOBS, change_scope, requires_full_verification, workflow_gate


@pytest.mark.parametrize(
    "path",
    [
        "apps/api/meridian/auth.py",
        "apps/web/app/page.tsx",
        "apps/worker/outbox.py",
        "apps/api/migrations/versions/0012.py",
        "tests/e2e/security.spec.ts",
        "package-lock.json",
        "uv.lock",
        ".github/workflows/ci.yml",
        "scripts/ci.py",
        "render.yaml",
        "unknown.txt",
        "docs/tool.py",
        "docs/../apps/api/meridian/auth.py",
        "docs/readme.md\napps/api/x.py",
    ],
)
def test_runtime_or_unknown_changes_require_every_product_suite(path):
    assert requires_full_verification(["README.md", path]) is True


def test_only_documentation_and_version_update_settings_use_the_small_check():
    assert (
        requires_full_verification(
            [
                "README.md",
                "docs/REVIEWER_GUIDE.md",
                "docs/portfolio/screens/workforce.png",
                ".github/dependabot.yml",
            ]
        )
        is False
    )
    assert requires_full_verification([]) is True


def test_a_renamed_executable_still_requires_product_tests(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "ci@synthetic.invalid")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/runtime.py").write_text("print('runtime')\n")
    git("add", ".")
    git("commit", "-qm", "Runtime")
    before = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    git("mv", "scripts/runtime.py", "docs/runtime.md")
    git("commit", "-qm", "Rename")
    assert change_scope(tmp_path, "push", {"before": before}) == (True, 2)
    assert change_scope(tmp_path, "workflow_dispatch", {}) == (True, 0)
    assert change_scope(tmp_path, "pull_request", {}) == (True, 0)


def workflow(full):
    return {
        "scope": {"result": "success", "outputs": {"full": str(full).lower()}},
        **{name: {"result": "success" if full else "skipped"} for name in REQUIRED_JOBS},
    }


@pytest.mark.parametrize("full", [True, False])
def test_workflow_gate_requires_exact_results_for_its_scope(full):
    needs = workflow(full)
    assert workflow_gate(needs) is full
    for state in ("failure", "cancelled", "pending", "skipped" if full else "success"):
        failed = copy.deepcopy(needs)
        failed["backend"]["result"] = state
        with pytest.raises(ValueError):
            workflow_gate(failed)
    needs["scope"]["outputs"]["full"] = "unknown"
    with pytest.raises(ValueError):
        workflow_gate(needs)


def test_documentation_lane_cannot_pass_a_missing_or_failed_classifier():
    needs = workflow(False)
    del needs["browsers"]
    with pytest.raises(ValueError):
        workflow_gate(needs)
    needs = workflow(False)
    needs["scope"]["result"] = "failure"
    with pytest.raises(ValueError):
        workflow_gate(needs)
