"""应用路径：开发态 / PyInstaller 冻结态统一解析。

- 源码运行：一切相对项目根目录（hexwand 包的上一级）。
- 打包运行：可变数据（日志、配置、崩溃转储）写 %APPDATA%/HexWand；
  只读资源（图标等）从 PyInstaller 解包目录 sys._MEIPASS 读取。

**所有图片资源统一放在项目根的 `assets/`**（icon.ico / icon.png /
icon_tray.png / mascot.png）。加新图片时请一并更新 `HexWand.spec` 的
`datas`，否则源码态能跑、打包后读不到。
（键位页的鼠标图 2026-09-17 起改为 QPainter 矢量绘制，不再读图片资源。）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "HexWand"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_data_dir() -> Path:
    """可写数据目录（日志/配置/崩溃转储）。

    正常 Windows 用户会话下 APPDATA 一定存在（资源管理器拉起的开机自启进程
    同样继承完整环境）。这里仍保留兜底：某些精简环境/沙箱会剥掉 APPDATA，
    此时退回 LOCALAPPDATA，再退回主目录下的**隐藏**目录——避免直接在
    C:\\Users\\<用户名>\\ 里凭空建一个可见文件夹。
    """
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    d = (Path(base) / APP_NAME) if base else (Path.home() / f".{APP_NAME}")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def project_dir() -> Path:
    """项目根目录（仅源码运行时有意义）。"""
    if is_frozen():
        return app_data_dir()
    return Path(__file__).resolve().parents[2]


def resource(rel: str) -> Path:
    """只读资源路径（图标等）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / rel
    return project_dir() / rel


LOG_PATH = app_data_dir() / "startup.log"
CRASH_PATH = app_data_dir() / "crash.log"

# 只读资源统一在 assets/ 下（见模块 docstring）
ICON_PATH = resource("assets/icon.ico")
ICON_PNG_PATH = resource("assets/icon.png")
ICON_TRAY_PATH = resource("assets/icon_tray.png")
MASCOT_PATH = resource("assets/mascot.png")   # DPI 浮窗右侧的看板娘（透明底 PNG）
