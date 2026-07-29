import os
import shutil
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import validate_market_package as package_validator
from scripts.validate_market_package import (
    ARCHIVE_FILE_MODE,
    ARCHIVE_FILES,
    ARCHIVE_TIMESTAMP,
    EXPECTED_VERSION,
    MAX_ARCHIVE_SIZE,
    ROOT,
    build_market_package,
    validate_source,
)


class MarketPackageValidationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repository"
        for name in (*ARCHIVE_FILES, "pyproject.toml"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)

    def tearDown(self):
        self.tmp.cleanup()

    def test_archive_contains_only_sorted_controlled_files(self):
        unexpected_files = (
            ".github/workflows/ci.yml",
            "astrbot_plugin_steam_skin_ops.egg-info/PKG-INFO",
            "local-secret.txt",
            "scripts/validate_market_package.py",
            "tests/test_core.py",
        )
        for name in unexpected_files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must not be packaged", encoding="utf-8")

        output = Path(self.tmp.name) / "output" / "market.zip"
        archive = build_market_package(output, root=self.root)

        self.assertEqual(archive, output)
        self.assertFalse((self.root / "dist").exists())
        self.assertLess(archive.stat().st_size, MAX_ARCHIVE_SIZE)
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
        self.assertEqual(names, list(ARCHIVE_FILES))
        self.assertEqual(names, sorted(names))
        self.assertTrue(set(names).isdisjoint(unexpected_files))
        self.assertIn("main.py", names)
        self.assertFalse(any(name.startswith(f"{self.root.name}/") for name in names))
        for name in names:
            parts = Path(name).parts
            self.assertNotIn(parts[0], {".github", "scripts", "tests"})
            self.assertFalse(any(part.endswith(".egg-info") for part in parts))

    def test_archive_is_byte_for_byte_reproducible(self):
        first = build_market_package(
            Path(self.tmp.name) / "first.zip",
            root=self.root,
        )

        for index, name in enumerate(ARCHIVE_FILES):
            timestamp = 1_000_000_000 + index * 10_000
            os.utime(self.root / name, (timestamp, timestamp))
        readme = self.root / "README.md"
        readme.write_bytes(readme.read_bytes().replace(b"\n", b"\r\n"))

        second = build_market_package(
            Path(self.tmp.name) / "second.zip",
            root=self.root,
        )

        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(second) as package:
            for info in package.infolist():
                self.assertEqual(info.date_time, ARCHIVE_TIMESTAMP)
                self.assertEqual(info.external_attr >> 16, ARCHIVE_FILE_MODE)

    def test_unlisted_runtime_module_is_rejected(self):
        module = self.root / "core" / "local_debug.py"
        module.write_text("LOCAL_ONLY = True\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError,
            "runtime Python files missing from ARCHIVE_FILES",
        ):
            validate_source(self.root)

    def test_output_must_not_overwrite_a_source_file(self):
        source = self.root / "main.py"
        before = source.read_bytes()

        with self.assertRaisesRegex(ValueError, "output must end in .zip"):
            build_market_package(source, root=self.root)

        self.assertEqual(source.read_bytes(), before)

    def test_output_must_not_overwrite_a_zip_source_file(self):
        source = self.root / "asset.zip"
        source.write_bytes(b"source asset")
        files = tuple(sorted((*ARCHIVE_FILES, "asset.zip")))

        with patch.object(package_validator, "ARCHIVE_FILES", files):
            with self.assertRaisesRegex(
                ValueError,
                "output must not overwrite a source file",
            ):
                build_market_package(source, root=self.root)

        self.assertEqual(source.read_bytes(), b"source asset")

    def test_archive_file_list_must_not_contain_duplicates(self):
        files = tuple(sorted((*ARCHIVE_FILES, "README.md")))

        with patch.object(package_validator, "ARCHIVE_FILES", files):
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                validate_source(self.root)

    def test_failed_build_preserves_existing_archive(self):
        output = Path(self.tmp.name) / "market.zip"
        output.write_bytes(b"previous valid archive")
        (self.root / "logo.png").write_bytes(b"x" * MAX_ARCHIVE_SIZE)

        with self.assertRaisesRegex(ValueError, "smaller than 16 MiB"):
            build_market_package(output, root=self.root)

        self.assertEqual(output.read_bytes(), b"previous valid archive")
        self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_release_versions_must_match_all_three_sources(self):
        mutations = {
            "metadata.yaml": (
                f'version: "{EXPECTED_VERSION}"',
                'version: "9.9.9"',
            ),
            "pyproject.toml": (
                f'version = "{EXPECTED_VERSION}"',
                'version = "9.9.9"',
            ),
        }
        for name, (current, replacement) in mutations.items():
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_text(encoding="utf-8")
                path.write_text(original.replace(current, replacement), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "release versions must all equal"):
                    validate_source(self.root)
                path.write_text(original, encoding="utf-8")

    def test_runtime_dependencies_must_match_requirements(self):
        requirements = self.root / "requirements.txt"
        requirements.write_text(
            requirements.read_text(encoding="utf-8") + "unexpected-package>=1\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError,
            r"requirements\.txt and project\.dependencies must match",
        ):
            validate_source(self.root)


if __name__ == "__main__":
    unittest.main()
