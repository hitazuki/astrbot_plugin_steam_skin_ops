from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from .market import MarketSnapshot
from .rules import get_rule_definition


def same_display_price(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return round(float(left), 2) == round(float(right), 2)


def level_text(level: object) -> str:
    return {"low": "低", "medium": "中", "high": "高"}.get(str(level), "未知")


def format_quote(data: dict) -> str:
    marker = "（缓存）" if data.get("cached") else "（实时）"
    if data.get("stale"):
        marker = "（过期快照）"
    ratio = data.get("ratio")
    ratio_text = f"{float(ratio):.2%}" if ratio is not None else "未知"
    source_time = data.get("source_updated_at", "")
    try:
        source_time = (
            datetime.fromisoformat(source_time)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
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
        forecast_ratio_text = (
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
            f"预测倒余额比例：{forecast_ratio_text}",
        ])
    else:
        reason = (forecast.get("reasons") or ["历史或当前行情不足"])[0]
        lines.append(f"七日预测：不可用（{reason}）")

    risk = data.get("risk_assessment") or {}
    if risk.get("status") == "ready":
        dimensions = risk.get("dimensions") or {}
        dimension_labels = {
            "price": "价格",
            "volatility": "波动",
            "inventory": "库存",
            "volume": "成交量",
        }
        detail_parts = []
        for key, label in dimension_labels.items():
            dimension = dimensions.get(key) or {}
            dimension_level = {"low": "低", "medium": "中", "high": "高"}.get(
                dimension.get("level"), "不可用"
            )
            detail_parts.append(f"{label}{dimension_level}")
        detail = "、".join(detail_parts)
        risk_confidence = "正常" if risk.get("confidence") == "normal" else "低"
        lines.append(
            f"风险评估：总体{level_text(risk.get('overall_level'))}"
            f"（{risk_confidence}置信度）｜{detail}"
        )
        risk_ratio = risk.get("risk_balance_ratio")
        risk_net = risk.get("risk_steam_net")
        current_net = data.get("steam_net")
        forecast_net = forecast.get("predicted_steam_net")
        history_p25 = data.get("t7_steam_net_p25")
        if risk_ratio is None:
            lines.append("风险倒余额比例：不可用")
        elif same_display_price(risk_net, current_net):
            lines.append("风险倒余额比例：同即时比例（当前到手价为风险底价）")
        elif (
            forecast.get("status") == "ready"
            and same_display_price(risk_net, forecast_net)
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
            lines.append(f"风险倒余额比例：{float(risk_ratio):.2%}{risk_net_text}")
        for reason in (risk.get("reasons") or [])[:3]:
            lines.append(f"- {reason}")
    else:
        lines.append("风险评估：不可用")
    lines.extend([
        f"数据更新时间：{source_time}",
        f"SMIS：{data['links']['smis']}",
        f"Steam：{data['links']['steam']}",
    ])
    if data.get("warning"):
        lines.append(f"警告：{data['warning']}")
    return "\n".join(lines)


def analysis_reference_lines(
    forecast: dict,
    risk: dict,
    *,
    current_steam_net: float | None = None,
    t7_steam_net_p25: float | None = None,
) -> list[str]:
    lines: list[str] = []
    if forecast.get("status") == "ready":
        ratio = forecast.get("forecast_balance_ratio")
        ratio_text = f"{float(ratio):.2%}" if ratio is not None else "不可用"
        window = int(forecast.get("window_days") or 0)
        window_text = f"{window} 日" if window else ""
        lines.extend([
            f"七日预测：¥{float(forecast['predicted_steam_net']):.2f}"
            f"（{float(forecast['change_pct']) / 100:+.1%}）"
            f"｜预测倒余额比例 {ratio_text}",
            f"预测模式：{window_text}{forecast['mode_label']}（"
            f"{'正常' if forecast.get('confidence') == 'normal' else '低'}置信度）",
        ])
    else:
        reasons = forecast.get("reasons") or ["历史或当前行情不足"]
        lines.append(f"七日预测：不可用（{reasons[0]}）")

    if risk.get("status") == "ready":
        dimensions = risk.get("dimensions") or {}
        labels = {
            "price": "价格",
            "volatility": "波动",
            "inventory": "库存",
            "volume": "成交量",
        }
        dimension_parts = []
        for key in labels:
            level = (dimensions.get(key) or {}).get("level")
            dimension_level = "不可用" if level in {None, "unknown"} else level_text(level)
            dimension_parts.append(f"{labels[key]}{dimension_level}")
        dimension_text = "、".join(dimension_parts)
        ratio = risk.get("risk_balance_ratio")
        confidence = "正常" if risk.get("confidence") == "normal" else "低"
        lines.append(
            f"风险评估：总体{level_text(risk.get('overall_level'))}"
            f"（{confidence}置信度）｜{dimension_text}"
        )
        risk_net = risk.get("risk_steam_net")
        forecast_net = forecast.get("predicted_steam_net")
        if ratio is None:
            lines.append("风险倒余额比例：不可用")
        elif same_display_price(risk_net, current_steam_net):
            lines.append("风险倒余额比例：同即时比例（当前到手价为风险底价）")
        elif (
            forecast.get("status") == "ready"
            and same_display_price(risk_net, forecast_net)
        ):
            lines.append("风险倒余额比例：同预测比例（七日预测价为风险底价）")
        elif same_display_price(risk_net, t7_steam_net_p25):
            lines.append(
                f"风险倒余额比例：{float(ratio):.2%}"
                "（七日历史 P25 为风险底价）"
            )
        else:
            risk_net_text = (
                f"（风险底价 ¥{float(risk_net):.2f}）"
                if risk_net is not None else ""
            )
            lines.append(f"风险倒余额比例：{float(ratio):.2%}{risk_net_text}")
        reasons = risk.get("reasons") or []
        if reasons:
            lines.append(f"风险说明：{'；'.join(str(reason) for reason in reasons[:3])}")
    else:
        lines.append("风险评估：不可用")
    return lines


def daily_analysis_suffix(forecast: dict, risk: dict) -> str:
    if forecast.get("status") == "ready":
        forecast_ratio = forecast.get("forecast_balance_ratio")
        forecast_text = (
            f"预测{float(forecast['change_pct']) / 100:+.1%}"
            f"/{float(forecast_ratio):.2%}"
            if forecast_ratio is not None
            else f"预测{float(forecast['change_pct']) / 100:+.1%}"
        )
        mode_text = str(forecast.get("mode_label") or "未知模式")
    else:
        forecast_text, mode_text = "预测不可用", ""
    if risk.get("status") == "ready":
        risk_ratio = risk.get("risk_balance_ratio")
        risk_text = f"风险{level_text(risk.get('overall_level'))}"
        if risk_ratio is not None:
            forecast_ratio = forecast.get("forecast_balance_ratio")
            if (
                forecast.get("status") == "ready"
                and forecast_ratio is not None
                and f"{float(risk_ratio):.2%}" == f"{float(forecast_ratio):.2%}"
            ):
                risk_text += "/同预测比例"
            else:
                risk_text += f"/{float(risk_ratio):.2%}"
    else:
        risk_text = "风险不可用"
    mode_suffix = f"({mode_text})" if mode_text else ""
    return f" · {forecast_text}{mode_suffix} · {risk_text}"


def market_reference_lines(
    snapshot: MarketSnapshot,
    stats: dict,
    forecast: dict,
    risk: dict,
) -> list[str]:
    lines: list[str] = []
    lowest = snapshot.lowest_platform
    if lowest:
        lines.append(f"最低平台：{lowest[0]} ¥{lowest[1]:.2f}（在售 {lowest[2]}）")
    lines.extend([
        f"Steam 售价：¥{float(snapshot.steam_sell_price or 0):.2f}",
        f"Steam 预计到手：¥{snapshot.steam_net:.2f}",
        f"Steam 在售/日成交：{snapshot.steam_sell_num or 0}/"
        f"{snapshot.steam_transaction_quantity or 0}",
    ])
    if stats.get("t7_steam_net_p25") is not None:
        history_label = (
            "七日历史基准" if stats.get("t7_sufficient")
            else "七日历史参考（样本不足）"
        )
        lines.append(
            f"{history_label}：到手最低 ¥{stats['t7_steam_net_low']:.2f}｜"
            f"P25 ¥{stats['t7_steam_net_p25']:.2f}｜"
            f"中位 ¥{stats['t7_steam_net_median']:.2f}"
        )
    lines.extend(analysis_reference_lines(
        forecast,
        risk,
        current_steam_net=snapshot.steam_net,
        t7_steam_net_p25=stats.get("t7_steam_net_p25"),
    ))
    lines.extend([
        "数据更新时间："
        f"{snapshot.source_updated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"SMIS：https://smis.club/commodity/{snapshot.smis_id}",
        "Steam：https://steamcommunity.com/market/listings/"
        f"{snapshot.appid}/{quote(snapshot.name)}",
    ])
    return lines


def format_breakthrough_alert(
    snapshot: MarketSnapshot,
    stats: dict,
    forecast: dict,
    risk: dict,
    rule: dict,
    value: float,
    depth: float,
    level: int,
) -> tuple[str, str]:
    definition = get_rule_definition(str(rule["rule_type"]))
    if definition is None:
        raise ValueError(f"不支持的规则类型：{rule['rule_type']}")
    value_text = definition.format_value(value)
    threshold_text = definition.format_threshold(float(rule["threshold"]))
    lines = [
        f"规则 #{int(rule['id'])} · {snapshot.name_zh} / {snapshot.name}",
        f"{definition.metric_label}：{value_text}（阈值 {threshold_text}）",
        f"相对阈值继续突破：{depth:.2%}，达到第 {level} 档",
    ]
    lines.extend(market_reference_lines(snapshot, stats, forecast, risk))
    return f"【继续突破·第 {level} 档】{snapshot.name_zh} {value_text}", "\n".join(lines)


def format_rule_alert(
    snapshot: MarketSnapshot,
    stats: dict,
    forecast: dict,
    risk: dict,
    rule: dict,
    value: float,
) -> tuple[str, str]:
    definition = get_rule_definition(str(rule["rule_type"]))
    if definition is None:
        raise ValueError(f"不支持的规则类型：{rule['rule_type']}")
    value_text = definition.format_value(value)
    threshold_text = definition.format_threshold(float(rule["threshold"]))
    lines = [
        f"规则 #{int(rule['id'])} · {snapshot.name_zh} / {snapshot.name}",
        f"{definition.metric_label}：{value_text}（阈值 {threshold_text}）",
    ]
    lines.extend(market_reference_lines(snapshot, stats, forecast, risk))
    return f"{definition.alert_prefix}{snapshot.name_zh} {value_text}", "\n".join(lines)
