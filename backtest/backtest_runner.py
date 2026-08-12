# -*- coding: utf-8 -*-
"""
A股 SOP 历史回测引擎 v1.0
===========================
基于 ashare-sop-engine L1+L3 规则，使用腾讯免费 K 线 API。
不依赖数据库，不依赖 API key，开箱即用。

用法：
    python backtest_runner.py                        # 使用默认配置跑
    python backtest_runner.py --config my_config.json  # 指定配置文件
"""

import sys, io, os, json, time, csv, argparse
from datetime import datetime, timedelta
from collections import defaultdict, OrderedDict

# === 防 emoji 崩溃（Git Bash GBK 编码） ===
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import requests
import pandas as pd
import numpy as np

# === 常量 ===
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 指数代码 → 腾讯格式
INDEX_MAP = OrderedDict({
    "sh000001": "上证指数",   # L1 主判断依据
    "sz399001": "深证成指",   # L1 辅助
    "sz399006": "创业板指",   # L1 辅助
})

BENCHMARK_CODE = "sh000300"  # 沪深300 — 基准对比

# K 线缓存过期时间（秒）：12 小时
CACHE_TTL = 43200


# ============================================================
#  工具函数
# ============================================================

def symbol_to_tx(symbol: str) -> str:
    """600519 -> sh600519, 002384 -> sz002384"""
    if symbol.startswith(('6', '9')):
        return f"sh{symbol}"
    else:
        return f"sz{symbol}"


def tx_to_symbol(tx_code: str) -> str:
    """sh600519 -> 600519"""
    return tx_code[2:]


def is_shanghai(symbol: str) -> bool:
    return symbol.startswith(('6', '9'))


def load_config(config_path: str) -> dict:
    """加载配置文件，补默认值"""
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    cfg.setdefault("initial_capital", 1_000_000)
    cfg.setdefault("position_size_pct", 0.1)
    cfg.setdefault("max_positions", 5)
    cfg.setdefault("hold_days", 5)
    cfg.setdefault("commission", 0.0003)
    cfg.setdefault("stamp_tax", 0.001)
    cfg.setdefault("l1_enabled", True)
    cfg.setdefault("limit_up_only", False)
    cfg.setdefault("stop_loss_pct", -0.05)
    cfg.setdefault("output_dir", "./output")
    cfg.setdefault("cache_dir", "./cache")
    cfg.setdefault("symbols_file", None)

    # 支持从外部文件加载股票列表（如 hs300_symbols.json）
    if cfg.get("symbols_file"):
        symbols_path = cfg["symbols_file"]
        if not os.path.isabs(symbols_path):
            symbols_path = os.path.join(os.path.dirname(config_path), symbols_path)
        if os.path.exists(symbols_path):
            with open(symbols_path, 'r', encoding='utf-8') as f:
                file_symbols = json.load(f)
            if isinstance(file_symbols, list) and len(file_symbols) > 0:
                cfg["symbols"] = file_symbols
                print(f"[INFO] 从 {cfg['symbols_file']} 加载了 {len(file_symbols)} 只股票")
        else:
            print(f"[WARN] symbols_file 不存在: {symbols_path}")

    return cfg


# ============================================================
#  数据拉取（腾讯 API + 本地缓存）
# ============================================================

def fetch_klines_raw(tx_code: str, limit: int = 250) -> list:
    """
    从腾讯 API 拉取日K线（前复权）。
    返回 list of [date(str), open(float), close(float), high(float), low(float), volume(float)]
    """
    url = (
        f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={tx_code},day,,,{limit},qfq"
    )
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            print(f"  [WARN] {tx_code} API返回 code={data.get('code')}: {data.get('msg', '')}")
            return []

        raw = data.get("data", {}).get(tx_code, {})
        if raw is None:
            return []

        day_list = raw.get("day")
        if not day_list:
            # 尝试 qfqday（某些旧格式）
            day_list = raw.get("qfqday", [])

        rows = []
        for item in day_list:
            if len(item) < 6:
                continue
            try:
                rows.append([
                    str(item[0]),               # date
                    float(item[1]),             # open
                    float(item[2]),             # close
                    float(item[3]),             # high
                    float(item[4]),             # low
                    float(item[5]),             # volume (股)
                ])
            except (ValueError, TypeError):
                continue
        return rows
    except Exception as e:
        print(f"  [ERROR] {tx_code} 拉取失败: {e}")
        return []


def get_cache_path(tx_code: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{tx_code}.csv")


def is_cache_fresh(cache_path: str) -> bool:
    if not os.path.exists(cache_path):
        return False
    age = time.time() - os.path.getmtime(cache_path)
    return age < CACHE_TTL


def load_cache(cache_path: str) -> list:
    """从 CSV 缓存读取K线，返回与 fetch_klines_raw 相同的格式"""
    rows = []
    with open(cache_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip header
        for line in reader:
            if len(line) < 6:
                continue
            try:
                rows.append([line[0], float(line[1]), float(line[2]),
                             float(line[3]), float(line[4]), float(line[5])])
            except (ValueError, IndexError):
                continue
    return rows


def save_cache(rows: list, cache_path: str):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "close", "high", "low", "volume"])
        writer.writerows(rows)


def fetch_klines(tx_code: str, cache_dir: str, limit: int = 250) -> list:
    """
    拉取K线数据，缓存优先。
    返回 list of [date, open, close, high, low, volume]，按日期升序排列。
    """
    cache_path = get_cache_path(tx_code, cache_dir)
    if is_cache_fresh(cache_path):
        rows = load_cache(cache_path)
        if rows:
            return rows

    print(f"    拉取 {tx_code} ...")
    rows = fetch_klines_raw(tx_code, limit)
    if rows:
        # 按日期升序
        rows.sort(key=lambda r: r[0])
        save_cache(rows, cache_path)
    else:
        # 尝试读过期缓存
        if os.path.exists(cache_path):
            print(f"    [WARN] {tx_code} API 返回空，使用过期缓存")
            rows = load_cache(cache_path)

    return rows


def klines_to_dict(rows: list) -> dict:
    """
    将K线列表转为 {date: {open,close,high,low,volume}} 字典。
    """
    result = OrderedDict()
    for r in rows:
        result[r[0]] = {
            "open": r[1],
            "close": r[2],
            "high": r[3],
            "low": r[4],
            "volume": r[5],
        }
    return result


# ============================================================
#  技术指标
# ============================================================

def moving_average(values: list, period: int) -> list:
    """简单移动均线，前 period-1 个位置为 NaN"""
    if len(values) < period:
        return [float('nan')] * len(values)
    result = [float('nan')] * (period - 1)
    window = list(values[:period])
    window_sum = sum(window)
    result.append(window_sum / period)
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result.append(window_sum / period)
    return result


def ma_direction(ma_values: list, idx: int, lookback: int = 5) -> str:
    """
    判断均线方向。
    返回 "UP" / "DOWN" / "FLAT"
    """
    if idx < lookback:
        return "FLAT"
    cur = ma_values[idx]
    prev = ma_values[idx - lookback]
    if pd.isna(cur) or pd.isna(prev):
        return "FLAT"
    if cur > prev * 1.001:
        return "UP"
    elif cur < prev * 0.999:
        return "DOWN"
    return "FLAT"


# ============================================================
#  L1 宏观环境分类
# ============================================================

def classify_l1(index_closes: dict, trading_days: list, date_idx: int) -> str:
    """
    基于上证指数 MA20/MA60 判定市场环境。

    参数：
        index_closes: {tx_code: [close_per_day]}  — 对齐 trading_days
        trading_days: 交易日列表
        date_idx: 当前日期在 trading_days 中的索引

    返回：
        "系统性风险" / "震荡市" / "多头趋势"
    """
    sh_closes = index_closes.get("sh000001", [])

    if date_idx < 60:
        return "震荡市"  # MA60 还没形成，保守处理

    # 计算 MA20 和 MA60
    ma20_vals = moving_average(sh_closes, 20)
    ma60_vals = moving_average(sh_closes, 60)

    close = sh_closes[date_idx]
    ma20 = ma20_vals[date_idx]
    ma60 = ma60_vals[date_idx]

    if pd.isna(ma20) or pd.isna(ma60):
        return "震荡市"

    ma20_dir = ma_direction(ma20_vals, date_idx)
    ma60_dir = ma_direction(ma60_vals, date_idx)

    # 条件1：连续3天收盘在 MA20 下方 AND MA20 下行
    below_ma20_streak = 0
    for i in range(date_idx, max(-1, date_idx - 3), -1):
        if sh_closes[i] < ma20_vals[i]:
            below_ma20_streak += 1
        else:
            break

    # 条件2：收盘在 MA60 下方 AND MA60 下行
    below_ma60 = close < ma60 and ma60_dir == "DOWN"

    # 系统性风险判定
    if (below_ma20_streak >= 3 and ma20_dir == "DOWN") or below_ma60:
        return "系统性风险"

    # 多头趋势判定
    if (close > ma20 and close > ma60
            and ma20_dir == "UP" and ma60_dir == "UP"):
        return "多头趋势"

    # 其余为震荡市
    return "震荡市"


# ============================================================
#  L3 个股买卖评分
# ============================================================

def compute_l3_scores(stock_data: dict, trading_days: list, date_idx: int) -> dict | None:
    """
    对给定股票在给定交易日计算 buy_score 和 sell_score。

    参数：
        stock_data: {date: {open,close,high,low,volume}}
        trading_days: 全局交易日历
        date_idx: 当前日期索引

    返回：
        {"buy": int, "sell": int, "rules": {"buy": [...], "sell": [...]}}
        如果数据不够返回 None
    """
    # 需要至少 40 天数据（但20天也够基础判断了）
    if date_idx < 20:
        return None

    date = trading_days[date_idx]
    if date not in stock_data:
        return None

    today = stock_data[date]
    close = today["close"]
    high = today["high"]
    low = today["low"]
    vol = today["volume"]

    # 前一日
    prev_date = trading_days[date_idx - 1] if date_idx > 0 else None

    # --- 收集历史数据 ---
    closes = []
    highs = []
    lows = []
    volumes = []
    for i in range(date_idx + 1):
        d = trading_days[i]
        if d in stock_data:
            closes.append(stock_data[d]["close"])
            highs.append(stock_data[d]["high"])
            lows.append(stock_data[d]["low"])
            volumes.append(stock_data[d]["volume"])
        else:
            # 停牌日：补前一日数据
            if closes:
                closes.append(closes[-1])
                highs.append(highs[-1])
                lows.append(lows[-1])
                volumes.append(0)
            else:
                return None

    if len(closes) < 21:
        return None

    # 均线
    ma5_vals = moving_average(closes, 5)
    ma10_vals = moving_average(closes, 10)

    ma5 = ma5_vals[-1]   # 当日 MA5
    ma10 = ma10_vals[-1]  # 当日 MA10

    prev_close = closes[-2] if len(closes) >= 2 else close
    prev_ma5 = ma5_vals[-2] if len(ma5_vals) >= 2 else ma5

    # 20日最高/最低
    high_20d = max(highs[-21:-1]) if len(highs) >= 21 else high
    low_10d = min(lows[-11:-1]) if len(lows) >= 11 else low

    # 量比 = 当日量 / 5日均量（排除当日）
    avg_vol_5d = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else vol
    vol_ratio = vol / avg_vol_5d if avg_vol_5d > 0 else 1.0

    # 涨跌幅
    chg_pct = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0.0

    # --- 计算 5 条买入规则 ---
    buy_score = 0
    buy_rules = []

    # R1: 突破20日高 + 放量 — DEPRECATED
    # 回测验证：全市场扫描时触发率 41%（虚高 buy_score），但涨停池内触发率 0%
    # 该规则仅在涨停池中有意义，全市场模式下禁用。保留代码供涨停池模式使用。
    # if close > high_20d * 1.002 and vol_ratio >= 1.25:
    #     buy_score += 1
    #     buy_rules.append("R1:突破20D高放量")

    # R2: 重站5日线 + 放量 (price > MA5 AND prev_close < prev_MA5 AND vol_ratio >= 1.10)
    if close > ma5 and prev_close < prev_ma5 and vol_ratio >= 1.10:
        buy_score += 1
        buy_rules.append("R2:重站5日线放量")

    # R3: 5/10日线上方走强 (price > MA5 > MA10 AND chg_pct >= 0.8%)
    if close > ma5 > ma10 and chg_pct >= 0.8:
        buy_score += 1
        buy_rules.append("R3:5-10线上方走强")

    # R4: 异常放量上行 (vol_ratio >= 1.8 AND chg_pct > 0)
    if vol_ratio >= 1.8 and chg_pct > 0:
        buy_score += 1
        buy_rules.append("R4:异常放量上行")

    # R5: 刷新20日高点 (price >= high_20d)
    if close >= high_20d:
        buy_score += 1
        buy_rules.append("R5:刷新20D高点")

    # --- 计算 4 条卖出规则 ---
    sell_score = 0
    sell_rules = []

    # S1: 跌破10日低 + 放量 (price < low_10d * 0.998 AND vol_ratio >= 1.20)
    if close < low_10d * 0.998 and vol_ratio >= 1.20:
        sell_score += 1
        sell_rules.append("S1:跌破10D低放量")

    # S2: 跌破5日线 + 跌幅扩大 (price < MA5 AND chg_pct <= -1.5%)
    if close < ma5 and chg_pct <= -1.5:
        sell_score += 1
        sell_rules.append("S2:跌破5日线跌幅扩大")

    # S3: 回撤超3% (chg_pct <= -3.0%)
    if chg_pct <= -3.0:
        sell_score += 1
        sell_rules.append("S3:回撤超3%")

    # S4: 异常放量下行 (vol_ratio >= 1.8 AND chg_pct < -1.0%)
    if vol_ratio >= 1.8 and chg_pct < -1.0:
        sell_score += 1
        sell_rules.append("S4:异常放量下行")

    return {
        "buy": buy_score,
        "sell": sell_score,
        "rules": {"buy": buy_rules, "sell": sell_rules},
        "chg_pct": round(chg_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "ma5": round(ma5, 2) if not pd.isna(ma5) else None,
        "ma20": round(moving_average(closes, 20)[-1], 2) if len(closes) >= 20 else None,
        "close": close,
    }


# ============================================================
#  前向收益计算
# ============================================================

def compute_forward_returns(stock_data: dict, signal_date: str,
                             trading_days: list, horizons: list = None) -> dict:
    """
    计算信号日后 N 个交易日的收益率。
    返回 {"D1": 0.0123, "D3": 0.0234, ...} 或 None 表示无数据。
    """
    if horizons is None:
        horizons = [1, 3, 5, 10]

    if signal_date not in trading_days:
        return {}

    signal_idx = trading_days.index(signal_date)
    entry_price = stock_data.get(signal_date, {}).get("close")
    if entry_price is None:
        return {}

    result = {}
    for h in horizons:
        target_idx = signal_idx + h
        if target_idx >= len(trading_days):
            result[f"D{h}"] = None
            continue

        target_date = trading_days[target_idx]
        target_data = stock_data.get(target_date)
        if target_data is None:
            result[f"D{h}"] = None
        else:
            exit_price = target_data["close"]
            result[f"D{h}"] = round((exit_price - entry_price) / entry_price, 6)

    return result


# ============================================================
#  主回测流程
# ============================================================

def run_backtest(config: dict) -> dict:
    """
    主回测函数。
    返回包含 signals、l1_states、trading_days、benchmark_returns 的结果字典。
    """
    start_date = config["start_date"]
    end_date = config["end_date"]
    symbols = config["symbols"]
    l1_enabled = config.get("l1_enabled", True)
    cache_dir = config["cache_dir"]
    output_dir = config["output_dir"]

    os.makedirs(output_dir, exist_ok=True)

    print("=" * 50)
    print("  A股 SOP 回测系统 v1.0 — L1+L3 规则引擎")
    print("=" * 50)
    print(f"  回测区间: {start_date} → {end_date}")
    print(f"  标的数量: {len(symbols)} 只")
    print(f"  L1 门控: {'开启' if l1_enabled else '关闭'}")
    print()

    # ---- 第一步：拉取所有 K 线数据 ----
    print("[1/5] 拉取K线数据...")

    all_tx_codes = [symbol_to_tx(s) for s in symbols]
    index_codes = list(INDEX_MAP.keys()) + [BENCHMARK_CODE]

    kline_raw = {}  # {tx_code: [rows]}

    # 拉指数
    for tx in index_codes:
        rows = fetch_klines(tx, cache_dir, limit=250)
        kline_raw[tx] = rows
        name = INDEX_MAP.get(tx, "沪深300")
        status = "缓存命中" if is_cache_fresh(get_cache_path(tx, cache_dir)) else "API拉取"
        print(f"  {name} ({tx}): {len(rows)} 条 ({status})")

    # 拉个股
    for tx in all_tx_codes:
        rows = fetch_klines(tx, cache_dir, limit=250)
        kline_raw[tx] = rows
        status = "缓存命中" if is_cache_fresh(get_cache_path(tx, cache_dir)) else "API拉取"
        print(f"  {tx}: {len(rows)} 条 ({status})")
        time.sleep(0.3)  # 温柔限速

    # ---- 第二步：预处理数据 ----
    print()
    print("[2/5] 预处理数据...")

    # 转为 dict 格式
    kline_dict = {}
    for tx, rows in kline_raw.items():
        kline_dict[tx] = klines_to_dict(rows)

    # 以 上证指数 的日期为交易日历
    sh_rows = kline_raw.get("sh000001", [])
    if not sh_rows:
        print("[ERROR] 上证指数数据为空，无法构建交易日历！")
        return {}

    all_trading_days = [r[0] for r in sh_rows]

    # 过滤到回测区间内的交易日
    trading_days = [d for d in all_trading_days if start_date <= d <= end_date]

    if not trading_days:
        print(f"[ERROR] 区间 {start_date}~{end_date} 内无交易日！")
        return {}

    print(f"  交易日历: {len(all_trading_days)} 个交易日（全部）")
    print(f"  回测区间: {len(trading_days)} 个交易日")

    # ---- 第三步：逐日计算 L1 和 L3，生成信号 ----
    print()
    print("[3/5] 扫描买卖信号...")

    # 构建上证指数 close 序列（对齐 all_trading_days）
    sh_closes = []
    for d in all_trading_days:
        bar = kline_dict["sh000001"].get(d)
        sh_closes.append(bar["close"] if bar else float('nan'))

    sz_closes = []
    for d in all_trading_days:
        bar = kline_dict.get("sz399001", {}).get(d)
        sz_closes.append(bar["close"] if bar else float('nan'))

    index_closes = {
        "sh000001": sh_closes,
        "sz399001": sz_closes,
    }

    # 计算每日 L1 状态
    l1_states = {}
    l1_counts = defaultdict(int)

    for i, date in enumerate(all_trading_days):
        state = classify_l1(index_closes, all_trading_days, i)
        l1_states[date] = state
        if start_date <= date <= end_date:
            l1_counts[state] += 1

    print(f"  L1 分布: 多头趋势={l1_counts['多头趋势']}天, "
          f"震荡市={l1_counts['震荡市']}天, "
          f"系统性风险={l1_counts['系统性风险']}天")

    # 扫描信号
    signals = []
    scan_count = 0
    l1_blocked = 0

    for i, date in enumerate(trading_days):
        l1 = l1_states[date]

        # L1 门控
        if l1_enabled and l1 == "系统性风险":
            l1_blocked += len(symbols)
            continue

        # 在 all_trading_days 中的索引
        global_idx = all_trading_days.index(date)

        for sym in symbols:
            tx = symbol_to_tx(sym)
            stock_dict = kline_dict.get(tx, {})
            scan_count += 1

            scores = compute_l3_scores(stock_dict, all_trading_days, global_idx)
            if scores is None:
                continue

            # 买入信号：buy_score >= 2 AND buy_score >= sell_score + 1
            if scores["buy"] >= 2 and scores["buy"] >= scores["sell"] + 1:
                signal = {
                    "date": date,
                    "symbol": sym,
                    "l1_state": l1,
                    "buy_score": scores["buy"],
                    "sell_score": scores["sell"],
                    "rules_hit": scores["rules"]["buy"],
                    "price": scores["close"],
                    "chg_pct": scores["chg_pct"],
                    "vol_ratio": scores["vol_ratio"],
                }

                # 计算前向收益
                fwd = compute_forward_returns(stock_dict, date, all_trading_days)
                signal["forward_returns"] = fwd

                signals.append(signal)

    # --- 涨停过滤（可选）---
    limit_up_only = config.get("limit_up_only", False)
    if limit_up_only:
        # 主板 10% 涨停，创业板/科创板 20% 涨停
        def is_limit_up(sig):
            sym = sig["symbol"]
            chg = sig["chg_pct"]
            if sym.startswith(('30', '68')):  # 创业板/科创板 20% 涨跌停
                return chg >= 19.5
            return chg >= 9.5  # 主板 10% 涨跌停

        before = len(signals)
        signals = [s for s in signals if is_limit_up(s)]
        print(f"  涨停过滤: {before} → {len(signals)} 个信号 (仅保留涨停股)")

    print(f"  扫描 {len(symbols)} 只股票 x {len(trading_days)} 天 = {scan_count} 次")
    print(f"  L1 屏蔽: {l1_blocked} 次（系统性风险日）")
    print(f"  发现信号: {len(signals)} 个")

    if not signals:
        print()
        print("[WARN] 未发现任何买入信号！可能原因：")
        print("  1. 回测区间太短或全是系统性风险")
        print("  2. 股票池太小")
        print("  3. buy_score 阈值太高")
        return {
            "signals": [],
            "l1_states": l1_states,
            "trading_days": trading_days,
            "l1_counts": dict(l1_counts),
        }

    # ---- 第四步：统计收益 ----
    print()
    print("[4/5] 统计信号收益...")

    # 按 buy_score 分层
    score_buckets = defaultdict(list)
    for sig in signals:
        score_buckets[sig["buy_score"]].append(sig)

    print()
    print("  buy_score 分层绩效：")
    print(f"  {'Score':<7} {'N':<6} {'D1均值':<10} {'D3均值':<10} {'D5均值':<10} {'D10均值':<10} {'D5胜率':<8}")
    print(f"  {'-'*55}")

    stats_by_score = {}
    for score in sorted(score_buckets.keys()):
        bucket = score_buckets[score]
        stats = {}
        for h in [1, 3, 5, 10]:
            returns = [s["forward_returns"].get(f"D{h}") for s in bucket]
            valid = [r for r in returns if r is not None]
            stats[f"D{h}"] = {
                "mean": round(np.mean(valid), 4) if valid else None,
                "median": round(np.median(valid), 4) if valid else None,
                "win_rate": round(sum(1 for r in valid if r > 0) / len(valid), 3) if valid else None,
                "n": len(valid),
            }
        stats_by_score[score] = stats

        d1_str = f"{stats['D1']['mean']*100:+.2f}%" if stats['D1']['mean'] is not None else "N/A"
        d3_str = f"{stats['D3']['mean']*100:+.2f}%" if stats['D3']['mean'] is not None else "N/A"
        d5_str = f"{stats['D5']['mean']*100:+.2f}%" if stats['D5']['mean'] is not None else "N/A"
        d10_str = f"{stats['D10']['mean']*100:+.2f}%" if stats['D10']['mean'] is not None else "N/A"
        wr_str = f"{stats['D5']['win_rate']*100:.1f}%" if stats['D5']['win_rate'] is not None else "N/A"
        print(f"  {score:<7} {len(bucket):<6} {d1_str:<10} {d3_str:<10} {d5_str:<10} {d10_str:<10} {wr_str:<8}")

    # 全部信号汇总
    print()
    print("  全部信号汇总：")
    all_stats = {}
    for h in [1, 3, 5, 10]:
        returns = [s["forward_returns"].get(f"D{h}") for s in signals]
        valid = [r for r in returns if r is not None]
        all_stats[f"D{h}"] = {
            "mean": round(np.mean(valid), 4) if valid else None,
            "median": round(np.median(valid), 4) if valid else None,
            "win_rate": round(sum(1 for r in valid if r > 0) / len(valid), 3) if valid else None,
            "n": len(valid),
        }
        if valid:
            print(f"  D{h}: 均值={np.mean(valid)*100:+.3f}%, "
                  f"中位数={np.median(valid)*100:+.3f}%, "
                  f"胜率={sum(1 for r in valid if r>0)/len(valid)*100:.1f}%, "
                  f"N={len(valid)}")

    # ---- 第五步：规则有效性统计 ----
    print()
    print("[5/5] 规则有效性...")

    rule_counts = defaultdict(int)
    for sig in signals:
        for rule in sig["rules_hit"]:
            rule_name = rule.split(":")[0] if ":" in rule else rule
            rule_counts[rule_name] += 1

    rule_order = ["R1", "R2", "R3", "R4", "R5"]
    for r in rule_order:
        cnt = rule_counts.get(r, 0)
        pct = cnt / len(signals) * 100 if signals else 0
        print(f"  {r}: {cnt} 次触发 ({pct:.1f}% of signals)")

    # ---- 保存结果 ----
    print()
    print("保存结果...")

    # 信号详情 → signals.jsonl
    signals_path = os.path.join(output_dir, "signals.jsonl")
    with open(signals_path, 'w', encoding='utf-8') as f:
        for sig in signals:
            f.write(json.dumps(sig, ensure_ascii=False) + '\n')
    print(f"  信号详情: {signals_path} ({len(signals)} 条)")

    # 绩效汇总 → performance.json
    perf = {
        "config": {
            "start_date": start_date,
            "end_date": end_date,
            "symbols_count": len(symbols),
            "l1_enabled": l1_enabled,
        },
        "l1_counts": dict(l1_counts),
        "total_signals": len(signals),
        "all_signals": all_stats,
        "by_score": {str(k): v for k, v in stats_by_score.items()},
        "rule_effectiveness": {r: rule_counts.get(r, 0) for r in rule_order},
    }
    perf_path = os.path.join(output_dir, "performance.json")
    with open(perf_path, 'w', encoding='utf-8') as f:
        json.dump(perf, f, ensure_ascii=False, indent=2)
    print(f"  绩效汇总: {perf_path}")

    print()
    print("=" * 50)
    print("  回测完成!")
    print("=" * 50)

    return {
        "signals": signals,
        "performance": perf,
        "l1_states": l1_states,
        "trading_days": trading_days,
        "l1_counts": dict(l1_counts),
        "kline_dict": kline_dict,
    }


# ============================================================
#  入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="A股 SOP 历史回测引擎")
    parser.add_argument("--config", "-c", default="backtest_config.json",
                        help="配置文件路径 (默认: backtest_config.json)")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, args.config) if not os.path.isabs(args.config) else args.config

    if not os.path.exists(config_path):
        print(f"[ERROR] 配置文件不存在: {config_path}")
        sys.exit(1)

    config = load_config(config_path)

    # 处理相对路径
    for key in ["output_dir", "cache_dir"]:
        if not os.path.isabs(config[key]):
            config[key] = os.path.join(script_dir, config[key])

    result = run_backtest(config)

    # 如果发现信号，自动生成报告
    if result.get("signals"):
        try:
            from backtest_report import generate_report
            generate_report(result, config)
        except ImportError:
            print("[INFO] backtest_report.py 未找到，跳过图表生成")


if __name__ == "__main__":
    main()
