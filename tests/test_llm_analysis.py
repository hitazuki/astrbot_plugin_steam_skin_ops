import asyncio
import json
import unittest

from core.llm_analysis import (
    ANALYSIS_SCHEMA_VERSION,
    LlmAnalysisError,
    LlmAnalysisService,
    LlmRateLimitError,
)


def valid_output() -> str:
    return (
        "独立观察：候选 A 的市场数据可供独立分析。【candidates.0.identity.smis_id】\n"
        "算法对照：P50 来自既有模型输出。"
        "【candidates.0.model_output.forecast.p50_steam_net_cny】\n"
        "不确定性：样本质量仍需关注。"
    )


def candidates() -> list[dict]:
    return [{
        "identity": {"smis_id": 1579, "name_zh": "裂空武器箱"},
        "derived_history": {
            "coverage": 0.9,
            "daily_series_21d": {
                "date": [f"2026-07-{day:02d}" for day in range(1, 22)],
                "steam_net_cny": [float(day) for day in range(1, 22)],
            },
        },
        "observed_market": {"steam_net_cny": 9.8},
        "model_output": {"forecast": {"p50_steam_net_cny": 10.0}},
        "deterministic_ranking": {"eligible": False, "score": None},
    }]


class LlmAnalysisServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_service_does_not_call_provider(self):
        calls = 0

        async def generate(*_args):
            nonlocal calls
            calls += 1
            return valid_output()

        service = LlmAnalysisService(enabled=False)
        with self.assertRaises(LlmAnalysisError):
            await service.analyze(
                umo="session-a",
                provider_id="provider-a",
                task="quote_explanation",
                candidates=candidates(),
                evaluation_summary=None,
                generate=generate,
            )
        self.assertEqual(calls, 0)

    async def test_request_contract_validates_citations_and_does_not_include_umo(self):
        captured = {}

        async def generate(provider_id, prompt, system_prompt):
            captured.update(
                provider_id=provider_id, prompt=prompt, system_prompt=system_prompt
            )
            return valid_output()

        service = LlmAnalysisService(enabled=True)
        result = await service.analyze(
            umo="private-recipient-value",
            provider_id="provider-a",
            task="quote_explanation",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        request = json.loads(captured["prompt"].rsplit("\n", 1)[1])
        self.assertEqual(request["schema_version"], ANALYSIS_SCHEMA_VERSION)
        self.assertFalse(request["response_contract"]["may_override_score"])
        self.assertNotIn("deterministic_ranking", request["candidates"][0])
        self.assertIn(
            "daily_series_21d", request["candidates"][0]["derived_history"]
        )
        self.assertEqual(
            len(
                request["candidates"][0]["derived_history"]
                ["daily_series_21d"]["date"]
            ),
            21,
        )
        self.assertNotIn("private-recipient-value", captured["prompt"])
        self.assertIn("独立识别", captured["system_prompt"])
        self.assertTrue(
            request["response_contract"][
                "must_analyze_observations_before_deterministic_results"
            ]
        )
        self.assertIn("【SMIS ID】", result.text)
        self.assertIn("【T+7 P50】", result.text)
        self.assertNotIn("candidates.0", result.text)
        self.assertFalse(result.cached)

    async def test_recommendation_view_keeps_ranking_and_bounded_observations(self):
        captured = {}

        async def generate(_provider_id, prompt, _system_prompt):
            captured["prompt"] = prompt
            return valid_output()

        service = LlmAnalysisService(enabled=True)
        await service.analyze(
            umo="session-a",
            provider_id="provider-a",
            task="recommendation_explanation",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        request = json.loads(captured["prompt"].rsplit("\n", 1)[1])
        self.assertIn("deterministic_ranking", request["candidates"][0])
        self.assertIn(
            "daily_series_21d", request["candidates"][0]["derived_history"]
        )
        self.assertEqual(
            len(
                request["candidates"][0]["derived_history"]
                ["daily_series_21d"]["date"]
            ),
            14,
        )

    async def test_results_are_not_retained_and_rate_limit_is_per_session(self):
        calls = 0

        async def generate(*_args):
            nonlocal calls
            calls += 1
            return valid_output()

        service = LlmAnalysisService(enabled=True, calls_per_window=1)
        kwargs = dict(
            provider_id="provider-a",
            task="quote_explanation",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        first = await service.analyze(umo="session-a", **kwargs)
        self.assertFalse(first.cached)
        self.assertEqual(calls, 1)

        with self.assertRaises(LlmRateLimitError):
            await service.analyze(umo="session-a", **kwargs)

        changed = candidates()
        changed[0]["identity"]["smis_id"] = 2
        with self.assertRaises(LlmRateLimitError):
            await service.analyze(umo="session-a", **{**kwargs, "candidates": changed})
        await service.analyze(umo="session-b", **{**kwargs, "candidates": changed})
        self.assertEqual(calls, 2)

    async def test_repairs_missing_sections_and_replaces_unknown_citations(self):
        outputs = iter(
            (
                "独立观察【candidates.0.identity.smis_id】算法对照"
                "【candidates.0.model_output.forecast.p50_steam_net_cny】",
                "独立观察【candidates.0.identity.smis_id】算法对照"
                "【candidates.0.unknown】不确定性",
                "独立观察【candidates.0.identity.smis_id】算法对照，不确定性同上。",
            )
        )

        async def generate(*_args):
            return next(outputs)

        service = LlmAnalysisService(enabled=True, calls_per_window=3)
        repaired = await service.analyze(
            umo="session-a",
            provider_id="provider-a",
            task="case-0",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        self.assertIn("不确定性：原始回复未遵循固定章节格式", repaired.text)
        self.assertEqual(service.status("session-a")["stats"]["reformatted_outputs"], 1)
        corrected = await service.analyze(
            umo="session-a",
            provider_id="provider-a",
            task="case-1",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        self.assertNotIn("unknown", corrected.text)
        self.assertIn("【当前 Steam 到手价】", corrected.text)
        injected = await service.analyze(
            umo="session-a",
            provider_id="provider-a",
            task="case-2",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        self.assertIn("【当前 Steam 到手价】", injected.text)
        stats = service.status("session-a")["stats"]
        self.assertEqual(stats["invalid_outputs"], 0)
        self.assertEqual(stats["injected_citations"], 2)
        self.assertEqual(stats["replaced_invalid_citations"], 1)

    async def test_long_markdown_response_preserves_three_complete_sections(self):
        async def generate(*_args):
            return (
                "**独立观察：**走势需要谨慎解释。"
                "【candidates.0.identity.smis_id】\n"
                "**算法对照：**"
                + "这是较长但应按完整句子裁剪的自然语言依据。" * 80
                + "【candidates.0.model_output.forecast.p50_steam_net_cny】\n"
                "**不确定性：**模型结果不是未来保证。"
            )

        service = LlmAnalysisService(enabled=True, max_output_chars=500)
        result = await service.analyze(
            umo="session-a",
            provider_id="provider-a",
            task="quote_explanation",
            candidates=candidates(),
            evaluation_summary=None,
            generate=generate,
        )
        self.assertLessEqual(len(result.text), 500)
        self.assertIn("独立观察：", result.text)
        self.assertIn("算法对照：", result.text)
        self.assertIn("不确定性：模型结果不是未来保证。", result.text)
        self.assertNotIn("candidates.0", result.text)

    async def test_provider_failure_is_not_retried_or_cached(self):
        calls = 0

        async def generate(*_args):
            nonlocal calls
            calls += 1
            raise RuntimeError("provider unavailable")

        service = LlmAnalysisService(enabled=True, calls_per_window=2)
        with self.assertRaisesRegex(LlmAnalysisError, "Provider"):
            await service.analyze(
                umo="session-a",
                provider_id="provider-a",
                task="quote_explanation",
                candidates=candidates(),
                evaluation_summary=None,
                generate=generate,
            )
        self.assertEqual(calls, 1)

    async def test_concurrency_limit_is_enforced(self):
        active = peak = 0

        async def generate(*_args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return valid_output()

        service = LlmAnalysisService(enabled=True, max_concurrency=1)
        await asyncio.gather(
            service.analyze(
                umo="a",
                provider_id="provider-a",
                task="one",
                candidates=candidates(),
                evaluation_summary=None,
                generate=generate,
            ),
            service.analyze(
                umo="b",
                provider_id="provider-a",
                task="two",
                candidates=candidates(),
                evaluation_summary=None,
                generate=generate,
            ),
        )
        self.assertEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
