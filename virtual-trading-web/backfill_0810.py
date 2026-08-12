# -*- coding: utf-8 -*-
"""
backfill_0810.py — 补跑 2026-08-10（周一）收盘简报
============================================================
背景：portfolio.json 从 08-07 之后就没更新，08-10 收盘简报缺失
（缓存数据在 19:49 已采集）。导致 08-11 收盘任务在"跳过两天"的
状态下直接开跑，虚拟账户直接跳到买入 10 只。

本脚本只做一件事：把 08-10 的 daily_log 条目补进去，恢复日期连续性。
安全设计：
  - 只插入 daily_log（按日期排序，08-07 和 08-11 之间）
  - 绝不修改 account / holdings（保护 08-11 已开仓状态）
  - 08-10 是震荡市 + 0 强候选，本来也不会有虚拟买入
"""
import json
import sys
import io
from datetime import datetime
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO, cache_dir

DATE = "2026-08-10"
BASE = cache_dir(DATE)


def load(name):
    with open(BASE / name, encoding="utf-8") as f:
        return json.load(f)


# ── 1. 读缓存 ──────────────────────────────────────────
l1 = load("l1_index.json")
l2_zt = load("l2_zt_pool.json")
l2_zb = load("l2_zb_pool.json")
rot = load("l2_rotation.json")
l3 = load("l3_stocks.json")

with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

# ── 2. L1 门控 ─────────────────────────────────────────
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
        f"MA20={d['ma20']:.0f}({d['ma20_direction']}) MA60={d['ma60']:.0f}"
    )
if bull == 3:
    gate = "正常模式"
elif risk >= 2:
    gate = "暂停交易"
else:
    gate = "降低权重"

l1_detail = " ".join(l1_detail_lines) + \
    f"。均涨幅{l1['avg_chg_pct']:+.1f}% 量比{l1['avg_vol_ratio']:.2f}。{l1['volume_analysis']}"

# ── 3. L2 市场结构 ─────────────────────────────────────
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

# ── 4. L3 信号分布 ─────────────────────────────────────
l3_stocks = l3["stocks"]
strong = sum(1 for s in l3_stocks if s.get("label") == "强候选")
observe = sum(1 for s in l3_stocks if s.get("label") == "观察")
riskc = sum(1 for s in l3_stocks if s.get("label") == "风控")
weak = sum(1 for s in l3_stocks if s.get("label") == "弱")

# ── 5. 昨日回顾（08-07 信号在 08-10 的表现）────────────
# 08-07 缓存里有 l3_stocks.json，但没有今日行情快照文件，
# 这里留空并在 decisions 里注明（保持结构完整）
yesterday_review = []

# ── 6. 构造 daily_log 条目（不交易、不改账户）─────────
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
    "date": DATE,
    "session": "收盘简报(补跑 08-11)",
    "l1_state": state,
    "l1_gate": gate,
    "l1_bull": bull,
    "l1_risk": risk,
    "l1_changed": False,
    "l1_detail": l1_detail,
    "l1_indexes": idx,
    "l2_summary": f"{zt_count}家涨停, 炸板率{zbr}%, 最高{max_lb}板, 主线: {main_line}",
    "l2_zt_count": zt_count,
    "l2_zb_count": zb_count,
    "l2_zbr": zbr,
    "l2_max_lb": max_lb,
    "l2_sectors": [{"name": k, "count": v} for k, v in main_sectors],
    "l2_rotation": rotation_label,
    "l3_summary": f"强候选:{strong}, 观察:{observe}, 风控:{riskc}, 弱:{weak} — {rotation_label}",
    "signals": signals_legacy,
    "yesterday_review": yesterday_review,
    "trades": {"bought": [], "sold": [], "note": "补跑：震荡市无强候选，无虚拟交易"},
    "holdings_snapshot": pf.get("holdings", []),
    "decisions": (
        f"{state}→{gate}（补跑）。震荡市无强候选，不执行虚拟买卖。"
        f"涨停{zt_count}家，炸板率{zbr}%，最高{max_lb}板，主线: {main_line}。"
        f"L1: {l1_detail}"
    ),
    "observations": [
        f"L1收盘: {l1_detail}",
        f"最高板{max_lb}板",
        f"主线分布: {main_line}",
        f"涨停{zt_count}家 / 炸板率{zbr}%",
        f"轮动判定: {rotation_label}",
    ],
    "portfolio_snapshot": {
        "cash": pf["account"].get("cash", 0),
        "total_value": pf["account"].get("total_value", 0),
        "pnl_pct": pf["account"].get("pnl_pct", 0),
        "holdings": len(pf.get("holdings", [])),
    },
    "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "backfilled": True,
}

# ── 7. 插入 daily_log（去重 + 按日期排序）──────────────
# 先去掉可能存在的同日期收盘条目，再追加，最后排序
pf["daily_log"] = [
    e for e in pf["daily_log"]
    if not (e.get("date") == DATE and str(e.get("session", "")).startswith("收盘简报"))
]
pf["daily_log"].append(daily_log_entry)
pf["daily_log"].sort(key=lambda e: e.get("date", ""))

# 注意：不修改 pf["account"] 和 pf["holdings"]！

with open(PORTFOLIO, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

# ── 8. 输出验证 ────────────────────────────────────────
print(f"✅ 08-10 收盘简报已补写（不涉及账户/持仓）")
print(f"  L1: {state} → {gate}")
print(f"  L2: {zt_count}家涨停 炸板率{zbr}% 最高{max_lb}板 主线: {main_line}")
print(f"  L3: 强候选{strong} 观察{observe} 风控{riskc} 弱{weak}")
print(f"  daily_log 现有 {len(pf['daily_log'])} 条")
print("  daily_log 日期顺序:")
for e in pf["daily_log"][-6:]:
    print(f"    {e.get('date')}  {str(e.get('session', ''))[:20]}")
