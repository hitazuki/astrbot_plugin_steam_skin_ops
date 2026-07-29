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
/skin recommend [预算上限]                 确定性即时挂刀 Top 5
/skin items                                查看已监控饰品
/skin rule list [SMIS_ID]                  查看当前会话规则
/skin rule add <目标> <ratio|t7|platform|steam> <阈值>
/skin rule set <RULE_ID> <新阈值>
/skin rule remove <RULE_ID>
/skin test                                 测试主动推送
/skin status                               查看插件运行状态
/skin help                                 显示帮助
```

`search`、`quote`、`recommend`、`items`、`rule list` 和 `help` 对普通用户开放；修改规则、推送测试和状态管理要求 AstrBot 管理员权限。规则使用 `unified_msg_origin` 隔离，不同会话只能查看和修改自己的规则。

比例规则的阈值使用百分数，例如 `72` 表示 `72%`；价格规则使用人民币金额。

## 配置

| 配置 | 默认值 | 说明 |
| --- | ---: | --- |
| `enabled` | `true` | 是否启动后台轮询与每日汇总 |
| `interval_seconds` | `1800` | 行情轮询间隔 |
| `quote_cache_seconds` | `60` | 即时报价缓存时间 |
| `max_items` | `20` | 全插件最多监控饰品数 |
| `max_workers` | `2` | 单轮采集并发，范围 1-8 |
| `breakthrough_step_percent` | `3` | 向有利方向每次进入新档位时通知；回落后重新进入同一档也会再次通知 |
| `daily_summary_time` | `09:00` | 北京时间每日汇总时间 |
| `smis_timeout_seconds` | `15` | 上游请求超时 |
| `smis_max_retries` | `3` | 临时错误最大尝试次数 |
| `smis_min_request_interval_seconds` | `1` | 全局请求最小间隔 |
| `recommend_min_price` | `30` | 推荐候选最低平台买入价 |
| `recommend_max_price` | `5000` | 推荐候选最高平台买入价，也是命令预算的配置上限 |
| `recommend_max_ratio_percent` | `80` | 推荐候选最高即时挂刀比例 |
| `recommend_min_steam_volume` | `50` | 推荐候选最低 Steam 日成交量 |
| `recommend_max_age_minutes` | `120` | 推荐候选最大行情年龄 |

配置不合法时插件不会启动后台任务，`/skin status` 会列出需要修复的字段。

## 确定性挂刀推荐

`/skin recommend` 从 SMIS 当前挂刀候选中最多读取 50 项，在本地重新计算 Steam
预计到手价和即时挂刀比例，然后返回最多 5 项。可以提供一次性的预算上限，
例如 `/skin recommend 500`；该金额只会收紧配置中的最高买入价，不会突破配置
上限。

首版只保留同时满足以下条件的候选：CS2 饰品、上游时间有效且未过期、最低价
对应平台在售数大于 0、Steam 在售数大于 0、日成交量和价格范围达标、即时比例
不高于配置阈值。排序依次使用即时比例升序、库存消化天数升序、Steam 日成交量
降序、平台在售量降序和数据更新时间降序；完全相同时按 SMIS ID 排序。符合条件
的候选不足 5 项时不会使用缺失、过期或零在售数据补足。

该推荐是即时行情候选，不读取账户、库存或交易记录，也尚未纳入 T+7 预测和四维
风险评级。购买前仍需核对实际挂单、磨损/印花等具体属性和七天交易冷却。

## 数据来源

插件当前唯一的远程数据上游是 [SMIS](https://smis.club/) 聚合接口。搜索结果、商品元数据、当前报价、平台挂单数量、Steam 在售与成交量以及历史行情均从 SMIS 获取；插件不直接请求 Steam、BUFF、悠悠有品、C5、IGXE 或 ECO 官方接口。

| 数据层 | 来源及用途 |
| --- | --- |
| 饰品搜索与身份 | SMIS 提供饰品 ID、AppID、市场哈希名、中文名和稀有度 |
| 当前市场行情 | SMIS 提供 BUFF、悠悠有品、C5、IGXE、ECO 卖价与挂单数量，以及 Steam 售价、在售数量和日成交量 |
| 历史行情 | SMIS 提供最多 30 天的 BUFF 与 Steam 价格、库存和成交量序列，用于历史补全与本地分析 |
| 挂刀候选 | SMIS 批量候选用于本地可交易性过滤和确定性 Top 5 排序 |
| 本地派生数据 | 插件计算 Steam 预计到手价、最低平台价、各种比例、七日预测、风险评级、推荐排序和告警状态 |

平台名称表示 SMIS 返回字段所对应的市场，并不代表插件取得了相应平台的官方实时数据。上游采集频率、原始口径、延迟和字段可用性由 SMIS 控制；异常或缺失会影响查询、告警、预测和风险结果。插件不读取 Steam 账户、交易记录、用户库存或其他个人资产数据。

具体接口、字段映射、时间戳和本地计算方法见[《数据来源、七日预测与风险计算规则》](docs/DATA_SOURCES_AND_METHODS.md)。

## 数据与存储

数据库和备份位于 AstrBot 的 `data/plugin_data/astrbot_plugin_steam_skin_ops/`。更新插件不会覆盖该目录；卸载插件前如需保留规则，请先备份该目录。插件不读取或迁移旧版独立服务的 `monitor.db`。

默认最多监控 20 个饰品、每 30 分钟轮询一次。纯插件版省去独立 Python/Uvicorn 容器，但行情计算与 SQLite 负载会计入 AstrBot 进程。

## 数据口径与风险提示

- `ratio`：最低第三方平台价 ÷ 当前 Steam 预计到手价。
- `t7`：最低第三方平台价 ÷ 动态模型预测的七天后 Steam 预计到手价；触发比例与行情查询展示的“预测倒余额比例”一致。
- `platform`：最低第三方平台价达到买入目标。
- `steam`：Steam 展示售价达到库存清理目标。

七日预测会根据数据覆盖、回测误差和趋势诊断动态选择持平、近期水平、稳健线性或稳健对数模型，并用于 `t7` 规则；价格、波动、库存、成交量风险评估仅供参考，不参与四类规则的基础阈值判断。第三方行情可能延迟、缺失或发生口径变化；本插件不构成投资建议，使用者应独立判断交易风险。

预测的数据预处理、候选模型、回测选择、置信度以及价格、波动、库存、成交量四维风险阈值，详见[《数据来源、七日预测与风险计算规则》](docs/DATA_SOURCES_AND_METHODS.md)。

## 开发

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
ruff check .
python -m compileall -q main.py core
python scripts/validate_market_package.py
```

提交信息和 PR 标题使用带 scope 的 Conventional Commits，例如
`feat(rules): use dynamic forecast for t7 alerts`。新克隆可执行
`git config core.hooksPath .githooks` 启用本地提交校验；完整开发与发布约定见
[`AGENTS.md`](AGENTS.md)。

MIT License。行情与风险核心重构自 [steam-skin-ops](https://github.com/hitazuki/steam-skin-ops)。
