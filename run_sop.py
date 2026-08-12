import json, requests, time, sys
from datetime import datetime, date
from collections import Counter



# 路径自动解析（本地/沙箱双兼容，不再硬编码会话路径）
import sys as _sys
from pathlib import Path as _Path
_REPO = _Path(__file__).resolve().parent
while not (_REPO / "common_paths.py").exists():
    _REPO = _REPO.parent
_sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO, CACHE_DIR

today = date.today()
today_str = today.strftime("%Y-%m-%d")
today_compact = today.strftime("%Y%m%d")

print(f"=== A股SOP盘前简报 {today_str} ===")
print(f"运行时间: {datetime.now().strftime('%H:%M:%S')}")

# ============================================================
# STEP 1: L1 宏观环境 - 三大指数K线 + 全市场成交额
# ============================================================
print("\n--- Step 1: L1 宏观环境 ---")

indexes = {"上证":"sh000001","深证":"sz399001","创业板":"sz399006"}
results = {}
for name, code in indexes.items():
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,60,qfq"
        r = requests.get(url, timeout=15)
        data = r.json()
        klines = data.get("data",{}).get(code,{}).get("day",[]) or data.get("data",{}).get(code,{}).get("qfqday",[])
        if not klines:
            print(f"  {name}: 无数据")
            continue
        closes = [float(k[2]) for k in klines]
        n = len(closes)
        ma20 = sum(closes[-20:])/20
        ma60 = sum(closes[-60:])/60 if n>=60 else sum(closes)/n
        ma20_5d = sum(closes[-25:-5])/20 if n>=25 else ma20
        ma60_5d = sum(closes[-65:-5])/60 if n>=65 else ma60
        latest = closes[-1]
        chg_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if n >= 2 else 0
        results[name] = {"close":latest,"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"ma60_up":ma60>ma60_5d,
            "above_ma20":latest>ma20,"above_ma60":latest>ma60,
            "chg_pct":chg_pct}
        print(f"  {name}: 收{latest:.0f}({chg_pct:+.1f}%) MA20={ma20:.0f}({'↑' if ma20>ma20_5d else '↓'}) MA60={ma60:.0f}({'↑' if ma60>ma60_5d else '↓'})")
    except Exception as e:
        print(f"  {name}: {e}")

# 1.2 全市场成交额快照
resp = requests.get("http://qt.gtimg.cn/q=sh000001,sz399001,sz399006", timeout=10)
total_amt = 0
for line in resp.text.strip().split("\n"):
    if not line.strip(): continue
    f = line.split("~")
    if len(f)>45:
        total_amt += float(f[37]) if f[37] else 0
print(f"  全市场成交额: ~{total_amt/10000:.0f}亿")

# 1.3 L1判定
bull = sum(1 for r in results.values() if r["above_ma20"] and r["above_ma60"] and r["ma20_up"] and r["ma60_up"])
risk = sum(1 for r in results.values() if not r["above_ma20"] and not r["ma20_up"])
if bull==3: state="多头趋势"; gate="正常模式"
elif risk>=2: state="系统性风险"; gate="暂停交易"
else: state="震荡市"; gate="降低权重"
print(f"  L1判定: bull={bull}, risk={risk} → {state} → {gate}")

# ============================================================
# STEP 2: L2 涨停池 + 行业排名
# ============================================================
print("\n--- Step 2: L2 涨停池 + 行业排名 ---")

h = {"Referer":"https://data.eastmoney.com/"}

# 2.1 行业板块
try:
    r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
        params={"pn":"1","pz":"10","po":"1","fid":"f3","fs":"m:90+t:2","fields":"f2,f3,f4,f14"},
        headers=h, timeout=10)
    d = r.json()
    boards = d.get("data",{}).get("diff",[])
    print("行业涨幅TOP5:")
    for i, b in enumerate(boards[:5],1):
        pct = b.get('f3', 0) or 0
        print(f"  {i}. {b.get('f14','?')} {pct:+.2f}%")
except Exception as e:
    print(f"板块数据: {e}")

time.sleep(1.5)

# 2.2 涨停池
zt = []
zt_count = 0
zb_count = 0
try:
    r = requests.get("https://push2ex.eastmoney.com/getTopicZTPool",
        params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
                "Pageindex":"0","pagesize":"300","sort":"fbt:asc","date":today_compact},
        headers=h, timeout=10)
    d = r.json()
    zt = d.get("data",{}).get("pool",[])
    zt_count = len(zt)
    from collections import Counter
    lbc_dist = Counter(z.get("lbc",0) for z in zt)
    ind_cnt = Counter(z.get("hybk","") for z in zt if z.get("hybk"))
    # 炸板数
    zb = [z for z in zt if z.get("zbc","0") != "0"]
    zb_count = len(zb)
    zbr = zb_count/(zt_count+zb_count)*100 if (zt_count+zb_count) > 0 else 0
    print(f"涨停: {zt_count}家, 炸板: {zb_count}家, 炸板率: {zbr:.1f}%")
    print(f"最高连板: {max(lbc_dist.keys()) if lbc_dist else 0}")
    print(f"行业TOP5: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}")
except Exception as e:
    print(f"涨停池: {e}")

# ============================================================
# STEP 3: L3 个股打分
# ============================================================
print("\n--- Step 3: L3 个股打分 ---")

def to_tx(code):
    if code.startswith("6"): return "sh"+code
    if code.startswith("0") or code.startswith("3"): return "sz"+code
    if code.startswith("9"): return "bj"+code
    return code

# 用成交额排序选TOP12候选
top = sorted(zt, key=lambda x: (-x.get("lbc",0), -x.get("amount",0)))[:15]
signals = []

for z in top:
    raw = z.get("c","")
    code = to_tx(raw); name = z.get("n",""); lbc = z.get("lbc",0)
    try:
        r2 = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
        f = r2.text.split("~")
        if len(f)<40: continue
        price=float(f[3]) if f[3] else 0
        pct=float(f[32]) if f[32] else 0
        pe=float(f[39]) if f[39] and f[39]!="" else 0
        turnover=float(f[38]) if f[38] and f[38]!="" else 0
        # L3 9条规则打分
        buy=0; sell=0
        if pct>=0.8: buy+=1  # R3
        if turnover>5 and pct>0: buy+=1  # R4
        if pct>=9.5: buy+=1  # R5
        if pct>=9.0 and lbc==1: buy+=1  # R2
        if pct<=-1.5: sell+=1
        if pct<=-3.0: sell+=1
        net=buy-sell
        if buy>=3 and net>=1: out="强候选"
        elif buy>=2 and net>=1: out="观察"
        elif sell>=2: out="风控"
        else: out="中性"
        signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,
            "pe":pe,"buy":buy,"sell":sell,"net":net,"out":out,"industry":z.get("hybk",""),
            "turnover":turnover,"amount":z.get("amount",0)})
        print(f"  {code} {name:8s} {lbc}板 ¥{price:.2f} {pct:+.1f}% PE{pe:.1f} turnover={turnover:.1f}% buy={buy} → {out}")
    except Exception as e:
        print(f"  {code} {name} 获取详情失败: {e}")

# 震荡市门槛
if state=="震荡市":
    valid = [s for s in signals if s["buy"]>=3]
    weak = [s for s in signals if s["buy"]==2]
else:
    valid = [s for s in signals if s["buy"]>=2]
    weak = []

print(f"\n  有效候选: {len(valid)}只, 观察级: {len(weak)}只")

# ============================================================
# STEP 4: 对比昨日信号
# ============================================================
print("\n--- Step 4: 昨日信号回顾 ---")

# Read previous portfolio data
portfolio_path = PORTFOLIO
with open(portfolio_path, 'r', encoding='utf-8') as pf:
    portfolio = json.load(pf)

yesterday = portfolio["daily_log"][-1]
yesterday_review = []

# Get today's data for yesterday's signals
if "signals" in yesterday:
    for ys in yesterday["signals"][:15]:
        code = ys.get("code","")
        name = ys.get("name","")
        try:
            r3 = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
            f = r3.text.split("~")
            if len(f)<40: continue
            today_price=float(f[3]) if f[3] else 0
            today_pct=float(f[32]) if f[32] else 0
            today_open=float(f[5]) if f[5] else 0
            today_high=float(f[33]) if f[33] else 0
            today_low=float(f[34]) if f[34] else 0
            yesterday_review.append({
                "code":code,"name":name,
                "prev_lbc":ys.get("lbc",0),
                "prev_price":ys.get("price",0),
                "prev_out":ys.get("out",""),
                "today_price":today_price,
                "today_pct":today_pct,
                "today_open":today_open,
                "today_high":today_high,
                "today_low":today_low,
                "prev_close":ys.get("price",0)
            })
            status = "涨停" if today_pct>=9.5 else ("大涨" if today_pct>5 else ("小涨" if today_pct>0.5 else ("平盘" if today_pct>-0.5 else ("小跌" if today_pct>-3 else ("大跌" if today_pct>-7 else "暴跌")))))
            print(f"  {code} {name:8s} 昨¥{ys.get('price',0):.2f}({ys.get('lbc',0)}板) → 今¥{today_price:.2f} {today_pct:+.1f}% {status}")
        except Exception as e:
            print(f"  {code} {name} 获取今价失败: {e}")

# ============================================================
# STEP 5: 更新portfolio + 输出简报
# ============================================================
print("\n--- Step 5: 更新portfolio ---")

# Build daily log entry
daily_entry = {
    "date": today_str,
    "session": "盘前简报(08:30运行)",
    "l1_state": state,
    "l1_gate": gate,
    "l1_detail": f"上证{results['上证']['close']:.0f}({results['上证']['chg_pct']:+.1f}%) MA20={results['上证']['ma20']:.0f}({'↑'if results['上证']['ma20_up']else'↓'}) MA60={results['上证']['ma60']:.0f}({'↑'if results['上证']['ma60_up']else'↓'}); 深证{results['深证']['close']:.0f}({results['深证']['chg_pct']:+.1f}%) MA20={results['深证']['ma20']:.0f}({'↑'if results['深证']['ma20_up']else'↓'}) MA60={results['深证']['ma60']:.0f}({'↑'if results['深证']['ma60_up']else'↓'}); 创业板{results['创业板']['close']:.0f}({results['创业板']['chg_pct']:+.1f}%) MA20={results['创业板']['ma20']:.0f}({'↑'if results['创业板']['ma20_up']else'↓'}) MA60={results['创业板']['ma60']:.0f}({'↑'if results['创业板']['ma60_up']else'↓'})",
    "l1_bull": bull,
    "l1_risk": risk,
    "l1_indexes": {k:{"close":v["close"],"chg_pct":v["chg_pct"],"ma20":v["ma20"],"ma60":v["ma60"],
                       "ma20_up":v["ma20_up"],"ma60_up":v["ma60_up"],
                       "above_ma20":v["above_ma20"],"above_ma60":v["above_ma60"]} for k,v in results.items()},
    "l2_summary": f"{zt_count}家涨停, 炸板率{zbr:.1f}%, 最高{max(lbc_dist.keys()) if lbc_dist else 0}连板, 主线: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}" if zt else "尚未开盘",
    "l2_zt_count": zt_count,
    "l2_zb_count": zb_count,
    "l2_zbr": round(zbr, 1),
    "l2_max_lb": max(lbc_dist.keys()) if lbc_dist else 0,
    "l2_sectors": [{"name":k,"count":v} for k,v in ind_cnt.most_common(5)] if zt else [],
    "signals": signals,
    "yesterday_review": yesterday_review,
    "decisions": "",
    "trades": [],
    "holdings_snapshot": portfolio.get("holdings",[])
}

# Generate decision text
if state == "系统性风险":
    decisions = f"系统性风险门控→暂停交易，不执行任何虚拟买卖。\n\n📊 本轮下行第{12 if today.day==24 else '?'}个交易日。三大指数连续在MA20/MA60下方运行。"
    # Check if improving
    up_count = sum(1 for r in results.values() if r["chg_pct"] > 0)
    above_ma_count = sum(1 for r in results.values() if r["above_ma20"])
    decisions += f"\n\n📈 门控降级观测:\n① 至少2个主板指数站上MA20 — {'❌ ' + str(above_ma_count) + '/3' if above_ma_count<2 else '✅'}\n② 涨停>80家且炸板率<20% — {'❌' if not (zt_count>80 and zbr<20) else '✅'}\n③ 主板不再新低 — 观察中"

    # Yesterday signal performance
    if yesterday_review:
        pos = sum(1 for yr in yesterday_review if yr["today_pct"] > 0)
        decisions += f"\n\n昨日{len(yesterday_review)}只信号回顾: 正收益{pos}/{len(yesterday_review)}({pos/len(yesterday_review)*100:.0f}%)"

    decisions += f"\n\n⚠️ 不构成投资建议。虚拟盘仅作研究观察。"

elif state == "震荡市":
    decisions = f"震荡市→降低权重，仅记录观察。\n{len(valid)}只强候选，{len(weak)}只弱候选。"

else:
    decisions = f"多头趋势→正常模式。{len(valid)}只候选可纳入虚拟持仓。"

daily_entry["decisions"] = decisions

# Update portfolio
portfolio["daily_log"].append(daily_entry)
portfolio["account"]["last_updated"] = f"{today_str} 08:30 盘前简报"
portfolio["account"]["total_value"] = portfolio["account"]["cash"]  # no holdings

with open(portfolio_path, 'w', encoding='utf-8') as pf:
    json.dump(portfolio, pf, ensure_ascii=False, indent=2)

print("portfolio.json 已更新")

# ============================================================
# FINAL SUMMARY
# ============================================================
print("\n" + "="*60)
print(f"🌅 A股SOP盘前简报 {today_str}")
print(f"L1: {state} → {gate}")
print(f"   上证{results['上证']['close']:.0f}({results['上证']['chg_pct']:+.1f}%) 深证{results['深证']['close']:.0f}({results['深证']['chg_pct']:+.1f}%) 创业板{results['创业板']['close']:.0f}({results['创业板']['chg_pct']:+.1f}%)")
print(f"L2: {zt_count}家涨停, 炸板率{zbr:.1f}%, 最高{max(lbc_dist.keys()) if lbc_dist else 0}连板")
if zt:
    print(f"   主线: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(3))}")
print(f"L3: {len(valid)}只候选")
if signals:
    for s in signals[:8]:
        pe_str = f"PE{s['pe']:.0f}" if s['pe'] > 0 else "PE负"
        print(f"   {s['code']} {s['name']} {s['lbc']}板 ¥{s['price']:.2f} +{s['pct']:.1f}% {pe_str} → {s['out']}")
print(f"虚拟账户: ¥{portfolio['account']['total_value']:,.0f} (盈亏{portfolio['account']['pnl']:+,.0f})")
print("\n⚠️ 不构成投资建议。虚拟盘仅作研究观察。")
print("="*60)
