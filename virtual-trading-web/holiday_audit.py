# -*- coding: utf-8 -*-
"""
holiday_audit.py — 交易日历对账（把日历的固有风险变成可检测的错误）
============================================================
背景：MARKET_HOLIDAYS 是人工维护的表，可能多标（误判交易日为休市）
或少标（漏标休市日）。多标会让那天被永久跳过——少一天简报 = 少一天
止损检查，代价非零。本模块两条对账断言：

  ① 多标检测: 我们当休市跳过的日期，事后 K 线出现蜡烛了吗？
     → 出现即报警（表多标，需修正；K线权威已保证次日自愈，
       但依然要喊出来让人知道表错了）
  ② 少标检测: 我们跑过简报的日期，K 线里没有蜡烛吗？
     → 没有即报警（表少标/校验失效，那天可能跑了假数据）

用法:
    from holiday_audit import record_skip, audit_calendar
    record_skip("2026-10-05", "closing")     # 判定非交易日时记录
    issues = audit_calendar(trading_dates, daily_log_dates)  # 对账
"""
import json
import sys
from datetime import date
from pathlib import Path

# 注意：本模块被其他脚本 import，禁止在模块级包装 sys.stdout
# （重复包装会导致调用方 I/O operation on closed file）。

BASE_DIR = Path(__file__).resolve().parent
_REPO = BASE_DIR.parent
sys.path.insert(0, str(_REPO))
from common_paths import CACHE_DIR

SKIPS_FILE = CACHE_DIR / "holiday_skips.json"


def _load_skips() -> dict:
    try:
        return json.loads(SKIPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_skip(date_str: str, source: str):
    """记录一次'判定为非交易日跳过'（幂等：同日多来源合并记录）"""
    if not date_str:
        return
    skips = _load_skips()
    entry = skips.setdefault(date_str, {"sources": []})
    if source not in entry["sources"]:
        entry["sources"].append(source)
    SKIPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKIPS_FILE.write_text(json.dumps(skips, ensure_ascii=False, indent=2), encoding="utf-8")


def audit_calendar(trading_dates: list, daily_log_dates: list = None) -> list:
    """对账交易日历。返回 issue 列表（空 = 全部一致）。

    trading_dates   : fetch_trading_dates() 的 K 线交易日列表
    daily_log_dates : 跑过收盘简报的日期列表（portfolio daily_log）
    """
    issues = []
    if not trading_dates:
        return issues  # K线不可用时不判（避免误报）
    today = date.today().strftime("%Y-%m-%d")

    # ① 多标检测：曾跳过的日期，K线出现蜡烛
    skips = _load_skips()
    for d, meta in sorted(skips.items()):
        if d in trading_dates:
            issues.append(
                f"日历多标: {d} 曾被当作休市跳过（来源{meta.get('sources')}），"
                f"但K线已有蜡烛→该日期实际交易，请从 MARKET_HOLIDAYS 移除"
            )

    # ② 少标检测：跑过简报的日期，K线无蜡烛（限K线窗口覆盖范围内的历史日期）
    if daily_log_dates and trading_dates:
        window_start, window_end = trading_dates[0], trading_dates[-1]
        for d in daily_log_dates:
            if d >= today:
                continue  # 今天及以后蜡烛可能未生成，不判
            if window_start <= d <= window_end and d not in trading_dates:
                issues.append(
                    f"日历少标: {d} 跑过简报但K线无蜡烛→疑似休市日跑了假数据，"
                    f"请加入 MARKET_HOLIDAYS 并检查该日简报"
                )
    return issues


if __name__ == "__main__":
    # 自测
    sys.path.insert(0, str(BASE_DIR))
    from data_collector import fetch_trading_dates
    tds = fetch_trading_dates()
    issues = audit_calendar(tds, [])
    print(f"对账（多标检测，{len(tds)} 个K线交易日）:")
    if issues:
        for i in issues:
            print(f"  ⚠️ {i}")
    else:
        print("  ✅ 无多标（跳过记录与K线事实一致）")
