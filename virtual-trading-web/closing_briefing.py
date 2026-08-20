# -*- coding: utf-8 -*-
"""
closing_briefing.py — A股SOP收盘简报（通用版）
============================================================
替代每天从零生成的 closing_briefing_MMDD.py。
用法：
    python closing_briefing.py            # 今天收盘简报（自动用 date.today()）
    python closing_briefing.py --date 2026-08-10   # 回补历史日期
    python closing_briefing.py --dry-run  # 只计算打印，不写 portfolio.json

前置：当天缓存必须已采集（data_collector.py 跑过），否则报错退出。

流程：L1 收盘复核 → L2 涨停池/题材 → L3 个股打分
      → 盘前候选对比 → 五层止损 → 虚拟买入 → 更新模拟账户
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ── 路径自动解析（本地/沙箱双兼容） ──
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_paths import PORTFOLIO, cache_dir

# ── 参数 ──
parser = argparse.ArgumentParser(description="A股SOP收盘简报（通用版）")
parser.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
parser.add_argument("--dry-run", action="store_true", help="只计算打印，不写 portfolio.json")
parser.add_argument("--pf", help="指定 portfolio.json 路径（回测/预演用，默认 common_paths.PORTFOLIO）")
parser.add_argument("--force", action="store_true", help="当天已运行时强制重跑（谨慎使用）")
args = parser.parse_args()

today = args.date or date.today().strftime("%Y-%m-%d")
BASE = cache_dir(today)
PF = Path(args.pf) if args.pf else PORTFOLIO

# ── 前置检查：缓存是否齐 ──
REQUIRED_CACHE = ["l1_index.json", "l2_zt_pool.json", "l2_zb_pool.json",
                  "l2_rotation.json", "l3_stocks.json"]
missing = [f for f in REQUIRED_CACHE if not (BASE / f).exists()]
if missing:
    print(f"❌ {today} 缓存不完整，缺少: {missing}")
    print(f"   请先运行: python data_collector.py --date {today}")
    sys.exit(1)

with open(BASE / "l1_index.json", encoding="utf-8") as f:
    l1 = json.load(f)
with open(BASE / "l2_zt_pool.json", encoding="utf-8") as f:
    l2_zt = json.load(f)
with open(BASE / "l2_zb_pool.json", encoding="utf-8") as f:
    l2_zb = json.load(f)
with open(BASE / "l2_rotation.json", encoding="utf-8") as f:
    rot = json.load(f)
with open(BASE / "l3_stocks.json", encoding="utf-8") as f:
    l3 = json.load(f)
with open(PF, encoding="utf-8") as f:
    pf = json.load(f)

# ═══════════ 幂等锁：当天已跑过则跳过（防重跑覆盖） ═══════════
already_run = any(
    e.get("date") == today and str(e.get("session", "")).startswith("收盘简报")
    for e in pf["daily_log"]
)
if already_run and not getattr(args, "force", False):
    print(f"⏭️  {today} 已有收盘简报，当天已运行过，跳过（如需强制重跑加 --force）")
    sys.exit(0)

# ═══════════ 缓存口径防御：非 15:30 收盘快照则警告 ═══════════
# 若缓存是晚上重采的，涨停池/炸板池与收盘时刻不一致，结果可能失真
try:
    import time as _time
    mtime = (BASE / "l2_zt_pool.json").stat().st_mtime
    mtime_h = _time.localtime(mtime).tm_hour + _time.localtime(mtime).tm_min / 60
    if abs(mtime_h - 15.5) > 2:  # 距 15:30 超过 2 小时
        print(f"⚠️  缓存快照时间 {_time.strftime('%m-%d %H:%M', _time.localtime(mtime))}"
              f" 非 15:30 收盘快照，结果可能失真")
except Exception:
    pass  # mtime 读取失败不阻断

# ═══════════ L1 收盘复核 ═══════════
idx = l1["indices"]
state = l1["regime"]
bull = 0
risk = 0
l1_detail_lines = []
for k in ["sh000001", "sz399001", "sz399006"]:
    d = idx[k]
    above20 = d["above_ma20"]
    above60 = d["above_ma60"]
    ma20_up = d["ma20_direction"] == "UP"
    if above20 and above60 and ma20_up:
        bull += 1
    if not above20 and not ma20_up:
        risk += 1
    l1_detail_lines.append(
        f"{d['name']}收{d['close']}({d['chg_pct']:+.1f}%) "
        f"MA20={d['ma20']:.0f}({'UP' if ma20_up else 'DOWN'}) MA60={d['ma60']:.0f}"
    )
if bull == 3:
    gate = "正常模式"
elif risk >= 2:
    gate = "暂停交易"
else:
    gate = "降低权重"

# L1 与盘前对比（同一天盘前简报）
l1_changed = False
pre_entry = None
for e in pf["daily_log"]:
    if e.get("date") == today and str(e.get("session", "")).startswith("盘前简报"):
        pre_entry = e
        break
if pre_entry and "l1_indexes" in pre_entry:
    for k in ["sh000001", "sz399001", "sz399006"]:
        pd = pre_entry["l1_indexes"].get(k)
        cd = idx.get(k)
        if pd and cd and abs(pd["close"] - cd["close"]) / pd["close"] > 0.01:
            l1_changed = True

l1_detail = " ".join(l1_detail_lines) + \
    f"。均涨幅{l1['avg_chg_pct']:+.1f}% 量比{l1['avg_vol_ratio']:.2f}。{l1['volume_analysis']}"

# ═══════════ L2 涨停池/题材 ═══════════
zt_stocks = l2_zt["stocks"]
zt_count = l2_zt["total"]
zb_count = l2_zb["total"]
zbr = round(zb_count / (zt_count + zb_count) * 100, 1) if zt_count + zb_count else 0
lbc_dist = Counter(s.get("lb", 0) for s in zt_stocks)
max_lb = max(lbc_dist) if lbc_dist else 0
ind_cnt = Counter(s.get("hybk", "") for s in zt_stocks if s.get("hybk"))
main_sectors = ind_cnt.most_common(5)
main_line = ", ".join(f"{k}({v})" for k, v in main_sectors[:3])
rotation_label = rot.get("rotation_label", "")
top1 = main_sectors[0] if main_sectors else ("?", 0)
concentration = round(top1[1] / zt_count * 100, 1) if zt_count else 0

# ═══════════ L3 信号分布 ═══════════
l3_stocks = l3["stocks"]
strong = sum(1 for s in l3_stocks if s.get("label") == "强候选")
observe = sum(1 for s in l3_stocks if s.get("label") == "观察")
riskc = sum(1 for s in l3_stocks if s.get("label") == "风控")
weak = sum(1 for s in l3_stocks if s.get("label") == "弱")

# ═══════════ 盘前信号对比 ═══════════
# 基准：当天盘前简报的 signals → 用腾讯实时价算当日表现
from data_collector import fetch_qt_batch

pre_signals = []
if pre_entry:
    pre_signals = pre_entry.get("signals", [])

yesterday_review = []
if pre_signals:
    codes = [s.get("code", "") for s in pre_signals if s.get("code")]
    q = fetch_qt_batch(codes) if codes else {}
    for s in pre_signals:
        code = s.get("code", "")
        pp = s.get("price", 0)
        qd = q.get(code, {})
        tp = qd.get("price")
        pct = qd.get("chg_pct")
        if tp is None or tp == 0:
            tp = pp
            pct = 0
        chg_from_pre = (tp - pp) / pp * 100 if (pp and pp > 0) else 0
        if pct >= 9.5:
            status = "涨停"
        elif pct >= 5:
            status = "大涨"
        elif pct >= 1:
            status = "小涨"
        elif pct >= -1:
            status = "平盘"
        elif pct >= -5:
            status = "小跌"
        elif pct >= -9.5:
            status = "大跌"
        else:
            status = "跌停"
        yesterday_review.append({
            "code": code, "name": s.get("name", ""), "lbc": s.get("lbc", 1),
            "pre_price": pp, "price": tp, "today_pct": round(pct, 2),
            "open": None, "high": None, "low": None,
            "status": status, "chg_from_pre": round(chg_from_pre, 2),
            "sealed": pct >= 9.5,
            "pre_label": s.get("out", ""), "pre_buy": s.get("buy", 0),
        })
print(f"盘前信号对比(今日盘前→收盘): {len(yesterday_review)} 只")

pos_n = sum(1 for r in yesterday_review if r["today_pct"] > 0)
neg_n = sum(1 for r in yesterday_review if r["today_pct"] < 0)
zt_n = sum(1 for r in yesterday_review if r["status"] == "涨停")
win_rate = pos_n / len(yesterday_review) * 100 if yesterday_review else 0

trades = []
cash = pf["account"]["cash"]
holdings = pf["holdings"]

# ═══════════ 虚拟买入：L3 强候选 + 收盘封板 ═══════════
can_buy = gate != "暂停交易" and "全面暂停" not in rotation_label and strong > 0
bought_codes = []
if can_buy:
    for s in l3_stocks:
        if s.get("label") == "强候选" and s.get("chg_pct", 0) >= 9.5:
            code = s["tx_code"]
            name = s["name"]
            price = s["price"]
            if any(h["code"] == code for h in holdings) or code in bought_codes:
                continue
            buy_amount = 100000
            shares = int(buy_amount / price / 100) * 100
            if shares >= 100 and cash >= shares * price:
                cash -= shares * price
                holdings.append({
                    "code": code, "name": name, "shares": shares, "cost": price,
                    "buy_date": today, "buy_price": price,
                    "hybk": s.get("hybk", ""),  # 必须存行业，板块止损依赖
                    "buy_reason": f"{s.get('lb', 0)}板强候选 收盘封板",
                })
                trades.append({"date": today, "type": "buy", "code": code, "name": name,
                               "shares": shares, "price": price, "amount": shares * price})
                bought_codes.append(code)
                print(f"  买入: {name} {shares}股 ¥{price:.2f}")

# ═══════════ 虚拟卖出：五层止损 ═══════════
from stop_loss import fetch_tencent_prices, run_stop_loss

sell_plan = []
if holdings:
    prices = fetch_tencent_prices([h["code"] for h in holdings])
    l2_boards = None
    boards_path = BASE / "l2_boards.json"
    if boards_path.exists():
        with open(boards_path, encoding="utf-8") as f:
            l2_boards = json.load(f)
    sell_plan = run_stop_loss(holdings, prices, l2_boards, l2_zt, l2_zb, today)

sold = []
for sp in sell_plan:
    code = sp["code"]
    name = sp["name"]
    price = sp["price"]
    shares = sp["shares"]
    amount = sp["amount"]
    reasons = "; ".join(sp["reasons"])
    half = sp.get("half", False)
    remaining = 0
    new_holdings = []
    for h in holdings:
        if h["code"] == code:
            left = h["shares"] - shares
            if left > 0:
                new_holdings.append({**h, "shares": left})
                remaining = left
        else:
            new_holdings.append(h)
    holdings = new_holdings
    cash += amount
    note = reasons + (f"（半仓减仓，剩{remaining}股）" if half else "")
    sold.append({
        "date": today, "type": "sell", "code": code, "name": name,
        "shares": shares, "price": price, "amount": amount,
        "note": note,
    })
    print(f"  ⛔ 止损卖出: {name} {shares}股 ¥{price:.2f} | {note}")

# ═══════════ 账户估值（持仓按实时价） ═══════════
hold_prices = fetch_tencent_prices([h["code"] for h in holdings]) if holdings else {}
total_hold = 0
holdings_snapshot = []
for h in holdings:
    cost = h.get("cost", h.get("buy_price", 0))
    mv_price = hold_prices.get(h["code"], cost)
    mv = mv_price * h["shares"]
    total_hold += mv
    pnl_h = (mv_price - cost) / cost * 100 if cost else 0
    holdings_snapshot.append({
        **h, "market_price": round(mv_price, 2), "market_value": round(mv, 2),
        "pnl": round(mv - cost * h["shares"], 2), "chg_pct": round(pnl_h, 2),
    })
total_value = cash + total_hold
pnl = total_value - pf["account"]["initial_capital"]
pnl_pct = pnl / pf["account"]["initial_capital"] * 100

trade_note = f"L1={state}→{gate}，买入{len(trades)}只/卖出{len(sold)}只"
trade_note += "（止损触发）" if sold else "（无止损触发）"

# ═══════════ dry-run 模式：到此为止，不写文件 ═══════════
if args.dry_run:
    print()
    print("=" * 50)
    print(f"🔍 DRY-RUN {today}（不写 portfolio.json）")
    print("=" * 50)
    print(f"L1: {state} → {gate}")
    print(f"L2: {zt_count}家涨停 炸板率{zbr}% 主线{main_line}")
    print(f"今日信号: {strong}只强候选, {observe}只观察")
    print(f"虚拟交易: 买入{len(trades)}只 / 卖出{len(sold)}只")
    if sold:
        for s in sold:
            print(f"  卖 {s['name']} {s['shares']}股 ¥{s['price']:.2f} | {s['note']}")
    print(f"账户将变为: ¥{total_value:,.0f} ({pnl_pct:+.2f}%)  现金: ¥{cash:,.0f}  持仓: {len(holdings)}只")
    print("=" * 50)
    sys.exit(0)

# ═══════════ 写回 portfolio.json ═══════════
pf["account"]["cash"] = round(cash, 2)
pf["account"]["total_value"] = round(total_value, 2)
pf["account"]["pnl"] = round(pnl, 2)
pf["account"]["pnl_pct"] = round(pnl_pct, 2)
pf["account"]["last_updated"] = f"{today} 收盘简报"
pf["holdings"] = holdings

# 每日快照写入 daily_snapshots（Web 折线图数据源，按日期去重）
pf["daily_snapshots"] = [
    s for s in pf.get("daily_snapshots", []) if s.get("date") != today
]
pf["daily_snapshots"].append({
    "date": today,
    "total_value": round(total_value, 2),
    "cash": round(cash, 2),
    "pnl": round(pnl, 2),
    "pnl_pct": round(pnl_pct, 2),
    "holdings": len(holdings),
})
pf["daily_snapshots"].sort(key=lambda s: s.get("date", ""))

# trades 写入前去重：过滤掉与本次 date+type+code 相同的旧记录，防历史重复追加
old_trades = pf.get("trades", [])
new_keys = {(t["date"], t["type"], t["code"]) for t in trades + sold}
pf["trades"] = [
    t for t in old_trades
    if (t.get("date"), t.get("type"), t.get("code")) not in new_keys
] + trades + sold

l2_summary = f"{zt_count}家涨停, 炸板率{zbr}%, 最高{max_lb}板, 主线: {main_line}"
l3_summary_text = f"强候选:{strong}, 观察:{observe}, 风控:{riskc}, 弱:{weak} — {rotation_label}"

decisions = (
    f"{state}→{gate}。{trade_note}。"
    f"盘前{len(yesterday_review)}只信号→今收盘: 正收益{pos_n}只({win_rate:.0f}%胜率), 涨停{zt_n}只, 下跌{neg_n}只。"
    f"主线集中度: {main_line} (TOP1 {top1[1]}/{zt_count}={concentration}%)。"
)

observations = [
    f"L1收盘: {l1_detail}",
    f"最高板{max_lb}板({', '.join(s['name'] for s in zt_stocks if s.get('lb', 0) == max_lb)}) — {'情绪标的属性' if max_lb >= 8 else '连板高度一般'}",
    f"主线分布: {main_line} — 行业集中度{concentration}%",
    f"涨停{zt_count}家 / 炸板率{zbr}% — {'情绪偏弱' if zbr >= 20 else '情绪正常'}",
    f"轮动判定: {rotation_label}",
]

signals_legacy = []
for s in l3_stocks:
    signals_legacy.append({
        "code": s.get("tx_code", s.get("code", "")),
        "name": s.get("name", ""),
        "lbc": s.get("lb", 0),
        "price": s.get("price", 0),
        "pct": s.get("chg_pct", 0),
        "pe": s.get("pe", 0),
        "buy": s.get("buy_score", 0),
        "sell": 0,
        "out": s.get("label", ""),
        "industry": s.get("industry", "") or s.get("hybk", ""),
        "hits": s.get("rules", []),
        "turnover": s.get("turnover", 0),
    })

daily_log_entry = {
    "date": today,
    "session": "收盘简报(15:30运行)",
    "l1_state": state,
    "l1_gate": gate,
    "l1_bull": bull,
    "l1_risk": risk,
    "l1_changed": l1_changed,
    "l1_detail": l1_detail,
    "l1_indexes": idx,
    "l2_summary": l2_summary,
    "l2_zt_count": zt_count,
    "l2_zb_count": zb_count,
    "l2_zbr": zbr,
    "l2_max_lb": max_lb,
    "l2_sectors": [{"name": k, "count": v} for k, v in main_sectors],
    "l2_rotation": rotation_label,
    "l3_summary": l3_summary_text,
    "signals": signals_legacy,
    "yesterday_review": yesterday_review,
    "trades": {"bought": trades, "sold": sold, "note": trade_note},
    "holdings_snapshot": holdings_snapshot,
    "decisions": decisions,
    "observations": observations,
    "portfolio_snapshot": {"cash": pf["account"]["cash"], "total_value": pf["account"]["total_value"], "pnl_pct": pf["account"]["pnl_pct"], "holdings": len(holdings)},
    "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}

# ═══════════ 交易快照合并保护（--force 重跑时防篡改历史） ═══════════
# 重跑时若缓存是晚上重采的，止损条件可能不满足 → 算出「0 买卖」，
# 若整体覆盖会把当天已发生的交易从 daily_log 快照里清掉（历史失真）。
# 策略：本次算出的 bought/sold 为空、而旧快照有交易 → 保留旧交易。
old_entry = None
for e in pf["daily_log"]:
    if e.get("date") == today and str(e.get("session", "")).startswith("收盘简报"):
        old_entry = e
        break

merged_any = False
if old_entry:
    old_t = old_entry.get("trades", {})
    new_t = daily_log_entry["trades"]
    old_bought = old_t.get("bought", [])
    old_sold = old_t.get("sold", [])
    if not new_t.get("bought") and old_bought:
        new_t["bought"] = old_bought
        merged_any = True
    if not new_t.get("sold") and old_sold:
        new_t["sold"] = old_sold
        merged_any = True
    if merged_any:
        # 合并后沿用旧 note（描述更贴近真实发生的交易）
        new_t["note"] = old_t.get("note", new_t.get("note", ""))
        print(f"⚠️  本次重跑算出空买卖，已保留旧快照交易"
              f"（bought {len(old_bought)} 条 / sold {len(old_sold)} 条）")

# 去重（同日期同 session 只保留一条）+ 追加 + 按日期排序
pf["daily_log"] = [
    e for e in pf["daily_log"]
    if not (e.get("date") == today and str(e.get("session", "")).startswith("收盘简报"))
]
pf["daily_log"].append(daily_log_entry)
pf["daily_log"].sort(key=lambda e: e.get("date", ""))

with open(PF, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

# ═══════════ 输出 ═══════════
print()
print("=" * 50)
print(f"收盘简报 {today}")
print("=" * 50)
print(f"L1: {state} → {gate}")
print(f"L2: {zt_count}家涨停 炸板率{zbr}% 主线{main_line}")
print(f"今日信号: {strong}只强候选, {observe}只观察")
print(f"虚拟交易: 买入{len(trades)}只 / 卖出{len(sold)}只")
if trades:
    for t in trades:
        print(f"  买 {t['name']} {t['shares']}股 ¥{t['price']:.2f}")
else:
    print("  (无)")
if sold:
    for s in sold:
        print(f"  卖 {s['name']} {s['shares']}股 ¥{s['price']:.2f} | {s['note']}")
print(f"账户: ¥{total_value:,.0f} ({pnl_pct:+.2f}%)  现金: ¥{cash:,.0f}  持仓: {len(holdings)}只")
print(f"盘前信号今日表现: {pos_n}/{len(yesterday_review)}正收益({win_rate:.0f}%), {zt_n}只涨停")
print("=" * 50)
