# -*- coding: utf-8 -*-
"""
shadow_report.py — 影子模式周报
============================================================
Claude哥要求：每周看一次累计对比，别等 6 周开盲盒——
中途发现新规则在涨潮日反而不如旧的，能早发现。

配对口径：同一天内 新规则 top3 vs 现行规则 top3（apples-to-apples）
out-of-sample 分界线：shadow_scoring.enabled_date（首次运行日封印）

用法:
    python shadow_report.py             # 全部周
    python shadow_report.py --oos-only  # 只看 out-of-sample（启用后）数据
"""
import argparse
import json
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


def week_key(date_str):
    """日期 → ISO 周标签（如 2026-W37）"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def agg(items, key):
    vals = [x[key] for x in items if x.get(key) is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "avg": sum(vals) / len(vals),
        "win": sum(1 for v in vals if v > 0) / len(vals) * 100,
    }


# 按周分组
weeks = defaultdict(lambda: {"new": [], "cur": []})
for p in picks_new:
    weeks[week_key(p["date"])]["new"].append(p)
for p in picks_cur:
    weeks[week_key(p["date"])]["cur"].append(p)

print("=" * 78)
print(f"📊 影子模式周报（配对口径: 同日各取 top3）")
print(f"   影子启用日: {enabled or '未知'}  |  out-of-sample = 启用日之后的信号")
if args.oos_only:
    print(f"   [模式] 仅 out-of-sample 数据")
print("=" * 78)
print(f"{'周次':<10}{'期间':<13}{'新规则 D1':<22}{'现行 D1':<22}{'差':<11}{'样本'}")
print("-" * 78)

for wk in sorted(weeks):
    w = weeks[wk]
    dates = sorted(set(p["date"] for p in w["new"] + w["cur"]))
    span = f"{dates[0][5:]}/{dates[-1][5:]}" if dates else "-"
    nn = agg(w["new"], "d1")
    nc = agg(w["cur"], "d1")
    # out-of-sample 需该周全部信号日期都晚于启用日
    oos = bool(enabled) and dates and all(d > enabled for d in dates)
    tag = "OOS" if oos else "in-sample"
    if nn and nc:
        diff = nn["avg"] - nc["avg"]
        nn_s = f"{nn['avg']:+.2f}% {nn['win']:.0f}%(n={nn['n']})"
        nc_s = f"{nc['avg']:+.2f}% {nc['win']:.0f}%(n={nc['n']})"
        print(f"{wk:<10}{span:<13}{nn_s:<22}{nc_s:<22}{diff:+.2f}pp".ljust(67) + f"  {tag}")
    else:
        print(f"{wk:<10}{span:<13}(样本不足)".ljust(67) + f"  {tag}")

print("-" * 78)
# 累计对比
for label, np_, cp_ in [("全部", picks_new, picks_cur),
                        ("OOS ", [p for p in picks_new if enabled and p["date"] > enabled],
                         [p for p in picks_cur if enabled and p["date"] > enabled])]:
    if not np_:
        if label == "OOS ":
            print(f"  [{label}] 暂无 out-of-sample 数据（启用日之后尚无信号）")
        continue
    print(f"  [{label}]")
    for o in ["d1", "d3", "d5"]:
        nn = agg(np_, o)
        nc = agg(cp_, o)
        if nn and nc:
            print(f"    {o.upper()}: 新 {nn['avg']:+.2f}% 胜{nn['win']:.0f}% (n={nn['n']})  |  "
                  f"现行 {nc['avg']:+.2f}% 胜{nc['win']:.0f}% (n={nc['n']})  |  "
                  f"差 {nn['avg'] - nc['avg']:+.2f}pp")
print("=" * 78)
print(f"⚠️ {shadow.get('warning', '')}")
