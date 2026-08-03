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
/skin recommend [最高价] [最低价] [最低Steam日成交量]
                                            确定性综合挂刀 Top 5
/skin ai quote <SMIS_ID|名称>                AI 解读单品确定性结果
/skin ai recommend [最高价] [最低价] [最低Steam日成交量]
                                            AI 解读确定性 Top 5
/skin ai compare <ID1,ID2[,ID3...]>         AI 比较 2～5 个 SMIS ID
/skin ai eval [7|30|90|180] [all|lt100|100-999|1000+]
                                            管理员查看并解读预测评估
/skin ai status                             查看本会话 AI 状态和额度
/skin items                                查看已监控饰品
/skin rule list [SMIS_ID]                  查看当前会话规则
/skin rule add <目标> <ratio|t7|platform|steam> <阈值>
/skin rule set <RULE_ID> <新阈值>
/skin rule remove <RULE_ID>
/skin test                                 测试主动推送
/skin status                               查看插件运行状态
/skin help                                 显示帮助
```

`search`、`quote`、`recommend`、`ai quote`、`ai recommend`、`ai compare`、
`ai status`、`items`、`rule list` 和 `help` 对普通用户开放；`ai eval`、修改规则、
推送测试和状态管理要求 AstrBot 管理员权限。规则和 AI 调用额度均按
`unified_msg_origin` 隔离。

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
| `recommend_min_price` | `1` | 推荐候选最低平台买入价，也是命令价格下限的配置边界 |
| `recommend_max_price` | `5000` | 推荐候选最高平台买入价，也是命令预算的配置上限 |
| `recommend_max_ratio_percent` | `80` | 推荐候选最高即时挂刀比例 |
| `recommend_min_steam_volume` | `50` | 推荐候选默认最低 Steam 日成交量；命令可临时覆盖至最低 1 |
| `recommend_max_age_minutes` | `120` | 推荐候选最大行情年龄 |
| `llm_analysis_enabled` | `false` | 是否开放 `/skin ai` 模型解读；确定性命令不受影响 |
| `llm_calls_per_window` | `3` | 每个会话在限流窗口内最多发起的模型调用数 |
| `llm_rate_window_seconds` | `600` | AI 会话限流窗口 |
| `llm_timeout_seconds` | `60` | 单次 AI Provider 调用超时 |
| `llm_max_concurrency` | `2` | 全插件并发 AI 调用上限 |

配置不合法时插件不会启动后台任务，`/skin status` 会列出需要修复的字段。

## 确定性挂刀推荐

`/skin recommend` 从 SMIS 当前挂刀候选中最多读取 50 项，不限制 CS，以上游实际
返回的游戏范围为准。命令参数依次为最高价、最低价和最低 Steam 日成交量，例如
`/skin recommend 500 1 5`。单参数仍表示最高价；价格只能收紧配置范围，成交量可
临时覆盖配置默认值但不能低于 1。

候选必须具备有效且未过期的行情、平台与 Steam 实际在售、至少一笔 Steam 日成交，
并满足本次价格、成交量和即时比例筛选。系统按第 5 名即时比例动态选择 6、8、10
或 12 项进行历史分析，再使用与 `/skin quote` 相同的 T+7 预测和四维风险计算。
最终分数由 T+7 P50 比例 40%、P20 压力比例 35% 和即时比例 25% 组成，并扣除中高风险、
低置信度及较旧历史缓存的分数；成交量在用户门槛之外通过风险维度影响排名，不再
重复设置独立权重。

每次推荐最多新增 8 次历史请求，30 天原始历史会写入 SQLite 复用；6 小时内无需
更新，6～24 小时可低时效使用并限量刷新，超过 24 小时必须更新。推荐缓存 14 天
未使用或超过 200 项时按最久未使用清理，监控规则正在使用的共享历史不受影响。
历史、预测或风险不足时不会用不可靠数据补足 Top 5。购买前仍需核对实际挂单、
磨损/印花等具体属性和七天交易冷却。

推荐策略标识为 `t7_risk_instant_score_v3`。每个结果同时生成符合
[`recommendation_candidate.v1`](docs/recommendation_candidate.v1.schema.json)
的结构化记录，保存市场观测、21 日日序列、P20/P50/P80、风险、确定性分项与扣分。
结构供大模型解读使用，但模型只能解释和比较确定性 Top N，不能改变准入或分数。

## AI 只读解读

AI 功能默认关闭。启用 `llm_analysis_enabled` 后，插件复用当前 AstrBot 会话已经选择的
Provider，不保存 API Key，也不增加独立模型配置。`/skin ai quote`、`recommend` 和
`compare` 会先完整展示确定性行情、预测、风险或排名，再附加模型解读；Provider
不可用、超时、限流或输出校验失败时，仅降级为确定性结果，不影响普通命令。

模型收到的是 `skin_llm_analysis_request.v1` 结构化数据，不包含会话 recipient，且不
携带聊天历史。输出必须包含“结论、主要依据、不确定性”三段，并至少引用两个真实字段
路径。模型只能解释、比较与总结，不能改写候选准入、40/35/25 分数、P20/P50/P80、
风险等级、规则或告警，也不会触发交易。相同 Provider 与相同输入缓存 10 分钟；调用
记录和模型输出只保存在内存，不写入数据库。

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

数据库和备份位于 AstrBot 的 `data/plugin_data/astrbot_plugin_steam_skin_ops/`。
规则、行情和通知继续使用 schema v8 的 `monitor.db`；监控饰品的 T+7 真实到期评估
使用独立 schema v1 的 `forecast_evaluation.db`。两个数据库均每日在线备份。更新插件
不会覆盖该目录；卸载插件前如需保留规则，请先备份该目录。插件不读取或迁移旧版
独立服务的 `monitor.db`。

默认最多监控 20 个饰品、每 30 分钟轮询一次。纯插件版省去独立 Python/Uvicorn 容器，但行情计算与 SQLite 负载会计入 AstrBot 进程。

## 数据口径与风险提示

- `ratio`：最低第三方平台价 ÷ 当前 Steam 预计到手价。
- `t7`：最低第三方平台价 ÷ 动态模型预测的七天后 Steam 预计到手价；触发比例与行情查询展示的“预测倒余额比例”一致。
- `platform`：最低第三方平台价达到买入目标。
- `steam`：Steam 展示售价达到库存清理目标。

七日预测保留 SMIS 原生时间戳并按 UTC 日聚合，以 exact-H7 回测选择持平、近期水平、
稳健线性或稳健对数模型，输出 P20/P50/P80、上涨概率、趋势状态和区间来源。
`t7` 使用 P50；风险底价使用 P20，并按当前成交量 `<100`、`100–999`、`≥1000`
分层判断波动，以多信号规则聚合总体风险。数据不足进入质量状态，不会产生虚假低风险
或交易告警。风险评估仅供参考，不参与四类规则的基础阈值判断。

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
