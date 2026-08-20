# -*- coding: utf-8 -*-
"""
backfill_snapshots.py — 回填 daily_snapshots 历史快照
============================================================
背景：closing_briefing.py 之前只更新 account 字段，从不写 daily_snapshots，
导致 Web 前端折线图没有小克的历史曲线。

本脚本从 daily_log 的收盘简报 portfolio_snapshot 提取历史快照，
写入 portfolio.json 的 daily_snapshots 数组（按日期排序去重）。

用法：
    python backfill_snapshots.py            # 回填（幂等：已有日期跳过）
    python backfill_snapshots.py --dry-run  # 预览不写

注意：closing_briefing.py 已改为每天收盘时自动写快照，本脚本只在
历史数据缺失时跑一次。
"""
import json
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO

import argparse
parser = argparse.ArgumentParser(description="回填 daily_snapshots 历史快照")
parser.add_argument("--dry-run", action="store_true", help="只预览不写")
args = parser.parse_args()

with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

existing_dates = {s.get("date") for s in pf.get("daily_snapshots", [])}

# 从 daily_log 收盘简报提取 portfolio_snapshot
new_snaps = []
for e in pf["daily_log"]:
    if not str(e.get("session", "")).startswith("收盘简报"):
        continue
    date_str = e.get("date", "")
    ps = e.get("portfolio_snapshot")
    if not ps or date_str in existing_dates:
        continue
    new_snaps.append({
        "date": date_str,
        "total_value": ps.get("total_value", 0),
        "cash": ps.get("cash", 0),
        "pnl": (ps.get("total_value", 0) or 0) - pf["account"].get("initial_capital", 1000000),
        "pnl_pct": ps.get("pnl_pct", 0),
        "holdings": ps.get("holdings", 0),
    })

new_snaps.sort(key=lambda s: s["date"])

if not new_snaps:
    print("✅ 无需回填（daily_snapshots 已是最新）")
    sys.exit(0)

print(f"待回填 {len(new_snaps)} 条:")
for s in new_snaps:
    print(f"  {s['date']}: total={s['total_value']:,.0f} ({s['pnl_pct']:+.2f}%) 持仓{s['holdings']}只")

if args.dry_run:
    print("\n🔍 DRY-RUN 不写文件")
    sys.exit(0)

pf["daily_snapshots"] = pf.get("daily_snapshots", []) + new_snaps
pf["daily_snapshots"].sort(key=lambda s: s["date"])

with open(PORTFOLIO, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

print(f"\n✅ 已回填 {len(new_snaps)} 条，daily_snapshots 现有 {len(pf['daily_snapshots'])} 条")
