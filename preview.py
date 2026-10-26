"""UI 预览工具：不开真机、不装低级钩子，把各个界面渲染成 PNG。

为什么要这个东西：改界面时想看图，但正常启动会把**全系统的鼠标按键**接管
（软件层改键）。预览工具因此在 import 之前就把 `MouseRemapper` 打成桩，
绝不允许碰系统输入链；设备用项目自带的模拟设备（`--selftest` 用的同一只
毒蝰 V3 极速版），所以也不会去连真鼠标。

用法：
    .venv\\Scripts\\python.exe preview.py

输出（screenshots/）：
    main.png           主面板
    remap.png          键位页
    remap_editor.png   键位页 + 选中某只键、展开编辑器
    dropdown_N.png     编辑区里第 N 个下拉框的 3 倍放大特写（查重复箭头用）
    devices.png        设备页（等后台扫描完成再抓）
    diag.png           诊断页（等后台采集完成再抓）
    hud.png            物理 DPI 键触发的右下角浮窗（独立顶层窗口，单独 grab）
    main_back.png      从诊断页切回主面板（验证切页不再改窗口尺寸/不留残留）

四个页面共用同一个窗口尺寸：日志里每步都会打
`pages={各页 sizeHint}` 与 `target=`，**每一步都必须完全一致**才算过。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "screenshots")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---- 必须在 import hexwand 之前打桩（见模块 docstring）--------------------
import hexwand_rust as _rust_mod  # noqa: E402


class _StubRemapper:
    """假 MouseRemapper：绝不装系统级钩子，接口与 Rust 版一致。"""

    def __init__(self, *a, **k) -> None:
        self.enabled = True

    def start(self) -> str:
        return ""

    def stop(self) -> None:
        pass

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def set_map(self, *a) -> None:
        pass

    def poll_events(self) -> list:
        return []

    @property
    def active(self) -> bool:
        return False

    dropped = 0
    stalls = 0


_rust_mod.MouseRemapper = _StubRemapper

from hexwand import hardware as hw                               # noqa: E402
from hexwand.hardware import controller as _ctl_mod             # noqa: E402
from hexwand.hardware.controller import Controller               # noqa: E402
# preview 绝不碰真机：Controller() 会跑 autodetect（真 SetupDi 枚举，命中即
# switch_device(save=True) 改写项目根 config.json）。这里把扫描打桩成空，
# 首启认领与 note_detected 都变成无害空转，模拟设备照常用。
# 两个名字都要打：controller 模块内的（autodetect 用）与 hw 门面上的
# （window._scan_then_paint_devices 用）——漏一个，设备页就会写真 config。
_ctl_mod.scan_known_devices = lambda: []
hw.scan_known_devices = lambda: []
from hexwand.ui.qt import QtCore, QtGui, QtWidgets                # noqa: E402
from hexwand.ui.theme import apply_theme                         # noqa: E402
from hexwand.ui.widgets import Dropdown                          # noqa: E402
from hexwand.ui.window import MainWindow, _measure_page_h        # noqa: E402


def _log_path() -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, "_preview.log")


def log(msg: str) -> None:
    """自己写日志文件，不走 stdout。

    原因：Qt 会往 stderr 打 `qt.qpa.*` 警告（本机是 DPI awareness 那句），
    混进 PowerShell 的输出流后编码不可控，读出来是二进制乱码。
    """
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def main() -> int:
    open(_log_path(), "w", encoding="utf-8").close()      # 每次清空
    app = QtWidgets.QApplication(sys.argv[:1])
    apply_theme(app)
    app.setQuitOnLastWindowClosed(False)

    controller = Controller()
    # 项目自带的模拟设备（--selftest 同款），不连真机
    controller.dev = hw.MouseDevice(pid=0x00B8, simulation=True)
    win = MainWindow(controller, listener=None, silent=False)
    win.show()
    win.raise_()

    out_dir = OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    files: list[str] = []

    def _page_info() -> str:
        hs = {k: _measure_page_h(p) for k, p in win._pages.items()}
        return (f"win={win.width()}x{win.height()} pages={hs} "
                f"target={win._target_win_h()}")

    def _content_bottom(page) -> str:
        """页面内容的最底边（相对窗口顶）与窗口高度对比 —— 数字大于窗口高就是被裁。

        光看截图不容易发现"底部被裁几像素"，量出来最直接。
        """
        if page is None:
            return "n/a"
        bottom = 0.0
        for ch in page.findChildren(
                QtWidgets.QWidget,
                options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            if not ch.isVisible():
                continue
            tl = ch.mapTo(win, QtCore.QPoint(0, 0))
            bottom = max(bottom, tl.y() + ch.height())
        return f"bottom={bottom:.0f} winH={win.height()}"

    def shoot_img(name: str, label: str, page=None, extra: str = "") -> None:
        """抓一张整窗图 + 打一行度量（页面内容底边、额外信息）。"""
        p = os.path.join(out_dir, name)
        win.grab().save(p)
        files.append(p)
        tail = f" [{_content_bottom(page)}]" if page is not None else ""
        log(f"{label}: {_page_info()}{tail}{extra}")

    def _diag_metrics(page, label: str) -> str:
        """诊断页的文本度量：溢出多少（滚动条 maximum）一眼可见。"""
        try:
            t = page.text
            n = t.toPlainText().count("\n") + 1
            return (f" lines={n} viewportH={t.viewport().height()} "
                    f"vscroll={t.verticalScrollBar().maximum()} "
                    f"hscroll={t.horizontalScrollBar().maximum()}")
        except Exception as e:  # noqa: BLE001
            return f" {label} metrics failed: {e}"

    # ---- 调度链：四个页面 + 浮窗 + 切回主面板 ----------------------------
    def shoot_main() -> None:
        # 主面板 / 键位 / 设备 / 诊断四页逐个出图；后两页依赖后台采集，
        # 每步都留够时间（设备页要跑一遍 SetupDi 扫描，诊断页还要读设备状态）
        shoot_img("main.png", "main page", win._page_main)
        win.show_page("remap")
        QtCore.QTimer.singleShot(600, shoot_remap)

    def shoot_remap() -> None:
        shoot_img("remap.png", "remap page", win._page_remap)
        # 顺手把"选中某只键 + 展开编辑器"的状态也留一张
        win.remap_page.select_button("x1")
        QtCore.QTimer.singleShot(400, shoot_editor)

    def shoot_editor() -> None:
        shoot_img("remap_editor.png", "editor page", win._page_remap,
                  extra=f" frozen_h={win.remap_page._editor_h}")
        _shoot_dropdown_zoom()
        win.show_page("devices")
        QtCore.QTimer.singleShot(1200, shoot_devices)

    def shoot_devices() -> None:
        shoot_img("devices.png", "devices page", win._page_devices,
                  extra=f" busy={win._devices_busy}")
        win.show_page("diag")
        QtCore.QTimer.singleShot(1200, shoot_diag)

    def shoot_diag() -> None:
        shoot_img("diag.png", "diag page", win._page_diag,
                  extra=f" busy={win._diag_busy}"
                        f"{_diag_metrics(win._page_diag, 'diag')}")
        # DPI 浮窗：平时只在按鼠标物理切档键时出现，这里手动触发一次出图。
        # 注意它自己是独立顶层窗口，要单独 grab。
        win.hud.show(1600, 2, "灵敏度档位 3/5")
        QtCore.QTimer.singleShot(500, shoot_hud)

    def _shoot_dropdown_zoom() -> None:
        """把编辑区里的下拉框单独抠出来放大 3 倍，专门验证"只有一个箭头"。

        为什么要单独出图：系统原生箭头（QSS `::down-arrow`）与自绘三角如果同时在
        画，整窗截图上只差几个像素、肉眼很容易漏判（用户就是这样发现"怎么有 2 个"
        而我没第一时间看出来）。抠图放大后边缘一览无余。
        """
        host = win.remap_page._editor
        dds = host.findChildren(Dropdown) if host is not None else []
        log(f"dropdowns in editor: {len(dds)}")
        for i, dd in enumerate(dds):
            pm = dd.grab()
            zoom = pm.scaled(
                pm.width() * 3, pm.height() * 3,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.FastTransformation)
            zp = os.path.join(out_dir, f"dropdown_{i}.png")
            zoom.save(zp)
            files.append(zp)
            log(f"  dropdown[{i}] {pm.width()}x{pm.height()}px -> "
                f"{os.path.basename(zp)}")

    def shoot_hud() -> None:
        hw_win = win.hud._win
        if hw_win is None:
            log("HUD FAILED: window not created")
        else:
            p = os.path.join(out_dir, "hud.png")
            hw_win.grab().save(p)
            files.append(p)
            log(f"hud: {hw_win.width()}x{hw_win.height()} "
                f"alpha={win.hud._alpha:.2f}")
            # 记下"到底摆到哪了"：光标所在屏幕、可用区、浮窗矩形、四边留白。
            # 用户问过"是不是真的右下角"，光看窗口截图看不出来，得靠这几个数。
            geo = win.hud._last_geo
            log(f"  hud rect      = {geo}")
            for s in QtWidgets.QApplication.screens():
                g = s.geometry()
                a = s.availableGeometry()
                log(f"  screen {s.name()}: full {g.width()}x{g.height()} "
                    f"x={g.left()}..{g.right()} y={g.top()}..{g.bottom()}")
                log(f"                   avail {a.width()}x{a.height()} "
                    f"x={a.left()}..{a.right()} y={a.top()}..{a.bottom()}")
                log(f"                   scale={s.devicePixelRatio()}")
            if geo:
                s = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
                g = s.availableGeometry()
                log(f"  margins       = right {g.right() - (geo[0] + geo[2])} "
                    f"/ bottom {g.bottom() - (geo[1] + geo[3])}")
            win.hud.destroy()
        # 切回主面板：这一步专门验证"切页不再改窗口尺寸 / 不留残留"。
        # 页面多了之后更要测（每一步都要重算尺寸），所以从最远的诊断页返回。
        win.show_page("main")
        QtCore.QTimer.singleShot(300, shoot_back_main)

    def shoot_back_main() -> None:
        shoot_img("main_back.png", "back to main", win._page_main)
        log("PREVIEW OK")
        for f in files:
            log("  " + os.path.basename(f))
        _cleanup()

    def _cleanup() -> None:
        """收尾：**必须走窗口自己的 `quit_app()`**。

        只 `dev.close()` + `app.quit()` 是不够的：托盘线程是**非守护**线程
        （pystray 的 `run_detached`），解释器退出时要 join 它 → 脚本明明跑完了、
        进程却一直挂着不退（2026-09-17 实际踩到，还被误当成"卡死"）。
        `quit_app()` 会依次停状态机、摘钩子（这里是桩）、收浮窗、停托盘、退事件循环。
        """
        try:
            win.quit_app()
        except Exception as e:  # noqa: BLE001
            log(f"cleanup: quit_app failed: {e}")
            try:
                controller.dev.close()
            except Exception:  # noqa: BLE001
                pass
            app.quit()

    QtCore.QTimer.singleShot(1500, shoot_main)
    app.exec()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        log("PREVIEW FAILED\n" + traceback.format_exc())
        raise SystemExit(1)
