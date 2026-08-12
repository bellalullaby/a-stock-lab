import requests, json, time, math, sys
from datetime import datetime, date
from collections import Counter
from pathlib import Path

today = date.today().strftime("%Y-%m-%d")
today_c = today.replace("-","")
h = {"Referer":"https://data.eastmoney.com/"}
print(f"=== A股SOP收盘简报 {today} ===\n")

# L1: 指数复核
print("## L1: 三大指数收盘复核")
indexes = {"上证":"sh000001","深证":"sz399001","创业板":"sz399006"}
l1_results = {}
for name, code in indexes.items():
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,70,qfq"
        r = requests.get(url, timeout=15)
        data = r.json()
        klines = data.get("data",{}).get(code,{}).get("day",[])
        if not klines: continue
        closes = [float(k[2]) for k in klines]
        n = len(closes)
        ma20 = sum(closes[-20:])/20
        ma60 = sum(closes[-60:])/60 if n>=60 else sum(closes)/n
        ma20_5d = sum(closes[-25:-5])/20 if n>=25 else ma20
        ma60_5d = sum(closes[-65:-5])/60 if n>=65 else ma60
        latest = closes[-1]
        prev = closes[-2] if n>=2 else latest
        chg_pct = (latest - prev) / prev * 100
        l1_results[name] = {"close":latest,"chg_pct":round(chg_pct,2),"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"ma60_up":ma60>ma60_5d,
            "above_ma20":latest>ma20,"above_ma60":latest>ma60}
        print(f"  {name}: 收{latest:.2f} ({chg_pct:+.2f}%) MA20={ma20:.2f}({'↑' if ma20>ma20_5d else '↓'}) MA60={ma60:.2f}({'↑' if ma60>ma60_5d else '↓'}) 站上MA20:{latest>ma20} MA60:{latest>ma60}")
    except Exception as e:
        print(f"  {name}: ERROR {e}")

bull = sum(1 for r in l1_results.values() if r["above_ma20"] and r["above_ma60"] and r["ma20_up"] and r["ma60_up"])
risk = sum(1 for r in l1_results.values() if not r["above_ma20"] and not r["ma20_up"])
if bull==3: state="多头趋势"; gate="正常模式"
elif risk>=2: state="系统性风险"; gate="暂停交易"
else: state="震荡市"; gate="降低权重"
print(f"  L1收盘判定: {state} → {gate}")

# L2: 行业板块 + 涨停池
print("\n## L2: 题材 + 涨停+砸板")

# 行业涨幅
try:
    r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
        params={"pn":"1","pz":"10","po":"1","fid":"f3","fs":"m:90+t:2","fields":"f2,f3,f14"},
        headers=h, timeout=10)
    d = r.json()
    boards = d.get("data",{}).get("diff",[])
    print("行业涨幅TOP5:")
    for i, b in enumerate(boards[:5],1):
        print(f"  {i}. {b.get('f14','?')} +{b.get('f3',0) or 0:.2f}%")
except Exception as e:
    print(f"行业板块: {e}")

time.sleep(1.5)

# 涨停池
zt_pool = []
try:
    r = requests.get("https://push2ex.eastmoney.com/getTopicZTPool",
        params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
                "Pageindex":"0","pagesize":"300","sort":"fbt:asc","date":today_c},
        headers=h, timeout=10)
    d = r.json()
    zt_pool = d.get("data",{}).get("pool",[])
    lbc_dist = Counter(z.get("lbc",0) for z in zt_pool)
    ind_cnt = Counter(z.get("hybk","") for z in zt_pool if z.get("hybk"))
    zt_count = len(zt_pool)
    max_lb = max(lbc_dist.keys()) if lbc_dist else 0
    lb2plus = sum(v for k,v in lbc_dist.items() if k>=2)
    print(f"涨停: {zt_count}家  最高连板: {max_lb}  2板+: {lb2plus}家")
    print(f"行业TOP5: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}")
    print(f"连板分布: {dict(sorted(lbc_dist.items()))}")
except Exception as e:
    print(f"涨停池: {e}")

# 炸板池
zb_count = 0
try:
    time.sleep(1.5)
    r = requests.get("https://push2ex.eastmoney.com/getTopicZBPool",
        params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
                "Pageindex":"0","pagesize":"300","sort":"fbt:asc","date":today_c},
        headers=h, timeout=10)
    zb_count = len(r.json().get("data",{}).get("pool",[]))
    zbr = zb_count/(zt_count+zb_count)*100 if zt_count+zb_count>0 else 0
    print(f"炸板: {zb_count}家  炸板率: {zbr:.1f}%")
except Exception as e:
    print(f"炸板池: {e}")
time.sleep(1)

# L3: 个股打分
print("\n## L3: 连板龙头个股打分 (Top 15)")
def to_tx(code):
    if code.startswith("6"): return "sh"+code
    if code.startswith("0") or code.startswith("3"): return "sz"+code
    if code.startswith("9") or code.startswith("4") or code.startswith("8"): return "bj"+code
    return code

top = sorted(zt_pool, key=lambda x: (-x.get("lbc",0), -x.get("amount",0)))[:15]
signals = []

for z in top:
    raw = z.get("c",""); code = to_tx(raw); name = z.get("n",""); lbc = z.get("lbc",0)
    try:
        r2 = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
        f = r2.text.split("~")
        if len(f)<40: continue
        price = float(f[3]) if f[3] else 0
        pct = float(f[32]) if f[32] else 0
        pe = float(f[39]) if f[39] and f[39]!="" else 0
        turnover = float(f[38]) if f[38] and f[38]!="" else 0
        cap = float(f[45]) if f[45] and f[45]!="" else 0
        buy = 0; sell = 0; hits = []
        if pct >= 0.8: buy += 1; hits.append("R3")
        if turnover > 5 and pct > 0: buy += 1; hits.append("R4")
        if pct >= 9.5: buy += 1; hits.append("R5")
        if pct >= 9.0 and lbc == 1: buy += 1; hits.append("R2")
        if pct <= -1.5: sell += 1
        if pct <= -3.0: sell += 1
        net = buy - sell
        if buy >= 3 and net >= 1: out = "强候选"
        elif buy >= 2 and net >= 1: out = "观察"
        elif sell >= 2: out = "风控"
        else: out = "中性"
        cap_yi = cap/10000 if cap else 0
        signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,
            "pe":pe,"buy":buy,"sell":sell,"net":net,"out":out,
            "industry":z.get("hybk",""),"cap_yi":cap_yi,"hits":hits})
        print(f"  {code} {name:8s} {lbc}板 ¥{price:.2f} +{pct:.1f}% PE{pe:.1f} 市值{cap_yi:.0f}亿 buy={buy} {out}")
    except Exception as e:
        print(f"  {code} {name}: {e}")

# L4: 对比盘前信号
print("\n## L4: 盘前vs收盘对比")

# Read portfolio
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from common_paths import PORTFOLIO
pf_path = str(PORTFOLIO)
with open(pf_path, 'r', encoding='utf-8') as f:
    pf = json.load(f)

# Find today's 盘前 signals
prequel_signals = []
for log in pf["daily_log"]:
    if log.get("date") == today and "盘前" in str(log.get("session","")):
        prequel_signals = log.get("signals", [])
        break

review_results = []
if prequel_signals:
    print(f"盘前信号: {len(prequel_signals)}只")
    for ps in prequel_signals:
        code = ps["code"]
        try:
            r2 = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
            f = r2.text.split("~")
            if len(f)<40: continue
            price = float(f[3]) if f[3] else 0
            prev_close = float(f[4]) if f[4] else 0
            open_p = float(f[5]) if f[5] else 0
            high = float(f[33]) if f[33] else 0
            low = float(f[34]) if f[34] else 0
            chg_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
            y_price = ps.get("price",0)
            y_pct = ps.get("pct",0)

            # Determine status
            if chg_pct >= 9.5: status = "涨停"
            elif chg_pct >= 5: status = "大涨"
            elif chg_pct >= 1: status = "小涨"
            elif chg_pct >= -1: status = "横盘"
            elif chg_pct >= -5: status = "小跌"
            elif chg_pct >= -10: status = "大跌"
            else: status = "跌停"

            review_results.append({
                "code": code, "name": ps["name"],
                "lbc": ps.get("lbc",0),
                "yesterday_price": y_price,
                "today_price": price, "today_pct": round(chg_pct,2),
                "today_open": open_p, "today_high": high, "today_low": low,
                "status": status
            })
            print(f"  {code} {ps['name']:8s} 盘前¥{y_price:.2f} → 收盘¥{price:.2f} ({chg_pct:+.2f}%) {status}")
        except Exception as e:
            print(f"  {code} {ps['name']}: {e}")
else:
    print("无盘前信号(非盘前简报运行)")

# Compare with yesterday's 盘前 from prior days - get the last closing log
prev_close_log = None
for log in reversed(pf["daily_log"]):
    if log.get("date") != today and "盘前" not in str(log.get("session","")):
        prev_close_log = log
        break

# Holdings
holdings = pf.get("holdings", [])
print(f"\n当前持仓: {len(holdings)}只")

# Today's signals count
strong = sum(1 for s in signals if s["out"]=="强候选")
watch = sum(1 for s in signals if s["out"]=="观察")
neutral = sum(1 for s in signals if s["out"]=="中性")
wind_ctrl = sum(1 for s in signals if s["out"]=="风控")
print(f"今日信号: {strong}只强候选, {watch}只观察, {neutral}只中性, {wind_ctrl}只风控")

# Virtual trading decisions
buys = []; sells_holdings = []

if gate == "暂停交易":
    print("\n🚫 门控=暂停交易，不执行虚拟买卖")
    trade_note = "门控禁止开仓"
elif gate == "降低权重":
    print("\n⚠️ 门控=降低权重，仅小仓位试探")
    trade_note = "降低权重，不开新仓"
else:
    print("\n✅ 门控=正常模式")
    txn = []
    for s in signals:
        if s["out"] == "强候选" and len(buys) < 5:
            buys.append(s)
        if len(buys) >= 5: break
    trade_note = f"正常模式，买入{len(buys)}只"

# Print final summary
print(f"\n{'='*60}")
print(f"📊 A股SOP收盘简报 {today}")
print(f"{'='*60}")
print(f"L1: {state} → {gate}")
for name in ["上证","深证","创业板"]:
    r = l1_results.get(name,{})
    print(f"    {name} {r.get('close','?'):.2f} MA20={r.get('ma20','?'):.2f} MA60={r.get('ma60','?'):.2f}")
print(f"L2: {zt_count}家涨停 炸板率{zbr:.1f}% 最高{max_lb}连板 主线{ind_cnt.most_common(3)}")
print(f"L3: {strong}只强候选, {watch}只观察")
print(f"虚拟交易: 买入{len(buys)}只 / 卖出{len(sells_holdings)}只")
print(f"账户: ¥{pf['account']['total_value']:,.0f} ({pf['account']['pnl_pct']:+.2f}%)")
print(f"交易决策: {trade_note}")

# Save signals for portfolio update
result_dict = {
    "l1_results": {k: {kk: (round(vv,2) if isinstance(vv,float) else vv) for kk,vv in v.items()} for k,v in l1_results.items()},
    "l1_state": state,
    "l1_gate": gate,
    "zt_count": zt_count,
    "zb_count": zb_count,
    "zbr": round(zbr,1),
    "max_lb": max_lb,
    "lb2plus": lb2plus,
    "lbc_dist": dict(sorted(lbc_dist.items())),
    "top_industries": ind_cnt.most_common(5),
    "top_boards": str(boards[:5]) if boards else "N/A",
    "signals": signals,
    "review_results": review_results,
    "strong_count": strong,
    "watch_count": watch,
    "trade_note": trade_note,
    "buys": len(buys),
    "sells": len(sells_holdings)
}

# Output JSON
print("\n__RESULT_JSON__")
print(json.dumps(result_dict, ensure_ascii=False, default=str))
