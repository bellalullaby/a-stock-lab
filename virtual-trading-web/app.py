"""
A股虚拟盘 Web 系统 - Flask 后端
双账户对比：酱酱手动盘 (SQLite) + 小克纪律盘 (portfolio.json)
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# UTF-8 stdout 防 GBK emoji 崩溃
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from flask import Flask, jsonify, render_template, request

# ── Paths ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "cache"
ACCOUNTS_DIR = BASE_DIR / "accounts"
JJ_DB = ACCOUNTS_DIR / "jiangjiang.db"
XK_PORTFOLIO = BASE_DIR.parent / "virtual-portfolio" / "portfolio.json"
COLLECT_LOCK = ACCOUNTS_DIR / ".collect.lock"

# ── Flask app ──────────────────────────────────────────────
app = Flask(__name__)


# ═══════════════════════════════════════════════════════════
#  STARTUP: auto-init accounts/ + SQLite
# ═══════════════════════════════════════════════════════════

def init_accounts():
    """首次启动：创建 accounts/ 目录 + 初始化 SQLite + 写 100 万初始快照"""
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(JJ_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS holdings (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            shares INTEGER NOT NULL,
            cost_price REAL NOT NULL,
            buy_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('buy','sell')),
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            date TEXT PRIMARY KEY,
            total_value REAL NOT NULL,
            cash REAL NOT NULL,
            holdings_value REAL NOT NULL,
            pnl REAL NOT NULL,
            pnl_pct REAL NOT NULL
        );
    """)

    # 幂等：只在没有快照时写入初始值
    existing = conn.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0]
    if existing == 0:
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO daily_snapshots (date, total_value, cash, holdings_value, pnl, pnl_pct) "
            "VALUES (?, 1000000.0, 1000000.0, 0.0, 0.0, 0.0)",
            (today,),
        )
        conn.commit()

    conn.close()
    print(f"[init] accounts/ ready, DB at {JJ_DB}")


# ── Auto-init on import ────────────────────────────────────
init_accounts()


# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════

def get_db():
    """获取 SQLite 连接（每次请求新连接，确保线程安全）"""
    conn = sqlite3.connect(str(JJ_DB))
    conn.row_factory = sqlite3.Row
    return conn


def load_json(path):
    """安全读 JSON，不存在返回 None"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def list_cache_dates():
    """遍历 data/cache/ 返回所有日期目录，按日期倒序"""
    if not CACHE_DIR.exists():
        return []
    dates = []
    for d in CACHE_DIR.iterdir():
        if d.is_dir():
            dates.append(d.name)
    dates.sort(reverse=True)
    return dates


def cache_date_path(date_str):
    """返回某日期的缓存目录路径"""
    return CACHE_DIR / date_str


CACHE_FILES = [
    "l1_index.json",
    "l2_boards.json",
    "l2_zt_pool.json",
    "l2_zb_pool.json",
    "l2_rotation.json",
    "l3_stocks.json",
]


def cache_completeness(date_str):
    """检查某日期缓存是否完整（6 文件都存在）"""
    d = cache_date_path(date_str)
    missing = [f for f in CACHE_FILES if not (d / f).exists()]
    return {
        "date": date_str,
        "complete": len(missing) == 0,
        "missing": missing,
        "files": [f for f in CACHE_FILES if (d / f).exists()],
    }


def load_xk_portfolio():
    """读小克 portfolio.json，不存在则返回空模板"""
    data = load_json(XK_PORTFOLIO)
    if data is None:
        return {
            "cash": 1000000.0,
            "total_value": 1000000.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "holdings": [],
            "trades": [],
            "daily_snapshots": [],
        }
    return data


def get_xk_snapshots():
    """从 portfolio.json 提取小克每日快照"""
    pf = load_xk_portfolio()
    return pf.get("daily_snapshots", [])


# ═══════════════════════════════════════════════════════════
#  PAGE
# ═══════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════
#  API 1: /api/status — 双账户概览
# ═══════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    # 酱酱：取最新 daily_snapshot
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row:
            jj = {
                "total_value": row["total_value"],
                "cash": row["cash"],
                "holdings_value": row["holdings_value"],
                "pnl": row["pnl"],
                "pnl_pct": row["pnl_pct"],
                "date": row["date"],
            }
        else:
            jj = {"total_value": 1000000, "cash": 1000000, "holdings_value": 0, "pnl": 0, "pnl_pct": 0, "date": ""}
    finally:
        conn.close()

    # 小克：从 portfolio.json 取最新 snapshot
    xk_data = load_xk_portfolio()
    snaps = xk_data.get("daily_snapshots", [])
    if snaps:
        latest = snaps[-1]
        xk = {
            "total_value": latest.get("total_value", 1000000),
            "cash": latest.get("cash", 1000000),
            "holdings_value": latest.get("holdings_value", 0),
            "pnl": latest.get("pnl", 0),
            "pnl_pct": latest.get("pnl_pct", 0),
            "date": latest.get("date", ""),
        }
    else:
        xk = {"total_value": 1000000, "cash": 1000000, "holdings_value": 0, "pnl": 0, "pnl_pct": 0, "date": ""}

    # 酱酱持仓数
    conn = get_db()
    try:
        jj_count = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    finally:
        conn.close()

    return jsonify({
        "jiangjiang": jj,
        "xiaoke": xk,
        "jj_holdings_count": jj_count,
        "xk_holdings_count": len(xk_data.get("holdings", [])),
    })


# ═══════════════════════════════════════════════════════════
#  API 2: /api/signals?date= — 某日信号池
# ═══════════════════════════════════════════════════════════

@app.route("/api/signals")
def api_signals():
    date_str = request.args.get("date", "")
    if not date_str:
        # 默认最新日期
        dates = list_cache_dates()
        date_str = dates[0] if dates else ""

    data = load_json(cache_date_path(date_str) / "l3_stocks.json")
    l1 = load_json(cache_date_path(date_str) / "l1_index.json")

    if data is None:
        return jsonify({"date": date_str, "total": 0, "stocks": [], "error": "无缓存数据"})

    return jsonify({
        "date": data.get("date", date_str),
        "total": data.get("total", 0),
        "l1_regime": data.get("l1_regime", ""),
        "summary": data.get("summary", {}),
        "volume_analysis": l1.get("volume_analysis", "") if l1 else "",
        "stocks": data.get("stocks", []),
    })


# ═══════════════════════════════════════════════════════════
#  API 3: /api/holdings/jj — 酱酱持仓
# ═══════════════════════════════════════════════════════════

@app.route("/api/holdings/jj")
def api_holdings_jj():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM holdings").fetchall()
        holdings = [dict(r) for r in rows]
    finally:
        conn.close()
    return jsonify({"holdings": holdings})


# ═══════════════════════════════════════════════════════════
#  API 4: /api/holdings/xk — 小克持仓
# ═══════════════════════════════════════════════════════════

@app.route("/api/holdings/xk")
def api_holdings_xk():
    pf = load_xk_portfolio()
    return jsonify({"holdings": pf.get("holdings", [])})


# ═══════════════════════════════════════════════════════════
#  API 5: /api/trades/jj — 酱酱交易记录
# ═══════════════════════════════════════════════════════════

@app.route("/api/trades/jj")
def api_trades_jj():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 200").fetchall()
        trades = [dict(r) for r in rows]
    finally:
        conn.close()
    return jsonify({"trades": trades})


# ═══════════════════════════════════════════════════════════
#  API 6: /api/trades/xk — 小克交易记录
# ═══════════════════════════════════════════════════════════

@app.route("/api/trades/xk")
def api_trades_xk():
    pf = load_xk_portfolio()
    return jsonify({"trades": pf.get("trades", [])})


# ═══════════════════════════════════════════════════════════
#  API 7: POST /api/trade — 酱酱提交交易
# ═══════════════════════════════════════════════════════════

@app.route("/api/trade", methods=["POST"])
def api_trade():
    body = request.get_json()
    if not body:
        return jsonify({"ok": False, "error": "请求体为空"}), 400

    trade_type = body.get("type")  # "buy" or "sell"
    code = body.get("code", "").strip()
    name = body.get("name", "").strip()
    try:
        price = float(body.get("price", 0))
        shares = int(body.get("shares", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "价格或股数无效"}), 400

    if trade_type not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "交易类型只能是 buy 或 sell"}), 400
    if not code or not name:
        return jsonify({"ok": False, "error": "代码和名称不能为空"}), 400
    if price <= 0 or shares <= 0:
        return jsonify({"ok": False, "error": "价格和股数必须大于0"}), 400

    today = datetime.now().strftime("%Y-%m-%d")
    amount = round(price * shares, 2)

    conn = get_db()
    try:
        # 取最新资金
        snap = conn.execute(
            "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not snap:
            return jsonify({"ok": False, "error": "账户未初始化"}), 500

        cash = snap["cash"]

        if trade_type == "buy":
            if amount > cash:
                return jsonify({"ok": False, "error": f"资金不足（需要 ¥{amount:,.2f}，可用 ¥{cash:,.2f}）"}), 400

            # 更新持仓（已持有则合并）
            existing = conn.execute(
                "SELECT * FROM holdings WHERE code = ?", (code,)
            ).fetchone()
            if existing:
                total_shares = existing["shares"] + shares
                total_cost = existing["cost_price"] * existing["shares"] + price * shares
                new_cost = round(total_cost / total_shares, 4)
                conn.execute(
                    "UPDATE holdings SET shares = ?, cost_price = ? WHERE code = ?",
                    (total_shares, new_cost, code),
                )
            else:
                conn.execute(
                    "INSERT INTO holdings (code, name, shares, cost_price, buy_date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, name, shares, price, today),
                )

            # 记交易 + 扣现金
            new_cash = round(cash - amount, 2)
            conn.execute(
                "INSERT INTO trades (date, type, code, name, price, shares, amount, note) "
                "VALUES (?, 'buy', ?, ?, ?, ?, ?, '手动买入')",
                (today, code, name, price, shares, amount),
            )

        else:  # sell
            existing = conn.execute(
                "SELECT * FROM holdings WHERE code = ?", (code,)
            ).fetchone()
            if not existing:
                return jsonify({"ok": False, "error": f"持仓中没有 {code} {name}"}), 400
            if shares > existing["shares"]:
                return jsonify({"ok": False, "error": f"卖出股数({shares})超过持仓({existing['shares']})"}), 400

            if shares == existing["shares"]:
                conn.execute("DELETE FROM holdings WHERE code = ?", (code,))
            else:
                conn.execute(
                    "UPDATE holdings SET shares = ? WHERE code = ?",
                    (existing["shares"] - shares, code),
                )

            new_cash = round(cash + amount, 2)
            cost = existing["cost_price"]
            pnl = round((price - cost) * shares, 2)
            pnl_pct = round((price - cost) / cost * 100, 2)
            note = f"手动卖出 | 成本 ¥{cost} → 卖价 ¥{price} | 盈亏 ¥{pnl} ({pnl_pct:+.1f}%)"
            conn.execute(
                "INSERT INTO trades (date, type, code, name, price, shares, amount, note) "
                "VALUES (?, 'sell', ?, ?, ?, ?, ?, ?)",
                (today, code, name, price, shares, amount, note),
            )

        # 重算持仓市值 + 写 snapshot
        holdings_rows = conn.execute("SELECT * FROM holdings").fetchall()
        total_holdings_value = 0.0
        for h in holdings_rows:
            # 尝试从最新缓存取现价，取不到用成本价
            latest_price = h["cost_price"]
            dates = list_cache_dates()
            if dates:
                l3 = load_json(cache_date_path(dates[0]) / "l3_stocks.json")
                if l3:
                    for s in l3.get("stocks", []):
                        if s.get("code") == h["code"]:
                            latest_price = s.get("price", h["cost_price"])
                            break
            total_holdings_value += round(latest_price * h["shares"], 2)

        total_value = round(new_cash + total_holdings_value, 2)
        initial_capital = 1000000.0
        pnl = round(total_value - initial_capital, 2)
        pnl_pct = round(pnl / initial_capital * 100, 2)

        conn.execute(
            "INSERT OR REPLACE INTO daily_snapshots (date, total_value, cash, holdings_value, pnl, pnl_pct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (today, total_value, new_cash, total_holdings_value, pnl, pnl_pct),
        )
        conn.commit()

        return jsonify({
            "ok": True,
            "type": trade_type,
            "code": code,
            "name": name,
            "price": price,
            "shares": shares,
            "amount": amount,
            "new_cash": new_cash,
            "new_total_value": total_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
#  API 8: /api/compare — 双账户收益对比
# ═══════════════════════════════════════════════════════════

@app.route("/api/compare")
def api_compare():
    # 酱酱 daily_snapshots
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT date, total_value, pnl, pnl_pct FROM daily_snapshots ORDER BY date ASC"
        ).fetchall()
        jj_snaps = [dict(r) for r in rows]
    finally:
        conn.close()

    # 小克 daily_snapshots（从 portfolio.json）
    xk_snaps = get_xk_snapshots()

    return jsonify({
        "jiangjiang": jj_snaps,
        "xiaoke": xk_snaps,
    })


# ═══════════════════════════════════════════════════════════
#  API 9: /api/rotation?date= — 轮动速度 + 板块连续性
# ═══════════════════════════════════════════════════════════

@app.route("/api/rotation")
def api_rotation():
    date_str = request.args.get("date", "")
    if not date_str:
        dates = list_cache_dates()
        date_str = dates[0] if dates else ""

    data = load_json(cache_date_path(date_str) / "l2_rotation.json")
    if data is None:
        return jsonify({"error": "无缓存数据"})

    return jsonify(data)


# ═══════════════════════════════════════════════════════════
#  API 10: /api/stop-loss/jj — 酱酱持仓止损状态
#  (Claude哥提示：五层止损各自封装成独立函数)
# ═══════════════════════════════════════════════════════════

def check_hard_stop(holding, current_price):
    """第一层：硬止损。现价 ≤ 成本 × 0.92 → 触发"""
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


def check_time_stop(holding, check_date):
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

    # 从缓存取现价
    dates = list_cache_dates()
    current_price = cost
    if dates:
        l3 = load_json(cache_date_path(dates[0]) / "l3_stocks.json")
        if l3:
            for s in l3.get("stocks", []):
                if s.get("code") == holding.get("code"):
                    current_price = s.get("price", cost)
                    break

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

    # 找到排名
    rank = next((i + 1 for i, b in enumerate(boards) if b["name"] == hybk), -1)
    return {
        "triggered": False,
        "level": 3,
        "rule": "板块止损",
        "detail": f"「{hybk}」排名 #{rank}，板块健康",
    }


def check_lb_break_stop(holding, l2_zb_pool):
    """第四层：炸板止损。持仓票在炸板池中 → 触发"""
    code = holding.get("code", "")
    if not code or not l2_zb_pool:
        return {"triggered": False, "level": 4, "rule": "炸板止损", "detail": "无炸板数据"}

    zb_stocks = l2_zb_pool.get("stocks", [])
    for zb in zb_stocks:
        if zb.get("code") == code:
            return {
                "triggered": True,
                "level": 4,
                "rule": "炸板止损",
                "detail": f"{zb.get('name', code)} 炸板！封板时间 {zb.get('fbt', 'N/A')}，涨幅收至 {zb.get('chg_pct', 'N/A')}%",
            }

    return {"triggered": False, "level": 4, "rule": "炸板止损", "detail": "未炸板"}


def check_board_gradient_stop(holding, l2_zt_pool):
    """第五层：连板梯度止损。根据连板数调整硬止损阈值"""
    code = holding.get("code", "")
    if not code or not l2_zt_pool:
        return {"triggered": False, "level": 5, "rule": "连板梯度止损", "detail": "无涨停池数据"}

    zt_stocks = l2_zt_pool.get("stocks", [])
    for zt in zt_stocks:
        if zt.get("code") == code:
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


@app.route("/api/stop-loss/jj")
def api_stop_loss_jj():
    """酱酱持仓止损状态 — 逐条对照五层止损规则"""
    # 读取持仓
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM holdings").fetchall()
        holdings = [dict(r) for r in rows]
    finally:
        conn.close()

    if not holdings:
        return jsonify({"holdings": [], "any_triggered": False, "message": "酱酱还没有持仓～"})

    # 取最新缓存日期
    dates = list_cache_dates()
    check_date = dates[0] if dates else datetime.now().strftime("%Y-%m-%d")

    # 读 L2/L3 数据
    l3_stocks = load_json(cache_date_path(check_date) / "l3_stocks.json")
    l2_boards = load_json(cache_date_path(check_date) / "l2_boards.json")
    l2_rotation = load_json(cache_date_path(check_date) / "l2_rotation.json")
    l2_zb_pool = load_json(cache_date_path(check_date) / "l2_zb_pool.json")
    l2_zt_pool = load_json(cache_date_path(check_date) / "l2_zt_pool.json")

    # 为每只持仓找 hybk（从涨停池/信号池匹配）
    # 优先从 l3_stocks 中匹配
    code_to_hybk = {}
    code_to_current_price = {}
    if l3_stocks:
        for s in l3_stocks.get("stocks", []):
            code_to_hybk[s["code"]] = s.get("hybk", "")
            code_to_current_price[s["code"]] = s.get("price", 0)
    # 补充从涨停池匹配
    if l2_zt_pool:
        for s in l2_zt_pool.get("stocks", []):
            if s["code"] not in code_to_hybk:
                code_to_hybk[s["code"]] = s.get("hybk", "")
            if s["code"] not in code_to_current_price:
                code_to_current_price[s["code"]] = s.get("price", 0)
    # 补充从炸板池匹配
    if l2_zb_pool:
        for s in l2_zb_pool.get("stocks", []):
            if s["code"] not in code_to_hybk:
                code_to_hybk[s["code"]] = s.get("hybk", "")

    results = []
    any_triggered = False

    for h in holdings:
        code = h["code"]
        current_price = code_to_current_price.get(code, h["cost_price"])
        h_with_hybk = {**h, "hybk": code_to_hybk.get(code, ""), "current_price": current_price}

        hard = check_hard_stop(h, current_price)
        time_stop = check_time_stop(h, check_date)
        sector = check_sector_stop(h_with_hybk, l2_boards)
        lb_break = check_lb_break_stop(h, l2_zb_pool)
        gradient = check_board_gradient_stop(h, l2_zt_pool)

        checks = [hard, time_stop, sector, lb_break, gradient]
        triggered = [c for c in checks if c["triggered"]]
        if triggered:
            any_triggered = True

        # 综合状态颜色
        if hard["triggered"] or lb_break["triggered"]:
            status_color = "red"
            status_text = "🔴 触发卖出"
        elif time_stop["triggered"] or sector["triggered"]:
            status_color = "orange"
            status_text = "🟠 预警"
        elif current_price < h["cost_price"]:
            status_color = "yellow"
            status_text = "🟡 浮亏"
        else:
            status_color = "green"
            status_text = "🟢 安全"

        results.append({
            "code": code,
            "name": h["name"],
            "cost_price": h["cost_price"],
            "current_price": current_price,
            "buy_date": h["buy_date"],
            "shares": h["shares"],
            "hybk": code_to_hybk.get(code, ""),
            "status_color": status_color,
            "status_text": status_text,
            "checks": checks,
            "triggered_count": len(triggered),
        })

    return jsonify({
        "check_date": check_date,
        "holdings": results,
        "any_triggered": any_triggered,
        "l1_regime": l3_stocks.get("l1_regime", "") if l3_stocks else "",
    })


# ═══════════════════════════════════════════════════════════
#  API 11: /api/dates — 有缓存的日期列表
#  (Claude哥提示：标注每个日期缓存是否完整，不完整的灰色置底)
# ═══════════════════════════════════════════════════════════

@app.route("/api/dates")
def api_dates():
    dates = list_cache_dates()
    result = [cache_completeness(d) for d in dates]

    # 完整的排前，不完整的置底
    result.sort(key=lambda x: (not x["complete"], x["date"]), reverse=False)
    # complete=True 的在前面，然后按日期降序

    return jsonify({"dates": result, "latest": dates[0] if dates else ""})


# ═══════════════════════════════════════════════════════════
#  API 12: POST /api/collect — 手动触发数据采集
#  (Claude哥提示：用 sys.executable，加锁文件防重复触发)
# ═══════════════════════════════════════════════════════════

@app.route("/api/collect", methods=["POST"])
def api_collect():
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    # 检查锁文件
    if COLLECT_LOCK.exists():
        try:
            lock_time = datetime.fromtimestamp(COLLECT_LOCK.stat().st_mtime)
            age = (now - lock_time).total_seconds()
            if age < 300:  # 5 分钟内视为仍在运行
                return jsonify({
                    "ok": False,
                    "reason": "采集进行中",
                    "detail": f"锁文件存在 {age:.0f} 秒，请等待完成后重试",
                })
            else:
                # 锁过期，删除
                COLLECT_LOCK.unlink()
        except Exception:
            pass

    # 写锁文件
    COLLECT_LOCK.write_text(f"started at {now.isoformat()}")

    try:
        # 用 sys.executable 获取 Python 路径
        collector = BASE_DIR / "data_collector.py"
        if not collector.exists():
            COLLECT_LOCK.unlink()
            return jsonify({"ok": False, "reason": f"未找到 {collector}"}), 500

        # 后台执行
        subprocess.Popen(
            [sys.executable, str(collector), "--date", date_str],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({
            "ok": True,
            "date": date_str,
            "message": f"开始采集 {date_str} 数据，请等待约 30-60 秒后刷新",
        })
    except Exception as e:
        # 失败时清理锁文件
        if COLLECT_LOCK.exists():
            try:
                COLLECT_LOCK.unlink()
            except Exception:
                pass
        return jsonify({"ok": False, "reason": str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  API 13: /api/collect/status — 查询采集状态（解锁）
# ═══════════════════════════════════════════════════════════

@app.route("/api/collect/status")
def api_collect_status():
    """检查采集状态。锁文件存在但今天缓存已完整 → 视为完成"""
    today = datetime.now().strftime("%Y-%m-%d")
    completeness = cache_completeness(today)

    # 今天缓存 6 文件齐全 → 采集已完成（不管锁还在不在）
    if completeness["complete"]:
        # 清理残留锁文件
        if COLLECT_LOCK.exists():
            try:
                COLLECT_LOCK.unlink()
            except Exception:
                pass
        return jsonify({"running": False, "complete": True, "date": today})

    # 锁文件存在且未过期 → 仍在运行
    if COLLECT_LOCK.exists():
        try:
            lock_age = time.time() - COLLECT_LOCK.stat().st_mtime
            if lock_age < 600:  # 10 分钟内
                return jsonify({"running": True, "date": today})
            else:
                # 过期锁，清理
                COLLECT_LOCK.unlink()
        except Exception:
            pass

    return jsonify({"running": False, "complete": False, "date": today})


# ═══════════════════════════════════════════════════════════
#  API 14: /api/l1?date= — L1 宏观门控数据（前端面板用）
# ═══════════════════════════════════════════════════════════

@app.route("/api/l1")
def api_l1():
    date_str = request.args.get("date", "")
    if not date_str:
        dates = list_cache_dates()
        date_str = dates[0] if dates else ""

    data = load_json(cache_date_path(date_str) / "l1_index.json")
    if data is None:
        return jsonify({"error": "无缓存数据"})

    return jsonify(data)


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🏦 A股虚拟盘 Web 系统启动中...")
    print(f"   📁 缓存目录: {CACHE_DIR}")
    print(f"   📁 账户目录: {ACCOUNTS_DIR}")
    print(f"   🌐 http://localhost:5000")
    app.run(debug=False, host="127.0.0.1", port=5000)
