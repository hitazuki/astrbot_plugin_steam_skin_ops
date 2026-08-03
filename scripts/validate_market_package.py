from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).parents[1]
EXPECTED_VERSION = "1.1.0"
MAX_ARCHIVE_SIZE = 16 * 1024 * 1024
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ARCHIVE_FILE_MODE = 0o100644
ARCHIVE_COMPRESSION = zipfile.ZIP_STORED
BINARY_ARCHIVE_FILES = frozenset({"logo.png"})

# Keep this list explicit: a local or generated file must never become part of a
# market release merely because it happens to exist in the repository checkout.
ARCHIVE_FILES = (
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "_conf_schema.json",
    "core/__init__.py",
    "core/alerting.py",
    "core/candidate_features.py",
    "core/config.py",
    "core/evaluation.py",
    "core/history.py",
    "core/history_backfill.py",
    "core/integrations/__init__.py",
    "core/integrations/smis.py",
    "core/manager.py",
    "core/market.py",
    "core/presentation.py",
    "core/recommendation.py",
    "core/repository.py",
    "core/risk.py",
    "core/rules.py",
    "core/runtime.py",
    "docs/DATA_SOURCES_AND_METHODS.md",
    "docs/recommendation_candidate.v1.schema.json",
    "logo.png",
    "main.py",
    "metadata.yaml",
    "requirements.txt",
)

EXPECTED_METADATA = {
    "name": "astrbot_plugin_steam_skin_ops",
    "author": "hitazuki",
    "repo": "https://github.com/hitazuki/astrbot_plugin_steam_skin_ops",
    "astrbot_version": ">=4.16,<5",
}


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _normalise_requirement(value: str) -> str:
    return "".join(value.split())


def _requirements_from_file(path: Path) -> tuple[str, ...]:
    requirements = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-e ", "--editable ", "-r ", "--requirement ")):
            raise ValueError(
                "requirements.txt must list runtime dependencies directly"
            )
        requirements.append(_normalise_requirement(line))
    return tuple(sorted(requirements))


def _project_dependencies(pyproject: dict[str, Any]) -> tuple[str, ...]:
    dependencies = pyproject.get("project", {}).get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(value, str) for value in dependencies
    ):
        raise ValueError("project.dependencies must be a list of strings")
    return tuple(sorted(_normalise_requirement(value) for value in dependencies))


def validate_source(root: Path = ROOT) -> None:
    if len(ARCHIVE_FILES) != len(set(ARCHIVE_FILES)):
        raise ValueError("ARCHIVE_FILES must not contain duplicates")
    if ARCHIVE_FILES != tuple(sorted(ARCHIVE_FILES)):
        raise ValueError("ARCHIVE_FILES must be sorted")
    unknown_binary_files = sorted(BINARY_ARCHIVE_FILES - set(ARCHIVE_FILES))
    if unknown_binary_files:
        raise ValueError(
            "binary archive files missing from ARCHIVE_FILES: "
            + ", ".join(unknown_binary_files)
        )
    missing = sorted(name for name in ARCHIVE_FILES if not (root / name).is_file())
    if missing:
        raise ValueError(f"missing market files: {', '.join(missing)}")

    runtime_files = {
        path.relative_to(root).as_posix()
        for path in (root / "core").rglob("*.py")
        if path.is_file()
    }
    listed_runtime_files = {
        name for name in ARCHIVE_FILES if name.startswith("core/")
    }
    unlisted_runtime_files = sorted(runtime_files - listed_runtime_files)
    if unlisted_runtime_files:
        raise ValueError(
            "runtime Python files missing from ARCHIVE_FILES: "
            + ", ".join(unlisted_runtime_files)
        )

    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata.yaml must contain a mapping")
    for key, value in EXPECTED_METADATA.items():
        if metadata.get(key) != value:
            raise ValueError(f"metadata {key!r} must equal {value!r}")
    if not metadata.get("desc") or not metadata.get("short_desc"):
        raise ValueError("metadata desc and short_desc are required")

    pyproject = _load_toml(root / "pyproject.toml")
    project_version = pyproject.get("project", {}).get("version")
    metadata_version = metadata.get("version")
    versions = {
        "metadata.yaml": metadata_version,
        "pyproject.toml": project_version,
        "validate_market_package.py": EXPECTED_VERSION,
    }
    if any(value != EXPECTED_VERSION for value in versions.values()):
        details = ", ".join(f"{name}={value!r}" for name, value in versions.items())
        raise ValueError(f"release versions must all equal {EXPECTED_VERSION!r}: {details}")

    requirements = _requirements_from_file(root / "requirements.txt")
    dependencies = _project_dependencies(pyproject)
    if requirements != dependencies:
        raise ValueError(
            "requirements.txt and project.dependencies must match: "
            f"requirements={requirements!r}, project.dependencies={dependencies!r}"
        )

    json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))


def build_market_package(
    output: Path | None = None,
    *,
    root: Path = ROOT,
) -> Path:
    validate_source(root)
    archive = output or root / "dist" / "astrbot_plugin_steam_skin_ops.zip"
    if archive.suffix.casefold() != ".zip":
        raise ValueError("market package output must end in .zip")
    protected_inputs = {
        (root / name).resolve()
        for name in (
            *ARCHIVE_FILES,
            "pyproject.toml",
            "scripts/validate_market_package.py",
        )
    }
    if archive.resolve() in protected_inputs:
        raise ValueError("market package output must not overwrite a source file")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{archive.name}.",
        suffix=".tmp",
        dir=archive.parent,
        delete=False,
    ) as handle:
        temporary_archive = Path(handle.name)
    try:
        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=ARCHIVE_COMPRESSION,
        ) as package:
            for name in ARCHIVE_FILES:
                info = zipfile.ZipInfo(name, date_time=ARCHIVE_TIMESTAMP)
                info.compress_type = ARCHIVE_COMPRESSION
                info.create_system = 3
                info.external_attr = ARCHIVE_FILE_MODE << 16
                content = (root / name).read_bytes()
                if name not in BINARY_ARCHIVE_FILES:
                    content = content.replace(b"\r\n", b"\n").replace(
                        b"\r",
                        b"\n",
                    )
                package.writestr(info, content)
        if temporary_archive.stat().st_size >= MAX_ARCHIVE_SIZE:
            raise ValueError("market zip must be smaller than 16 MiB")
        with zipfile.ZipFile(temporary_archive) as package:
            if package.namelist() != list(ARCHIVE_FILES):
                raise ValueError("market zip content does not match ARCHIVE_FILES")
            corrupt_file = package.testzip()
            if corrupt_file is not None:
                raise ValueError(f"market zip contains a corrupt file: {corrupt_file}")
        temporary_archive.replace(archive)
    finally:
        temporary_archive.unlink(missing_ok=True)
    return archive


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and build the market package")
    parser.add_argument(
        "--output",
        type=Path,
        help="write the archive to this path instead of dist/",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        archive = build_market_package(args.output)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise SystemExit(str(error)) from error
    print(f"validated {archive} ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
