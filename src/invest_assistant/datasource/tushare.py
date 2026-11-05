"""Tushare Pro 数据源：基本面 / 估值 / 行业 / 资金面 / 可转债。

为什么只做这几类：行情与日线已由东财/腾讯/新浪覆盖，Tushare 的价值是**价格以外的信息**：
- `daily_basic`：每日 PE_TTM / PB / 换手率 / 总市值（全历史），做估值分位与低换手因子；
- `fina_indicator` / `income`：带 **ann_date（公告日）** 的时点财务，避免未来函数；
- `index_classify` / `index_member`：申万行业分类与成分，做行业动量；
- `top_list` / `hsgt_top10` / `margin_detail`：龙虎榜、北向、两融；
- `cb_basic` / `cb_daily`：可转债。

设计：
- **不引入 tushare SDK**，直接走 HTTP（`POST http://api.tushare.pro`），少一个依赖、便于 mock；
- token 从 `settings.tushare_token`（.env）读；未配置时 `configured=False`，调用抛 `MarketDataError`，
  上层按"数据不可用"降级，不影响 akShare 主链路；
- 返回 `list[dict]`，字段名与 Tushare 原始一致，调用方各取所需；
- 积分不足/无权限时抛 `MarketDataError` 并保留原始 msg（便于排查是否要加钱）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .base import MarketDataError

log = logging.getLogger(__name__)

API_URL = "http://api.tushare.pro"
RATE_LIMIT_HINT = "每分钟"  # Tushare 限频报错里通常含此字样


@dataclass
class TushareResult:
    rows: list[dict]
    fields: list[str]

    def __len__(self) -> int:
        return len(self.rows)


class TushareClient:
    """Tushare Pro 只读客户端（HTTP 直连）。"""

    name = "tushare"

    def __init__(self, token: str = "", *, timeout: float = 30.0, retries: int = 2):
        self.token = (token or "").strip()
        self.timeout = timeout
        self.retries = retries

    @classmethod
    def from_settings(cls, settings=None) -> TushareClient:
        from ..config import get_settings

        settings = settings or get_settings()
        return cls(getattr(settings, "tushare_token", "") or "")

    @property
    def configured(self) -> bool:
        return bool(self.token)

    # ---------------------------------------------------------------- 底层

    def call(self, api_name: str, params: dict | None = None, fields: str = "") -> TushareResult:
        if not self.configured:
            raise MarketDataError("Tushare 未配置：请在 .env 设置 TUSHARE_TOKEN")
        payload = {
            "api_name": api_name,
            "token": self.token,
            "params": params or {},
            "fields": fields,
        }
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(API_URL, json=payload, timeout=self.timeout)
                data = resp.json()
            except requests.RequestException as exc:
                last_error = f"网络错误: {exc}"
            except ValueError as exc:
                last_error = f"响应非 JSON: {exc}"
            else:
                code = data.get("code")
                if code == 0:
                    body = data.get("data") or {}
                    cols = body.get("fields") or []
                    items = body.get("items") or []
                    return TushareResult(
                        rows=[dict(zip(cols, item, strict=False)) for item in items],
                        fields=list(cols),
                    )
                msg = str(data.get("msg") or "").strip()
                # 限频：等 1 秒重试；权限/积分问题直接抛，重试无意义
                if RATE_LIMIT_HINT in msg and attempt < self.retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise MarketDataError(f"Tushare {api_name} 失败: {msg or 'code=' + str(code)}")
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        raise MarketDataError(f"Tushare {api_name} 失败: {last_error}")

    # ---------------------------------------------------------------- 基础

    def stock_basic(self, list_status: str = "L") -> list[dict]:
        return self.call(
            "stock_basic", {"list_status": list_status},
            "ts_code,symbol,name,area,industry,market,list_date,delist_date",
        ).rows

    # ---------------------------------------------------------------- 估值/换手

    def daily_basic(self, ts_code: str, start_date: str, end_date: str) -> list[dict]:
        """每日估值与换手（`start_date`/`end_date` 形如 20230101）。"""
        return self.call(
            "daily_basic",
            {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
            "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
            "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,total_share,float_share,total_mv,circ_mv",
        ).rows

    def daily_basic_by_date(self, trade_date: str) -> list[dict]:
        """某一天的全市场估值快照（用于横截面排序）。"""
        return self.call(
            "daily_basic", {"trade_date": trade_date},
            "ts_code,trade_date,close,turnover_rate,pe_ttm,pb,total_mv,circ_mv",
        ).rows

    # ---------------------------------------------------------------- 财务（时点）

    def fina_indicator(self, ts_code: str, start_date: str = "20100101",
                       end_date: str = "") -> list[dict]:
        params = {"ts_code": ts_code, "start_date": start_date}
        if end_date:
            params["end_date"] = end_date
        return self.call(
            "fina_indicator", params,
            "ts_code,ann_date,end_date,roe,roe_dt,roa,netprofit_yoy,dt_netprofit_yoy,"
            "grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio,eps,bps",
        ).rows

    def income(self, ts_code: str, start_date: str = "20100101", end_date: str = "") -> list[dict]:
        params = {"ts_code": ts_code, "start_date": start_date}
        if end_date:
            params["end_date"] = end_date
        return self.call(
            "income", params,
            "ts_code,ann_date,f_ann_date,end_date,report_type,revenue,operate_profit,"
            "n_income,n_income_attr_p,basic_eps",
        ).rows

    # ---------------------------------------------------------------- 行业

    def sw_industries(self, level: str = "L1", src: str = "SW2021") -> list[dict]:
        return self.call(
            "index_classify", {"level": level, "src": src},
            "index_code,industry_name,level,industry_code,src",
        ).rows

    def sw_members(self, index_code: str) -> list[dict]:
        return self.call(
            "index_member", {"index_code": index_code},
            "index_code,con_code,con_name,in_date,out_date,is_new",
        ).rows

    # ---------------------------------------------------------------- 资金面

    def top_list(self, trade_date: str) -> list[dict]:
        return self.call(
            "top_list", {"trade_date": trade_date},
            "ts_code,trade_date,name,close,pct_change,turnover_rate,amount,l_buy,l_sell,net_amount,reason",
        ).rows

    def hsgt_top10(self, trade_date: str) -> list[dict]:
        return self.call(
            "hsgt_top10", {"trade_date": trade_date},
            "trade_date,ts_code,name,close,change,rank,market_type,net_amount,buy,sell,amount",
        ).rows

    def margin_detail(self, trade_date: str) -> list[dict]:
        return self.call(
            "margin_detail", {"trade_date": trade_date},
            "trade_date,ts_code,name,rzye,rqye,rzrqye,rqyl,rqmcl",
        ).rows

    # ---------------------------------------------------------------- 可转债

    def cb_basic(self) -> list[dict]:
        return self.call(
            "cb_basic", {},
            "ts_code,bond_short_name,stk_code,stk_short_name,list_date,delist_date,"
            "par,issue_size,value_date,maturity_date,rate,remain_size",
        ).rows

    def cb_daily(self, trade_date: str) -> list[dict]:
        return self.call(
            "cb_daily", {"trade_date": trade_date},
            "ts_code,trade_date,close,open,high,low,vol,amount",
        ).rows

    # ---------------------------------------------------------------- 自检

    def doctor(self) -> dict:
        """连通性与权限自检（不返回 token）。"""
        info: dict = {"configured": self.configured, "api": API_URL}
        if not self.configured:
            info["error"] = "未配置 TUSHARE_TOKEN"
            return info
        checks = [
            ("stock_basic", {}),
            ("daily_basic", {"trade_date": "20261030"}),
            ("index_classify", {"level": "L1", "src": "SW2021"}),
            ("fina_indicator", {"ts_code": "600519.SH", "start_date": "20250101"}),
            ("top_list", {"trade_date": "20261030"}),
            ("margin_detail", {"trade_date": "20261030"}),
            ("cb_basic", {}),
        ]
        ok, denied = [], []
        for api, params in checks:
            try:
                result = self.call(api, params)
                ok.append({"api": api, "rows": len(result)})
            except MarketDataError as exc:
                denied.append({"api": api, "error": str(exc)})
        info["ok"] = ok
        info["failed"] = denied
        return info
