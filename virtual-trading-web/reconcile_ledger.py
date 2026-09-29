"""Recover ledger entries from archived evidence. Preview by default; --apply commits.

Never force a balance with a plug entry. Refuse ambiguous dates, conflicting trades,
negative inventory, or a cash difference not explained by missing principal.
"""
import argparse
import copy
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from data_integrity import atomic_json_write, trade_fee

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PF = ROOT / "virtual-portfolio" / "portfolio.json"


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def signature(t):
    return (t["type"], t["code"], int(t["shares"]), money(t["price"]), money(t["amount"]))


def reconcile(pf, sources):
    out = copy.deepcopy(pf)
    dated = defaultdict(lambda: defaultdict(list))
    original = {}
    for name, source in sources:
        for entry in source.get("daily_log", []):
            buckets = entry.get("trades")
            if not isinstance(buckets, dict):
                continue
            for bucket, kind in (("bought", "buy"), ("sold", "sell")):
                for trade in buckets.get(bucket, []):
                    t = {**trade, "type": trade.get("type", kind)}
                    day = t.get("date") or entry["date"]
                    key = signature(t)
                    dated[key][day].append(f"{name}:daily_log[{entry['date']}].{bucket}")
                    original[(day, key)] = t
        for h in source.get("holdings", []):
            price = h.get("cost") or h.get("cost_price") or h.get("buy_price")
            if price and h.get("buy_date"):
                t = {"type": "buy", "code": h["code"], "shares": h["shares"],
                     "price": price, "amount": round(price * h["shares"], 2)}
                dated[signature(t)][h["buy_date"]].append(f"{name}:holdings[{h['code']}]")

    changes = []
    for t in out["trades"]:
        if t.get("date"):
            continue
        evidence = dated.get(signature(t), {})
        if len(evidence) != 1:
            raise ValueError(f"日期无唯一依据: {t['code']} {t['type']}: {list(evidence)}")
        day = next(iter(evidence))
        t["date"] = day
        t["recovery_sources"] = sorted(evidence[day])
        changes.append({"action": "restore_date", "code": t["code"], "type": t["type"],
                        "date": day, "sources": t["recovery_sources"]})

    # Only add absent buys proven by a dated trade AND matching archived holdings.
    present = {(t["date"], signature(t)) for t in out["trades"]}
    missing_principal = Decimal(0)
    added_fees = Decimal(0)
    for (day, key), t in sorted(original.items()):
        if key[0] != "buy" or (day, key) in present:
            continue
        evidence = dated[key][day]
        if not any(":holdings[" in item for item in evidence):
            raise ValueError(f"买入缺少持仓佐证: {day} {key[1]}")
        # Do not create another trade when date/type/code exists at a different price.
        if any(x.get("date") == day and x["type"] == "buy" and x["code"] == key[1]
               for x in out["trades"]):
            raise ValueError(f"买入记录冲突: {day} {key[1]}")
        restored = {**t, "date": day, "amount": float(key[4]),
                    "fee": trade_fee("buy", float(key[4])), "recovery_sources": sorted(evidence)}
        out["trades"].append(restored)
        present.add((day, key))
        missing_principal += key[4]
        added_fees += money(restored["fee"])
        changes.append({"action": "restore_buy", "trade": restored})

    cash = money(out["account"]["initial_capital"])
    inventory = defaultdict(int)
    # End-of-day inventory invariant, independent of the old buy-before-sell ordering.
    days = sorted({t["date"] for t in out["trades"]})
    for day in days:
        for t in (t for t in out["trades"] if t["date"] == day):
            amount, fee = money(t["amount"]), money(t.get("fee", 0))
            if amount != money(t["price"] * t["shares"]):
                raise ValueError(f"成交金额不匹配: {t}")
            sign = 1 if t["type"] == "buy" else -1
            inventory[t["code"]] += sign * t["shares"]
            cash -= sign * amount + fee
        if any(n < 0 for n in inventory.values()):
            raise ValueError(f"{day} 出现负持仓")
    actual = {h["code"]: h["shares"] for h in out.get("holdings", [])}
    rebuilt = {c: n for c, n in inventory.items() if n}
    if rebuilt != actual:
        raise ValueError(f"持仓对不上: {rebuilt} != {actual}")
    old_cash = money(out["account"]["cash"])
    # Principal was already debited historically. Only newly discovered fees may move cash.
    if cash != old_cash - added_fees:
        raise ValueError(f"仍有无法解释现金差额: 重建 {cash}, 应为 {old_cash - added_fees}")

    if changes:
        out["trades"].sort(key=lambda t: (t["date"], 0 if t["type"] == "buy" else 1))
        acct = out["account"]
        acct["cash"] = float(cash)
        acct["total_value"] = float(money(acct["total_value"]) - added_fees)
        acct["pnl"] = round(acct["total_value"] - acct["initial_capital"], 2)
        acct["pnl_pct"] = round(acct["pnl"] / acct["initial_capital"] * 100, 2)
        acct["total_fees"] = round(sum(t.get("fee", 0) for t in out["trades"]), 2)
        adjustment = {"date": date.today().isoformat(), "type": "recovered_trade_fees",
                      "amount": float(added_fees),
                      "note": "补齐遗漏买入的费用；本金历史已扣，不重复扣本金。费用已分配至对应流水，不另算第二次。"}
        out.setdefault("ledger_adjustments", []).append(adjustment)
        # Preserve old historical snapshots. Correct today's endpoint only, with provenance.
        for snapshot in out.get("daily_snapshots", []):
            if snapshot.get("date") == adjustment["date"]:
                for field in ("cash", "total_value", "pnl", "pnl_pct"):
                    snapshot[field] = acct[field]
                snapshot["ledger_adjustment"] = adjustment

    return out, {"changes": changes, "restored_principal_already_debited": float(missing_principal),
                 "newly_debited_fees": float(added_fees), "old_cash": float(old_cash),
                 "reconstructed_cash": float(cash), "inventory": rebuilt,
                 "checks": {"cash": True, "inventory": True, "dated_trades": True}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = DEFAULT_PF.read_bytes()
    pf = json.loads(raw)
    sources = []
    for path in sorted(DEFAULT_PF.parent.glob("portfolio*")):
        if path.is_file():
            try:
                sources.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, UnicodeError):
                continue
    fixed, report = reconcile(pf, sources)
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.apply and report["changes"]:
        # Preserve exact bytes and reject a concurrently changed account.
        if DEFAULT_PF.read_bytes() != raw:
            raise RuntimeError("账户已被其他进程修改，停止修复")
        backup = DEFAULT_PF.with_name("portfolio.json.bak_ledger_" + report["input_sha256"][:12])
        with backup.open("xb") as stream:
            stream.write(raw)
        report["backup"] = str(backup)
        atomic_json_write(DEFAULT_PF, fixed)
    if args.apply:
        report_path = Path(__file__).parent / "data" / "ledger_reconciliation.json"
        if report["changes"] or not report_path.exists():
            atomic_json_write(report_path, report)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
