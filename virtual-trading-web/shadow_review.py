# -*- coding: utf-8 -*-
"""
shadow_review.py — 移动止盈影子模式转正评审（P1）
============================================================
用途：影子模式跑满观察期后（约 10-06），评估是否转正。
每月/每周可跑一次看累计口径。

口径声明:
  - 模拟侧: 影子触发日若执行——减半档卖 50%、清仓档卖 100%，
    成交价 = 触发日收盘价（trailing_shadow.cur），卖出现金不再投资
  - 实际侧: 同一只票实际持有到最近收盘的收益（未执行止盈）
  - 对比: 同票同触发点，"模拟收益" - "实际收益"，中位数口径
样本边界:
  - 数据源: portfolio.json 的 trailing_shadow（09-23 起积累）
  - 预期管理: 触发条件苛刻（浮盈≥20% 后回吐 1/3），账户多空仓/轻仓，
    3 周可能只有个位数触发——样本不足时宁可延长观察，别硬转正

转正判据（成文，二者同时满足）:
  ① 有效触发数 ≥ 5 个（同票多次触发只计首次）
  ② 模拟优于实际: 触发点"模拟-实际"中位差 > 0
  满足 → 建议转正；不满足 → 延长观察期
"""
import json
import statistics
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
_REPO = BASE_DIR.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(BASE_DIR))
from common_paths import PORTFOLIO

MIN_TRIGGERS = 5  # 转正判据①：有效触发数下限

with open(PORTFOLIO, encoding="utf-8") as f:
    pf = json.load(f)

shadow = pf.get("trailing_shadow", [])
triggers = [s for s in shadow if s.get("trigger")]

print("=" * 66)
print("🌅 移动止盈影子模式 · 转正评审")
print("=" * 66)

# ── 第一部分: D1 模式适用性分析（Claude哥 09-23 追加单）──
# 时间止损 P0 复核后默认 D1 快走（持有期 1 交易日）。移动止盈启用线
# 浮盈≥+20% vs D1 持有期单日涨幅上限（主板 10%）→ 数学上不可达。
# 机制时代错位: 移动止盈为"持有多日等主升"的旧世界设计，D1 新世界
# 里没有存在场景——评审若只看触发计数会开成空气会议（永远0触发
# →样本不足→延长的无限循环）。
from stop_loss import TIME_STOP_MODE

print()
print("【第一部分: D1 模式适用性分析】")
print(f"  当前 TIME_STOP_MODE = {TIME_STOP_MODE}")
if TIME_STOP_MODE == "D1":
    print("  ⚠️ D1 快走模式下移动止盈【无存在场景】:")
    print("    - 持有期 = 1 交易日，主板日涨幅上限 10% < 启用线 20% → 数学不可达")
    print("    - 20cm 创业板/科创板单日理论可达，但需买入日即涨停 20% 且当日")
    print("      回吐 1/3 → 概率趋零，不构成有效样本")
    print("  结论: 移动止盈应随 D1 模式【退役】（影子记录继续积累仅作档案）。")
    print("  若未来回退 D3D5 模式（持有多日等主升），本评审自动恢复有效。")
    print()
    print(f"  [档案] 影子记录 {len(shadow)} 条，有效触发 {len(triggers)} 次（D1 下预期恒为 0）")
    print()
    print("评审结论: 不转正（机制退役）。第 4-6 周 OOS 期间无需再跑本评审，")
    print("除非 TIME_STOP_MODE 回退。")
    sys.exit(0)
else:
    print(f"  D3D5 模式（持有多日）→ 移动止盈有存在场景，评审继续。")
print()

print(f"影子记录总数: {len(shadow)} 条（09-23 起积累）")
print(f"有效触发: {len(triggers)} 次（判据①要求 ≥{MIN_TRIGGERS}）")
print()

if not triggers:
    print("结论: 样本不足（0 触发）。")
    print("  预期内——触发条件苛刻（浮盈≥20% 后回吐 1/3），且账户多数时间")
    print("  空仓/轻仓。继续积累，建议每两周跑一次本脚本。")
    sys.exit(0)

# 同票多次触发只计首次（与信号统计去重口径一致）
seen = set()
first_triggers = []
for t in sorted(triggers, key=lambda x: x["date"]):
    if t["code"] in seen:
        continue
    seen.add(t["code"])
    first_triggers.append(t)

# 实际侧：该票最近已知价格（从持仓或 trades 推断——简化用 shadow 最新记录）
latest_by_code = {}
for s in shadow:
    latest_by_code[s["code"]] = s  # shadow 按日期追加，最后一条最新

diffs = []
print("触发点明细（同票只计首次）:")
for t in first_triggers:
    code = t["code"]
    cost = t.get("cost", 0)
    trigger_price = t.get("cur", 0)
    latest = latest_by_code.get(code, {})
    latest_price = latest.get("cur", trigger_price)
    shares = 0
    # 持仓里有 → 实际还在拿；没有 → 实际已按其他规则卖
    holding = next((h for h in pf.get("holdings", []) if h["code"] == code), None)
    if holding:
        shares = holding["shares"]
    sim_gain = (trigger_price - cost) / cost * 100 if cost else 0
    act_gain = (latest_price - cost) / cost * 100 if cost else 0
    d = sim_gain - act_gain
    diffs.append(d)
    print(f"  {t['date']} {t['name']} [{t['trigger']}档] 触发价¥{trigger_price:.2f} "
          f"→ 最新¥{latest_price:.2f} | 模拟锁定{sim_gain:+.1f}% vs 实际{act_gain:+.1f}% "
          f"| 差{d:+.1f}pp")

print()
if diffs:
    med = statistics.median(diffs)
    print(f"模拟-实际 中位差: {med:+.2f}pp（>0 = 止盈有用；<0 = 止盈砍了大牛）")
    print()
    print("═" * 66)
    print("转正判据评估:")
    c1 = len(first_triggers) >= MIN_TRIGGERS
    c2 = med > 0
    print(f"  ① 有效触发数 ≥{MIN_TRIGGERS}: {len(first_triggers)} 个 → {'✅' if c1 else '❌ 未满足'}")
    print(f"  ② 模拟优于实际（中位差>0）: {med:+.2f}pp → {'✅' if c2 else '❌ 未满足'}")
    print()
    if c1 and c2:
        print("  🎉 判据满足 → 建议转正（TIME_STOP 体系加入移动止盈层）")
    else:
        print("  ⏳ 判据未满足 → 延长观察期，继续积累影子数据")
    print("═" * 66)
