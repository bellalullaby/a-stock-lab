import requests, json, time
from datetime import date, datetime
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
print(f"=== A股SOP盘前简报 {today} ===")

# ============================================================
# Step 1: L1 宏观环境
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
        chg_5d = (closes[-1]/closes[-5]-1)*100 if n>=5 else 0
        results[name] = {"close":latest,"ma20":ma20,"ma60":ma60,
            "ma20_up":ma20>ma20_5d,"ma60_up":ma60>ma60_5d,
            "above_ma20":latest>ma20,"above_ma60":latest>ma60,
            "chg_5d": chg_5d}
        print(f"  {name}: 收{latest:.0f} MA20={ma20:.0f}({'↑' if ma20>ma20_5d else '↓'}) MA60={ma60:.0f}({'↑' if ma60>ma60_5d else '↓'}) 5日{chg_5d:+.1f}%")
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
print(f"  L1判定: bull={bull} risk={risk} → {state} → {gate}")

# ============================================================
# Step 2: L2 涨停池 + 行业排名
# ============================================================
print("\n--- Step 2: L2 涨停池 ---")
h = {"Referer":"https://data.eastmoney.com/"}
today_compact = today.replace("-","")

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

time.sleep(1.5)

# 2.2 涨停池
zt = []
zt_count = 0
try:
    r = requests.get("https://push2ex.eastmoney.com/getTopicZTPool",
        params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt",
                "Pageindex":"0","pagesize":"300","sort":"fbt:asc","date":today_compact},
        headers=h, timeout=10)
    d = r.json()
    zt = d.get("data",{}).get("pool",[])
    zt_count = len(zt)
    lbc_dist = Counter(z.get("lbc",0) for z in zt)
    ind_cnt = Counter(z.get("hybk","") for z in zt if z.get("hybk"))
    print(f"涨停: {zt_count}家  最高连板: {max(lbc_dist.keys()) if lbc_dist else 0}")
    print(f"连板分布: {dict(sorted(lbc_dist.items()))}")
    print(f"行业TOP5: {', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}")
except Exception as e:
    print(f"涨停池: {e}")

# ============================================================
# Step 3: L3 个股打分
# ============================================================
print("\n--- Step 3: L3 个股打分 ---")

def to_tx(code):
    if code.startswith("6"): return "sh"+code
    if code.startswith("0") or code.startswith("3"): return "sz"+code
    if code.startswith("9"): return "bj"+code
    return code

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
        amount=float(f[37]) if f[37] else 0

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

        cap_yi = amount / 100000000 if amount > 0 else 0
        signals.append({"code":code,"name":name,"lbc":lbc,"price":price,"pct":pct,
            "pe":pe,"buy":buy,"sell":sell,"net":net,"out":out,
            "industry":z.get("hybk",""),"hits":hits,
            "amount":amount,"turnover":turnover,"cap_yi":cap_yi})
        print(f"  {code} {name:8s} {lbc}板 ¥{price:.2f} +{pct:.1f}% PE{pe:.1f} "
              f"换手{turnover:.1f}% buy={buy} sell={sell} → {out}")
        time.sleep(0.3)
    except Exception as e:
        print(f"  {code} {name}: 查询失败 {e}")

if state=="震荡市":
    valid = [s for s in signals if s["buy"]>=3]
    weak = [s for s in signals if s["buy"]==2]
else:
    valid = [s for s in signals if s["buy"]>=2]
    weak = []

# ============================================================
# Step 4: 对比昨日信号
# ============================================================
print("\n--- Step 4: 对比昨日信号 ---")
with open(PORTFOLIO, "r") as f:
    portfolio = json.load(f)

yesterday_signals = []
if portfolio.get("daily_log"):
    last_log = portfolio["daily_log"][-1]
    yesterday_signals = last_log.get("signals", [])
    print(f"昨日({last_log.get('date','?')})有{len(yesterday_signals)}个信号")

review_results = []
for ys in yesterday_signals:
    code = ys.get("code","")
    name = ys.get("name","")
    yesterday_price = ys.get("price",0)
    yesterday_lbc = ys.get("lbc",0)
    yesterday_out = ys.get("out","")
    try:
        r3 = requests.get(f"http://qt.gtimg.cn/q={code}", timeout=5)
        f = r3.text.split("~")
        if len(f)<40: continue
        today_price = float(f[3]) if f[3] else 0
        today_pct = float(f[32]) if f[32] else 0
        today_high = float(f[33]) if f[33] else 0
        today_low = float(f[34]) if f[34] else 0
        today_open = float(f[5]) if f[5] else 0
        chg = (today_price / yesterday_price - 1) * 100 if yesterday_price > 0 else 0

        status = "平盘"
        if chg >= 9.5: status = "✅涨停"
        elif chg >= 5: status = "大涨"
        elif chg >= 1: status = "小涨"
        elif chg > -3: status = "横盘"
        elif chg > -7: status = "下跌"
        elif chg > -9.5: status = "大跌"
        else: status = "跌停"

        review_results.append({
            "code": code, "name": name,
            "yesterday_price": yesterday_price,
            "yesterday_lbc": yesterday_lbc,
            "yesterday_out": yesterday_out,
            "today_price": today_price,
            "today_pct": today_pct,
            "chg_from_yesterday": chg,
            "today_open": today_open,
            "today_high": today_high,
            "today_low": today_low,
            "status": status
        })
        print(f"  {code} {name:8s} 昨{yesterday_price:.2f}→今{today_price:.2f} {chg:+.1f}% {status}")
        time.sleep(0.2)
    except Exception as e:
        print(f"  {code} {name}: {e}")

up_count = sum(1 for r in review_results if r["chg_from_yesterday"] > 0)
zt_count_review = sum(1 for r in review_results if r["status"] == "✅涨停")
dt_count_review = sum(1 for r in review_results if r["status"] == "跌停")
print(f"  回顾: {up_count}/{len(review_results)}正收益, {zt_count_review}涨停, {dt_count_review}跌停")

# ============================================================
# Save
# ============================================================
l1_idx = results.get("上证",{})
l1_sz = results.get("深证",{})
l1_cy = results.get("创业板",{})

new_log = {
    "date": today,
    "session": "盘前简报(08:35运行, 数据截至前一交易日收盘)",
    "l1_state": state,
    "l1_gate": gate,
    "l1_detail": f"上证{l1_idx.get('close',0):.0f} MA20={l1_idx.get('ma20',0):.0f}({'↑' if l1_idx.get('ma20_up') else '↓'}) MA60={l1_idx.get('ma60',0):.0f}({'↑' if l1_idx.get('ma60_up') else '↓'}); 深证{l1_sz.get('close',0):.0f} MA20={l1_sz.get('ma20',0):.0f}({'↑' if l1_sz.get('ma20_up') else '↓'}) MA60={l1_sz.get('ma60',0):.0f}({'↑' if l1_sz.get('ma60_up') else '↓'}); 创业板{l1_cy.get('close',0):.0f} MA20={l1_cy.get('ma20',0):.0f}({'↑' if l1_cy.get('ma20_up') else '↓'}) MA60={l1_cy.get('ma60',0):.0f}({'↑' if l1_cy.get('ma60_up') else '↓'}); bull={bull} risk={risk}",
    "l2_summary": f"{zt_count}家涨停, 最高{max(lbc_dist.keys()) if lbc_dist else 0}板, 连板分布{dict(sorted(lbc_dist.items()))}, 主线:{', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}",
    "l1_indexes": {k: {"close": v["close"], "ma20": v["ma20"], "ma60": v["ma60"],
                       "ma20_up": v["ma20_up"], "ma60_up": v["ma60_up"],
                       "above_ma20": v["above_ma20"], "above_ma60": v["above_ma60"]} for k,v in results.items()},
    "signals": signals,
    "yesterday_review": review_results,
    "decisions": "",
    "trades": [],
    "note": ""
}

if gate == "暂停交易":
    new_log["decisions"] = f"系统性风险门控→暂停交易，不执行任何虚拟买卖。\n\n今日涨停{zt_count}家。连板分布{dict(sorted(lbc_dist.items()))}。主线:{', '.join(f'{k}({v})' for k,v in ind_cnt.most_common(5))}。\n\n昨日信号回顾：{up_count}/{len(review_results)}正收益, {zt_count_review}涨停, {dt_count_review}跌停。"
    if review_results:
        zt_names = [r['name'] for r in review_results if r['status'] == '✅涨停']
        dt_names = [r['name'] for r in review_results if r['status'] == '跌停']
        new_log["decisions"] += f"\n涨停留存：{', '.join(zt_names) if zt_names else '无'}。跌停：{', '.join(dt_names) if dt_names else '无'}。"
    new_log["note"] = "门控禁止开仓，仅记录观察。"
elif gate == "降低权重":
    new_log["decisions"] = f"震荡市门控→降低权重。有效候选{len(valid)}只：\n"
    for v in valid:
        new_log["decisions"] += f"  {v['code']} {v['name']} {v['lbc']}板 buy={v['buy']} [{v['industry']}]\n"
else:
    new_log["decisions"] = f"多头趋势→正常模式。候选{len(valid)}只有效。"

portfolio["daily_log"].append(new_log)
portfolio["account"]["last_updated"] = f"{today} 08:35 盘前简报"

with open(PORTFOLIO, "w") as f:
    json.dump(portfolio, f, ensure_ascii=False, indent=2)

print(f"\n✅ portfolio.json已更新 ({today})")
