# 架构现状（As-Built Architecture）

| 项 | 值 |
|---|---|
| 版本 | v13（策略口径）/ 文档 2026-10-31 |
| 状态 | **当前实现的事实基准**；`docs/design.md` 为 2026-10-11 的初始设计稿，已被本文覆盖 |
| 关联 | `docs/requirements.md`（愿景与边界）｜`docs/rule-experiments.md`（策略演进）｜`CHANGELOG.md`（版本内容） |
| 规模 | 生产代码 ≈ 13,800 行；测试 ≈ 6,400 行 / 42 文件 / 281 用例 |

> 本文只描述**代码现在做什么**。需求意图看 `requirements.md`，设计权衡的原始推演看 `design.md`。
> 三者冲突时，以本文 + 代码为准。

## 1. 系统定位

单用户、单机、常驻的 **A 股投资纪律系统**，分三层能力：

1. **买什么**：全市场量化初筛 → 买点评估（回踩/突破/超跌）→ LLM 分析卡 → 结构化操作计划；
2. **怎么卖**：强制退出规则 + 分钟级盘中监控 + 双通道提醒（邮件/微信）；
3. **怎么改**：交易日志 → 清仓复盘 → 经验注入提示词；预测校准 + 历史回放 → 独立于"感觉"的策略迭代。

明确不做：自动下单（实盘为**半自动**：生成指令单，人工在东财下单后回填）、收益承诺、多用户、港股美股。

## 2. 运行时形态

单进程、双入口：`invest-assistant serve` 同时启动 **FastAPI 面板** 与 **APScheduler 调度器**，共享同一 SQLite（WAL）。
Windows 上用 NSSM 注册为服务（回退计划任务），开机自启 + 崩溃重启。面板默认仅 `127.0.0.1`。

```
浏览器 ──▶ FastAPI（Jinja2 + htmx + ECharts 本地 vendor，可选密码）
                │
        ┌───────┴────────┐
        ▼                ▼
   domain 服务层      调度作业（进程内，每分钟）
   持仓/规则/计划      轮询 → 提醒落库 → 解读 → 投递
        │                │
        ▼                ▼
   SQLite(WAL)      数据源（东财→腾讯→新浪，缓存/重试/显式降级）
                    LLM（DeepSeek，JSON 模式 + pydantic 校验）
                    通知（SMTP + Server酱）
```

## 3. 模块地图

| 模块 | 规模 | 职责 | 关键入口 |
|---|---|---|---|
| `engine/` | 5,003 + 实盘执行器 | 调度、轮询、模拟盘、实盘、回放、校准、作业 | `scheduler.build_scheduler`、`poller.Poller.run_once`、`live_app.AppExecutor` |
| `datasource/` | 1,556 | 行情/日线/净值/财务/公告/资金面/大盘环境，多源兜底 | `service.MarketDataService` |
| `web/` | 1,354 + 模板 976 | 页面（11 个）、JSON API、可选密码 | `app.create_app`、`pages`、`api` |
| `domain/` | 964 | 持仓与交易、**规则引擎（实盘/回测共用）**、计划解析、指标、日志 | `rules.evaluate`（纯函数）、`positions.PositionService` |
| `llm/` | 824 | 分析卡、触发解读、盘中否决、提示词、用量 | `analyzer.CardAnalyzer`、`veto.VetoReviewer` |
| `screening/` | 552 | 初筛模板、执行、买点评估、经验校准 | `runner.screen_and_analyze` |
| `db/` | 417 + 迁移 664 | 29 张表、会话工厂、Alembic、备份 | `db.models` |
| `backtest/` | 395 | 规则级回测（日线事件循环、费用、统计） | `engine.run_backtest` |
| `notify/` | 324 | 双通道分发、重试、投递记录、消息模板 | `dispatcher.Dispatcher` |
| `reports/` | 122 | 周报 | `weekly.weekly_report` |

单体内部按"**引擎编排 / 领域纯逻辑 / 适配器隔离**"分层，没有服务间调用，没有消息队列。

## 4. 数据模型（29 张表）

| 组 | 表 |
|---|---|
| 标的与行情 | `instrument` `daily_bar` `fund_nav` `financial_snapshot` `news_item` `trading_calendar` |
| 持仓与交易 | `position` `trade` `dividend` `corporate_action` `exit_rule` `order_intent` |
| 自选与提醒 | `watchlist_item` `alert` |
| 分析与预测 | `analysis_card` `llm_usage` `prediction` `calibration_report` |
| 选股 | `screen_template` `candidate` |
| 模拟盘 | `paper_position` `paper_trade` `paper_report` `paper_daily` |
| 复盘 | `trade_journal` `trading_lesson` |
| 回测与运维 | `backtest_run` `job_run` `kv_setting` |

约定：时间统一为 **Asia/Shanghai 本地 naive datetime**；金额/价格用 `Float`；JSON 列存结构化上下文（`context_json` / `state_json` / `content_json`）。
幂等靠 `alert.dedup_key` 唯一约束；作业可观测性靠 `job_run`（每次执行一行，含状态与详情）。

## 5. 调度作业（进程内，`Asia/Shanghai`）

| 时间 | 作业 id | 内容 |
|---|---|---|
| 交易日 9:30–15:00 每 5 分钟 | `market_regime` | 刷新强弱市状态（双指数 + ±1% 滞回） |
| 交易日 9–15 每 `poll.interval_minutes` 分钟 | `poll_quotes` | 持仓规则 + 自选到价 + 模拟盘撮合 / 纪律止损（本地判断） |
| 每 1 分钟（全天） | `notify_pending` | 消费 `pending` 提醒：LLM 解读 + 邮件/微信投递 |
| 交易日 15:30 | `eod_close` | 收盘体检 + AI 操作建议 |
| 交易日 15:40 | `paper_review` | 模拟盘估值 + AI 日报（次日开盘执行） |
| 交易日 18:30 | `screen_candidates` | 初筛 → 买点评估 → 批量分析卡 → 汇总推送 |
| 周六 10:00 | `weekly_report` | 周报 |
| 周六 10:15 | `calibration` | 预测校准（首次回填近 90 天） |
| 周日 23:00 | `housekeeping` | 数据清理 |
| 每日 23:30 | `backup` | 数据库备份（保留 30 份） |
| 每 1 分钟 | `heartbeat` | 调度心跳（看门狗据此判断停摆） |
| 启动 +10s / +25s | `catchup_on_start` `startup_compensation` | 补报 / 错过任务补偿 |

所有作业 `max_instances=1 + coalesce=True` 防重入；`JobRunner` 统一写 `job_run`。

## 6. 关键链路

### 6.1 盘中监控（骨架）

```
tick → 交易日/时段校验 → 加载持仓 + 自选 + 模拟盘持仓
     → 批量行情（东财→腾讯→新浪，缓存 TTL 内复用）
     → 逐规则 evaluate()（纯函数；持仓用 last price，回测用 high/low）
     → 命中：写 alert（dedup_key 幂等）+ 更新规则状态（锁存/复位）
     → 降级：数据源全失败 → 连续失败计数 → 系统告警（≥3 轮首告，每 24 轮复提醒）
```

工程约定：**轮询本身不调用 LLM**（`poller.py` 模块文档明示"0 LLM 调用"）。解读与投递由独立的通知作业消费 `alert.status=pending`，两条链路互不阻塞。

### 6.2 规则引擎（实盘/回测共用）

5 类规则：`fixed_take_profit` / `fixed_stop_loss` / `trailing_stop` / `daily_move` / `max_holding`。
`evaluate(rule_type, params, state, ctx) -> (new_state, Trigger|None)` 为**纯函数**，不触库：

- 锁存类（止盈/止损/移动止盈/持有到期）触发后不再重复，人工复位才重新武装；
- `daily_move` 按交易日自动复位；
- 回测语义：止盈用当日 high、止损用 low、trailing 先更新峰值再判定；同日双触发按止损优先。

同一份代码服务实盘监控与回测，避免"回测赚钱是因为两边逻辑不同"。

### 6.3 分析 → 计划 → 执行

LLM 分析卡除结论外产出结构化 `plan`（买入区间 / 确认条件 / 失效价 / 目标 / 仓位），由 `domain/plan.py`
解析为机器可读价位，再由模拟盘在盘中按枚举确认条件（缩量/放量/收阳/站上均线）逐分钟判定后才自动买入。
把自然语言建议转成**可回放、可统计**的信号，是后续校准与回放的数据基础。

### 6.4 AI 与纪律的边界（重要设计约束）

| 场景 | AI 权限 | 理由 |
|---|---|---|
| 分析卡 / 收盘体检 / 模拟盘日报 | 生成内容 | 失败降级为模板或跳过 |
| 每次模拟盘买卖前复核 | **否决制**，只能取消不能发起 | 模型不能越过纪律创造交易 |
| 纪律止损（跌破失效价/机械止损） | **豁免复核**，直接执行 | 保护性卖出优先于模型意见 |
| 自选到价提醒 | 不参与（轻量模板） | 成本与时效 |

### 6.5 复盘的三个闭环

1. **交易日志 → 经验**：每笔操作连同"当时情况"入库；清仓后用 `llm.deep_model` 复盘出 2–4 条教训，注入后续提示词；
2. **预测校准**：分析卡建议 + 当时环境入库为 `prediction`，20 天后结算触发率/5/10/20 日表现/失效价正确性，按「建议类型 × 大盘环境」统计"在什么条件下容易错"；
3. **历史回放**：`playbook_replay` 把策略的确定性部分在近 2 年日线（可扩展到样本外 150 只）上重演，产出分形态胜率，避免等实盘慢慢积累样本。

三者共同构成"用历史成绩校准未来判断"，且全部有落库证据可回查。

### 6.6 可靠性兜底

心跳看门狗（停摆 >5 分钟重建调度器，重建失败 `os._exit(1)` 交给服务守护）、启动补报、错过任务补偿、每日备份、`dedup_key` 幂等。

## 7. 配置与密钥

- `config/config.yaml`（版本化）：轮询/阈值/费用/模拟盘/实盘/市场判定等非密钥参数；
- `data/overrides.yaml`：设置页在线修改的运行参数，启动时**叠加**在 config.yaml 之上（重启生效）；
- `.env`（gitignore）：`DEEPSEEK_API_KEY` / `SMTP_*` / `SERVERCHAN_SENDKEY` / `PANEL_PASSWORD`；
- 面板仅本机绑定；开启密码后走签名 Session Cookie（`SameSite=Lax`）。
- **启动护栏**（`guards.py`）：① 本机 UTC 偏移非 +08:00 时显式告警（否则交易时段/持有天数会静默漂移）；
  ② `--host` 指向非回环地址却未设 `PANEL_PASSWORD` 时**拒绝启动**。

## 8. 已知债务

| 级 | 项 | 现状 |
|---|---|---|
| 高 | 文档滞后 | 需求/设计停留在 v0.2/v0.1，本文是为了补上这个缺口 |
| 中 | 巨型聚合 | `engine/poller.py` 902 行（纯判定已抽到 `engine/triggers.py`），仍同时承担持仓规则/自选/模拟盘/实盘镜像/强弱市编排；`engine/paper.py` 948 行未拆 |
| 中 | 时间不带时区 | 全项目 naive `datetime.now()`，跨时区或误改系统时区会静默漂移（已有启动护栏，但未根治） |
| 中 | 新闻与财务不落库 | `news_item` / `financial_snapshot` 长期为空：新闻用完即弃、回测的基本面模板无法做时点过滤 |
| 低 | 成本可观测性 | `llm_usage` 只有 tokens/cost，无延迟与失败原因 |
| 低 | 静态检查 | 已配 ruff（E/F/W/I/B/UP，E501 因中文注释不强制）；仍无 mypy 与 CI |

## 9. 变更历史

| 文档版本 | 日期 | 说明 |
|---|---|---|
| v1 | 2026-10-31 | 首次建立 as-built 文档：覆盖模拟盘、半自动实盘、回放、校准、强弱市等设计稿之后新增的能力 |
