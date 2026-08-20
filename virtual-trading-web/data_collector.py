# -*- coding: utf-8 -*-
"""
data_collector.py — A股虚拟盘数据采集脚本（第一期）
==================================================
收盘后拉取 L1/L2/L3 数据，生成缓存供 Web 系统使用。

用法:
    python data_collector.py                           # 拉取今天的数据
    python data_collector.py --date 2026-07-20         # 拉取指定日期

输出:
    data/cache/YYYY-MM-DD/
    ├── l1_index.json       # 五大指数 MA20/MA60 + 门控判定 + 量价分析
    ├── l2_boards.json      # 行业板块涨幅 TOP10
    ├── l2_zt_pool.json     # 涨停池全量（东财 push2ex）
    ├── l2_zb_pool.json     # 炸板池（东财 push2ex）
    └── l3_stocks.json      # 涨停票 qt 数据 + K线均线 + SOP 打分

API 数据源:
    - 腾讯 ifzq.gtimg.cn  → 日K线（MA 计算），无频率限制
    - 腾讯 qt.gtimg.cn     → 实时快照（价格/PE/市值/换手率），支持批量
    - 东财 push2ex         → 涨停池/炸板池，间隔 ≥1.5s，必须带 Referer
    - 东财 clist/get       → 行业板块 TOP10 + 概念板块搜索

参考:
    - SPEC.md v1.4（2026-07-20 晚）
    - backtest/backtest_runner.py（K线+MA 已验证模式）

作者: cc · 2026-07-20
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Git Bash GBK 编码兼容：防止 emoji/特殊字符崩溃
if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != "utf-8":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except (ValueError, AttributeError):
        pass

import requests

# ═══════════════════════════════════════════════════════════════
# 路径 & 常量
# ═══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
CACHE_ROOT = BASE_DIR / "data" / "cache"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 东财接口限流间隔（秒）
EM_GAP = 1.5

# 东财请求头（必须带 Referer，否则 404）
EM_HEADERS = {
    "Referer": "https://data.eastmoney.com/",
    "User-Agent": UA,
}

# ═══════════════════════════════════════════════════════════════
# 三大指数 — L1 核心判定（腾讯 K 线 API，✅ 已验证可用）
# ═══════════════════════════════════════════════════════════════

MAIN_INDICES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}

# ═══════════════════════════════════════════════════════════════
# 辅助指数 — L1 门控降级信号（东财概念板块当日涨跌幅，无 K 线）
# ═══════════════════════════════════════════════════════════════

# 这些是东财概念板块的中文名，通过搜索板块列表匹配
AUX_BOARD_KEYWORDS = ["微盘股", "ST板块"]  # ST 会模糊匹配 "ST板块" / "ST股" 等

# ═══════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════

TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QT    = "http://qt.gtimg.cn/q={codes}"
EM_ZT_POOL    = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_ZB_POOL    = "https://push2ex.eastmoney.com/getTopicZBPool"
# 行业/概念板块 API 在 push2 子域名（非 push2ex）
# 注意：必须用 https。东财 2026-08 起拒绝明文 HTTP（返回 502），
# 之前"HTTP 绕过 SSL 阻断"的做法已失效（P0 bug 教训：板块采集降级导致止损误杀）
EM_CLIST      = "https://push2.eastmoney.com/api/qt/clist/get"

# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

# 东财/腾讯等国内接口强制不走代理。
# 教训（P0）：环境变量代理指向 Clash → 全部流量走 chain-us → us-iproyal
# 纽约住宅 IP → 东财风控 502 → 板块采集降级 → 止损引擎集体误杀。
# 国内数据源直连天经地义，永不经过代理。
NO_PROXY = {"http": None, "https": None}


def em_get(url, params=None, headers=None, timeout=15):
    """国内接口统一请求入口：强制不走代理 + 复用 UA"""
    return requests.get(
        url, params=params,
        headers=headers or {"User-Agent": UA},
        timeout=timeout,
        proxies=NO_PROXY,
    )


def to_em_date(dt_str: str) -> str:
    """2026-07-20 → 20260720（东财 API 日期格式，无连字符）"""
    return dt_str.replace("-", "")


def to_iso_date(dt_str: str) -> str:
    """20260720 → 2026-07-20"""
    return f"{dt_str[:4]}-{dt_str[4:6]}-{dt_str[6:8]}"


def code_to_tx(symbol: str) -> str:
    """
    东财纯数字代码 → 腾讯 qt 格式。
    600519 → sh600519, 002384 → sz002384, 920305 → bj920305
    """
    symbol = str(symbol).strip()
    if symbol.startswith("6"):
        return f"sh{symbol}"
    elif symbol.startswith(("0", "3")):
        return f"sz{symbol}"
    elif symbol.startswith(("4", "8")):
        return f"bj{symbol}"
    return symbol


def tx_to_symbol(tx_code: str) -> str:
    """sh600519 → 600519"""
    return tx_code[2:]


def sma(values: list, period: int) -> list:
    """
    简单移动均线。
    返回与输入等长的列表，前 period-1 个位置为 None。
    """
    if len(values) < period:
        return [None] * len(values)
    result = [None] * (period - 1)
    window_sum = sum(values[:period])
    result.append(window_sum / period)
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result.append(window_sum / period)
    return result


def ma_direction(ma_vals: list, idx: int, lookback: int = 5) -> str:
    """
    判断均线方向：UP / DOWN / FLAT。
    比较当前 MA 值与 lookback 天前的 MA 值。
    """
    if idx < lookback:
        return "FLAT"
    cur = ma_vals[idx]
    prev = ma_vals[idx - lookback]
    if cur is None or prev is None:
        return "FLAT"
    if cur > prev * 1.001:
        return "UP"
    elif cur < prev * 0.999:
        return "DOWN"
    return "FLAT"


def ensure_dir(path: Path):
    """静默创建目录（含父目录）。"""
    path.mkdir(parents=True, exist_ok=True)


def is_weekend(date_str: str) -> bool:
    """2026-07-20 是周六日吗？"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.weekday() >= 5


# ═══════════════════════════════════════════════════════════════
# 数据拉取 — 腾讯 K 线
# ═══════════════════════════════════════════════════════════════

def tx_to_secid(tx_code: str) -> str:
    """
    腾讯代码 → 东财 secid。
    sh600519 → 1.600519, sz000001 → 0.000001, bj920305 → 0.920305
    """
    prefix = tx_code[:2]
    num = tx_code[2:]
    mkt = "1" if prefix == "sh" else "0"
    return f"{mkt}.{num}"


def _fetch_klines_em(tx_code: str, limit: int = 120) -> list:
    """
    东财 push2his 日K线（腾讯 K线不可用时的降级源）。
    返回格式与腾讯一致: [[date(str), open, close, high, low, volume], ...]
    """
    secid = tx_to_secid(tx_code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101",          # 日K
        "fqt": "1",            # 前复权
        "end": "20500101",     # 到最新
        "lmt": str(limit),
    }
    try:
        resp = requests.get(
            url, params=params,
            headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=15, proxies=NO_PROXY,
        )
        resp.raise_for_status()
        data = resp.json()
        klines = (data.get("data") or {}).get("klines") or []
        rows = []
        for item in klines:
            parts = str(item).split(",")
            if len(parts) < 6:
                continue
            try:
                rows.append([
                    parts[0],           # date
                    float(parts[1]),    # open
                    float(parts[2]),    # close
                    float(parts[3]),    # high
                    float(parts[4]),    # low
                    float(parts[5]),    # volume
                ])
            except (ValueError, TypeError):
                continue
        return rows
    except Exception as e:
        print(f"  ✗ {tx_code} 东财K线拉取失败: {e}")
        return []


def fetch_klines(tx_code: str, limit: int = 120) -> list:
    """
    从腾讯 API 拉取日K线（前复权）。
    返回: [[date(str), open, close, high, low, volume], ...]
    失败或为空时降级到东财 push2his。
    """
    url = f"{TENCENT_KLINE}?param={tx_code},day,,,{limit},qfq"
    try:
        # P0 教训：requests.get 默认读环境代理(Clash) → 财经接口 502/SSL 失败，
        # 必须强制直连（与 em_get 一致）
        resp = requests.get(
            url, headers={"User-Agent": UA}, timeout=15, proxies=NO_PROXY
        )
        resp.raise_for_status()
        data = resp.json()

        # 北交所（bj 前缀）等不支持的类型，API 可能返回空列表
        if isinstance(data, list):
            return _fetch_klines_em(tx_code, limit)

        if data.get("code") != 0:
            print(f"  ⚠ {tx_code} API code={data.get('code')}: {data.get('msg', '')}")
            return _fetch_klines_em(tx_code, limit)

        raw = data.get("data", {})
        # 北交所等不支持类型：data["data"] 可能是 list 而非 dict
        if isinstance(raw, list):
            return _fetch_klines_em(tx_code, limit)
        raw = raw.get(tx_code, {}) if isinstance(raw, dict) else {}
        if raw is None:
            return _fetch_klines_em(tx_code, limit)

        # 北交所（bj 前缀）可能返回 list 而非 dict
        if isinstance(raw, list):
            return _fetch_klines_em(tx_code, limit)

        day_list = raw.get("day")
        if not day_list:
            day_list = raw.get("qfqday", [])  # 旧格式兼容

        rows = []
        for item in day_list:
            if len(item) < 6:
                continue
            try:
                row_date = str(item[0])
                rows.append([
                    row_date,       # date (YYYY-MM-DD)
                    float(item[1]),     # open
                    float(item[2]),     # close
                    float(item[3]),     # high
                    float(item[4]),     # low
                    float(item[5]),     # volume (股)
                ])
            except (ValueError, TypeError):
                continue
        if not rows:
            return _fetch_klines_em(tx_code, limit)
        return rows

    except Exception as e:
        print(f"  ✗ {tx_code} K线拉取失败: {e}")
        return _fetch_klines_em(tx_code, limit)


# ═══════════════════════════════════════════════════════════════
# 数据拉取 — 腾讯实时快照（批量）
# ═══════════════════════════════════════════════════════════════

def fetch_qt_batch(tx_codes: list) -> dict:
    """
    批量拉取腾讯实时快照。
    返回: {tx_code: {price, chg_pct, turnover, pe, market_value, name, ...}}
    """
    if not tx_codes:
        return {}

    # 腾讯 qt 接口支持逗号分隔批量，每次最多 ~50 只
    result = {}
    batch_size = 50
    for i in range(0, len(tx_codes), batch_size):
        batch = tx_codes[i:i + batch_size]
        url = TENCENT_QT.format(codes=",".join(batch))
        try:
            # P0 教训：必须强制直连，否则环境代理(Clash)导致 502
            resp = requests.get(
                url, headers={"User-Agent": UA}, timeout=15, proxies=NO_PROXY
            )
            resp.encoding = "gbk"  # 腾讯 qt 返回 GBK
            text = resp.text

            # 解析每一行: v_sh600519="..."\n
            for line in text.strip().split("\n"):
                if "=" not in line or line.startswith("v_"):
                    # 提取 tx_code 和字段
                    pass
                # 格式: v_sh600519="1~贵州茅台~..."
                line = line.strip()
                if not line or "=" not in line:
                    continue
                prefix, _, fields_str = line.partition("=")
                # prefix = "v_sh600519"
                tx = prefix.replace("v_", "").strip()
                fields_str = fields_str.strip().strip('"').strip("'")
                if not fields_str:
                    continue
                fields = fields_str.split("~")

                # 字段位置（SPEC §9 确认）:
                # f[1]=名称, f[3]=现价, f[32]=涨跌幅%, f[38]=换手率%, f[39]=PE, f[45]=总市值
                def f(idx, default=""):
                    return fields[idx] if idx < len(fields) else default

                try:
                    result[tx] = {
                        "name": f(1),
                        "price": float(f(3)) if f(3) else 0,
                        "open": float(f(5)) if f(5) else 0,   # 今开（T+1 跟踪用）
                        "chg_pct": float(f(32)) if f(32) else 0,
                        "turnover": float(f(38)) if f(38) else 0,
                        "pe": float(f(39)) if f(39) and f(39) != "-" else None,
                        "market_value": float(f(45)) if f(45) else 0,
                    }
                except (ValueError, TypeError):
                    continue

        except Exception as e:
            print(f"  ✗ qt 批量拉取失败: {e}")

    return result


# ═══════════════════════════════════════════════════════════════
# 数据拉取 — 东财涨停池 / 炸板池
# ═══════════════════════════════════════════════════════════════

def fetch_zt_pool(date_str: str) -> list:
    """
    拉取东财涨停池。
    date_str: 2026-07-20（ISO 格式，内部转为 20260720）
    返回: [{code, name, price, chg_pct, lb(连板数), fbt(封板时间), ...}, ...]
    非交易日返回空列表。
    """
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "300",
        "sort": "fbt:asc",         # 按封板时间升序（最早封板排前）
        "date": to_em_date(date_str),
    }
    try:
        resp = em_get(EM_ZT_POOL, params=params, headers=EM_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        pool = data.get("data", {}).get("pool", [])
        if not pool:
            return []

        # 提取关键字段。
        # 东财 push2ex 涨停池 API 实测字段名（2026-07-14）:
        #   c=代码, n=名称, p=价格(千分之一元), zdp=涨跌幅%, lbc=连板数,
        #   fbt=封板时间, lbt=最后封板时间, zbc=炸板次数,
        #   fund=封板资金, amount=成交额, ltsz=流通市值, hs=换手率%,
        #   hybk=行业板块名称, zttj={days=连板天数, ct=涨停次数}
        stocks = []
        for item in pool:
            raw_p = item.get("p", 0)
            price = raw_p / 1000 if raw_p and raw_p > 100 else raw_p  # 千分之一元 → 元

            stock = {
                "code": str(item.get("c", "")).zfill(6),
                "name": item.get("n", ""),
                "price": round(price, 2),
                "chg_pct": item.get("zdp", 0),
                "lb": item.get("lbc", 1),            # 连板数
                "fbt": item.get("fbt", ""),           # 封板时间 (如 92500 = 09:25:00)
                "lbt": item.get("lbt", ""),           # 最后封板时间
                "zbc": item.get("zbc", 0),            # 炸板次数
                "fund": item.get("fund", 0),          # 封板资金
                "amount": item.get("amount", 0),      # 成交额
                "ltsz": item.get("ltsz", 0),          # 流通市值
                "hs": item.get("hs", 0),              # 换手率%
                "hybk": item.get("hybk", ""),         # 所属行业板块
                "zt_days": item.get("zttj", {}).get("days", 1) if isinstance(item.get("zttj"), dict) else 1,
            }
            stocks.append(stock)
        return stocks

    except Exception as e:
        print(f"  ✗ 涨停池拉取失败: {e}")
        return []


def fetch_zb_pool(date_str: str) -> list:
    """
    拉取东财炸板池。API 格式与涨停池完全相同，仅 ZT → ZB。
    """
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "300",
        "sort": "fbt:asc",
        "date": to_em_date(date_str),
    }
    try:
        resp = em_get(EM_ZB_POOL, params=params, headers=EM_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        pool = data.get("data", {}).get("pool", [])
        stocks = []
        for item in pool:
            raw_p = item.get("p", 0)
            price = raw_p / 1000 if raw_p and raw_p > 100 else raw_p
            stocks.append({
                "code": str(item.get("c", "")).zfill(6),
                "name": item.get("n", ""),
                "price": round(price, 2),
                "chg_pct": item.get("zdp", 0),
                "lb": item.get("lbc", 1),
                "fbt": item.get("fbt", ""),
                "hybk": item.get("hybk", ""),
            })
        return stocks
    except Exception as e:
        print(f"  ✗ 炸板池拉取失败: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# 数据拉取 — 东财行业板块 / 概念板块
# ═══════════════════════════════════════════════════════════════

def fetch_industry_boards(top_n: int = 20) -> list:
    """
    拉取行业板块 TOP N（主源东财涨幅榜，失败返回空——由调用方降级为
    涨停池集中度排名，见 analyze_l2）。
    返回: [{code, name, chg_pct, close, ...}, ...]

    注意：必须 ≥ 20。板块止损分层依赖 TOP20（TOP20 全卖 / TOP10 减半），
    只存 TOP10 会导致持仓行业永远不在榜单内 → 集体误判清仓（P0 bug 教训）。

    重要：榜单必须与持仓 hybk 同源（东财体系）。同花顺行业名与东财
    不一致（环境治理 vs 环保设备），用作止损榜单会集体误杀（教训）。
    """
    return _fetch_industry_boards_em(top_n)


def _fetch_industry_boards_em(top_n: int = 20) -> list:
    """东财行业板块涨幅 TOP N（直连，不走代理——代理 IP 会被东财风控）"""
    params = {
        "pn": "1",
        "pz": str(top_n),
        "po": "1",              # 降序
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": "m:90+t:2",        # t:2 = 行业板块（申万行业），t:3 = 概念板块
        "fields": "f2,f3,f4,f12,f14",
    }
    try:
        resp = em_get(EM_CLIST, params=params, headers=EM_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("diff", [])
        boards = []
        for item in items:
            boards.append({
                "code": item.get("f12", ""),
                "name": item.get("f14", ""),
                "close": item.get("f2", 0),
                "chg_pct": item.get("f3", 0),
                "chg_amt": item.get("f4", 0),
            })
        return boards
    except Exception as e:
        print(f"  ✗ 东财行业板块拉取失败: {e}")
        return []


def _fetch_industry_boards_ths(top_n: int = 20) -> list:
    """
    同花顺行业板块涨幅 TOP N（备用源）。
    东财 push2 被 IP 风控时降级使用（直连不通，走系统代理可达）。

    页面结构（q.10jqka.com.cn/thshy/）每行:
      <tr>... <td>排名</td> <td><a href="/thshy/detail/code/881121/">行业名</a></td> <td>涨幅%</td> ...
    """
    import re as _re
    try:
        resp = requests.get(
            "https://q.10jqka.com.cn/thshy/",
            headers={"User-Agent": UA},
            timeout=20,
            proxies=NO_PROXY,  # 同花顺国内直连稳定；走代理反而慢/超时
        )
        resp.encoding = "gbk"
        html = resp.text

        boards = []
        for row in _re.finditer(r"<tr[^>]*>(.*?)</tr>", html, _re.S):
            tds = _re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), _re.S)
            if len(tds) < 3:
                continue
            name_m = _re.search(r">([^<]+)</a>", tds[1])
            name = name_m.group(1).strip() if name_m else ""
            chg_text = _re.sub(r"<[^>]+>", "", tds[2]).strip()
            if not name or not chg_text:
                continue
            try:
                chg = float(chg_text)
            except ValueError:
                continue
            boards.append({
                "code": "",
                "name": name,
                "close": 0,
                "chg_pct": chg,
                "chg_amt": 0,
                "_source": "ths",
            })
        # 按涨幅降序（同花顺页面可能不是严格按涨幅排）
        boards.sort(key=lambda b: b["chg_pct"], reverse=True)
        return boards[:top_n]
    except Exception as e:
        print(f"  ✗ 同花顺行业板块拉取失败: {e}")
        return []


def fetch_concept_board_by_keyword(keywords: list) -> dict:
    """
    拉取东财概念板块全列表，按关键词匹配名称。
    返回: {"关键词": {"name": "完整名称", "chg_pct": 1.23}, ...}

    用于获取微盘股、ST 板块等概念板块的当日涨跌幅。
    这些板块在腾讯 API 中无 K 线数据（SPEC §3.1 v1.4 实测结论），
    只能用东财当日涨跌幅做辅助信号。
    """
    # 拉取概念板块全列表（pz=500 足够覆盖所有概念板块）
    params = {
        "pn": "1",
        "pz": "500",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": "m:90+t:3",        # t:3 = 概念板块
        "fields": "f2,f3,f4,f12,f14",
    }
    try:
        resp = em_get(EM_CLIST, params=params, headers=EM_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("diff", [])

        result = {}
        for kw in keywords:
            matched = None
            for item in items:
                name = item.get("f14", "")
                if kw in name:
                    matched = {
                        "name": name,
                        "code": item.get("f12", ""),
                        "chg_pct": item.get("f3", 0),
                        "close": item.get("f2", 0),
                    }
                    break  # 取第一个匹配
            if matched:
                result[kw] = matched
                print(f"    找到概念板块: {matched['name']} ({matched['code']}) 涨跌幅 {matched['chg_pct']:+.2f}%")
            else:
                print(f"    ⚠ 未找到匹配概念板块: {kw}")

        return result

    except Exception as e:
        print(f"  ✗ 概念板块搜索失败: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════
#  数据拉取 — 上一交易日数据（用于轮动计算）
# ═══════════════════════════════════════════════════════════════

def find_prev_cache_date(current_date: str) -> str | None:
    """
    在 data/cache/ 下查找最近一个早于 current_date 的缓存目录。
    用于计算板块连续性（需要前几天的 l2_boards.json）。
    """
    if not CACHE_ROOT.exists():
        return None

    current = datetime.strptime(current_date, "%Y-%m-%d")
    prev_dates = []
    for d in CACHE_ROOT.iterdir():
        if d.is_dir():
            try:
                dt = datetime.strptime(d.name, "%Y-%m-%d")
                if dt < current:
                    prev_dates.append(d.name)
            except ValueError:
                continue

    prev_dates.sort(reverse=True)
    # 返回最近 3 个交易日（用于板块连续性判断）
    return prev_dates[:3] if prev_dates else []


def load_prev_boards(prev_dates: list) -> dict:
    """
    读取历史 l2_boards.json。
    返回: {date_str: [board, ...], ...}
    """
    result = {}
    for d in prev_dates:
        path = CACHE_ROOT / d / "l2_boards.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                result[d] = data.get("boards", [])
            except Exception:
                continue
    return result


# ═══════════════════════════════════════════════════════════════
#  L1 宏观门控分析
# ═══════════════════════════════════════════════════════════════

def analyze_l1(date_str: str, aux_board_data: dict) -> dict:
    """
    L1 五指数体系分析（三指数 MA20/MA60 + 二辅助板块当日涨跌幅）。

    SPEc §3.1:
    - 三指数（上证/深证/创业板）→ 腾讯 K 线算 MA20/MA60 + 量比
    - 微盘股/ST 板块 → 东财当日涨跌幅（无 K 线，不算 MA）
    - L1 三态判定 + 门控降级条件 + 量价配合分析

    返回 l1_index.json 的内容。
    """
    print("\n📊 步骤 1/5: L1 宏观门控分析")

    # ── 拉取三指数 K 线 ──
    indices_result = {}
    for tx_code, name in MAIN_INDICES.items():
        print(f"  拉取 {name} ({tx_code}) K线...")
        klines = fetch_klines(tx_code, limit=120)
        if not klines:
            print(f"    ✗ {name} K线为空，跳过")
            continue

        # 🆕 日期过滤：只保留 ≤ 目标日期的 K 线
        # 防止 7/20 跑 7/15 的数据时拿到 7/17~7/20 的未来 K 线
        klines = [k for k in klines if k[0] <= date_str]
        if not klines:
            print(f"    ✗ {name} 截至 {date_str} 无 K 线数据")
            continue
        print(f"    ✓ {name}: {len(klines)} 条日K线（截至 {klines[-1][0]}）")

        # 提取收盘价序列和成交量序列
        close_prices = [r[2] for r in klines]
        volumes = [r[5] for r in klines]
        latest = klines[-1]

        # MA20 / MA60
        ma20_all = sma(close_prices, 20)
        ma60_all = sma(close_prices, 60)
        ma20 = ma20_all[-1] if ma20_all[-1] is not None else 0
        ma60 = ma60_all[-1] if ma60_all[-1] is not None else 0

        # MA20 方向
        ma20_dir = ma_direction(ma20_all, len(ma20_all) - 1)

        # 量比（当日成交量 / 过去20日均量）
        vol_ma20_all = sma(volumes, 20)
        vol_ma20 = vol_ma20_all[-1] if vol_ma20_all[-1] is not None else volumes[-1]
        vol_ratio = volumes[-1] / vol_ma20 if vol_ma20 and vol_ma20 > 0 else 1.0

        # 当日涨跌幅（基于最新两根K线）
        prev_close = klines[-2][2] if len(klines) >= 2 else latest[1]
        chg_pct = (latest[2] - prev_close) / prev_close * 100 if prev_close else 0

        indices_result[tx_code] = {
            "name": name,
            "close": round(latest[2], 2),
            "ma20": round(ma20, 2) if ma20 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "above_ma20": latest[2] > ma20 if ma20 else False,
            "above_ma60": latest[2] > ma60 if ma60 else False,
            "ma20_rising": ma20_dir == "UP",
            "ma20_direction": ma20_dir,
            "vol_ratio": round(vol_ratio, 4),
            "chg_pct": round(chg_pct, 2),
        }

    if len(indices_result) < 3:
        print("  ⚠ 三指数数据不全，降级为保守模式")
        return {
            "date": date_str,
            "regime": "震荡市",
            "regime_downgraded": False,
            "error": "指数数据不全",
            "indices": indices_result,
            "aux_signals": {},
            "downgrade": {"triggered": False, "reason": "数据不足"},
            "volume_analysis": "无法分析（数据不足）",
        }

    # ── L1 三态判定 ──
    idx_vals = list(indices_result.values())
    above_ma20_count = sum(1 for v in idx_vals if v["above_ma20"])
    above_ma60_count = sum(1 for v in idx_vals if v["above_ma60"])
    ma20_rising_count = sum(1 for v in idx_vals if v["ma20_rising"])

    # 系统性风险：≥2/3 在 MA20 下方 且 ≥2/3 MA20 下行
    below_ma20_count = 3 - above_ma20_count
    ma20_falling_count = sum(1 for v in idx_vals if v["ma20_direction"] == "DOWN")

    if below_ma20_count >= 2 and ma20_falling_count >= 2:
        regime = "系统性风险"
    elif above_ma20_count == 3 and above_ma60_count == 3 and ma20_rising_count >= 2:
        regime = "多头趋势"
    else:
        regime = "震荡市"

    print(f"  三指数判定: {regime}")
    print(f"    站上 MA20: {above_ma20_count}/3, MA60: {above_ma60_count}/3, MA20上行: {ma20_rising_count}/3")

    # ── 量价配合分析 ──
    avg_vol_ratio = sum(v["vol_ratio"] for v in idx_vals) / 3
    avg_chg_pct = sum(v["chg_pct"] for v in idx_vals) / 3

    if avg_chg_pct < 0 and avg_vol_ratio < 0.9:
        vol_analysis = "缩量阴跌 — 底部蓄力，非恐慌性抛售"
    elif avg_chg_pct < 0 and avg_vol_ratio > 1.2:
        vol_analysis = "放量下跌 — 恐慌抛售，真正的系统性风险 ⚠"
    elif avg_chg_pct > 0 and avg_vol_ratio > 1.0:
        vol_analysis = "价涨量增 — 良性反弹，有资金支持"
    elif avg_chg_pct > 0 and avg_vol_ratio < 0.9:
        vol_analysis = "价涨量缩 — 反弹乏力，缺乏持续性"
    else:
        vol_analysis = "量价正常，无极端信号"

    print(f"  量价分析: {vol_analysis}（均量比={avg_vol_ratio:.2f}, 均涨幅={avg_chg_pct:+.2f}%）")

    # ── 门控降级条件（仅系统性风险时可触发） ──
    downgrade_signals = 0
    aux_signals_detail = {}

    for kw, info in aux_board_data.items():
        chg = info.get("chg_pct", 0)
        is_signal = chg > 0
        aux_signals_detail[kw] = {
            "name": info.get("name", kw),
            "chg_pct": round(chg, 2),
            "signal": is_signal,
        }
        if is_signal:
            downgrade_signals += 1

    # 信号3: ≥2 主板指数收盘站上 MA20
    if above_ma20_count >= 2:
        downgrade_signals += 1
        aux_signals_detail["主板站上MA20"] = {"signal": True, "count": above_ma20_count}
    else:
        aux_signals_detail["主板站上MA20"] = {"signal": False, "count": above_ma20_count}

    downgrade_triggered = regime == "系统性风险" and downgrade_signals >= 2
    effective_regime = "震荡市" if downgrade_triggered else regime

    if downgrade_triggered:
        print(f"  🟢 门控降级触发！{regime} → 震荡市（信号 {downgrade_signals}/3 ≥ 2）")
    elif regime == "系统性风险":
        print(f"  🔴 门控降级未触发（信号 {downgrade_signals}/3 < 2），维持 {regime}")

    return {
        "date": date_str,
        "regime": effective_regime,
        "original_regime": regime,
        "regime_downgraded": downgrade_triggered,
        "indices": indices_result,
        "aux_signals": aux_signals_detail,
        "downgrade": {
            "triggered": downgrade_triggered,
            "signals_count": downgrade_signals,
            "need": 2,
        },
        "volume_analysis": vol_analysis,
        "avg_vol_ratio": round(avg_vol_ratio, 4),
        "avg_chg_pct": round(avg_chg_pct, 2),
    }


# ═══════════════════════════════════════════════════════════════
#  L2 行业板块 + 涨停池 + 炸板池 + 轮动分析
# ═══════════════════════════════════════════════════════════════

def analyze_l2(date_str: str) -> dict:
    """
    L2 层：拉取行业 TOP10、涨停池、炸板池、计算轮动速度和板块连续性。

    返回:
        l2_result = {
            "boards": [...],       # l2_boards.json 内容
            "zt_pool": [...],      # l2_zt_pool.json 内容
            "zb_pool": [...],      # l2_zb_pool.json 内容
            "rotation": {...},     # l2_rotation.json 内容
        }
    """
    print("\n📊 步骤 2/5: L2 行业板块 + 涨停池")

    # ── 先拉涨停池（后续行业板块降级方案需要用到 hybk）──
    print("  拉取涨停池...")
    zt_pool = fetch_zt_pool(date_str)
    n_zt = len(zt_pool)
    n_lb = sum(1 for s in zt_pool if s.get("lb", 1) >= 2)
    print(f"    ✓ {n_zt} 家涨停（{n_lb} 家连板）")

    time.sleep(EM_GAP)

    # ── 行业板块 TOP20（板块止损分层需要 TOP20，TOP10 不够） ──
    print("  拉取行业板块涨幅 TOP20...")
    boards = fetch_industry_boards(20)
    if not boards:
        # 东财涨幅榜失败（push2 风控/断连）时，用涨停池 hybk 集中度排名。
        # 注意：涨停池 hybk 与持仓 hybk 同属东财体系，行业名一致，
        # 不会出现同花顺那种名称错位导致的集体误杀（P0 教训）。
        print("    ⚠ 东财涨幅榜不可用，改用涨停池行业集中度排名（同源，止损安全）")
        sector_zt_count = {}
        for s in zt_pool:
            bk = s.get("hybk", "")
            if bk:
                sector_zt_count[bk] = sector_zt_count.get(bk, 0) + 1
        # 按涨停数量降序排列（同样取 TOP20，板块止损分级需要）
        ranked = sorted(sector_zt_count.items(), key=lambda x: x[1], reverse=True)
        for name, count in ranked[:20]:
            boards.append({
                "code": "",
                "name": name,
                "close": 0,
                "chg_pct": 0,
                "chg_amt": 0,
                "zt_count": count,
                "_note": "涨停集中度排名（东财涨幅榜不可用降级）",
            })

    if boards:
        b0 = boards[0]
        detail = f" {b0['zt_count']}家涨停" if b0.get("zt_count") else f" {b0['chg_pct']:+.2f}%"
        print(f"    ✓ TOP1: {b0['name']} ({detail})")
        for b in boards[:3]:
            zt_note = f" {b.get('zt_count', '')}家涨停" if b.get("zt_count") else f" {b['chg_pct']:+.2f}%"
            print(f"      {b['name']}:{zt_note}")
    else:
        print("    ⚠ 行业板块数据为空")

    time.sleep(EM_GAP)

    # ── 炸板池 ──
    print("  拉取炸板池...")
    zb_pool = fetch_zb_pool(date_str)
    n_zb = len(zb_pool)
    if n_zt + n_zb > 0:
        zb_rate = n_zb / (n_zt + n_zb) * 100
        print(f"    ✓ {n_zb} 家炸板（炸板率 {zb_rate:.1f}%）")
    else:
        print(f"    ✓ {n_zb} 家炸板")

    # ── 轮动速度 + 板块连续性 ──
    print("  计算轮动速度 + 板块连续性...")
    prev_dates = find_prev_cache_date(date_str)
    prev_boards = load_prev_boards(prev_dates)

    curr_top5_names = [b["name"] for b in boards[:5]]

    # 轮动速度 = |今日TOP5 ∩ 昨日TOP5| / 5
    rotation = {
        "date": date_str,
        "rotation_speed": None,
        "rotation_label": "首个交易日，暂无对比",
        "prev_top5_sectors": [],
        "curr_top5_sectors": curr_top5_names,
        "overlap": [],
        "continuity": {},
        "sector_structure": {},
        "zb_rate": round(n_zb / (n_zt + n_zb) * 100, 1) if (n_zt + n_zb) > 0 else 0,
    }

    if prev_dates and prev_boards:
        latest_prev_date = prev_dates[0]
        latest_prev_top5 = [b["name"] for b in prev_boards.get(latest_prev_date, [])[:5]]
        overlap = [n for n in curr_top5_names if n in latest_prev_top5]
        speed = len(overlap) / 5 if curr_top5_names else 0

        rotation["prev_top5_sectors"] = latest_prev_top5
        rotation["overlap"] = overlap
        rotation["rotation_speed"] = round(speed, 2)

        if speed >= 0.6:
            rotation["rotation_label"] = "主线延续，正常轮动"
        elif speed >= 0.2:
            rotation["rotation_label"] = "加速轮动，仅限 buy_score≥3"
        else:
            rotation["rotation_label"] = "电风扇行情 ⚠ 全面暂停，只观察"

        # ── 板块连续性（最近 3 个交易日在 TOP3 中出现的次数）──
        continuity = {}
        for name in curr_top5_names:
            count = 0
            for pd_date in prev_dates[:3]:
                pd_top3 = [b["name"] for b in prev_boards.get(pd_date, [])[:3]]
                if name in pd_top3:
                    count += 1
            if count > 0:
                continuity[name] = count
        rotation["continuity"] = continuity

    # ── 板块内部结构（涨停数/行业） ──
    # 不受 prev_dates 限制，始终从涨停池 hybk 统计
    zt_by_sector = {}
    for s in zt_pool:
        bk = s.get("hybk", "")
        if bk:
            zt_by_sector[bk] = zt_by_sector.get(bk, 0) + 1

    sector_structure = {}
    for name in curr_top5_names:
        zt_count = zt_by_sector.get(name, 0)
        if zt_count >= 3:
            structure = "健康"
        elif zt_count >= 2:
            structure = "分化"
        elif zt_count == 1:
            structure = "散沙（孤狼风险）"
        else:
            structure = "无涨停票"
        sector_structure[name] = {
            "zt_count_in_pool": zt_count,
            "structure": structure,
        }

    rotation["sector_structure"] = sector_structure

    print(f"    轮动速度: {rotation['rotation_label']}")

    return {
        "boards": boards,
        "zt_pool": zt_pool,
        "zb_pool": zb_pool,
        "rotation": rotation,
    }


# ═══════════════════════════════════════════════════════════════
#  L3 个股 SOP 打分
# ═══════════════════════════════════════════════════════════════

def compute_buy_score(
    close: float,
    high: float,
    low: float,
    vol: float,
    chg_pct: float,
    klines: list,
    lb: int,
) -> tuple:
    """
    SPEC §3.3: 对一只涨停票计算 buy_score（R2-R5，满分 4 分）。

    参数:
        close, high, low, vol: 当日数据
        chg_pct: 当日涨跌幅 %
        klines: 历史 K 线 [[date,open,close,high,low,vol], ...]
        lb: 连板数（来自涨停池）

    返回: (buy_score, rules_hit_list)
    """
    buy_score = 0
    rules = []

    if not klines or len(klines) < 25:
        return 0, ["数据不足"]

    # 提取历史序列
    closes = [r[2] for r in klines]
    highs = [r[3] for r in klines]
    volumes = [r[5] for r in klines]

    # 均线
    ma5_all = sma(closes, 5)
    ma10_all = sma(closes, 10)
    ma20_all = sma(closes, 20)
    ma5 = ma5_all[-1] if ma5_all[-1] is not None else 0
    ma10 = ma10_all[-1] if ma10_all[-1] is not None else 0
    ma20 = ma20_all[-1] if ma20_all[-1] is not None else 0

    # 20日最高价
    high_20d = max(highs[-20:]) if len(highs) >= 20 else close

    # 量比 = 当日量 / 5日均量
    vol_ma5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else vol  # 前5日均量
    vol_ratio = vol / vol_ma5 if vol_ma5 and vol_ma5 > 0 else 1.0

    # ── R2: 首板涨停 + 重站5日线 + 放量 ──
    # 条件：首板（lb=1）且涨幅 ≥ 9.0%
    if lb == 1 and chg_pct >= 9.0:
        # 涨停当天 close > MA5 是必然的（收盘=涨停价=最高价）
        # 核心是确认这是"重站"而非连续上涨：前一日收盘 < MA5
        prev_close = closes[-2] if len(closes) >= 2 else close
        prev_ma5 = ma5_all[-2] if len(ma5_all) >= 2 and ma5_all[-2] is not None else ma5
        if prev_close < prev_ma5:
            buy_score += 1
            rules.append("R2:首板重站5日线")

    # ── R3: 5/10线上方走强 ──
    # 条件: close > MA5 > MA10 AND 涨幅 ≥ 0.8%
    if close > ma5 > ma10 and chg_pct >= 0.8:
        buy_score += 1
        rules.append("R3:5/10线上方走强")

    # ── R4: 异常放量上行 ──
    # 条件: vol_ratio ≥ 1.8 AND 涨幅 > 0
    if vol_ratio >= 1.8 and chg_pct > 0:
        buy_score += 1
        rules.append("R4:异常放量上行")

    # ── R5: 刷新20日高点 ──
    # 条件: 涨停封板（涨幅 ≥ 9.5%）
    if chg_pct >= 9.5:
        buy_score += 1
        rules.append("R5:涨停封板")

    return buy_score, rules


def analyze_l3(date_str: str, zt_pool: list, l1_result: dict, l2_rotation: dict) -> dict:
    """
    L3 层：对涨停池 TOP 30 逐只拉取 K 线 + qt 快照 → SOP 打分。

    返回 l3_stocks.json 的内容。
    """
    print(f"\n📊 步骤 3/5: L3 个股 SOP 打分（TOP {min(30, len(zt_pool))}）")

    if not zt_pool:
        print("  ⚠ 涨停池为空，跳过 L3")
        return {"date": date_str, "total": 0, "stocks": []}

    top_stocks = zt_pool[:30]
    l1_regime = l1_result.get("regime", "震荡市")
    rotation_label = l2_rotation.get("rotation_label", "") if l2_rotation else ""

    results = []

    for i, stock in enumerate(top_stocks):
        code = stock["code"]
        name = stock.get("name", "")
        tx_code = code_to_tx(code)
        chg_pct = stock.get("chg_pct", 0)
        lb = stock.get("lb", 1)

        print(f"  [{i+1}/{len(top_stocks)}] {code} {name} ...", end=" ")

        # ── 拉取 K 线 ──
        klines = fetch_klines(tx_code, limit=120)
        if not klines:
            # 尝试旧格式
            klines = fetch_klines(tx_code, limit=250)

        latest = klines[-1] if klines else None
        close = latest[2] if latest else 0

        # ── 拉取 qt 快照 ──
        qt_data = fetch_qt_batch([tx_code])
        qt = qt_data.get(tx_code, {})

        price = qt.get("price", close) or close
        pe = qt.get("pe")
        turnover = qt.get("turnover", 0)
        market_value = qt.get("market_value", 0)

        # ── 计算 K 线指标 ──
        if klines and len(klines) >= 25:
            closes = [r[2] for r in klines]
            highs = [r[3] for r in klines]
            volumes = [r[5] for r in klines]

            ma5_all = sma(closes, 5)
            ma10_all = sma(closes, 10)
            ma20_all = sma(closes, 20)

            ma5 = ma5_all[-1] if ma5_all[-1] is not None else 0
            ma10 = ma10_all[-1] if ma10_all[-1] is not None else 0
            ma20 = ma20_all[-1] if ma20_all[-1] is not None else 0

            high_20d = max(highs[-20:])

            vol_ma5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else volumes[-1]
            vol_ratio = volumes[-1] / vol_ma5 if vol_ma5 and vol_ma5 > 0 else 1.0
        else:
            ma5 = ma10 = ma20 = high_20d = 0
            vol_ratio = 1.0

        # ── SOP 打分 ──
        buy_score, rules = compute_buy_score(
            close=close, high=latest[3] if latest else 0,
            low=latest[4] if latest else 0, vol=latest[5] if latest else 0,
            chg_pct=chg_pct, klines=klines, lb=lb,
        )

        # ── 信号标签 ──
        if buy_score >= 3:
            if l1_regime == "系统性风险" or rotation_label == "电风扇行情 ⚠ 全面暂停，只观察":
                label = "风控"
                label_note = "技术满足但全局禁止开仓"
            else:
                label = "强候选"
                label_note = ""
        elif buy_score == 2:
            label = "观察"
            label_note = ""
        else:
            label = "弱"
            label_note = "评分不足，不展示"

        print(f"buy_score={buy_score} [{label}] {' '.join(rules)}")

        results.append({
            "code": code,
            "tx_code": tx_code,
            "name": name,
            "price": round(price, 2),
            "chg_pct": round(chg_pct, 2),
            "lb": lb,
            "pe": round(pe, 1) if pe else None,
            "turnover": round(turnover, 2),
            "market_value": market_value,
            "vol_ratio": round(vol_ratio, 4),
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "high_20d": round(high_20d, 2) if high_20d else None,
            "buy_score": buy_score,
            "label": label,
            "label_note": label_note,
            "rules": rules,
            "l1_regime": l1_regime,
        })

        # 两个东财 API 之间间隔
        time.sleep(0.3)

    # 统计
    by_label = {"强候选": 0, "观察": 0, "风控": 0, "弱": 0}
    for s in results:
        lbl = s["label"]
        by_label[lbl] = by_label.get(lbl, 0) + 1

    summary = f"强候选 {by_label.get('强候选',0)} | 观察 {by_label.get('观察',0)}"
    if by_label.get("风控", 0) > 0:
        summary += f" | 风控 {by_label['风控']}"
    print(f"  ✓ L3 完成: {summary}")

    return {
        "date": date_str,
        "total": len(results),
        "l1_regime": l1_regime,
        "summary": {k: v for k, v in by_label.items() if v > 0},
        "stocks": results,
    }


# ═══════════════════════════════════════════════════════════════
#  步骤 0.5: 缺口检测 + 自动回补
# ═══════════════════════════════════════════════════════════════

# 从第一条缓存到昨天之间的交易日（排除周末和节假日，用涨停池验证）
# 跟已有缓存比较，找出缺失日期


def find_trading_gaps(up_to_date: str) -> list:
    """
    扫描 data/cache/，找出从最早缓存日到 up_to_date 之间缺失的日期。
    注意：不是精确交易日检测（会拉涨停池验证），这里先做日期扫描，
    具体是否交易日由 collect() 内部判断。

    返回: 缺失的日期列表 (YYYY-MM-DD)，按日期升序
    """
    if not CACHE_ROOT.exists():
        return []

    # 收集已有缓存日期
    existing = set()
    for d in CACHE_ROOT.iterdir():
        if d.is_dir():
            try:
                datetime.strptime(d.name, "%Y-%m-%d")
                if (d / "l1_index.json").exists():
                    existing.add(d.name)
            except ValueError:
                continue

    if not existing:
        return []

    all_dates = sorted(existing)
    first_date_str = all_dates[0]  # 最早
    last_cached = all_dates[-1]    # 最新已缓存

    # 从最早缓存日到 up_to_date
    try:
        first_date = datetime.strptime(first_date_str, "%Y-%m-%d")
        target_date = datetime.strptime(up_to_date, "%Y-%m-%d")
    except ValueError:
        return []

    # 找缺口：从 first_date 到 target_date 之间，不在 existing 里的日期
    gaps = []
    curr = first_date
    while curr <= target_date:
        ds = curr.strftime("%Y-%m-%d")
        if ds not in existing:
            gaps.append(ds)
        curr += timedelta(days=1)

    return gaps


def auto_backfill(requested_date: str, verbose: bool = True):
    """
    🆕 缺口自动回补（cc review，2026-07-29）

    1. 扫描 data/cache/ 找出缺失日期
    2. 对每个缺失日期调用 collect()
    3. 标注 "backfilled": True
    4. 最后跑 requested_date 的采集

    参数:
        requested_date: 用户请求的日期（YYYY-MM-DD）
        verbose: 是否打印进度
    """
    gaps = find_trading_gaps(requested_date)

    backfilled = []
    skipped = []

    for gap_date in gaps:
        if verbose:
            print(f"\n🔙 自动回补: {gap_date}")

        # 检查是否已有缓存（双重检查，防止并发场景下的重复回补）
        gap_cache = CACHE_ROOT / gap_date
        if gap_cache.exists() and (gap_cache / "l1_index.json").exists():
            if verbose:
                print(f"  ⏭ {gap_date} 已有缓存，跳过")
            skipped.append(gap_date)
            continue

        # 跳过周末（周六日直接标记已跳过）
        if is_weekend(gap_date):
            if verbose:
                print(f"  ⏭ {gap_date} 是周末，跳过")
            skipped.append(gap_date)
            continue

        # 拉取涨停池验证是否为交易日
        zt_pool = fetch_zt_pool(gap_date)
        if not zt_pool:
            if verbose:
                print(f"  ⚠ {gap_date} 涨停池为空（非交易日/休市），跳过")
            skipped.append(gap_date)
            continue

        # 跑采集（collect 内部通过 is_backfill 标记）
        try:
            collect(gap_date, is_backfill=True)
            backfilled.append(gap_date)
            time.sleep(EM_GAP)  # 回补间间隔
        except Exception as e:
            if verbose:
                print(f"  ✗ {gap_date} 回补失败: {e}")
            skipped.append(gap_date)

    if verbose and backfilled:
        print(f"\n✅ 回补完成: {len(backfilled)} 个日期 ({', '.join(backfilled)})")
    if verbose and skipped:
        print(f"⏭ 跳过: {len(skipped)} 个日期")

    return backfilled, skipped

def collect(date_str: str, is_backfill: bool = False):
    """
    主入口：按 SPEC §7.2 流程拉取全量数据并写入缓存。

    参数:
        date_str: 目标日期 (YYYY-MM-DD)
        is_backfill: 🆕 是否来自自动回补（标记在 l1_index.json）
    """
    cache_dir = CACHE_ROOT / date_str
    ensure_dir(cache_dir)

    print("=" * 60)
    print(f"  🏦 A股虚拟盘 · 数据采集")
    print(f"  📅 {date_str}")
    print(f"  📁 {cache_dir}")
    print("=" * 60)

    # ── 步骤 0: 交易日检测 ──
    print("\n📊 步骤 0: 交易日检测")

    if is_weekend(date_str):
        print(f"  ⚠ {date_str} 是周末，没有交易数据哦")
        print(f"  退出。如果确实需要补录历史（周一补周五），用 --date 指定日期。")
        return

    # 尝试拉涨停池，判断是否为交易日
    zt_check = fetch_zt_pool(date_str)
    if not zt_check:
        print(f"  ⚠ 涨停池返回 0 条")
        print(f"  可能原因: 非交易日 / 节假日 / API 故障")
        print(f"  退出。如果是 API 临时故障，稍后重试。")
        return

    print(f"  ✓ 涨停池 {len(zt_check)} 家 → 确认为交易日")
    time.sleep(EM_GAP)

    # ── 步骤 1+2: L1 宏观门控（含辅助指数） ──
    # 先拉辅助板块数据（微盘股/ST 当日涨跌幅）
    print("\n  拉取辅助板块涨跌幅（微盘股 / ST板块）...")
    aux_data = fetch_concept_board_by_keyword(AUX_BOARD_KEYWORDS)
    time.sleep(EM_GAP)

    l1_result = analyze_l1(date_str, aux_data)

    # ── 步骤 3-5: L2 行业 + 涨停池 + 轮动 ──
    l2_result = analyze_l2(date_str)

    # 把轮动结果注入 L3（用于风控标签判断）
    time.sleep(EM_GAP)

    # ── 步骤 6-7: L3 个股打分 ──
    l3_result = analyze_l3(date_str, l2_result["zt_pool"], l1_result, l2_result["rotation"])

    # ── 写 JSON 文件 ──
    print("\n💾 写缓存文件...")

    files_written = []

    # l1_index.json
    path = cache_dir / "l1_index.json"
    if is_backfill:
        l1_result["backfilled"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(l1_result, f, ensure_ascii=False, indent=2)
    files_written.append(str(path))

    # l2_boards.json
    path = cache_dir / "l2_boards.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "boards": l2_result["boards"],
        }, f, ensure_ascii=False, indent=2)
    files_written.append(str(path))

    # l2_zt_pool.json
    path = cache_dir / "l2_zt_pool.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "total": len(l2_result["zt_pool"]),
            "stocks": l2_result["zt_pool"],
        }, f, ensure_ascii=False, indent=2)
    files_written.append(str(path))

    # l2_zb_pool.json
    path = cache_dir / "l2_zb_pool.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "total": len(l2_result["zb_pool"]),
            "stocks": l2_result["zb_pool"],
        }, f, ensure_ascii=False, indent=2)
    files_written.append(str(path))

    # l2_rotation.json
    path = cache_dir / "l2_rotation.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(l2_result["rotation"], f, ensure_ascii=False, indent=2)
    files_written.append(str(path))

    # l3_stocks.json
    path = cache_dir / "l3_stocks.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(l3_result, f, ensure_ascii=False, indent=2)
    files_written.append(str(path))

    for fp in files_written:
        print(f"  ✓ {fp}")

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print("  ✅ 数据采集完成！")
    print(f"  📁 {cache_dir}")
    print(f"  📊 L1: {l1_result['regime']}" +
          (" (降级)" if l1_result.get("regime_downgraded") else ""))
    print(f"  📊 L2: {l2_result['boards'][0]['name'] if l2_result['boards'] else '?'} 领涨"
          f" | 涨停 {len(l2_result['zt_pool'])} 家"
          f" | 炸板 {len(l2_result['zb_pool'])} 家")
    print(f"  📊 L3: {l3_result['total']} 只打分 | {l3_result.get('summary', {})}")
    print("=" * 60)

    # ── 清理采集锁文件（Web触发时用） ──
    lock_file = BASE_DIR / "accounts" / ".collect.lock"
    if lock_file.exists():
        try:
            lock_file.unlink()
            print("  🔓 已释放采集锁")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="A股虚拟盘数据采集脚本 — 收盘后拉取 L1/L2/L3 数据"
    )
    parser.add_argument(
        "--date", "-d",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="目标日期，格式 YYYY-MM-DD（默认今天）",
    )
    args = parser.parse_args()
    target_date = args.date

    # 验证日期格式
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        print(f"❌ 日期格式错误: {target_date}，应为 YYYY-MM-DD")
        sys.exit(1)

    # 🆕 步骤 0.5: 缺口检测 + 自动回补
    auto_backfill(target_date)

    # 🆕 跑目标日期采集
    collect(target_date)
