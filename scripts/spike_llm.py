"""S2 DeepSeek 分析卡原型（spike，v2：加入退避重试与上下文记录）。

真实调用 DeepSeek API，对 3 个标的（2 只 A 股 + 1 只场外基金）生成结构化分析卡，
记录：延迟、tokens、JSON 可解析率、行情/新闻获取情况、重复运行一致性。
结果输出：docs/spike-llm-raw.json（不含密钥）

运行：.venv\\Scripts\\python.exe scripts\\spike_llm.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "spike-llm-raw.json"
API = "https://api.deepseek.com/chat/completions"

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def load_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        envf = ROOT / ".env"
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        print("ERROR: 未找到 DEEPSEEK_API_KEY（环境变量或 .env）")
        sys.exit(2)
    return key


KEY = load_key()
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def with_retry(fn, attempts: int = 3, base_delay: float = 1.5):
    """退避重试：应对偶发 RemoteDisconnected。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            time.sleep(base_delay * (i + 1))
    assert last is not None
    raise last


def em_kline(secid: str, days: int = 40) -> list[str]:
    end = datetime.now().strftime("%Y%m%d")
    beg = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&klt=101&fqt=1"
        f"&beg={beg}&end={end}&lmt={days}"
    )
    r = requests.get(url, headers=EM_HEADERS, timeout=20)
    k = (r.json().get("data") or {}).get("klines") or []
    if not k:
        raise RuntimeError("EM kline 返回空")
    slim = []
    for line in k[-days:]:
        p = line.split(",")
        slim.append(f"{p[0]} 收{p[2]} 高{p[3]} 低{p[4]} 量{p[5]}")
    return slim


def stock_news(code: str, n: int = 5) -> list[str]:
    import akshare as ak

    df = ak.stock_news_em(symbol=code)
    out = []
    for _, row in df.head(n).iterrows():
        out.append(f"{str(row.get('发布时间', ''))[:16]} {str(row.get('新闻标题', ''))[:60]}")
    return out


def fund_nav(code: str, n: int = 12) -> list[str]:
    import akshare as ak

    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    out = []
    for _, row in df.tail(n).iterrows():
        out.append(f"{row.iloc[0]} 单位净值 {row.iloc[1]}")
    return out


SYSTEM = (
    "你是一名严谨的个人投资研究助手，服务对象是一位中国个人投资者。"
    "你必须只根据提供的行情与新闻数据分析，不得编造数据；无法判断时写“数据不足”。"
    "禁止承诺收益，禁止“必涨/必跌”式表述。"
    "请严格输出一个 JSON 对象（不要输出 JSON 以外的任何文字），字段："
    "summary(一句话结论)、technicals(技术面简评)、news_sentiment(消息面)、"
    "bull_points(数组，最多4条)、bear_points(数组，最多4条)、risks(数组，最多4条)、"
    "action_bias(取值: 观察/谨慎/回避/关注买入机会)、confidence(0到1)、data_caveats(数据局限)"
)


def call_llm(user: str, max_tokens: int = 900) -> dict:
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "stream": False,
    }
    t0 = time.perf_counter()
    r = requests.post(API, headers=HEADERS, json=payload, timeout=180)
    ms = round((time.perf_counter() - t0) * 1000)
    if r.status_code != 200:
        return {"ms": ms, "error": f"HTTP {r.status_code}: {r.text[:200]}", "parsed": None}
    j = r.json()
    content = j["choices"][0]["message"]["content"]
    usage = j.get("usage", {})
    try:
        parsed = json.loads(content)
        parse_error = None
    except Exception as exc:
        parsed, parse_error = None, str(exc)
    return {"ms": ms, "usage": usage, "content": content, "parsed": parsed, "parse_error": parse_error}


TARGETS = [
    {"code": "600519", "name": "贵州茅台", "type": "A股", "secid": "1.600519"},
    {"code": "300750", "name": "宁德时代", "type": "A股", "secid": "0.300750"},
    {"code": "011186", "name": "场外基金(011186)", "type": "场外基金", "secid": None},
]

results = []
total_in = total_out = 0

for t in TARGETS:
    context = {"prices": [], "news": []}
    if t["secid"]:
        try:
            context["prices"] = with_retry(lambda: em_kline(t["secid"], 40))
        except Exception as exc:
            context["prices"] = [f"行情获取失败: {exc}"]
        try:
            context["news"] = with_retry(lambda: stock_news(t["code"], 5))
        except Exception as exc:
            context["news"] = [f"新闻获取失败: {exc}"]
    else:
        try:
            context["prices"] = with_retry(lambda: fund_nav(t["code"], 12))
        except Exception as exc:
            context["prices"] = [f"净值获取失败: {exc}"]

    first_item = str(context["prices"][0]) if context["prices"] else ""
    prices_ok = not first_item.startswith(("行情获取失败", "净值获取失败"))

    user = json.dumps({"标的": t, "近期行情": context["prices"], "最新新闻标题": context["news"]}, ensure_ascii=False)
    rec = {
        "target": t,
        "input_chars": len(user),
        "prices_ok": prices_ok,
        "prices_n": len(context["prices"]),
        "news_n": len(context["news"]),
        "context": context,
    }
    try:
        res = call_llm(user)
        rec.update(res)
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["parsed"] = None
    u = rec.get("usage") or {}
    total_in += u.get("prompt_tokens", 0) or 0
    total_out += u.get("completion_tokens", 0) or 0
    ok = rec.get("parsed") is not None
    bias = (rec.get("parsed") or {}).get("action_bias", "")
    print(
        f"[{'OK  ' if ok else 'FAIL'}] {t['name']:14s} {rec.get('ms', '?'):>7} ms  "
        f"prices={'ok' if prices_ok else 'ERR'}  in={u.get('prompt_tokens')} out={u.get('completion_tokens')}  "
        f"bias={bias}  {'' if ok else str(rec.get('error') or rec.get('parse_error'))[:90]}"
    )
    results.append(rec)

# 重复运行一致性抽样（同一输入再跑一次第一个标的）
first = results[0]
t = TARGETS[0]
try:
    context2 = {
        "prices": with_retry(lambda: em_kline(t["secid"], 40)),
        "news": with_retry(lambda: stock_news(t["code"], 5)),
    }
    user = json.dumps({"标的": t, "近期行情": context2["prices"], "最新新闻标题": context2["news"]}, ensure_ascii=False)
    res2 = call_llm(user)
    u2 = res2.get("usage") or {}
    total_in += u2.get("prompt_tokens", 0) or 0
    total_out += u2.get("completion_tokens", 0) or 0
    b1 = (first.get("parsed") or {}).get("action_bias")
    b2 = (res2.get("parsed") or {}).get("action_bias")
    print(f"[INFO] 重复运行: 第一次 bias={b1} / 第二次 bias={b2} (json_ok={res2.get('parsed') is not None})")
    results.append({"target": t, "repeat": True, "ms": res2.get("ms"), "usage": res2.get("usage"), "parsed": res2.get("parsed")})
except Exception as exc:
    print(f"[INFO] 重复运行失败: {type(exc).__name__}: {exc}")

summary = {
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "model": "deepseek-chat",
    "total_input_tokens": total_in,
    "total_output_tokens": total_out,
    "prices_ok_count": sum(1 for r in results[:3] if r.get("prices_ok")),
    "json_success": sum(1 for r in results[:3] if r.get("parsed")),
    "results": results,
}
OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"\ntotal tokens: in={total_in} out={total_out}  prices_ok={summary['prices_ok_count']}/3  JSON={summary['json_success']}/3  raw -> {OUT}")
