"""HexWand —— 鼠标本地控制工具（Qt6 界面 + 系统托盘）。

分层：`hardware/` 设备层、`core/` 运行期设施、`ui/` Qt6 界面。
前两层与 GUI 工具包完全解耦，可独立测试。

内置多型号设备库（hardware/catalog.py）。
"""

from __future__ import annotations

__version__ = "2.0.0"
