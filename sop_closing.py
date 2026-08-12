#!/usr/bin/env python3
"""A-stock SOP Closing Briefing."""
import requests, json, time
from datetime import date
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
H = {"Referer":"https://data.eastmoney.com/"}
PF = PORTFOLIO
print(f"=== A股SOP收盘简报 {today} ===\n")

# ---- Step 1: L1 ----
print("--- L1 收盘复核 ---")
IX = {"上证":"sh000001","深证":"sz399001","创业板":"sz399006"}
results = {}
for name, code in IX.items():
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,60,qfq"
        r = requests.get(url, timeout=15)
        klines = r.json().get("data",{}).get(code,{}).get("day",[])
        if not klines: continue
        closes = [float(k[2]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        n = len(closes)
        ma20 = sum(closes[-20:])/20
        ma60 = sum(closes[-60:])/60 if n>=60 else sum(closes)/n
        ma20_5d = sum(closes[-25:-5])/20 if n>=25 else ma20
        latest = closes[-1]; prev = closes[-2]
        chg_pct = (latest-prev)/prev*100
        vol_t = volumes[-1]; vol_ma5 = sum(volumes[-5:])/5
        vol_ma20 = sum(volumes[-20:])/20
        vol_5d = sum(volumes[-10:-5])/5; vr = vol_t/vol_ma20
        vt = "up" if vol_ma5>vol_5d else "down"
        if vr>1.3: vl="big_up"
        elif vr>1.1: vl="up"
        elif vr<0.7: vl="big_down"
        elif vr<0.9: vl="down"
        else: vl="flat"
        pu = latest>prev
        if pu and "up" in vl: pv="price_up_vol_up"
        elif not pu and "down" in vl: pv="price_down_vol_down"
        elif not pu and "up" in vl: pv="panic"
        elif pu and "down" in vl: pv="weak_bounce"
        else: pv="neutral"
        results[name]={"close":latest,"chg_pct":chg_pct,"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"above_ma20":latest>ma20,"above_ma60":latest>ma60,
            "vol_today":vol_t,"vol_ma5":vol_ma5,"vol_ma20":vol_ma20,
            "vol_ratio":round(vr,2),"vol_label":vl,"price_vol":pv}
        print(f"  {name}: {latest:.0f}({chg_pct:+.1f}%) MA20={ma20:.0f} vol:{vl}({vr:.2f}x)")
    except Exception as e: print(f"  {name}: {e}")

bull = sum(1 for r in results.values() if r.get("above_ma20") and r.get("above_ma60") and r.get("ma20_up"))
risk = sum(1 for r in results.values() if not r.get("above_ma20") and not r.get("ma20_up"))
if bull==3: state="bull"; gate="normal"
elif risk>=2: state="bear"; gate="halt"
else: state="range"; gate="cautious"
print(f"  L1: {state} -> {gate}\n")

# ---- Step 2: L2 ----
print("--- L2 ---")
top_inds = ""
try:
    r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
        params={"pn":"1","pz":"10","fid":"f3","fs":"m:90+t:2","fields":"f2,f3,f14"},
        headers=H, timeout=10)
    boards = r.json().get("data",{}).get("diff",[])
    for i,b in enumerate((boards or [])[:5],1):
        print(f"  board{i}: {b.get('f14','?')} +{(b.get('f3',0)or 0):.2f}%")
except Exception as e: print(f"  boards err: {e}")
time.sleep(1.5)
zt_pool = []
try:
    r = requests.get("https://push2ex.eastmoney.com/getTopicZTPool",
        params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
            "Pageindex":"0","pagesize":"300","sort":"fbt:asc","date":today_c},
        headers=H, timeout=10)
    zt_pool = r.json().get("data",{}).get("pool",[])
    lbc_dist = Counter(z.get("lbc",0) for z in zt_pool)
    ind_cnt = Counter(z.get("hybk","") for z in zt_pool if z.get("hybk"))
    zt_count = len(zt_pool)
    max_lb = max(lbc_dist.keys()) if lbc_dist else 0
    print(f"  ZT: {zt_count} max_lb:{max_lb} 2b+:{sum(v for k,v in lbc_dist.items() if k>=2)}")
    top_inds = ", ".join(f"{k}({v})" for k,v in ind_cnt.most_common(5))
    if top_inds: print(f"  inds: {top_inds}")
except Exception as e: print(f"  zt err: {e}")
zbr=0
try:
    time.sleep(1.5)
    r = requests.get("https://push2ex.eastmoney.com/getTopicZBPool",
        params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
            "Pageindex":"0","pagesize":"300","sort":"fbt:asc","date":today_c},
        headers=H, timeout=10)
    zb_count = len(r.json().get("data",{}).get("pool",[]))
    zbr = zb_count/(zt_count+zb_count)*100 if zt_count+zb_count>0 else 0
    print(f"  ZB: {zb_count} rate:{zbr:.1f}%")
except Exception as e: print(f"  zb err: {e}")
print()

# ---- Helpers ----
def to_tx(c):
    if c.startswith("6"): return "sh"+c
    if c.startswith("0") or c.startswith("3"): return "sz"+c
    return c

def score_trend(klines):
    closes = [float(k[2]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    n = len(closes)
    if n<60: return 0,None,["<60"]
    ma20=sum(closes[-20:])/20; ma60=sum(closes[-60:])/60
    ma120=sum(closes[-120:])/120 if n>=120 else ma60
    ma20_5d=sum(closes[-25:-5])/20 if n>=25 else ma20
    latest=closes[-1]; prev=closes[-2]
    chg=(latest-prev)/prev*100
    premium=(latest-ma20)/ma20*100
    vol_ma5=sum(volumes[-5:])/5; vol_ma20=sum(volumes[-20:])/20
    vol_ratio=vol_ma5/vol_ma20
    score=0; flags=[]
    if ma20>ma60>ma120: score+=3; flags.append("ma_bull")
    if latest>ma20 and premium<15: score+=2; flags.append("above_ma20")
    if ma20>ma20_5d: score+=2; flags.append("ma20_up")
    if vol_ratio>1.1: score+=1; flags.append("vol_up")
    return score,{"close":latest,"chg_pct":round(chg,2),"ma20":ma20,"ma60":ma60,"ma120":ma120,"premium":round(premium,2),"vol_ratio":round(vol_ratio,2)},flags

def fetch_kline(code):
    url=f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,120,qfq"
    r=requests.get(url,timeout=10)
    dd=r.json().get("data",{}).get(code,{})
    return dd.get("qfqday",[]) or dd.get("day",[])

# ---- Step 3.1: Breakout ----
print("--- L3.1 ---")
top=sorted(zt_pool,key=lambda x:(-x.get("lbc",0),-x.get("amount",0)))[:15]
zt_signals=[]
for z in top:
    raw=z.get("c",""); code=to_tx(raw); name=z.get("n",""); lbc=z.get("lbc",0)
    try:
        r2=requests.get(f"http://qt.gtimg.cn/q={code}",timeout=5)
        f=r2.text.split("~")
        if len(f)<40: continue
        price=float(f[3]) if f[3] else 0; pct=float(f[32]) if f[32] else 0
        pe=float(f[39]) if f[39] and f[39]!="" else 0
        turnover=float(f[38]) if f[38] and f[38]!="" else 0
        buy=0; sell=0; hits=[]
        if pct>=0.8: buy+=1; hits.append("R3")
        if turnover>5 and pct>0: buy+=1; hits.append("R4")
        if pct>=9.5: buy+=1; hits.append("R5")
        if pct>=9.0 and lbc==1: buy+=1; hits.append("R2")
        if pct<=-1.5: sell+=1
        if pct<=-3.0: sell+=1
        net=buy-sell
        if buy>=3 and net>=1: out="strong"
        elif buy>=2 and net>=1: out="watch"
        elif sell>=2: out="risk"
        else: out="neutral"
        zt_signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,"pe":pe,"buy":buy,"out":out,"industry":z.get("hybk",""),"hits":hits})
        print(f"  {code} {name:8s} {lbc}b {price:.2f} +{pct:.1f}% buy={buy} {out}")
    except Exception as e: print(f"  {code} {name}: {e}")
print()

# ---- Step 3.5: Trend ----
print("--- L3.2 趋势股筛选 ---")
time.sleep(10)

# Build pool
pool=[]
try:
    for m in [("m:0+t:6","sh"),("m:1+t:6","sz")]:
        r = requests.get("https://push2.eastmoney.com/api/qt/clist/get",
            params={"pn":"1","pz":"100","po":"0","fid":"f62","fs":m[0],"fields":"f2,f3,f8,f12,f14,f20,f62"},
            headers=H,timeout=15)
        if r.status_code!=200: continue
        stocks = r.json().get("data",{}).get("diff",[]) or []
        for s in stocks:
            code=s.get("f12",""); pct=s.get("f3",0) or 0; turnover=s.get("f8",0) or 0
            if code and -5<=pct<=8.5 and turnover>=1.5:
                prefix="sh" if code.startswith("6") else "sz"
                pool.append({"code":prefix+code,"name":s.get("f14",""),"pct":pct,"turnover":turnover})
        time.sleep(2)
except Exception as e: print(f"  pool err: {e}")

FALLBACK="sh600519,茅台 sz000858,五粮液 sh600809,汾酒 sz000568,泸州老窖 sh600887,伊利 sz000333,美的 sz000651,格力 sh601398,工行 sh601318,平安 sh600036,招行 sh600030,中信 sz300059,东财 sh601688,华泰 sz300750,宁德 sz002594,比亚迪 sh601012,隆基 sz300274,阳光 sh600438,通威 sz002129,TCL中环 sh600276,恒瑞 sh603259,药明 sz300760,迈瑞 sz000538,云南白药 sh600196,复星医药 sz002415,海康 sz002230,科大 sz002371,北方华创 sh688981,中芯 sz000725,京东方 sh600703,三安光电 sz300124,汇川 sz002049,紫光国微 sh600900,长电 sh601088,神华 sh600585,海螺 sh600031,三一 sh600309,万华 sh600660,福耀 sz002460,赣锋 sh601899,紫金 sh600893,航发 sh600760,沈飞 sz002013,中航机电 sh601127,赛力斯 sz002463,沪电 sh600745,闻泰 sh600011,华能 sh601985,中核 sh600905,三峡能源 sh601166,兴业 sz000001,平安银行 sz002384,东山精密 sh600183,生益科技 sz000636,风华高科 sh600536,中软 sz300033,同花顺 sz002432,九安医疗 sz300003,乐普医疗 sz000876,新希望 sz002714,牧原 sh601728,电信 sh600104,上汽 sz300015,爱尔 sh601857,石油 sh600028,中石化 sh601939,建行 sh601988,中行 sh600050,联通 sz002475,立讯 sh601628,人寿 sh601390,中铁"
if len(pool)<30:
    existed={s["code"] for s in pool}
    for item in FALLBACK.split():
        parts=item.split(",")
        if len(parts)==2 and parts[0] not in existed:
            pool.append({"code":parts[0],"name":parts[1],"pct":0,"turnover":0})
    seen=set(); uni=[]
    for s in pool:
        if s["code"] not in seen: seen.add(s["code"]); uni.append(s)
    pool=uni[:200]

print(f"  pool: {len(pool)} stocks")
trend_signals=[]
fetched=0; skipped=0
for s in pool:
    try:
        klines=fetch_kline(s["code"])
        if not klines or len(klines)<60: skipped+=1; continue
        sc,detail,flags=score_trend(klines)
        fetched+=1
        if sc>=5:
            trend_signals.append({"code":s["code"],"name":s["name"],"score":sc,"flags":flags,
                "close":detail["close"],"premium":detail["premium"],"vol_ratio":detail["vol_ratio"],
                "ma20":detail["ma20"],"ma60":detail["ma60"],"ma120":detail["ma120"],
                "chg_pct":detail["chg_pct"],"out":"trend"})
    except: skipped+=1
    time.sleep(0.15)

trend_signals.sort(key=lambda x: (-x["score"], -(x.get("premium",0) or 0)))
trend_signals=trend_signals[:15]
print(f"  scanned:{fetched} skipped:{skipped} trends:{len(trend_signals)}")
for i,t in enumerate(trend_signals,1):
    pr=t.get("premium",0) or 0; vr=t.get("vol_ratio",0) or 0; fl=t.get("flags",[])
    print(f"  {i:2d}. {t['code']} {t['name']} s:{t['score']} cls:{t['close']:.0f} prem:{pr:+.1f}% vr:{vr:.2f} {fl}")
print()

# ---- Step 4: Yesterday ----
print("--- Yesterday Review ---")
with open(PF,"r") as f: portfolio=json.load(f)
yesterday_log=None
for log in portfolio["daily_log"]:
    if log["date"]<today and "signals" in log: yesterday_log=log; break
yesterday_review=[]
if yesterday_log and yesterday_log.get("signals"):
    for sig in yesterday_log["signals"]:
        code=sig["code"]
        try:
            r2=requests.get(f"http://qt.gtimg.cn/q={code}",timeout=5)
            f=r2.text.split("~")
            if len(f)<40: continue
            tp=float(f[3]) if f[3] else 0; tpct=float(f[32]) if f[32] else 0
            to=float(f[5]) if f[5] else 0; co=(tp-to)/to*100 if to else 0
            if tpct>=9.5: st="ZT"
            elif tpct>=5: st="big_up"
            elif tpct>=1: st="up"
            elif tpct>=-1: st="flat"
            elif tpct>=-5: st="down"
            elif co<=-5: st="big_kill"
            else: st="down"
            yesterday_review.append({"code":code,"name":sig.get("name",""),"y_lbc":sig.get("lbc",0),"y_price":sig.get("price",0),"t_price":tp,"t_pct":tpct,"status":st})
            print(f"  {code} {sig.get('name',''):8s} {sig.get('lbc','?')}b {sig.get('price',0):.2f}->{tp:.2f}({tpct:+.1f}%) {st}")
        except Exception as e: print(f"  {code}: {e}")
print()

# ---- Step 5: Trading ----
print("--- Trading ---")
trades=[]; holdings=portfolio.get("holdings",[])
if gate=="halt": print("  HALTED")
elif gate=="cautious":
    print("  CAUTIOUS: trend only")
    for s in trend_signals[:5]:
        if portfolio["account"]["cash"]>=50000:
            shares=int(50000/s["close"]/100)*100
            if shares>0:
                cost=shares*s["close"]; portfolio["account"]["cash"]-=cost
                holdings.append({"code":s["code"],"name":s["name"],"shares":shares,"buy_price":s["close"],"buy_date":today,"strat":"trend"})
                trades.append({"type":"buy","code":s["code"],"name":s["name"],"shares":shares,"price":s["close"],"cost":cost,"strat":"trend"})
                print(f"  BUY trend {s['code']} {s['name']} {shares}@{s['close']:.2f} cost:{cost:.0f}")
else:
    strong=[s for s in zt_signals if s["buy"]>=3]
    for s in strong[:5]:
        if portfolio["account"]["cash"]>=100000:
            shares=int(100000/s["price"]/100)*100
            if shares>0:
                cost=shares*s["price"]; portfolio["account"]["cash"]-=cost
                holdings.append({"code":s["code"],"name":s["name"],"shares":shares,"buy_price":s["price"],"buy_date":today,"strat":"zt"})
                trades.append({"type":"buy","code":s["code"],"name":s["name"],"shares":shares,"price":s["price"],"cost":cost,"strat":"zt"})
                print(f"  BUY zt {s['code']} {s['name']} {shares}@{s['price']:.2f} cost:{cost:.0f}")
    for s in trend_signals[:5]:
        if portfolio["account"]["cash"]>=50000:
            shares=int(50000/s["close"]/100)*100
            if shares>0:
                cost=shares*s["close"]; portfolio["account"]["cash"]-=cost
                holdings.append({"code":s["code"],"name":s["name"],"shares":shares,"buy_price":s["close"],"buy_date":today,"strat":"trend"})
                trades.append({"type":"buy","code":s["code"],"name":s["name"],"shares":shares,"price":s["close"],"cost":cost,"strat":"trend"})
                print(f"  BUY trend {s['code']} {s['name']} {shares}@{s['close']:.2f} cost:{cost:.0f}")

if not trades: print("  no trades")

thv=0
for h in holdings:
    try:
        r2=requests.get(f"http://qt.gtimg.cn/q={h['code']}",timeout=5)
        f=r2.text.split("~"); cp=float(f[3]) if len(f)>=40 and f[3] else h["buy_price"]
    except: cp=h["buy_price"]
    h["cur"]=cp; h["val"]=h["shares"]*cp; h["pnl"]=h["val"]-h["shares"]*h["buy_price"]
    h["pnl_pct"]=(cp-h["buy_price"])/h["buy_price"]*100; thv+=h["val"]

tv=portfolio["account"]["cash"]+thv
pnl=tv-portfolio["account"]["initial_capital"]
pnl_pct=pnl/portfolio["account"]["initial_capital"]*100
portfolio["account"]["total_value"]=tv; portfolio["account"]["pnl"]=pnl
portfolio["account"]["pnl_pct"]=pnl_pct; portfolio["holdings"]=holdings

entry={"date":today,"l1_state":state,"l1_gate":gate,"l1_indexes":results,
    "l2_summary":f"{zt_count}zt zbr:{zbr:.1f}% max_lb:{max_lb}",
    "signals":zt_signals,"trend_signals":trend_signals,
    "yesterday_review":yesterday_review,"trades":trades,
    "decisions":f"{state}/{gate}. zt:{zt_count}. trend:{len(trend_signals)}. trades:{len(trades)}"}

found=False
for i,log in enumerate(portfolio["daily_log"]):
    if log["date"]==today: portfolio["daily_log"][i]=entry; found=True; break
if not found: portfolio["daily_log"].append(entry)

with open(PF,"w") as f: json.dump(portfolio,f,ensure_ascii=False,indent=2)

print(f"\n{'='*60}")
print(f"SOP CLOSING {today}")
print(f"L1: {state}/{gate}")
print(f"L2: {zt_count}zt {zbr:.1f}%zbr max_lb:{max_lb}")
zs=sum(1 for s in zt_signals if s["out"]=="strong")
zw=sum(1 for s in zt_signals if s["out"]=="watch")
print(f"L3.1 ZT: {zs}strong {zw}watch")
tt=trend_signals[0] if trend_signals else None
line=f"L3.2 Trend: {len(trend_signals)}"
if tt: line+=f" (top:{tt['name']} s:{tt['score']})"
print(line)
print(f"Trades: buy{sum(1 for t in trades if t['type']=='buy')} sell{sum(1 for t in trades if t['type']=='sell')}")
print(f"Account: {tv:,.0f} ({pnl_pct:+.2f}%) cash:{portfolio['account']['cash']:,.0f}")
