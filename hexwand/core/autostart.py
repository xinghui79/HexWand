"""开机自启动：HKCU Run 注册表键。

- 冻结态：注册 exe 自身（带 --silent，启动只留托盘）。
- 源码态：优先指向 dist/HexWand.exe（无控制台窗口）；没有则用当前解释器 + 脚本。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if sys.platform == "win32":
    import winreg

log = logging.getLogger(__name__)

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "HexWand"


def _startup_command() -> str:
    from .paths import project_dir
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --silent'
    exe_candidate = project_dir() / "dist" / "HexWand.exe"
    if exe_candidate.exists():
        return f'"{exe_candidate}" --silent'
    script = Path(sys.argv[0]).resolve()
    # 优先 pythonw.exe：注册成 python.exe 的话，开机每次都会闪一个黑色控制台
    # 窗口，用户关掉它还顺手把程序一起杀了
    gui_py = Path(sys.executable).with_name("pythonw.exe")
    interp = gui_py if gui_py.exists() else Path(sys.executable)
    return f'"{interp}" "{script}" --silent'


def _read_value() -> str | None:
    """当前 Run 键里的命令串；没有该值（或键）返回 None。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_READ) as key:
            v, _type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return v if isinstance(v, str) else None
    except OSError:
        return None


def is_enabled() -> bool:
    """是否开机自启 **并且指向当前这个可执行位置**。

    只判"值存在"是不够的：exe 挪过位置（本项目就在 OneDrive 的桌面目录下，
    改名、换盘、重打包都常见）或开发态/打包态互换之后，登录时启动的是一个
    已经不存在的路径，而托盘勾选照样显示 ✓。更糟的是 `toggle()` 会因此算出
    "关掉"，把这一项直接**删掉**——用户要点两下才修得回来。
    比对存的命令与"本构建应当写入的命令"，不一致就当未启用：这样第一次点击
    就是重写正确路径，一步修好。
    """
    got = _read_value()
    return got is not None and got.strip() == _startup_command().strip()


def set_enabled(enable: bool) -> bool:
    """写入/删除 Run 键。成功返回 True，失败记录日志并返回 False。"""
    if sys.platform != "win32":
        return False
    try:
        # CreateKey 而非 OpenKey(KEY_SET_VALUE)：HKCU\...\Run 在个别精简/新建
        # 用户配置文件里可能还不存在，OpenKey 抛 FileNotFoundError 又被下面吞掉，
        # 表现就是"自启动永远打不开、也不说为什么"。CreateKey 是"有则开、无则建"。
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            if enable:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.warning("autostart set(%s) failed: %s", enable, e)
        return False


def toggle() -> tuple[bool, bool]:
    """切换自启动状态，返回 (是否成功, **实际的**新状态)。

    失败时第二个值返回实际状态（= 旧状态）。现在唯一的调用点（托盘菜单，
    tray.py:115）本来就会先看 `ok` 再决定提示语，所以行为不变；但把"本来想设成
    的值"当成新状态返回，谁要是漏看 `ok`，界面就会显示成已启用、而注册表里
    根本没写（2026-09-17 审查）。
    """
    was = is_enabled()
    target = not was
    ok = set_enabled(target)
    return ok, (target if ok else was)
