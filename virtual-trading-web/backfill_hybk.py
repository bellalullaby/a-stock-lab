# -*- coding: utf-8 -*-
"""
backfill_hybk.py — 持仓行业字段回填（B 修复项）
============================================================
背景：hybk 缺失时板块止损（第五层之一）直接跳过——"五层变四层"，
深中华A 的板块止损因此站岗。本脚本双保险回填：
  1) 买入日 l2_zt_pool.json 查 hybk（买入时的行业，最准确）
  2) 最新缓存 l3_stocks.json 兜底
新买入的强制带行业在 closing_briefing.py 已保证；缺失时打印报警。

用法：
    python backfill_hybk.py            # 回填并写回
    python backfill_hybk.py --dry-run  # 只预览
"""
import json
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO, CACHE_DIR

import argparse
parser = argparse.ArgumentParser(description="持仓 hybk 行业回填")
parser.add_argument("--dry-run", action="store_true", help="只预览不写")
args = parser.parse_args()


def norm(code: str) -> str:
    c = str(code or "")
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            return c[2:]
    return c


with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

# 最新缓存日期（有 l3_stocks.json 的最新一天）
cache_dates = sorted(
    [d.name for d in CACHE_DIR.iterdir()
     if d.is_dir() and len(d.name) == 10 and (d / "l3_stocks.json").exists()]
)
latest_l3 = {}
if cache_dates:
    with open(CACHE_DIR / cache_dates[-1] / "l3_stocks.json", encoding="utf-8") as f:
        for s in json.load(f).get("stocks", []):
            latest_l3[norm(s.get("tx_code", s.get("code", "")))] = s.get("hybk", "")

filled = []
for h in pf.get("holdings", []):
    if h.get("hybk"):
        continue
    raw = norm(h.get("code", ""))
    buy_date = h.get("buy_date", "")
    # 保险1: 买入日涨停池
    hybk = ""
    zt_path = CACHE_DIR / buy_date / "l2_zt_pool.json"
    if zt_path.exists():
        zt = json.loads(zt_path.read_text(encoding="utf-8"))
        for s in zt.get("stocks", []):
            if norm(s.get("code", "")) == raw:
                hybk = s.get("hybk", "")
                break
    source = f"买入日({buy_date})涨停池" if hybk else ""
    # 保险2: 最新 l3
    if not hybk and latest_l3.get(raw):
        hybk = latest_l3[raw]
        source = f"最新缓存({cache_dates[-1]})l3"
    if hybk:
        filled.append({"code": h["code"], "name": h["name"], "hybk": hybk, "source": source})
        print(f"  ✅ {h['name']}: hybk='{hybk}' ({source})")
    else:
        print(f"  ⚠️ {h['name']}: 双保险都没查到行业，请人工确认")

if not filled:
    print("✅ 所有持仓行业齐全，无需回填")
    sys.exit(0)

if args.dry_run:
    print("\n🔍 DRY-RUN 不写文件")
    sys.exit(0)

for h in pf["holdings"]:
    for f2 in filled:
        if h["code"] == f2["code"]:
            h["hybk"] = f2["hybk"]

with open(PORTFOLIO, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

print(f"\n✅ 已回填 {len(filled)} 只，板块止损恢复五层完整")
