"""S1 数据源冒烟验证（spike）。

逐项测试本工具所需的免费数据源接口，记录成功/失败、延迟、数据样例。
结果输出：docs/spike-datasource-raw.json（UTF-8）

运行：.venv\\Scripts\\python.exe scripts\\spike_datasource.py
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

try:
    import akshare as ak
except Exception as exc:  # pragma: no cover
    print(f"akshare import failed: {exc}")
    sys.exit(2)

RESULTS: list[dict] = []
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "spike-datasource-raw.json"

TODAY = datetime.now()
START = (TODAY - timedelta(days=90)).strftime("%Y%m%d")
END = TODAY.strftime("%Y%m%d")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def find_ak_func(*names: str):
    for n in names:
        fn = getattr(ak, n, None)
        if callable(fn):
            return fn, n
    raise AttributeError(f"akshare {ak.__version__} 中找不到: {names}")


def probe(name: str, fn):
    t0 = time.perf_counter()
    rec = {"name": name, "ok": False, "ms": None, "rows": None, "sample": None, "error": None}
    try:
        out = fn()
        rec["ok"] = True
        if isinstance(out, tuple):
            rows, sample = out
            rec["rows"] = rows
            rec["sample"] = sample
        else:
            rec["sample"] = out
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["trace_tail"] = traceback.format_exc()[-400:]
    rec["ms"] = round((time.perf_counter() - t0) * 1000)
    RESULTS.append(rec)
    mark = "OK  " if rec["ok"] else "FAIL"
    msg = "" if rec["ok"] else rec["error"][:120]
    print(f"[{mark}] {name:36s} {rec['ms']:>8} ms  rows={rec['rows']}  {msg}")
    return rec


def timed_get(url: str, runs: int = 3, headers: dict | None = None, **kwargs):
    h = headers or HEADERS
    lat = []
    resp = None
    for _ in range(runs):
        t0 = time.perf_counter()
        resp = requests.get(url, headers=h, timeout=15, **kwargs)
        lat.append(round((time.perf_counter() - t0) * 1000))
    return lat, resp


print(f"python={sys.version.split()[0]}  akshare={ak.__version__}  today={TODAY:%F}")

# ---------- 交易日历 ----------
def calendar():
    fn, n = find_ak_func("tool_trade_date_hist_sina")
    df = fn()
    return len(df), {"func": n, "tail": df.tail(3).astype(str).to_dict("records")}


probe("交易日历", calendar)

# ---------- A股日线 ----------
def stock_daily():
    fn, n = find_ak_func("stock_zh_a_hist")
    df = fn(symbol="600519", period="daily", start_date=START, end_date=END, adjust="qfq")
    return len(df), {"func": n, "tail": df.tail(1).astype(str).to_dict("records")}


probe("A股日线(600519,前复权)", stock_daily)

# ---------- 场内ETF日线 ----------
def etf_daily():
    fn, n = find_ak_func("fund_etf_hist_em", "fund_etf_hist_sina")
    df = fn(symbol="510300", period="daily", start_date=START, end_date=END, adjust="qfq")
    return len(df), {"func": n, "tail": df.tail(1).astype(str).to_dict("records")}


probe("ETF日线(510300)", etf_daily)

# ---------- 指数日线(基准) ----------
def index_daily():
    fn, n = find_ak_func("index_zh_a_hist", "stock_zh_index_daily_em")
    df = fn(symbol="000300", period="daily", start_date=START, end_date=END)
    return len(df), {"func": n, "tail": df.tail(1).astype(str).to_dict("records")}


probe("指数日线(000300)", index_daily)

# ---------- 全市场快照 ----------
def stock_spot():
    fn, n = find_ak_func("stock_zh_a_spot_em")
    df = fn()
    row = df[df["代码"] == "600519"]
    return len(df), {"func": n, "600519": row[["代码", "名称", "最新价"]].astype(str).to_dict("records")}


probe("全市场快照(东财)", stock_spot)

# ---------- 单只实时(akshare) ----------
def ak_realtime_single():
    fn, n = find_ak_func("stock_bid_ask_em", "stock_individual_spot_xq")
    lat = []
    df = None
    for _ in range(3):
        t0 = time.perf_counter()
        df = fn(symbol="600519")
        lat.append(round((time.perf_counter() - t0) * 1000))
    return (len(df) if df is not None else None), {"func": n, "latency_ms_3runs": lat}


probe("单只实时行情(akshare)", ak_realtime_single)

# ---------- 新浪批量实时(兜底源) ----------
def sina_batch():
    codes = "sh600519,sz000001,sz300750,sh510300,sh588000"
    h = {**HEADERS, "Referer": "https://finance.sina.com.cn"}
    url = f"https://hq.sinajs.cn/list={codes}"
    lat, resp = timed_get(url, runs=3, headers=h)
    resp.encoding = "gbk"
    lines = [l for l in resp.text.strip().splitlines() if l.strip()]
    return len(lines), {"latency_ms_3runs": lat, "first": lines[0][:90] if lines else ""}


probe("新浪批量实时行情", sina_batch)

# ---------- 东财直连实时(兜底源) ----------
def em_direct():
    url = "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f57,f58,f60,f170"
    lat, resp = timed_get(url, runs=3)
    return None, {"latency_ms_3runs": lat, "data": resp.json().get("data")}


probe("东财直连实时行情", em_direct)

# ---------- 场外基金净值 ----------
def fund_nav():
    fn, n = find_ak_func("fund_open_fund_info_em")
    try:
        df = fn(symbol="011186", indicator="单位净值走势")
    except TypeError:
        df = fn(fund="011186", indicator="单位净值走势")
    return len(df), {"func": n, "tail": df.tail(2).astype(str).to_dict("records")}


probe("场外基金净值(011186)", fund_nav)

# ---------- 场外基金盘中估值(天天基金) ----------
def fundgz():
    url = "http://fundgz.1234567.com.cn/js/011186.js"
    lat, resp = timed_get(url, runs=3)
    return None, {"latency_ms_3runs": lat, "body": resp.text[:150]}


probe("基金盘中估值(fundgz)", fundgz)

# ---------- 个股新闻 ----------
def stock_news():
    fn, n = find_ak_func("stock_news_em")
    df = fn(symbol="600519")
    cols = [c for c in ["发布时间", "新闻标题"] if c in df.columns]
    return len(df), {"func": n, "head": df.head(2)[cols].astype(str).to_dict("records")}


probe("个股新闻(600519)", stock_news)

# ---------- 公告 ----------
def announcements():
    if callable(getattr(ak, "stock_zh_a_disclosure_report_cninfo", None)):
        df = ak.stock_zh_a_disclosure_report_cninfo(symbol="600519", market="沪深京")
        return len(df), {
            "func": "stock_zh_a_disclosure_report_cninfo",
            "head": df.head(2).astype(str).to_dict("records"),
        }
    if callable(getattr(ak, "stock_notice_report", None)):
        df = ak.stock_notice_report(symbol="全部")
        return len(df), {"func": "stock_notice_report(全部)", "head": df.head(2).astype(str).to_dict("records")}
    raise AttributeError("没有可用的公告接口函数")


probe("个股公告(600519)", announcements)

# ---------- 财务摘要 ----------
def financials():
    fn, n = find_ak_func("stock_financial_abstract", "stock_financial_analysis_indicator")
    df = fn(symbol="600519")
    return (len(df) if hasattr(df, "__len__") else None), {"func": n, "shape": str(getattr(df, "shape", None))}


probe("财务数据(600519)", financials)

# ---------- 名称扫描(便于后续适配) ----------
def scan_names():
    fund_est = sorted(n for n in dir(ak) if "fund" in n and ("estimate" in n or "value" in n))[:20]
    return None, {"fund_estimate_like": fund_est}


probe("akshare 基金估值类函数扫描", scan_names)

# ---------- 输出 ----------
summary = {
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "python": sys.version.split()[0],
    "akshare": ak.__version__,
    "results": RESULTS,
}
OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
ok = sum(1 for r in RESULTS if r["ok"])
print(f"\n{ok}/{len(RESULTS)} probes passed. raw -> {OUT}")
