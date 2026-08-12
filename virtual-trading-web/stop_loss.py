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

import urllib.request
from datetime import datetime


# ── 工具函数 ──────────────────────────────────────────────

def norm_code(code: str) -> str:
    """归一化代码：sh600272 / sz002721 / 600272 → 600272"""
    c = str(code or "")
    for prefix in ("sh", "sz", "bj"):
        if c.startswith(prefix):
            return c[2:]
    return c


def fetch_tencent_prices(codes):
    """批量拉腾讯实时行情，返回 {原始code: 现价}（code 带 sh/sz 前缀）"""
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
                prices[key] = float(vals[3])
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
    if loss_pct <= -5:
        return {
            "triggered": True,
            "level": 1,
            "rule": "硬止损（单日）",
            "detail": f"单日跌幅 {loss_pct}%（现价¥{current_price:.2f}）",
        }
    return {"triggered": False, "level": 1, "rule": "硬止损", "detail": f"浮亏 {loss_pct}% 未触发"}


def check_time_stop(holding, current_price, check_date):
    """第二层：时间止损。D+3 < 2% 或 D+5 < 5% → 触发"""
    buy_date_str = holding.get("buy_date", "")
    if not buy_date_str:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": "无买入日期"}
    try:
        buy_date = datetime.strptime(buy_date_str, "%Y-%m-%d")
        check_dt = datetime.strptime(check_date, "%Y-%m-%d")
    except ValueError:
        return {"triggered": False, "level": 2, "rule": "时间止损", "detail": "日期格式错误"}

    days = (check_dt - buy_date).days
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
            return {
                "triggered": True,
                "level": 4,
                "rule": "炸板止损",
                "detail": f"{zb.get('name', code)} 炸板！封板时间 {zb.get('fbt', 'N/A')}，涨幅收至 {zb.get('chg_pct', 'N/A')}%",
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


# ── 执行入口 ──────────────────────────────────────────────

def run_stop_loss(holdings, prices, l2_boards, l2_zt_pool, l2_zb_pool, check_date):
    """
    对持仓逐只跑五层止损检查，返回卖出建议列表。

    参数:
      holdings   : portfolio.json 的 holdings 列表（code 带前缀）
      prices     : fetch_tencent_prices() 结果 {code: 现价}
      l2_boards  : l2_boards.json 解析结果（板块排名）
      l2_zt_pool : l2_zt_pool.json 解析结果（涨停池）
      l2_zb_pool : l2_zb_pool.json 解析结果（炸板池）
      check_date : 检查日期 "YYYY-MM-DD"

    返回:
      [{code, name, shares, price, amount, cost_price, reasons: [触发详情...]}, ...]
    """
    sells = []
    for h in holdings:
        code = h.get("code", "")
        # 补 cost_price 字段（portfolio.json 存的是 cost/buy_price）
        cost = h.get("cost") or h.get("cost_price") or h.get("buy_price") or 0
        holding_norm = {**h, "cost_price": cost}

        # 现价：优先腾讯实时，拿不到用成本
        cur = prices.get(code) or cost
        shares = h.get("shares", 0)

        # 逐层检查
        checks = [
            check_hard_stop(holding_norm, cur),
            check_time_stop(holding_norm, cur, check_date),
            check_sector_stop(holding_norm, l2_boards),
            check_lb_break_stop(holding_norm, l2_zb_pool),
            check_board_gradient_stop(holding_norm, l2_zt_pool),
        ]
        # 买入当天（D+0）跳过板块止损：当天收盘封板买入，行业排名靠后
        # 不代表板块转弱，D+1 起才按板块排名判断（否则刚买入就被误杀）
        if h.get("buy_date") == check_date:
            checks[2] = {
                "triggered": False, "level": 3, "rule": "板块止损",
                "detail": "买入当天跳过板块止损（D+1 起生效）",
            }
        triggered = [c for c in checks if c.get("triggered")]

        if triggered:
            reasons = [f"{c['rule']}: {c['detail']}" for c in triggered]
            # 半仓规则：只有"纯板块 TOP10 触发"才减半仓
            # 硬止损/时间止损/炸板/TOP20 任一触发 → 全卖（亏损在扩大不能留半仓）
            strict_full = any(
                "硬止损" in c["rule"] or "时间止损" in c["rule"]
                or "炸板" in c["rule"] or "板块止损 (TOP20)" in c["rule"]
                for c in triggered
            )
            half = (not strict_full) and any("板块止损 (TOP10)" in c["rule"] for c in triggered)
            sell_shares = shares // 2 if half else shares
            # 减半后不足 1 手（100股）则全卖，避免留碎股
            if half and sell_shares < 100:
                sell_shares = shares
            sells.append({
                "code": code,
                "name": h.get("name", ""),
                "shares": sell_shares,
                "price": cur,
                "amount": round(cur * sell_shares, 2),
                "cost_price": cost,
                "half": half,          # 半仓标记：调用方要保留剩余持仓
                "reasons": reasons,
            })
    return sells


if __name__ == "__main__":
    # 自测：空数据不崩
    r = run_stop_loss([], {}, None, None, None, "2026-08-11")
    print(f"空持仓测试: {len(r)} 个卖出建议（应为 0）")
