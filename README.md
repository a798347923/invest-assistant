# invest-assistant（AI 投资分析助手）

面向个人投资者的本地化投资纪律系统：全市场初筛 + LLM 深度分析解决「选什么」，强制退出规则 + 盘中监控提醒解决「获利回吐」，规则级回测验证方法有效性。

> 个人研究项目，源码以 **MIT** 许可证开源。**仅供学习与研究，不构成投资建议，不承诺收益；实盘自动化默认关闭，启用风险自负。**

## 开源说明

- 欢迎 Issue / PR；开发与提交约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，安全问题见 [`SECURITY.md`](SECURITY.md)。
- 仓库不包含任何账户、密钥或真实交易记录：`.env`、`data/`、`logs/`、数据库与本地覆盖配置均被 gitignore。
- 实盘功能依赖第三方券商 App / 模拟器 / ADB 自动化，可能违反券商协议或当地监管要求；默认 `live.enabled=false`、`live.dry_run=true`，仅供研究，请自行评估合规与资金风险。
- 行情与数据来自第三方公开接口，可能存在延迟、缺失或错误；使用前请阅读对应数据源的条款。

## 文档

- `docs/architecture.md` — **架构现状（as-built，当前实现的事实基准）**
- `CHANGELOG.md` — 版本变更记录
- `docs/requirements.md` — 需求文档（v0.2：愿景与合规边界；部分能力已超出本文）
- `docs/design.md` — 技术设计（v0.1：历史基线，架构细节以 `architecture.md` 为准）
- `docs/plan.md` — 实施计划（批次 1/2/3）
- `docs/rule-experiments.md`、`docs/portfolio-simulation.md` — 策略实验与组合模拟记录
- `docs/weak-market-study.md` — **弱市盈利研究（训练/验证分离，29 个组合）**：结论为线上规则不变
- `docs/portfolio-study.md` — **组合级研究（5 槽容量：排序/持有期/配仓）**：仅波动率平价配仓通过，默认关
- `docs/weak-market-alternatives.md` — **弱市替代方向（多指数 / 停泊防守 ETF）**：多指数否决，停泊债券通过（未落地）
- `docs/live-trading-assessment.md` — 实盘方案草案（半自动部分已实现，默认关闭）
- `docs/data-sources.md` — **数据源（Tushare Pro 5000 积分）**：实测权限清单、历史覆盖、时点数据注意事项
- `docs/spike-datasource.md`、`docs/spike-llm.md` — 先行验证报告（S1/S2）

## 环境

- Windows 10/11，Python 3.11+（当前 3.14.6）
- 国内网络建议华为云 PyPI 镜像（本项目已配置）：

```powershell
git clone https://github.com/a798347923/invest-assistant.git
cd invest-assistant
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 使用

```powershell
# 启动常驻服务（Web 面板 + 调度器），浏览器打开 http://127.0.0.1:8787
.\.venv\Scripts\invest-assistant.exe serve
# 运行日志写入 logs\app.log（滚动 5MB×5，不再输出到终端——终端被选中"冻结"不影响服务）
# 实时查看：Get-Content -Wait -Tail 50 logs\app.log
```

常用 CLI：

```powershell
# 持仓
.\.venv\Scripts\invest-assistant.exe position add 600519 --qty 100 --price 1480 --strategy value --take-profit 20 --trailing 8 --max-holding 90
.\.venv\Scripts\invest-assistant.exe position list --quotes
# 手动执行作业
.\.venv\Scripts\invest-assistant.exe once poll      # 立即评估全部规则
.\.venv\Scripts\invest-assistant.exe once eod       # 立即生成收盘体检
.\.venv\Scripts\invest-assistant.exe once screen    # 立即全市场初筛（初筛 → 买点评估 → 候选池）
.\.venv\Scripts\invest-assistant.exe once paper     # 立即生成模拟盘日报（force 重生成）
.\.venv\Scripts\invest-assistant.exe once calibration  # 立即评估历史建议并生成校准报告
.\.venv\Scripts\invest-assistant.exe once replay   # 历史回放：机械骨架在历史日线上模拟 → 统计经验
.\.venv\Scripts\invest-assistant.exe once portfolio  # 组合级资金曲线：50k/5槽/整手/费用，输出收益·回撤·胜率
.\.venv\Scripts\invest-assistant.exe paper-reset --yes  # 归档并清空模拟盘（保留自选/分析卡/实盘/回放经验）
.\.venv\Scripts\invest-assistant.exe paper-add 600519 --qty 100 --cost 1500  # 手动导入模拟盘持仓
# 实盘（半自动）：指令单 + 风控（人工在东财 APP/客户端下单，再回填）
# 开启 live.enabled 后：模拟盘买入成功 / 实盘持仓规则触发 → 自动生成指令单
# 实盘买入前过滤近期负面公告；到 +10% 自动生成减半卖出指令（余仓目标 +12%）
.\.venv\Scripts\invest-assistant.exe live create 600519 100 1500 --side buy --ref-price 1490
.\.venv\Scripts\invest-assistant.exe live list
.\.venv\Scripts\invest-assistant.exe live fill 1 1498     # 回填成交 → 自动更新实盘持仓与日志

# 实盘（自动）：ADB 驱动 MuMu 里的东财 App（executor=ui_app；默认 dry_run 只填单不提交）
.\.venv\Scripts\invest-assistant.exe live doctor        # 自检：adb / 模拟器 / 东财 App 是否就绪
.\.venv\Scripts\invest-assistant.exe live probe         # 导出当前界面控件树（校准按钮文案用）
.\.venv\Scripts\invest-assistant.exe live exec 12       # 执行指令单 #12（dry_run 时只填单并回读校验）
.\.venv\Scripts\invest-assistant.exe live run           # 批量执行 pending 指令（需 live.enabled=true）

# 数据源自检（Tushare Pro：估值/财务/行业/资金面/可转债，不打印 token）
.\.venv\Scripts\invest-assistant.exe data-check
.\.venv\Scripts\invest-assistant.exe once weekly    # 立即生成周报
.\.venv\Scripts\invest-assistant.exe once cleanup   # 数据清理
.\.venv\Scripts\invest-assistant.exe once calendar  # 刷新交易日历
.\.venv\Scripts\invest-assistant.exe once catchup   # 补报/追赶
.\.venv\Scripts\invest-assistant.exe backup         # 备份数据库
```

**面板功能**：总览｜持仓（录入/卖出）｜自选（盘中**AI 买入计划到价提醒**：进入建议买入区间 / 跌破失效价，另可选**手动到价**；异动提醒默认关闭）｜**候选池**（每日 18:30：初筛 → **买点评估**（回踩支撑/突破在即/超跌企稳）→ **回放经验校准**（弱市剔除突破型/回踩降权/上限收紧）→ 每日最多 5 只「近期有买点」候选，含理由与一键分析）｜
分析卡（AI 生成：结论/多空/风险 + **具体操作计划**（买入区间/条件/失效价/目标/仓位）｜可回看、一键加自选）｜**模拟盘**（买点自动买入 + 每日 AI 操作建议 + 次日自动执行 + 收益跟踪）｜**复盘**（交易日志 + 清仓经验库 + **预测校准**，均自动注入 AI 提示词）｜提醒历史（htmx 标记）｜**回测**（含权益曲线与交易明细）｜用量（成本看板）。

**顶栏常驻市场状态徽标**：红=强市 / 绿=弱市 / 灰=未知（双指数 + ±1% 滞回判定）；悬停显示上证/创业板偏离度与更新时间，每 60 秒自动刷新（后端每 5 分钟刷新并缓存，不拖慢页面）。

**模拟盘（纸面交易）**：自选池触发 AI 买点**且计划确认条件全部满足**时自动买入（单笔 ≈ 总资产 × `paper.position_fraction`，最多 `max_positions` 只，含费用/滑点撮合；
确认条件为缩量/放量/收阳/站上均线等枚举，本地逐分钟判定、全部满足才买入，旧卡自动从 triggers 文字解析；无确认条件的计划按价格触发）；
**每次买入/卖出前 AI 复核（否决制）**：反对则取消本次操作；同一标的同方向 15 分钟内复用结论、每日最多 8 次；买入"问不到即取消"、卖出"问不到按原计划执行"、**纪律止损默认豁免复核**（均在设置页可调）；
**回放经验已固化到执行链路（v12，胜率约束 + 利润优先）**：①市场强弱=**双指数（上证+创业板）+ ±1% 滞回带**，弱市暂停全部自动买入（弱市独立策略经 4 套配置扫描，胜率均 <60% 且多为负收益，故保持 0 仓位）；弱市默认也**不推送「买入机会」到价提醒**（`watchlist.suppress_plan_alerts_in_weak` 可改）；自选异动提醒默认关闭（`watchlist.movement_alerts` 可开）；②回踩型附加闸门：上证未站上 20 日线时不做（突破不受限）；③回踩型多重确认（站上 MA5 + 收复前一日高点 + MA20 上行 + 站稳 MA60）；④形态差异化机械止损（回踩=近10日低−1×ATR14 自适应缓冲、突破=近10日低×0.95，**入场时固化存储**，跌破即清仓；存量持仓仍按卡片失效价）；⑤目标与持有：突破目标 +12%，**到 +10% 自动减半**（余仓继续持有），持有上限 30 个交易日（模拟盘盘中自动执行；实盘半自动生成减半卖出指令）；
每日 15:40 用收盘行情估值并生成 AI 操作建议（加仓/减仓/清仓/持有，附具体理由；跌破失效价按纪律强制清仓），邮件+微信推送；
建议在**下一交易日开盘自动执行**（当日卖出后防反手重买）；每日存收益快照。初始资金等参数见 `config/config.yaml` 的 `paper` 段（初始资金可自由调整，改后重启生效）。

**复盘闭环（模拟盘 + 实盘）**：所有操作（建仓/加仓/减仓/清仓/分红/拆股、模拟盘自动买卖与执行建议）连同「当时情况」
（计划价位、组合状态、触发原因、费用与盈亏）自动写入交易日志（`trade_journal`）；每笔**清仓后**由高级模型
（`llm.deep_model`，默认 `deepseek-reasoner`，设置页可改）自动复盘，产出 2~4 条具体经验教训（含对错证据与价位纪律，见 `trading_lesson`）；
经验会注入之后的**分析卡 / 模拟盘日报 / 收盘体检**提示词，持续校准 AI 判断。面板「复盘」页查看日志与经验库。

**预测校准闭环**：每张分析卡的建议连同「当时环境」（大盘在 20 日线上下、个股多空、价格分位）自动入库为一条预测；
满 20 个自然日后自动评估：触发率（回踩是否到区间/突破是否成立）、触发后 5 日表现、失效价是否正确、回避是否有效、5/10/20 日收益；
每周六按「建议类型 × 大盘环境」统计出**"在什么条件下容易错"**的结论（如：弱市追突破为负），推送报告并注入后续提示词——系统会用自己的历史成绩校准未来的判断。首次运行自动回填近 90 天历史分析卡。

**历史回放（机械骨架）**：`once replay` 把买入计划的**确定性部分**（回踩/突破入场 + 缩量放量/收阳/站上均线确认 + 止损/止盈/超时，全部次日开盘成交、无未来函数）
在自选/持仓标的近 2 年历史日线上逐日重演，产出按「形态 × 大盘环境」的胜率/平均收益/止损率等统计，写入经验库并自动注入提示词——
不用等几周实盘积累就能得到第一批"历史经验"。支持扩展样本做**样本外验证**（随机抽样 150 只非自选股，2022-08 起）：
v5 回踩增强（多重确认 + ATR 止损）使回踩型胜率 49%→58%、止损率 39%→17%；v6/v7 强弱判定加强（双指数+滞回）使突破型 +1.36%→+1.93%、整体 +1.37%→+1.95%；
v12 在**胜率 ≥60% 约束下继续提升利润**（2026-10）：突破目标 +12%、到 +10% 卖一半（余仓跑）、持有 30 日、弱市 0 仓位——策略胜率维持 66%，样本外总收益 3056（B）/ 6588（C，+10%），组合中位收益 +4.6% / +37%，回撤基本持平（−17.0% / −15.6%）。仓位保持 20%（25% 会把回撤推到 −21%/−19%，不取）。弱市维持不交易。逐条记录见 `docs/rule-experiments.md`。
支持**组合级资金曲线模拟**（`once portfolio`）：按 5 万初始资金、5 槽、单笔 20%、整手与费用逐日盯市；同日多信号按**动量**排序竞争槽位（`--rank` 可切 volume/rs/composite），可选组合回撤熔断（`--breaker 15`）。自选组合实测 v11：+41.5%（年化 15.4%，最大回撤 −8.4%，胜率 62.3%；纯利润版 v10：+25.8%、回撤 −17.2%、胜率 49%）；随机 15 只组合蒙特卡洛中位 +42%~47%、胜率 64%（详见 `docs/portfolio-simulation.md`）。
口径说明：不含 AI 当日建议层，样本存在选股偏差，统计仅供参考。

**数据面（给 AI 的上下文）**：行情主源东财 → **腾讯（前复权）兜底** → 新浪不复权；分析卡 / 收盘体检 / 模拟盘日报自动附带
**大盘环境**（四大指数位置、两市成交额、涨跌家数情绪）、**个股资金面**（当日/5 日主力净额）、**融资融券**（余额与环比）、**最近公告**（业绩预告/减持/解禁等重要事件标记）、
**基本面**（最近财报营收/净利/同比/ROE + 公告日期）、**估值**（PE/PB/市值）、**行业联动**（行业当日均涨跌、涨跌家数与近期累计）与**进阶技术面**（MACD/RSI/布林/ATR、约 3 年价格分位、周线 20 周线位置——本地确定性计算）。

**设置页**：查看密钥/通知配置状态（脱敏显示）与调度心跳、最近作业；在线调整运行参数
（轮询频率、候选数量、模拟盘资金/仓位、备份份数等——写入 `data/overrides.yaml` 叠加在 config.yaml 之上，重启服务后生效）；
一键备份数据库、发送测试通知验证渠道。

**调度作业**（服务运行时自动执行）：

| 时间 | 作业 |
|---|---|
| 交易日 9:30–11:30 / 13:00–15:00 **每 1 分钟** | 盘中轮询：持仓规则触发 + **自选池监控**（AI 买入计划到价 / 手动到价；异动提醒默认关闭；弱市不推买入机会 → 模板消息推邮件+微信）+ **模拟盘自动买入/执行建议** |
| 交易日 15:30 | 收盘纪律体检：规则距离 + **AI 操作建议**（加/减仓、止盈止损，含触发条件） |
| 交易日 15:40 | **模拟盘日报**：估值 + AI 操作建议（加/减/清/持有）推送，供下一交易日开盘自动执行 |
| 交易日 18:30 | 候选初筛 + 批量 AI 分析 + 汇总推送 |
| 周六 10:00 | 周报（持仓/提醒执行/错过的卖点） |
| 周六 10:15 | **预测校准**：评估历史建议结局（首次回填近 90 天分析卡）→ 按「建议类型 × 大盘环境」统计 → 推送报告并注入提示词 |
| 周日 23:00 | 数据清理（新闻/提醒/作业记录/回测轮转） |
| 每日 23:30 | 数据库备份（保留 30 份） |
| 服务启动时 | 补报/追赶（重发未送达 + 离线漏触发回放） |

通知渠道：邮件（SMTP）+ 微信（Server酱），双通道并行；凭证放 `.env`（gitignore，模板见 `.env.example`）。

## 回测（v1）口径与局限

- 信号：动量代理（收盘价 > MA20 且 20 日涨幅 ≥ 3%），**T 日收盘产生信号、T+1 开盘买入**；
- 退出：止盈/止损/移动止盈/持有期，**与实盘共用同一规则引擎**；同日同时触发按止损优先（保守）；
- 成交价近似：止损/移动止盈 = min(开盘, 线价)、止盈 = max(开盘, 目标价)、跳空按开盘价；
- 费用：佣金（万 2.5，最低 5 元）、印花税（卖出 0.05%）、滑点 0.1%，均可配；
- 槽位等权（默认 5）、A 股 100 股整手；基准为沪深 300。
- **局限**：标的池取当前快照按成交额 Top N（幸存者偏差）；基本面时点模板（ROE/增速）属二期；
  结论仅用于验证"退出规则 + 仓位纪律"的方法有效性，不代表实盘收益。

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest -q            # 单元/集成测试（默认排除 live）
.\.venv\Scripts\python.exe -m pytest -m live -q    # 真实网络冒烟（数据源）
.\.venv\Scripts\python.exe -m ruff check src tests # 静态检查（E/F/W/I/B/UP；E501 因中文注释不强制）
```

### 策略研究（训练集/验证集分离）

```powershell
# 1) 抓数据（236 只 × 2019-2026 前复权日线，约 2 分钟，缓存到 data/research/）
.\.venv\Scripts\python.exe scripts\research\data.py --n 240
# 2) 跑实验（diagnose 诊断 / full 弱市+强市候选 / reversal 反转 / gates 指数斜率 / weak60 指数MA60拆分）
.\.venv\Scripts\python.exe scripts\research\study.py --set diagnose
# 3) 汇总训练集 vs 验证集对照表
.\.venv\Scripts\python.exe scripts\research\summary.py
# 4) 组合级（5 槽）研究：排序因子 / 持有期 / 配仓，带随机基准
.\.venv\Scripts\python.exe scripts\research\portfolio.py --window both --hold 30 --seeds 30
.\.venv\Scripts\python.exe scripts\research\portfolio.py --window both --hold 30 --seeds 30 --sizing atr
```

口径与结论见 `docs/weak-market-study.md`、`docs/portfolio-study.md`；研究不触碰生产库（写入 `data/research/`）。

## 部署与自启（Windows）

```powershell
.\scripts\install_service.ps1 -Action install -DryRun   # 预览
.\scripts\install_service.ps1 -Action install           # 安装（NSSM 优先，回退计划任务）
.\scripts\install_service.ps1 -Action uninstall
.\scripts\install_service.ps1 -Action status
```

- 电源设置建议：`powercfg /change standby-timeout-ac 0`（仅插电时禁用睡眠）。
- 数据源韧性：行情/快照/日线均为「东财优先 → 新浪兜底 → 显式报错」，实测网络波动可自动恢复。

## 状态

- [x] 需求 / 设计 / 计划
- [x] S1 数据源验证、S2 LLM 验证
- [x] 批次 1（地基）：数据源层、持仓与规则、盘中轮询、邮件提醒、补报、备份
- [x] 批次 2（变好用）：Web 面板、微信推送、收盘体检报告、LLM 触发解读与分析卡、用量看板
- [x] 批次 3（追完善）：全市场初筛候选池、候选批量分析、规则级回测、周报、数据清理 —— **一期完成（v0.3.0）**
- [x] 一期之后（v0.4.0）：模拟盘、半自动实盘、历史回放、组合回放、预测校准、强弱市判定、复盘闭环 —— 详见 `CHANGELOG.md`

## 参与贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。提交前请确保：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
```

## 许可证

本项目以 **MIT License** 发布，详见 [`LICENSE`](LICENSE)。

## 免责声明

本项目仅用于个人学习、研究与工程实践，不构成任何投资建议、收益承诺或代客理财服务。作者不对任何直接或间接损失负责。市场有风险，投资需谨慎；实盘自动化可能涉及券商协议、程序化交易报备与当地监管要求，请在充分理解风险并自行确认合规后使用。
