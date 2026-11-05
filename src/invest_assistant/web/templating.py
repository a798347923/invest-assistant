"""模板与静态资源定位。"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _market_regime() -> dict:
    from ..engine.regime_display import regime_snapshot

    return regime_snapshot()


TEMPLATES.env.globals["market_regime"] = _market_regime
