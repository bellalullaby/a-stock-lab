import requests, json, time
from datetime import datetime, date
from collections import Counter

today = date.today().strftime("%Y-%m-%d")
today_c = today.replace("-","")
h = {"Referer":"https://data.eastmoney.com/"}
print(f"=== A股SOP收盘简报 {today} ===")
print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ============ Step 1: L1 收盘复核 ============
print("\n--- Step 1: L1 大盘收盘复核 ---")
indexes = {"上证":"sh000001","深证":"sz399001","创业板":"sz399006"}
results = {}
for name, code in indexes.items():
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,60,qfq"
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
        chg_pct = (closes[-1] - closes[-2])/closes[-2]*100 if n>=2 else 0
        results[name] = {"close":latest,"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"ma60_up":ma60>ma60_5d,
            "above_ma20":latest>ma20,"above_ma60":latest>ma60,
            "chg_pct":chg_pct}
        ma20_arrow = '↑' if ma20>ma20_5d else '↓'
        ma60_arrow = '↑' if ma60>ma60_5d else '↓'
        print(f"  {name}: 收{latest:.2f}({chg_pct:+.2f}%) MA20={ma20:.2f}({ma20_arrow}) MA60={ma60:.2f}({ma60_arrow}) 站MA20:{latest>ma20} MA60:{latest>ma60}")
    except Exception as e:
        print(f"  {name}: {e}")

bull = sum(1 for r in results.values() if r["above_ma20"] and r["above_ma60"] and r["ma20_up"] and r["ma60_up"])
risk = sum(1 for r in results.values() if not r["above_ma20"] and not r["ma20_up"])
if bull==3: state="多头趋势"; gate="正常模式"
elif risk>=2: state="系统性风险"; gate="暂停交易"
else: state="震荡市"; gate="降低权重"
print(f"  L1收盘: {state} → {gate} (bull={bull}, risk={risk})")

# ============ Step 2: L2 涨停池 + 题材 ============
print("\n--- Step 2: L2 涨停池 + 题材 ---")

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
    print(f"行业TOP5: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}")
    print(f"连板分布: {dict(sorted(lbc_dist.items()))}")
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
    print(f"炸板池: {e}")

# ============ Step 3: L3 个股打分 ============
print("\n--- Step 3: L3 个股打分 ---")

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
        time.sleep(0.3)
        r2 = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
        f = r2.text.split("~")
        if len(f)<40: continue
        price=float(f[3]) if f[3] else 0
        pct=float(f[32]) if f[32] else 0
        pe=float(f[39]) if f[39] and f[39]!="" else 0
        turnover=float(f[38]) if f[38] and f[38]!="" else 0
        cap=float(f[45]) if f[45] and f[45]!="" else 0
        buy=0; sell=0; hits=[]
        if pct>=0.8: buy+=1; hits.append("R3走强")
        if turnover>5 and pct>0: buy+=1; hits.append("R4放量")
        if pct>=9.5: buy+=1; hits.append("R5涨停")
        if pct>=9.0 and lbc==1: buy+=1; hits.append("R2首板")
        if pct<=-1.5: sell+=1
        if pct<=-3.0: sell+=1
        net=buy-sell
        if buy>=3 and net>=1: out="强候选"
        elif buy>=2 and net>=1: out="观察"
        elif sell>=2: out="风控"
        else: out="中性"
        cap_yi=cap/100000000 if cap else 0
        open_price=float(f[4]) if f[4] and f[4]!="" else 0
        high=float(f[33]) if f[33] and f[33]!="" else 0
        low=float(f[34]) if f[34] and f[34]!="" else 0
        signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,
            "pe":pe,"buy":buy,"sell":sell,"net":net,"out":out,
            "industry":z.get("hybk",""),"cap_yi":cap_yi,"hits":hits,
            "turnover":turnover,"open":open_price,"high":high,"low":low})
        print(f"  {code} {name:8s} {lbc}板 ¥{price:.2f} {pct:+.1f}% PE{pe:.1f} 市值{cap_yi:.0f}亿 buy={buy}/{sell} {out}")
        if hits: print(f"    命中: {', '.join(hits)}")
    except Exception as e:
        print(f"  {code} {name}: {e}")

# ============ Step 4: 对比盘前信号 ============
print("\n--- Step 4: 盘前信号复盘 ---")
pre_signals = [
    {"code":"sz001258","name":"立新能源","lbc":6,"pre_price":12.11},
    {"code":"sz000815","name":"美利云","lbc":4,"pre_price":17.74},
    {"code":"sz000603","name":"盛达资源","lbc":3,"pre_price":24.83},
    {"code":"sz002197","name":"证通电子","lbc":3,"pre_price":7.67},
    {"code":"sz002879","name":"长缆科技","lbc":3,"pre_price":16.41},
    {"code":"sh603221","name":"爱丽家居","lbc":3,"pre_price":12.73},
    {"code":"sh600396","name":"华电辽能","lbc":2,"pre_price":16.41},
    {"code":"sh600722","name":"金牛化工","lbc":2,"pre_price":10.9},
    {"code":"sh603619","name":"中曼石油","lbc":2,"pre_price":23.47},
    {"code":"sz002900","name":"哈三联","lbc":2,"pre_price":12.1},
    {"code":"sz002412","name":"汉森制药","lbc":2,"pre_price":7.38},
    {"code":"sh605162","name":"新中港","lbc":2,"pre_price":8.45},
    {"code":"sh603988","name":"中电电机","lbc":2,"pre_price":17.17},
    {"code":"sz000595","name":"新能股份","lbc":2,"pre_price":5.52},
    {"code":"sz301234","name":"五洲医疗","lbc":2,"pre_price":48.96},
]

yesterday_review = []
for s in pre_signals:
    try:
        time.sleep(0.3)
        r = requests.get(f"http://qt.gtimg.cn/q={s['code']}", timeout=5)
        f = r.text.split("~")
        if len(f)<40: continue
        price = float(f[3]) if f[3] else 0
        pct = float(f[32]) if f[32] else 0
        open_p = float(f[4]) if f[4] and f[4]!="" else 0
        high = float(f[33]) if f[33] and f[33]!="" else 0
        low = float(f[34]) if f[34] and f[34]!="" else 0
        pre = s['pre_price']
        chg_pct = (price - pre) / pre * 100
        if pct >= 9.5: status = "涨停"
        elif chg_pct >= 3: status = "大涨"
        elif chg_pct >= 1: status = "小涨"
        elif chg_pct >= -1: status = "平盘"
        elif chg_pct >= -5: status = "小跌"
        elif chg_pct >= -10: status = "大跌"
        else: status = "跌停"
        hit_sell = 0
        if chg_pct <= -1.5: hit_sell += 1
        if chg_pct <= -3.0: hit_sell += 1
        entry = {
            "code": s['code'], "name": s['name'], "lbc": s['lbc'],
            "pre_price": pre, "today_price": price, "today_open": open_p,
            "today_high": high, "today_low": low,
            "pct": pct, "chg_from_pre": round(chg_pct, 2),
            "status": status, "hit_sell": hit_sell
        }
        yesterday_review.append(entry)
        print(f"  {s['code']} {s['name']:8s} {s['lbc']}板 → ¥{price:.2f} {chg_pct:+.1f}% [{status}]")
    except Exception as e:
        print(f"  {s['code']} {s['name']}: {e}")

# ============ Step 5: 输出收盘简报 ============
print("\n" + "="*60)
print(f"📊 A股SOP收盘简报 {today}")

idx_lines = []
for name, r in results.items():
    ma20_arrow = '↑' if r['ma20_up'] else '↓'
    ma60_arrow = '↑' if r['ma60_up'] else '↓'
    above = f"站MA20:{'Y' if r['above_ma20'] else 'N'} MA60:{'Y' if r['above_ma60'] else 'N'}"
    idx_lines.append(f"  {name}: {r['close']:.0f}({r['chg_pct']:+.1f}%) MA20={r['ma20']:.0f}({ma20_arrow}) MA60={r['ma60']:.0f}({ma60_arrow}) {above}")

pre_l1 = "系统性风险"
l1_changed = state != pre_l1
l1_status = f"{state}{' CHANGED!' if l1_changed else ''} -> {gate}"

l2_str = f"{zt_count}家涨停 炸板率{zbr:.1f}% 最高{max_lb}板"
ind_str = ', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))

strong = [s for s in signals if s['out'] == '强候选']
observe = [s for s in signals if s['out'] == '观察']
sig_str = f"{len(strong)}只强候选, {len(observe)}只观察"

if gate == "暂停交易":
    trade_str = "门控暂停, 无虚拟交易"
else:
    trade_str = "执行虚拟交易"

pos_count = sum(1 for yr in yesterday_review if yr['chg_from_pre'] > 0)
zt_count_today = sum(1 for yr in yesterday_review if yr['status'] == '涨停')
neg_count = sum(1 for yr in yesterday_review if yr['chg_from_pre'] <= 0)
yr_str = f"盘前15只信号->正收益{pos_count}/15, 涨停{zt_count_today}只, 负收益{neg_count}只"

print(f"L1: {l1_status}")
for line in idx_lines:
    print(line)
print(f"L2: {l2_str}")
print(f"  主线: {ind_str}")
print(f"今日信号: {sig_str}")
print(f"虚拟交易: {trade_str}")
print(f"盘前信号复盘: {yr_str}")
print(f"账户: 1,000,000 (0.00%) - 满仓现金, 无持仓")

if strong:
    print(f"\n强候选Top10:")
    for i, s in enumerate(strong[:10], 1):
        print(f"  {i}. {s['code']} {s['name']} {s['lbc']}板 {s['price']:.2f} {s['pct']:+.1f}% PE{s['pe']:.1f} {s['industry']}")

if yesterday_review:
    print(f"\n盘前信号15只详细复盘:")
    for yr in yesterday_review:
        arrow = "DOWN" if yr['chg_from_pre'] <= -3 else ("FLAT" if yr['chg_from_pre'] < 0 else "UP")
        print(f"  [{arrow}] {yr['code']} {yr['name']:8s} {yr['lbc']}板 {yr['pre_price']}->{yr['today_price']:.2f} {yr['chg_from_pre']:+.1f}% [{yr['status']}]")

print("="*60)

# ============ Save results ============
closing_data = {
    "date": today,
    "session": "收盘简报(15:30运行)",
    "l1_state": state,
    "l1_gate": gate,
    "l1_changed": l1_changed,
    "l1_bull": bull,
    "l1_risk": risk,
    "l1_indexes": {
        name: {
            "close": r['close'],
            "chg_pct": r['chg_pct'],
            "ma20": r['ma20'],
            "ma60": r['ma60'],
            "ma20_up": r['ma20_up'],
            "ma60_up": r['ma60_up'],
            "above_ma20": r['above_ma20'],
            "above_ma60": r['above_ma60']
        } for name, r in results.items()
    },
    "l1_detail": '\n'.join(idx_lines),
    "l2_zt_count": zt_count,
    "l2_zb_count": zb_count,
    "l2_zbr": round(zbr, 1),
    "l2_max_lb": max_lb,
    "l2_sectors": [{"name": k, "count": v} for k, v in ind_cnt.most_common(5)],
    "l2_summary": l2_str,
    "signals": signals,
    "yesterday_review": yesterday_review,
    "yesterday_review_summary": yr_str,
    "trades": [],
    "holdings_snapshot": [],
    "decisions": f"门控: {gate}。{trade_str}。{yr_str}。",
    "run_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
}

import sys as _sys
from pathlib import Path as _Path
_REPO = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_REPO))
from common_paths import CLOSING_TEMP
with open(CLOSING_TEMP, "w", encoding="utf-8") as f:
    json.dump(closing_data, f, ensure_ascii=False, indent=2)

print(f"\nDone. L1状态{'已切换!' if l1_changed else '未变'}: {state} -> {gate}")
