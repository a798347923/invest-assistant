"""把主流优化器搬到"参数调度"上：SGD / Momentum / Adam / Lion / 学习率衰减。

背景：我们不是在优化一个可微目标，而是在一个**噪声代理信号**（趋势效率 z-score）上挪参数。
所以优化器在这里只起两种作用：平滑（momentum / Lion）与自适应缩放（Adam）。
二阶/矩阵预条件（Sophia、Muon）在一维参数上没有意义，不测。

同时做**严格协议检验**：只用训练集挑"优化器 × 学习率"，验证集与压力窗口完全不动，
看这个选择在样本外还成不成立（这才是"事后选择"该被检验的方式）。

用法：python scripts/research/optimizers.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from adaptive_market import rolling_z, signals  # noqa: E402
from adaptive_online import HI, HOLDS, LO, START, nearest, replay_by_hold  # noqa: E402
from study import STRESS, TRAIN, VALID, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct  # noqa: E402

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
OPTIMIZERS = ("sgd", "momentum", "adam", "lion", "decay")


class State:
    """各优化器的状态与更新规则（作用在"方向信号" g = -z 上）。"""

    def __init__(self, kind: str, lr: float, beta1: float = 0.9, beta2: float = 0.999):
        self.kind = kind
        self.lr0 = lr
        self.lr = lr
        self.b1, self.b2 = beta1, beta2
        self.m = 0.0
        self.v = 0.0
        self.t = 0

    def step(self, g: float) -> float:
        self.t += 1
        lr = self.lr0
        if self.kind == "sgd":
            return lr * g
        if self.kind == "momentum":
            self.m = self.b1 * self.m + (1 - self.b1) * g
            return lr * self.m
        if self.kind == "adam":
            self.m = self.b1 * self.m + (1 - self.b1) * g
            self.v = self.b2 * self.v + (1 - self.b2) * g * g
            mhat = self.m / (1 - self.b1 ** self.t)
            vhat = self.v / (1 - self.b2 ** self.t)
            return lr * mhat / (math.sqrt(vhat) + 1e-8)
        if self.kind == "lion":
            self.m = self.b1 * self.m + (1 - self.b1) * g
            return lr * (1.0 if self.m > 0 else (-1.0 if self.m < 0 else 0.0))
        if self.kind == "decay":
            return (self.lr0 / (1 + self.t / 500.0)) * g
        return 0.0


def simulate(per_hold, window, sig, kind: str, lr: float,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0) -> dict:
    start, end = window
    all_dates = sorted({dt.date.fromisoformat(t.entry_date) for h in HOLDS for t in per_hold[h]}
                       | {dt.date.fromisoformat(t.exit_date) for h in HOLDS for t in per_hold[h]})
    all_dates = [d for d in all_dates if start <= d <= end]

    h = START
    opt = State(kind, lr)
    cash = cash0
    pos: list = []
    curve: list[float] = []

    for day in all_dates:
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
            else:
                still.append(p)
        pos = still

        z = rolling_z(sig["values"], day, sig["days"], 250)
        if z is not None and lr > 0:
            # 反向映射：z 越高（趋势越强）→ 持有越短，故方向为 -z
            h = min(HI, max(LO, h + opt.step(-15.0 * z)))  # 与 30-15z 的目标映射同尺度

        active = nearest(h)
        for t in per_hold[active]:
            if t.entry_date != day.isoformat():
                continue
            if len(pos) >= slots:
                break
            equity = cash + sum(q["value"] for q in pos)
            size = min(equity * fraction, cash)
            if size < 1000:
                break
            cash -= size
            pos.append({"trade": t, "value": size,
                        "exit_day": dt.date.fromisoformat(t.exit_date)})
        curve.append(cash + sum(p["value"] for p in pos))

    if not curve:
        return {}
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": (curve[-1] / cash0 - 1) * 100, "dd": mdd * 100}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="trend")
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    print("重放各持有期…")
    per_hold = replay_by_hold(bars_map, sh, cost)
    sig_all = signals(sh)
    sig = {"values": sig_all["signals"][args.signal], "days": sig_all["days"]}

    base = simulate(per_hold, WINDOWS["TRAIN"], sig, "sgd", 0.0)
    print(f"\n自检（不动参数, TRAIN）：{base['total']:+.1f}% / {base['dd']:+.1f}%"
          f"（应为 +74.0 / −19.2）")

    results: dict = {("TRAIN", "sgd", 0.0): base}
    for name, window in WINDOWS.items():
        results[(name, "sgd", 0.0)] = simulate(per_hold, window, sig, "sgd", 0.0)
        print(f"\n===== {name} =====")
        print(f"{'优化器':10s}{'lr':>6s}{'总收益':>9s}{'回撤':>9s}")
        for kind in OPTIMIZERS:
            for lr in (0.01, 0.02, 0.05):
                res = simulate(per_hold, window, sig, kind, lr)
                results[(name, kind, lr)] = res
                print(f"{kind:10s}{lr:>6.2f}{res['total']:>+9.1f}{res['dd']:>+9.1f}")

    # 严格协议：只用训练集挑，验证集/压力窗口当样本外
    print("\n===== 严格协议（仅用训练集选优化器/学习率） =====")
    best = max(((k, lr) for k in OPTIMIZERS for lr in (0.01, 0.02, 0.05)),
               key=lambda x: results[("TRAIN", x[0], x[1])]["total"])
    kind, lr = best
    print(f"训练集选出：{kind} / lr={lr}  → 训练 {results[('TRAIN', kind, lr)]['total']:+.1f}%")
    for name in ("VALID", "STRESS"):
        r = results[(name, kind, lr)]
        b = results[(name, "sgd", 0.0)]
        print(f"  {name}：{r['total']:+.1f}% / 回撤 {r['dd']:+.1f}%（不动参数：{b['total']:+.1f}%）")
    print(f"对照：基准（不动参数）验证 {results[('VALID','sgd',0.0)]['total']:+.1f}%、"
          f"压力 {results[('STRESS','sgd',0.0)]['total']:+.1f}%")


if __name__ == "__main__":
    main()
