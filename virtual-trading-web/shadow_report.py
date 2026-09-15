# -*- coding: utf-8 -*-
"""
shadow_report.py — 影子模式周报（严谨统计口径 v2）
============================================================
Claude哥口径要求（2026-09-15，今后所有信号统计通用）：
  1. 按日配对——消同日市场 beta，同日对比而非跨日混算
  2. 同票去重——每票只计首次入选（重复入选=同一逻辑重复计数，
     哈药股份连续 4 天入选曾撑起 +14.93pp 的假象）
  3. 主看中位数 + 逐日胜率——不看均值（均值被厚尾支配）

用法:
    python shadow_report.py             # 全部周
    python shadow_report.py --oos-only  # 只看 out-of-sample
"""
import argparse
import json
import statistics
import sys
import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
TRACKER = BASE_DIR / "data" / "signal_tracker.json"

parser = argparse.ArgumentParser(description="影子模式周报")
parser.add_argument("--oos-only", action="store_true", help="只看启用后的 out-of-sample 数据")
args = parser.parse_args()

with open(TRACKER, encoding="utf-8") as f:
    data = json.load(f)

shadow = data.get("shadow_scoring")
if not shadow:
    print("⚠️ 无影子数据（signal_tracker 还没跑过或版本过旧）")
    sys.exit(1)

enabled = shadow.get("enabled_date", "")
picks_new = shadow.get("picks", [])
picks_cur = shadow.get("current_picks", [])
if args.oos_only:
    picks_new = [p for p in picks_new if p["date"] > enabled]
    picks_cur = [p for p in picks_cur if p["date"] > enabled]


def dedup_first(picks):
    """口径2: 同票只计首次入选（按日期排序取首现）"""
    seen = set()
    out = []
    for p in sorted(picks, key=lambda x: x["date"]):
        if p["code"] in seen:
            continue
        seen.add(p["code"])
        out.append(p)
    return out


def vals_of(picks, key):
    return [p[key] for p in picks if p.get(key) is not None]


def desc_stats(vals):
    """中位数优先的描述统计（口径3）"""
    if not vals:
        return None
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "mean": sum(vals) / len(vals),
        "win": sum(1 for v in vals if v > 0) / len(vals) * 100,
    }


def daily_means(picks, key):
    """口径1: 按日分组求均值 → {date: mean}"""
    by = defaultdict(list)
    for p in picks:
        v = p.get(key)
        if v is not None:
            by[p["date"]].append(v)
    return {d: sum(vs) / len(vs) for d, vs in by.items()}


def paired_compare(new_picks, cur_picks, key):
    """按日配对比较（同日新 vs 现行），返回逐日差与胜负统计"""
    nd = daily_means(new_picks, key)
    cd = daily_means(cur_picks, key)
    common = sorted(set(nd) & set(cd))
    diffs = [nd[d] - cd[d] for d in common]
    wins = sum(1 for x in diffs if x > 0.005)
    losses = sum(1 for x in diffs if x < -0.005)
    ties = len(diffs) - wins - losses
    return {
        "days": len(common), "diffs": diffs,
        "wins": wins, "losses": losses, "ties": ties,
        "mean": sum(diffs) / len(diffs) if diffs else 0,
        "median": statistics.median(diffs) if diffs else 0,
    }


# 去重
dn = dedup_first(picks_new)
dc = dedup_first(picks_cur)

print("=" * 78)
print("📊 影子模式周报（口径: 按日配对 + 同票去重 + 中位数优先）")
print(f"   影子启用日: {enabled or '未知'}  |  out-of-sample = 启用日之后的信号")
if args.oos_only:
    print("   [模式] 仅 out-of-sample 数据")
print("=" * 78)

# ── 三个周期的主口径统计 ──
for o in ["d1", "d3", "d5"]:
    sn, sc = desc_stats(vals_of(dn, o)), desc_stats(vals_of(dc, o))
    pc = paired_compare(dn, dc, o)
    if not sn or not sc:
        continue
    print(f"\n【{o.upper()}】")
    print(f"  股票层面(去重后每票一次):")
    print(f"    新规则  n={sn['n']:<3} 中位{sn['median']:+.2f}%  均{sn['mean']:+.2f}%  胜{sn['win']:.0f}%")
    print(f"    现行    n={sc['n']:<3} 中位{sc['median']:+.2f}%  均{sc['mean']:+.2f}%  胜{sc['win']:.0f}%")
    total = pc["days"]
    if total:
        print(f"  按日配对(可比 {total} 天):")
        print(f"    新规则胜 {pc['wins']} 天 / 现行胜 {pc['losses']} 天 / 平 {pc['ties']} 天"
              f"  ({pc['wins'] / total * 100:.0f}%)")
        print(f"    日均差 {pc['mean']:+.2f}pp  |  中位差 {pc['median']:+.2f}pp")

# ── 逐周（按日配对，中位数） ──
print()
print("-" * 78)
print("逐周（去重后按日配对）")
print(f"{'周次':<10}{'期间':<14}{'可比天':<8}{'新胜/现行胜/平':<18}{'中位差':<11}{'样本'}")
print("-" * 78)

weeks = defaultdict(lambda: {"new": [], "cur": []})
for p in dn:
    d = datetime.strptime(p["date"], "%Y-%m-%d").isocalendar()
    weeks[f"{d[0]}-W{d[1]:02d}"]["new"].append(p)
for p in dc:
    d = datetime.strptime(p["date"], "%Y-%m-%d").isocalendar()
    weeks[f"{d[0]}-W{d[1]:02d}"]["cur"].append(p)

for wk in sorted(weeks):
    w = weeks[wk]
    dates = sorted(set(p["date"] for p in w["new"] + w["cur"]))
    span = f"{dates[0][5:]}/{dates[-1][5:]}" if dates else "-"
    oos = bool(enabled) and dates and all(d > enabled for d in dates)
    tag = "OOS" if oos else "in-sample"
    pc = paired_compare(w["new"], w["cur"], "d1")
    if pc["days"]:
        wr = f"{pc['wins']}/{pc['losses']}/{pc['ties']}"
        print(f"{wk:<10}{span:<14}{pc['days']:<8}{wr:<18}{pc['median']:+.2f}pp".ljust(67) + f"  {tag}")
    else:
        print(f"{wk:<10}{span:<14}(不可配对)".ljust(67) + f"  {tag}")

print("=" * 78)
print(f"⚠️ {shadow.get('warning', '')}")
print("⚠️ 主看中位数与逐日胜率；均值受厚尾支配（去重后尤其），仅供参考")
