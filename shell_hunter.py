"""
shell_hunter.py — 借壳概念股扫描器
====================================
筛选条件：老登行业(建材/地产/钢铁/煤炭/纺织/造纸等) × 市值<25亿 × 大股东持股≥35%
输出：终端表格 + JSON文件

用法：
    py shell_hunter.py                # 扫描全A股
    py shell_hunter.py --top 100      # 仅扫描市值最小的100只
    py shell_hunter.py --output cute.json

数据源：
    腾讯 qt.gtimg.cn — 市值、PE（不封IP）
    东财 RPT_F10_EH_HOLDERS — 大股东持股（限流，1.5s间隔）
"""

import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import requests

BASE_DIR = Path(__file__).parent
H = {"Referer": "https://data.eastmoney.com/", "User-Agent": "Mozilla/5.0"}

# ── 老登行业关键词 ──
OLD_KEYWORDS = [
    "建材", "水泥", "玻璃", "陶瓷", "石材",
    "地产", "房地产", "园区开发",
    "钢铁", "冶铁", "铁矿石",
    "煤炭", "焦炭",
    "纺织", "服装", "化纤",
    "造纸", "包装",
    "家具", "装饰", "装修",
    "化工", "化学", "塑料", "橡胶",
    "矿业", "石油", "天然气",
    "机械", "重工", "柴油机",
    "港口", "航运", "运输",
]

# ── 工具 ──

def tx_quote_batch(codes: list) -> dict:
    """
    腾讯批量快照。codes 格式: ["sh600569","sz000619"]
    返回: {code: {name, price, pe, mkt_cap_yi, industry}, ...}
    """
    if not codes:
        return {}
    result = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i+50]
        url = f"http://qt.gtimg.cn/q={','.join(batch)}"
        try:
            r = requests.get(url, timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if "=" not in line: continue
                prefix, _, fields = line.partition("=")
                tx_code = prefix.replace("v_", "").strip()
                raw = fields.strip().strip('"').strip("'")
                f = raw.split("~")
                if len(f) < 46: continue
                try:
                    mkt_cap = float(f[45]) / 10000 if f[45] and f[45] != "" else 0
                    result[tx_code] = {
                        "code": tx_code,
                        "name": f[1],
                        "price": float(f[3]) if f[3] else 0,
                        "pe": float(f[39]) if f[39] and f[39] != "" else None,
                        "mkt_cap_yi": round(mkt_cap, 1),
                        "industry": f[114] if len(f) > 114 else "",
                    }
                except (ValueError, IndexError, TypeError):
                    continue
        except Exception:
            continue
    return result


def fetch_holder(code_num: str) -> tuple:
    """
    东财大股东。code_num: 600569(纯数字)
    返回: (holder_name, ratio_pct, date_str) 或 (None, 0, "")
    """
    try:
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "reportName": "RPT_F10_EH_HOLDERS",
            "columns": "SECURITY_CODE,HOLDER_NAME,HOLD_NUM_RATIO,END_DATE",
            "filter": f'(SECURITY_CODE="{code_num}")',
            "pageSize": 1, "pageNumber": 1,
            "sortColumns": "END_DATE", "sortTypes": -1,
        }
        r = requests.get(url, params=params, headers=H, timeout=15)
        d = r.json()
        if d.get("success") and d.get("result", {}).get("data"):
            item = d["result"]["data"][0]
            return (
                item.get("HOLDER_NAME", ""),
                item.get("HOLD_NUM_RATIO", 0) or 0,
                (item.get("END_DATE", "") or "")[:10],
            )
    except Exception:
        pass
    return (None, 0, "")


def is_old_industry(name: str, industry_tag: str) -> bool:
    """判断名字或行业标签是否属于老登行业"""
    combined = f"{name} {industry_tag}"
    return any(kw in combined for kw in OLD_KEYWORDS)


# ── 主流程 ──

def hunt(limit: int = None):
    """
    扫描全A股市值最小的股票，过滤老登行业 + 大股东≥35%。

    limit: 不超过 limit 只候选（None=全部）
    """
    print("=" * 60)
    print("  🔍 借壳概念股扫描器")
    print(f"  条件: 老登行业 x 市值<25亿 x 大股东≥35%")
    print("=" * 60)

    # ── 1. 全A股市值排序 ──
    print("\n[1/3] 获取全A股市值排名...")

    all_codes = []
    for page in range(1, 6):
        try:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": str(page), "pz": "200", "po": "1",
                "fid": "f20",  # 按总市值升序
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f14,f20,f100",
            }
            r = requests.get(url, params=params, headers=H, timeout=15)
            d = r.json()
            items = d.get("data", {}).get("diff", [])
            if not items:
                break
            for item in items:
                code = item.get("f12", "")
                name = item.get("f14", "")
                mkt_cap = item.get("f20", 0) or 0
                industry = item.get("f100", "")
                if mkt_cap > 0:
                    all_codes.append({
                        "code": code, "name": name,
                        "mkt_cap_yi": mkt_cap, "industry": industry,
                    })
        except Exception as e:
            print(f"  ⚠ 页码{page}失败: {e}")
        time.sleep(2)  # 东财 clist 安全间隔

    all_codes.sort(key=lambda x: x["mkt_cap_yi"])
    print(f"  全A股: {len(all_codes)} 只已加载")

    # ── 2. 过滤老登行业 + 市值<25亿 ──
    print("\n[2/3] 过滤老登行业 + 市值<25亿...")

    small = [c for c in all_codes if c["mkt_cap_yi"] < 25]
    old_small = [c for c in small if is_old_industry(c["name"], c["industry"])]
    print(f"  市值<25亿: {len(small)} 只")
    print(f"  其中老登行业: {len(old_small)} 只")

    # 限制数量
    if limit:
        old_small = old_small[:limit]
        print(f"  (限制前{limit}只)")

    # ── 3. 逐只查大股东 ──
    print(f"\n[3/3] 查询大股东持股 (共{len(old_small)}只, 预计{len(old_small)*2}秒)...")

    results = []
    n = 0
    for c in old_small:
        n += 1
        code = c["code"]
        code_num = code if not code.startswith(("sh","sz","bj")) else code[2:]
        if n % 20 == 0:
            print(f"  进度: {n}/{len(old_small)} (找到{len(results)}只≥35%)")

        holder, ratio, date = fetch_holder(code_num)
        if holder and ratio >= 35:
            results.append({
                "code": code,
                "name": c["name"],
                "mkt_cap_yi": c["mkt_cap_yi"],
                "industry": c["industry"],
                "holder": holder,
                "holder_ratio": round(ratio, 2),
                "holder_date": date,
            })
            print(f"  ✅ {code} {c['name']} {c['mkt_cap_yi']:.1f}亿  {holder} {ratio:.1f}%")

        time.sleep(1.5)  # 东财限流保护

    # ── 输出 ──
    print(f"\n{'='*60}")
    print(f"  📋 结果 ({len(results)} 只)")
    print(f"{'='*60}")
    print(f"{'代码':<10s} {'名称':<10s} {'市值':>6s} {'PE':>6s} {'大股东':<20s} {'持股%':>6s}")
    print("-" * 65)
    for r in results:
        print(f"{r['code']:<10s} {r['name']:<10s} {r['mkt_cap_yi']:>5.1f}亿 {'':>6s} {r['holder'][:20]:<20s} {r['holder_ratio']:>5.1f}%")

    # 存JSON
    out_path = BASE_DIR / "shell_hunt_result.json"
    out_path.write_text(
        json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count": len(results),
            "criteria": "老登行业 x 市值<25亿 x 大股东持股≥35%",
            "note": "实控人年龄/卖公司公告需手动补充",
            "results": results,
        }, ensure_ascii=False, indent=2),
        "utf-8",
    )
    print(f"\n✅ 结果已保存 → {out_path}")
    print(f"⚠️ 实控人年龄、卖公司公告等字段需要手动补充（天眼查/企查查/巨潮公告）")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="借壳概念股扫描器")
    parser.add_argument("--top", type=int, default=None, help="仅扫描市值最小的N只")
    parser.add_argument("--output", type=str, default=None, help="输出JSON路径")
    args = parser.parse_args()

    results = hunt(limit=args.top)

    if args.output:
        out = Path(args.output)
        out.write_text(
            json.dumps({
                "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "count": len(results),
                "results": results,
            }, ensure_ascii=False, indent=2),
            "utf-8",
        )
        print(f"📁 附加输出 → {out}")
