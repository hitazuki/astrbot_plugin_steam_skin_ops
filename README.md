# astrbot_plugin_steam_skin_ops

Steam 饰品行情、风险评估与按会话监控插件。行情采集、SQLite 存储和定时任务全部运行在 AstrBot 进程内，不需要额外部署 API 服务或 Docker 容器。

## 安装

在 AstrBot WebUI 插件页通过仓库地址安装：

```text
https://github.com/hitazuki/astrbot_plugin_steam_skin_ops
```

要求 AstrBot `>=4.16,<5`。安装后在插件配置中检查轮询间隔、监控数量和每日汇总时间，然后重载插件。

## 命令

```text
/skin search <名称>                         搜索 SMIS 饰品 ID
/skin quote <SMIS_ID|名称>                 查询全市场报价和风险
/skin items                                查看已监控饰品
/skin rule list [SMIS_ID]                  查看当前会话规则
/skin rule add <目标> <ratio|t7|platform|steam> <阈值>
/skin rule set <RULE_ID> <新阈值>
/skin rule remove <RULE_ID>
/skin test                                 测试主动推送
/skin status                               查看插件运行状态
/skin help                                 显示帮助
```

`search`、`quote`、`items`、`rule list` 和 `help` 对普通用户开放；修改规则、推送测试和状态管理要求 AstrBot 管理员权限。规则使用 `unified_msg_origin` 隔离，不同会话只能查看和修改自己的规则。

比例规则的阈值使用百分数，例如 `72` 表示 `72%`；价格规则使用人民币金额。

## 配置

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `enabled` | `true` | 是否启动后台轮询与每日汇总 |
| `interval_seconds` | `1800` | 行情轮询间隔 |
| `quote_cache_seconds` | `60` | 即时报价缓存时间 |
| `max_items` | `20` | 全插件最多监控饰品数 |
| `max_workers` | `2` | 单轮采集并发，范围 1-8 |
| `breakthrough_step_percent` | `3` | 继续向有利方向变化后的再次通知档位 |
| `daily_summary_time` | `09:00` | 北京时间每日汇总时间 |
| `smis_timeout_seconds` | `15` | 上游请求超时 |
| `smis_max_retries` | `3` | 临时错误最大尝试次数 |
| `smis_min_request_interval_seconds` | `1` | 全局请求最小间隔 |

配置不合法时插件不会启动后台任务，`/skin status` 会列出需要修复的字段。

## 数据与资源

数据库和备份位于 AstrBot 的 `data/plugin_data/astrbot_plugin_steam_skin_ops/`。更新插件不会覆盖该目录；卸载插件前如需保留规则，请先备份该目录。插件不读取或迁移旧版独立服务的 `monitor.db`。

默认最多监控 20 个饰品、每 30 分钟轮询一次。纯插件版省去独立 Python/Uvicorn 容器，但行情计算与 SQLite 负载会计入 AstrBot 进程。

## 数据口径与风险提示

- `ratio`：最低第三方平台价 ÷ 当前 Steam 预计到手价。
- `t7`：最低第三方平台价 ÷ 过去七日 Steam 预计到手价 P25。
- `platform`：最低第三方平台价达到买入目标。
- `steam`：Steam 展示售价达到库存清理目标。

七日预测和价格、波动、库存、成交量风险评估仅供参考，不参与四类规则的基础阈值判断。第三方行情可能延迟、缺失或发生口径变化；本插件不构成投资建议，使用者应独立判断交易风险。

## 开发

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
ruff check .
```

MIT License。行情与风险核心重构自 [steam-skin-ops](https://github.com/hitazuki/steam-skin-ops)。
