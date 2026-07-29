import json
import unittest
from dataclasses import asdict
from pathlib import Path

from core.config import PluginSettings, load_settings

ROOT = Path(__file__).parents[1]


class ConfigContractTestCase(unittest.TestCase):
    def test_schema_keys_and_defaults_match_plugin_settings(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        defaults = asdict(PluginSettings())

        self.assertEqual(set(schema), set(defaults))
        self.assertEqual(
            {name: value["default"] for name, value in schema.items()},
            defaults,
        )
        self.assertEqual(asdict(load_settings({})), defaults)

    def test_schema_declared_types_match_default_values(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        expected_types = {
            "bool": bool,
            "int": int,
            "float": (int, float),
            "string": str,
        }
        for name, definition in schema.items():
            with self.subTest(name=name):
                self.assertIsInstance(
                    definition["default"],
                    expected_types[definition["type"]],
                )


if __name__ == "__main__":
    unittest.main()
