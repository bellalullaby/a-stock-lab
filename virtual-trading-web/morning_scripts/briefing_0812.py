#!/usr/bin/env python3
"""A股SOP盘前简报 2026-08-12（08:30 定时任务）
数据截至 2026-08-11 收盘（使用 data_collector 缓存），面向 2026-08-12 交易。

⚠️ 本次运行网络受限（沙箱出站对财经接口 502），无法重新采集/拉取实时行情，
   使用 2026-08-11 22:22 采集的收盘缓存 + 晚间补跑已存持仓快照。
"""
import json, os, sys, io, time, requests
from datetime import datetime, date
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_REPO = Path(__file__).resolve().parent
while not (_REPO / "common_paths.py").exists():
    _REPO = _REPO.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO, CACHE_DIR

run_date = "2026-08-12"          # 运行日（今天）
data_date = "2026-08-11"         # 数据截至日（最近交易日收盘）
facing = "2026-08-12"            # 面向交易
CACHE = CACHE_DIR / data_date

print(f"=== A股SOP盘前简报 {run_date} ===")
print(f"   数据截至 {data_date} 收盘, 面向 {facing} 交易")
print(f"   运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

l1 = load_json(CACHE / "l1_index.json")
l2 = load_json(CACHE / "l2_zt_pool.json")
l2_zb = load_json(CACHE / "l2_zb_pool.json")
l2b = load_json(CACHE / "l2_boards.json")
rot = load_json(CACHE / "l2_rotation.json")
l3 = load_json(CACHE / "l3_stocks.json")

with open(PORTFOLIO, "r", encoding="utf-8") as f:
    pf = json.load(f)

idx = l1["indices"]

# ── L1 摘要 ──
def fmt_idx(name, k):
    v = idx[k]
    return (f"{name}收{v['close']:.0f}({v.get('chg_pct',0):+.1f}%) "
            f"MA20={v['ma20']:.0f}({'↑' if v.get('ma20_rising') else '↓'}) "
            f"MA60={v['ma60']:.0f}")

l1_text = (fmt_idx("上证", "sh000001") + "; " +
           fmt_idx("深证", "sz399001") + "; " +
           fmt_idx("创业板", "sz399006") +
           f"。均涨幅{l1.get('avg_chg_pct', 0):+.1f}% 量比{l1.get('avg_vol_ratio', 0):.2f}。"
           f"{l1.get('volume_analysis', '')}")

regime = l1.get("regime", "震荡市")
gate = "正常模式" if regime == "多头趋势" else ("暂停交易" if regime == "系统性风险" else "降低权重")

# ── L2 摘要 ──
zt_stocks = l2.get("stocks", [])
zt_count = len(zt_stocks)
zb_count = l2_zb.get("total", 0)
zbr = round(zb_count / (zt_count + zb_count) * 100, 1) if (zt_count + zb_count) > 0 else 0
max_lb = max((s.get("lb", 0) for s in zt_stocks), default=0)
max_lb_name = next((s.get("name", "") for s in zt_stocks if s.get("lb", 0) == max_lb), "")
ind_cnt = Counter(s.get("hybk", "") for s in zt_stocks if s.get("hybk"))
top_sectors = ind_cnt.most_common(5)
boards_text = ", ".join(f"{n}({c})" for n, c in top_sectors)
l2_text = f"{zt_count}家涨停, 炸板率{zbr}%, 最高{max_lb}板({max_lb_name}), 主线: {boards_text}"
rotation_label = rot.get("rotation_label", "")

# ── L3 信号（从缓存转换）──
l3_stocks = l3.get("stocks", [])
l3_summary_obj = l3.get("summary", {})
signals = []
for s in l3_stocks:
    signals.append({
        "code": s.get("tx_code", ""),
        "name": s.get("name", ""),
        "lbc": s.get("lb", 1),
        "price": s.get("price", 0),
        "pct": s.get("chg_pct", 0),
        "pe": s.get("pe"),
        "buy": s.get("buy_score", 0),
        "sell": 0,
        "out": s.get("label", ""),
        "industry": s.get("hybk", ""),
        "hits": s.get("rules", []),
        "turnover": s.get("turnover", 0)
    })

strong = [s for s in signals if s.get("out", "").startswith("强候选")]
watch = [s for s in signals if s.get("out", "") == "观察"]
risk = [s for s in signals if s.get("out", "").startswith("风控")]
buy_ge3 = sum(1 for s in signals if s.get("buy", 0) >= 3)

# ── 昨日信号回顾（08-10 信号 → 08-11 表现，取 08-11 收盘简报记录）──
closing_entries = [e for e in pf["daily_log"] if "收盘" in e.get("session", "")]
prev_close_entry = closing_entries[-1] if closing_entries else None
tracking = []
if prev_close_entry and prev_close_entry.get("yesterday_review"):
    for t in prev_close_entry["yesterday_review"]:
        tracking.append({
            "code": t.get("code", ""),
            "name": t.get("name", ""),
            "lbc": t.get("today_lb", t.get("lbc", 1)),
            "yesterday_price": t.get("yesterday_price", 0),
            "today_price": t.get("today_price", 0),
            "today_pct": t.get("today_pct", 0),
            "status": t.get("status", "")
        })
hits_up = [t for t in tracking if t.get("today_pct", 0) > 0]
hits_zt = [t for t in tracking if t.get("status") in ("涨停", "✅涨停")]
hits_down = [t for t in tracking if t.get("today_pct", 0) < 0]
n_track = len(tracking)
up_rate = len(hits_up) / n_track * 100 if n_track else 0

# ── 持仓估值（网络不可用 → 使用晚间补跑已存快照 = 08-11 收盘价）──
last_entry = pf["daily_log"][-1]
saved_snapshot = last_entry.get("holdings_snapshot", []) if last_entry else []
holdings = pf.get("holdings", [])
cash = pf.get("account", {}).get("cash", 0)

if saved_snapshot:
    holdings_snapshot = saved_snapshot
    hold_value = sum(h.get("market_value", 0) for h in holdings_snapshot)
    print(f"  持仓估值: 使用晚间补跑快照 ({len(holdings_snapshot)}只, 08-11收盘价) → 持仓市值¥{hold_value:,.0f}")
else:
    holdings_snapshot = []
    hold_value = 0.0
    for h in holdings:
        cost = h.get("cost", h.get("buy_price", 0))
        shares = h.get("shares", 0)
        mv = cost * shares
        hold_value += mv
        holdings_snapshot.append({**h, "market_price": cost, "market_value": round(mv, 2),
                                   "pnl": 0, "chg_pct": 0, "note": "网络不可用，以成本价估值"})
    print(f"  持仓估值: 无快照，以成本价兜底 → 持仓市值¥{hold_value:,.0f}")

total_value = cash + hold_value
pnl = total_value - pf.get("account", {}).get("initial_capital", 1000000)
pnl_pct = pnl / pf.get("account", {}).get("initial_capital", 1000000) * 100 if pf.get("account", {}).get("initial_capital") else 0
print(f"  现金¥{cash:,.0f} + 持仓¥{hold_value:,.0f} = 总资产¥{total_value:,.0f} ({pnl:+,.0f} / {pnl_pct:+.2f}%)")

# ── 决策 ──
if regime == "系统性风险":
    decision = (f"系统性风险门控→暂停交易，不执行任何虚拟买卖。\n\n"
                f"📊 {run_date}盘前（数据截至{data_date}收盘）：{l1_text}\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
elif regime == "震荡市":
    decision = (f"震荡市门控→降低权重。当前虚拟账户满仓（{len(holdings)}只），不执行新增买入。\n\n"
                f"📊 {run_date}盘前（数据截至{data_date}收盘）：{l1_text}\n\n"
                f"📈 市场结构：{l2_text}。\n"
                f"轮动判定：{rotation_label}。\n\n"
                f"🔄 昨日信号回顾（08-10信号→08-11表现）：共{n_track}只，涨停{len(hits_zt)}只，正收益{len(hits_up)}只({up_rate:.0f}%)，下跌{len(hits_down)}只。\n"
                f"   今日盘前候选：技术面达标{buy_ge3}只，其中强候选{len(strong)}只 / 观察{len(watch)}只 / 风控{len(risk)}只（轮动电风扇/震荡市下不新增买入，仅跟踪）。\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
else:
    decision = (f"多头趋势→正常模式。{len(strong)}只候选可纳入虚拟持仓。\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")

# ── 观察要点 ──
observations = []
observations.append(f"L1收盘: {l1_text}")
if max_lb_name:
    observations.append(f"最高板{max_lb_name} {max_lb}连板 — {'情绪标的属性' if max_lb >= 7 else '连板梯队中段'}")
if top_sectors:
    observations.append(f"主线分布：{' / '.join(f'{n}({c})' for n, c in top_sectors[:3])} — {'主线延续' if ind_cnt.get(top_sectors[0][0], 0) >= 10 else '多线分散、轮动偏快'}")
observations.append(f"涨停{zt_count}家 / 炸板率{zbr}% — {'情绪偏强' if zt_count > 100 and zbr < 20 else ('情绪中性' if zt_count > 80 else '情绪偏弱')}")
observations.append(f"持仓{len(holdings)}只浮盈{sum(h.get('pnl',0) for h in holdings_snapshot):+,.0f}元 — 关注开盘去留")
if strong:
    observations.append(f"盘前强候选{len(strong)}只：{'、'.join(s['name'] for s in strong[:10])}")
observations.append("L1震荡市+轮动电风扇 → 不追高、只观察，等待①主板指数站稳MA60②涨停>80且炸板率<20%③主线连续2日不走一日游")
if not saved_snapshot:
    observations.append("⚠️ 本次运行网络受限，持仓以成本价兜底估值，待行情恢复后重估。")

# ── 新条目 ──
entry = {
    "date": run_date,
    "session": f"盘前简报(08:30运行, 数据截至{data_date}收盘, 面向{facing}交易)",
    "l1_state": regime,
    "l1_gate": gate,
    "l1_bull": sum(1 for v in idx.values() if v.get("above_ma20") and v.get("above_ma60") and v.get("ma20_rising")),
    "l1_risk": sum(1 for v in idx.values() if not v.get("above_ma20") and not v.get("ma20_rising")),
    "l1_changed": False,
    "l1_detail": l1_text,
    "l1_indexes": {
        k: {
            "name": v.get("name", k),
            "close": v["close"],
            "chg_pct": v.get("chg_pct", 0),
            "ma20": v["ma20"],
            "ma60": v["ma60"],
            "above_ma20": v.get("above_ma20"),
            "above_ma60": v.get("above_ma60"),
            "ma20_rising": v.get("ma20_rising"),
            "ma20_direction": v.get("ma20_direction", ""),
            "vol_ratio": v.get("vol_ratio")
        }
        for k, v in idx.items()
    },
    "l2_summary": l2_text,
    "l2_zt_count": zt_count,
    "l2_zb_count": zb_count,
    "l2_zbr": zbr,
    "l2_max_lb": max_lb,
    "l2_sectors": [{"name": n, "count": c} for n, c in top_sectors],
    "l2_rotation": rotation_label,
    "l3_summary": f"技术面达标{buy_ge3}只(强{len(strong)}/观{len(watch)}/风{len(risk)}) — {rotation_label}",
    "signals": signals,
    "yesterday_review": tracking,
    "trades": {"bought": [], "sold": [], "note": "震荡市→降低权重，满仓不新增，盘前仅记录观察。"},
    "holdings_snapshot": holdings_snapshot,
    "decisions": decision,
    "observations": observations,
    "portfolio_snapshot": {"cash": round(cash, 2), "total_value": round(total_value, 2),
                            "pnl_pct": round(pnl_pct, 2), "holdings": len(holdings)},
    "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "data_note": "网络受限，基于08-11收盘缓存+晚间快照，未重新采集"
}

# ── 写入 portfolio.json（替换当天已有盘前条目，避免重复）──
has_today = any(e["date"] == run_date and "盘前" in e.get("session", "") for e in pf["daily_log"])
if has_today:
    for i, e in enumerate(pf["daily_log"]):
        if e["date"] == run_date and "盘前" in e.get("session", ""):
            pf["daily_log"][i] = entry
            break
    print("  已替换当日已有盘前条目")
else:
    pf["daily_log"].append(entry)
    print("  已追加新条目")

pf["account"]["last_updated"] = f"{run_date} 08:30 盘前简报"
pf["account"]["total_value"] = round(total_value, 2)
pf["account"]["pnl"] = round(pnl, 2)
pf["account"]["pnl_pct"] = round(pnl_pct, 2)

with open(PORTFOLIO, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

print(f"\n✅ Portfolio updated: {PORTFOLIO}")
print(f"   daily_log 条目数: {len(pf['daily_log'])}")
print(f"   账户: 现金¥{pf['account']['cash']:,.0f} 总资产¥{pf['account']['total_value']:,.0f} ({pf['account']['pnl']:+,.0f} / {pf['account']['pnl_pct']:+.2f}%)")

# ── 摘要输出 ──
print("\n" + "="*60)
print(f"🌅 A股SOP盘前观察简报 {run_date}（数据截至{data_date}收盘，面向{facing}）")
print(f"L1: {regime} → {gate}")
print(f"   {l1_text}")
print(f"L2: {zt_count}家涨停, 炸板率{zbr}%, 最高{max_lb}板({max_lb_name}), 主线: {boards_text}")
print(f"   轮动: {rotation_label}")
print(f"L3: 技术面达标{buy_ge3}只(强{len(strong)}/观{len(watch)}/风{len(risk)})")
if signals:
    for s in signals[:8]:
        pe_str = f"PE{s['pe']:.0f}" if s.get('pe') and s['pe'] > 0 else "PE负"
        print(f"   {s['code']} {s['name']:8s} {s['lbc']}板 ¥{s['price']:.2f} +{s['pct']:.1f}% {pe_str} → {s['out']}")
print(f"昨日信号回顾: {len(hits_up)}/{n_track}正收益({up_rate:.0f}%), {len(hits_zt)}涨停, {len(hits_down)}下跌")
print(f"持仓: {len(holdings)}只, 浮盈{sum(h.get('pnl',0) for h in holdings_snapshot):+,.0f}元")
print(f"虚拟账户: ¥{pf['account']['total_value']:,.0f} ({pf['account']['pnl']:+,.0f} / {pf['account']['pnl_pct']:+.2f}%)")
print("\n⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
print("="*60)
