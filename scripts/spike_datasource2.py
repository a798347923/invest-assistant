"""S1 追加验证：东财/新浪直连适配器的可行性（针对首轮 push2 失败项）。

结果输出：docs/spike-datasource-raw2.json
运行：.venv\\Scripts\\python.exe scripts\\spike_datasource2.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
RESULTS: list[dict] = []


def probe(name: str, fn):
    t0 = time.perf_counter()
    rec = {"name": name, "ok": False, "ms": None, "sample": None, "error": None}
    try:
        rec["sample"] = fn()
        rec["ok"] = True
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["ms"] = round((time.perf_counter() - t0) * 1000)
    RESULTS.append(rec)
    mark = "OK  " if rec["ok"] else "FAIL"
    msg = "" if rec["ok"] else rec["error"][:110]
    print(f"[{mark}] {name:40s} {rec['ms']:>7} ms  {msg}")
    return rec


def em_kline(secid: str):
    end = datetime.now().strftime("%Y%m%d")
    beg = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&klt=101&fqt=1"
        f"&beg={beg}&end={end}&lmt=150"
    )
    r = requests.get(url, headers=HEADERS, timeout=20)
    j = r.json()
    k = (j.get("data") or {}).get("klines") or []
    return {"rows": len(k), "last": k[-1] if k else None}


probe("EM直连-日线 600519", lambda: em_kline("1.600519"))
probe("EM直连-日线 ETF510300", lambda: em_kline("1.510300"))
probe("EM直连-日线 指数000300", lambda: em_kline("1.000300"))


def em_ulist():
    secids = "1.600519,0.000001,0.300750,1.510300,1.588000"
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
        f"&secids={secids}&fields=f12,f13,f14,f2,f3,f4,f18"
    )
    r = requests.get(url, headers=HEADERS, timeout=15)
    d = ((r.json().get("data") or {}).get("diff")) or []
    return {"rows": len(d), "first": d[0] if d else None}


probe("EM直连-批量实时 ulist(5只)", em_ulist)


def em_clist():
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3"
        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f2,f3,f8,f20"
    )
    r = requests.get(url, headers=HEADERS, timeout=20)
    d = ((r.json().get("data") or {}).get("diff")) or []
    tot = (r.json().get("data") or {}).get("total")
    return {"rows": len(d), "total": tot, "first": d[0] if d else None}


probe("EM直连-全市场列表(初筛用)", em_clist)


def sina_kline():
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=90"
    )
    r = requests.get(url, headers={**HEADERS, "Referer": "https://finance.sina.com.cn"}, timeout=15)
    j = r.json()
    return {"rows": len(j), "last": j[-1] if j else None}


probe("新浪直连-日线 600519", sina_kline)


def sina_batch100():
    codes = ",".join([f"sh60{i:04d}" for i in range(50)] + [f"sz00{i:04d}" for i in range(50)])
    url = f"https://hq.sinajs.cn/list={codes}"
    t0 = time.perf_counter()
    r = requests.get(url, headers={**HEADERS, "Referer": "https://finance.sina.com.cn"}, timeout=15)
    r.encoding = "gbk"
    lines = [l for l in r.text.strip().splitlines() if l.strip()]
    return {"rows": len(lines), "ms": round((time.perf_counter() - t0) * 1000)}


probe("新浪直连-批量100只", sina_batch100)


def ak_sina_daily():
    import akshare as ak

    if not callable(getattr(ak, "stock_zh_a_daily", None)):
        raise AttributeError("no stock_zh_a_daily")
    df = ak.stock_zh_a_daily(symbol="sh600519", start_date="20260701", end_date="20261011", adjust="qfq")
    return {"rows": len(df), "last": df.tail(1).astype(str).to_dict("records")}


probe("akshare-新浪日线 stock_zh_a_daily", ak_sina_daily)


def ak_retry_hist():
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol="600519", period="daily", start_date="20260901", end_date="20261011", adjust="qfq"
    )
    return {"rows": len(df), "last": df.tail(1).astype(str).to_dict("records")}


probe("akshare-EM日线重试 stock_zh_a_hist", ak_retry_hist)

summary = {"generated_at": datetime.now().isoformat(timespec="seconds"), "results": RESULTS}
out = Path(__file__).resolve().parent.parent / "docs" / "spike-datasource-raw2.json"
out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
ok = sum(1 for r in RESULTS if r["ok"])
print(f"\n{ok}/{len(RESULTS)} passed. raw -> {out}")
