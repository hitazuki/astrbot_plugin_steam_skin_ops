from __future__ import annotations

import json
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
REQUIRED = {
    "main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt",
    "README.md", "LICENSE", "CHANGELOG.md", "logo.png",
}


def main() -> None:
    missing = sorted(name for name in REQUIRED if not (ROOT / name).is_file())
    if missing:
        raise SystemExit(f"missing market files: {', '.join(missing)}")
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    expected = {
        "name": "astrbot_plugin_steam_skin_ops",
        "author": "hitazuki",
        "version": "1.0.0",
        "repo": "https://github.com/hitazuki/astrbot_plugin_steam_skin_ops",
        "astrbot_version": ">=4.16,<5",
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise SystemExit(f"metadata {key!r} must equal {value!r}")
    if not metadata.get("desc") or not metadata.get("short_desc"):
        raise SystemExit("metadata desc and short_desc are required")
    json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    archive = ROOT / "dist" / "astrbot_plugin_steam_skin_ops.zip"
    archive.parent.mkdir(exist_ok=True)
    excluded = {".git", ".venv", "dist", "__pycache__", ".ruff_cache"}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if path.is_file() and not any(part in excluded for part in relative.parts):
                package.write(path, relative)
    if archive.stat().st_size >= 16 * 1024 * 1024:
        raise SystemExit("market zip must be smaller than 16 MiB")
    print(f"validated {archive} ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
