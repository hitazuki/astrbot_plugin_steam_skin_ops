import json
import unittest
from dataclasses import asdict
from pathlib import Path

from core.config import ConfigError, PluginSettings, load_settings

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

    def test_recommendation_thresholds_reject_unsafe_ranges(self):
        for config, message in (
            (
                {"recommend_min_price": 100, "recommend_max_price": 50},
                "recommend_max_price",
            ),
            ({"recommend_max_ratio_percent": 101}, "recommend_max_ratio_percent"),
        ):
            with self.subTest(config=config):
                with self.assertRaises(ConfigError) as caught:
                    load_settings(config)
                self.assertIn(message, str(caught.exception))

    def test_llm_limits_reject_unsafe_ranges(self):
        for config, message in (
            ({"llm_calls_per_window": 21}, "llm_calls_per_window"),
            ({"llm_timeout_seconds": 121}, "llm_timeout_seconds"),
            ({"llm_max_concurrency": 9}, "llm_max_concurrency"),
            ({"llm_analysis_enabled": "yes"}, "llm_analysis_enabled"),
        ):
            with self.subTest(config=config):
                with self.assertRaises(ConfigError) as caught:
                    load_settings(config)
                self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
