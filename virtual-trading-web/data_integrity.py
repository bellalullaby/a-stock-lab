"""Shared date/price/account invariants; no network or import-time writes."""
import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path


def valid_price(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def latest_closing_quotes(portfolio, as_of):
    """Latest known valuation per code, never after as_of; retain actual quote date."""
    result = {}
    for entry in sorted(portfolio.get("daily_log", []),
                        key=lambda e: e.get("date", ""), reverse=True):
        day = entry.get("date", "")
        if not day or day > as_of or not str(entry.get("session", "")).startswith("收盘简报"):
            continue
        for h in entry.get("holdings_snapshot", []):
            code = h.get("code")
            price = h.get("market_price")
            quote_date = h.get("price_date", day)
            if (code and valid_price(price) and quote_date and quote_date <= as_of
                    and (code not in result or quote_date > result[code]["date"])):
                result[code] = {"price": price, "date": quote_date,
                                "source": h.get("price_source", "closing_snapshot")}
    return result


def trading_age(buy_date, check_date, trading_dates):
    """None when the calendar does not prove both endpoints; never count weekdays."""
    dates = sorted(set(trading_dates or []))
    if buy_date not in dates or check_date not in dates:
        return None
    return dates.index(check_date) - dates.index(buy_date)


def require_live_date(target_date, today=None):
    today = today or date.today().isoformat()
    date.fromisoformat(target_date)
    if target_date != today:
        raise ValueError(
            f"拒绝用实时接口补跑 {target_date}。历史分析请使用离线研究脚本；"
            "历史账户重放需要期初账户及完整、同日的数据，不能改写当前账户。")


def trade_fee(trade_type, amount):
    return round(max(amount * 0.00025, 5.0)
                 + (amount * 0.0005 if trade_type == "sell" else 0)
                 + amount * 0.00001, 2)


def atomic_json_write(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
