"""
stop_loss.py — 五层止损引擎（收盘简报执行版）
============================================================
背景：SOP 文档里写了五层止损，但收盘脚本里卖出写死 sold=[]，
从没真正执行过。本模块把 app.py 里的五层检查函数搬过来，
并提供 run_stop_loss() 执行入口：每天收盘后对持仓逐只检查，
触发就生成卖出记录。

五层规则（与 app.py /api/stop-loss/jj 展示逻辑一致）：
  1. 硬止损     : 累计亏损 ≤ -8% 或单日 ≤ -5%
  2. 时间止损   : D+3 涨幅 < 2% 或 D+5 涨幅 < 5%
  3. 板块止损   : 持仓行业跌出板块 TOP20（TOP10 减半仓）
  4. 炸板止损   : 持仓票在今日炸板池
  5. 连板梯度   : 根据连板数调整硬止损阈值（展示用，不直接触发）

用法（收盘脚本内）:
    from stop_loss import run_stop_loss, fetch_tencent_prices
    prices = fetch_tencent_prices([h["code"] for h in holdings])
    sell_list = run_stop_loss(holdings, prices, l2_boards, l2_zt, l2_zb, check_date)
    # sell_list: [{code, name, shares, price, amount, reasons: [...]}, ...]
"""

import json
import urllib.request
from datetime import datetime
from data_integrity import valid_price, trading_age


# ── 工具函数 ──────────────────────────────────────────────

def norm_code(code: str) -> str:
    """归一化代码：sh600272 / sz002721 / 600272 → 600272"""
    c = str(code or "")
    for prefix in ("sh", "sz", "bj"):
        if c.startswith(prefix):
            return c[2:]
    return c


def fetch_tencent_prices(codes, check_date=None, with_details=False):
    """批量拉腾讯实时行情，返回 {原始code: 现价}（code 带 sh/sz 前缀）"""
    check_date = check_date or datetime.now().strftime("%Y-%m-%d")
    if not codes:
        return {}
    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(codes)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        # P0 教训：urllib 默认读环境代理(Clash) → 财经接口 502/SSL 失败，
        # 必须显式置空代理，强制直连（与 data_collector.NO_PROXY 一致）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = opener.open(req, timeout=10).read().decode("gbk")
        prices = {}
        for line in data.strip().split(";"):
            if not line.strip() or "=" not in line or '"' not in line:
                continue
            key = line.split("=")[0].split("_")[-1]  # 如 sh600272
            vals = line.split('"')[1].split("~")
            if len(vals) > 40 and vals[3]:
                stamp = vals[30]
                quote_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
                price = float(vals[3])
                # Stale/suspended/malformed quotes cannot execute a trade.
                if quote_date != check_date or not valid_price(price) or float(vals[6] or 0) <= 0:
                    continue
                prices[key] = ({"price": price, "date": quote_date,
                                "prev_close": float(vals[4]), "source": "tencent_qt"}
                               if with_details else price)
        return prices
    except Exception:
        return {}


# ── 五层止损检查（与 app.py 逻辑一致）───────────────────

def check_hard_stop(holding, current_price):
    """第一层：硬止损。累计亏损 ≤ -8% 或单日 ≤ -5% → 触发"""
    cost = holding.get("cost_price", 0)
    if cost <= 0 or current_price <= 0:
        return {"triggered": False, "level": 1, "rule": "硬止损", "detail": "数据不足"}
    loss_pct = round((current_price - cost) / cost * 100, 2)
    if loss_pct <= -8:
        return {
            "triggered": True,
            "level": 1,
            "rule": "硬止损",
            "detail": f"累计亏损 {loss_pct}%（成本¥{cost:.2f} → 现价¥{current_price:.2f}）",
        }
    prev_close = holding.get("prev_close")
    daily_pct = ((current_price / prev_close - 1) * 100
                 if valid_price(prev_close) else None)
    if daily_pct is not None and daily_pct <= -5:
        return {
            "triggered": True,
            "level": 1,
            "rule": "硬止损（单日）",
            "detail": f"单日跌幅 {daily_pct:.2f}%（现价¥{current_price:.2f}）",
        }
    return {"triggered": False, "level": 1, "rule": "硬止损", "detail": f"浮亏 {loss_pct}% 未触发"}


def check_time_stop(holding, current_price, check_date, trading_dates=None):
    """第二层：时间止损。D+3 < 2% 或 D+5 < 5% → 触发"""
    buy_date_str = holding.get("buy_date", "")
    if not buy_date_str:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": "无买入日期"}
    try:
        buy_date = datetime.strptime(buy_date_str, "%Y-%m-%d")
        check_dt = datetime.strptime(check_date, "%Y-%m-%d")
    except ValueError:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": "日期格式错误"}

    days = trading_age(buy_date_str, check_date, trading_dates)
    if days is None:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": "交易日历不足，未执行"}
    if days <= 0:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": f"D{days} 尚未满一日"}

    cost = holding.get("cost_price", 0)
    if cost <= 0:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": "数据不足"}

    gain_pct = round((current_price - cost) / cost * 100, 2)

    if days >= 5 and gain_pct < 5:
        return {
            "triggered": True,
            "level": 2,
            "rule": "时间止损 (D+5)",
            "detail": f"D{days} 累计涨幅 {gain_pct}%，未达 +5% 目标",
        }
    if days >= 3 and gain_pct < 2:
        return {
            "triggered": True,
            "level": 2,
            "rule": "时间止损 (D+3)",
            "detail": f"D{days} 累计涨幅 {gain_pct}%，未达 +2% 目标",
        }

    return {
        "triggered": False,
        "level": 2,
        "rule": "时间止损",
        "detail": f"D{days} 涨幅 {gain_pct}% — "
        f"{'D+3目标≥2%' if days < 5 else 'D+5目标≥5%'}",
    }


def check_sector_stop(holding, l2_boards):
    """第三层：板块止损。行业不在 TOP10/20 → 触发"""
    hybk = holding.get("hybk", "")
    if not hybk or not l2_boards:
        return {"triggered": False, "level": 3, "rule": "板块止损", "detail": "无行业数据"}

    boards = l2_boards.get("boards", [])
    # 防御：榜单不足 20 条时跳过（数据不全，避免误判"跌出 TOP20"集体误杀）
    if len(boards) < 20:
        return {
            "triggered": False, "level": 3, "rule": "板块止损",
            "detail": f"榜单仅 {len(boards)} 条（<20），数据不足跳过板块止损",
        }
    top10_names = [b["name"] for b in boards[:10]]
    top20_names = [b["name"] for b in boards[:20]]

    if hybk not in top20_names:
        return {
            "triggered": True,
            "level": 3,
            "rule": "板块止损 (TOP20)",
            "detail": f"「{hybk}」已跌出 TOP20 — 建议全部卖出",
        }
    if hybk not in top10_names:
        return {
            "triggered": True,
            "level": 3,
            "rule": "板块止损 (TOP10)",
            "detail": f"「{hybk}」掉出 TOP10 — 建议减半仓",
        }

    rank = next((i + 1 for i, b in enumerate(boards) if b["name"] == hybk), -1)
    return {
        "triggered": False,
        "level": 3,
        "rule": "板块止损",
        "detail": f"「{hybk}」排名 #{rank}，板块健康",
    }


def check_lb_break_stop(holding, l2_zb_pool):
    """第四层：炸板止损。持仓票在炸板池中 → 触发"""
    code = norm_code(holding.get("code", ""))
    if not code or not l2_zb_pool:
        return {"triggered": False, "level": 4, "rule": "炸板止损", "detail": "无炸板数据"}

    zb_stocks = l2_zb_pool.get("stocks", [])
    for zb in zb_stocks:
        if norm_code(zb.get("code", "")) == code:
            chg = zb.get("chg_pct")
            chg_str = f"{chg:.2f}%" if isinstance(chg, (int, float)) else f"{chg}%"
            return {
                "triggered": True,
                "level": 4,
                "rule": "炸板止损",
                "detail": f"{zb.get('name', code)} 炸板！封板时间 {zb.get('fbt', 'N/A')}，涨幅收至 {chg_str}",
            }

    return {"triggered": False, "level": 4, "rule": "炸板止损", "detail": "未炸板"}


def check_board_gradient_stop(holding, l2_zt_pool):
    """第五层：连板梯度止损。根据连板数调整硬止损阈值（展示用）"""
    code = norm_code(holding.get("code", ""))
    if not code or not l2_zt_pool:
        return {"triggered": False, "level": 5, "rule": "连板梯度止损", "detail": "无涨停池数据"}

    zt_stocks = l2_zt_pool.get("stocks", [])
    for zt in zt_stocks:
        if norm_code(zt.get("code", "")) == code:
            lb = zt.get("lb", 1)
            thresholds = {1: -3, 2: -5, 3: -7, 4: -8}
            threshold = thresholds.get(min(lb, 4), -8)
            return {
                "triggered": False,
                "level": 5,
                "rule": "连板梯度止损",
                "detail": f"{lb}板，硬止损阈值 {threshold}%",
            }

    return {"triggered": False, "level": 5, "rule": "连板梯度止损", "detail": "不在今日涨停池"}


# ── 移动止盈（第六层·影子模式：只计算记录，不真实卖出）──────────
# 规则（参数可配）：浮盈 ≥ +20% 启用；从持有期最高价回吐 ≥ 1/3 → 减半档；
# 回吐 ≥ 1/2 → 清仓档。先影子跑三周，拿"如果生效会怎样"的数据再决定启用。
TRAIL_ACTIVATE = 0.20    # 浮盈启用阈值
TRAIL_HALF = 1 / 3       # 回吐 1/3 → 减半
TRAIL_FULL = 1 / 2       # 回吐 1/2 → 清仓


def fetch_tencent_highs(code: str, since_date: str):
    """拉腾讯日K线，返回 (peak_high, cur_close)——持有期最高价与最新收盘"""
    import urllib.request
    from datetime import datetime
    try:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={code},day,,,320,qfq")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = json.loads(opener.open(req, timeout=10).read().decode("utf-8"))
        klines = (data.get("data", {}).get(code, {}).get("qfqday")
                  or data.get("data", {}).get(code, {}).get("day") or [])
        highs = [float(k[3]) for k in klines if k[0] >= since_date]
        closes = [float(k[2]) for k in klines if k[0] >= since_date]
        if not highs:
            return None, None
        return max(highs), closes[-1]
    except Exception:
        return None, None


def check_trailing_stop(cur: float, peak: float, cost: float):
    """
    移动止盈判定。返回 None（未触发）或 {"trigger": "清仓"|"减半", "gain_pct", "drawdown_pct"}。

    启用判定用"历史最大浮盈"（peak 相对 cost）而非当前浮盈：
    否则"涨上去又跌回来"（收益回吐）的场景永远不触发——那正是本规则要防的。
    """
    if cost <= 0 or cur <= 0 or peak is None or peak <= 0:
        return None
    peak_gain = (peak - cost) / cost          # 历史最大浮盈
    if peak_gain < TRAIL_ACTIVATE:
        return None  # 从未达到启用线
    cur_gain = (cur - cost) / cost
    drawdown = (peak - cur) / peak
    if drawdown >= TRAIL_FULL:
        return {"trigger": "清仓", "gain_pct": round(cur_gain * 100, 2),
                "drawdown_pct": round(drawdown * 100, 2)}
    if drawdown >= TRAIL_HALF:
        return {"trigger": "减半", "gain_pct": round(cur_gain * 100, 2),
                "drawdown_pct": round(drawdown * 100, 2)}
    return None


# ── 时间止损模式（保留默认值；旧切换依据已撤回，2026-09-29）────
# 2026-09-23 切换 D1 所用“中位 +1.17%、按日胜 73%”已撤回：
# 原研究混入观察/门控禁止信号，并假设收盘信号可按信号日收盘买入。
# 不得继续引用它证明 D1 显著优于 D3D5；原胜率、收益及路径占比均非有效依据。
# 新口径见 data/execution_review.json / AUDIT_REPAIR.md：
# 严格筛选后的共同样本为 82 条、6 个信号日；D1 按日胜出 3/6（50%）。
# 配对扣费中位数：D1 -2.4385%，D3D5 -3.8294%。50%不是收益相等，
# 也不是两者等效的统计证明；小样本历史诊断不证明任何版本有效。
# 此处 D3D5 比较仅针对时间退出，不是完整五层策略的回放。
# 按用户决定保留 D1，不自动回退，不改买入时点；保留配置不代表研究背书。
# 本变量只选择卖出模式，不是全局停买开关；门控放行时原买入逻辑仍会运行。
TIME_STOP_MODE = "D1"   # D1=买入后的交易日收盘退出；D3D5=D+3<2%/D+5<5%


# ── 执行入口 ──────────────────────────────────────────────

def run_stop_loss(holdings, prices, l2_boards, l2_zt_pool, l2_zb_pool, check_date,
                  l2_dt_pool=None, trading_dates=None):
    """Return (sells, deferred). prices must be date-validated by the quote adapter.

    D1 timing and the existing oc-based fill model are unchanged. Data failures
    never produce executions; established exit intentions survive retries.
    """
    sells, deferred = [], []
    pool_ok = (isinstance(l2_dt_pool, dict) and l2_dt_pool.get("status") == "ok"
               and l2_dt_pool.get("date") == check_date
               and isinstance(l2_dt_pool.get("stocks"), list))
    dt_by_code = {norm_code(d.get("code")): d for d in l2_dt_pool["stocks"]} if pool_ok else {}

    for h in holdings:
        code = h.get("code", "")
        if not h.get("buy_date") or h["buy_date"] >= check_date:
            continue
        pending = h.get("pending_exit") or {}
        reasons = pending.get("reasons", [])
        half = pending.get("half", False)
        if TIME_STOP_MODE == "D1":
            reasons = ["时间止损 (D+1快走): 买入后的交易日收盘退出"]
            half = False
        cur = prices.get(code)
        cost = h.get("cost") or h.get("cost_price") or h.get("buy_price") or 0

        def defer(reason, message, **extra):
            deferred.append({"date": check_date, "code": code, "name": h.get("name", ""),
                             "reason": reason, "reason_text": message,
                             "price": cur if valid_price(cur) else None,
                             "trigger_reasons": reasons, "half": half, **extra})

        if not valid_price(cur):
            defer("missing_quote", "无有效当日报价，未成交，保留持仓")
            continue
        if TIME_STOP_MODE != "D1":
            norm = {**h, "cost_price": cost}
            checks = [check_hard_stop(norm, cur),
                      check_time_stop(norm, cur, check_date, trading_dates),
                      check_sector_stop(norm, l2_boards),
                      check_lb_break_stop(norm, l2_zb_pool),
                      check_board_gradient_stop(norm, l2_zt_pool)]
            triggered = [c for c in checks if c.get("triggered")]
            if triggered:
                fresh_reasons = [f"{c['rule']}: {c['detail']}" for c in triggered]
                fresh_half = all(c["rule"] == "板块止损 (TOP10)" for c in triggered)
                half = fresh_half and (not reasons or half)
                reasons = reasons + fresh_reasons
            if not reasons:
                if trading_age(h["buy_date"], check_date, trading_dates) is None:
                    defer("missing_calendar", "交易日历不足，时间止损未执行")
                continue
        if not pool_ok:
            defer("market_data_unavailable", "当日跌停池未验证，未成交，保留退出意图")
            continue
        dt = dt_by_code.get(norm_code(code))
        sell_price = cur
        if dt is not None:
            try:
                oc = int(dt.get("oc", 0) or 0)
            except (ValueError, TypeError):
                oc = 0
            if oc <= 0:
                defer(1, "跌停封死，排队未成交，次日重试", oc=0,
                      days=dt.get("days", 1), fund=dt.get("fund", 0))
                continue
            sell_price = dt.get("price")
            if not valid_price(sell_price):
                defer("invalid_limit_price", "跌停池价格无效，未成交")
                continue
        shares = h.get("shares", 0)
        sell_shares = (shares // 200) * 100 if half else shares
        if half and sell_shares < 100:
            sell_shares, half = shares, False
        sells.append({"code": code, "name": h.get("name", ""), "shares": sell_shares,
                      "price": sell_price, "amount": round(sell_price * sell_shares, 2),
                      "cost_price": cost, "half": half, "reasons": reasons})
    return sells, deferred


if __name__ == "__main__":
    # 自测：空数据不崩
    sells, missed = run_stop_loss([], {}, None, None, None, "2026-08-11")
    print(f"空持仓测试: {len(sells)} 个卖出建议（应为 0），{len(missed)} 个拒卖")
