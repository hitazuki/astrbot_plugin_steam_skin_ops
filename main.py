from __future__ import annotations

import asyncio
import math
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.config import ConfigError, PluginSettings, load_settings
from .core.evaluation import ForecastEvaluationStorage
from .core.integrations.smis import SmisClient
from .core.llm_analysis import LlmAnalysisError, LlmAnalysisService
from .core.manager import MonitoringManager, ServiceError
from .core.presentation import format_quote, format_recommendations
from .core.recommendation import RecommendationPolicy
from .core.repository import MonitorStorage
from .core.rules import get_rule_definition
from .core.runtime import PluginRuntime

PLUGIN_NAME = "astrbot_plugin_steam_skin_ops"


HELP_TEXT = """Steam Skin Ops 命令：
/skin search <名称> - 搜索 SMIS 饰品 ID
/skin quote <SMIS_ID|名称> - 全市场即时查询
/skin recommend [最高价] [最低价] [最低Steam日成交量] - 确定性挂刀 Top 5
/skin ai quote <SMIS_ID|名称> - AI 独立观察并对照单品算法结果
/skin ai recommend [最高价] [最低价] [最低Steam日成交量] - AI 双轨分析确定性 Top 5
/skin ai compare <ID1,ID2[,ID3...]> - AI 比较 2～5 个饰品
/skin ai eval [7|30|90|180] [all|lt100|100-999|1000+] - 管理员评估摘要
/skin ai status - 查看当前会话 AI 分析状态
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
            self.source = self.storage = self.manager = self.runtime = None
            self.llm_analysis = None
            logger.error("Steam Skin Ops 配置无效：%s", "；".join(exc.errors))
            return

        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.source = SmisClient(
            timeout=self.settings.smis_timeout_seconds,
            max_retries=self.settings.smis_max_retries,
            min_request_interval=self.settings.smis_min_request_interval_seconds,
        )
        self.storage = MonitorStorage(data_dir / "monitor.db")
        self.evaluation_storage = ForecastEvaluationStorage(
            data_dir / "forecast_evaluation.db"
        )
        self.manager = MonitoringManager(
            self.storage,
            self.source,
            max_items=self.settings.max_items,
            quote_cache_seconds=self.settings.quote_cache_seconds,
            breakthrough_step_percent=self.settings.breakthrough_step_percent,
            poll_interval_seconds=self.settings.interval_seconds,
            recommendation_policy=RecommendationPolicy(
                min_price=self.settings.recommend_min_price,
                max_price=self.settings.recommend_max_price,
                max_ratio=self.settings.recommend_max_ratio_percent / 100,
                min_steam_volume=self.settings.recommend_min_steam_volume,
                max_source_age_minutes=self.settings.recommend_max_age_minutes,
            ),
            evaluation_storage=self.evaluation_storage,
        )
        self.llm_analysis = LlmAnalysisService(
            enabled=self.settings.llm_analysis_enabled,
            calls_per_window=self.settings.llm_calls_per_window,
            window_seconds=self.settings.llm_rate_window_seconds,
            timeout_seconds=self.settings.llm_timeout_seconds,
            max_concurrency=self.settings.llm_max_concurrency,
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

    async def _call(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _generate_analysis(
        self, provider_id: str, prompt: str, system_prompt: str
    ) -> str:
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=900,
            temperature=0.2,
            request_max_retries=1,
        )
        return str(response.completion_text or "")

    async def _ai_text(
        self,
        event: AstrMessageEvent,
        *,
        task: str,
        candidates: list[dict] | None = None,
        evaluation_summary: dict | None = None,
    ) -> str:
        if self.llm_analysis is None:
            raise LlmAnalysisError("AI 分析服务不可用")
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception as exc:
            raise LlmAnalysisError("当前会话未配置可用 AI Provider") from exc
        if not provider_id:
            raise LlmAnalysisError("当前会话未配置可用 AI Provider")
        result = await self.llm_analysis.analyze(
            umo=event.unified_msg_origin,
            provider_id=provider_id,
            task=task,
            candidates=candidates,
            evaluation_summary=evaluation_summary,
            generate=self._generate_analysis,
        )
        return f"AI 双轨分析：\n{result.text}"

    async def _with_ai_fallback(self, deterministic: str, analysis) -> str:
        try:
            return deterministic + "\n\n" + await analysis
        except Exception as exc:
            return deterministic + f"\n\nAI 解读不可用：{self._error_text(exc)}"

    async def _dispatch_outbox(self) -> dict[str, int]:
        self._require_manager()
        async with self._dispatch_lock:
            sent = failed = 0
            messages = await self._call(self.storage.due_notifications)
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
                        self.storage.mark_notification_failed,
                        message["id"],
                        str(exc),
                    )
                    failed += 1
                    logger.warning("Steam Skin Ops 主动消息发送失败：%s", exc)
                else:
                    await self._call(
                        self.storage.mark_notification_sent, message["id"]
                    )
                    sent += 1
            return {"sent": sent, "failed": failed}

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, LlmAnalysisError):
            return str(exc)
        if isinstance(exc, ServiceError):
            if not exc.data:
                return exc.message
            lines = [exc.message]
            for row in exc.data:
                suffix = f" / {row.get('name')}" if row.get("name") else ""
                lines.append(f"{row['smis_id']} - {row['name_zh']}{suffix}")
            return "\n".join(lines)
        logger.exception("Steam Skin Ops 命令处理出现未预期异常")
        return "插件内部错误，请查看 AstrBot 日志"

    @staticmethod
    def _format_quote(data: dict) -> str:
        return format_quote(data)

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

    @skin.command("recommend")
    async def recommend_items(
        self,
        event: AstrMessageEvent,
        max_price: float | None = None,
        min_price: float | None = None,
        min_steam_volume: int | None = None,
    ):
        """按 T+7、风险和即时比例综合推荐最多五个挂刀候选。"""
        try:
            data = await self._call(
                self._require_manager().recommend,
                max_price,
                min_price,
                min_steam_volume,
            )
            yield event.plain_result(format_recommendations(data))
        except Exception as exc:
            yield event.plain_result(f"推荐失败：{self._error_text(exc)}")

    @skin.command("items")
    async def list_items(self, event: AstrMessageEvent):
        """列出已配置饰品。"""
        try:
            rows = await self._call(
                self._require_manager().list_items,
                event.unified_msg_origin,
            )
            if not rows:
                yield event.plain_result("尚未配置饰品。管理员可使用 /skin rule add 添加规则。")
                return
            lines = ["已配置饰品："]
            lines.extend(f"{row['smis_id']} - {row['cn_name']} / {row['hash_name']}" for row in rows)
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"查询失败：{self._error_text(exc)}")

    @skin.group("ai")
    def ai():
        """只读 AI 与确定性算法双轨分析。"""
        pass

    @ai.command("quote")
    async def ai_quote(self, event: AstrMessageEvent, query: str):
        """独立分析单品观察数据，再对照确定性行情和风险。"""
        try:
            data = await self._call(self._require_manager().quote, query)
            deterministic = format_quote(data)
            analysis = self._ai_text(
                event,
                task="quote_explanation",
                candidates=[data["recommendation_candidate"]],
            )
            yield event.plain_result(
                await self._with_ai_fallback(deterministic, analysis)
            )
        except Exception as exc:
            yield event.plain_result(f"AI 行情解读失败：{self._error_text(exc)}")

    @ai.command("recommend")
    async def ai_recommend(
        self,
        event: AstrMessageEvent,
        max_price: float | None = None,
        min_price: float | None = None,
        min_steam_volume: int | None = None,
    ):
        """独立分析市场结构并对照确定性 Top 5，不改变排名。"""
        try:
            data = await self._call(
                self._require_manager().recommend,
                max_price,
                min_price,
                min_steam_volume,
            )
            deterministic = format_recommendations(data)
            candidates = data.get("candidate_records") or []
            if not candidates:
                yield event.plain_result(deterministic)
                return
            analysis = self._ai_text(
                event,
                task="recommendation_explanation",
                candidates=candidates,
            )
            yield event.plain_result(
                await self._with_ai_fallback(deterministic, analysis)
            )
        except Exception as exc:
            yield event.plain_result(f"AI 推荐解读失败：{self._error_text(exc)}")

    @ai.command("compare")
    async def ai_compare(self, event: AstrMessageEvent, smis_ids: str):
        """比较 2～5 个逗号分隔的 SMIS ID。"""
        try:
            tokens = [value.strip() for value in smis_ids.replace("，", ",").split(",")]
            if not 2 <= len(tokens) <= 5 or any(not value.isdigit() for value in tokens):
                raise ServiceError(422, "invalid_compare_items", "请提供 2～5 个逗号分隔的 SMIS ID")
            ids = [int(value) for value in tokens]
            if len(set(ids)) != len(ids):
                raise ServiceError(422, "duplicate_compare_items", "比较列表不能包含重复 ID")
            rows = [
                await self._call(self._require_manager().quote, str(smis_id))
                for smis_id in ids
            ]
            lines = ["确定性比较（保持输入顺序）："]
            for index, row in enumerate(rows, 1):
                forecast = row.get("forecast") or {}
                risk = row.get("risk_assessment") or {}
                lines.append(
                    f"{index}. {row['name_zh']}（SMIS {row['smis_id']}） · "
                    f"即时 {float(row.get('ratio') or 0):.2%} · "
                    f"P50 {float(forecast.get('forecast_balance_ratio') or 0):.2%} · "
                    f"P20 压力 {float(risk.get('risk_balance_ratio') or 0):.2%} · "
                    f"风险 {risk.get('overall_level') or 'unknown'}"
                )
            deterministic = "\n".join(lines)
            analysis = self._ai_text(
                event,
                task="comparison",
                candidates=[row["recommendation_candidate"] for row in rows],
            )
            yield event.plain_result(
                await self._with_ai_fallback(deterministic, analysis)
            )
        except Exception as exc:
            yield event.plain_result(f"AI 比较失败：{self._error_text(exc)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ai.command("eval")
    async def ai_evaluation(
        self,
        event: AstrMessageEvent,
        days: int = 30,
        cohort: str = "all",
    ):
        """解释预测评估聚合指标。"""
        try:
            if days not in {7, 30, 90, 180} or cohort not in {
                "all", "lt100", "100-999", "1000+",
            }:
                raise ServiceError(
                    422,
                    "invalid_evaluation_filter",
                    "用法：/skin ai eval [7|30|90|180] "
                    "[all|lt100|100-999|1000+]",
                )
            summary = await self._call(
                self.evaluation_storage.summary, days=days, cohort=cohort
            )
            lines = [
                f"确定性预测评估：最近 {summary['days']} 天 · {summary['cohort_filter']}",
                f"签发 {summary['issued_count']} · 状态 {summary['status_counts']}",
            ]
            for group in summary["groups"]:
                lines.append(
                    f"- {group['volume_cohort']}/{group['mode']}："
                    f"n={group['resolved_count']} · MAE {group['mae_pct']:.2f}% · "
                    f"方向 {group['direction_accuracy']:.1%} · "
                    f"区间命中 {group['interval_coverage']:.1%} · {group['sample_quality']}"
                )
            deterministic = "\n".join(lines)
            analysis = self._ai_text(
                event,
                task="evaluation_summary",
                evaluation_summary=summary,
            )
            yield event.plain_result(
                await self._with_ai_fallback(deterministic, analysis)
            )
        except Exception as exc:
            yield event.plain_result(f"AI 评估失败：{self._error_text(exc)}")

    @ai.command("status")
    async def ai_status(self, event: AstrMessageEvent):
        """查看当前会话 AI 分析状态，不调用模型。"""
        try:
            if self.llm_analysis is None:
                raise LlmAnalysisError("AI 分析服务不可用")
            status = self.llm_analysis.status(event.unified_msg_origin)
            provider = "不可用"
            if status["enabled"]:
                try:
                    provider = await self.context.get_current_chat_provider_id(
                        umo=event.unified_msg_origin
                    )
                except Exception:
                    pass
            yield event.plain_result("\n".join([
                f"AI 分析启用：{'是' if status['enabled'] else '否'}",
                f"当前 Provider：{provider}",
                f"会话剩余额度：{status['remaining']}/{status['limit']}（"
                f"{status['window_seconds']} 秒窗口）",
                f"运行计数：{status['stats']}",
            ]))
        except Exception as exc:
            yield event.plain_result(f"AI 状态查询失败：{self._error_text(exc)}")

    async def _resolve_smis_id(self, query: str) -> int:
        return await self._call(self._require_manager().resolve_smis_id, query)

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
            lines = ["当前会话规则："]
            for row in rows:
                if row.get("operational", 1) != 1:
                    active = "已隔离"
                else:
                    active = "告警中" if row["state"].get("alert_active") else "监控中"
                definition = get_rule_definition(str(row["rule_type"]))
                label = definition.list_label if definition else f"不支持：{row['rule_type']}"
                try:
                    threshold = float(row["threshold"])
                    if not math.isfinite(threshold):
                        raise ValueError
                    threshold_text = f"{threshold:g}{definition.unit if definition else ''}"
                except (TypeError, ValueError):
                    threshold_text = "无效阈值"
                lines.append(
                    f"#{row['id']} {row['smis_id']} - {row['cn_name']} · "
                    f"{label} {threshold_text}（{active}，{row['state'].get('status')}）"
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
            definition = get_rule_definition(str(data["rule_type"]))
            unit = definition.unit if definition else ""
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
