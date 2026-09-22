"""Build a public-only ZIP from an explicit file allowlist."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

FILES = (
    "SKILL.md", "LICENSE", "agents/openai.yaml", "assets/USER.template.md", "assets/PROFILE.template.md", "assets/SEARCH_PLAN.template.md", "assets/OUTREACH.template.md",
    "references/operations.md", "references/search.md", "references/review.md", "references/sharing.md", "references/intake.md", "references/outreach.md",
    "scripts/boss.py", "scripts/requirements.txt", "scripts/package_skill.py",
    "scripts/bosslib/__init__.py", "scripts/bosslib/local.py", "scripts/bosslib/page.py", "scripts/bosslib/runtime.py",
    "scripts/bosslib/catalog.py", "scripts/bosslib/filters.py", "scripts/bosslib/filter-catalog.json", "scripts/bosslib/outreach.py",
    "scripts/bosslib/reports.py", "scripts/bosslib/reviews.py",
    "scripts/bosslib/browser_process.py",
    "scripts/tests/test_boss.py", "scripts/tests/test_filters.py", "scripts/tests/test_outreach.py", "scripts/tests/test_batches.py",
    "scripts/tests/test_browser_lifecycle.py",
)


def package(root, out):
    root, out = Path(root).resolve(), Path(out).resolve()
    content = {}
    for name in FILES:
        file = root / name
        if not file.is_file() or file.is_symlink() or root not in file.resolve().parents:
            raise ValueError("Missing or unsafe public file: " + name)
        content[name] = file.read_bytes()
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    with zipfile.ZipFile(out, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in content.items():
            archive.writestr("boss-job-copilot/" + name, data)
        archive.writestr("boss-job-copilot/MANIFEST.json", json.dumps(manifest, indent=2))
    return {"zip": str(out), "files": len(content), "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parents[1], args.out)))
