from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.config import ConfigError, load_settings
from core.manager import MonitoringManager
from core.repository import MonitorStorage
from core.runtime import PluginRuntime

ROOT = Path(__file__).parents[1]


class FakeLogger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


class FakeMessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text = text
        return self


class FakeEvent:
    unified_msg_origin = "test:FriendMessage:1"

    def plain_result(self, text):
        return text


class FakeContext:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []
        self.llm_calls = []

    async def send_message(self, umo, chain):
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append((umo, chain.text))

    async def get_current_chat_provider_id(self, *, umo):
        return f"provider:{umo}"

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        return types.SimpleNamespace(
            completion_text=(
                "结论：数据可供分析。【candidates.0.identity.smis_id】\n"
                "与算法的差异：引用现有预测。【candidates.0.model_output.p50】\n"
                "后续关注：仍需留意数据质量。"
            )
        )


def _command_decorator(*_args, **_kwargs):
    def decorate(func):
        func.command = lambda *_a, **_k: _command_decorator()
        func.group = lambda *_a, **_k: _command_decorator()
        return func

    return decorate


def load_plugin(data_path: Path):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    paths = types.ModuleType("astrbot.core.utils.astrbot_path")

    class Star:
        def __init__(self, context):
            self.context = context

    class PermissionType:
        ADMIN = "admin"

    filter_stub = types.SimpleNamespace(
        command_group=_command_decorator,
        permission_type=_command_decorator,
        PermissionType=PermissionType,
    )
    api.AstrBotConfig = dict
    api.logger = FakeLogger()
    event.AstrMessageEvent = FakeEvent
    event.MessageChain = FakeMessageChain
    event.filter = filter_stub
    star.Context = FakeContext
    star.Star = Star
    paths.get_astrbot_plugin_data_path = lambda: str(data_path)
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.utils": utils,
            "astrbot.core.utils.astrbot_path": paths,
        }
    )
    package = types.ModuleType("astrbot_plugin_steam_skin_ops")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    name = "astrbot_plugin_steam_skin_ops.main"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class EmptySource:
    def search_items(self, query, limit=10):
        return []

    def fetch_metadata(self, smis_id):
        raise RuntimeError("unused")

    def fetch_current(self, item):
        raise RuntimeError("unused")

    def fetch_history(self, item, days=30):
        return []


class ConfigTestCase(unittest.TestCase):
    def test_defaults_and_validation(self):
        settings = load_settings({})
        self.assertEqual(settings.interval_seconds, 1800)
        self.assertEqual(settings.max_workers, 2)
        with self.assertRaises(ConfigError) as caught:
            load_settings({"daily_summary_time": "9am", "max_workers": 9})
        self.assertIn("daily_summary_time", str(caught.exception))
        self.assertIn("max_workers", str(caught.exception))


class RuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_empty_cycle_and_daily_backup(self):
        with TemporaryDirectory() as tmp:
            storage = MonitorStorage(Path(tmp) / "monitor.db")
            manager = MonitoringManager(storage, EmptySource())
            dispatches = []

            async def dispatch():
                dispatches.append(True)
                return {"sent": 0, "failed": 0}

            runtime = PluginRuntime(
                manager,
                dispatch,
                interval_seconds=3600,
                max_workers=2,
                daily_summary_time="09:00",
                backup_dir=Path(tmp) / "backups",
            )
            runtime.start()
            await asyncio.sleep(0.1)
            self.assertTrue(runtime.status()["running"])
            self.assertTrue(dispatches)
            self.assertTrue(list((Path(tmp) / "backups").glob("*.db")))
            await runtime.stop()
            self.assertFalse(runtime.status()["running"])


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_plugin_initializes_without_background_tasks(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            plugin = module.SteamSkinOpsPlugin(FakeContext(), {"enabled": False})
            self.assertIsNotNone(plugin.manager)
            self.assertFalse(plugin.runtime.status()["running"])
            self.assertTrue((Path(tmp) / module.PLUGIN_NAME / "monitor.db").exists())
            self.assertTrue((Path(tmp) / module.PLUGIN_NAME / "forecast_evaluation.db").exists())
            await plugin.terminate()

    async def test_invalid_config_is_reported_by_status(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            plugin = module.SteamSkinOpsPlugin(FakeContext(), {"enabled": True, "max_workers": 99})
            results = [value async for value in plugin.service_status(FakeEvent())]
            self.assertIn("配置无效", results[0])
            self.assertIn("max_workers", results[0])

    async def test_ai_uses_current_session_provider_without_persisting_context(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            context = FakeContext()
            plugin = module.SteamSkinOpsPlugin(
                context,
                {"enabled": False, "llm_analysis_enabled": True},
            )
            text = await plugin._ai_text(
                FakeEvent(),
                task="quote_explanation",
                candidates=[{"identity": {"smis_id": 1579}, "model_output": {"p50": 10}}],
            )

            self.assertIn("AI 双轨分析", text)
            self.assertEqual(
                context.llm_calls[0]["chat_provider_id"],
                f"provider:{FakeEvent.unified_msg_origin}",
            )
            self.assertNotIn(FakeEvent.unified_msg_origin, context.llm_calls[0]["prompt"])
            await plugin.terminate()

    async def test_ai_status_does_not_call_model(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            context = FakeContext()
            plugin = module.SteamSkinOpsPlugin(context, {"enabled": False})
            results = [value async for value in plugin.ai_status(FakeEvent())]
            self.assertIn("AI 分析启用：否", results[0])
            self.assertEqual(context.llm_calls, [])
            await plugin.terminate()

    async def test_direct_outbox_delivery_marks_success_and_failure(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            plugin = module.SteamSkinOpsPlugin(FakeContext(), {"enabled": False})
            plugin.manager.storage.enqueue_notification(
                "one", "test:FriendMessage:1", "test", "标题", "正文", driver="astrbot"
            )
            result = await plugin._dispatch_outbox()
            self.assertEqual(result, {"sent": 1, "failed": 0})
            self.assertEqual(plugin.manager.storage.outbox_counts()["sent"], 1)
            plugin.context.fail = True
            plugin.manager.storage.enqueue_notification(
                "two", "test:FriendMessage:2", "test", "标题", "正文", driver="astrbot"
            )
            result = await plugin._dispatch_outbox()
            self.assertEqual(result, {"sent": 0, "failed": 1})
            self.assertEqual(plugin.manager.storage.outbox_counts()["pending"], 1)
            await plugin.terminate()

    async def test_concurrent_outbox_dispatch_sends_each_job_once(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            context = FakeContext()
            plugin = module.SteamSkinOpsPlugin(context, {"enabled": False})
            plugin.manager.storage.enqueue_notification(
                "concurrent:one",
                "test:FriendMessage:1",
                "test",
                "标题",
                "正文",
                driver="astrbot",
            )

            results = await asyncio.gather(
                plugin._dispatch_outbox(),
                plugin._dispatch_outbox(),
            )

            self.assertEqual(len(context.messages), 1)
            self.assertEqual(sum(result["sent"] for result in results), 1)
            self.assertEqual(plugin.manager.storage.outbox_counts()["sent"], 1)
            await plugin.terminate()

    async def test_rule_list_keeps_quarantined_invalid_threshold_visible(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            plugin = module.SteamSkinOpsPlugin(FakeContext(), {"enabled": False})
            plugin.manager.storage.upsert_item(
                {
                    "smis_id": 1579,
                    "item_key": "smis:1579",
                    "appid": 730,
                    "name": "Fracture Case",
                    "name_zh": "裂空武器箱",
                }
            )
            rule = plugin.manager.storage.add_rule(
                1579,
                FakeEvent.unified_msg_origin,
                "platform",
                4.0,
            )
            with plugin.manager.storage.connect() as connection:
                connection.execute(
                    "UPDATE rules SET threshold='broken' WHERE id=?",
                    (int(rule["id"]),),
                )
            plugin.manager._audit_rule_configurations()

            results = [value async for value in plugin.rule_list(FakeEvent())]

            self.assertIn("已隔离", results[0])
            self.assertIn("无效阈值", results[0])
            self.assertNotIn("查询失败", results[0])
            await plugin.terminate()

    def test_command_contract_and_admin_permissions_are_present(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        for command in (
            "skin",
            "search",
            "quote",
            "recommend",
            "items",
            "ai",
            "compare",
            "eval",
            "rule",
            "list",
            "add",
            "set",
            "remove",
            "test",
            "status",
            "help",
        ):
            self.assertIn(f'("{command}")', source)
        self.assertGreaterEqual(source.count("PermissionType.ADMIN"), 6)
        self.assertIn("event.unified_msg_origin", source)
        self.assertIn("get_current_chat_provider_id", source)
        self.assertIn("context.llm_generate", source)
        self.assertIn("request_max_retries=1", source)
        self.assertIn("max_tokens=900", source)
        self.assertIn("max_price: float | None = None", source)
        self.assertIn("min_price: float | None = None", source)
        self.assertIn("min_steam_volume: int | None = None", source)
        self.assertNotIn("service_base_url", source)
        self.assertNotIn("/api/v1/im/message", source)


if __name__ == "__main__":
    unittest.main()
