"""把 study 结果 JSON 汇总成对照表（训练集 vs 验证集，只看关键列）。"""

import glob
import json
import os

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")


def load(pattern: str) -> dict:
    files = sorted(glob.glob(os.path.join(RESEARCH, pattern)))
    if not files:
        raise SystemExit(f"没有匹配 {pattern} 的结果")
    with open(files[-1], encoding="utf-8") as fh:
        return json.load(fh)


def fmt(stat: dict | None) -> str:
    if not stat:
        return "  -  "
    return (f"{stat['n']:5d} {stat['win_rate']:5.1f} {stat['avg_ret']:+6.2f} "
            f"{stat['t_stat']:+5.2f} {stat['stop_rate']:5.1f} {stat['target_rate']:5.1f} "
            f"{stat['timeout_rate']:5.1f}")


def main() -> None:
    for pattern, label in (("study_full_*.json", "full"), ("study_reversal_*.json", "reversal"),
                           ("study_gates_*.json", "gates")):
        path = sorted(glob.glob(os.path.join(RESEARCH, pattern)))
        if not path:
            continue
        data = load(pattern)
        print(f"\n########## {label} ##########")
        for window in ("TRAIN", "VALID"):
            print(f"\n--- {window} ---")
            print(f"{'变体':24s}{'子集':8s}{'n':>5s} {'胜率':>5s} {'期望':>6s} {'t':>5s} "
                  f"{'止损':>5s} {'目标':>5s} {'超时':>5s}")
            for name, per_window in data["variants"].items():
                for subset in ("双指数强市", "双指数弱市"):
                    stat = per_window.get(window, {}).get(subset)
                    if not stat:
                        continue
                    print(f"{name:24s}{subset[-2:]:8s}{fmt(stat)}")


if __name__ == "__main__":
    main()
