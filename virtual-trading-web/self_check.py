# -*- coding: utf-8 -*-
"""
self_check.py — 数据自检（机器断言，替代人眼盯输出）
============================================================
背景：连续三次同类事故（L3 分桶数字打架、L1 措辞自相矛盾、止损理由浮点未格式化）
——数据没错，拼装错了。人工眼检不可靠，用断言守住输出层。

检查项（Claude哥提案 1-3，第 4 条"假哨兵检测"待排期）：
  1. 字段完整性: holdings 每条必须有 hybk；缺失报警（且止损层明确标记"未执行"）
  2. 分桶和校验: L3 强/观/风/弱 四桶之和 == 信号池总数（不等=有标签漏网）
  3. 维度词校验: volume_analysis 不得含趋势词（底部/良性/乏力/系统性风险…）

用法：
    from self_check import run_self_check, format_check_line
    issues = run_self_check(holdings=..., signals=..., l1=...)
    print(format_check_line(issues))
    # 输出: "数据自检：通过"  或  "数据自检：⚠️ 2 项异常 [ ... ]"
"""

# 量能字段里不应出现的趋势判断词——量能描述与趋势判定是两个维度，
# 混入趋势词会与 regime 打架（"系统性风险→暂停交易"旁边写"底部蓄力"事故）
TREND_WORDS = ["底部", "蓄力", "良性", "乏力", "系统性风险", "反转", "见顶", "牛市", "熊市"]

# L3 标签的四个合法分桶
LABEL_BUCKETS = ["强候选", "观察", "风控", "弱"]


def check_holdings_hybk(holdings):
    """检查1: 持仓行业字段完整性"""
    issues = []
    missing = [h.get("name", h.get("code", "?")) for h in (holdings or []) if not h.get("hybk")]
    if missing:
        issues.append(
            f"hybk缺失{len(missing)}只[{','.join(missing)}]"
            f"→板块止损对这些票未执行(第五层空转)"
        )
    return issues


def check_signal_buckets(signals):
    """检查2: L3 分桶之和 == 信号池总数（漏网标签会破坏输出层表述）
    兼容两种字段名：out（早报/legacy 转换后）与 label（l3_stocks 原始）"""
    issues = []
    if not signals:
        return issues
    total = len(signals)
    counted = 0
    unknown = []
    for s in signals:
        out = s.get("out") or s.get("label", "")
        if out.startswith("强候选"):
            counted += 1
        elif out == "观察":
            counted += 1
        elif out.startswith("风控"):
            counted += 1
        elif out == "弱":
            counted += 1
        else:
            unknown.append(f"{s.get('name', '?')}({out!r})")
    if counted != total:
        detail = f"未归类[{','.join(unknown[:3])}{'...' if len(unknown) > 3 else ''}]" if unknown else ""
        issues.append(f"分桶和{counted}≠信号池{total} {detail}")
    return issues


def check_volume_wording(l1):
    """检查3: 量能文案不得含趋势判断词"""
    issues = []
    text = (l1 or {}).get("volume_analysis", "")
    hit = [w for w in TREND_WORDS if w in text]
    if hit:
        issues.append(f"量能文案含趋势词[{','.join(hit)}]：{text[:40]}")
    return issues


def run_self_check(holdings=None, signals=None, l1=None):
    """跑全部自检，返回异常列表（空列表 = 全部通过）"""
    issues = []
    issues += check_holdings_hybk(holdings)
    issues += check_signal_buckets(signals)
    issues += check_volume_wording(l1)
    return issues


def format_check_line(issues):
    """格式化输出一行（附在简报末尾）"""
    if not issues:
        return "数据自检：✅ 通过"
    return f"数据自检：⚠️ {len(issues)}项异常 [{'; '.join(issues)}]"


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    # 自测：正常数据
    ok_h = [{"name": "A", "hybk": "医药"}, {"name": "B", "hybk": "电力"}]
    ok_s = [{"name": "x", "out": "强候选"}, {"name": "y", "out": "观察"}] + \
           [{"name": "z", "out": "风控"}, {"name": "w", "out": "弱"}]
    ok_l1 = {"volume_analysis": "缩量阴跌 — 量能萎缩，未现恐慌性放量"}
    print("正常场景:", format_check_line(run_self_check(ok_h, ok_s, ok_l1)))

    # 自测：三条异常全触发
    bad_h = [{"name": "C", "hybk": ""}]
    bad_s = [{"name": "x", "out": "强候选"}, {"name": "y", "out": "中性"}]  # 中性漏网
    bad_l1 = {"volume_analysis": "缩量阴跌 — 底部蓄力，非恐慌性抛售"}
    print("异常场景:", format_check_line(run_self_check(bad_h, bad_s, bad_l1)))
