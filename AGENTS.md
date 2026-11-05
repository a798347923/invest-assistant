# AGENTS.md — 协作约定与已验证结论

> 给所有在本仓库工作的 agent（opencode / DSH / 其他）。**新会话请先读本文件，再读 `docs/`。**
> 内容来自 2026-10-11 ～ 10-30 的 opencode 会话（1192 条消息、3 次压缩摘要）与 2026-10-31 之后的续作，
> 只保留"照做/别做"的部分；完整历史见 `docs/`。

## 1. 项目是什么

本仓库 —— 单机常驻的 A 股投资纪律系统（开源研究项目）：量化初筛 + LLM 分析（买什么）、
强制退出规则 + 分钟级提醒（何时卖）、交易日志 + 预测校准 + 历史回放（怎么改）。

- 文档地图：[architecture.md](docs/architecture.md)（**架构事实基准**）、[CHANGELOG.md](CHANGELOG.md)、
  [rule-experiments.md](docs/rule-experiments.md)（策略实验）、[weak-market-study.md](docs/weak-market-study.md)、
  [portfolio-study.md](docs/portfolio-study.md)、[live-trading-assessment.md](docs/live-trading-assessment.md)
- `docs/requirements.md` / `docs/design.md` 是 2026-10-11 的历史基线，**已被 architecture.md 覆盖**

## 2. 服务管理（最容易踩的一条）

- **默认由用户自己在终端运行**：`.\.venv\Scripts\invest-assistant.exe serve`（127.0.0.1:8787）。
  改完代码**不要**擅自起服务，提醒用户 `Ctrl+C` 后重跑即可。
- 若用户**明确要求**助手启动：用受管后台作业，并告知「会话级进程 + PID + 端口」，说明会话结束即消失；
  需要常驻请改用 `scripts/install_service.ps1`（NSSM）。
- **禁止**用通配条件批量 kill Python：历史上有一次过滤条件匹配到了助手自己的 shell。
  要停服务请按 PID 精确操作，并排除当前 `$PID`。
- 日志只写 `logs/app.log`（队列 → 滚动文件，无控制台 handler）。终端"冻结"曾导致全线程挂起，不要再加控制台 handler。
- 密钥只在 `.env`（gitignore）：**不要打印、不要提交、不要写进日志/文档**。

## 3. 策略硬约束（改动必须满足）

| 约束 | 值 | 备注 |
|---|---|---|
| 胜率 | **≥ 60%** | 用户 2026-10-17 明确的硬门槛；提高利润但要守住胜率 |
| 单笔仓位 | **20%** | 25% 会把回撤推高（已试过）；`max_position_weight` 0.3 |
| 回撤容忍 | **约 20%** | 超过即视为不可接受 |
| 弱市仓位 | **0** | 双指数（上证+创业板）+ ±1% 滞回判弱；理由已从"负期望"修正为"降回撤与换手" |
| 实盘 | **默认关** | `live.enabled=false`、`live.dry_run=true`，任何自动化都先纸面/dry-run |

**实验纪律**：一次只改一条（组合算一条）；在**样本外/验证集**通过才上线，否则回退；
负结果要在 `docs/rule-experiments.md` 留记录。不要为了让曲线好看而堆参数。

## 4. 已验证 / 已否决（不要重复提这些方案）

| 方案 | 结论 | 证据 |
|---|---|---|
| 固定百分比止损 | ✗ | 止损率 43% vs 机械 11%，总利润 −23~26% |
| 信号强度加权仓位 | ✗ | 收益 +4% 但回撤 +0.8pp；另一组持平但回撤 +2pp |
| 组合回撤熔断 | ✗ | 跨样本不稳定 |
| 增加槽位（5→10） | ✗ | 训练 +74.1→+45.7、回撤 −17.4→−23.7，随机分位 65%→5%；验证 +27.1→−0.0。5 槽的稀缺性本身就是策略的一部分 |
| 提高目标价（+15%/+20%） | ✗ | 仅近 3 年有效；验证集胜率 −1.7/−3.8pp（近期牛市过拟合） |
| ATR 移动止盈、保本止损、时间止损 | ✗ | 双窗口互斥或显著变差 |
| 弱市入场（13 种：只回踩/只突破/相对强度/快进快出/低吸/反转…） | ✗ | 仅「反转 8/4/8/10」验证集过线而训练集为负 |
| 弱市 + 指数 MA60 上方（牛市回调） | ✗ | 验证集最好 +0.85%（t=2.08）、训练集最差 −0.98%，符号翻转 |
| 指数 MA20 斜率闸门（等走好再买） | ✗ | 5 种全部更差 |
| 弱市过滤器边界（快释放/无滞回/放宽 MA60/宽度） | ✗ | 提前解除在训练集 −20%~−8%（现行 +77.6%）；放宽类验证集好、训练集差；加宽度的更严版仅 +3.0/+1.2pp（标准误 4.7/3.2）→ 不显著 |
| 多指数确认（+沪深300/+中证500，任一/多数/全部破位） | ✗ | 最像改进的 I3_any 验证集 +21.2pp 但训练集 −11.1pp；"全部破位才判弱"的放宽类训练集 −61~−72pp |
| 9 种横截面排序（反转/低波/ATR/RS/成交量稳定/复合/板块） | ✗ | 5 槽下全部落在随机基准带内（17~80 分位） |
| 持有期缩到 15 日 | ✗ | 仅验证集改善，两窗口方向不一致 |
| 波动率平价配仓 | ✗ **已回退** | 原池：训练持平、验证 +19pp；**跨股票池复验翻转**（陌生池训练 +12pp、验证 −9pp）→ 2026-11-05 关闭 |
| 负面事件闸门（解禁/减持/预亏） | ✗ | 减持闸门剔 283 笔后逐笔期望反升到 +1.53%，组合却 +74.1→+54.6、验证集 −13.4%——5 槽下"槽位重排"的破坏大于事件质量差异 |
| PEAD 业绩漂移（预告后次日开盘买入） | ✗ | 只在压力窗口为正（+1.14%/t=3.44），最近两窗口均输给随机；胜率 42–48% 低于 60% 门槛 |
| 行业分散（同行业最多 1/2/3 只） | ✗ 已关闭 | 5 槽：验证集 +27.1→+36.5、回撤 −33.1→−25.5，但训练集回撤 −25.0 超线。**10 槽复测更差**：训练 +74.1→+45.7（随机 5 分位）、三个上限全部低于加约束后的随机均值；压力窗口 ≤1 为 −15.5% |
| 研报预期修正（report_rc） | ⊘ 数据受限 | 5000 积分档限频 1 次/分钟：按股票要 4 小时、按日期要 28 小时，做不出全历史因子。字段可用但不可回测 |
| 弱市停泊换低价转债 | ✗ | 训练集 −1.6%~−22.4%、回撤 −23.6%~−28.6%（远超 20% 容忍）；验证集 +35~53% 属 2019–2023 转债牛市。低价≠安全：2023 后信用风险重定价让"最低价"选中风险最高的债 |
| 收紧准入（每天只取 1–2 笔） | ✗ | 训练 +74.1→+7.7（15 分位）、验证 +27.1→−5.2；槽位空转是主因（随机基准同步从 +66.5 掉到 +40.5）。仅压力窗口有效（90 分位）——只在熊市里"少做"才值钱 |
| 用 ETF 替代个股做主策略 | ✗ | 同规则三窗口：验证集逐笔 −0.53%（个股 +0.58%）、组合 −14.4%（+27.1%）；胜率 51–57% 低于 60% 门槛；相对自身随机基准无 edge。压力窗口抗跌好（回撤 −27% vs −46%）→ 只能当防守资产 |
| **弱市停泊（债7:金3）** | ✓ **已落地（跨池复验通过）** | 原池 +18.5/+11.4pp；**陌生池三窗口 +19.3/+17.0/+9.4pp 且回撤均改善**——本会话唯一经得起换样本的改动 |

> 注意第 9 行的方法论教训：`chrono`（按代码号排序）曾在 15 日持有下跑出 93/77 分位，
> 显式检验后发现它等价于"买 000xxx 深主板"——**凡是"两个窗口都略好但说不清原因"的结果，先怀疑是隐藏暴露**。

## 5. 改策略时必须用的研究协议

1. **窗口分离**：训练集 2023-11-01～2026-10-31（近 3 年，可调参）；验证集 2019-11-01～2023-10-31（只验收，开发期不看）。
2. **口径**：复用线上 `engine/playbook_replay._replay_code`（次日开盘成交、0.15%/笔成本），不要另写撮合。
3. **验证门槛**：验证集期望 > 0、胜率 ≥ 50%、样本 ≥ 40、t ≥ 2；**且训练集不得亏钱**；强市子集不得变差。
4. **逐笔 ≠ 可实现**：真实只有 5 槽，2000 笔信号里落地的不到 8%。任何结论都要再过一遍组合级模拟
   （`scripts/research/portfolio.py`，含 30~40 次随机基准），只有落在随机带外才算有 edge。
5. **股票池必须冻结**：`data.py` 默认复用 `universe.json`（`--resample` 才重抽）。曾有事故：排除集合变化导致 240 只全部换人，同代码同一策略的组合收益从 +77.6% 变成 +8.3%——任何跨窗口结论都要绑定固定池子。
6. **复现命令**：
   ```powershell
   .\.venv\Scripts\python.exe scripts\research\data.py --n 240
   .\.venv\Scripts\python.exe scripts\research\study.py --set diagnose|full|reversal|gates|weak60
   .\.venv\Scripts\python.exe scripts\research\summary.py
   .\.venv\Scripts\python.exe scripts\research\portfolio.py --window both --hold 30 --seeds 30 [--sizing atr]
   ```
   数据落在 `data/research/`（gitignore），**不碰生产库**。

## 6. 工程约定

- **提交**：Conventional Commits（`feat/refactor/docs/chore/perf(live): …`），一个逻辑变更一个提交，
  正文写"为什么"而不只是"改了什么"；提交前 `pytest -q` 与 `ruff check src tests scripts` 必须全绿。
- **测试**：`.\.venv\Scripts\python.exe -m pytest -q`（默认 **364 passed, 4 deselected**，`-m live` 才跑真实网络）。
- **控制台是 GBK**：跑 Python 脚本先设 `$env:PYTHONIOENCODING='utf-8'`；
  一次性脚本放 `%TEMP%`（历史上放在 `%TEMP%\opencode\`），不要污染仓库。
- **PowerShell 引号**：`python -c "…"` 里嵌 SQL/正则极易被解析坏，复杂逻辑请写临时 .py 文件再执行。
- **数据源现实**：东财 push2/push2his 常 `RemoteDisconnected`；腾讯 qfq 只给 640 根、`span>2000` 会返回空
  （已知缺陷，长区间取数用 akshare/新浪通道）；OTC 基金走日频净值。
- **不引入**：Redis / Celery / Docker / 消息队列（单机单用户，属过度设计）。
- **数据源**：行情走东财→腾讯→新浪；**基本面/估值/行业/资金面走 Tushare Pro（5000 积分档，需自备 token）**，
  适配器 datasource/tushare.py，token 在 .env 的 TUSHARE_TOKEN，自检命令 data-check。
  详见 docs/data-sources.md（实测权限清单、历史起点、时点数据注意）。

## 7. 实盘自动执行（2026-11 起）

- 载体：**MuMu 里的东财 App + ADB/UIAutomator 控件树**（`engine/live_app.py`）。
  ⚠️ 2026-10-17 取消的是「PC 客户端 + 屏幕坐标」方案（自绘控件、窗口一动就错），**不要复活那个方案**。
- 安全契约（fail-closed，均有单测）：定位失败/回读不一致/找不到确认框 → 中止且零提交；
  `dry_run=true` 只填单不提交；提交后不推测成功；提交只点一次不重试。
- 上线节奏：`live doctor` → `live probe` 校准文案 → dry-run ≥2 周（一致率 ≥95%）→ 100 股小额 1–2 周 → 才关 dry_run。
- 已知缺口：提交前未校验可用资金/可卖数量、无自动撤单、无自动对账。

## 8. 当前状态快照

- 版本 `0.4.0`；**364 测试全绿**；ruff 全绿。
- 默认配置：`paper.enabled=true`、`live.enabled=false`、`live.dry_run=true`、
  `paper.risk_based_sizing=false`、`paper.parking_enabled=true`（纸面观察）。
- 个人持仓/自选/交易数据属于本机私有数据，不纳入仓库（`data/` 已 gitignore）。
- 定时作业：盘中每分钟轮询（含自选到价）→ 每分钟提醒投递 → 15:30 收盘体检 → 15:40 模拟盘日报 →
  18:30 候选初筛 → 周六 10:00 周报 / 10:15 预测校准 → 周日 23:00 清理 → 23:30 备份。

## 9. 目录速查

```
src/invest_assistant/
  engine/    scheduler poller paper live live_app(实盘App执行器) parking(弱市停泊) eod screening runs
             playbook_replay(回放/实验开关) portfolio_sim(组合资金曲线) calibration catchup triggers
  domain/    rules(实盘回测共用规则引擎★) positions plan conditions indicators journal valuation
  datasource/ service(多源兜底) em em_ext tencent sina fund news calendar
  llm/       client analyzer(分析卡) veto(盘中否决) prompts usage
  web/       app pages api templates(htmx+ECharts 本地 vendor)
  db/        models(29 张表) session migrations backups
scripts/     install_service.ps1 backup_db.ps1 research/{data,study,summary,portfolio}.py
docs/        架构、需求、设计、实验记录、弱市研究、组合研究、弱市替代方向、实盘评估
tests/       单元/集成测试（默认排除 live）
```
