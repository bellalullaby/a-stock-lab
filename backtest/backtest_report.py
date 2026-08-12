# -*- coding: utf-8 -*-
"""
A股 SOP 回测报告生成器
======================
从 signals.jsonl 读取信号，计算绩效指标并生成图表。

用法：
    python backtest_report.py                         # 读取默认 output/signals.jsonl
    python backtest_report.py --signals output/signals.jsonl
"""

import sys, io, os, json, argparse
from collections import defaultdict
from datetime import datetime

# 安全地设置 UTF-8 输出（避免重复包装报错）
if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except (ValueError, AttributeError):
        pass

import numpy as np
import pandas as pd

# 尝试导入绘图库
try:
    import matplotlib
    matplotlib.use('Agg')  # 无 GUI 后端
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("[WARN] matplotlib 未安装，跳过图表生成。安装: pip install matplotlib")


# ============================================================
#  绩效指标计算
# ============================================================

def compute_sharpe(daily_returns: list, risk_free: float = 0.02) -> float | None:
    """年化夏普比率"""
    if len(daily_returns) < 2:
        return None
    arr = np.array(daily_returns)
    mean = np.mean(arr)
    std = np.std(arr, ddof=1)
    if std == 0:
        return None
    return float((mean - risk_free / 252) / std * np.sqrt(252))


def compute_max_drawdown(equity_curve: list) -> float | None:
    """最大回撤"""
    if not equity_curve:
        return None
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    return float(max_dd)


def compute_profit_factor(signals: list, horizon: str = "D5") -> float | None:
    """盈亏比：总盈利 / 总亏损"""
    gains = []
    losses = []
    for s in signals:
        r = s.get("forward_returns", {}).get(horizon)
        if r is not None:
            if r > 0:
                gains.append(r)
            elif r < 0:
                losses.append(abs(r))
    if not losses or sum(losses) == 0:
        return float('inf') if gains else None
    return float(sum(gains) / sum(losses))


# ============================================================
#  简单投资组合模拟
# ============================================================

def simulate_portfolio(signals: list, kline_dict: dict, trading_days: list,
                        config: dict) -> dict:
    """
    基于信号做简单的投资组合模拟。
    - 每次买入用 position_size_pct 的初始资金
    - 最多同时持有 max_positions 个
    - 持仓 hold_days 天后卖出（或 sell_score >= 2 提前止损）
    - 扣除佣金（买卖双向）和印花税（仅卖出）
    """
    capital = config["initial_capital"]
    position_pct = config["position_size_pct"]
    max_pos = config["max_positions"]
    hold_days = config["hold_days"]
    commission = config["commission"]
    stamp_tax = config["stamp_tax"]

    positions = []        # 当前持仓
    closed_trades = []    # 已平仓交易
    equity_curve = []     # 每日权益 {(date, value)}

    # 构建日期→信号映射
    signals_by_date = defaultdict(list)
    for sig in signals:
        signals_by_date[sig["date"]].append(sig)

    for i, date in enumerate(trading_days):
        # --- 检查持仓是否需要平仓 ---
        for pos in positions[:]:
            days_held = i - pos["entry_idx"]

            # 获取当日该股票数据
            tx = pos["tx_code"]
            stock_dict = kline_dict.get(tx, {})
            bar = stock_dict.get(date)

            exit_price = bar["close"] if bar else pos["entry_price"]
            exit_reason = None

            # 计算当前浮亏
            current_return = (exit_price - pos["entry_price"]) / pos["entry_price"] if bar else 0

            if days_held >= hold_days:
                exit_reason = "到期"
            elif config.get("stop_loss_pct") and current_return <= config["stop_loss_pct"]:
                exit_reason = f"止损({current_return*100:.1f}%)"
            elif bar:
                # 检查 sell_score
                from backtest_runner import compute_l3_scores
                scores = compute_l3_scores(stock_dict, trading_days, i)
                if scores and scores["sell"] >= 2:
                    exit_reason = "卖出信号"

            if exit_reason:
                # 平仓
                gross_return = (exit_price - pos["entry_price"]) / pos["entry_price"]
                # 扣除成本：买入佣金 + 卖出佣金 + 印花税
                cost = commission * 2 + stamp_tax
                net_return = gross_return - cost
                pnl = pos["capital_used"] * net_return

                closed_trades.append({
                    "symbol": pos["symbol"],
                    "entry_date": trading_days[pos["entry_idx"]],
                    "exit_date": date,
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "days_held": days_held,
                    "exit_reason": exit_reason,
                    "gross_return": round(gross_return, 6),
                    "net_return": round(net_return, 6),
                    "pnl": round(pnl, 2),
                })

                capital += pos["capital_used"] + pnl  # 回收本金+盈亏
                positions.remove(pos)

        # --- 检查新信号 ---
        day_signals = signals_by_date.get(date, [])
        # 按 buy_score 降序排列（优先买入高分信号）
        day_signals.sort(key=lambda s: s["buy_score"], reverse=True)

        for sig in day_signals:
            if len(positions) >= max_pos:
                break

            # 检查是否已经持有
            if any(p["symbol"] == sig["symbol"] for p in positions):
                continue

            # 检查是否在同一天刚平仓（避免重复交易）
            # （简化处理，实际中当天平仓后可以再开仓）

            # 开仓
            position_capital = config["initial_capital"] * position_pct
            if position_capital > capital * 0.8:  # 保留至少20%现金
                continue

            positions.append({
                "symbol": sig["symbol"],
                "tx_code": f"sh{sig['symbol']}" if sig['symbol'].startswith(('6','9')) else f"sz{sig['symbol']}",
                "entry_idx": i,
                "entry_price": sig["price"],
                "capital_used": position_capital,
            })

            capital -= position_capital

        # --- 记录当日权益 ---
        total_equity = capital
        for pos in positions:
            tx = pos["tx_code"]
            bar = kline_dict.get(tx, {}).get(date)
            if bar:
                pos_value = pos["capital_used"] * (bar["close"] / pos["entry_price"])
                total_equity += pos_value
            else:
                total_equity += pos["capital_used"]  # 停牌按原值

        equity_curve.append({"date": date, "equity": round(total_equity, 2)})

    # 计算指标
    returns = []
    for i in range(1, len(equity_curve)):
        r = (equity_curve[i]["equity"] - equity_curve[i-1]["equity"]) / equity_curve[i-1]["equity"]
        returns.append(r)

    total_return = (equity_curve[-1]["equity"] - config["initial_capital"]) / config["initial_capital"] if equity_curve else 0
    n_days = len(equity_curve)
    annual_return = (1 + total_return) ** (252 / n_days) - 1 if n_days > 0 else 0

    return {
        "initial_capital": config["initial_capital"],
        "final_equity": equity_curve[-1]["equity"] if equity_curve else config["initial_capital"],
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "sharpe": compute_sharpe(returns),
        "max_drawdown": compute_max_drawdown([e["equity"] for e in equity_curve]),
        "total_trades": len(closed_trades),
        "winning_trades": sum(1 for t in closed_trades if t["net_return"] > 0),
        "win_rate": round(sum(1 for t in closed_trades if t["net_return"] > 0) / len(closed_trades), 3) if closed_trades else None,
        "profit_factor": compute_profit_factor_from_trades(closed_trades),
        "avg_return_per_trade": round(np.mean([t["net_return"] for t in closed_trades]), 4) if closed_trades else None,
        "equity_curve": equity_curve,
        "closed_trades": closed_trades,
    }


def compute_profit_factor_from_trades(trades: list) -> float | None:
    gains = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return round(gains / losses, 2) if losses > 0 else None


# ============================================================
#  图表生成
# ============================================================

def plot_equity_curve(portfolio: dict, benchmark_equity: list, output_dir: str):
    """策略 vs 基准收益曲线"""
    if not HAS_MPL:
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    # 策略收益曲线
    eq = portfolio["equity_curve"]
    dates = [e["date"] for e in eq]
    values = [e["equity"] / portfolio["initial_capital"] for e in eq]
    ax.plot(range(len(dates)), values, 'b-', linewidth=1.5, label='SOP Strategy')

    # 基准收益曲线
    if benchmark_equity:
        bm_dates = [e["date"] for e in benchmark_equity]
        bm_values = [e["equity"] / benchmark_equity[0]["equity"] if benchmark_equity else 1 for e in benchmark_equity]
        # 对齐长度
        ax.plot(range(len(bm_values)), bm_values, 'gray', linewidth=1, alpha=0.6, label='CSI 300')

    ax.axhline(y=1.0, color='black', linestyle='--', linewidth=0.5, alpha=0.3)
    ax.set_xlabel('Trading Days')
    ax.set_ylabel('Net Value')
    ax.set_title('Equity Curve: SOP Strategy vs CSI 300')
    ax.legend(loc='upper left')
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x:.2f}'))
    ax.grid(True, alpha=0.3)

    # 标注最大回撤区间
    peak = 0
    dd_start = 0
    for i, v in enumerate(values):
        if v > peak:
            peak = v
            dd_start = i
    max_dd = portfolio.get("max_drawdown", 0)
    if max_dd and max_dd > 0.02:
        ax.annotate(f'Max DD: {max_dd*100:.1f}%',
                    xy=(dd_start, values[dd_start]),
                    fontsize=8, color='red', alpha=0.7)

    plt.tight_layout()
    path = os.path.join(output_dir, "charts", "equity_curve.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  收益曲线: {path}")


def plot_monthly_returns(signals: list, output_dir: str):
    """月度收益热力图"""
    if not HAS_MPL or not signals:
        return

    # 按月份汇总信号收益
    monthly = defaultdict(list)
    for s in signals:
        month = s["date"][:7]  # "2026-01"
        r = s.get("forward_returns", {}).get("D5")
        if r is not None:
            monthly[month].append(r)

    if not monthly:
        return

    months = sorted(monthly.keys())
    avg_returns = [np.mean(monthly[m]) * 100 for m in months]
    counts = [len(monthly[m]) for m in months]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={'height_ratios': [3, 1]})

    # 柱状图：月度平均 D5 收益
    colors = ['#e74c3c' if r < 0 else '#2ecc71' for r in avg_returns]
    bars = ax1.bar(months, avg_returns, color=colors, alpha=0.85, edgecolor='white')
    ax1.axhline(y=0, color='black', linewidth=0.5)
    ax1.set_ylabel('Avg D5 Return (%)')
    ax1.set_title('Monthly Average D5 Return by Signal')
    for bar, val in zip(bars, avg_returns):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
                 f'{val:+.2f}%', ha='center', fontsize=9, fontweight='bold')
    ax1.grid(True, alpha=0.2, axis='y')

    # 信号数量
    ax2.bar(months, counts, color='#3498db', alpha=0.7, edgecolor='white')
    ax2.set_ylabel('Signal Count')
    ax2.set_xlabel('Month')
    for bar, cnt in zip(ax2.containers[0] if ax2.containers else [], counts):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                 str(cnt), ha='center', fontsize=8)
    ax2.grid(True, alpha=0.2, axis='y')

    plt.tight_layout()
    path = os.path.join(output_dir, "charts", "monthly_returns.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  月度收益: {path}")


def plot_score_breakdown(stats_by_score: dict, output_dir: str):
    """buy_score 分层绩效柱状图"""
    if not HAS_MPL or not stats_by_score:
        return

    scores = sorted(stats_by_score.keys(), key=lambda x: int(x))
    horizons = ["D1", "D3", "D5", "D10"]
    colors = ["#f1c40f", "#e67e22", "#e74c3c", "#9b59b6"]

    fig, ax = plt.subplots(figsize=(10, 6))

    x = np.arange(len(scores))
    width = 0.2

    for i, (h, c) in enumerate(zip(horizons, colors)):
        means = []
        for s in scores:
            m = stats_by_score[s].get(h, {}).get("mean")
            means.append(m * 100 if m is not None else 0)
        offset = (i - len(horizons)/2 + 0.5) * width
        ax.bar(x + offset, means, width, label=h, color=c, alpha=0.85, edgecolor='white')

    ax.set_xticks(x)
    ax.set_xticklabels([f"Score={s}" for s in scores])
    ax.axhline(y=0, color='black', linewidth=0.5)
    ax.set_ylabel('Mean Return (%)')
    ax.set_title('Forward Returns by buy_score')
    ax.legend()
    ax.grid(True, alpha=0.2, axis='y')

    plt.tight_layout()
    path = os.path.join(output_dir, "charts", "score_breakdown.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  分层绩效: {path}")


def plot_signal_density(signals: list, trading_days: list, output_dir: str):
    """信号密度分布"""
    if not HAS_MPL or not signals:
        return

    # 统计每日信号数
    signal_dates = defaultdict(int)
    for s in signals:
        signal_dates[s["date"]] += 1

    dates_in_range = sorted(set(s["date"] for s in signals))
    if not dates_in_range:
        return

    counts = [signal_dates.get(d, 0) for d in dates_in_range]

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.fill_between(range(len(dates_in_range)), counts, alpha=0.3, color='#3498db')
    ax.plot(range(len(dates_in_range)), counts, 'b-', linewidth=0.8, alpha=0.8)

    # 标注高信号日
    threshold = np.percentile(counts, 90) if counts else 0
    for i, cnt in enumerate(counts):
        if cnt > threshold and cnt > 0:
            ax.annotate(dates_in_range[i], (i, cnt),
                        fontsize=6, rotation=90, ha='center', va='bottom', alpha=0.6)

    ax.set_xlabel('Trading Days')
    ax.set_ylabel('Signal Count')
    ax.set_title('Daily Signal Density')
    ax.grid(True, alpha=0.2)

    plt.tight_layout()
    path = os.path.join(output_dir, "charts", "signal_density.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  信号密度: {path}")


# ============================================================
#  汇总报告
# ============================================================

def generate_report(result: dict, config: dict):
    """
    主入口：从回测结果生成完整报告。
    result: run_backtest() 的返回值
    config: 配置字典
    """
    signals = result.get("signals", [])
    output_dir = config["output_dir"]
    l1_counts = result.get("l1_counts", {})

    if not signals:
        print("[WARN] 无信号，跳过报告生成")
        return

    print()
    print("=" * 50)
    print("  绩效报告")
    print("=" * 50)

    # --- 信号层面统计 ---
    print()
    print("【信号统计】")

    # D5 收益分布
    d5_returns = [s["forward_returns"].get("D5") for s in signals]
    d5_valid = [r for r in d5_returns if r is not None]

    if d5_valid:
        print(f"  总信号数: {len(signals)}")
        print(f"  D5 平均收益: {np.mean(d5_valid)*100:+.3f}%")
        print(f"  D5 中位数收益: {np.median(d5_valid)*100:+.3f}%")
        print(f"  D5 胜率: {sum(1 for r in d5_valid if r>0)/len(d5_valid)*100:.1f}%")
        print(f"  盈亏比: {compute_profit_factor(signals, 'D5') or 'N/A'}")
        print(f"  最大单笔D5盈利: {max(d5_valid)*100:+.2f}%")
        print(f"  最大单笔D5亏损: {min(d5_valid)*100:+.2f}%")

    # --- L1 状态分布 ---
    print()
    print("【L1 环境分布】")
    total_days = sum(l1_counts.values())
    for state, cnt in sorted(l1_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {state}: {cnt} 天 ({cnt/total_days*100:.1f}%)" if total_days > 0 else f"  {state}: {cnt}")

    # 按 L1 状态分组的信号绩效
    print()
    print("【L1 状态分层】")
    l1_signal_stats = defaultdict(list)
    for s in signals:
        l1_signal_stats[s["l1_state"]].append(s)

    for state in ["多头趋势", "震荡市", "系统性风险"]:
        ss = l1_signal_stats.get(state, [])
        if ss:
            d5s = [s["forward_returns"].get("D5") for s in ss]
            d5v = [r for r in d5s if r is not None]
            if d5v:
                print(f"  {state}: {len(ss)} 信号, D5均值={np.mean(d5v)*100:+.3f}%, "
                      f"胜率={sum(1 for r in d5v if r>0)/len(d5v)*100:.1f}%")
            else:
                print(f"  {state}: {len(ss)} 信号 (D5数据不足)")
        else:
            print(f"  {state}: 0 信号")

    # --- 生成图表 ---
    if HAS_MPL:
        print()
        print("【生成图表】")

        # 设置中文字体
        plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False

        # 1. 收益曲线（需要投资组合模拟）
        portfolio = result.get("portfolio")
        benchmark = result.get("benchmark_equity", [])

        if portfolio is None:
            # 简单投资组合模拟
            try:
                kline_dict = result.get("kline_dict", {})
                trading_days = result.get("trading_days", [])
                portfolio = simulate_portfolio(signals, kline_dict, trading_days, config)
                result["portfolio"] = portfolio

                print()
                print("【投资组合模拟】")
                print(f"  初始资金: ¥{portfolio['initial_capital']:,.0f}")
                print(f"  最终权益: ¥{portfolio['final_equity']:,.0f}")
                print(f"  总收益率: {portfolio['total_return']*100:+.2f}%")
                print(f"  年化收益: {portfolio['annual_return']*100:+.2f}%")
                print(f"  夏普比率: {portfolio['sharpe']:.2f}" if portfolio['sharpe'] else "  夏普比率: N/A")
                print(f"  最大回撤: {portfolio['max_drawdown']*100:.2f}%" if portfolio['max_drawdown'] else "  最大回撤: N/A")
                print(f"  总交易: {portfolio['total_trades']} 笔")
                print(f"  胜率: {portfolio['win_rate']*100:.1f}%" if portfolio['win_rate'] else "  胜率: N/A")
                print(f"  盈亏比: {portfolio['profit_factor']}" if portfolio['profit_factor'] else "  盈亏比: N/A")
            except Exception as e:
                print(f"  [WARN] 投资组合模拟失败: {e}")
                portfolio = {"equity_curve": []}

        plot_equity_curve(portfolio or {"equity_curve": [], "initial_capital": 1000000},
                          benchmark, output_dir)
        plot_monthly_returns(signals, output_dir)
        plot_signal_density(signals, result.get("trading_days", []), output_dir)

        perf = result.get("performance", {})
        stats = perf.get("by_score", {})
        plot_score_breakdown(stats, output_dir)

    print()
    print("=" * 50)
    print("  报告生成完毕!")
    print(f"  输出目录: {os.path.abspath(output_dir)}")
    print("=" * 50)


# ============================================================
#  独立运行
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="A股 SOP 回测报告生成器")
    parser.add_argument("--signals", "-s", default="./output/signals.jsonl",
                        help="信号文件路径 (默认: ./output/signals.jsonl)")
    parser.add_argument("--performance", "-p", default="./output/performance.json",
                        help="绩效文件路径 (默认: ./output/performance.json)")
    parser.add_argument("--output", "-o", default="./output",
                        help="输出目录")
    args = parser.parse_args()

    if not os.path.exists(args.signals):
        print(f"[ERROR] 信号文件不存在: {args.signals}")
        sys.exit(1)

    # 加载信号
    signals = []
    with open(args.signals, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                signals.append(json.loads(line))

    print(f"加载 {len(signals)} 条信号")

    # 加载绩效
    perf = {}
    if os.path.exists(args.performance):
        with open(args.performance, 'r', encoding='utf-8') as f:
            perf = json.load(f)

    # 组装 result 字典
    result = {
        "signals": signals,
        "performance": perf,
        "l1_counts": {},
        "trading_days": [],
        "kline_dict": {},
    }

    config = {
        "initial_capital": 1_000_000,
        "position_size_pct": 0.1,
        "max_positions": 5,
        "hold_days": 5,
        "commission": 0.0003,
        "stamp_tax": 0.001,
        "output_dir": args.output,
    }

    generate_report(result, config)


if __name__ == "__main__":
    main()
