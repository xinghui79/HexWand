"""Qt 绑定适配层：项目只用 PyQt6，UI 包**只从这里**取 Qt 名字。

为什么收在一处：别处一律不许 `import PyQt6`——换绑定时只改这个文件，调用点不动。
写代码时的两条硬规矩：
1. **枚举一律写全限定名**：`Qt.AlignmentFlag.AlignLeft`，不写 `Qt.AlignLeft`
   （PyQt6 只认全限定名，短名是 PySide6 的写法）。
2. **信号一律 `from .qt import Signal`**，不写 `pyqtSignal` 字面名。

**历史上这里同时支持 PySide6 与 PyQt6（try PySide6 / except ImportError 回落）。
已删除**：实测环境从没装过 PySide6，那条分支从来没被执行过，是纯兼容层
（用户规则：不为假设中的未来需求保留 fallback）。
授权提示：PyQt6 是 GPL / 商业双许可，自用没问题；若将来要闭源对外分发，
应换回 PySide6（LGPL）—— 届时才值得重新引入适配分支。
"""

from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import pyqtSignal as Signal        # noqa: N812

BINDING: str = "PyQt6"

try:
    QT_VERSION: str = QtCore.qVersion()
except Exception:  # noqa: BLE001
    QT_VERSION = ""

# 常用枚举的短别名：调用点写 AlignmentFlag.AlignLeft 之类，省得每处都敲全限定名。
# **只列确实存在且确实用到的**——错一个名字就是 import 期直接崩，
# 而错误信息（"type object 'Qt' has no attribute ..."）并不告诉你该去哪儿找替代。
# 踩过的例子：FrameShape 属于 QFrame，**不在** QtCore.Qt 下。
AlignmentFlag = QtCore.Qt.AlignmentFlag
CursorShape = QtCore.Qt.CursorShape
FocusPolicy = QtCore.Qt.FocusPolicy
WindowType = QtCore.Qt.WindowType
WidgetAttribute = QtCore.Qt.WidgetAttribute
ConnectionType = QtCore.Qt.ConnectionType
TransformationMode = QtCore.Qt.TransformationMode
PenStyle = QtCore.Qt.PenStyle
MouseButton = QtCore.Qt.MouseButton
FrameShape = QtWidgets.QFrame.Shape        # 注意：在 QFrame 上，不在 Qt 上
# 尺寸策略是**枚举**：setSizePolicy(SizePolicy.Fixed, SizePolicy.Expanding)。
# 踩过的坑：误把 SizePolicy 指成 QSizePolicy 那个**类**，于是 SizePolicy.Fixed
# 报 "type object 'QSizePolicy' has no attribute 'Fixed'"。
# 真要构造 QSizePolicy(...) 对象时，直接用 QtWidgets.QSizePolicy。
SizePolicy = QtWidgets.QSizePolicy.Policy
RenderHint = QtGui.QPainter.RenderHint


def app_version_text() -> str:
    """给启动日志与提示文案用的一行绑定版本说明。"""
    return f"{BINDING} (Qt {QT_VERSION})" if QT_VERSION else BINDING


__all__ = [
    "BINDING", "QT_VERSION", "app_version_text",
    "QtCore", "QtGui", "QtWidgets", "Signal",
    "AlignmentFlag", "CursorShape", "FocusPolicy", "WindowType",
    "WidgetAttribute", "ConnectionType", "TransformationMode", "PenStyle",
    "MouseButton", "FrameShape", "SizePolicy", "RenderHint",
]
