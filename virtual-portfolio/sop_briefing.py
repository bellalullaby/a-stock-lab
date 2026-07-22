import requests, json, time
from datetime import datetime, date
from collections import Counter

today = date.today().strftime("%Y-%m-%d")
today_compact = today.replace("-","")
print(f"=== A股SOP盘前简报 {today} ===")

# ============================================================
# Step 1: L1 宏观环境 - 三大指数60日K线
# ============================================================
print("\n--- Step 1: L1 宏观 ---")
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
        results[name] = {"close":latest,"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"ma60_up":ma60>ma60_5d,
            "above_ma20":latest>ma20,"above_ma60":latest>ma60}
        print(f"  {name}: 收{latest:.0f} MA20={ma20:.0f}({'↑' if ma20>ma20_5d else '↓'}) MA60={ma60:.0f}({'↑' if ma60>ma60_5d else '↓'})")
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
print(f"  L1判定: {state} → {gate}")
print(f"  判定细节: bull={bull} risk={risk}")

# ============================================================
# Step 2: L2 涨停池 + 行业排名
# ============================================================
print("\n--- Step 2: L2 涨停池 ---")
h = {"Referer":"https://data.eastmoney.com/"}

# 2.1 行业板块
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
    print(f"板块数据: {e}")
    boards = []

time.sleep(1.5)

# 2.2 涨停池
zt_all = []
try:
    for pg in range(0, 3):
        r = requests.get("https://push2ex.eastmoney.com/getTopicZTPool",
            params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
                    "Pageindex":str(pg),"pagesize":"500","sort":"fbt:asc","date":today_compact},
            headers=h, timeout=10)
        d = r.json()
        pool = d.get("data",{}).get("pool",[]) or []
        if not pool:
            break
        zt_all.extend(pool)
        if len(pool) < 500:
            break
except Exception as e:
    print(f"涨停池: {e}")

zt = zt_all
zt_count = len(zt)
if zt:
    lbc_dist = Counter(z.get("lbc",0) for z in zt)
    ind_cnt = Counter(z.get("hybk","") for z in zt if z.get("hybk"))
    print(f"涨停: {zt_count}家  最高连板: {max(lbc_dist.keys())}")
    print(f"行业TOP5: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}")
else:
    print("涨停数据为空 (可能是盘前暂无数据)")
    lbc_dist = {}

# ============================================================
# Step 3: L3 个股打分
# ============================================================
print("\n--- Step 3: L3 个股打分 ---")
def to_tx(code):
    if code.startswith("6"): return "sh"+code
    if code.startswith("0") or code.startswith("3"): return "sz"+code
    if code.startswith("9"): return "bj"+code
    return code

signals = []

if zt:
    top = sorted(zt, key=lambda x: (-x.get("lbc",0), -x.get("amount",0)))[:15]

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
            hits = []
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

            # PE warning
            if pe < 0:
                out += "(PE负)"
            elif pe > 500:
                out += "(PE虚高)"

            signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,
                "pe":pe,"turnover":turnover,"buy":buy,"sell":sell,"net":net,"out":out,"industry":z.get("hybk",""),
                "hits":hits,"amount":z.get("amount",0)})
            print(f"  {code} {name:8s} {lbc}板 ¥{price:.2f} +{pct:.1f}% PE{pe:.1f} T/O{turnover:.1f}% buy={buy} {out}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {code} {name}: {e}")
else:
    print("无涨停数据，跳过个股打分")

# 震荡市门槛: buy>=3才算有效
if state=="震荡市":
    valid = [s for s in signals if s["buy"]>=3]
    weak = [s for s in signals if s["buy"]==2]
else:
    valid = [s for s in signals if s["buy"]>=2]
    weak = []

print(f"\n候选统计: 有效候选{len(valid)}只, 弱信号{len(weak)}只")

# ============================================================
# Store results as JSON for next steps
# ============================================================
output = {
    "date": today,
    "l1": {
        "state": state,
        "gate": gate,
        "indexes": {k: {"close": v["close"], "ma20": v["ma20"], "ma60": v["ma60"],
                         "ma20_up": v["ma20_up"], "ma60_up": v["ma60_up"],
                         "above_ma20": v["above_ma20"], "above_ma60": v["above_ma60"]}
                    for k,v in results.items()},
        "total_amount_yi": total_amt/10000
    },
    "l2": {
        "zt_count": zt_count,
        "max_lbc": max(lbc_dist.keys()) if lbc_dist else 0,
        "top_industries": ind_cnt.most_common(5) if zt else [],
        "lbc_dist": dict(lbc_dist) if zt else {}
    },
    "l3": {
        "signals": signals,
        "valid_count": len(valid),
        "valid": valid,
        "weak": weak
    }
}

print("\n=== Results JSON ===")
print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
