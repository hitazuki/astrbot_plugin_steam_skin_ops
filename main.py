from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.config import ConfigError, PluginSettings, load_settings
from .core.integrations.smis import SmisClient
from .core.manager import MonitoringManager, ServiceError
from .core.repository import MonitorStorage
from .core.runtime import PluginRuntime

PLUGIN_NAME = "astrbot_plugin_steam_skin_ops"


HELP_TEXT = """Steam Skin Ops 命令：
/skin search <名称> - 搜索 SMIS 饰品 ID
/skin quote <SMIS_ID|名称> - 全市场即时查询
/skin rule add <SMIS_ID|名称> <ratio|t7|platform|steam> <阈值>
/skin rule list [SMIS_ID] - 当前会话规则
/skin rule set <RULE_ID> <新阈值>
/skin rule remove <RULE_ID>
/skin test - 测试主动推送
/skin status - 查看服务状态
/skin help - 显示帮助

管理操作仅限 AstrBot 管理员。"""


class SteamSkinOpsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        try:
            self.settings: PluginSettings | None = load_settings(config)
            self.config_errors: list[str] = []
        except ConfigError as exc:
            self.settings = None
            self.config_errors = exc.errors
            self.source = self.manager = self.runtime = None
            logger.error("Steam Skin Ops 配置无效：%s", "；".join(exc.errors))
            return

        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.source = SmisClient(
            timeout=self.settings.smis_timeout_seconds,
            max_retries=self.settings.smis_max_retries,
            min_request_interval=self.settings.smis_min_request_interval_seconds,
        )
        self.manager = MonitoringManager(
            MonitorStorage(data_dir / "monitor.db"),
            self.source,
            max_items=self.settings.max_items,
            quote_cache_seconds=self.settings.quote_cache_seconds,
            breakthrough_step_percent=self.settings.breakthrough_step_percent,
            poll_interval_seconds=self.settings.interval_seconds,
        )
        self._dispatch_lock = asyncio.Lock()
        self.runtime = PluginRuntime(
            self.manager,
            self._dispatch_outbox,
            interval_seconds=self.settings.interval_seconds,
            max_workers=self.settings.max_workers,
            daily_summary_time=self.settings.daily_summary_time,
            backup_dir=data_dir / "backups",
        )
        if self.settings.enabled:
            self.runtime.start()

    def _require_manager(self) -> MonitoringManager:
        if self.manager is None:
            raise ServiceError(
                503, "invalid_config", "插件配置无效：" + "；".join(self.config_errors)
            )
        return self.manager

    async def _call(self, func, *args):
        return await asyncio.to_thread(func, *args)

    async def _dispatch_outbox(self) -> dict[str, int]:
        manager = self._require_manager()
        async with self._dispatch_lock:
            sent = failed = 0
            messages = await self._call(manager.storage.due_notifications)
            for message in messages:
                try:
                    chain = MessageChain().message(
                        f"{message['title']}\n{message['content']}"
                    )
                    await self.context.send_message(message["recipient_key"], chain)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._call(
                        manager.storage.mark_notification_failed,
                        message["id"],
                        str(exc),
                    )
                    failed += 1
                    logger.warning("Steam Skin Ops 主动消息发送失败：%s", exc)
                else:
                    await self._call(
                        manager.storage.mark_notification_sent, message["id"]
                    )
                    sent += 1
            return {"sent": sent, "failed": failed}

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, ServiceError) and exc.data:
            lines = [exc.message]
            for row in exc.data:
                suffix = f" / {row.get('name')}" if row.get("name") else ""
                lines.append(f"{row['smis_id']} - {row['name_zh']}{suffix}")
            return "\n".join(lines)
        return str(exc)

    @staticmethod
    def _format_quote(data: dict) -> str:
        marker = "（缓存）" if data.get("cached") else "（实时）"
        if data.get("stale"):
            marker = "（过期快照）"
        ratio = data.get("ratio")
        ratio_text = f"{float(ratio):.2%}" if ratio is not None else "未知"
        source_time = data.get("source_updated_at", "")
        try:
            source_time = datetime.fromisoformat(source_time).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except (TypeError, ValueError):
            pass
        lines = [
            f"{data['name_zh']} / {data['name']}  [SMIS {data['smis_id']}]",
            f"即时倒余额比例：{ratio_text} {marker}",
        ]
        platforms = data.get("platforms") or []
        if platforms:
            lines.append("平台售价：")
            for platform in platforms:
                tags = []
                if platform.get("is_lowest"):
                    tags.append("最低")
                tags.append(f"在售 {platform.get('sell_num') or 0}")
                prefix = "★" if platform.get("is_lowest") else "-"
                lines.append(
                    f"{prefix} {platform['name']}：¥{float(platform['sell_price']):.2f}"
                    f"（{'，'.join(tags)}）"
                )
        else:
            lines.append("平台售价：暂无有效数据")
        lines.extend([
            f"Steam 售价：¥{float(data['steam_sell_price'] or 0):.2f}",
            f"Steam 预计到手：¥{float(data['steam_net'] or 0):.2f}",
            f"Steam 日成交量：{data.get('steam_transaction_quantity') or 0}",
        ])
        if data.get("t7_steam_net_p25") is not None:
            history_label = (
                "七日历史基准" if data.get("t7_sufficient")
                else "七日历史参考（样本不足）"
            )
            lines.append(
                f"{history_label}：Steam 到手 P25 "
                f"¥{float(data['t7_steam_net_p25']):.2f}"
                f"（{data.get('t7_sample_count', 0)} 点 / "
                f"{data.get('t7_span_days', 0)} 天）"
            )
        forecast = data.get("forecast") or {}
        if forecast.get("status") == "ready":
            forecast_ratio = forecast.get("forecast_balance_ratio")
            ratio_text = (
                f"{float(forecast_ratio):.2%}"
                if forecast_ratio is not None else "不可用"
            )
            lines.append(
                f"七日预测：¥{float(forecast['predicted_steam_net']):.2f}"
                f"（{float(forecast['change_pct']) / 100:+.1%}）"
            )
            window = int(forecast.get("window_days") or 0)
            window_text = f"{window} 日" if window else ""
            confidence = "正常" if forecast.get("confidence") == "normal" else "低"
            lines.extend([
                f"预测模式：{window_text}{forecast.get('mode_label') or '未知'}"
                f"（{confidence}置信度）",
                f"预测倒余额比例：{ratio_text}",
            ])
        else:
            reason = (forecast.get("reasons") or ["历史或当前行情不足"])[0]
            lines.append(f"七日预测：不可用（{reason}）")

        risk = data.get("risk_assessment") or {}
        if risk.get("status") == "ready":
            level_text = {"low": "低", "medium": "中", "high": "高"}
            dimensions = risk.get("dimensions") or {}
            dimension_labels = {
                "price": "价格", "volatility": "波动",
                "inventory": "库存", "volume": "成交量",
            }
            detail_parts = []
            for key, label in dimension_labels.items():
                dimension = dimensions.get(key) or {}
                dimension_level = level_text.get(dimension.get("level"), "不可用")
                detail_parts.append(f"{label}{dimension_level}")
            detail = "、".join(detail_parts)
            risk_confidence = "正常" if risk.get("confidence") == "normal" else "低"
            lines.append(
                f"风险评估：总体{level_text.get(risk.get('overall_level'), '未知')}"
                f"（{risk_confidence}置信度）｜{detail}"
            )
            risk_ratio = risk.get("risk_balance_ratio")
            risk_net = risk.get("risk_steam_net")
            current_net = data.get("steam_net")
            forecast_net = forecast.get("predicted_steam_net")
            history_p25 = data.get("t7_steam_net_p25")

            def same_display_price(left: object, right: object) -> bool:
                if left is None or right is None:
                    return False
                return round(float(left), 2) == round(float(right), 2)

            if risk_ratio is None:
                lines.append("风险倒余额比例：不可用")
            elif same_display_price(risk_net, current_net):
                lines.append("风险倒余额比例：同即时比例（当前到手价为风险底价）")
            elif forecast.get("status") == "ready" and same_display_price(
                risk_net, forecast_net
            ):
                lines.append("风险倒余额比例：同预测比例（七日预测价为风险底价）")
            elif same_display_price(risk_net, history_p25):
                lines.append(
                    f"风险倒余额比例：{float(risk_ratio):.2%}"
                    "（七日历史 P25 为风险底价）"
                )
            else:
                risk_net_text = (
                    f"（风险底价 ¥{float(risk_net):.2f}）"
                    if risk_net is not None else ""
                )
                lines.append(
                    f"风险倒余额比例：{float(risk_ratio):.2%}{risk_net_text}"
                )
            for reason in (risk.get("reasons") or [])[:3]:
                lines.append(f"- {reason}")
        else:
            lines.append("风险评估：不可用")
        lines.extend([
            f"数据更新时间：{source_time}", f"SMIS：{data['links']['smis']}",
            f"Steam：{data['links']['steam']}",
        ])
        if data.get("warning"):
            lines.append(f"警告：{data['warning']}")
        return "\n".join(lines)

    @filter.command_group("skin")
    def skin():
        """饰品行情与监控。"""
        pass

    @skin.command("quote")
    async def quote_item(self, event: AstrMessageEvent, query: str):
        """即时查询 SMIS 全市场饰品。"""
        try:
            data = await self._call(self._require_manager().quote, query)
            yield event.plain_result(self._format_quote(data))
        except Exception as exc:
            yield event.plain_result(f"查询失败：{self._error_text(exc)}")

    @skin.command("search")
    async def search_item(self, event: AstrMessageEvent, query: str):
        """搜索 SMIS 全市场饰品。"""
        try:
            rows = await self._call(self._require_manager().search_items, query, 10)
            if not rows:
                yield event.plain_result("SMIS 未找到匹配饰品，请尝试更完整的中文名称。")
                return
            lines = ["SMIS 搜索结果："]
            for row in rows:
                rarity = f"（{row['rarity']}）" if row.get("rarity") else ""
                lines.append(f"{row['smis_id']} - {row['name_zh']}{rarity}")
            if len(rows) > 1:
                lines.append("结果不唯一，请使用 SMIS ID 添加监控。")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"搜索失败：{self._error_text(exc)}")

    @skin.command("items")
    async def list_items(self, event: AstrMessageEvent):
        """列出已配置饰品。"""
        try:
            rows = await self._call(self._require_manager().list_items)
            if not rows:
                yield event.plain_result("尚未配置饰品。管理员可使用 /skin rule add 添加规则。")
                return
            lines = ["已配置饰品："]
            lines.extend(f"{row['smis_id']} - {row['cn_name']} / {row['hash_name']}" for row in rows)
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"查询失败：{self._error_text(exc)}")

    async def _resolve_smis_id(self, query: str) -> int:
        target = query.strip()
        if target.isdigit():
            return int(target)
        rows = await self._call(self._require_manager().search_items, target, 10)
        exact = [
            row for row in rows
            if str(row.get("name_zh") or "").casefold() == target.casefold()
        ]
        candidates = exact if exact else rows
        if not candidates:
            raise ServiceError(404, "item_not_found", "SMIS 未找到匹配饰品，请先使用 /skin search。")
        if len(candidates) > 1:
            lines = ["名称匹配到多个饰品，请改用 SMIS ID："]
            lines.extend(f"{row['smis_id']} - {row['name_zh']}" for row in candidates)
            raise ServiceError(409, "ambiguous_item", "\n".join(lines))
        return int(candidates[0]["smis_id"])

    @skin.group("rule")
    def rule():
        """统一监控规则管理。"""
        pass

    @rule.command("list")
    async def rule_list(self, event: AstrMessageEvent, smis_id: int | None = None):
        """列出当前会话规则。"""
        try:
            rows = await self._call(
                self._require_manager().list_rules,
                event.unified_msg_origin,
                smis_id,
            )
            if not rows:
                yield event.plain_result("当前会话尚未配置规则。")
                return
            labels = {"ratio": "即时比例", "t7": "T+7比例", "platform": "平台买入价", "steam": "Steam清仓价"}
            lines = ["当前会话规则："]
            for row in rows:
                active = "告警中" if row["state"].get("alert_active") else "监控中"
                threshold = float(row["threshold"])
                unit = "%" if row["rule_type"] in {"ratio", "t7"} else "元"
                lines.append(
                    f"#{row['id']} {row['smis_id']} - {row['cn_name']} · "
                    f"{labels[row['rule_type']]} {threshold:g}{unit}（{active}，{row['state'].get('status')}）"
                )
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"查询失败：{self._error_text(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @rule.command("add")
    async def rule_add(self, event: AstrMessageEvent, query: str, rule_type: str, threshold: float):
        """添加监控规则。"""
        try:
            smis_id = await self._resolve_smis_id(query)
            data = await self._call(
                self._require_manager().add_rule,
                event.unified_msg_origin,
                smis_id,
                rule_type.lower(),
                threshold,
            )
            unit = "%" if data["rule_type"] in {"ratio", "t7"} else "元"
            action = data.get("action", "created")
            if action == "updated":
                previous = float(data["previous_threshold"])
                result = (
                    f"规则已更新：#{data['id']} {data['cn_name']}\n"
                    f"类型：{data['rule_type']} · 阈值：{previous:g}{unit} → "
                    f"{float(data['threshold']):g}{unit}"
                )
            elif action == "unchanged":
                result = (
                    f"规则已存在：#{data['id']} {data['cn_name']}\n"
                    f"类型：{data['rule_type']} · 阈值：{float(data['threshold']):g}{unit}"
                )
            else:
                result = (
                    f"规则已添加：#{data['id']} {data['cn_name']}\n"
                    f"类型：{data['rule_type']} · 阈值：{float(data['threshold']):g}{unit}"
                )
            yield event.plain_result(
                result
            )
        except Exception as exc:
            yield event.plain_result(f"添加失败：{self._error_text(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @rule.command("set")
    async def rule_set(self, event: AstrMessageEvent, rule_id: int, threshold: float):
        """修改规则阈值。"""
        try:
            data = await self._call(
                self._require_manager().update_rule,
                event.unified_msg_origin,
                rule_id,
                threshold,
            )
            yield event.plain_result(f"规则 #{data['id']} 阈值已更新为 {float(data['threshold']):g}")
        except Exception as exc:
            yield event.plain_result(f"修改失败：{self._error_text(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @rule.command("remove")
    async def rule_remove(self, event: AstrMessageEvent, rule_id: int):
        """删除规则。"""
        try:
            await self._call(
                self._require_manager().remove_rule,
                event.unified_msg_origin,
                rule_id,
            )
            yield event.plain_result(f"规则 #{rule_id} 已删除")
        except Exception as exc:
            yield event.plain_result(f"删除失败：{self._error_text(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @skin.command("test")
    async def push_test(self, event: AstrMessageEvent):
        """测试主动推送链路。"""
        try:
            await self._call(
                self._require_manager().test_event, event.unified_msg_origin
            )
            result = await self._dispatch_outbox()
            if not result["sent"]:
                raise ServiceError(503, "delivery_failed", "主动消息发送失败，请查看 AstrBot 日志")
            yield event.plain_result("主动推送请求已成功执行，请检查当前会话。")
        except Exception as exc:
            yield event.plain_result(f"推送测试失败：{self._error_text(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @skin.command("status")
    async def service_status(self, event: AstrMessageEvent):
        """查看插件监控状态。"""
        if self.config_errors:
            yield event.plain_result(
                "插件配置无效，后台监控未启动：\n- " + "\n- ".join(self.config_errors)
            )
            return
        try:
            data = self.runtime.status()
            lines = [
                f"插件启用：{'是' if self.settings.enabled else '否'}",
                f"服务运行：{'是' if data.get('running') else '否'}",
                f"饰品数：{data.get('items', 0)}",
                f"规则数：{data.get('rules', 0)}",
                f"最近周期：{data.get('last_cycle_at') or '尚未执行'}",
                f"周期正常：{data.get('last_cycle_ok')}",
                f"待发送：{(data.get('outbox') or {}).get('pending', 0)}",
            ]
            if data.get("last_error"):
                lines.append(f"最近错误：{data['last_error']}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.warning("Steam Skin Ops 状态查询失败: %s", exc)
            yield event.plain_result(f"状态查询失败：{exc}")

    @skin.command("help")
    async def skin_help(self, event: AstrMessageEvent):
        """显示帮助。"""
        yield event.plain_result(HELP_TEXT)

    async def terminate(self):
        """停止后台任务并关闭行情客户端。"""
        if self.runtime is not None:
            await self.runtime.stop()
        if self.source is not None and hasattr(self.source.session, "close"):
            await asyncio.to_thread(self.source.session.close)
