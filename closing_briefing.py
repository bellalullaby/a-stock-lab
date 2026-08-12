import requests, json, time
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

today = date.today().strftime("%Y-%m-%d")
today_c = today.replace("-","")
h = {"Referer":"https://data.eastmoney.com/"}
print(f"=== A股SOP收盘简报 {today} ===")

# ========== Step 1: L1 收盘复核 ==========
print("\n--- L1 指数复核 ---")
indexes = {"上证":"sh000001","深证":"sz399001","创业板":"sz399006"}
results = {}
for name, code in indexes.items():
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,80,qfq"
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
        results[name] = {"close":latest,"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"ma60_up":ma60>ma60_5d,
            "above_ma20":latest>ma20,"above_ma60":latest>ma60}
        print(f"  {name}: 收{latest:.0f} MA20={ma20:.0f}({'↑' if ma20>ma20_5d else '↓'}) MA60={ma60:.0f}({'↑' if ma60>ma60_5d else '↓'}) 站上MA20:{latest>ma20} MA60:{latest>ma60}")
    except Exception as e:
        print(f"  {name}: {e}")

bull = sum(1 for r in results.values() if r["above_ma20"] and r["above_ma60"] and r["ma20_up"] and r["ma60_up"])
risk = sum(1 for r in results.values() if not r["above_ma20"] and not r["ma20_up"])
if bull==3: state="多头趋势"; gate="正常模式"
elif risk>=2: state="系统性风险"; gate="暂停交易"
else: state="震荡市"; gate="降低权重"
print(f"  L1收盘判定: {state} → {gate} (bull={bull}, risk={risk})")

# ========== Step 2: L2 涨停池 + 题材 ==========
print("\n--- L2 涨停池 ---")
time.sleep(1.5)

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
    print(f"涨停: {zt_count}家  最高连板: {max_lb}  2板+: {sum(v for k,v in lbc_dist.items() if k>=2)}家")
    print(f"连板分布: {dict(sorted(lbc_dist.items()))}")
    print(f"行业TOP5: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}")
except Exception as e:
    print(f"涨停池: {e}")

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
    print(f"炸板: {e}")

# ========== Step 3: L3 个股打分 ==========
print(f"\n--- L3 个股打分 (Top 15) ---")
def to_tx(code):
    if code.startswith("6"): return "sh"+code
    if code.startswith("0") or code.startswith("3"): return "sz"+code
    if code.startswith("9"): return "bj"+code
    return code

top = sorted(zt_pool, key=lambda x: (-x.get("lbc",0), -x.get("amount",0)))[:15]
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
        cap=float(f[45]) if f[45] and f[45]!="" else 0
        buy=0; sell=0; hits=[]
        if pct>=0.8: buy+=1; hits.append("R3")
        if turnover>5 and pct>0: buy+=1; hits.append("R4")
        if pct>=9.5: buy+=1; hits.append("R5")
        if pct>=9.0 and lbc==1: buy+=1; hits.append("R2")
        if pct<=-1.5: sell+=1
        if pct<=-3.0: sell+=1
        net=buy-sell
        if buy>=3 and net>=1: out="强候选"
        elif buy>=2 and net>=1: out="观察"
        elif sell>=2: out="风控"
        else: out="中性"
        cap_yi=cap/10000 if cap else 0
        signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,
            "pe":pe,"buy":buy,"sell":sell,"net":net,"out":out,
            "industry":z.get("hybk",""),"cap_yi":cap_yi,"hits":hits,
            "turnover":turnover})
        print(f"  {code} {name:8s} {lbc}板 ¥{price:.2f} +{pct:.1f}% PE{pe:.1f} 市值{cap_yi:.0f}亿 buy={buy} {out}")
    except Exception as e:
        print(f"  {code} {name}: {e}")

# ========== Step 4: 对比盘前信号 + 虚拟交易 ==========
print(f"\n--- Step 4: 信号回顾 & 虚拟交易 ---")

with open(PORTFOLIO,"r") as f:
    portfolio = json.load(f)

pre_market = portfolio["daily_log"][-1]
pre_signals = pre_market.get("signals",[])

yesterday_review = []
for ps in pre_signals:
    code = ps["code"]
    try:
        r = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
        f = r.text.split("~")
        if len(f)<40: continue
        today_price = float(f[3]) if f[3] else 0
        today_pct = float(f[32]) if f[32] else 0
        today_open = float(f[5]) if f[5] else 0
        today_high = float(f[33]) if f[33] else 0
        today_low = float(f[34]) if f[34] else 0
        chg = (today_price - ps["price"])/ps["price"]*100

        if today_pct >= 9.5: status = "涨停"
        elif today_pct >= 5: status = "大涨"
        elif today_pct >= 1: status = "小涨"
        elif today_pct >= -1: status = "平盘"
        elif today_pct >= -5: status = "小跌"
        elif today_pct >= -9.5: status = "大跌"
        else: status = "跌停"

        yesterday_review.append({
            "code": code, "name": ps["name"], "lbc": ps["lbc"],
            "yesterday_price": ps["price"],
            "today_price": today_price, "today_pct": today_pct,
            "today_open": today_open, "today_high": today_high, "today_low": today_low,
            "status": status, "chg_from_pre": chg
        })
        print(f"  {code} {ps['name']:8s} 昨¥{ps['price']:.2f}→今¥{today_price:.2f} +{today_pct:.1f}% [{status}]")
    except Exception as e:
        print(f"  {code} {ps['name']}: {e}")

trades = []
holdings = portfolio["holdings"]
cash = portfolio["account"]["cash"]

pos_review = sum(1 for r in yesterday_review if r["today_pct"] > 0)
neg_review = sum(1 for r in yesterday_review if r["today_pct"] < 0)
zt_review = sum(1 for r in yesterday_review if r["status"] == "涨停")
dt_review = sum(1 for r in yesterday_review if r["status"] in ("跌停","大跌"))
print(f"\n盈亏统计: 正收益{pos_review}/{len(yesterday_review)} ({pos_review/len(yesterday_review)*100:.0f}%), 涨停{zt_review}只, 跌/大跌{dt_review}只")

if gate == "暂停交易":
    print("门控: 暂停交易 → 不执行任何虚拟买卖")
    decisions = "系统性风险门控→暂停交易，不执行虚拟买卖。"
else:
    for s in signals:
        if s["out"] == "强候选" and s["lbc"] >= 1 and s["pct"] >= 9.5:
            if not any(h["code"]==s["code"] for h in holdings):
                buy_amount = 100000
                shares = int(buy_amount / s["price"] / 100) * 100
                if shares >= 100 and cash >= shares * s["price"]:
                    cash -= shares * s["price"]
                    holdings.append({
                        "code": s["code"], "name": s["name"],
                        "shares": shares, "cost": s["price"],
                        "buy_date": today, "buy_price": s["price"],
                        "buy_reason": f"{s['lbc']}板强候选 {s['industry']}"
                    })
                    trades.append({"type":"buy","code":s["code"],"name":s["name"],
                        "shares":shares,"price":s["price"],"amount":shares*s["price"]})
                    print(f"  买入: {s['code']} {s['name']} {shares}股 ¥{s['price']:.2f} 金额{shares*s['price']:.0f}")
                    break

    for h in list(holdings):
        try:
            r = requests.get(f"http://qt.gtimg.cn/q={h['code']}", timeout=5)
            f = r.text.split("~")
            if len(f)<40: continue
            today_price = float(f[3]) if f[3] else 0
            today_pct = float(f[32]) if f[32] else 0
            sell_score = 0
            if today_pct <= -1.5: sell_score += 1
            if today_pct <= -3.0: sell_score += 1
            if today_price < h["cost"] * 0.95: sell_score += 1
            if sell_score >= 2:
                cash += h["shares"] * today_price
                trades.append({"type":"sell","code":h["code"],"name":h["name"],
                    "shares":h["shares"],"price":today_price,"amount":h["shares"]*today_price,
                    "pnl": (today_price-h["cost"])*h["shares"]})
                holdings.remove(h)
                print(f"  卖出: {h['code']} {h['name']} {h['shares']}股 ¥{today_price:.2f} sell_score={sell_score}")
        except: pass

total_holdings_value = 0
for h in holdings:
    try:
        r = requests.get(f"http://qt.gtimg.cn/q={h['code']}", timeout=5)
        f = r.text.split("~")
        if len(f)<40: continue
        total_holdings_value += h["shares"] * float(f[3])
    except: pass

total_value = cash + total_holdings_value
pnl = total_value - portfolio["account"]["initial_capital"]
pnl_pct = pnl / portfolio["account"]["initial_capital"] * 100

portfolio["account"]["cash"] = round(cash, 2)
portfolio["account"]["total_value"] = round(total_value, 2)
portfolio["account"]["pnl"] = round(pnl, 2)
portfolio["account"]["pnl_pct"] = round(pnl_pct, 2)
portfolio["account"]["last_updated"] = f"{today} 收盘简报"
portfolio["holdings"] = holdings

daily_log_entry = {
    "date": today,
    "l1_state": state,
    "l1_gate": gate,
    "l1_bull": bull,
    "l1_risk": risk,
    "l1_indexes": {k: {"close": v["close"], "ma20": v["ma20"], "ma60": v["ma60"],
                       "above_ma20": v["above_ma20"], "above_ma60": v["above_ma60"],
                       "ma20_up": v["ma20_up"], "ma60_up": v["ma60_up"]} for k,v in results.items()},
    "l2_summary": f"{zt_count}家涨停, 炸板率{zbr:.1f}%, 最高{max_lb}板, 主线{ind_cnt.most_common(1)[0][0] if ind_cnt else '?'}({ind_cnt.most_common(1)[0][1] if ind_cnt else 0})",
    "l2_zt_count": zt_count,
    "l2_max_lb": max_lb,
    "l2_zb_rate": round(zbr, 1),
    "signals": signals,
    "yesterday_review": yesterday_review,
    "trades": trades,
    "holdings_snapshot": holdings,
    "decisions": f"{gate}→{decisions if gate=='暂停交易' else (gate+'模式。')} 盘前{len(pre_signals)}只信号→今正收益{pos_review}只({pos_review/len(pre_signals)*100:.0f}%胜率), 涨停{zt_review}只。",
    "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
}
portfolio["daily_log"].append(daily_log_entry)

with open(PORTFOLIO,"w") as f:
    json.dump(portfolio, f, ensure_ascii=False, indent=2)

# ========== Step 5: 输出收盘简报 ==========
print(f"""
{'='*50}
📊 A股SOP收盘简报 {today}
{'='*50}
L1: {state} → {gate}
{chr(10).join(f'  {n}: 收{v["close"]:.0f} MA20={v["ma20"]:.0f}({"↑" if v["ma20_up"] else "↓"}) MA60={v["ma60"]:.0f}({"↑" if v["ma60_up"] else "↓"})' for n,v in results.items())}
L2: {zt_count}家涨停 最高{max_lb}板 炸板率{zbr:.1f}% 主线{' '.join(f'{k}({v})' for k,v in ind_cnt.most_common(3))}
今日信号: {sum(1 for s in signals if s['out']=='强候选')}只强候选, {sum(1 for s in signals if s['out']=='观察')}只观察
虚拟交易: 买入{sum(1 for t in trades if t['type']=='buy')}只 / 卖出{sum(1 for t in trades if t['type']=='sell')}只
{chr(10).join(f"  {'买' if t['type']=='buy' else '卖'} {t['name']} {t['shares']}股 ¥{t['price']:.2f}" for t in trades) if trades else '  (无)'}
账户: ¥{total_value:,.0f} ({pnl_pct:+.2f}%)  现金: ¥{cash:,.0f}  持仓: {len(holdings)}只
盘前信号今日表现: {pos_review}/{len(pre_signals)}正收益({pos_review/len(pre_signals)*100:.0f}%), {zt_review}只涨停
{'='*50}
""")
