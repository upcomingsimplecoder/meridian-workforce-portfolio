"""Check current portfolio copy and local links without installing the application."""

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from scripts.public_release import validate_public_text

ROOT = Path(__file__).resolve().parents[1]
ROOT_DOCS = {"README.md", "SECURITY.md", "PORTFOLIO_ONE_PAGER.md", "INTERVIEW_BRIEF.md"}
GUIDES = {
    "DEVELOPMENT.md",
    "DEPLOYMENT_READINESS.md",
    "REVIEWER_GUIDE.md",
    "SYSTEM_DESIGN_CASE_STUDY.md",
    "RUNBOOKS.md",
    "PUBLIC_AUDIT_SUMMARY.md",
    "PUBLIC_GALLERY.md",
}
COPY_RULE = re.compile(
    r"\b(?:Morgan Stanley|Microsoft|robust|seamless|cutting-edge|leveraging|empowering|"
    r"comprehensive solution|root agent|implementation agent|CUA|Astra|Sol|P0[0-9]|P1[012])\b",
    re.IGNORECASE,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    public = args.public or (ROOT / "EXPORT_MANIFEST.json").is_file()
    docs = []
    for name in filter(None, tracked):
        path = Path(name)
        if (
            name in ROOT_DOCS
            or (name.startswith("docs/") and path.name in GUIDES)
            or name == "docs/portfolio/GALLERY.md"
            or (public and name.startswith("docs/adr/") and path.suffix == ".md")
        ):
            docs.append(path)
        if public:
            if any(
                part
                in {"meridian_blueprint", "history", ".local", "test-results", "playwright-report"}
                for part in path.parts
            ) or path.suffix in {".log", ".zip", ".mp4"}:
                raise ValueError(f"Private archive file in public tree: {name}")
            if name.startswith("docs/evidence/") and path.name != ".gitkeep":
                raise ValueError(f"Raw evidence in public tree: {name}")
    if public:
        screens = [name for name in tracked if name.startswith("docs/portfolio/screens/")]
        if not 8 <= len(screens) <= 12:
            raise ValueError("Keep eight to twelve selected product screenshots")
    links = 0
    for path in docs:
        data = (ROOT / path).read_bytes()
        validate_public_text(str(path), data)
        text = data.decode()
        if COPY_RULE.search(text):
            raise ValueError(f"Public copy needs editing: {path}")
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            url = urlsplit(target.strip("<>"))
            if url.scheme or not url.path:
                continue
            local = (ROOT / path.parent / unquote(url.path)).resolve()
            if not local.is_relative_to(ROOT) or not local.is_file():
                raise ValueError(f"Broken or private link in {path}: {target}")
            links += 1
    print(f"Portfolio copy: {len(docs)} documents and {links} local links passed")


if __name__ == "__main__":
    main()
