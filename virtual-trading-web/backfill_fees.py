"""Fill missing fees only; never debit already recorded fees again."""
import argparse
import copy
import json
from datetime import date
from pathlib import Path
from data_integrity import trade_fee, atomic_json_write


def fill_missing_fees(portfolio):
    pf = copy.deepcopy(portfolio)
    additional = 0.0
    count = 0
    for t in pf.get("trades", []):
        if t.get("fee") is None and t.get("amount", 0) > 0:
            t["fee"] = trade_fee(t["type"], t["amount"])
            additional += t["fee"]
            count += 1
    additional = round(additional, 2)
    acct = pf["account"]
    acct["total_fees"] = round(sum(t.get("fee", 0) for t in pf.get("trades", [])), 2)
    if count:
        # Preserve marked-to-market holdings value; fees move cash, not the quote.
        acct["cash"] = round(acct["cash"] - additional, 2)
        acct["total_value"] = round(acct["total_value"] - additional, 2)
        acct["pnl"] = round(acct["total_value"] - acct["initial_capital"], 2)
        acct["pnl_pct"] = round(acct["pnl"] / acct["initial_capital"] * 100, 2)
        pf.setdefault("ledger_adjustments", []).append({
            "date": date.today().isoformat(), "type": "missing_trade_fees",
            "amount": additional, "trades": count,
            "note": "Only newly assigned trade fees debited; already assigned fees unchanged."})
    return pf, count, additional


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parent.parent / "virtual-portfolio" / "portfolio.json"
    raw = path.read_bytes()
    original = json.loads(raw)
    fixed, count, amount = fill_missing_fees(original)
    print(f"Missing fee records: {count}; additional debit: {amount:.2f}")
    if not args.dry_run and fixed != original:
        if path.read_bytes() != raw:
            raise RuntimeError("Account changed during fee reconciliation")
        atomic_json_write(path, fixed)


if __name__ == "__main__":
    main()
