"""回测费用模型（默认值可在 config.yaml backtest.fees 覆盖）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fees:
    commission_rate: float = 0.00025  # 佣金（双边）
    commission_min: float = 5.0  # 单笔最低佣金（元）
    stamp_tax_sell: float = 0.0005  # 印花税（仅卖出）
    slippage: float = 0.001  # 滑点（买入上浮/卖出下浮）

    def buy_fee(self, amount: float) -> float:
        return max(amount * self.commission_rate, self.commission_min)

    def sell_fee(self, amount: float) -> float:
        return max(amount * self.commission_rate, self.commission_min) + amount * self.stamp_tax_sell

    @classmethod
    def from_config(cls, config: dict | None) -> Fees:
        if not config:
            return cls()
        return cls(
            commission_rate=float(config.get("commission_rate", cls.commission_rate)),
            commission_min=float(config.get("commission_min", cls.commission_min)),
            stamp_tax_sell=float(config.get("stamp_tax_sell", cls.stamp_tax_sell)),
            slippage=float(config.get("slippage", cls.slippage)),
        )
