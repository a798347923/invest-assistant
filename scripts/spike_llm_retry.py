"""S2 补充：验证「连接重置 → 重试」策略可行（带退避重试 3 次）。

结果追加到 docs/spike-llm-raw.json 的 retry_check 字段。
运行：.venv\\Scripts\\python.exe scripts\\spike_llm_retry.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "docs" / "spike-llm-raw.json"
API = "https://api.deepseek.com/chat/completions"


def load_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    if not key:
        sys.exit("no key")
    return key


HEADERS = {"Authorization": f"Bearer {load_key()}", "Content-Type": "application/json"}

payload = {
    "model": "deepseek-chat",
    "messages": [
        {"role": "system", "content": "你是投资研究助手。输出 JSON：{\"summary\": str, \"action_bias\": str}"},
        {"role": "user", "content": json.dumps({"标的": "贵州茅台(600519)", "任务": "一句话总结并给出 action_bias"}, ensure_ascii=False)},
    ],
    "response_format": {"type": "json_object"},
    "temperature": 0.4,
    "max_tokens": 200,
}

attempts = []
ok = False
for i in range(3):
    t0 = time.perf_counter()
    try:
        r = requests.post(API, headers=HEADERS, json=payload, timeout=120)
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            attempts.append({"attempt": i + 1, "ms": ms, "error": f"HTTP {r.status_code}"})
            print(f"attempt {i+1}: HTTP {r.status_code} {r.text[:120]}")
        else:
            j = r.json()
            parsed = json.loads(j["choices"][0]["message"]["content"])
            attempts.append({"attempt": i + 1, "ms": ms, "usage": j.get("usage"), "parsed": parsed})
            print(f"attempt {i+1}: OK {ms} ms  {parsed}")
            ok = True
            break
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000)
        attempts.append({"attempt": i + 1, "ms": ms, "error": f"{type(exc).__name__}: {exc}"})
        print(f"attempt {i+1}: FAIL {type(exc).__name__}: {exc}")
    time.sleep(2 * (i + 1))  # 2s, 4s 退避

raw = json.loads(RAW.read_text(encoding="utf-8")) if RAW.exists() else {}
raw["retry_check"] = {"eventual_ok": ok, "attempts": attempts}
RAW.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("retry_check ->", RAW)
