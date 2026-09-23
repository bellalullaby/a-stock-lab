# -*- coding: utf-8 -*-
"""
test_time_stop_mode.py — 时间止损模式单测（落盘版，可复跑）
============================================================
覆盖: D1 快走模式 5 项 + D3D5 回退模式
运行: python test_time_stop_mode.py
"""
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
from stop_loss import run_stop_loss
import stop_loss

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}")


print("=== D1 快走模式（TIME_STOP_MODE='D1' 默认）===")
# 1. 昨天买的 → 今天 D+1 全走
h1 = {"code": "sz000001", "name": "昨买票", "shares": 1000, "cost": 10.0, "buy_date": "2026-09-22"}
sells, missed = run_stop_loss([h1], {"sz000001": 10.5}, None, None, None, "2026-09-23")
check("昨买票 D+1 全走（卖价=实时价10.5, amount=10500）",
      len(sells) == 1 and sells[0]["price"] == 10.5 and sells[0]["amount"] == 10500.0)

# 2. 今天买的 → T+1 挡
h2 = {**h1, "name": "今买票", "buy_date": "2026-09-23"}
sells2, _ = run_stop_loss([h2], {"sz000001": 10.5}, None, None, None, "2026-09-23")
check("T+1 当日买入不卖", len(sells2) == 0)

# 3. D1 遇跌停封死 → 拒卖进 missed_sells
h3 = {"code": "sz000002", "name": "跌停票", "shares": 500, "cost": 10.0, "buy_date": "2026-09-22"}
dt_locked = {"stocks": [{"code": "000002", "name": "跌停票", "price": 9.0, "oc": 0, "days": 1}]}
sells3, missed3 = run_stop_loss([h3], {"sz000002": 9.0}, None, None, None, "2026-09-23",
                                l2_dt_pool=dt_locked)
check("D1 遇跌停封死拒卖（missed_sells 1 条）",
      len(sells3) == 0 and len(missed3) == 1 and missed3[0]["reason"] == 1)

# 4. D1 遇炸板跌停 → 按跌停价走
dt_opened = {"stocks": [{"code": "000002", "name": "跌停票", "price": 9.0, "oc": 1, "days": 1}]}
sells4, _ = run_stop_loss([h3], {"sz000002": 9.0}, None, None, None, "2026-09-23",
                          l2_dt_pool=dt_opened)
check("D1 遇炸板跌停按跌停价走（9.0）",
      len(sells4) == 1 and sells4[0]["price"] == 9.0)

print()
print("=== D3D5 回退模式 ===")
_old = stop_loss.TIME_STOP_MODE
stop_loss.TIME_STOP_MODE = "D3D5"
h4 = {"code": "sz000003", "name": "老票", "shares": 1000, "cost": 10.0, "buy_date": "2026-09-01"}
sells5, _ = run_stop_loss([h4], {"sz000003": 10.5}, None, None, None, "2026-09-23")
check("D3D5 模式: +7% 未触发 D3<2/D5<5 → 不卖", len(sells5) == 0)
h5 = {**h4, "cost": 10.0}
sells6, _ = run_stop_loss([h5], {"sz000003": 10.1}, None, None, None, "2026-09-23")
check("D3D5 模式: +1% < 2% → 时间止损触发", len(sells6) == 1)
stop_loss.TIME_STOP_MODE = _old

print()
print(f"结果: {PASS} 过 / {FAIL} 败")
sys.exit(1 if FAIL else 0)
