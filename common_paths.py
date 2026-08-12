"""
common_paths.py — 仓库统一路径解析
============================================================
解决历史问题：9 个脚本里硬编码了 Claude哥的沙箱路径
（/sessions/xxx/mnt/MyClaude/a-stock-lab/...），每次新会话
路径就变，脚本直接崩。

原理：所有脚本和 portfolio.json 都在同一个仓库树里，
用 Path(__file__) 相对定位就同时兼容两种环境：
  - 本地 Windows:  D:\\MyClaude\\a-stock-lab\\...
  - Claude哥沙箱:  /sessions/xxx/mnt/MyClaude/a-stock-lab/...

用法（脚本顶部）:
    import sys
    from pathlib import Path
    _REPO = Path(__file__).resolve().parent
    while not (_REPO / "common_paths.py").exists():
        _REPO = _REPO.parent
    sys.path.insert(0, str(_REPO))
    from common_paths import PORTFOLIO, CACHE_DIR
"""

from pathlib import Path

# 仓库根 = common_paths.py 所在目录
REPO_ROOT = Path(__file__).resolve().parent

# 核心数据文件
PORTFOLIO = REPO_ROOT / "virtual-portfolio" / "portfolio.json"          # 虚拟盘账户+日报
CLOSING_TEMP = REPO_ROOT / "virtual-portfolio" / "closing_temp.json"    # 收盘简报临时输出
CACHE_DIR = REPO_ROOT / "virtual-trading-web" / "data" / "cache"        # data_collector 缓存根

# 小克虚拟盘 Web 系统
APP_DIR = REPO_ROOT / "virtual-trading-web"


def cache_dir(date_str: str) -> Path:
    """返回某日期的缓存目录（如 2026-08-10）"""
    return CACHE_DIR / date_str


def find_portfolio() -> Path:
    """查找 portfolio.json，找不到时给出友好报错"""
    if PORTFOLIO.exists():
        return PORTFOLIO
    raise FileNotFoundError(
        f"未找到 portfolio.json: {PORTFOLIO}\n"
        "请确认脚本在仓库内运行（本地 D:\\MyClaude\\a-stock-lab 或沙箱挂载点）"
    )


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print(f"仓库根: {REPO_ROOT}")
    print(f"portfolio: {PORTFOLIO}  存在={PORTFOLIO.exists()}")
    print(f"缓存目录: {CACHE_DIR}  存在={CACHE_DIR.exists()}")
