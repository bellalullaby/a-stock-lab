"""
email_briefing.py — 读取 portfolio.json 最新简报，发送到 QQ 邮箱
============================================================
用法:
    python email_briefing.py                          # 发最新简报
    python email_briefing.py --sender 123@qq.com --pwd 授权码 --to 456@qq.com

配置:
    首次运行交互式输入 QQ 邮箱 + 授权码，保存到同目录 .email_config.json。
    后续直接读配置，不再询问。
"""

import io
import json
import smtplib
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from pathlib import Path

# UTF-8 stdout 防 Windows GBK emoji 崩溃
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = Path(__file__).parent
PORTFOLIO = BASE_DIR.parent / "virtual-portfolio" / "portfolio.json"
CONFIG_FILE = BASE_DIR / ".email_config.json"


def load_config():
    """读配置文件，不存在返回None"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception:
            return None
    return None


def save_config(sender, password, receivers):
    """保存配置到文件"""
    CONFIG_FILE.write_text(
        json.dumps({"sender": sender, "password": password, "receivers": receivers}, indent=2),
        "utf-8",
    )
    print(f"✅ 配置已保存到 {CONFIG_FILE}")


def prompt_config():
    """交互式输入邮箱配置"""
    print("📧 首次配置 QQ 邮箱推送")
    print("   1. 先在 QQ 邮箱 设置→账户→POP3/SMTP 开启服务")
    print("   2. 获取 16 位 SMTP 授权码（不是 QQ 密码！）")
    print()
    sender = input("  QQ 邮箱地址（如 123456@qq.com）: ").strip()
    password = input("  SMTP 授权码（16 位）: ").strip()
    to = input("  收件邮箱（默认跟发件相同）: ").strip()
    receivers = [to] if to else [sender]
    return sender, password, receivers


def build_briefing_html(portfolio: dict) -> str:
    """从 portfolio.json 构建 HTML 邮件正文"""
    logs = portfolio.get("daily_log", [])
    if not logs:
        return "<p>暂无简报数据</p>"

    latest = logs[-1]
    date_str = latest.get("date", "")
    l1 = latest.get("l1_state", "?")
    gate = latest.get("l1_gate", "")
    l1_detail = latest.get("l1_detail", "")[:200]
    l2 = latest.get("l2_summary", "")
    decisions = latest.get("decisions", "")[:300]
    signals = latest.get("signals", [])
    yesterday = latest.get("yesterday_review", [])
    trades = latest.get("trades", [])

    # 账户状态
    acct = portfolio.get("account", {})
    total = acct.get("total_value", 1000000)
    pnl = acct.get("pnl", 0)
    pnl_pct = acct.get("pnl_pct", 0)
    pnl_color = "#22c55e" if pnl >= 0 else "#ef4444"

    # 信号汇总
    strong = [s for s in signals if s.get("out", "").startswith("强候选")]
    watch = [s for s in signals if s.get("out") == "观察"]
    risk = [s for s in signals if s.get("out", "").startswith("风控")]

    # 昨日回顾
    yesterday_ok = [y for y in yesterday if y.get("today_pct", 0) > 0]
    yesterday_bad = [y for y in yesterday if y.get("today_pct", 0) < 0]

    # L1 颜色
    l1_color = "#22c55e" if l1 == "多头趋势" else ("#f59e0b" if l1 == "震荡市" else "#ef4444")

    html = f"""
    <div style="max-width:600px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                background:#1a1d28;color:#e1e4eb;padding:24px;border-radius:12px;">
      <!-- 头部 -->
      <div style="text-align:center;padding-bottom:20px;border-bottom:1px solid #2a2d38;margin-bottom:20px;">
        <h2 style="margin:0;font-size:20px;">🏦 A股 SOP 虚拟盘日报</h2>
        <p style="margin:4px 0 0;color:#8b8fa3;font-size:13px;">{date_str}</p>
      </div>

      <!-- 账户卡片 -->
      <div style="background:#222534;border-radius:8px;padding:16px;margin-bottom:16px;text-align:center;">
        <span style="font-size:12px;color:#8b8fa3;">账户总资产</span>
        <div style="font-size:28px;font-weight:700;margin:4px 0;">¥{total:,.0f}</div>
        <span style="font-size:14px;color:{pnl_color};">{pnl:+,.0f} ({pnl_pct:+.2f}%)</span>
      </div>

      <!-- L1 状态 -->
      <div style="background:#222534;border-radius:8px;padding:16px;margin-bottom:16px;
                  border-left:4px solid {l1_color};">
        <div style="font-size:16px;font-weight:600;margin-bottom:4px;">
          L1 宏观环境：<span style="color:{l1_color};">{l1}</span>
          {f' · {gate}' if gate else ''}
        </div>
        <div style="font-size:12px;color:#8b8fa3;">{l1_detail}</div>
      </div>

      <!-- L2 市场 -->
      <div style="background:#222534;border-radius:8px;padding:16px;margin-bottom:16px;">
        <div style="font-size:14px;font-weight:600;margin-bottom:6px;">📊 L2 市场结构</div>
        <div style="font-size:13px;color:#c1c6d4;">{l2}</div>
      </div>
    """

    # 信号表格
    if signals:
        html += """
        <div style="background:#222534;border-radius:8px;padding:16px;margin-bottom:16px;">
          <div style="font-size:14px;font-weight:600;margin-bottom:8px;">
            📡 今日信号（强候选 {strong_count} · 观察 {watch_count} · 风控 {risk_count}）
          </div>
        """.format(
            strong_count=len(strong), watch_count=len(watch), risk_count=len(risk)
        )
        html += """
          <table style="width:100%;border-collapse:collapse;font-size:12px;">
            <tr style="color:#8b8fa3;border-bottom:1px solid #2a2d38;">
              <th style="padding:6px;text-align:left;">代码</th>
              <th style="padding:6px;text-align:left;">名称</th>
              <th style="padding:6px;text-align:center;">连板</th>
              <th style="padding:6px;text-align:right;">价格</th>
              <th style="padding:6px;text-align:right;">涨幅</th>
              <th style="padding:6px;text-align:center;">评分</th>
              <th style="padding:6px;text-align:left;">信号</th>
            </tr>
        """
        for s in signals[:10]:  # TOP 10
            chg_color = "#22c55e" if s.get("pct", 0) > 0 else "#ef4444"
            out = s.get("out", "")
            label_color = {"强候选": "#22c55e", "观察": "#f59e0b", "风控": "#ef4444"}.get(
                out.split("(")[0] if out else "", "#8b8fa3"
            )
            html += f"""
            <tr style="border-bottom:1px solid #2a2d38;">
              <td style="padding:6px;">{s.get('code','')}</td>
              <td style="padding:6px;">{s.get('name','')}</td>
              <td style="padding:6px;text-align:center;">{s.get('lbc','1')}板</td>
              <td style="padding:6px;text-align:right;">¥{s.get('price',0):.2f}</td>
              <td style="padding:6px;text-align:right;color:{chg_color};">{s.get('pct',0):+.1f}%</td>
              <td style="padding:6px;text-align:center;">{s.get('buy',0)}/4</td>
              <td style="padding:6px;color:{label_color};font-weight:600;">{out}</td>
            </tr>"""
        html += "</table></div>"

    # 昨日回顾
    if yesterday:
        html += """
        <div style="background:#222534;border-radius:8px;padding:16px;margin-bottom:16px;">
          <div style="font-size:14px;font-weight:600;margin-bottom:6px;">
            🔄 昨日信号回顾
          </div>
        """
        for y in yesterday[:8]:
            pct = y.get("today_pct", 0)
            icon = "📈" if pct > 5 else ("↗" if pct > 0 else ("↘" if pct > -5 else "📉"))
            color = "#22c55e" if pct > 0 else "#ef4444"
            html += f"""
            <div style="font-size:12px;margin:2px 0;">
              {icon} {y.get('name','')} | 昨{y.get('lbc','')}板{y.get('yesterday_price',0)}→
              <span style="color:{color};">{pct:+.1f}%</span> {y.get('status','')}
            </div>"""
        html += "</div>"

    # 决策
    if decisions:
        html += f"""
        <div style="background:#222534;border-radius:8px;padding:16px;margin-bottom:16px;">
          <div style="font-size:14px;font-weight:600;margin-bottom:6px;">💡 今日决策</div>
          <div style="font-size:13px;color:#c1c6d4;">{decisions}</div>
        </div>"""

    # 尾部
    html += f"""
      <div style="text-align:center;color:#8b8fa3;font-size:11px;padding-top:16px;
                  border-top:1px solid #2a2d38;margin-top:8px;">
        ⚠️ 研究观察，不构成投资建议 · 自动生成于 {datetime.now().strftime('%m-%d %H:%M')}
      </div>
    </div>"""
    return html


def build_plain_briefing(portfolio: dict) -> str:
    """纯文本版本（邮件备选）"""
    logs = portfolio.get("daily_log", [])
    if not logs:
        return "暂无简报数据"

    latest = logs[-1]
    date = latest.get("date", "")
    l1 = latest.get("l1_state", "?")
    l2 = latest.get("l2_summary", "")
    acct = portfolio.get("account", {})
    total = acct.get("total_value", 1000000)
    pnl_pct = acct.get("pnl_pct", 0)

    lines = [
        f"🏦 A股 SOP 虚拟盘日报 — {date}",
        f"L1: {l1} | 总资产: ¥{total:,.0f} ({pnl_pct:+.2f}%)",
        f"L2: {l2}",
        "",
        "⚠️ 研究观察，不构成投资建议",
    ]
    return "\n".join(lines)


def send_briefing_email(portfolio: dict, sender: str, password: str, receivers: list) -> bool:
    """发送简报邮件"""
    date_str = portfolio.get("daily_log", [{}])[-1].get("date", datetime.now().strftime("%Y-%m-%d"))
    subject = f"📈 A股 SOP 虚拟盘日报 - {date_str}"

    html = build_briefing_html(portfolio)
    plain = build_plain_briefing(portfolio)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("A股SOP虚拟盘", sender))
    msg["To"] = ", ".join(receivers)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print(f"✅ 邮件已发送 → {', '.join(receivers)}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("❌ 认证失败！请检查：1) QQ邮箱已开启 SMTP 2) 用的是授权码不是 QQ 密码")
        return False
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="发送 A股 SOP 虚拟盘日报到邮箱")
    parser.add_argument("--sender", help="发件 QQ 邮箱")
    parser.add_argument("--pwd", help="QQ 邮箱 SMTP 授权码")
    parser.add_argument("--to", help="收件邮箱（逗号分隔多个）")
    parser.add_argument("--dry-run", action="store_true", help="只生成预览，不发送")
    args = parser.parse_args()

    # 读 portfolio
    if not PORTFOLIO.exists():
        print(f"❌ 找不到 portfolio.json: {PORTFOLIO}")
        sys.exit(1)

    portfolio = json.loads(PORTFOLIO.read_text("utf-8"))

    # dry-run 模式：只生成预览 HTML，不需要邮箱配置
    if args.dry_run:
        html = build_briefing_html(portfolio)
        out = BASE_DIR / ".preview_email.html"
        out.write_text(html, "utf-8")
        print(f"✅ 预览已生成 → {out}")
        return

    # 正式发送：获取邮箱配置
    # 优先级：命令行参数 > 已保存配置 > 交互式输入
    if args.sender and args.pwd:
        config = {"sender": args.sender, "password": args.pwd,
                  "receivers": (args.to or args.sender).split(",")}
        save_config(args.sender, args.pwd, config["receivers"])
    else:
        config = load_config()
        if not config:
            sender, pwd, receivers = prompt_config()
            save_config(sender, pwd, receivers)
            config = {"sender": sender, "password": pwd, "receivers": receivers}

    send_briefing_email(
        portfolio,
        config["sender"],
        config["password"],
        config["receivers"],
    )


if __name__ == "__main__":
    main()
