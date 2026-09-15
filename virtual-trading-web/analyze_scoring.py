# -*- coding: utf-8 -*-
"""
analyze_scoring.py — L3 打分体系独立复算（验证"倒挂"结论）
============================================================
Claude哥发现：观察(buy=2) 表现优于 强候选(buy≥3)，怀疑打分因子方向反了。
本脚本用 signal_tracker.json 同源数据独立复算，不盲信结论。

复算项：
  1. 分组对比：观察 vs 强候选 的 D1 均值/胜率/中位
  2. 按日配对：同一天内两者对比（消掉市场 beta）
  3. 换手率梯度 / 连板数梯度（单调性检查）
  4. 规则级对比：R2/R3/R4/R5 命中 vs 未命中的 D1 差
  5. 回测：低换手+高连板+PE>0 重排 vs 现行强候选 D1/D3/D5

用法: python analyze_scoring.py
"""
import json
import sys
import io
import statistics
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
TRACKER = BASE_DIR / "data" / "signal_tracker.json"

with open(TRACKER, encoding="utf-8") as f:
    data = json.load(f)

signals = [s for s in data.get("signals", []) if s.get("d1_return") is not None]
print(f"样本: {len(signals)} 条（有 D1 数据）/ 全量 {data.get('total_signals')}")
print()


def stat_group(items, field="d1_return"):
    """返回 (n, avg, win_rate, median)"""
    vals = [s[field] for s in items if s.get(field) is not None]
    if not vals:
        return 0, 0, 0, 0
    avg = sum(vals) / len(vals)
    win = sum(1 for v in vals if v > 0) / len(vals) * 100
    med = statistics.median(vals)
    return len(vals), avg, win, med


# ═══ 1. 分组对比：观察 vs 强候选 ═══
print("═" * 62)
print("【1】分组对比（Claude哥结论复算）")
print("═" * 62)
observe = [s for s in signals if s.get("out") == "观察" or s.get("buy_score") == 2]
strong = [s for s in signals if "强候选" in s.get("out", "") or s.get("buy_score", 0) >= 3]

for label, grp in [("观察(buy=2)", observe), ("强候选(buy≥3)", strong)]:
    n, avg, win, med = stat_group(grp)
    print(f"  {label:<16} n={n:<4} D1均{avg:+.2f}%  胜率{win:.0f}%  中位{med:+.2f}%")


# ═══ 2. 按日配对 ═══
print()
print("═" * 62)
print("【2】按日配对（同日观察 vs 强候选，消市场 beta）")
print("═" * 62)
by_date = defaultdict(lambda: {"obs": [], "str": []})
for s in signals:
    d = s["signal_date"]
    if s.get("out") == "观察" or s.get("buy_score") == 2:
        by_date[d]["obs"].append(s["d1_return"])
    elif "强候选" in s.get("out", "") or s.get("buy_score", 0) >= 3:
        by_date[d]["str"].append(s["d1_return"])

obs_wins = str_wins = 0
diffs = []
for d in sorted(by_date):
    o, st = by_date[d]["obs"], by_date[d]["str"]
    if not o or not st:
        continue
    om = sum(o) / len(o)
    sm = sum(st) / len(st)
    diffs.append(om - sm)
    if om > sm:
        obs_wins += 1
    else:
        str_wins += 1

if diffs:
    print(f"  可比交易日: {len(diffs)} 天")
    print(f"  观察胜: {obs_wins} 天 / 强候选胜: {str_wins} 天 ({obs_wins / len(diffs) * 100:.0f}%)")
    print(f"  日均差(观察-强候选): {sum(diffs) / len(diffs):+.2f}pp  中位差: {statistics.median(diffs):+.2f}pp")


# ═══ 3. 梯度检查 ═══
print()
print("═" * 62)
print("【3】底层变量梯度（单调性）")
print("═" * 62)

print("  换手率梯度:")
turn_bins = [(0, 5), (5, 10), (10, 20), (20, 999)]
for lo, hi in turn_bins:
    grp = [s for s in signals if lo <= (s.get("turnover") or 0) < hi]
    n, avg, win, med = stat_group(grp)
    if n:
        label = f"{lo}-{hi}%" if hi < 999 else f"≥{lo}%"
        print(f"    {label:<10} n={n:<4} D1均{avg:+.2f}%  胜率{win:.0f}%")

print()
print("  连板数梯度:")
lb_bins = [1, 2, 3, 4, 5, 6]
for lb in lb_bins:
    grp = [s for s in signals if (s.get("lbc") or 0) == lb]
    n, avg, win, med = stat_group(grp)
    if n:
        print(f"    {lb}板      n={n:<4} D1均{avg:+.2f}%  胜率{win:.0f}%")


# ═══ 4. 规则级对比 ═══
print()
print("═" * 62)
print("【4】规则级对比（命中 vs 未命中 D1 差）")
print("═" * 62)
for rule in ["R2", "R3", "R4", "R5"]:
    hit = [s for s in signals if any(r.startswith(rule) for r in (s.get("hits") or []))]
    miss = [s for s in signals if not any(r.startswith(rule) for r in (s.get("hits") or []))]
    nh, avgh, _, _ = stat_group(hit)
    nm, avgm, _, _ = stat_group(miss)
    if nh and nm:
        print(f"  {rule}: 命中 n={nh:<4} D1均{avgh:+.2f}%  |  未命中 n={nm:<4} D1均{avgm:+.2f}%  |  差{avgh - avgm:+.2f}pp")
    else:
        print(f"  {rule}: 命中 n={nh} 未命中 n={nm}（样本过偏，无区分度）")


# ═══ 5. 回测：新规则 vs 现行 ═══
print()
print("═" * 62)
print("【5】回测：低换手+高连板+PE>0 重排 vs 现行强候选")
print("═" * 62)

def new_rule_score(s):
    """新规则草案：低换手加分、高连板加分、PE负剔除"""
    turn = s.get("turnover") or 0
    lb = s.get("lbc") or 0
    pe = s.get("pe")
    score = 0
    if turn < 5:
        score += 2
    elif turn < 10:
        score += 1
    if lb >= 2:
        score += lb  # 连板高度线性加分
    if pe is not None and pe < 0:
        score -= 5  # PE 负剔除
    return score

# 每日取现行强候选 vs 新规则 top3
cur_returns = {"d1": [], "d3": [], "d5": []}
new_returns = {"d1": [], "d3": [], "d5": []}

by_date_full = defaultdict(list)
for s in data.get("signals", []):
    by_date_full[s["signal_date"]].append(s)

for d, day_sigs in sorted(by_date_full.items()):
    # 现行：强候选
    cur_picks = [s for s in day_sigs if "强候选" in s.get("out", "")]
    for s in cur_picks:
        for o in ["d1", "d3", "d5"]:
            v = s.get(f"{o}_return")
            if v is not None:
                cur_returns[o].append(v)
    # 新规则：按 new_rule_score 排序取 top3（同日不重复票）
    ranked = sorted(day_sigs, key=new_rule_score, reverse=True)[:3]
    for s in ranked:
        for o in ["d1", "d3", "d5"]:
            v = s.get(f"{o}_return")
            if v is not None:
                new_returns[o].append(v)

print(f"  {'周期':<6}{'现行强候选':<28}{'新规则top3':<28}")
for o in ["d1", "d3", "d5"]:
    c = cur_returns[o]
    n = new_returns[o]
    cs = f"n={len(c):<4} 均{sum(c) / len(c):+.2f}% 胜{sum(1 for x in c if x > 0) / len(c) * 100:.0f}%" if c else "无样本"
    ns = f"n={len(n):<4} 均{sum(n) / len(n):+.2f}% 胜{sum(1 for x in n if x > 0) / len(n) * 100:.0f}%" if n else "无样本"
    print(f"  {o.upper():<6}{cs:<28}{ns:<28}")
