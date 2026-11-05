# 技术设计文档：AI 投资分析助手

| 项 | 值 |
|---|---|
| 版本 | v0.1（待评审） |
| 日期 | 2026-10-11 |
| 关联需求 | `docs/requirements.md` v0.2 |
| 总体结论 | 单进程 Python 应用：FastAPI（Web）+ APScheduler（调度）+ SQLite（存储），规则引擎实盘/回测双用，DeepSeek 负责分析与解读 |

> ⚠️ **文档状态：历史基线（2026-10-11 初始设计）**
>
> 实现已明显超出本文范围（新增模拟盘、半自动实盘、历史回放、预测校准、强弱市、复盘闭环、29 张表）。
> **当前架构事实以 [`docs/architecture.md`](architecture.md) 为准**；本文保留的价值是记录当初的选型权衡与理由。
> 需求意图见 [`docs/requirements.md`](requirements.md)，版本内容见 [`CHANGELOG.md`](../CHANGELOG.md)。

## 1. 设计目标与约束

- 依据需求 v0.2，给出可直接指导开发的架构、选型、数据模型、关键流程与实施计划。
- 约束：个人自用、单用户；Windows 电脑常开；免费数据源优先；DeepSeek（OpenAI 兼容接口）；面板仅本机访问；不做自动交易。

## 2. 总体架构

```
┌──────────────────────────── 本机（Windows，常开） ────────────────────────────┐
│                                                                              │
│   ┌──────────┐      ┌──────────────────────────┐    ┌──────────────────┐     │
│   │  浏览器   │─────▶│  Web 层（FastAPI）        │    │  调度器           │     │
│   │  面板     │      │  Jinja2 + htmx + ECharts │    │  (APScheduler)   │     │
│   └──────────┘      └────────────┬─────────────┘    └────────┬─────────┘     │
│                                  │                           │               │
│                                  ▼                           ▼               │
│   ┌─────────────────────────────────────────────────────────────────────┐    │
│   │                        应用核心（domain）                            │    │
│   │  持仓服务 │ 规则引擎 │ 轮询管线 │ 补报/追赶 │ 收盘报告 │ 回测 │ 初筛   │    │
│   └──────┬───────────────┬──────────────────┬───────────────┬──────────┘    │
│          ▼               ▼                  ▼               ▼               │
│   ┌────────────┐  ┌──────────────┐  ┌────────────┐  ┌────────────────┐      │
│   │ SQLite      │  │ 数据源层      │  │ LLM 层      │  │ 通知层          │      │
│   │ SQLAlchemy  │  │ AkShare/新浪  │  │ DeepSeek    │  │ 邮件 + 微信     │      │
│   │ + Alembic   │  │ 缓存/重试/降级│  │ (可切换)    │  │ (Server酱)      │      │
│   └────────────┘  └──────┬───────┘  └────────────┘  └────────────────┘      │
│                          ▼                                                   │
│                免费数据源（东方财富 / 新浪 / 天天基金 …）                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

**模块职责**

| 模块 | 职责 |
|---|---|
| web | 面板页面与 HTTP API；可选密码认证；静态资源本地化 |
| engine | 调度、盘中轮询、补报/追赶、收盘任务；串联数据源→规则→LLM→通知 |
| domain | 持仓/交易/分红服务；规则引擎（与回测共用） |
| datasource | 行情/净值/财务/公告/新闻/交易日历；缓存、重试、降级标注 |
| llm | 分析卡与触发解读；提示词版本管理；用量与成本统计 |
| notify | 邮件 + 微信双通道；投递记录与重试 |
| backtest | 规则级回测：初筛模板 + 退出规则；统计与报告 |
| screening | 初筛模板（价值/成长/趋势）与候选池生成 |
| reports | 收盘体检、次日计划、周报 |

## 3. 关键技术选型（方案对比）

| 决策点 | 候选 | 结论 | 理由 |
|---|---|---|---|
| 进程架构 | A. 单进程（Web+调度） B. Web/Worker 分离 C. 纯脚本+系统计划任务 | **A** | 单用户、单机；只需守护一个进程；共享内存态与代码。重任务（回测/LLM）用后台任务+线程池，不在请求内同步执行。触发升级 B 的条件：多用户或多机部署 |
| Web 框架 | FastAPI / Flask / Django | **FastAPI** | 原生 async（大量 IO 等待：数据源、LLM、推送）；pydantic 校验；TestClient 便于测试；自动 OpenAPI |
| 前端 | Jinja2 + htmx + ECharts / Vue SPA | **Jinja2 + htmx** | 免 npm 构建链，个人工具迭代最快；图表用 ECharts（本地 vendor，避免 CDN 不稳）。交互复杂化再考虑 SPA |
| 存储 | SQLite / PostgreSQL / DuckDB | **SQLite（WAL）** | 单用户零运维；文件即备份；SQLAlchemy 保证未来可迁移 Postgres。回测数据规模（日线）SQLite 足够 |
| 调度 | APScheduler / Windows 计划任务 / Celery | **APScheduler（进程内）** | cron/interval、misfire 容错、coalesce 防重入；交易日窗口在作业内判断。系统层只需注册一个「开机自启 serve」 |
| 数据源 | AkShare / Tushare Pro / 直连爬取 | **akshare（基金/资讯/财务/日历）+ 东财/新浪直连（行情）** | S1 实测：akshare 东财接口不可用、直连可用且快；Adapter 隔离 + 健康探测，可切换 |
| LLM | DeepSeek / Qwen / OpenAI | **DeepSeek（deepseek-chat）** | 需求确认；OpenAI 兼容 SDK，`LLMClient` 抽象可切换；JSON 输出模式保证结构化 |
| 回测 | 自研轻量 / backtrader / vectorbt / qlib | **自研（pandas）** | 核心诉求是「初筛+退出规则」模拟而非通用策略框架；**与实盘共用同一规则评估器，语义完全一致**；数据规模小。策略复杂化后再评估 vectorbt |
| 打包与 CLI | uv / pip+venv；typer / argparse | **pip + venv；typer** | 已确认使用 pip；pyproject.toml 管理依赖（`pip install -e ".[dev]"`）；CLI 提供 `serve / once <job> / backup` 等子命令 |
| 服务守护 | NSSM / 任务计划程序 | **NSSM 推荐，任务计划程序备选** | 注册为 Windows 服务：开机自启、崩溃自动重启；安装脚本随项目提供 |

**明确不引入**：Redis、Celery、消息队列、Docker（个人单机场景均为过度设计）；未来需要再说。

## 4. 项目结构

```
invest-assistant/
├── docs/
│   ├── requirements.md          # 需求 v0.2（已定稿候选）
│   └── design.md                # 本文档
├── config/
│   └── config.yaml              # 非密钥配置（版本化）
├── src/invest_assistant/
│   ├── __main__.py              # CLI 入口（typer：serve / once / backup）
│   ├── config.py                # pydantic-settings：config.yaml + .env
│   ├── db/
│   │   ├── models.py            # SQLAlchemy 2.0 模型
│   │   ├── session.py           # 引擎/会话工厂（WAL）
│   │   └── migrations/          # Alembic
│   ├── datasource/
│   │   ├── base.py              # Protocol 与数据结构（Quote/Bar/FundNav/NewsItem）
│   │   ├── akshare_provider.py  # 主数据源
│   │   ├── sina_quotes.py       # 批量实时行情兜底（轻量）
│   │   └── calendar.py          # 交易日历（缓存 + 兜底）
│   ├── domain/
│   │   ├── positions.py         # 交易/持仓/分红/拆股服务
│   │   └── rules.py             # 规则引擎（实盘与回测共用）★
│   ├── engine/
│   │   ├── scheduler.py         # 作业注册
│   │   ├── poller.py            # 盘中轮询
│   │   ├── catchup.py           # 补报/离线漏触发检测
│   │   └── eod.py               # 收盘体检与报告
│   ├── llm/
│   │   ├── client.py            # OpenAI 兼容客户端 + 重试
│   │   ├── analyzer.py          # 分析卡 / 触发解读
│   │   ├── usage.py             # token 与成本统计
│   │   └── prompts/             # 版本化提示词（*.md）
│   ├── notify/
│   │   ├── base.py              # Notifier Protocol + Message
│   │   ├── email.py             # SMTP
│   │   ├── wechat.py            # Server酱 / PushPlus
│   │   └── dispatcher.py        # 双通道投递、重试、记录
│   ├── backtest/
│   │   ├── engine.py            # 日线事件循环
│   │   ├── universe.py          # 标的池来源
│   │   ├── fees.py              # 费用模型
│   │   └── stats.py             # 指标计算
│   ├── screening/
│   │   ├── templates.py         # 价值/成长/趋势模板定义
│   │   └── screener.py          # 初筛执行
│   ├── reports/
│   │   ├── close_report.py
│   │   └── weekly.py
│   └── web/
│       ├── app.py
│       ├── routes/              # API 与页面路由
│       ├── templates/           # Jinja2
│       └── static/              # 本地 vendor：htmx、ECharts
├── tests/
│   ├── fixtures/                # 行情/财报样本数据
│   ├── unit/
│   └── integration/
├── scripts/
│   ├── backup_db.ps1
│   └── install_service.ps1      # NSSM 安装/卸载
├── pyproject.toml
├── .env.example
└── README.md
```

## 5. 数据模型（SQLite，SQLAlchemy 2.0）

> 约定：时间统一存储为 Asia/Shanghai 本地时间；价格/金额用 `Numeric(18,6)`，计算层转 Decimal，展示保留 2~3 位。

| 组 | 表 | 关键字段 | 说明 |
|---|---|---|---|
| 标的 | `instrument` | code(PK), name, type(a_share/etf/otc_fund), exchange, status | 标的字典，自动补全 |
| 持仓 | `position` | id, code(FK), strategy, qty, avg_cost, opened_at, status(open/closed), note | 由交易与分红驱动更新 |
| 持仓 | `trade` | id, position_id(FK), side(buy/sell), qty, price, fee, traded_at | 交易流水（唯一事实源） |
| 持仓 | `dividend` | id, position_id, ex_date, cash_amount | 分红摊薄成本 |
| 持仓 | `corporate_action` | id, position_id, type, detail_json, applied_at | 拆股等手动修正留痕 |
| 规则 | `exit_rule` | id, position_id, type, params_json, state_json, status(armed/triggered/disabled) | 见 6.2 语义 |
| 自选 | `watchlist_item` | code(PK), group, note, added_at | |
| 提醒 | `alert` | id, position_id, rule_id, kind, triggered_at, trigger_price, context_json, interpretation_json, status, is_catchup, delivery_json, dedup_key(UNIQUE) | dedup_key = rule_id+日期+触发情况，保证幂等 |
| 分析 | `analysis_card` | id, code, kind, created_at, model, prompt_ver, content_json, sources_json, tokens_in/out, cost | 当日分析缓存来源 |
| 分析 | `llm_usage` | id, ts, purpose, model, tokens_in, tokens_out, cost_est | 用量看板数据源 |
| 选股 | `screen_template` | id, name, params_json, enabled | 价值/成长/趋势 |
| 选股 | `candidate` | id, date, code, template_id, score, reasons_json, status | 每日候选池 |
| 行情 | `daily_bar` | code+date(PK), OHLCV, amount | 历史缓存（前/后复权标记） |
| 行情 | `fund_nav` | code+date(PK), nav, acc_nav | 场外基金净值 |
| 财务 | `financial_snapshot` | code+report_period(PK), ann_date, metrics_json | 含公告日，供回测时点过滤 |
| 资讯 | `news_item` | id, code, ts, source, title, summary, url, hash(UNIQUE) | hash 去重 |
| 基础 | `trading_calendar` | date(PK), is_open | 交易日历缓存 |
| 基础 | `job_run` | id, job, started_at, finished_at, status, detail | 可观测性 |
| 回测 | `backtest_run` | id, created_at, params_json, status, progress, result_json, error | 异步执行 |
| 基础 | `kv_setting` | key(PK), value_json | 面板里可改的非密钥设置 |

索引要点：`alert(status, triggered_at)`、`candidate(date)`、`daily_bar(code, date)`、`news_item(code, ts)`。

## 6. 核心模块设计

### 6.1 数据源层

```python
class MarketDataProvider(Protocol):
    def get_quotes(self, codes: list[str]) -> dict[str, Quote]: ...          # 批量；含 ts/source/is_delayed
    def get_daily_bars(self, code: str, start: date, end: date) -> list[Bar]: ...
    def get_fund_nav(self, code: str) -> FundNav | None: ...
    def get_fund_estimate(self, codes: list[str]) -> dict[str, FundEstimate]: ...  # 盘中估值，默认关闭
    def get_financials(self, code: str) -> list[FinancialSnapshot]: ...
    def get_news(self, code: str, days: int = 7) -> list[NewsItem]: ...
    def get_trading_calendar(self) -> list[date]: ...
```

- **阻塞调用处理**：AkShare 为同步阻塞库，所有数据源调用经 `asyncio.to_thread` 执行，避免阻塞事件循环。
- **缓存策略**：实时行情 TTL = 轮询间隔；日线当日已取不重取；财务/新闻 6~24h；交易日历按月刷新。缓存入 SQLite，启动可离线回放。
- **重试**：tenacity 指数退避 ×3；仍失败 → 抛 `DataUnavailable`，上层显式降级（告警措辞标注「数据不可用」，**禁止静默用旧数据冒充**）。
- **实现（S1 实测修订）**：行情类（批量实时、日线、全市场快照）以**自建直连适配器**为主——`EastmoneyClient`（push2/push2his/clist）与 `SinaClient`（hq/kline）互为兜底；akshare 保留用于：交易日历、场外基金净值、基金盘中估值（fundgz 直连）、新闻、公告、财务数据。原因：S1 实测 akshare 东财接口在本机网络不可用、直连全部可用且快（详见 `docs/spike-datasource.md`）。数据源健康状态写入 `job_run`，保留源健康探测以便随时切换。

### 6.2 规则引擎（实盘/回测共用 ★）

| 规则类型 | 参数 | 触发语义（两处一致） | 复位语义 |
|---|---|---|---|
| `fixed_take_profit` | price 或 pct | 现价 ≥ 目标价 | 触发后停止；人工处理后可复位 |
| `fixed_stop_loss` | price 或 pct | 现价 ≤ 止损价 | 同上 |
| `trailing_stop` | drawdown_pct | 现价 ≤ 峰值 ×(1−d)；峰值持续更新（初始 = 建仓价） | 同上；复位时峰值重置为当前价 |
| `daily_move` | pct | 相对昨收 |涨跌幅| ≥ pct | 每交易日自动复位 |
| `max_holding` | days | 持有天数 ≥ N → **复核提醒**（非卖出指令） | 人工处理 |

- 评估函数（纯函数，重点单测对象）：

```python
def evaluate(rule: ExitRule, ctx: RuleContext) -> tuple[RuleState, Trigger | None]
```

- **去重（episodes）**：同一规则在「未复位」状态不重复触发；`dedup_key` 唯一约束兜底幂等。
- **回测特有语义**（与实盘书面约定）：止损用当日 low、止盈用当日 high；同日同时触发按**止损优先**（保守）；trailing 先用 high 更新峰值再以 low 判定；跳空用开盘价成交近似。

### 6.3 调度与盘中轮询

**作业表**

| 作业 | 触发时间 | 说明 |
|---|---|---|
| `poll_quotes` | 交易日 09:30–11:30 / 13:00–15:00 每 10 分钟 | 纯本地规则判断，0 LLM 调用 |
| `catchup` | 服务启动时 | 补报 + 离线漏触发检测 |
| `eod_close` | 交易日 15:30 | 收盘体检 + 报告推送 |
| `fund_nav` | 交易日 19:30 | 场外基金净值更新与规则检查 |
| `screen_candidates` | 交易日 18:30 | 初筛 + 候选池 LLM 分析 |
| `weekly_report` | 周六 10:00 | 周报 |
| `backup` | 每日 23:30 | SQLite 备份（保留 30 天） |
| `housekeeping` | 周日 23:00 | 过期数据清理（新闻/提醒/日志按保留策略） |

**轮询管线**（`max_instances=1, coalesce=True` 防重入）：

```
tick → 交易日/时段校验 → 加载 open 持仓与 armed 规则（内存快照）
     → 批量取行情（缓存 TTL 内复用）→ 逐规则 evaluate（更新峰值状态，事务提交）
     → 未触发：结束（0 token）
     → 触发：写 alert → 异步任务：构建上下文 → LLM 解读（失败则模板兜底）
             → Dispatcher 双通道推送 → 记录投递结果 → 面板可见
```

**失败模式**：数据源失败→本轮跳过并记录，连续 N 轮失败→发「系统异常」邮件；LLM 失败→模板消息兜底（标注「AI 解读暂不可用」）；推送失败→重试 2 次，仍失败→面板警示。

### 6.4 LLM 解读与推送

- **调用链**：触发事件 → 组装上下文（持仓、规则、当前盈亏、近 N 日 K 线摘要、相关新闻 ≤8 条、数据时间戳）→ prompt（系统提示固定前缀，利于 DeepSeek 上下文缓存）→ JSON 输出 → pydantic 校验（失败重试 1 次）→ 入库 → 推送。
- **降级链**：LLM 失败 → 模板消息（仍含触发原因/价格/盈亏）→ 不阻塞纪律提醒本身。
- **缓存**：同一标的当日分析卡复用（触发解读基于缓存数据，不重复全量分析）。
- **成本**：每次调用写 `llm_usage`（model/purpose/tokens/成本估算）；面板「用量」页展示当日与近 7 日；软阈值仅提示不降级（需求已确认）。
- **提示词管理**：`llm/prompts/*.md` 文件化 + 版本号；分析卡与触发解读各一套；输出 JSON Schema 写入 prompt 并用 `response_format=json_object` 约束。
- **推送**：`Notifier` 协议 → `EmailNotifier`（SMTP 465/SSL）+ `ServerChanNotifier`（兼容 PushPlus）；Dispatcher 并发发送、记录每通道结果、指数退避重试；消息模板含免责尾注。

```python
class Notifier(Protocol):
    name: str
    async def send(self, msg: Message) -> DeliveryResult: ...
```

### 6.5 补报与追赶（US-4.5 落地）

服务启动（或长时间断线恢复）时执行，两条逻辑：

1. **未送达重发**：扫描 `alert` 中 `status in (pending, failed)` 的记录 → 重新投递；超过时效标注「已过期，请复核」。
2. **离线漏触发检测**：对 armed 规则，取上次检查时间至今的日线 high/low（及当日分时可得时的极值）→ 用同一 `evaluate` 回放 → 生成 `is_catchup=1` 的 alert（标注「延迟补报」）；`dedup_key` 保证不重复补。

### 6.6 收盘任务与报告

- **15:30 收盘体检**：更新 EOD 行情/净值 → 全持仓规则体检（distances 计算）→ LLM 生成体检摘要 + 次日行动清单 → 推送「收盘汇总」。
- **18:30 候选初筛**：执行启用的 `screen_template` → 候选入池（默认 10 只，附入选理由）→ 对候选逐只生成分析卡（顺序执行、间隔限速）→ 汇总推送。
- **19:30 基金净值**：场外基金净值更新 + 规则检查（净值触发）。
- **周六**：周报（持仓变化、触发/执行统计、错过的卖点回看）。

### 6.7 回测引擎（规则级）

- **输入**：区间、标的池（全部A股当前快照 / 指数成分当前 / 自定义列表）、初筛模板、退出规则、费用参数、持仓槽位数（默认 5，等权）。
- **时点正确性**：
  - 财务数据按 `ann_date` 生效；缺失公告日时用「报告期结束 + 45 天」保守滞后（可配），报告披露。
  - 信号在 T 日收盘计算，**T+1 开盘执行**；开盘涨停（买）/跌停（卖）近似不成交并记录。
- **费用模型**：佣金双边（默认万 2.5、最低 5 元）、印花税卖出 0.05%、滑点 0.1%（均可配）；股票/ETF/基金费率模板区分。
- **一致性**：退出判定复用 `domain/rules.py` 同一评估器，回测与实盘语义不分叉。
- **输出**：年化收益、最大回撤、胜率、盈亏比、持有期分布、交易明细、净值曲线 vs 基准（沪深300/中证500）；报告自动附「局限声明」（幸存者偏差、数据覆盖、成交近似）。
- **性能**：日线数据缓存后，N 年 × M 标的在分钟内完成；作为后台任务执行，进度写 `backtest_run.progress`。

### 6.8 Web 面板

| 页面 | 功能 | 主要 API |
|---|---|---|
| 总览 | 持仓概览、当日提醒、系统状态 | `GET /api/status` |
| 持仓 | 列表/新增（强制规则校验）/卖出/分红 | `GET/POST /api/positions` `POST /api/positions/{id}/close` |
| 候选池 | 每日候选 + 入选理由 + 一键分析 | `GET /api/candidates` `POST /api/analyze` |
| 分析卡 | 历史分析回看（含来源与时间戳） | `GET /api/cards/{code}` |
| 提醒历史 | 筛选、标记已处理/忽略/过期 | `GET /api/alerts` `POST /api/alerts/{id}/mark` |
| 回测 | 创建任务、进度、报告与曲线 | `GET/POST /api/backtests` |
| 用量 | 当日/近 7 日 token 与花费、软阈值设置 | `GET /api/usage` |
| 设置 | 策略档案模板、监控参数、通知配置（非密钥） | `GET/PUT /api/settings` |

- **认证**：默认仅 `127.0.0.1`；设置中可开启密码（bcrypt 哈希 + itsdangerous 签名会话 Cookie）。
- **静态资源**：htmx / ECharts 本地 vendor（不走 CDN），K 线图用 ECharts。

### 6.9 配置与密钥

- `config/config.yaml`（版本化，非密钥）：轮询间隔、推送上限、软阈值、初筛默认数量、费用默认值等。
- `.env`（gitignore，密钥）：`DEEPSEEK_API_KEY`、`SMTP_*`、`SERVERCHAN_SENDKEY`、`PANEL_PASSWORD` 等；`.env.example` 提供模板。

```yaml
# config.yaml 示例
poll: { interval_minutes: 10, windows: ["09:30-11:30", "13:00-15:00"] }
notify: { daily_push_limit: 30 }
llm: { model: deepseek-chat, soft_daily_cost_alert_cny: 5, max_news_per_analysis: 8 }
screen: { daily_candidates: 10 }
backtest:
  slots: 5
  fees: { commission_rate: 0.00025, commission_min: 5, stamp_tax_sell: 0.0005, slippage: 0.001 }
  fundamentals_lag_days_fallback: 45
```

### 6.10 日志与可观测性

- `logging`：控制台 + 滚动文件（`logs/app.log`，按天/大小轮转）；级别可配。
- 结构化关键事件：轮询周期摘要（耗时/触发数）、LLM 调用（purpose/tokens/耗时/成败）、投递结果（通道/成败/重试次数）、作业执行（`job_run`）。
- 面板「系统状态」：最近轮询时间、下一任务时间、数据源健康、LLM 用量。

## 7. 非功能落地方案

| 需求 | 机制 |
|---|---|
| 可靠性 | NSSM 服务 + 自动重启；启动即 `catchup`；`max_instances=1` 防重入；电源设置指引（禁睡眠） |
| 性能 | 数据源调用线程池；批量行情单请求；LLM/回测异步；轮询单轮 < 30s |
| 成本 | 去抖 + 当日缓存 + 用量看板 + 软阈值提示（不降级） |
| 安全 | 密钥仅 `.env`；面板默认本机；可选密码；错误信息不回传密钥 |
| 合规 | 免责声明（启动 + 设置页 + 每张分析卡/推送尾注）；不做收益承诺 |

## 8. 测试策略

| 层 | 内容 | 工具 |
|---|---|---|
| 单元 | 规则引擎全类型与状态迁移、费用计算、回测统计（已知样本）、补报判定、成本估算、JSON 解析容错 | pytest |
| 集成 | 假数据源（fixtures）驱动：轮询→触发→假通知全链路；收盘任务；回测在样本数据上的端到端；Web API（TestClient） | pytest + httpx |
| 契约/网络 | 真实数据源连通性冒烟（`@pytest.mark.live`，默认跳过） | pytest marker |
| 确定性 | 时间冻结（freezegun）、固定随机种子、fixtures 替代网络 | freezegun |

重点单测清单：`trailing_stop` 峰值更新与复位、同日止损优先、dedup_key 幂等、分红摊薄成本、离线漏触发回放。

## 9. 发布与回滚

- **版本**：里程碑打 git tag（如 `v0.1.0` 对应批次 1 完成）；提交遵循 Conventional Commits。
- **数据库迁移**：Alembic；执行升级前自动 `scripts/backup_db.ps1`（备份至 `data/backups/`，保留 30 天）。
- **回滚**：停服务 → `git checkout <tag>` → 若含 schema 变更则恢复对应备份 → 启动服务。
- **配置与数据分离**：代码升级不动 `data/`；`.env` 与 `config.yaml` 独立维护。

## 10. 风险与开放问题

| # | 风险 | 缓解 |
|---|---|---|
| 1 | AkShare 接口高频变动/失效 | Adapter 隔离 + 兜底源（新浪/东财直连）+ 失败显式降级；锁依赖版本 |
| 2 | 免费实时行情延迟/限流 | 批量接口 + 间隔下限保护（≥5 分钟）+ 退避；延迟标注 |
| 3 | Server酱/PushPlus 免费额度 | 每日推送上限 + 收盘汇总合并；支持双通道互备 |
| 4 | LLM 输出 JSON 不稳定 | `json_object` 模式 + pydantic 校验 + 重试 + 模板兜底 |
| 5 | 回测数据质量（公告日/幸存者偏差） | 保守滞后兜底 + 报告强制披露局限 |
| 6 | 电脑休眠/断电 | 补报/追赶 + 电源设置指引 + 服务自动重启 |
| 7 | 场外基金 T+1 净值滞后 | 盘中估值仅供参考并标注；默认关闭估值轮询 |

**已确认小项**（2026-10-11）：
- [x] 包管理：pip + venv
- [x] 推送内容：纯文本（无 K 线图，后续需要再加）
- [x] 常驻方式：NSSM 注册 Windows 服务（无托盘图标）

## 11. 实施批次与下一步

1. **批次 1（地基）**：项目骨架（pyproject/config/db/logging/CLI）→ 数据源层 → 持仓/交易/分红 → 规则引擎 → 轮询调度 → 邮件通知 → 补报 → NSSM 安装脚本
2. **批次 2（变好用）**：Web 面板（页面 + 认证）→ 微信通知 → 收盘体检报告 → 自选池 → LLM 客户端/分析卡/触发解读 → 用量看板
3. **批次 3（追完善）**：初筛模板与候选池 → 回测引擎与报告页 → 周报 → 数据清理

**下一步**：使用 `task-breakdown` skill 将批次展开为 `docs/plan.md`（每个任务含范围、验证方式、依赖），随后按批次 1 开工。
