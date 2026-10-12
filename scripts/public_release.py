"""Export a bounded portfolio tree from a tested Git revision, never its history."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT_FILES = {
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "PORTFOLIO_ONE_PAGER.md",
    "INTERVIEW_BRIEF.md",
    "Makefile",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "playwright.config.ts",
    "render.yaml",
    ".gitignore",
    ".dockerignore",
    ".env.example",
}
SOURCE_ROOTS = {"apps", "packages", "analytics", "scripts", "tests", "infra", "fixtures", ".github"}
DOCS = {
    "docs/DEVELOPMENT.md",
    "docs/DEPLOYMENT_READINESS.md",
    "docs/REVIEWER_GUIDE.md",
    "docs/SYSTEM_DESIGN_CASE_STUDY.md",
    "docs/RUNBOOKS.md",
    "docs/PUBLIC_AUDIT_SUMMARY.md",
    "docs/PUBLIC_GALLERY.md",
}
PUBLIC_ADRS = {
    "docs/public/adr/001-workforce-history-and-scope.md",
    "docs/public/adr/002-deterministic-financial-calculations.md",
    "docs/public/adr/003-revision-bound-approvals.md",
    "docs/public/adr/004-outbox-and-derived-analytics.md",
    "docs/public/adr/005-decision-replay.md",
    "docs/public/adr/006-runtime-and-observability-boundaries.md",
    "docs/adr/024-hosted-storage-identity-and-free-demo.md",
}
DOCS |= PUBLIC_ADRS
MAPPING = {
    "docs/PUBLIC_GALLERY.md": "docs/portfolio/GALLERY.md",
    **{path: path.replace("docs/public/adr/", "docs/adr/") for path in PUBLIC_ADRS},
}
FORBIDDEN_PARTS = {
    "node_modules",
    ".next",
    ".local",
    ".venv",
    "__pycache__",
    "target",
    "logs",
    "history",
    "test-results",
    "playwright-report",
}
PRIVATE_METADATA = re.compile(
    r"/" + r"Users/[A-Za-z0-9_.-]+/|/home/[A-Za-z0-9_.-]+/|\b(?:tea|team|prj|srv)-[a-z0-9]{16,}\b"
)


def selected(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or any(part in FORBIDDEN_PARTS for part in parts):
        return False
    if path in ROOT_FILES or path in DOCS:
        return True
    if parts[0] in SOURCE_ROOTS:
        return not path.endswith((".log", ".zip", ".mp4", ".csv", ".duckdb", ".pyc"))
    return path.startswith("docs/portfolio/screens/")


def validate_public_text(path: str, data: bytes) -> None:
    if Path(path).suffix not in {
        ".md",
        ".py",
        ".ts",
        ".tsx",
        ".mjs",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".example",
    }:
        return
    text = data.decode("utf-8")
    if PRIVATE_METADATA.search(text):
        raise RuntimeError(f"Private machine/account metadata in {path}")
    if chr(0x2014) in text:
        raise RuntimeError(f"Public em dash in {path}")


def export(root: Path, revision: str, destination: Path) -> dict:
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Public export requires an empty destination")
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=root, text=True
    ).strip()
    paths = subprocess.check_output(
        ["git", "ls-tree", "--name-only", "-r", sha], cwd=root, text=True
    ).splitlines()
    chosen = [path for path in paths if selected(path)]
    missing = (ROOT_FILES | DOCS) - set(chosen)
    if missing:
        raise RuntimeError("Public source/doc inventory incomplete: " + ", ".join(sorted(missing)))
    images = [path for path in chosen if path.startswith("docs/portfolio/screens/")]
    if not 8 <= len(images) <= 12:
        raise RuntimeError("Public portfolio requires eight to twelve screenshots")
    files = []
    data_by_path = {}
    for path in chosen:
        data = subprocess.check_output(["git", "show", sha + ":" + path], cwd=root)
        public = MAPPING.get(path, path)
        validate_public_text(public, data)
        if public in data_by_path:
            raise RuntimeError("Duplicate public destination")
        data_by_path[public] = data
        files.append(
            {"path": public, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    destination.mkdir(parents=True, exist_ok=True)
    for path, data in data_by_path.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (destination / "docs/evidence").mkdir(exist_ok=True)
    (destination / "docs/evidence/.gitkeep").touch()
    receipt = {
        "source_revision": sha,
        "file_count": len(files),
        "screenshot_count": len(images),
        "total_bytes": sum(file["bytes"] for file in files),
        "files": files,
        "scope": "Source snapshot only. Full private engineering history and forensic evidence excluded.",
    }
    (destination / "EXPORT_MANIFEST.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.destination.resolve()
    if destination == root or destination.is_relative_to(root):
        raise RuntimeError("Export must be separate from the engineering archive")
    receipt = export(root, args.revision, destination)
    print(json.dumps({key: value for key, value in receipt.items() if key != "files"}))


if __name__ == "__main__":
    main()
