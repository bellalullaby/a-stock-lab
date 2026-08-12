# -*- coding: utf-8 -*-
"""
signal_tracker.py — 信号追踪引擎
从 portfolio.json 提取所有历史信号 → 用腾讯K线算 D+1/D+3/D+5/D+10 表现 → 写缓存。
用法: python signal_tracker.py
输出: data/signal_tracker.json
"""

import io, json, os, sys, time
from datetime import datetime
from pathlib import Path
from collections import defaultdict

if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != "utf-8":
    try: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except: pass

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data"
XK_PORTFOLIO = BASE_DIR.parent / "virtual-portfolio" / "portfolio.json"
OUTPUT = CACHE_DIR / "signal_tracker.json"

def to_tx(code):
    c = code.replace("sh","").replace("sz","").replace("bj","")
    if c.startswith("6"): return "sh"+c
    if c.startswith("0") or c.startswith("3"): return "sz"+c
    if c.startswith("9"): return "bj"+c
    return code

def fetch_klines(tx_code, n=120):
    """拉取一只股票的日K线。注意：不带 qfq 参数，数据在 day 字段。"""
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tx_code},day,,,{n},"
        r = requests.get(url, timeout=10)
        data = r.json()
        raw = data.get("data", {}).get(tx_code, {}).get("day", [])
        if not raw:
            # fallback: 带 qfq 参数的响应数据在 qfqday
            raw = data.get("data", {}).get(tx_code, {}).get("qfqday", [])
        klines = []
        for k in raw:
            try:
                klines.append({
                    "date": k[0],
                    "open": float(k[1]),
                    "close": float(k[2]),
                    "high": float(k[3]),
                    "low": float(k[4]),
                })
            except: pass
        return klines
    except Exception as e:
        return []

def find_forward_prices(klines, signal_date, offsets=[1, 3, 5, 10]):
    """在K线数组中找到 signal_date 之后第 offset 个交易日的收盘价。"""
    idx = None
    for i, k in enumerate(klines):
        if k["date"] >= signal_date:
            idx = i
            break
    if idx is None:
        return {o: None for o in offsets}
    
    result = {}
    for o in offsets:
        target = idx + o
        if target < len(klines):
            result[o] = {
                "date": klines[target]["date"],
                "close": round(klines[target]["close"], 2),
            }
        else:
            result[o] = None
    return result

def extract_signals():
    """从 portfolio.json 提取信号，去重盘前/收盘。"""
    pf = json.loads(XK_PORTFOLIO.read_text(encoding="utf-8"))
    daily = pf.get("daily_log", [])
    
    # 去重：同日期优先收盘简报
    by_date = {}
    for e in daily:
        date = e["date"]
        session = e.get("session", "")
        signals = e.get("signals", [])
        if not signals: continue
        if date not in by_date or "收盘" in session:
            by_date[date] = e
    
    all_signals = []
    for date, e in sorted(by_date.items()):
        for s in e.get("signals", []):
            all_signals.append({
                "signal_date": date,
                "l1_state": e.get("l1_state", ""),
                "l1_gate": e.get("l1_gate", ""),
                "code": s.get("code", ""),
                "name": s.get("name", ""),
                "lbc": s.get("lbc", 0),
                "price": s.get("price", 0),
                "pct": s.get("pct", 0),
                "pe": s.get("pe", 0),
                "buy_score": s.get("buy", 0),
                "sell_score": s.get("sell", 0),
                "net": s.get("net", 0),
                "out": s.get("out", ""),
                "industry": s.get("industry", ""),
                "hits": s.get("hits", []),
                "cap_yi": s.get("cap_yi", 0),
                "turnover": s.get("turnover", 0),
            })
    return all_signals

def compute_forward_returns(signals):
    """为每只信号计算前向收益。"""
    unique_codes = sorted(set(s["code"] for s in signals))
    print(f"[信号追踪] {len(unique_codes)} 只不同股票，拉取K线...")
    
    stock_klines = {}
    for i, tx in enumerate(unique_codes):
        if i % 30 == 0:
            print(f"  K线进度: {i}/{len(unique_codes)}")
        klines = fetch_klines(tx)
        stock_klines[tx] = klines
        time.sleep(0.03)
    
    print(f"[信号追踪] K线拉取完成，计算前向收益...")
    
    results = []
    for s in signals:
        klines = stock_klines.get(s["code"], [])
        fwd = find_forward_prices(klines, s["signal_date"])
        entry = {**s}
        for o in [1, 3, 5, 10]:
            info = fwd.get(o)
            if info:
                ret = round((info["close"] - s["price"]) / s["price"] * 100, 2)
                entry[f"d{o}_date"] = info["date"]
                entry[f"d{o}_close"] = info["close"]
                entry[f"d{o}_return"] = ret
            else:
                entry[f"d{o}_date"] = None
                entry[f"d{o}_close"] = None
                entry[f"d{o}_return"] = None
        results.append(entry)
    return results

def build_daily_summary(signals):
    """按日期汇总统计。"""
    by_date = defaultdict(list)
    for s in signals:
        by_date[s["signal_date"]].append(s)
    
    daily = []
    for date in sorted(by_date.keys()):
        batch = by_date[date]
        n = len(batch)
        stats = {"date": date, "l1_state": batch[0]["l1_state"], "total": n}
        for o in [1, 3, 5, 10]:
            returns = [s[f"d{o}_return"] for s in batch if s.get(f"d{o}_return") is not None]
            if returns:
                stats[f"avg_d{o}"] = round(sum(returns) / len(returns), 2)
                stats[f"win_d{o}"] = round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1)
                stats[f"n_d{o}"] = len(returns)
            else:
                stats[f"avg_d{o}"] = None
                stats[f"win_d{o}"] = None
                stats[f"n_d{o}"] = 0
        daily.append(stats)
    return daily

def main():
    print(f"[信号追踪] 读取 {XK_PORTFOLIO}")
    signals = extract_signals()
    print(f"[信号追踪] 提取到 {len(signals)} 条信号（去重盘前）")
    
    date_counts = defaultdict(int)
    for s in signals:
        date_counts[s["signal_date"]] += 1
    for d in sorted(date_counts):
        print(f"  {d}: {date_counts[d]} 条")
    
    tracked = compute_forward_returns(signals)
    daily = build_daily_summary(tracked)
    
    total = len(tracked)
    strong = [s for s in tracked if "强候选" in s.get("out", "")]
    observe = [s for s in tracked if "观察" in s.get("out", "")]
    
    print(f"\n[信号追踪] 全局统计:")
    print(f"  总信号: {total}, 强候选: {len(strong)}, 观察: {len(observe)}")
    
    for label, batch in [("强候选", strong), ("全部", tracked)]:
        for o in [1, 3, 5, 10]:
            returns = [s[f"d{o}_return"] for s in batch if s.get(f"d{o}_return") is not None]
            if returns:
                avg = sum(returns) / len(returns)
                win = sum(1 for r in returns if r > 0) / len(returns) * 100
                print(f"  {label} D{o}: avg={avg:+.2f}% win={win:.0f}% (n={len(returns)})")
    
    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "first_date": min(date_counts.keys()) if date_counts else "",
        "last_date": max(date_counts.keys()) if date_counts else "",
        "total_signals": total,
        "total_stocks": len(set(s["code"] for s in tracked)),
        "strong_count": len(strong),
        "observe_count": len(observe),
        "daily_summary": daily,
        "signals": tracked,
    }
    
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n[信号追踪] 写入 {OUTPUT}")
    print("[信号追踪] Done.")

if __name__ == "__main__":
    main()
