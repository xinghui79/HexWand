"""UI 子包（Qt6）：绑定适配层、主题与 QSS、基础控件、主窗口、键位页、浮窗、托盘。

绑定：只用 PyQt6，集中在 `qt.py`，别处不许直接 import 绑定
（"先试 PySide6、失败回落 PyQt6"的双绑定兼容层已删，本机从未装过 PySide6）。
当前实测为 PyQt6 6.11.0 / Qt 6.11.0。

**本包原来的名字是 `hexwand/qtui/`，用来与并存的一套 Tk/Tkinter 实现
（旧的 `hexwand/ui/`）区分。Tk 版已于 2026-09-16 移除**（备份在
`%APPDATA%\\HexWand\\backup\\`），"qtui" 这个前缀失去了对照意义，于是改名回 ui。
下文各模块里提到的「Tk 版」都指那一版，作用是把当年踩过的坑与对应做法留下来。

注意：本包 `import` 就会拉 Qt（window.py → qt.py）。所以 `app.py` 对这里的
import 做了 try/except —— 缺 Qt 时给出可读的提示，而不是让程序崩在启动阶段
（那个提示框走 Win32 API，不依赖任何 GUI 工具包）。
"""

from __future__ import annotations

from .qt import BINDING, QT_VERSION, app_version_text
from .window import MainWindow, run_gui

__all__ = ["run_gui", "MainWindow", "BINDING", "QT_VERSION", "app_version_text"]
