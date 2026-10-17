"""诊断工具：列出 MAA 公告窗口的可滚动控件与关键按钮信息。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uiautomation as auto

sys.path.insert(0, str(Path(__file__).resolve().parent))


def walk(control, depth: int = 0, max_depth: int = 12):
    if depth > max_depth:
        return
    yield control, depth
    try:
        children = control.GetChildren()
    except Exception:  # noqa: BLE001
        return
    for child in children:
        yield from walk(child, depth + 1, max_depth)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", default="公告")
    args = parser.parse_args()

    auto.SetGlobalSearchTimeout(2)
    window = auto.WindowControl(searchDepth=1, Name=args.window)
    if not window.Exists(3, 0.5):
        print(f"未找到窗口 {args.window!r}")
        return 1

    print(f"窗口 {window.Name!r} 可滚动控件：")
    for control, depth in walk(window):
        try:
            pattern = control.GetScrollPattern()
        except Exception:  # noqa: BLE001
            pattern = None
        if pattern is None:
            continue
        try:
            info = (
                f"{'  ' * depth}- {control.ControlTypeName} H={pattern.HorizontallyScrollable}"
                f"({pattern.HorizontalScrollPercent:.0f}%) V={pattern.VerticallyScrollable}"
                f"({pattern.VerticalScrollPercent:.0f}%)"
            )
        except Exception:  # noqa: BLE001
            info = f"{'  ' * depth}- {control.ControlTypeName} (无法读取状态)"
        print(info)

    print("\n关键控件：")
    for name in ("下次公告更新前不再显示", "确认", "关闭"):
        control = window.Control(searchDepth=0xFFFFFFFF, SubName=name)
        if control.Exists(1, 0.2):
            rect = control.BoundingRectangle
            state = ""
            try:
                toggle = control.GetTogglePattern()
                if toggle:
                    state = f" toggleState={toggle.ToggleState}"
            except Exception:  # noqa: BLE001
                pass
            print(
                f"- {name!r}: {control.ControlTypeName} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})"
                f" enabled={control.IsEnabled}{state}"
            )
        else:
            print(f"- {name!r}: 未找到")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
