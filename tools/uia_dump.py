"""诊断工具：导出 MAA 窗口（以及公告相关内容）的 UI 自动化树，便于定位控件。

用法：
  python uia_dump.py            # 打印 MAA 主窗口结构
  python uia_dump.py --search 公告
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uiautomation as auto

sys.path.insert(0, str(Path(__file__).resolve().parent))


def dump(control, depth: int = 0, max_depth: int = 6, out=None) -> None:
    if depth > max_depth:
        return
    name = (control.Name or "").replace("\n", " ")[:60]
    print(f"{'  ' * depth}- {control.ControlTypeName} name={name!r}", file=out)
    if depth == max_depth:
        return
    try:
        children = control.GetChildren()
    except Exception:  # noqa: BLE001
        return
    for child in children:
        dump(child, depth + 1, max_depth, out)


def find_maa_window():
    auto.SetGlobalSearchTimeout(3)
    for window in auto.GetRootControl().GetChildren():
        name = window.Name or ""
        if "MAA" in name:
            return window
    return auto.WindowControl(searchDepth=1, RegexName=".*MAA.*")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", default=None, help="只查找名称包含该字符串的控件")
    parser.add_argument("--in-maa", action="store_true", help="只在 MAA 窗口内搜索")
    parser.add_argument("--list", action="store_true", help="列出所有顶层窗口")
    parser.add_argument("--window", default=None, help="导出指定标题窗口的结构")
    parser.add_argument("--depth", type=int, default=5)
    args = parser.parse_args()

    target = Path("uia-dump.txt")
    with open(target, "w", encoding="utf-8") as out:
        if args.search:
            print(f"# 搜索包含 {args.search!r} 的控件", file=out)
            if args.in_maa:
                window = find_maa_window()
                print(f"# MAA 窗口: {window.Name!r}", file=out)
                control = window.Control(searchDepth=0xFFFFFFFF, SubName=args.search)
            else:
                control = auto.Control(searchDepth=0xFFFFFFFF, SubName=args.search)
            if control.Exists(3, 0.5):
                dump(control, 0, args.depth, out)
            else:
                print("未找到", file=out)
        elif args.list:
            for window in auto.GetRootControl().GetChildren():
                print(f"- {window.ControlTypeName} name={window.Name!r}", file=out)
        elif args.window:
            window = auto.WindowControl(searchDepth=1, Name=args.window)
            if window.Exists(3, 0.5):
                print(f"# 窗口: {window.Name!r}", file=out)
                dump(window, 0, args.depth, out)
            else:
                print(f"未找到窗口 {args.window!r}", file=out)
        else:
            window = find_maa_window()
            print(f"# MAA 窗口: {window.Name!r}", file=out)
            dump(window, 0, args.depth, out)
    print(f"已写入 {target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
