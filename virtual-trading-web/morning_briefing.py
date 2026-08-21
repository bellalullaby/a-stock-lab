# -*- coding: utf-8 -*-
"""
morning_briefing.py — A股SOP盘前观察简报（通用版·只读）
============================================================
替代每天从零生成的 morning_scripts/briefing_MMDD.py。

设计原则（Claude哥验收钉子）：
  1. 只读不写：绝不修改 portfolio.json（账户权威口径只在收盘写）。
     观察结果输出到 data/morning_briefing_{date}.json，供收盘联动。
  2. 幂等：同 date 只跑一次（观察文件已存在则跳过），--force 可重跑。
  3. 三模式：默认今天 / --dry-run / --date 指定。

用法：
    python morning_briefing.py             # 今天早报
    python morning_briefing.py --dry-run   # 预演（不写观察文件）
    python morning_briefing.py --date 2026-08-20
"""
import argparse
import json
import sys
import io
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common_paths import PORTFOLIO, CACHE_DIR

# ── 参数 ──
parser = argparse.ArgumentParser(description="A股SOP盘前观察简报（通用版·只读）")
parser.add_argument("--date", help="运行日 YYYY-MM-DD（默认今天，自动回退到最近交易日）")
parser.add_argument("--dry-run", action="store_true", help="只打印不写观察文件")
parser.add_argument("--force", action="store_true", help="观察文件已存在时强制重跑")
args = parser.parse_args()

# ── 日期计算：run_date（运行日）+ data_date（数据截至日=最近交易日） ──
def prev_trading_day(d):
    """从 d 往前找最近交易日（跳过周末；节假日由缓存存在性兜底）"""
    from datetime import timedelta
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:  # 5=周六 6=周日
        cur -= timedelta(days=1)
    return cur

run_date = args.date or date.today().strftime("%Y-%m-%d")
data_date = prev_trading_day(date.today()).strftime("%Y-%m-%d")
if args.date:
    # --date 指定时，数据截至日取缓存里该日期之前的最近日期
    avail = sorted([p.name for p in CACHE_DIR.iterdir() if p.is_dir()])
    cands = [d for d in avail if d < args.date]
    data_date = cands[-1] if cands else args.date

CACHE = CACHE_DIR / data_date
OBS_FILE = CACHE_DIR / f"morning_briefing_{run_date}.json"

# ── 前置检查：缓存是否齐 ──
REQUIRED_CACHE = ["l1_index.json", "l2_zt_pool.json", "l2_zb_pool.json",
                  "l2_rotation.json", "l2_boards.json", "l3_stocks.json"]
missing = [f for f in REQUIRED_CACHE if not (CACHE / f).exists()]
if missing:
    print(f"❌ 数据日 {data_date} 缓存不完整，缺少: {missing}")
    print(f"   请先运行: python data_collector.py --date {data_date}")
    sys.exit(1)

# ── 幂等锁：同 date 已生成观察文件则跳过（防双触发烧额度） ──
if OBS_FILE.exists() and not args.force and not args.dry_run:
    print(f"⏭️  {run_date} 观察文件已存在，跳过（防双触发；--force 可重跑）")
    sys.exit(0)

# ═══════════ 读数据（只读） ═══════════
def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

l1 = load_json(CACHE / "l1_index.json")
l2 = load_json(CACHE / "l2_zt_pool.json")
l2_zb = load_json(CACHE / "l2_zb_pool.json")
l2b = load_json(CACHE / "l2_boards.json")
rot = load_json(CACHE / "l2_rotation.json")
l3 = load_json(CACHE / "l3_stocks.json")
with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

print(f"=== A股SOP盘前简报 {run_date} ===")
print(f"   数据截至 {data_date} 收盘, 面向 {run_date} 交易")
print(f"   运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

idx = l1["indices"]

# ── L1 摘要 ──
def fmt_idx(name, k):
    v = idx[k]
    ma20_dir = "↑" if v.get("ma20_rising") else "↓"
    return (f"{name}收{v['close']:.0f}({v.get('chg_pct',0):+.1f}%) "
            f"MA20={v['ma20']:.0f}({ma20_dir}) MA60={v['ma60']:.0f}")

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
is_fan = "电风扇" in rotation_label

# ── L3 信号（从缓存转换） ──
l3_stocks = l3.get("stocks", [])
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

# ── 昨日信号回顾（从最近收盘简报的 yesterday_review 提取，只读） ──
closing_entries = [e for e in pf["daily_log"] if "收盘" in e.get("session", "")]
prev_close_entry = closing_entries[-1] if closing_entries else None
tracking = []
if prev_close_entry and prev_close_entry.get("yesterday_review"):
    for t in prev_close_entry["yesterday_review"]:
        tracking.append({
            "code": t.get("code", ""),
            "name": t.get("name", ""),
            "lbc": t.get("today_lb", t.get("lbc", 1)),
            "yesterday_price": t.get("yesterday_price", t.get("pre_price", 0)),
            "today_price": t.get("today_price", t.get("price", 0)),
            "today_pct": t.get("today_pct", 0),
            "status": t.get("status", "")
        })
hits_up = [t for t in tracking if t.get("today_pct", 0) > 0]
hits_zt = [t for t in tracking if t.get("status", "") in ("涨停", "✅涨停")]
hits_down = [t for t in tracking if t.get("today_pct", 0) < 0]
n_track = len(tracking)
up_rate = len(hits_up) / n_track * 100 if n_track else 0

# ── 持仓估值（只读：用数据日缓存收盘价，不拉实时） ──
# 修正：盘前简报应显示"数据日收盘"估值（实时行情是收盘脚本的活）
holdings = pf.get("holdings", [])
cash = pf.get("account", {}).get("cash", 0)
l3_price_map = {s.get("tx_code"): s.get("price") for s in l3_stocks}

holdings_snapshot = []
hold_value = 0.0
for h in holdings:
    cost = h.get("cost", h.get("buy_price", 0))
    shares = h.get("shares", 0)
    mp = l3_price_map.get(h.get("code", "")) or cost
    mv = round(mp * shares, 2)
    hold_value += mv
    holdings_snapshot.append({**h, "market_price": mp, "market_value": mv,
                              "pnl": round((mp - cost) * shares, 2),
                              "note": f"数据日({data_date})收盘估值"})
print(f"  持仓估值: {len(holdings)}只, 市值¥{hold_value:,.0f}（{data_date}收盘价）")

total_value = cash + hold_value
initial = pf.get("account", {}).get("initial_capital", 1000000)
pnl = total_value - initial
pnl_pct = pnl / initial * 100 if initial else 0

# ── 决策文本 ──
if regime == "系统性风险":
    decision = (f"系统性风险门控→暂停交易，不执行任何虚拟买卖。\n\n"
                f"📊 {run_date}盘前（数据截至{data_date}收盘）：{l1_text}\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
elif is_fan:
    decision = (f"震荡市门控→降低权重；轮动判定为电风扇行情→全面暂停，只观察，不执行任何新增买入。\n\n"
                f"📊 {run_date}盘前（数据截至{data_date}收盘）：{l1_text}\n"
                f"昨日为放量大跌日（涨停{zt_count}家/炸板率{zbr}%/最高{max_lb}板），防御性板块领涨且无涨停结构。\n\n"
                f"📈 市场结构：{l2_text}。\n轮动判定：{rotation_label}。\n\n"
                f"🔄 昨日信号回顾（{data_date}盘前信号→{data_date}收盘）：共{n_track}只，涨停{len(hits_zt)}只，正收益{len(hits_up)}只({up_rate:.0f}%)，下跌{len(hits_down)}只。\n"
                f"   今日盘前候选：技术面达标{buy_ge3}只，但全局禁止开仓。持仓{len(holdings)}只重点盯开盘去留。\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
elif regime == "震荡市":
    decision = (f"震荡市门控→降低权重。当前持仓{len(holdings)}只，不执行新增买入。\n\n"
                f"📊 {run_date}盘前（数据截至{data_date}收盘）：{l1_text}\n\n"
                f"📈 市场结构：{l2_text}。\n轮动判定：{rotation_label}。\n\n"
                f"🔄 昨日信号回顾（{data_date}盘前信号→{data_date}收盘）：共{n_track}只，涨停{len(hits_zt)}只，正收益{len(hits_up)}只({up_rate:.0f}%)，下跌{len(hits_down)}只。\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
else:
    decision = (f"多头趋势→正常模式。{len(strong)}只候选可纳入虚拟持仓。\n\n"
                f"⚠️ 不构成投资建议。虚拟盘仅作研究观察。")

# ── 观察要点 ──
observations = [
    f"L1盘前(数据截至{data_date}收盘): {l1_text}",
    f"L2: {l2_text}",
    f"轮动判定: {rotation_label}",
    f"持仓{len(holdings)}只, 总资产¥{total_value:,.0f} ({pnl_pct:+.2f}%)",
]
if max_lb_name:
    observations.append(f"最高板{max_lb_name} {max_lb}连板")
if top_sectors:
    observations.append(f"涨停池主线：{' / '.join(f'{n}({c})' for n, c in top_sectors[:3])}")
if buy_ge3:
    observations.append(f"技术面达标{buy_ge3}只(强{len(strong)}/观{len(watch)}/风{len(risk)})")
if tracking:
    observations.append(f"昨日信号回顾: {len(hits_up)}/{n_track}正收益({up_rate:.0f}%), {len(hits_zt)}涨停, {len(hits_down)}下跌")
observations.append("⚠️ 不构成投资建议。虚拟盘仅作研究观察。")

# ═══════════ 输出结构 ═══════════
brief = {
    "date": run_date,
    "data_date": data_date,
    "facing": run_date,
    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "l1": {"regime": regime, "gate": gate, "detail": l1_text},
    "l2": {"zt_count": zt_count, "zb_count": zb_count, "zbr": zbr,
           "max_lb": max_lb, "max_lb_name": max_lb_name,
           "sectors": [{"name": n, "count": c} for n, c in top_sectors],
           "rotation": rotation_label, "detail": l2_text},
    "l3": {"buy_ge3": buy_ge3, "strong": len(strong), "watch": len(watch),
           "risk": len(risk)},
    "signals": signals,
    "yesterday_review": tracking,
    "holdings_snapshot": holdings_snapshot,
    "account_snapshot": {"cash": round(cash, 2), "total_value": round(total_value, 2),
                         "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2)},
    "decisions": decision,
    "observations": observations,
}

# ── dry-run：到此为止 ──
if args.dry_run:
    print("\n" + "=" * 60)
    print(f"🔍 DRY-RUN {run_date}（不写观察文件、不碰 portfolio.json）")
    print(f"🌅 A股SOP盘前观察简报 {run_date}（数据截至{data_date}收盘）")
    print(f"L1: {regime} → {gate}")
    print(f"   {l1_text}")
    print(f"L2: {l2_text}")
    print(f"   轮动: {rotation_label}")
    print(f"L3: 技术面达标{buy_ge3}只(强{len(strong)}/观{len(watch)}/风{len(risk)})")
    if signals:
        for s in signals[:8]:
            print(f"   {s['code']} {s['name']:8s} {s['lbc']}板 ¥{s['price']:.2f} {s['pct']:+.1f}% → {s['out']}")
    print(f"昨日信号回顾: {len(hits_up)}/{n_track}正收益({up_rate:.0f}%), {len(hits_zt)}涨停")
    print(f"虚拟账户: ¥{total_value:,.0f} ({pnl_pct:+.2f}%)  持仓{len(holdings)}只")
    print("=" * 60)
    sys.exit(0)

# ── 写观察文件（唯一写操作，不碰 portfolio.json） ──
CACHE_DIR.mkdir(parents=True, exist_ok=True)
with open(OBS_FILE, "w", encoding="utf-8") as f:
    json.dump(brief, f, ensure_ascii=False, indent=2)
print(f"\n✅ 观察文件已写入: {OBS_FILE}")

# ── 摘要输出（供 SKILL.md 读取） ──
print("\n" + "=" * 60)
print(f"🌅 A股SOP盘前观察简报 {run_date}（数据截至{data_date}收盘，面向{run_date}）")
print(f"L1: {regime} → {gate}")
print(f"   {l1_text}")
print(f"L2: {l2_text}")
print(f"   轮动: {rotation_label}")
print(f"L3: 技术面达标{buy_ge3}只(强{len(strong)}/观{len(watch)}/风{len(risk)})")
if signals:
    for s in signals[:8]:
        pe_str = f"PE{s['pe']:.0f}" if s.get('pe') and s['pe'] > 0 else "PE负"
        print(f"   {s['code']} {s['name']:8s} {s['lbc']}板 ¥{s['price']:.2f} {s['pct']:+.1f}% {pe_str} → {s['out']}")
if tracking:
    print(f"昨日信号回顾: {len(hits_up)}/{n_track}正收益({up_rate:.0f}%), {len(hits_zt)}涨停, {len(hits_down)}下跌")
print(f"持仓: {len(holdings)}只, 浮盈{sum(h.get('pnl',0) for h in holdings_snapshot):+,.0f}元")
print(f"虚拟账户: ¥{total_value:,.0f} ({pnl:+,.0f} / {pnl_pct:+.2f}%)")
print("\n⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
print("=" * 60)
