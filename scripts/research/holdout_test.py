"""跨股票池检验：在**陌生股票池**上一次性验证 `MFE×0.6 + 回撤节流`。

这是本会话唯一干净的检验——新池（seed=7，同一快照、不同标的）从未参与任何选择。
只测两个配置，不做搜索：

  A 基准：目标固定 12%，无控回撤
  B 候选：目标 = MFE 中位数×0.6（clip 5%~35%），叠加回撤节流

判定：B 是否在三个窗口都不差于 A（收益与回撤）。

用法：python scripts/research/holdout_test.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from dd_control import market_vol, replay, simulate  # noqa: E402
from study import STRESS, TRAIN, VALID, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct, _enhanced_regime_map  # noqa: E402

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def main() -> None:
    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    vol = market_vol(sh)
    import statistics

    vtarget = statistics.median(vol.values())
    print(f"陌生股票池：{len(bars_map)} 只（seed=7）")

    verdict = []
    for name, (start, end) in WINDOWS.items():
        print(f"\n===== {name} {start}..{end} =====")
        print(f"{'方案':26s}{'成交':>6s}{'胜率':>7s}{'总收益':>9s}{'回撤':>9s}")
        rows = {}
        for tmode, ctrl, label in (
            ("fixed", "none", "A 基准（固定12%）"),
            ("0.6", "dd_throttle", "B 候选（MFE×0.6+节流）"),
        ):
            trades = replay(name, bars_map, sh, regime, rules, cost, tmode, 30)
            res = simulate(trades, (start, end), ctrl, vol, vtarget)
            rows[label] = res
            print(f"{label:26s}{len(trades):>6d}"
                  f"{sum(1 for t in trades if t.ret_pct > 0) / len(trades) * 100 if trades else 0:>7.1f}"
                  f"{res['total']:>+9.1f}{res['dd']:>+9.1f}")
        a, b = rows["A 基准（固定12%）"], rows["B 候选（MFE×0.6+节流）"]
        better = b["total"] >= a["total"] and b["dd"] >= a["dd"]
        verdict.append((name, better, b["total"] - a["total"], b["dd"] - a["dd"]))
        print(f"  -> {'B 更好' if better else 'B 未同时胜出'}"
              f"（收益 {b['total'] - a['total']:+.1f}pp，回撤 {b['dd'] - a['dd']:+.1f}pp）")

    print("\n===== 汇总 =====")
    wins = sum(1 for _, ok, _, _ in verdict if ok)
    for name, ok, dr, dd in verdict:
        print(f"  {name}: {'✓' if ok else '✗'} 收益 {dr:+.1f}pp / 回撤 {dd:+.1f}pp")
    print(f"\n{len(verdict)} 个窗口中 B 同时胜出 {wins} 个")
    print("判定：" + ("通过——新池上成立，不是样本内巧合" if wins == len(verdict)
                   else "不通过——陌生池上不成立，之前的改善更可能是样本内拟合"))


if __name__ == "__main__":
    main()
