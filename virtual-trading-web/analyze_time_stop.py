# -*- coding: utf-8 -*-
"""
analyze_time_stop.py — 时间止损阈值复核（P0，纯回测零新数据）
============================================================
口径声明:
  - 买入口径: 信号日收盘价买入（与 signal_tracker d*_return 同口径）
  - 卖出口径: 全部收盘价（B 策略"择机"以 D+2 收盘近似，盘中滑点未建模）
  - 留存假设: 现行策略 D+5 达标(≥5%)留过观察期的，按 D+10 收盘卖出
    （现行规则只定义"踢"，未定义"留多久"，此为补全假设，已在结论标注）
  - 统计口径: 中位数优先 + 逐日胜率（v2 老规矩），均值仅供参考
样本边界:
  - 数据源: signal_tracker.json 全部可回测信号（剔风控/弱/门控禁止）
  - 同票去重（只计首次入选）
  - 样本期 45 个交易日，全为震荡市/系统性风险——未经历多头市，外推谨慎
  - 涨停买入滑点未建模（实盘 D+1 买入可能买不到信号价）

三策略:
  A) D+1 收盘走          —— 收益 = d1
  B) D+2 收盘走          —— 收益 = d2
  C) 现行: D+3 涨幅<2% 踢（收益=d3）；否则 D+5 <5% 踢（收益=d5）；
     否则留过观察期按 D+10 卖（收益=d10）

附带验证: D5 分布 vs ≥5% 阈值的错配量化（P(d5≥5%)、中位数对比）
"""
import json
import statistics
import sys
import io
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from signal_tracker import fetch_klines, find_forward_prices

TRACKER = BASE_DIR / "data" / "signal_tracker.json"

with open(TRACKER, encoding="utf-8") as f:
    data = json.load(f)

all_signals = data.get("signals", [])

# ── 过滤：可买池 + 同票去重 ──
def buyable(s):
    out = s.get("out", "")
    return "风控" not in out and out != "弱" and "门控禁止" not in out

pool = [s for s in all_signals if buyable(s)]
seen = set()
signals = []
for s in sorted(pool, key=lambda x: x["signal_date"]):
    if s["code"] in seen:
        continue
    seen.add(s["code"])
    signals.append(s)
print(f"样本: 可买池 {len(pool)} 条 → 去重后 {len(signals)} 条（{len(set(s['code'] for s in signals))} 只股票）")

# ── 拉 K 线，算 D1/D2/D3/D5/D10 ──
unique_codes = sorted(set(s["code"] for s in signals))
print(f"拉取 {len(unique_codes)} 只股票 K 线（robust_cn_get 双路径）...")
klines_cache = {}
fail = 0
for i, code in enumerate(unique_codes):
    if i % 20 == 0:
        print(f"  进度: {i}/{len(unique_codes)}")
    kl = fetch_klines(code)
    if not kl:
        fail += 1
    klines_cache[code] = kl

ok = len(unique_codes) - fail
print(f"K 线完成: 成功 {ok}/{len(unique_codes)}，失败 {fail}")
if unique_codes and fail == len(unique_codes):
    print("⚠️ K 线全失败（网络异常），拒绝输出结论")
    sys.exit(1)

# 每条信号算 d1/d2/d3/d5/d10
rows = []
for s in signals:
    kl = klines_cache.get(s["code"], [])
    if not kl:
        continue
    fwd = find_forward_prices(kl, s["signal_date"], offsets=[1, 2, 3, 5, 10])
    row = {**s}
    for o in [1, 2, 3, 5, 10]:
        info = fwd.get(o)
        row[f"d{o}_return"] = round((info["close"] / s["price"] - 1) * 100, 2) \
            if (info and s.get("price")) else None
    rows.append(row)

usable = [r for r in rows if r.get("d1_return") is not None and r.get("d2_return") is not None]
print()
print("样本漏斗（口径声明）:")
print(f"  全量信号            : {len(all_signals)}")
print(f"  → 可买池(剔风控/弱/门控禁止): {len(pool)}")
print(f"  → 同票去重(只计首次)   : {len(signals)}")
print(f"  → K线拉取成功        : {len([r for r in rows])}（失败 {fail} 只股票的信号被剔除）")
print(f"  → d1+d2 齐全         : {len(usable)}  ← 回测样本")


# ── 三策略收益 ──
def strategy_a(r):
    return r["d1_return"]


def strategy_b(r):
    return r["d2_return"]


def strategy_c(r):
    """现行: D+3<2% 踢；否则 D+5<5% 踢；否则留过观察期 D+10 卖"""
    d3, d5, d10 = r.get("d3_return"), r.get("d5_return"), r.get("d10_return")
    if d3 is not None and d3 < 2:
        return d3
    if d5 is not None and d5 < 5:
        return d5
    return d10  # 留过观察期按 D+10 卖（补全假设）


STRATS = [("A) D+1收盘走", strategy_a), ("B) D+2收盘走", strategy_b), ("C) 现行D+3/5", strategy_c)]


def desc(vals):
    if not vals:
        return "无样本"
    return (f"中位 {statistics.median(vals):+.2f}%  均 {sum(vals) / len(vals):+.2f}%  "
            f"胜 {sum(1 for v in vals if v > 0) / len(vals) * 100:.0f}%  (n={len(vals)})")


# ── 总体对比 ──
print()
print("═" * 66)
print("【总体对比】（全部可买信号，中位数优先）")
print("═" * 66)
for label, fn in STRATS:
    vals = [fn(r) for r in usable if fn(r) is not None]
    print(f"  {label:<16} {desc(vals)}")

# 按日配对：A vs C、B vs C
print()
print("  按日配对（同日均值差，消市场 beta）:")
by_date = defaultdict(list)
for r in usable:
    by_date[r["signal_date"]].append(r)
for a_label, a_fn, b_label, b_fn in [("A", strategy_a, "C", strategy_c),
                                     ("B", strategy_b, "C", strategy_c)]:
    diffs = []
    for d, rs in by_date.items():
        av = [a_fn(r) for r in rs if a_fn(r) is not None]
        bv = [b_fn(r) for r in rs if b_fn(r) is not None]
        if av and bv:
            diffs.append(sum(av) / len(av) - sum(bv) / len(bv))
    if diffs:
        wins = sum(1 for x in diffs if x > 0.005)
        print(f"    {a_label} vs {b_label}: {len(diffs)} 天, {a_label}胜{wins}天({wins / len(diffs) * 100:.0f}%)"
              f"  日均差{sum(diffs) / len(diffs):+.2f}pp  中位差{statistics.median(diffs):+.2f}pp")

# ── 分组：现行持有超观察期 vs 被踢 ──
print()
print("═" * 66)
print("【错配量化】D5 分布 vs ≥5% 阈值")
print("═" * 66)
d5_vals = [r.get("d5_return") for r in usable if r.get("d5_return") is not None]
if d5_vals:
    keep = sum(1 for v in d5_vals if v >= 5)
    print(f"  D5 中位数: {statistics.median(d5_vals):+.2f}%  |  阈值要求: ≥5%")
    print(f"  P(D5≥5%) = {keep / len(d5_vals) * 100:.0f}% —— 只有这么多比例能留在观察期之后")
    print(f"  其余 {100 - keep / len(d5_vals) * 100:.0f}% 在 D+5 被踢（含 D+3 已踢的）")
kept = [r for r in usable if (r.get("d5_return") or -99) >= 5]
kicked = [r for r in usable if (r.get("d5_return") is not None) and (r.get("d5_return") or -99) < 5]
if kept:
    k10 = [r.get("d10_return") for r in kept if r.get("d10_return") is not None]
    if k10:
        print(f"  留组(n={len(kept)}) D+10 中位 {statistics.median(k10):+.2f}%  ← 阈值留下了好的")
if kicked:
    k5 = [r.get("d5_return") for r in kicked if r.get("d5_return") is not None]
    if k5:
        print(f"  踢组(n={len(kicked)}) D+5 中位 {statistics.median(k5):+.2f}%  ← 阈值踢掉的")

# 分组细分（强候选/观察）
print()
print("═" * 66)
print("【分组细分】")
print("═" * 66)
for g_label, g_filter in [("观察", lambda r: r.get("out") == "观察"),
                          ("强候选", lambda r: "强候选" in r.get("out", ""))]:
    grp = [r for r in usable if g_filter(r)]
    if not grp:
        continue
    print(f"  ── {g_label} (n={len(grp)}) ──")
    for label, fn in STRATS:
        vals = [fn(r) for r in grp if fn(r) is not None]
        print(f"    {label:<16} {desc(vals)}")

print()
print("═" * 66)
print("口径声明: 买入=信号日收盘；卖出=收盘价；现行留存假设=D+10 卖")
print("样本边界: 45 交易日全震荡/风险市；涨停滑点未建模；同票去重只计首次")
print("═" * 66)
