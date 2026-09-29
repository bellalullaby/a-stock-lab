"""Offline, conditional execution study. Never imports or executes the live account.

Signals: archived exact 强候选 + explicit allowed L1 gate + allowed rotation.
Entry: next market session open, rejecting opening limit-up; no hindsight fill.
D1: pre-scheduled close on the session after purchase.
D3D5: end-of-day time-stop decision, next session open; planned liquidation
at purchase+10 close if no time-stop. This compares TIME EXITS, not all five layers.
Daily bars cannot verify auction queue fills; missing provenance remains explicit.
"""
import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from data_integrity import atomic_json_write, trade_fee, valid_price

BASE = Path(__file__).resolve().parent


def stats(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean_pct": round(statistics.mean(values), 4),
            "median_pct": round(statistics.median(values), 4),
            "win_pct": round(sum(v > 0 for v in values) / len(values) * 100, 2)}


def strict_signal(s):
    return s.get("out") == "强候选" and s.get("l1_gate") in ("正常模式", "降低权重")


def limit_price(previous, rate):
    return float((Decimal(str(previous)) * (1 + Decimal(str(rate))))
                 .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def board_rate(signal):
    code = signal["code"][-6:]
    # No inferred historic ST status / Beijing / IPO exemption rules.
    if "ST" in signal.get("name", "").upper() or "退" in signal.get("name", ""):
        return None
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("00", "60")):
        return 0.10
    return None


def exit_order(bars, calendar, buy_idx, buy_price, rate, mode):
    """Returns a priced order or None (right-censored / missing data).

    Close/open locked at lower limit => conservatively defer. This is a daily-bar
    assumption, not proof of a queue fill. All comparisons use market sessions.
    """
    planned_idx = buy_idx + 1 if mode == "D1" else buy_idx + 10
    field = 2  # scheduled close
    for idx in range(buy_idx + 1, len(calendar)):
        bar = bars.get(calendar[idx])
        prev = bars.get(calendar[idx - 1])
        if bar is None or prev is None:
            return None  # do not compress suspension/missing dates into trading days
        if idx >= planned_idx:
            price = bar[field]
            if not valid_price(price):
                return None
            if price <= limit_price(prev[2], -rate):
                continue
            return {"date": calendar[idx], "price": price,
                    "session": "open" if field == 1 else "close",
                    "delayed": idx > planned_idx}
        if mode == "D3D5":
            age = idx - buy_idx
            gain = (bar[2] / buy_price - 1) * 100
            if (age >= 5 and gain < 5) or (age >= 3 and gain < 2):
                planned_idx, field = idx + 1, 1  # close decision cannot fill at same close
    return None


def make_row(signal, rows, calendar):
    day = signal["signal_date"]
    bars = {k[0]: k for k in rows}
    rate = board_rate(signal)
    if rate is None:
        return None, "unsupported_security_rules"
    if day not in calendar or day not in bars:
        return None, "signal_date_missing"
    idx = calendar.index(day)
    if idx + 2 >= len(calendar):
        return None, "forward_window_missing"
    dates = calendar[idx:idx + 3]
    if any(d not in bars for d in dates):
        return None, "missing_market_session"
    t, t1, t2 = (bars[d] for d in dates)
    if any(not valid_price(v) for k in (t, t1, t2) for v in k[1:5]):
        return None, "invalid_price"
    # Cached bars lack adjustment metadata. Reject obvious mixed bases; passing
    # this check still does NOT certify absence of corporate actions.
    if not valid_price(signal.get("price")) or abs(t[2] - signal["price"]) > max(0.02, signal["price"] * .001):
        return None, "signal_price_basis_mismatch"
    if len([k for k in rows if k[0] <= day]) < 25:
        return None, "insufficient_listing_history"
    if t1[1] >= limit_price(t[2], rate):
        return None, "opening_limit_up_no_fill_assumed"
    entry = t1[1]
    shares = int(100000 / entry / 100) * 100
    minimum = 200 if signal["code"][-6:].startswith("68") else 100
    if shares < minimum:
        return None, "lot_budget"
    d1 = exit_order(bars, calendar, idx + 1, entry, rate, "D1")
    d35 = exit_order(bars, calendar, idx + 1, entry, rate, "D3D5")
    amount = shares * entry
    row = {"signal_date": day, "code": signal["code"], "name": signal.get("name"),
           "buy_date": dates[1], "buy_price": entry, "shares": shares,
           "l1_state": signal.get("l1_state"), "rotation": signal.get("rotation"),
           "gap_pct": (entry / t[2] - 1) * 100,
           "intraday_pct": (t1[2] / entry - 1) * 100,
           "old_d1_pct": (t1[2] / t[2] - 1) * 100,
           "exec_t2_gross_pct": (t2[2] / entry - 1) * 100,
           "gap_log": math.log(entry / t[2]), "intraday_log": math.log(t1[2] / entry),
           "D1": d1, "D3D5": d35}
    for mode, order in (("D1", d1), ("D3D5", d35)):
        if order:
            proceeds = order["price"] * shares
            fees = trade_fee("buy", amount) + trade_fee("sell", proceeds)
            order["net_pct"] = (proceeds - amount - fees) / (amount + trade_fee("buy", amount)) * 100
    return row, None


def portfolio_d1(rows, kline_cache, calendar, slippage_bps=0):
    """Conditional 1M simulation: opening entries, scheduled closing exits.

    Same capital/day caps as the live script, but entries only occur at T+1 open.
    This is research-only. Closing proceeds cannot fund that morning's purchases.
    """
    cash = 1000000.0
    holdings, trades, equity = {}, [], []
    skip = Counter()
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["buy_date"]].append(row)
    bars = {c: {k[0]: k for k in ks} for c, ks in kline_cache.items()}
    if not rows:
        return {"trades": 0}
    start = min(by_day)
    peak, max_dd = 1000000.0, 0.0
    for day in [d for d in calendar if d >= start]:
        buy_count = 0
        for row in by_day[day]:
            code = row["code"]
            if code in holdings:
                skip["already_held"] += 1
                continue
            rot = row.get("rotation") or ""
            cap, max_buys = ((.4, 2) if "加速轮动" in rot or "暂无对比" in rot
                            else ((.9, 5) if row.get("l1_state") == "多头趋势" else (.6, 3)))
            value = sum(bars[c].get(day, [None, h["mark"]])[1] * h["shares"]
                        for c, h in holdings.items())
            if buy_count >= max_buys or (value + 100000) / (cash + value) > cap:
                skip["position_limit"] += 1
                continue
            price = row["buy_price"] * (1 + slippage_bps / 10000)
            shares = int(100000 / price / 100) * 100
            amount = price * shares
            debit = amount + trade_fee("buy", amount)
            minimum = 200 if code[-6:].startswith("68") else 100
            if shares < minimum or debit > cash:
                skip["cash_or_lot"] += 1
                continue
            cash -= debit
            holdings[code] = {**row, "shares": shares, "mark": price, "debit": debit}
            trades.append({"date": day, "type": "buy", "code": code, "shares": shares,
                           "price": price, "fee": trade_fee("buy", amount)})
            buy_count += 1
        for code, h in list(holdings.items()):
            order = h["D1"]
            if order and order["date"] == day:
                price = order["price"] * (1 - slippage_bps / 10000)
                amount = price * h["shares"]
                cash += amount - trade_fee("sell", amount)
                trades.append({"date": day, "type": "sell", "code": code, "shares": h["shares"],
                               "price": price, "fee": trade_fee("sell", amount)})
                del holdings[code]
            elif day in bars[code]:
                h["mark"] = bars[code][day][2]
        value = sum(h["mark"] * h["shares"] for h in holdings.values())
        total = cash + value
        peak = max(peak, total)
        max_dd = min(max_dd, total / peak - 1)
        equity.append({"date": day, "cash": round(cash, 2), "total_value": round(total, 2),
                       "holdings_value": round(value, 2)})
    return {"return_pct": round((equity[-1]["total_value"] / 1000000 - 1) * 100, 4),
            "max_drawdown_pct": round(max_dd * 100, 4), "open_positions": len(holdings),
            "slippage_bps_each_side": slippage_bps, "skipped": dict(skip),
            "equity": equity, "trades": trades}


def run(base=BASE):
    tracker_path, kl_path = base / "data/signal_tracker.json", base / "data/kline_cache.json"
    all_signals = json.loads(tracker_path.read_text(encoding="utf-8"))["signals"]
    cache = json.loads(kl_path.read_text(encoding="utf-8"))
    calendar = sorted({k[0] for rows in cache.values() for k in rows})
    legacy_seen, legacy_rows = set(), []
    for signal in sorted(all_signals, key=lambda s: s["signal_date"]):
        label = signal.get("out", "")
        if "风控" in label or label == "弱" or "门控禁止" in label or signal["code"] in legacy_seen:
            continue
        legacy_seen.add(signal["code"])
        dates = [k[0] for k in cache.get(signal["code"], [])]
        if signal["signal_date"] in dates and dates.index(signal["signal_date"]) + 2 < len(dates):
            legacy_rows.append(signal)
    legacy_audit = {"original_valid_rows": len(legacy_rows),
                    "paused_gate_rows": sum(s.get("l1_gate") == "暂停交易" for s in legacy_rows),
                    "strict_label_and_gate_rows": sum(strict_signal(s) for s in legacy_rows)}
    counts = Counter(all_signals=len(all_signals))
    selected, seen = [], set()
    for signal in sorted(all_signals, key=lambda s: s["signal_date"]):
        if not strict_signal(signal):
            continue
        counts["strict_label_and_gate"] += 1
        if signal.get("pct", 0) < 9.5:
            counts["below_live_buy_pct_threshold"] += 1
            continue
        key = signal["signal_date"], signal["code"]
        if key in seen:
            counts["duplicate_same_day"] += 1
            continue
        seen.add(key)
        folder = base / "data/cache" / signal["signal_date"]
        try:
            l1 = json.loads((folder / "l1_index.json").read_text(encoding="utf-8"))
            rotation = json.loads((folder / "l2_rotation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counts["missing_gate_cache"] += 1
            continue
        if l1.get("backfilled") or l1.get("date") != signal["signal_date"]:
            counts["backfilled_or_wrong_date"] += 1
            continue
        label = rotation.get("rotation_label", "")
        if not label or rotation.get("date") != signal["signal_date"]:
            counts["missing_rotation"] += 1
            continue
        if "电风扇" in label or "全面暂停" in label:
            counts["rotation_blocks"] += 1
            continue
        selected.append({**signal, "rotation": label})
    rows = []
    for signal in selected:
        row, reason = make_row(signal, cache.get(signal["code"], []), calendar)
        if reason:
            counts[reason] += 1
        else:
            rows.append(row)
    counts["entry_eligible_under_model"] = len(rows)
    paired = [r for r in rows if r["D1"] is not None and r["D3D5"] is not None]
    counts["matched_completed_exits"] = len(paired)
    summary = {name: stats([r[name] for r in rows]) for name in
               ("gap_pct", "intraday_pct", "old_d1_pct", "exec_t2_gross_pct")}
    summary["paired_D1_net"] = stats([r["D1"]["net_pct"] for r in paired])
    summary["paired_D3D5_net"] = stats([r["D3D5"]["net_pct"] for r in paired])
    daily = defaultdict(list)
    for r in paired:
        daily[r["signal_date"]].append(r["D1"]["net_pct"] - r["D3D5"]["net_pct"])
    summary["paired_daily_D1_minus_D3D5"] = stats([statistics.mean(v) for v in daily.values()])
    # Additive attribution only in log space, on identical rows, never ratios of medians.
    summary["mean_log_components"] = ({"gap": statistics.mean(r["gap_log"] for r in rows),
                                        "intraday": statistics.mean(r["intraday_log"] for r in rows)} if rows else {})
    return {"status": "conditional_historical_research_not_OOS_or_verified_execution",
            "legacy_sample_audit": legacy_audit,
            "input_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (tracker_path, kl_path)},
            "funnel": dict(counts), "signal_days": len({r["signal_date"] for r in rows}),
            "summary": summary, "rows": rows,
            "portfolio_D1": [portfolio_d1(rows, cache, calendar, bps) for bps in (0, 10, 30)],
            "limitations": [
                "历史标签跨策略版本且部分快照无采集时间/完整来源；排除 backfilled 标记不等于历史已全部认证。",
                "日线无竞价委托队列；开盘非涨停假设可买，开盘涨停拒买。不是实盘成交证明。",
                "K线缓存缺复权元数据：仅排除明显价格基准差异，未完整处理分红送转。",
                "交易日历来自本地K线日期并集；股票缺该市场交易日则不顺延样本日期。",
                "D3D5 是时间退出对比，未模拟板块/炸板等完整五层；持有10交易日收盘退出是研究补充假设。",
                "组合仅模拟D1；不能将逐信号中位数当组合收益。未执行账户修复历史的反事实重放。",
                "停牌或后续数据缺失不能伪装卖出；组合保留未退出持仓并按最后可用价暂估。",
                "只作历史诊断，未改变现行买入时点、评分、门控或默认止损模式。"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BASE / "data/execution_review.json")
    args = parser.parse_args()
    report = run()
    atomic_json_write(args.output, report)
    print(json.dumps({k: report[k] for k in ("status", "funnel", "signal_days", "summary")}, ensure_ascii=False, indent=2))
    for p in report["portfolio_D1"]:
        print("D1 conditional portfolio:", {k: v for k, v in p.items() if k not in ("trades", "equity")})
    print("Saved:", args.output)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
