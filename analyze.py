"""
A股个股分析工具 — 日K线 + 实时估值
用法: python analyze.py 603377                  # 默认今年至今
      python analyze.py 600519 2025-01-01       # 指定起始日
      python analyze.py 002463 2026-03-01 2026-07-24  # 指定区间
"""
import urllib.request, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ── 参数解析 ───────────────────────────────────────────
if len(sys.argv) < 2:
    print("用法: python analyze.py <股票代码> [起始日] [结束日]")
    print("示例: python analyze.py 600519")
    print("      python analyze.py 603377 2026-01-01")
    sys.exit(1)

code = sys.argv[1].replace("SH","").replace("SZ","").replace("BJ","").replace("sh","").replace("sz","").replace("bj","")
start_date = sys.argv[2] if len(sys.argv) > 2 else "2026-01-01"
end_date   = sys.argv[3] if len(sys.argv) > 3 else "2099-12-31"

# ── 市场前缀 ───────────────────────────────────────────
if code.startswith(("6", "9")):
    prefix = "sh"
elif code.startswith("8"):
    prefix = "bj"
else:
    prefix = "sz"

# ── 1. 实时行情（拿名字+估值）─────────────────────────
try:
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    vals = urllib.request.urlopen(req, timeout=10).read().decode("gbk").split('"')[1].split("~")
    stock_name = vals[1]
    cur_price  = float(vals[3]) if vals[3] else 0
    pe_ttm     = float(vals[39]) if vals[39] else 0
    pb         = float(vals[46]) if vals[46] else 0
    mcap       = float(vals[44]) if vals[44] else 0
    turnover   = float(vals[38]) if vals[38] else 0
except:
    stock_name = f"股票{code}"
    cur_price = pe_ttm = pb = mcap = turnover = 0

# ── 2. 前复权日K线 ────────────────────────────────────
url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,300,qfq"
req = urllib.request.Request(url)
req.add_header("User-Agent", "Mozilla/5.0")
req.add_header("Referer", "https://gu.qq.com/")
data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))

klines = data.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday", [])
if not klines:
    klines = data.get("data", {}).get(f"{prefix}{code}", {}).get("day", [])

if not klines:
    print(f"❌ 未找到 {code} 的K线数据，请检查代码")
    sys.exit(1)

filtered = [k for k in klines if start_date <= k[0] <= end_date]
print(f"📊 {code} {stock_name} · K线总数: {len(klines)}, 筛选区间: {len(filtered)} 根")

if not filtered:
    print(f"⚠️ {start_date}~{end_date} 区间无数据")
    print("最近10根K线:")
    for k in klines[-10:]:
        print(f"  {k[0]}: O={k[1]} C={k[2]} H={k[3]} L={k[4]} V={k[5]}")
    sys.exit()

# ── 3. 计算关键指标 ────────────────────────────────────
opens  = [float(k[1]) for k in filtered]
closes = [float(k[2]) for k in filtered]
highs  = [float(k[3]) for k in filtered]
lows   = [float(k[4]) for k in filtered]
vols   = [float(k[5]) for k in filtered]

first_close = closes[0]
last_close  = closes[-1]
period_chg  = (last_close / first_close - 1) * 100
period_high = max(highs)
period_low  = min(lows)
high_date   = [k[0] for k in filtered if float(k[3]) == period_high][0]
low_date    = [k[0] for k in filtered if float(k[4]) == period_low][0]
avg_vol     = sum(vols) / len(vols)

# 涨跌统计
up_days   = sum(1 for i in range(len(closes)) if closes[i] >= opens[i])
down_days = len(closes) - up_days
win_rate  = up_days / len(closes) * 100

# 最大连续涨/跌
max_up = max_down = cur_up = cur_down = 0
for i in range(len(closes)):
    if closes[i] >= opens[i]:
        cur_up += 1; cur_down = 0
        max_up = max(max_up, cur_up)
    else:
        cur_down += 1; cur_up = 0
        max_down = max(max_down, cur_down)

# 最大回撤
peak = closes[0]
max_dd = 0; dd_date = filtered[0][0]
for i, c in enumerate(closes):
    if c > peak: peak = c
    dd = (c - peak) / peak * 100
    if dd < max_dd: max_dd = dd; dd_date = filtered[i][0]

# 月度表现
monthly = {}
for k in filtered:
    m = k[0][:7]
    if m not in monthly:
        monthly[m] = {"open": float(k[1]), "close": float(k[2]), "high": float(k[3]), "low": float(k[4])}
    monthly[m]["close"] = float(k[2])
    monthly[m]["high"] = max(monthly[m]["high"], float(k[3]))
    monthly[m]["low"]  = min(monthly[m]["low"], float(k[4]))

# ── 4. 输出 ────────────────────────────────────────────
print(f"""
{"="*58}
  📊 {code} {stock_name} · 个股分析
{"="*58}
  现价: ¥{cur_price:.2f}    PE(TTM): {pe_ttm:.1f}    PB: {pb:.1f}
  总市值: {mcap:.1f}亿    换手率: {turnover:.2f}%
  ─────────────────────────────────────────
  区间: {filtered[0][0]} → {filtered[-1][0]} ({len(filtered)}个交易日)
  涨跌幅: {period_chg:+.2f}%
  最高: ¥{period_high:.2f} ({high_date})
  最低: ¥{period_low:.2f} ({low_date})
  最大回撤: {max_dd:.2f}% (至 {dd_date})
  日均成交: {avg_vol/100:.0f}手
  阳线: {up_days}天  阴线: {down_days}天  胜率: {win_rate:.1f}%
  最长连阳: {max_up}天  最长连阴: {max_down}天""")

print(f"\n  📅 月度表现")
print(f"  {'月份':<10} {'开盘':>8} {'收盘':>8} {'涨跌幅':>8} {'最高':>8} {'最低':>8}")
print(f"  {'-'*50}")
for m in sorted(monthly.keys()):
    d = monthly[m]
    chg = (d["close"] / d["open"] - 1) * 100
    bar = "🟢" if chg > 0 else "🔴" if chg < -5 else "🟡" if chg < 0 else "⚪"
    print(f"  {bar} {m:<7} {d['open']:>8.2f} {d['close']:>8.2f} {chg:>7.2f}% {d['high']:>8.2f} {d['low']:>8.2f}")

print(f"\n  📋 最近10个交易日")
print(f"  {'日期':<12} {'开盘':>8} {'收盘':>8} {'最高':>8} {'最低':>8} {'成交量':>10} {'涨跌':>8}")
for k in filtered[-10:]:
    op, cl, hi, lo, vol = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
    chg = (cl / op - 1) * 100
    print(f"  {k[0]:<12} {op:>8.2f} {cl:>8.2f} {hi:>8.2f} {lo:>8.2f} {vol/100:>8.0f}万 {chg:>7.2f}%")
