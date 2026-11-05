"""模拟盘建仓权重：等权 vs 波动率平价（按止损距离反推仓位）。

组合级验证结论（2026-11，见 docs/portfolio-study.md）：
同一批交易下，等权 → 训练集 +77.6%/回撤 −15.1%、验证集 −11.5%/回撤 −33.3%；
波动率平价 → 训练集 +77.7%/回撤 −13.9%、验证集 +7.8%/回撤 −31.3%。
即：收益基本不变或显著改善，两个窗口回撤都下降。因此做成可选开关（默认关），先纸面观察。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base
from invest_assistant.db.session import make_engine
from invest_assistant.engine.paper import PaperService


class FakeSettings:
    def __init__(self, paper_cfg):
        self._cfg = {"paper": paper_cfg}

    def app_config(self):
        return self._cfg


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'sizing.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def service(sf, **over) -> PaperService:
    cfg = {"enabled": True, "position_fraction": 0.2, "max_position_weight": 0.3}
    cfg.update(over)
    return PaperService(session_factory=sf, settings=FakeSettings(cfg))


def test_disabled_uses_equal_weight(sf):
    svc = service(sf, risk_based_sizing=False)
    assert svc._entry_weight(10.0, 8.8) == pytest.approx(0.2)   # 止损 12% 也仍等权


def test_reference_distance_keeps_base_weight(sf):
    svc = service(sf, risk_based_sizing=True, risk_ref_stop_pct=6.0)
    assert svc._entry_weight(10.0, 9.4) == pytest.approx(0.2)   # 止损正好 6%


def test_wide_stop_reduces_weight_to_floor(sf):
    svc = service(sf, risk_based_sizing=True, risk_ref_stop_pct=6.0)
    # 止损 12%（波动大）→ 权重 0.1（一半下限）
    assert svc._entry_weight(10.0, 8.8) == pytest.approx(0.1)


def test_tight_stop_increases_weight_up_to_max(sf):
    svc = service(sf, risk_based_sizing=True, risk_ref_stop_pct=6.0)
    # 止损 3%（波动小）→ 0.4，但被 max_position_weight=0.3 截断
    assert svc._entry_weight(10.0, 9.7) == pytest.approx(0.3)


def test_missing_or_invalid_stop_falls_back(sf):
    svc = service(sf, risk_based_sizing=True)
    assert svc._entry_weight(10.0, None) == pytest.approx(0.2)
    assert svc._entry_weight(10.0, 0.0) == pytest.approx(0.2)
    assert svc._entry_weight(10.0, 11.0) == pytest.approx(0.2)  # 止损价高于现价属于异常
