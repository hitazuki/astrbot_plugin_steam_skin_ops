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

    async def send_message(self, umo, chain):
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append((umo, chain.text))


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
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": paths,
    })
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
            await plugin.terminate()

    async def test_invalid_config_is_reported_by_status(self):
        with TemporaryDirectory() as tmp:
            module = load_plugin(Path(tmp))
            plugin = module.SteamSkinOpsPlugin(
                FakeContext(), {"enabled": True, "max_workers": 99}
            )
            results = [value async for value in plugin.service_status(FakeEvent())]
            self.assertIn("配置无效", results[0])
            self.assertIn("max_workers", results[0])

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

    def test_command_contract_and_admin_permissions_are_present(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        for command in (
            "skin", "search", "quote", "items", "rule", "list", "add",
            "set", "remove", "test", "status", "help",
        ):
            self.assertIn(f'("{command}")', source)
        self.assertGreaterEqual(source.count("PermissionType.ADMIN"), 5)
        self.assertIn("event.unified_msg_origin", source)
        self.assertNotIn("service_base_url", source)
        self.assertNotIn("/api/v1/im/message", source)


if __name__ == "__main__":
    unittest.main()
