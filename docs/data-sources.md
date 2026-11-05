# 数据源：Tushare Pro（已购 5000 积分）

| 项 | 值 |
|---|---|
| 渠道 | Tushare Pro，个人版 **500 元/年 = 5000 积分** |
| 频次 | 500 次/分钟，常规数据无每日总量上限 |
| 接入 | `src/invest_assistant/datasource/tushare.py`（HTTP 直连，不依赖 tushare SDK） |
| 密钥 | `.env` 的 `TUSHARE_TOKEN`（gitignore；`data-check` 只显示长度与前 3/后 3 位） |
| 自检 | `invest-assistant data-check` |

## 1. 实测权限清单（2026-11-04，5000 积分档）

全部 10 个接口一次通过，**无权限缺口、无额外付费**：

| 接口 | 用途 | 实测返回 | 历史起点 |
|---|---|---|---|
| `stock_basic` | 股票列表（含行业、上市/退市日） | 5578 只 | — |
| `daily_basic` | **PE_TTM / PB / 换手率 / 市值** | 全市场 5571 行/日 | **2005-01-04** |
| `index_classify` | 申万一级行业 | 31 个 | — |
| `index_member` | 行业成分（含 `in_date`/`out_date`） | 单行业 184 只 | — |
| `fina_indicator` | ROE / 增速 / 毛利率 / 负债率（**带 ann_date**） | 100 期/股 | **2012-12-31** |
| `income` | 利润表（营收/净利，带 `ann_date`/`f_ann_date`） | 168 期/股 | **1999-12-31** |
| `top_list` | 龙虎榜 | 96 行/日 | — |
| `hsgt_top10` | 北向十大成交 | 20 行/日 | — |
| `margin_detail` | 两融余额明细 | 4486 行/日 | — |
| `cb_basic` / `cb_daily` | 可转债基础信息 / 日线 | 1178 只 / 315 行/日 | 2002 起 |

**关键结论**：估值与换手因子可回溯到 **2005 年**，财务指标到 **2012 年**——都覆盖我们现有的 2019–2026 窗口，
也支持把研究样本扩到 2015（含股灾与 2018 熊市）。

## 2. 没买、但以后可能要的

| 接口/权限 | 价格 | 什么时候需要 |
|---|---|---|
| 可转债转股价变动 | 500 元/年 | 做「双低转债轮动」时必需（转股价随分红/下修变化，不买就算不出转股溢价率） |
| 公告信息（含 PDF 链接） | 1000 元/年 | 想把"公告事件"做成因子或更准的 AI 上下文 |
| 新闻资讯（快讯/长篇/新闻联播） | 1000 元/年 | 同上；对利润率的提升是间接的 |
| 历史分钟（2009 起） | 2000 元/年 | **不需要**：我们是日线级策略（次日开盘成交、持有 27 天） |

## 3. 数据质量注意事项

1. **时点（point-in-time）**：财务必须用 `ann_date`/`f_ann_date` 生效，绝不能用 `end_date`（报告期），
   否则就是把"一个月后才公告的数字"用在了公告之前——典型未来函数。
2. **财务会更正/重述**：Tushare 返回的是**当前最新版**，历史上被更正的财报无法还原。
   做回测时应在报告里披露这一点（和幸存者偏差同级）。
3. **新股覆盖**：`daily_basic` 从上市日开始（如 301718.SZ 只有 10 行），
   做横截面排序时要按当日有效样本计算分位，不能拿"当前在市"的名单回溯。
4. **频率**：500 次/分钟。拉 800 只股票的全历史 `daily_basic` 属于 800 次调用，需要分批+限速。

## 4. 用法

```powershell
# 自检（打印权限清单，不打印 token）
.\.venv\Scripts\invest-assistant.exe data-check
```

```python
from invest_assistant.config import get_settings
from invest_assistant.datasource.tushare import TushareClient

ts = TushareClient.from_settings(get_settings())
assert ts.configured, "请在 .env 设置 TUSHARE_TOKEN"

bars = ts.daily_basic("600519.SH", "20230101", "20231231")   # 估值/换手序列
pe = bars[-1]["pe_ttm"]

facts = ts.fina_indicator("600519.SH", "20230101")           # 时点财务（含 ann_date）
industries = ts.sw_industries()                               # 申万一级
members = ts.sw_members(industries[0]["index_code"])          # 行业成分
```

## 5. 下一步（按验证协议）

第一个实验只做**一个**问题：**在 5 个槽位里，"估值低 + 换手低"能不能提高选中率？**

- 数据：`daily_basic` 的 `pe_ttm` 分位（按行业或全样本）+ `turnover_rate`；
- 方法：沿用 `scripts/research/portfolio.py` 的排序框架（已有随机基准）；
- 门槛：验证集随机均值不差于现行、且训练集不差（AGENTS §5）；
- 若通过 → 再接 `fina_indicator` 的 ROE；若不通过 → 停在这里，不再升级数据。
