# -*- coding: utf-8 -*-
"""
audit_legacy_holdings.py — 历史持仓旧口径审计
============================================================
背景：可成交过滤（从严 v1）上线前，旧脚本无过滤买入，可能把一字板/
硬板（zbc=0）买进了账户（虚高）。本脚本对当前全部持仓按各自买入日的
l2_zt_pool 快照跑新过滤器，不合格的持仓打标 legacy_invalid + reason。

策略（Claude哥拍板：标注，不回滚）：
  - 合格（买入日 zbc>=1 炸板票）→ 干净，不动
  - 不合格（买入日 zbc=0）→ 打标 legacy_invalid=true + legacy_reason
  - trades 历史一字不动
  - 持仓按止损规则自然换血出清，不做人工手术

用法：
    python audit_legacy_holdings.py            # 审计并打标
    python audit_legacy_holdings.py --dry-run  # 只预览不改
"""
import json
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO, cache_dir

import argparse
parser = argparse.ArgumentParser(description="历史持仓旧口径审计")
parser.add_argument("--dry-run", action="store_true", help="只预览不改")
args = parser.parse_args()

# 从严口径常量（与 closing_briefing.py 保持一致）
FBT_ONE_WORD = 93000   # 09:30 开盘瞬间


def norm(code: str) -> str:
    c = str(code or "")
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            return c[2:]
    return c


def audit_holding(h):
    """对单只持仓按买入日快照判定是否旧口径污染"""
    code = h.get("code", "")
    buy_date = h.get("buy_date", "")
    raw = norm(code)

    zt_path = cache_dir(buy_date) / "l2_zt_pool.json"
    if not zt_path.exists():
        return {"ok": True, "note": f"买入日 {buy_date} 无涨停池快照，无法审计"}

    zt = json.loads(zt_path.read_text(encoding="utf-8"))
    z = None
    for s in zt.get("stocks", []):
        if norm(s.get("code", "")) == raw:
            z = s
            break
    if z is None:
        return {"ok": True, "note": f"买入日 {buy_date} 不在涨停池（非涨停买入，无需过滤）"}

    try:
        fbt = int(z.get("fbt", 0))
        zbc = int(z.get("zbc", 0) or 0)
    except (TypeError, ValueError):
        return {"ok": True, "note": "fbt/zbc 数据异常"}

    if zbc >= 1:
        return {"ok": True, "note": f"买入日炸过板(zbc={zbc})，可成交，干净"}
    if fbt <= FBT_ONE_WORD:
        return {"ok": False, "reason": 1,
                "note": f"买入日一字/秒板(fbt={fbt})，旧口径虚高"}
    return {"ok": False, "reason": 2,
            "note": f"买入日排队未成交(fbt={fbt}, zbc=0)，旧口径虚高"}


with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

holdings = pf.get("holdings", [])
print(f"审计 {len(holdings)} 只持仓：\n")

invalid = []
for h in holdings:
    r = audit_holding(h)
    tag = "✅ 干净" if r["ok"] else f"🚩 旧口径污染 (reason={r['reason']})"
    print(f"  {h['name']:<8} {h.get('buy_date')} {tag} — {r['note']}")
    if not r["ok"]:
        invalid.append({"code": h["code"], "name": h["name"],
                        "buy_date": h.get("buy_date"),
                        "legacy_reason": r["reason"],
                        "note": r["note"]})

print(f"\n结论: {len(invalid)} 只旧口径污染 / {len(holdings)} 只持仓")
if not invalid:
    print("✅ 全部干净，无需打标")
    sys.exit(0)

if args.dry_run:
    print("\n🔍 DRY-RUN 不写文件")
    sys.exit(0)

# 打标（只加字段，不动 trades/account/其余字段）
for h in holdings:
    for inv in invalid:
        if h["code"] == inv["code"]:
            h["legacy_invalid"] = True
            h["legacy_reason"] = inv["legacy_reason"]

with open(PORTFOLIO, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

print(f"\n✅ 已打标 {len(invalid)} 只: legacy_invalid=true + legacy_reason")
print("   说明: 不回滚，按止损规则自然换血出清；日报脚注会反映剔除后收益")
