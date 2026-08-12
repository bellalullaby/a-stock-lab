"""
generate_briefing.py — 生成盘前简报并更新 portfolio.json
"""
import json
import os
from datetime import date

today = date.today().strftime("%Y-%m-%d")
print(f"=== A股SOP盘前简报 {today} ===")
print(f"  星期{['一','二','三','四','五','六','日'][date.today().weekday()]}")

BASE = r"D:\MyClaude\a-stock-lab\virtual-trading-web\data\cache"
PF = r"D:\MyClaude\a-stock-lab\virtual-portfolio\portfolio.json"

# Load cached data
with open(os.path.join(BASE, "2026-07-30", "l1_index.json"), encoding="utf-8") as f:
    l1 = json.load(f)
with open(os.path.join(BASE, "2026-07-30", "l2_zt_pool.json"), encoding="utf-8") as f:
    l2 = json.load(f)
with open(os.path.join(BASE, "2026-07-30", "l2_zb_pool.json"), encoding="utf-8") as f:
    l2_zb = json.load(f)
with open(os.path.join(BASE, "2026-07-30", "l3_stocks.json"), encoding="utf-8") as f:
    l3 = json.load(f)
with open(os.path.join(BASE, "2026-07-30", "l2_boards.json"), encoding="utf-8") as f:
    l2b = json.load(f)
with open(os.path.join(BASE, "2026-07-30", "l2_rotation.json"), encoding="utf-8") as f:
    rot = json.load(f)

# Load portfolio
with open(PF, "r", encoding="utf-8") as f:
    pf = json.load(f)

# === Build L1 summary ===
idx = l1["indices"]
l1_text = (
    f"上证收{idx['sh000001']['close']:.0f}({idx['sh000001']['chg_pct']:+.1f}%) "
    f"MA20={idx['sh000001']['ma20']:.0f}(↓) MA60={idx['sh000001']['ma60']:.0f}(↓); "
    f"深证收{idx['sz399001']['close']:.0f}({idx['sz399001']['chg_pct']:+.1f}%) "
    f"MA20={idx['sz399001']['ma20']:.0f}(↓) MA60={idx['sz399001']['ma60']:.0f}(↓); "
    f"创业板收{idx['sz399006']['close']:.0f}({idx['sz399006']['chg_pct']:+.1f}%) "
    f"MA20={idx['sz399006']['ma20']:.0f}(↓) MA60={idx['sz399006']['ma60']:.0f}(↓)"
)

# === L2 summary ===
zt_count = l2["total"]
zb_count = l2_zb["total"]
zbr = round(zb_count/(zt_count+zb_count)*100, 1) if zt_count+zb_count>0 else 0

# Find max_lb
max_lb = max(s.get("lb", 0) for s in l2["stocks"]) if l2["stocks"] else 0

# Board summary
boards_text = ", ".join(f"{b['name']}({b['zt_count']})" for b in l2b["boards"][:5])

l2_text = f"{zt_count}家涨停, 炸板率{zbr}%, 最高{max_lb}板, 主线: {boards_text}"

# === L3 summary ===
summary = l3["summary"]
l3_text = f"强候选:0, 观察:12, 风控:17, 弱:1 — 系统性风险下buy≥3全部强制风控"

# === Track yesterday's signals (7/29 收盘简报) ===
print("\n📊 昨日信号(7/29收盘) → 今日(7/30)表现:")

# Get 7/29 closing signals
yesterday_signals = None
for entry in pf["daily_log"]:
    if entry["date"] == "2026-07-29" and entry.get("session") == "收盘简报":
        yesterday_signals = entry.get("signals", [])
        break

# Build 7/30 zt pool lookup
zt_730 = {}
for s in l2["stocks"]:
    zt_730[s["code"]] = s

tracking = []
if yesterday_signals:
    for ys in yesterday_signals:
        code = ys["code"]
        name = ys["name"]
        y_price = ys["price"]
        y_lb = ys["lbc"]

        if code in zt_730:
            t = zt_730[code]
            t_price = t["price"]
            t_pct = t.get("chg_pct", 0)
            t_lb = t.get("lb", 0)
            chg = round((t_price - y_price) / y_price * 100, 1)
            tracking.append({
                "code": code, "name": name,
                "y_price": y_price, "y_lb": y_lb,
                "t_price": t_price, "t_lb": t_lb,
                "chg_pct": chg,
                "status": "连板继续" if t_lb > y_lb else "封板",
                "industry": ys.get("industry", "")
            })
            extra = "🔥连板!" if t_lb > y_lb else ""
            print(f"  ✅ {name}: {y_price}→{t_price} ({chg:+.1f}%) {y_lb}板→{t_lb}板 {extra}")
        else:
            tracking.append({
                "code": code, "name": name,
                "y_price": y_price, "y_lb": y_lb,
                "t_price": "未封板", "t_lb": 0,
                "chg_pct": "?",
                "status": "断板",
                "industry": ys.get("industry", "")
            })
            print(f"  ❌ {name}: {y_price}→断板 ({y_lb}板止步)")
else:
    print("  (无7/29收盘信号)")

# Count hits
hits_continue = [t for t in tracking if t["status"] in ("连板继续", "封板")]
hits_broken = [t for t in tracking if t["status"] == "断板"]
total_tracked = len(tracking)
if total_tracked > 0:
    continue_rate = len(hits_continue) / total_tracked * 100
    print(f"\n  连板晋级: {len(hits_continue)}/{total_tracked} = {continue_rate:.0f}%")
else:
    continue_rate = 0
    print("\n  无昨日信号可追踪")

# === Build decisions ===
def fmt_hits_continue(hl):
    if not hl:
        return "无"
    parts = []
    for t in hl:
        parts.append("%s(%d→%d板+%.1f%%)" % (t["name"], t["y_lb"], t["t_lb"], t["chg_pct"]))
    return ", ".join(parts)

def fmt_hits_broken(hl):
    if not hl:
        return "无"
    parts = []
    for t in hl:
        parts.append("%s(%d板止步)" % (t["name"], t["y_lb"]))
    return ", ".join(parts)

aili_note = ""
if any(t["name"] == "爱丽家居" for t in hits_continue):
    aili_note = "\n• 妖股爱丽家居8连板(16.94→18.63→20.49), 但PE=-250, 纯粹筹码博弈"

decision = "系统性风险 → 暂停交易 — 不执行任何虚拟买卖\n\n" \
    "📊 本轮下行: 三大指数连续在MA20/MA60下方运行。7/30创业板单日暴跌-4.0%，深证-2.7%，是近期最大单日跌幅。\n\n" \
    "🔑 7/29 收盘信号 → 7/30 表现:\n" \
    "• %d只连板晋级: %s\n" \
    "• %d只断板: %s\n" \
    "• 晋级率: %d/%d=%d%%%s\n\n" \
    "📉 市场广度恶化:\n" \
    "• 涨停数: 116(7/23)→81(7/29)→52(7/30) — 连续萎缩\n" \
    "• 炸板率: 17.7%(7/23)→14.7%(7/29)→26.8%(7/30) — 回升\n" \
    "• 创业板单日-4.0% — 成长股杀跌最狠\n\n" \
    "⚡ 板块轮动:\n" \
    "• 电网设备连续2天入榜(4→2天连续性)，是唯一的跨日主线\n" \
    "• 汽车零部今日最多涨停(6家)但昨日不在TOP5——新一日游风险高\n" \
    "• 化学制品(7/29 TOP1,7家)→今日仅2家涨停——典型一日游确认\n\n" \
    "📈 门控降级观测(3条件):\n" \
    "① 至少2个主板指数站上MA20 — ❌ 上证距MA20约100点(-2.6%%), 深证距约1138点(-7.9%%)\n" \
    "② 涨停>80家且炸板率<20%% — ❌ 52家/炸26.8%%\n" \
    "③ 成长板块连续2日不走一日游 — ❌ 化学制品7/29 TOP1→7/30仅2家\n" \
    "→ 0/3, 维持「暂停交易」\n\n" \
    "⚠️ 后市观察:\n" \
    "• 本轮下行仍在深化，创业板4%%单日暴跌不是见底信号\n" \
    "• 电网设备如果有第3天涨停集中(>5家)→可能是底部最先突围的板块\n" \
    "• 周末效应: 周五(7/31)通常缩量，不追涨\n" \
    "• 爱丽家居8连板 vs 大盘暴跌——妖股独立性越来越强，但PE=-250纯筹码游戏"

decision = decision % (
    len(hits_continue), fmt_hits_continue(hits_continue),
    len(hits_broken), fmt_hits_broken(hits_broken),
    len(hits_continue), total_tracked, int(continue_rate), aili_note
)

# === Updated portfolio entry ===
entry = {
    "date": "2026-07-30",
    "session": "盘前简报(08:30运行, 数据截至2026-07-30收盘, 面向2026-07-31交易)",
    "l1_state": "系统性风险",
    "l1_gate": "暂停交易",
    "l1_bull": 0,
    "l1_risk": 3,
    "l1_changed": False,
    "l1_detail": l1_text,
    "l1_indexes": {
        "上证": {
            "close": idx['sh000001']['close'],
            "chg_pct": idx['sh000001']['chg_pct'],
            "ma20": idx['sh000001']['ma20'],
            "ma60": idx['sh000001']['ma60'],
            "ma20_up": False, "ma60_up": False,
            "above_ma20": False, "above_ma60": False
        },
        "深证": {
            "close": idx['sz399001']['close'],
            "chg_pct": idx['sz399001']['chg_pct'],
            "ma20": idx['sz399001']['ma20'],
            "ma60": idx['sz399001']['ma60'],
            "ma20_up": False, "ma60_up": False,
            "above_ma20": False, "above_ma60": False
        },
        "创业板": {
            "close": idx['sz399006']['close'],
            "chg_pct": idx['sz399006']['chg_pct'],
            "ma20": idx['sz399006']['ma20'],
            "ma60": idx['sz399006']['ma60'],
            "ma20_up": False, "ma60_up": False,
            "above_ma20": False, "above_ma60": False
        }
    },
    "l2_summary": l2_text,
    "l2_zt_count": zt_count,
    "l2_zb_count": zb_count,
    "l2_zbr": zbr,
    "l2_max_lb": max_lb,
    "l2_sectors": [{"name": b["name"], "count": b["zt_count"]} for b in l2b["boards"][:5]],
    "l2_rotation": rot.get("rotation_label", ""),
    "l3_summary": l3_text,
    "signals": [],
    "yesterday_tracking": tracking,
    "trades": [],
    "decisions": decision
}

# Build signals from L3 (only meaningful ones)
for s in l3["stocks"]:
    entry["signals"].append({
        "code": s["tx_code"],
        "name": s["name"],
        "lbc": s["lb"],
        "price": s["price"],
        "pct": s["chg_pct"],
        "pe": s["pe"],
        "buy_score": s["buy_score"],
        "label": s["label"],
        "industry": "",
        "hits": s.get("rules", []),
        "turnover": s.get("turnover", 0)
    })

# Update portfolio
# Remove the placeholder last entry if it's the 7/30 stub
if pf["daily_log"][-1]["date"] == "2026-07-30" and len(pf["daily_log"][-1].get("signals",[]))>0:
    # Check if it's the auto-generated entry from yesterday's collector
    last = pf["daily_log"][-1]
    if "decisions" in last and "halt" in last.get("decisions",""):
        pf["daily_log"][-1] = entry  # replace
    else:
        pf["daily_log"].append(entry)
else:
    # Check if already has today's entry
    has_today = False
    for e in pf["daily_log"]:
        if e["date"] == "2026-07-30" and "盘前" in e.get("session", ""):
            has_today = True
            break
    if not has_today:
        pf["daily_log"].append(entry)

pf["account"]["last_updated"] = f"{today} 盘前简报"
pf["account"]["total_value"] = pf["account"]["cash"]  # No holdings

with open(PF, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

print(f"\n✅ Portfolio updated: {PF}")
print(f"   Total entries: {len(pf['daily_log'])}")
print(f"\n--- 简报正文 ---")
print(decision)
