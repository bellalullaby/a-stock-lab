# -*- coding: utf-8 -*-
"""
self_check.py — 数据自检（机器断言，替代人眼盯输出）
============================================================
背景：连续三次同类事故（L3 分桶数字打架、L1 措辞自相矛盾、止损理由浮点未格式化）
——数据没错，拼装错了。人工眼检不可靠，用断言守住输出层。
09-16 追加上游缓存质量层（Claude哥发现 09-15 缓存 MA 大面积缺失事故）：
自检不能只盯输出层拼装，还要能查出上游缓存过期/残缺。

检查项：
  输出层拼装：
    1. 字段完整性: holdings 每条必须有 hybk；缺失报警（且止损层明确标记"未执行"）
    2. 分桶和校验: L3 强/观/风/弱 四桶之和 == 信号池总数（不等=有标签漏网）
    3. 维度词校验: volume_analysis 不得含趋势词（底部/良性/乏力/系统性风险…）
  上游缓存质量（09-16 新增）:
    4. L3 MA 缺失率 >30% → 上游缓存疑似残缺，打分不可信
    5. 轮动基准新鲜度: prev_top5 与前日板块榜对不上 → 轮动判定不可信
    6. L3 全弱异常: 全部标'弱'且无任何达标 → 打分链路疑似失效（检查4的冗余哨兵）
  待排期:
    7. 假哨兵检测: 历史持仓回放各规则，命中率恒为 0 → 报警

用法：
    from self_check import run_self_check, format_check_line
    issues = run_self_check(holdings=..., signals=..., l1=...,
                            l3_stocks=..., l2_rotation=..., prev_day_boards=...)
    print(format_check_line(issues))
    # 输出: "数据自检：✅ 通过"  或  "数据自检：⚠️ N项异常 [ ... ]"
"""

# 量能字段里不应出现的趋势判断词——量能描述与趋势判定是两个维度，
# 混入趋势词会与 regime 打架（"系统性风险→暂停交易"旁边写"底部蓄力"事故）
TREND_WORDS = ["底部", "蓄力", "良性", "乏力", "系统性风险", "反转", "见顶", "牛市", "熊市"]

# L3 标签的四个合法分桶
LABEL_BUCKETS = ["强候选", "观察", "风控", "弱"]

# L3 MA 缺失率超过此阈值 → 上游缓存疑似残缺（09-15 事故：27/30 只 MA null）
MA_MISSING_ALERT_RATIO = 0.3


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


def check_l3_ma_missing(l3_stocks):
    """检查4: L3 缓存 MA 大面积缺失 → 上游缓存疑似残缺
    （09-15 事故：27/30 只 MA 全 null → buy_score 全 0 → 全标'弱'，简报严重失真）"""
    issues = []
    if not l3_stocks:
        return issues
    n = len(l3_stocks)
    null_ma = sum(1 for s in l3_stocks
                  if s.get("ma5") is None and s.get("ma10") is None and s.get("ma20") is None)
    if n and null_ma / n > MA_MISSING_ALERT_RATIO:
        issues.append(
            f"L3 MA缺失{null_ma}/{n}只(超{MA_MISSING_ALERT_RATIO * 100:.0f}%)"
            f"→上游缓存疑似残缺,打分不可信,建议重采"
        )
    return issues


def check_rotation_freshness(l2_rotation, prev_day_boards):
    """检查5: 轮动对比基准新鲜度——prev_top5 必须能在前一交易日板块榜里找到
    （09-15 事故：prev_top5 是 8 月初行业[航空机场等]，当期任何一天缓存里都没有）"""
    issues = []
    if not l2_rotation or not prev_day_boards:
        return issues  # 无数据不判（板块止损有独立的"榜单<20条跳过"防御）
    prev_top5 = l2_rotation.get("prev_top5_sectors", [])
    if not prev_top5:
        return issues
    board_names = {b.get("name") for b in (prev_day_boards or [])}
    if not board_names:
        return issues
    missing = [s for s in prev_top5 if s not in board_names]
    if len(missing) >= 3:  # prev_top5 里过半找不到 → 基准错乱
        issues.append(
            f"轮动基准疑似过期(prev_top5中{len(missing)}/5不在前日板块榜[{','.join(missing[:3])}...])"
            f"→轮动判定不可信,建议重采"
        )
    return issues


def check_all_weak(signals):
    """检查6: L3 结果特征异常——全部标'弱'且无任何达标
    （MA 缺失的结果特征：buy_score 全 0；作为检查4的冗余哨兵）"""
    issues = []
    if not signals or len(signals) < 10:
        return issues
    weak = sum(1 for s in signals if (s.get("out") or s.get("label", "")) == "弱")
    if weak == len(signals):
        issues.append(f"L3全部{len(signals)}只标'弱'→打分链路疑似失效(呼应MA缺失检查)")
    return issues


def run_self_check(holdings=None, signals=None, l1=None,
                   l3_stocks=None, l2_rotation=None, prev_day_boards=None):
    """跑全部自检，返回异常列表（空列表 = 全部通过）
    新增可选参数（上游缓存质量层）:
      l3_stocks       : l3_stocks.json 原始 stocks（MA 缺失检查用）
      l2_rotation     : l2_rotation.json（轮动基准检查用）
      prev_day_boards : 前一交易日 l2_boards.json 的 boards（轮动基准对照）"""
    issues = []
    issues += check_holdings_hybk(holdings)
    issues += check_signal_buckets(signals)
    issues += check_volume_wording(l1)
    issues += check_l3_ma_missing(l3_stocks)
    issues += check_rotation_freshness(l2_rotation, prev_day_boards)
    # 检查6用原始 stocks 的 label；没有时退回转换后的 signals
    issues += check_all_weak(l3_stocks or signals)
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
