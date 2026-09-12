# -*- coding: utf-8 -*-
"""
backfill_fees.py — 历史交易费用一次性回补（A 修复项）
============================================================
背景：虚拟盘此前零成本成交（无佣金/印花税/过户费），收益虚高。
本脚本对全部历史 trades 按 same 费率回补 fee 字段，并一次性调整账户：
  - trades[].fee 补齐（幂等：已有 fee 的跳过）
  - account.total_fees 累计
  - account.cash 扣减总费用、total_value/pnl/pnl_pct 重算
  - account.fee_adjustment 留痕

费率：佣金 万2.5 最低5元（双边）· 印花税 0.05%（卖单边）· 过户费 0.001%（双边）

用法：
    python backfill_fees.py            # 回补
    python backfill_fees.py --dry-run  # 只预览
"""
import argparse
import json
import sys
import io
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO

parser = argparse.ArgumentParser(description="历史交易费用回补")
parser.add_argument("--dry-run", action="store_true", help="只预览不写")
args = parser.parse_args()


def trade_fee(trade_type: str, amount: float) -> float:
    commission = max(amount * 0.00025, 5.0)
    stamp = amount * 0.0005 if trade_type == "sell" else 0.0
    transfer = amount * 0.00001
    return round(commission + stamp + transfer, 2)


with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

trades = pf.get("trades", [])
total_fees = 0.0
patched = 0
for t in trades:
    amount = t.get("amount", 0)
    if not amount or t.get("fee") is not None:
        continue
    fee = trade_fee(t.get("type", "buy"), amount)
    t["fee"] = fee
    total_fees += fee
    patched += 1

# 之前已回补过一部分时，累计总费用按全部 trades 重算（幂等）
total_fees = round(sum(t.get("fee", 0) for t in trades if t.get("fee") is not None), 2)

print(f"回补 {patched} 笔，历史总费用 ¥{total_fees:,.2f}")
old_cash = pf["account"].get("cash", 0)
new_cash = round(old_cash - total_fees, 2)
initial = pf["account"].get("initial_capital", 1000000)

# 市值按现估值不变，total_value = new_cash + 市值
hold_value = 0.0
for h in pf.get("holdings", []):
    cost = h.get("cost", h.get("buy_price", 0))
    hold_value += (h.get("buy_price") or cost) * h.get("shares", 0)  # 无实时价时保守用买入价
new_total = round(new_cash + hold_value, 2)
new_pnl = round(new_total - initial, 2)
new_pnl_pct = round(new_pnl / initial * 100, 2)

print(f"账户调整: cash {old_cash:,.2f} → {new_cash:,.2f}")
print(f"total_value {pf['account'].get('total_value', 0):,.2f} → {new_total:,.2f} "
      f"(pnl {pf['account'].get('pnl', 0):+,.2f} → {new_pnl:+,.2f} / {new_pnl_pct:+.2f}%)")

if args.dry_run:
    print("\n🔍 DRY-RUN 不写文件")
    sys.exit(0)

if patched == 0 and abs(pf["account"].get("total_fees", 0) - total_fees) < 0.01:
    print("✅ 已回补过，无需重复")
    sys.exit(0)

pf["account"]["cash"] = new_cash
pf["account"]["total_value"] = new_total
pf["account"]["pnl"] = new_pnl
pf["account"]["pnl_pct"] = new_pnl_pct
pf["account"]["total_fees"] = total_fees
pf["account"]["fee_adjustment"] = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "amount": total_fees,
    "note": f"历史 {len(trades)} 笔交易费用一次性回补（佣金万2.5/印花税0.05%卖/过户费0.001%），此后实时计费",
}

with open(PORTFOLIO, "w", encoding="utf-8") as f:
    json.dump(pf, f, ensure_ascii=False, indent=2)

print(f"\n✅ 回补完成，留痕于 account.fee_adjustment")
