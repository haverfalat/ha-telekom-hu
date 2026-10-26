#!/usr/bin/env python3
"""Package only the HACS component and public documentation, never local sessions."""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/telekom_hu"


def main():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    for field in (
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
        "config_flow",
    ):
        if not manifest.get(field):
            raise SystemExit(f"Missing manifest field: {field}")
    assert manifest["domain"] == "telekom_hu"
    assert (COMPONENT / "brand/icon.png").exists()
    for path in COMPONENT.rglob("*.json"):
        json.loads(path.read_text())
    files = [
        p for p in COMPONENT.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ]
    files += [
        ROOT / "hacs.json",
        ROOT / "LICENSE",
        ROOT / "README.md",
        ROOT / "docs/HOME_ASSISTANT.md",
    ]
    out = ROOT / "dist" / f"telekom_hu-{manifest['version']}-hacs.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(ROOT))
    with zipfile.ZipFile(out) as archive:
        assert not any(
            n.startswith((".telekom/", "analysis/")) or n.endswith((".apk", ".xapk"))
            for n in archive.namelist()
        )
    print(f"Created {out}: {len(files)} public files")


if __name__ == "__main__":
    main()
