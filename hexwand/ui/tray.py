"""托盘：pystray 封装。窗口关闭后驻留系统托盘，提供打开/自启动/电量/退出。

pystray 走 Win32 Shell_NotifyIcon，与 GUI 工具包无关，所以这一层从 Tk 时代
**原样留用**，迁移时没有重写。

两条线程契约（都因为 pystray 与 Qt 各跑各的线程）：
  1. pystray 的回调跑在它自己的线程里，**必须**经 MainWindow 的
     跨线程桥（`posted` 信号）调度回 GUI 线程才能碰控件。调用方负责调度，
     本模块只负责"把函数挂上去"。
  2. `start(on_done=…)` 的就绪回调可能在后台 tray-ready 线程触发（快路径
     0.3s 没等到时），同样由调用方负责调度回 GUI 线程。

pystray 的菜单弹出方向补丁（build.py 的 `ensure_pystray_patch()`）对本文件同样
生效——补丁改的是 pystray 库自身。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..core import autostart
from ..core.paths import ICON_PNG_PATH, ICON_TRAY_PATH

log = logging.getLogger(__name__)

_TRAY_PX = 32
# 等 pystray 在自己的线程里把图标建完的上限：成功时通常几十毫秒，
# 只有真出问题时才会等满（那时宁可多等一会儿也不要谎报"托盘可用"）。
_TRAY_READY_TIMEOUT_S = 2.0
_TRAY_READY_POLL_S = 0.05
# 快路径：GUI 线程上最多同步等这么久（够覆盖"几十毫秒就绪"的正常情况），
# 再不就绪就转后台线程继续等——启动期冻结主线程 2s 是实打实的卡顿。
_TRAY_FAST_READY_S = 0.3


def _tray_image_path() -> Path | None:
    for p in (ICON_TRAY_PATH, ICON_PNG_PATH):
        if p.exists():
            return p
    return None


class TrayIcon:
    """pystray.Icon 薄封装。回调均在托盘线程，需自行调度到 GUI 线程。"""

    def __init__(self) -> None:
        self._icon: Any = None
        self.ok = False

    def start(self,
              on_show: Callable[[], None],
              on_quit: Callable[[], None],
              on_refresh_battery: Callable[[], None],
              on_done: Callable[[bool], None] | None = None) -> bool:
        """启动托盘（run_detached）。成功返回 True，失败仅记录日志。

        on_done(ok) 提供时，就绪判定不再占住调用线程：快路径最多同步等
        `_TRAY_FAST_READY_S`，超时则由后台线程等满剩余时限并把结果交给
        on_done —— **回调可能在后台线程触发**，调用方需自行调度回 GUI 线程。
        没传 on_done 则维持旧的同步等待语义。
        """
        try:
            import pystray
            from PIL import Image

            img_path = _tray_image_path()
            if img_path is None:
                log.warning("tray icon image not found: %s / %s",
                            ICON_TRAY_PATH, ICON_PNG_PATH)
                self._set_ready(False, on_done, why="托盘图片缺失")
                return False
            tray_img = Image.open(img_path).convert("RGBA")
            tray_img = tray_img.resize((_TRAY_PX, _TRAY_PX),
                                       Image.Resampling.LANCZOS)
            self._icon = pystray.Icon(
                "HexWand",
                tray_img,
                # 初始 title 不写型号名：型号会随切换设备/设备库变化，写死在这里
                # 就成了第二个事实源。真正的文案由 MainWindow 按状态刷新。
                "HexWand · 启动中",
                pystray.Menu(lambda *_: self._build_menu(pystray, on_show,
                                                        on_quit,
                                                        on_refresh_battery)),
            )
            self._icon.run_detached()
            # `run_detached()` 只保证"起了循环线程"：图标本体是在那个线程里才建
            # 出来的（NIM_ADD / Shell_NotifyIcon），登录早期 shell 还没就绪时它
            # 会返回 FALSE，而失败**不会回传**到这里。不等就绪标志的话，主窗会把
            # 关闭按钮绑成"隐藏到托盘"——程序躲进一个根本不存在的图标里，
            # 看不见也退不出。
            if self._wait_ready(_TRAY_FAST_READY_S):
                self._set_ready(True, on_done)
                return True
            if on_done is None:
                ok = self._wait_ready(_TRAY_READY_TIMEOUT_S)
                self._set_ready(ok, None)
                return ok
            threading.Thread(
                target=self._await_ready_bg, args=(on_done,), daemon=True,
                name="tray-ready").start()
            return False          # 结论稍后经 on_done 给出
        except Exception as e:  # noqa: BLE001
            log.warning("tray icon failed: %s", e)
            self._set_ready(False, on_done, why=f"启动失败: {e}")
            return False

    def _await_ready_bg(self, on_done: Callable[[bool], None]) -> None:
        ok = self._wait_ready(_TRAY_READY_TIMEOUT_S - _TRAY_FAST_READY_S)
        self._set_ready(ok, on_done)

    def _set_ready(self, ok: bool, on_done: Callable[[bool], None] | None,
                   why: str = "") -> None:
        self.ok = ok
        if not ok:
            log.warning("托盘图标不可用（%s），按「没有托盘」处理："
                        "关闭窗口将直接退出程序", why or "未在 %ss 内就绪" % (
                            _TRAY_READY_TIMEOUT_S,))
        else:
            log.info("tray icon running")
        if on_done is not None:
            # 本方法在 start() 的 try 里被调用：回调抛错若冒泡出去，
            # except 分支会再 _set_ready(False, on_done) —— 二次回调 + 把
            # True 降级成 False。就绪结论已经给出，回调错误只记日志。
            try:
                on_done(ok)
            except Exception:  # noqa: BLE001
                log.warning("tray on_done failed", exc_info=True)

    def _wait_ready(self, timeout_s: float) -> bool:
        """等 pystray 的循环线程把图标真正建起来（Icon.visible）。

        pystray 的默认 setup 回调在它自己的线程里把 visible 置 True，也就是
        图标已经添加进通知区域之后；线程内建图标失败不会回传给调用方，所以
        这是调用方唯一能拿到的就绪信号。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self._icon is not None and self._icon.visible:
                    return True
            except Exception:  # noqa: BLE001 — 后端不支持该属性时按未就绪处理
                log.debug("读取 tray visible 失败", exc_info=True)
                return False
            time.sleep(_TRAY_READY_POLL_S)
        return False

    def _build_menu(self, pystray: Any,
                    on_show: Callable[[], None],
                    on_quit: Callable[[], None],
                    on_refresh_battery: Callable[[], None]) -> Any:
        """动态菜单：每次右键打开时重建，开机自启动勾选状态实时反映。"""
        def _toggle_autostart(_icon: Any = None, _item: Any = None) -> None:
            ok, new = autostart.toggle()
            if ok:
                self.notify("已启用开机自启动" if new else "已关闭开机自启动")
            else:
                self.notify("开机自启动设置失败")

        return pystray.Menu(
            pystray.MenuItem("打开", lambda *_: on_show(), default=True),
            pystray.MenuItem("开机自启", _toggle_autostart,
                             checked=lambda _item: autostart.is_enabled()),
            pystray.MenuItem("刷新电量", lambda *_: on_refresh_battery()),
            pystray.MenuItem("退出", lambda *_: on_quit()),
        )

    def set_tooltip(self, text: str) -> None:
        if self._icon is not None:
            try:
                self._icon.title = text
            except Exception:  # noqa: BLE001
                pass

    def notify(self, message: str, title: str = "HexWand") -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception as e:  # noqa: BLE001
                log.warning("tray notify failed: %s", e)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
