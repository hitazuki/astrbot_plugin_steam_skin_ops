import asyncio
import json
import unittest

from core.llm_analysis import (
    ANALYSIS_SCHEMA_VERSION,
    LlmAnalysisError,
    LlmAnalysisService,
    LlmOutputValidationError,
    LlmRateLimitError,
)


def valid_output() -> str:
    return (
        "结论：候选 A 的确定性结果可供比较。【candidates.0.identity.smis_id】\n"
        "主要依据：P50 来自既有模型输出。【candidates.0.model_output.p50】\n"
        "不确定性：样本质量仍需关注。"
    )


def candidates() -> list[dict]:
    return [{"identity": {"smis_id": 1579}, "model_output": {"p50": 10.0}}]


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
        request = json.loads(captured["prompt"].split("\n", 1)[1])
        self.assertEqual(request["schema_version"], ANALYSIS_SCHEMA_VERSION)
        self.assertFalse(request["response_contract"]["may_override_score"])
        self.assertNotIn("private-recipient-value", captured["prompt"])
        self.assertIn("只解释", captured["system_prompt"])
        self.assertFalse(result.cached)

    async def test_cache_precedes_rate_limit_and_rate_limit_is_per_session(self):
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
        cached = await service.analyze(umo="session-a", **kwargs)
        self.assertFalse(first.cached)
        self.assertTrue(cached.cached)
        self.assertEqual(calls, 1)

        changed = candidates()
        changed[0]["identity"]["smis_id"] = 2
        with self.assertRaises(LlmRateLimitError):
            await service.analyze(umo="session-a", **{**kwargs, "candidates": changed})
        await service.analyze(umo="session-b", **{**kwargs, "candidates": changed})
        self.assertEqual(calls, 2)

    async def test_repairs_missing_sections_but_rejects_invalid_paths(self):
        outputs = iter(
            (
                "结论【candidates.0.identity.smis_id】主要依据【candidates.0.model_output.p50】",
                "结论【candidates.0.identity.smis_id】主要依据【candidates.0.unknown】不确定性",
                "结论【candidates.0.identity.smis_id】主要依据，不确定性同上。",
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
        for index in range(1, 3):
            with self.subTest(index=index), self.assertRaises(LlmOutputValidationError):
                await service.analyze(
                    umo="session-a",
                    provider_id="provider-a",
                    task=f"case-{index}",
                    candidates=candidates(),
                    evaluation_summary=None,
                    generate=generate,
                )
        self.assertEqual(service.status("session-a")["stats"]["invalid_outputs"], 2)

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
