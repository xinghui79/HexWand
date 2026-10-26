"""主窗口：深色卡片布局 + 设备状态机接线 + 跨线程调度桥。

【本包的前身是一套 Tk/Tkinter 实现，已于 2026-09-16 移除（备份见
`%APPDATA%\\HexWand\\backup\\`）。下文提到的「Tk 版」都指那一版。】

**设备逻辑一行没改**：`_set_link`
单一写入点、`LinkMonitor` 回调接线、电量多采样取中位数、切换设备的 epoch 护栏、
`_probe_once` 心跳语义、写档位/轮询率的"设备没采纳就回滚 UI"——这些都是与 GUI
无关的业务规则，只是把载体从 `tk.StringVar` / `root.after` 换成了普通属性与
Qt 信号。

=== 迁移要点（这段是本次重构最需要理解的地方）===

1. **跨线程调度桥换成 Qt 信号。**
   Tk 版所有后台线程回调都经 `_post_to_main` → `root.after(0, fn)` 回主线程。
   Qt 里对应物是"发射一个信号，槽在 GUI 线程执行"：本类的 `posted` 信号显式以
   `QueuedConnection` 连到 `_run_posted`。**机制换了，纪律没换**——子线程里
   依然绝对不能碰控件/控件状态（Qt 下违反是未定义行为，可能直接崩，
   比 tkinter 抛 RuntimeError 更隐蔽）。

2. **DPI 缩放的手工乘算全部删掉。**
   Tk 版到处是 `int(NAV_BTN_W * self._ui_scale)`（`_remap_w` / `_hot_r_main` /
   `_wrap_w` 等）。Qt6 的坐标本来就是设备无关像素、高 DPI 缩放默认开启，
   保留这些乘算会在 150% 屏上变成 1.5×1.5=2.25 倍。**所以本文件里找不到
   `_ui_scale`，这是有意的。**

3. **尺寸策略：窗口不再自己算高度。**
   Tk 版要量 `winfo_reqheight()`、"卡片壳开销"、"chrome 高度"，再加一堆经验常数
   才算得出改键页该多高（还踩过"窗口刚 resize 时 pack 还没重排，每切一次页面长高
   一截"的坑）。Qt 里交给布局：切换页面后取 `layout().sizeHint().height()`，
   与窗口当前尺寸无关，不会漂移。

4. **静默启动必须 setQuitOnLastWindowClosed(False)。**
   Tk 的 `withdraw()` 之后 mainloop 照跑；Qt 一旦最后一个窗口被隐藏，事件循环
   会直接结束——程序"启动了但立刻退出"。这个开关在 `run_gui` 里统一设。
"""

from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
import time
import weakref

from .. import hardware as hw
from ..core import keyboard as kb
from ..core.paths import ICON_PATH
from ..core.single_instance import start_listener
from ..hardware.controller import Controller
from ..hardware.monitor import LinkMonitor, closest_stage
from . import pages as dlg
from .hud import DpiHud
# 软件层改键的 Rust 实现（rust/ 目录，cargo build 后得到 hexwand_rust.pyd）
from hexwand_rust import MouseRemapper
from .qt import (
    AlignmentFlag,
    ConnectionType,
    CursorShape,
    FocusPolicy,
    QtCore,
    QtGui,
    QtWidgets,
    Signal,
)
from .remap import RemapPage
from .theme import (
    FONT_BIG,
    FONT_HEAD_META,
    FONT_LABEL,
    FONT_POLL_BIG,
    FONT_SEG_VALUE,
    FONT_SMALL,
    FONT_TITLE,
    THEME,
)
from .battery import BatteryMixin
from .winsize import WIN_W, WindowSizeMixin
# 重新导出：`preview.py` 从本模块 import 它（量测每页高度用）
from .winsize import _measure_page_h as _measure_page_h  # noqa: F401
from .tray import TrayIcon
from .widgets import (
    BlockedClickWatcher,
    PillButton,
    RoundedInput,
    SegmentedControl,
    StageSegment,
    VBar,
    caption,
    hline,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 未连接时轮询率卡片显示的默认档位（读不到真值就不装作是设备当前值）；
# 连上之后由 _sync_device 用设备读回的真实值覆盖
DEFAULT_POLL_RATE: int = 500
# 2000/4000/8000 不在协议层的 POLL_TO_VAL 里 → Controller.set_poll 会自动改走
# set_poll_v2（HyperPolling 接收器的实验性命令；被设备拒绝时状态栏如实报错，
# 不会假装成功）。用户 2026-09-16 要求把 4000/8000 也放进选项。
POLL_OPTIONS: list[int] = [125, 250, 500, 1000, 2000, 4000, 8000]
STAGE_COUNT: int = 5
DPI_MIN: int = 100
# 未连接/还没同步到设备真值时高亮哪一档（0 基）。单列一个常量是因为
# "退回占位档"这件事现在有两处要做：初始值与转离线时的重置，两处必须一致。
STAGE_PLACEHOLDER: int = 2


# ===========================================================================
# MainWindow
# ===========================================================================
class MainWindow(WindowSizeMixin, BatteryMixin,
                 QtWidgets.QWidget):
    """主窗口。所有 UI 更新都发生在 GUI 线程；后台线程一律经 `_post_to_main`。"""

    # 跨线程调度桥：后台线程 emit(fn) → GUI 线程执行 fn
    posted = Signal(object)

    def __init__(self, controller: Controller, listener=None,
                 silent: bool = False) -> None:
        super().__init__()
        self.controller = controller
        self.theme = THEME
        self._listener = listener

        # ---- 跨线程桥：显式 QueuedConnection，槽一定在 GUI 线程跑 ----
        self.posted.connect(self._run_posted, ConnectionType.QueuedConnection)

        # 软件层改键：Rust 实现（PyO3）。映射表在键位页改动时经 `remapped`
        # 信号推给 Rust（键名→VK 的解析留在 Python，钩子回调只碰 Rust 数据、
        # 绝不跨 FFI 回调 Python——那是把 GIL 问题带回钩子线程的歧途）。
        # 失败不影响程序其它功能（原因记在 _remap_err 里，键位页底部会显示）。
        self.remapper = MouseRemapper()
        self._remap_err = ""

        self.tray = TrayIcon()
        self.hud = DpiHud(self, STAGE_COUNT)

        # 连接状态（唯一事实源，构造后立即以 offline 应用一次 UI）：
        #   None      = 尚未应用任何状态（构造完成瞬间）
        #   "online"  = 心跳连续成功（鼠标本体可达）；UI 绿字"已连接"
        #   "offline" = 心跳连续失败 / 尚未确认；UI 暗字"未连接"
        self._link: str | None = None

        self._build_window(silent)
        self._build_vars()
        self._build_layout()
        self._wire_tray()
        self._install_exit_hooks()

        # 应用级事件过滤器：点空白处收掉输入框焦点（Qt 不会自己做，
        # 见 `_dismiss_edit_focus`）。必须是应用级——事件过滤器只看得见发给
        # 自己的事件，装到本窗口上收不到子容器里发生的点击。
        _app = QtWidgets.QApplication.instance()
        if _app is not None:
            _app.installEventFilter(self)

        self._paint_active()
        self._paint_poll()
        self._set_inputs_enabled(False)
        self._paint_conn(online=False)

        # 唯一后台状态机（纯逻辑，见 hardware/monitor.py）：探测/心跳/DPI 同步/
        # 电量节拍全部经回调上报，这里把回调调度回主线程执行。
        self._monitor = LinkMonitor(
            probe=self._probe_once,
            read_dpi=self._read_dpi_once,
            on_link=lambda t: self._post_to_main(lambda: self._set_link(t)),
            on_dpi=lambda idx, x: self._post_to_main(
                lambda: self._on_dpi_sync(idx, x)),
            on_battery_tick=lambda: self._post_to_main(self._batt_display),
            stages=self.stages,
            reopen_hint=self._try_reopen,
        )
        self._monitor.start()
        self._remap_err = self.remapper.start()
        if self._remap_err:
            log.warning("改键未生效：%s", self._remap_err)
        else:
            log.info("改键已接管鼠标按键（低级钩子）")
        self._refresh_remap_state()

        # 启动即应用离线 UI（状态栏文案/输入禁用/tooltip），不等首轮探测结果
        self._set_link("offline")
        log.info("Qt UI 就绪（%s）", self._qt_desc())

    # ------------------------------------------------------------------
    # 窗口
    # ------------------------------------------------------------------
    @staticmethod
    def _qt_desc() -> str:
        try:
            from .qt import app_version_text
            return app_version_text()
        except Exception:  # noqa: BLE001
            return "?"

    def _build_window(self, silent: bool) -> None:
        self._silent = silent          # _wire_tray 在托盘失败时要用它兜底
        self.setWindowTitle("HexWand")
        self.setObjectName("root")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        try:
            if ICON_PATH.exists():
                self.setWindowIcon(QtGui.QIcon(str(ICON_PATH)))
        except Exception:  # noqa: BLE001
            pass
        # 尺寸由程序按内容算，不允许用户拖边缘改：拖大后卡片只是留出一片空白
        # 底色，拖小则内容被裁（原版 resizable(False, False) 的同一理由）。
        self.setFixedWidth(WIN_W)
        if silent:  # 开机自启/静默模式：不显示窗口，只留托盘
            self.hide()

    def _build_vars(self) -> None:
        self._status_text = "启动中…"
        # 配置里可能存着界面上没有按钮的档位值（4000/8000 等），那样五个按钮
        # 都不高亮 —— 退回默认档
        pr = self.controller.cfg.poll_rate
        self.poll_rate = pr if pr in POLL_OPTIONS else DEFAULT_POLL_RATE
        # 未连接时默认高亮第 3 档；连上后由 _sync_device 用设备当前 DPI
        # 落到的最接近档位覆盖
        self.active_idx = STAGE_PLACEHOLDER
        # 抑制 HUD 的**瞬时**标志：只在"收浮窗 → 隐藏主窗"这一小段里为真，
        # 挡住这期间已排队的切档回调（否则会在桌面留一个孤儿浮窗）。
        # 下一轮事件循环就解禁——托盘后台运行时物理切档**照样要弹浮窗**。
        self._hud_suppressed = False
        # 退出过程中为真：monitor/钩子都停了，但可能还有排队回调，一律不再建浮窗
        self._quitting = False
        # 设备页最近一次扫描到的 PID 集合：链路状态变化时用它原地重绘，
        # 不必重新扫一遍 HID（见 _repaint_devices_cached）
        self._dev_pids: set[int] = set()
        # 切换设备期间为 True：_set_link 忽略排队的 "online"
        self._switching_devices = False
        # 刚切换过目标鼠标：状态机作废 DPI 基线后第一次上报只是"把高亮跟到新鼠标
        # 当前档"，不是用户按了物理切档键 → 只同步数值，不播状态、不弹 HUD
        self._dpi_silent_sync = False
        self._page_cur = "main"

        stages = list(self.controller.cfg.stages or
                      [400, 800, 1600, 3200, 6400])
        stages = (stages + [400, 800, 1600, 3200, 6400])[:STAGE_COUNT]
        self.stages: list[int] = stages
        # 设备页/诊断页的"正在刷新"标志：切页会触发一次后台采集，避免重复排队
        self._devices_busy = False
        self._diag_busy = False
        # 窗口高度：量到连续两次相同就锁定（见 `_target_win_h` 的说明）
        self._win_h_prev = 0
        self._win_h_locked = 0
        # 锁定时所处的屏幕环境（分辨率/缩放/逻辑 DPI）：环境一变就作废重算
        self._win_h_fp: tuple = ()
        # resize 后要恢复的窗口顶边（见 `_set_window_size`）
        self._keep_y: int | None = None

    # ------------------------------------------------------------------
    # 状态辅助
    # ------------------------------------------------------------------
    @property
    def link_state(self) -> str | None:
        return self._link

    def _run_posted(self, fn) -> None:
        """GUI 线程：执行后台线程投递过来的回调。"""
        try:
            fn()
        except Exception:  # noqa: BLE001 — 收尾竞态下不该把异常抛进事件循环
            log.debug("posted callback failed", exc_info=True)

    def _post_to_main(self, fn) -> None:
        """把后台线程回调调度回 GUI 线程执行。

        取代 Tk 版的 `root.after(0, fn)`：QObject 信号跨线程发射是线程安全的，
        投递本身不会失败，所以这里不需要原版那句"窗口已销毁就静默丢弃"的兜底
        （退出后信号无人接收，自然什么都不发生）。
        """
        try:
            self.posted.emit(fn)
        except RuntimeError:
            # C++ 对象已销毁（退出竞态）
            pass

    # ------------------------------------------------------------------
    # 焦点（点空白处收掉输入框光标）
    # ------------------------------------------------------------------
    def eventFilter(self, obj, evt) -> bool:  # noqa: N802 — Qt 命名
        """应用级截获鼠标按下 → 判断要不要收掉输入框的焦点。

        装成**应用级**过滤器（而不是装在本窗口上）：Qt 的事件过滤器只看得见
        发给自己的事件，装到窗口上收不到子容器（面板、卡片、档位段框）里的点击。
        """
        try:
            if (evt.type() == QtCore.QEvent.Type.MouseButtonPress
                    and not getattr(self, "_quitting", False)):
                self._dismiss_edit_focus(evt)
        except Exception:  # noqa: BLE001
            # 虚拟方法里抛出的异常在 PyQt 里会被当成致命错误 → 直接 abort 整个进程
            # （不是"打印一下就过"）。收尾竞态下 widgetAt/focusWidget 拿到半销毁
            # 对象、parentWidget 链断掉都是有可能的，所以必须自己吞掉。
            log.debug("dismiss edit focus failed", exc_info=True)
        return super().eventFilter(obj, evt)

    def _dismiss_edit_focus(self, evt) -> None:
        """鼠标按在**惰性区域**（面板底色、分隔线、纯文字标签…）时收掉输入框焦点。

        为什么需要：Qt 里鼠标按在 **FocusPolicy=NoFocus** 的部件上**不会**把焦点
        从当前输入框移走 —— 用户点走了空白处，输入框里的打字光标还一直在闪
        （2026-09-17 反馈）。

        两个必须防的误判（都踩过）：
        - 一次点击会沿父链**冒泡多次**（探针实测 9 次），过滤器每次都会跑，
          所以判据必须只看"点在哪"，不能看调用次数；
        - 档位段的「档 N」标签与输入框是**兄弟**，点它不算"点在输入框内部"。
          若按"是否在输入框里"来判，就会在段落激活、输入框刚拿到焦点之后又把
          焦点清掉 —— 于是"点整段就能改数字"这个既有行为被弄坏。
          所以再加一条：点到交互件（按钮/下拉/自绘热区）就一律不管。

        另外，点在别的顶层窗口（下拉弹层、HUD）上时也不管，交给它们自己处理。
        """
        fw = QtWidgets.QApplication.focusWidget()
        if fw is None or fw is self:
            return
        try:
            pos = evt.globalPosition().toPoint()
        except AttributeError:          # 非 QMouseEvent（没有全局坐标）
            pos = QtGui.QCursor.pos()
        # 整段都要兜底：widgetAt / window() / parentWidget 在"控件正被销毁"时会抛
        # RuntimeError，而本函数是从 eventFilter 里调的（异常会 abort 进程）。
        try:
            w = QtWidgets.QApplication.widgetAt(pos)
            if w is not None and w.window() is not self.window():
                return
            p = w
            while p is not None and p is not self:
                if p is fw:
                    return              # 点在焦点部件自己（或它的子控件）上
                if self._is_interactive(p):
                    return              # 点在按钮/下拉/段框这类"有事发生"的部件上
                p = p.parentWidget()
            fw.clearFocus()
        except (RuntimeError, AttributeError):
            return

    @staticmethod
    def _is_interactive(w) -> bool:
        """这个部件是不是"点了有事发生"的交互件（用于区分空白区与交互区）。

        两类判据：① 手型光标（按钮/下拉/自绘热区都设了）；② 接受焦点
        （输入框、QPushButton 等）。真正惰性的只有面板底色、分隔线、纯文字标签。
        """
        try:
            if w.cursor().shape() == CursorShape.PointingHandCursor:
                return True
            return w.focusPolicy() != FocusPolicy.NoFocus
        except (AttributeError, RuntimeError):
            return False

    def set_status(self, msg: str) -> None:
        """更新状态栏。可安全地从后台线程调用：自动排队到 GUI 线程。"""
        if threading.current_thread() is not threading.main_thread():
            self._post_to_main(lambda m=msg: self.set_status(m))
            return
        self._status_text = msg
        try:
            self._status_lbl.setText(msg)
            self._update_status_dot()
        except (AttributeError, RuntimeError):  # 构造早期尚未创建
            pass
        log.info("[status] %s", msg)

    def _update_status_dot(self) -> None:
        """状态栏文案变化时更新状态点颜色（offline 红 / online 绿 / 其他 紫）。"""
        msg = self._status_text
        t = self.theme
        if "失败" in msg or "离线" in msg or "出错" in msg:
            color = t.offline
        elif "就绪" in msg or "已连接" in msg or "同步完成" in msg:
            color = t.good
        else:
            color = t.accent
        try:
            self._status_dot.setStyleSheet(f"color: {color};")
        except (AttributeError, RuntimeError):
            pass

    def run_bg(self, fn) -> threading.Thread:
        """设备 I/O 放后台线程，避免阻塞 UI。

        所有经此启动的线程统一套异常兜底：退出/销毁竞态下可能抛各种异常，
        一律记录日志而不是裸打印 traceback。
        """
        def _safe() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("[bg] %s 异常", getattr(fn, "__name__", "task"))

        th = threading.Thread(target=_safe, daemon=True)
        th.start()
        return th

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        t = self.theme
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶栏（单行）：左标题，右「设备名 + 连接状态」----
        # 用户 2026-09-16 要求"当前设备移到未连接那里"：原先设备名占一整行副标题，
        # 与右侧状态分开、顶栏也更高。合并后信息不重复（"离线"由状态词表达，
        # 不再往名字后追加"· 离线"），顶栏矮一行。
        head = QtWidgets.QWidget(self)
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(24, 18, 24, 10)
        hl.setSpacing(10)
        # 标题用主题紫：白字在这块深底上太"素"，压不住右侧的彩色信息
        title = QtWidgets.QLabel("HexWand", head)
        title.setFont(FONT_TITLE)
        title.setStyleSheet(f"color: {t.accent};")
        hl.addWidget(title, 0, AlignmentFlag.AlignVCenter)
        hl.addStretch(1)
        self._dev_lbl = QtWidgets.QLabel("", head)
        self._dev_lbl.setFont(FONT_HEAD_META)
        self._dev_lbl.setStyleSheet(f"color: {t.dim};")
        hl.addWidget(self._dev_lbl, 0, AlignmentFlag.AlignVCenter)
        self._conn_txt = QtWidgets.QLabel("未连接", head)
        self._conn_txt.setFont(FONT_HEAD_META)
        hl.addWidget(self._conn_txt, 0, AlignmentFlag.AlignVCenter)
        root.addWidget(head, 0)
        self._head = head
        self._refresh_subtitle()

        # ---- 页面导航（主面板 / 键位 / 设备 / 诊断）：整行等宽按钮 ----
        navrow = QtWidgets.QWidget(self)
        nl = QtWidgets.QHBoxLayout(navrow)
        nl.setContentsMargins(24, 0, 24, 12)
        nl.setSpacing(8)
        self._nav_btns: dict[str, PillButton] = {}
        for key, text in (("main", "主面板"), ("remap", "键位"),
                          ("devices", "设备"), ("diag", "诊断")):
            b = PillButton(text, navrow, width=78)   # 四个等宽，不按字数各给一个
            b.clicked.connect(lambda _c=False, k=key: self.show_page(k))
            nl.addWidget(b, 0)
            self._nav_btns[key] = b
        nl.addStretch(1)
        root.addWidget(navrow, 0)
        self._navrow = navrow

        # ---- 页面宿主 ----
        self._stack = QtWidgets.QStackedWidget(self)
        self._stack.setObjectName("page")
        root.addWidget(self._stack, 1)

        self._page_main = QtWidgets.QWidget(self._stack)
        self._stack.addWidget(self._page_main)
        self._build_main_page(self._page_main)

        self._page_remap = QtWidgets.QWidget(self._stack)
        rl = QtWidgets.QVBoxLayout(self._page_remap)
        rl.setContentsMargins(24, 0, 24, 12)
        self.remap_page = RemapPage(self.controller, self.remapper,
                                    self._remap_err, self._page_remap)
        self.remap_page.status.connect(self.set_status)
        # 键位页每次改动映射（set/clear）后把新表推给 Rust 钩子
        self.remap_page.remapped.connect(self._push_remap_map)
        rl.addWidget(self.remap_page, 1)
        self._stack.addWidget(self._page_remap)
        self._push_remap_map()

        # Rust 钩子的状态事件（救援键切换 / 合成卡死自动暂停 / 钩子意外停摆）
        # 走轮询：Rust 侧绝不跨 FFI 回调 Python
        self._remap_evt_timer = QtCore.QTimer(self)
        self._remap_evt_timer.timeout.connect(self._poll_remap_events)
        self._remap_evt_timer.start(300)

        # 设备页 / 诊断页：原先是两个独立子窗口，现在同一层页面
        # （页面自己覆写了 sizeHint，不会把窗口顶高，见 pages 模块 docstring）
        self._page_devices = dlg.DevicesPage(self)
        self._stack.addWidget(self._page_devices)
        self._page_diag = dlg.DiagnosePage(self)
        self._stack.addWidget(self._page_diag)
        self._pages = {
            "main": self._page_main,
            "remap": self._page_remap,
            "devices": self._page_devices,
            "diag": self._page_diag,
        }

        # ---- 状态栏 ----
        sb = QtWidgets.QWidget(self)
        sb.setObjectName("statusBar")
        sb.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        sl = QtWidgets.QHBoxLayout(sb)
        sl.setContentsMargins(18, 6, 18, 6)
        sl.setSpacing(6)
        self._status_dot = QtWidgets.QLabel("●", sb)
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setFont(FONT_SMALL)
        sl.addWidget(self._status_dot, 0)
        self._status_lbl = QtWidgets.QLabel(self._status_text, sb)
        self._status_lbl.setObjectName("statusText")
        self._status_lbl.setFont(FONT_SMALL)
        sl.addWidget(self._status_lbl, 0)
        sl.addStretch(1)
        root.addWidget(sb, 0)
        self._statusbar = sb

        self._update_status_dot()
        self.show_page("main")

    # ------------------------------------------------------------------
    # 主面板（Apple 设置风格：一个分组容器 + 行式分区）
    # ------------------------------------------------------------------
    @staticmethod
    def _panel_row(parent, pad_y: int = 12) -> tuple:
        """分组容器里的一行：左右内边距 18（分隔线也按这个 inset 对齐）。"""
        row = QtWidgets.QWidget(parent)
        hl = QtWidgets.QHBoxLayout(row)
        hl.setContentsMargins(18, pad_y, 18, pad_y)
        hl.setSpacing(12)
        return row, hl

    @staticmethod
    def _row_label(text: str, parent) -> QtWidgets.QLabel:
        """分组容器里每行的标题。

        **五行必须同规格**（12pt 暗色）—— 用户指出"电量"两个字太小，但单独
        改大一行又会和 DPI 档位/轮询率/节能参差，所以统一升一级。辅助说明
        （"点击切换 · 回车保存"之类）仍用 9pt caption，主次分明。
        """
        lbl = QtWidgets.QLabel(text, parent)
        lbl.setFont(FONT_LABEL)
        lbl.setStyleSheet(f"color: {THEME.dim};")
        return lbl

    @staticmethod
    def _panel_divider(parent):
        """1px 分隔线，**从左内边距开始**（满宽的线会把整块切得太生硬，

        macOS/iOS 设置的分隔线也只到文字左沿，而不是贯穿整个容器）。
        """
        wrap = QtWidgets.QWidget(parent)
        hl = QtWidgets.QHBoxLayout(wrap)
        hl.setContentsMargins(18, 0, 18, 0)
        hl.setSpacing(0)
        hl.addWidget(hline(wrap), 1)
        return wrap

    def _build_main_page(self, page: QtWidgets.QWidget) -> None:
        """主面板：一个 inset grouped 容器，内部按"行"分区。

        2026-09-16 两轮反馈后定的版式（用户："我没说一定要 4 个卡，你觉得苹果
        公司会如何设计"）。原先是四张等高小卡片，窗口高度按较高的键位页对齐后
        每张卡都被拉高、内容顶在中间四周全空 —— 本质问题是**在用卡片数量填充
        信息密度不够的版面**。

        苹果的做法（macOS 系统设置 / iOS 设置）：
          · **一个**分组容器，内部按行分区，行间用细分隔线，不用多张卡；
          · 每行是"左 label / 右 值·控件"，行高固定（44~56），信息密度由行决定，
            不需要靠拉高容器来"填满"；
          · 留白在**容器外**（背景留白），而不是卡片内部的空 —— 这是"空"和
            "留白"的区别；
          · 当前值本身就是界面元素（电量、轮询率都是大字），控件退居"改"的角色。

        富余高度只给 DPI 档位段（这一页唯一的主交互，点击区域越大越好），
        其余行保持自然高度。
        """
        t = self.theme
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(24, 0, 24, 12)
        lay.setSpacing(0)
        self.power_entries: list[RoundedInput] = []

        panel = QtWidgets.QFrame(page)
        panel.setObjectName("card")
        panel.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        pl = QtWidgets.QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        lay.addWidget(panel, 1)

        # ---- 电量（hero 行）----
        # 用户 2026-09-17 反馈：① 左侧的"未连接/无线连接"和顶栏状态重复，删；
        # ② 大数字要放大到"应有的大小"（hero 值，FONT_BIG 已升到 40pt）。
        # 行结构即苹果设置行的节奏：label 贴左，值（大数字 + 电量条）贴右。
        brow, bhl = self._panel_row(panel, pad_y=16)
        bhl.addWidget(self._row_label("电量", brow), 0)
        bhl.addStretch(1)
        self._batt_lbl = QtWidgets.QLabel("--", brow)
        self._batt_lbl.setFont(FONT_BIG)
        self._batt_lbl.setStyleSheet(f"color: {t.warn};")
        bhl.addWidget(self._batt_lbl, 0, AlignmentFlag.AlignVCenter)
        # 竖直电量条：贴行右缘撑满高度（横向空间窄，横条画不开）
        self._batt_vbar = VBar(brow, width=12, theme=t)
        bhl.addWidget(self._batt_vbar, 0)
        pl.addWidget(brow, 0)

        pl.addWidget(self._panel_divider(panel), 0)

        # ---- DPI 档位（吃富余高度的主交互区）----
        drow, dhl = self._panel_row(panel, pad_y=12)
        dcol = QtWidgets.QWidget(drow)
        dl = QtWidgets.QVBoxLayout(dcol)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(8)
        dhead = QtWidgets.QWidget(dcol)
        dh = QtWidgets.QHBoxLayout(dhead)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.setSpacing(8)
        dh.addWidget(self._row_label("DPI 档位", dhead), 0)
        dh.addStretch(1)
        dh.addWidget(caption("点击切换 · 回车保存", dhead), 0)
        dl.addWidget(dhead, 0)
        slot = QtWidgets.QFrame(dcol)
        slot.setObjectName("segSlot")
        slot.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        sl = QtWidgets.QHBoxLayout(slot)
        sl.setContentsMargins(6, 6, 6, 6)
        sl.setSpacing(4)
        self.stage_segs: list[StageSegment] = []
        for i in range(STAGE_COUNT):
            seg = StageSegment(i, slot)
            seg.set_value(self.stages[i])
            seg.activated.connect(self.on_stage_activate)
            seg.committed.connect(self.on_stage_return)
            seg.blurred.connect(self.restore_stage)
            sl.addWidget(seg, 1)
            self.stage_segs.append(seg)
        dl.addWidget(slot, 1)          # 段吃掉这一行的富余高度
        dhl.addWidget(dcol, 1)
        pl.addWidget(drow, 1)          # 整行吃容器里的富余高度

        pl.addWidget(self._panel_divider(panel), 0)

        # ---- 轮询率 ----
        prow, phl = self._panel_row(panel, pad_y=12)
        pcol = QtWidgets.QWidget(prow)
        pl2 = QtWidgets.QVBoxLayout(pcol)
        pl2.setContentsMargins(0, 0, 0, 0)
        pl2.setSpacing(8)
        phead = QtWidgets.QWidget(pcol)
        ph = QtWidgets.QHBoxLayout(phead)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.setSpacing(8)
        ph.addWidget(self._row_label("轮询率", phead), 0)
        ph.addStretch(1)
        # 当前值大字：既是信息层次（一眼看到设备现在的值），也吃掉一段高度。
        # 跟随 `_paint_poll` 与五段高亮**同源更新**，不会出现"大字一个值、
        # 高亮另一格"的假显示。
        self._poll_big = QtWidgets.QLabel("--", phead)
        self._poll_big.setFont(FONT_POLL_BIG)
        self._poll_big.setStyleSheet(f"color: {t.accent_light};")
        ph.addWidget(self._poll_big, 0)
        pl2.addWidget(phead, 0)
        self._poll_seg = SegmentedControl(pcol)
        for v in POLL_OPTIONS:
            self._poll_seg.add(str(v), v, font=FONT_SEG_VALUE)
        self._poll_seg.picked.connect(self.on_poll_click)
        # 未连接时段整体禁用，点击**没有任何反馈**（发不出 clicked）——
        # 装个"禁用态点击"哨兵，点下去照样告诉用户为什么不能改。
        _poll_guard = BlockedClickWatcher(
            self._poll_seg, [b for b, _v in self._poll_seg.buttons()],
            "未连接，无法设置轮询率")
        _poll_guard.blocked.connect(self.set_status)
        pl2.addWidget(self._poll_seg, 0)
        pl2.addWidget(
            caption("2000Hz 及以上需要 HyperPolling 接收器配合", pcol), 0)
        phl.addWidget(pcol, 1)
        pl.addWidget(prow, 0)

        pl.addWidget(self._panel_divider(panel), 0)

        # ---- 节能（两行设置：label 左 / 输入框右）----
        # sender 必须**在调用时**才解析 self.controller.dev：switch_device 会整体
        # 换掉 dev 对象，构造时捕获绑定方法会让切换后仍往旧设备（旧句柄）写。
        pl.addWidget(self._make_power_row(
            panel, "闲置睡眠（分钟）", "idle_minutes",
            1, 15, 5,
            lambda v: self.controller.dev.set_idle_timeout(v)), 0)
        pl.addWidget(self._panel_divider(panel), 0)
        pl.addWidget(self._make_power_row(
            panel, "低电量阈值（%）", "low_battery_pct",
            5, 100, 20,
            lambda v: self.controller.dev.set_low_battery_threshold(v)), 0)

    # ------------------------------------------------------------------
    # 页面切换 / 尺寸
    # ------------------------------------------------------------------
    def show_page(self, name: str) -> None:
        """切换页面（main 主面板 / remap 键位 / devices 设备 / diag 诊断）。

        切到"设备""诊断"时会顺手触发一次后台采集 —— 这两个页面的内容是**会变的**
        （设备插拔、链路状态），做成子窗口时是"点按钮 = 打开并刷新"，
        改成页面后必须保证"每次进来都刷新"，否则会看到上一次的旧状态。
        """
        if self._page_cur == "remap" and name != "remap":
            # 离开键位页就收掉"等你按键"状态：否则轮询会在后台继续跑，而且之后
            # 随便按一下键都会悄悄改写那只按键的映射
            self.remap_page.leave()
        if name not in self._pages:
            name = "main"
        self._page_cur = name
        self._stack.setCurrentWidget(self._pages[name])
        if name == "remap":
            self.remap_page.focus_first()
        elif name == "devices":
            self.refresh_devices()
        elif name == "diag":
            self.refresh_diagnose()
        self._apply_page_size_safe()
        for key, b in self._nav_btns.items():
            b.set_active(key == name)

    # Rust 钩子的映射推送与状态轮询
    # ------------------------------------------------------------------
    _RUST_MOUSE_IDX: dict[str, int] = {
        "left": 0, "right": 1, "middle": 2,
        "x1": 3, "x2": 4, "wheel_up": 5, "wheel_down": 6,
    }

    def _push_remap_map(self) -> None:
        """把 cfg.keymap 解析成 Rust 钩子的动作表并推送。

        键名→VK 的解析在这一步完成（kb.parse_hotkey）：钩子回调里只查 VK
        列表合成输入，绝不跨 FFI 回调 Python。半成品映射（类型选了但键名 /
        目标为空）不推送 = 钩子放行原事件，与 Python 版行为一致。
        """
        bids: list[str] = []
        kinds: list[int] = []
        data: list[list[int]] = []
        for bid, act in self.controller.get_keymap().items():
            atype = act.get("type", "default")
            if atype == "none":
                bids.append(bid)
                kinds.append(0)
                data.append([])
            elif atype == "key":
                vks = kb.parse_hotkey(act.get("key", ""))
                if vks:
                    bids.append(bid)
                    kinds.append(1)
                    data.append([int(v) for v in vks])
            elif atype == "mouse":
                target = act.get("button", "")
                if target in self._RUST_MOUSE_IDX:
                    bids.append(bid)
                    kinds.append(2)
                    data.append([self._RUST_MOUSE_IDX[target]])
        try:
            self.remapper.set_map(bids, kinds, data)
        except Exception as e:  # noqa: BLE001 — 推送失败不该带崩界面
            log.warning("推送映射表到 Rust 钩子失败：%s", e)

    def _poll_remap_events(self) -> None:
        """取 Rust 钩子的状态事件（救援键切换 / 卡死暂停 / 意外停摆）。

        整段自吞异常，理由同 `eventFilter`：这是 QTimer 回调，槽里抛出的异常
        会被 PyQt 判成致命错误并终止进程；而 `_refresh_remap_state` 要碰控件，
        收尾竞态下可能抛 RuntimeError（以前只兜住了 `poll_events` 那一行）。
        """
        try:
            events = self.remapper.poll_events()
            if events:
                self._refresh_remap_state()
        except Exception:  # noqa: BLE001
            log.debug("poll remap events failed", exc_info=True)

    def _refresh_subtitle(self) -> None:
        """顶栏右侧的**设备名**跟随当前目标鼠标（连接状态由 `_paint_conn` 负责）。

        名字来自 cfg.pid（目标设备），鼠标关机后目标不变、名字保留——
        在线/离线由旁边的状态词表达，名字上不再追加"· 离线"（两处说一件事）。
        """
        info = hw.known_device(self.controller.cfg.pid)
        try:
            self._dev_lbl.setText(info.product if info else "未选择设备")
        except (AttributeError, RuntimeError):
            pass

    # ------------------------------------------------------------------
    # 连接状态
    # ------------------------------------------------------------------
    def _paint_conn(self, online: bool) -> None:
        """连接指示：只有文字、无圆点——在线绿字 / 离线暗字。"""
        t = self.theme
        try:
            self._conn_txt.setText("已连接" if online else "未连接")
            self._conn_txt.setStyleSheet(
                f"color: {t.good if online else t.dim};")
        except (AttributeError, RuntimeError):
            pass

    def _set_link(self, target: str) -> None:
        """GUI 线程：连接状态的唯一写入点，驱动 UI 整体刷新。

        target: "online"（鼠标本体可达，可操作）/ "offline"（不可达或尚未确认）

        只在状态实际变化时刷新（_link 初值为 None，构造后首个目标必然生效）。
        切换设备期间（_switching_devices=True）忽略 "online"：那是旧 monitor
        线程 stop 前排队的残留回调，采纳它会在切换中途起线程读 HID。
        """
        if self._switching_devices and target == "online":
            return
        if target == self._link:
            return
        self._link = target
        self._apply_link()

    def _reannounce_link(self) -> None:
        """把当前链路状态**原样重播**一遍：状态栏文案跟着链路刷新。

        "正在切换…正在重连…"与"程序已最小化到托盘"这类临时文案，在
        _set_link 的去重（target==_link 直接 return）下永远等不到第二次播报，
        不重播就挂成永久。切设备重连结束、托盘唤醒都走这里。
        """
        prev, self._link = self._link, None
        self._set_link(prev if prev in ("online", "offline") else "offline")

    def _apply_link(self) -> None:
        online = self._link == "online"
        self.controller.connected = online
        self._set_inputs_enabled(online)
        if online:
            # 离线时 active_idx 被重置成占位档；醒来第一拍读到的 DPI 不是"用户按了
            # 切档键"。与切换设备同一招：开一次"只同步高亮、不弹 HUD"的静默窗口，
            # 由 _apply 或 monitor 的首次成功读取消费（2026-09-18 场景走查复现）。
            self._dpi_silent_sync = True
            self._sync_device()
            self.set_status("已连接 · 正在读取 DPI / 轮询率 / 电量")
            # 这一步只是"链路通了、后台还在同步"的中间态；电量读完后由
            # _apply_battery / _apply_wired 覆盖成连接方式(+电量)。
            self._set_conn_state("正在连接")
        else:
            dev = self.controller.dev
            wired = not dev.has_battery
            self._apply_batt_text("有线" if wired else "--",
                                  self.theme.warn if wired else self.theme.dim)
            # 离线读不到真值，退回默认档；连上后由 _sync_device 用设备值覆盖
            self.poll_rate = DEFAULT_POLL_RATE
            self._paint_poll()
            # DPI 档位同理：离线了还亮着某一档的紫色，看上去像"当前就在这档"，
            # 而轮询率那格已经诚实退回默认——同一件事不能一个装死一个装活
            self.active_idx = STAGE_PLACEHOLDER
            self._paint_active()
            if self.controller.cfg.pid:
                self.set_status("鼠标离线：后台每 3 秒探测一次，接入后自动连接")
            else:
                # pid=0：connect 对"未选择"直接短路，不存在"接入后自动连接"
                self.set_status("未选择设备：插上已收录的鼠标后重启会自动认领，"
                                "也可在「设备」页点「切换」")
            self._set_conn_state("未连接", tip="鼠标离线")
        self._paint_conn(online)
        # 副标题只报型号名（"· 离线"后缀已删：在线/离线由旁边的状态词表达，
        # 两处不说同一件事），但目标设备可能随切换变化，这里必须重取一次
        self._refresh_subtitle()
        # 设备页若正开着，那颗在线/离线圆点必须跟着链路一起变——设备页刷新只挂在
        # "切页"上，不补这一下就会一直显示进来时的旧状态。
        if self._page_cur == "devices":
            self._repaint_devices_cached()

    def _sync_device(self) -> None:
        """转在线时重读设备状态刷新 UI。

        HID 读取放后台：一次 get_dpi / get_poll 就是一整轮收发，鼠标刚醒时会
        走满 3 次重试（各约 0.25s），留在主线程就是"转在线那一刻界面卡半秒"。
        """
        def _read() -> None:
            dpi = self.controller.get_dpi()
            poll = self.controller.get_poll()
            # 只做 HID 读数：closest_stage 依赖与 GUI 就地共享的 stages 表，
            # 必须留到 _apply 在主线程算（本文件"不把共享列表交给后台线程"那条）

            def _apply() -> None:
                idx = self._closest_stage(dpi[0]) if dpi else None
                if idx is not None:
                    self.active_idx = idx
                    self._paint_active()
                # 把当前 DPI 灌给状态机当基线：**不能只在读到值时灌**。
                # seed_dpi 的作用就是"这次读数不算用户按了切档键"；留在 if 里面，
                # 一次 get_dpi() 失败（鼠标刚醒时很常见）就等于没灌基线，下一轮
                # 轮询读到任何值都会报一次"检测到物理按钮切档"并弹 HUD。
                self._monitor.seed_dpi(dpi[0] if dpi else None)
                # 静默窗口**只在真拿到 DPI 数值时**结束：读不到就把标志留着，
                # 让 monitor 下一次成功读取消费它。就地清掉的话，下一拍会被当成
                # "用户按了切档键"——误报 + 弹 HUD（2026-09-18 场景走查复现）。
                if idx is not None:
                    self._dpi_silent_sync = False
                if poll in POLL_OPTIONS:
                    self.poll_rate = poll
                    self._paint_poll()
                    self.set_status(
                        "已连接 · 同步完成（DPI 档位 / 轮询率已更新）"
                        if idx is not None else
                        "已连接 · 轮询率已更新（DPI 没读到，稍后自动同步）")
                elif poll is None:
                    # 设备在界面上没有的档位（如 2000Hz 的 HyperPolling 档，
                    # 协议层读不回来）：说清楚，不要默默留着默认值骗人
                    self.set_status(
                        f"已连接 · 轮询率读不出（可能是 2000Hz 及以上的 "
                        f"HyperPolling 档），显示为 {self.poll_rate} Hz")
                else:
                    # 读到了但不在界面档位里（如 4000/8000Hz）：如实播报，
                    # 高亮保持原样，不谎称"已更新"
                    self.set_status(
                        f"已连接 · 设备轮询率 {poll} Hz（界面不支持该档位，"
                        f"显示保持 {self.poll_rate} Hz）")

            self._post_to_main(_apply)
            self.refresh_battery()

        self.run_bg(_read)

    def _set_inputs_enabled(self, enable: bool) -> None:
        """连接时允许编辑档位/轮询率/节能输入；未连接时禁用（防误设）。

        档位段**不整体禁用**：未连接时点段会走 on_stage_activate 的「未连接」
        提示（原版也是这个行为），所以只把里面的输入框禁用、标签压暗。
        """
        for seg in self.stage_segs:
            seg.set_input_enabled(enable)
            seg.set_label_dimmed(not enable)
            seg.set_hoverable(enable)
        self._poll_seg.set_buttons_enabled(enable)
        for e in self.power_entries:
            e.setEnabled(enable)
        if enable:
            self._paint_active()
            self._paint_poll()

    # ------------------------------------------------------------------
    # DPI 档位
    # ------------------------------------------------------------------
    def _paint_active(self) -> None:
        for i, seg in enumerate(self.stage_segs):
            active = (i == self.active_idx)
            seg.set_active(active)
            seg.highlight_input(active)

    def restore_stage(self, i: int) -> None:
        """取消编辑：恢复原值（失焦不保存）。"""
        if 0 <= i < len(self.stage_segs):
            self.stage_segs[i].set_value(self.stages[i])

    def _sync_stage_values(self, written: list[int]) -> None:
        """GUI 线程：把设备**真正接受的**档位表同步回共享列表与五个输入框。

        就地改 `self.stages`（不能整体重绑：LinkMonitor 与调用方共用这一个
        list 对象，重绑会让状态机拿着一张过期表算索引）。
        """
        self.stages[:] = written
        for i, seg in enumerate(self.stage_segs):
            seg.set_value(self.stages[i])

    def on_stage_return(self, i: int) -> None:
        if not (0 <= i < len(self.stage_segs)):
            return
        seg = self.stage_segs[i]
        if not self.controller.connected:
            self.restore_stage(i)
            self.set_status("未连接，无法修改档位数字")
            return
        try:
            v = max(DPI_MIN, min(self.controller.dev.dpi_max, int(seg.value())))
        except ValueError:
            self.restore_stage(i)
            return
        seg.set_value(v)
        old = self.stages[i]
        self.stages[i] = v
        # 传快照，不把共享列表交给后台线程去遍历（状态机正拿着同一个对象）
        snapshot = list(self.stages)
        seg.set_hoverable(False)      # 编辑期间不再跟随 hover 变色

        def _revert() -> None:
            """GUI 线程：设备没接受这一版档位表时退回。

            只在用户没再改过这一格时才退——并发写入时旧请求的回滚会把更新的值
            盖回去。self.stages 归主线程管（状态机在它的线程上遍历同一张表），
            所以改动一律安排在 posted 回调里做。
            """
            if self.stages[i] == v:
                self.stages[i] = old
                self.restore_stage(i)

        def _adopt(written: list[int]) -> None:
            """GUI 线程：以设备真正写进去的表为准（越界值被钳过）。"""
            if list(written) != self.stages:
                self._sync_stage_values(written)

        def _apply(_target) -> tuple[str, object]:
            written, save_err = self.controller.set_dpi_stages(snapshot)
            return (self._save_note(
                save_err,
                f"第 {i + 1} 档已保存为 {written[i]} DPI (当前鼠标 DPI 未变)",
                f"第 {i + 1} 档已生效（{written[i]} DPI）"),
                lambda w=written: _adopt(w))

        self._optimistic_write(
            gone_msg="已取消：档位未修改（鼠标已切换）",
            # 部分型号不支持写档位命令（如炼狱蝰蛇标准版）。失败不静默：如实
            # 报告，并提示可用「档 N」直接把当前 DPI 设为该值。
            fail_msg=(f"写档位失败({{err}})，第 {i + 1} 档已退回 {old}。"
                      f"此型号可能不支持写档，"
                      f"可点「档 {i + 1}」直接把当前 DPI 设为 {v}"),
            apply=_apply,
            revert=_revert,
            # 无论成败都要把 hover 恢复回来：`set_hoverable(False)` 的语义是
            # "写入期间不跟随 hover 变色"，漏了恢复这一段就**从此不再有悬停高亮**
            # （2026-09-17 审查发现：原来只有 _set_inputs_enabled 会重新打开它，
            # 而那只在链路状态变化时才跑）。
            cleanup=lambda: seg.set_hoverable(True))

    def on_stage_activate(self, i: int) -> None:
        if not (0 <= i < len(self.stage_segs)):
            return
        if not self.controller.connected:
            self.set_status("未连接，无法切换档位")
            return
        if self.active_idx == i:
            self.stage_segs[i].input.setFocus()
            return
        prev = self.active_idx
        self.active_idx = i
        self._paint_active()
        v = min(self.stages[i], self.controller.dev.dpi_max)
        self.set_status(f"已切换到第 {i + 1} 档 ({v} DPI)")

        def _revert() -> None:
            # 只在用户之后没再选过别的档位时才退
            if self.active_idx == i:
                self.active_idx = prev
                self._paint_active()

        def _apply(target) -> tuple[str, None]:
            _epoch, dev = target
            # 用排队时那台设备，不在后台解析 controller.dev
            dev.set_dpi(v)
            return "", None

        # 没写进去就别把高亮留在错误档位上：设备真值没变，状态机的 DPI 轮询
        # 不会再 emit 来纠正，错档会一直显示到下次切档（回滚由助手负责）
        self._optimistic_write(
            gone_msg="已取消：未切换档位（鼠标已切换）",
            fail_msg="切换失败: {err}",
            apply=_apply,
            revert=_revert)

    # ------------------------------------------------------------------
    # 轮询率
    # ------------------------------------------------------------------
    def _paint_poll(self) -> None:
        self._poll_seg.set_current(self.poll_rate)
        # 轮询率卡的大字当前值与五段高亮**必须同源**（都是 self.poll_rate），
        # 否则会出现"大字一个值、高亮另一格"的假显示
        try:
            self._poll_big.setText(f"{self.poll_rate} Hz")
        except (AttributeError, RuntimeError):
            pass

    def on_poll_click(self, v: int) -> None:
        if not self.controller.connected:
            self.set_status("未连接，无法设置轮询率")
            return
        prev = self.poll_rate
        self.poll_rate = v
        self._paint_poll()

        def _revert() -> None:
            """回滚，但只在"用户之后没再选过别的值"时才回滚。

            连点两下会有两条写入并发（run_bg 一任务一线程），前一条失败时拿点击
            那一刻的 prev 去覆盖，会把后一条已经设好的值改回旧数字——界面上显示
            一个设备根本没采纳的轮询率。
            """
            if self.poll_rate != v:
                return
            self.poll_rate = prev
            self._paint_poll()

        def _apply(_target) -> tuple[str, None]:
            save_err = self.controller.set_poll(v)
            return (self._save_note(save_err, f"轮询率已设为 {v} Hz",
                                    f"轮询率已生效（{v} Hz）"), None)

        # 设备没采纳 → 高亮也退回去，否则界面长期显示一个假档位。
        # 控件状态只能在 GUI 线程改，整段回滚丢回主线程（助手负责）。
        self._optimistic_write(
            gone_msg="已取消：轮询率未修改（鼠标已切换）",
            fail_msg="设置失败: {err}",
            apply=_apply,
            revert=_revert)

    # ------------------------------------------------------------------
    # 节能
    # ------------------------------------------------------------------
    def _make_power_row(self, host, label_text: str, key: str,
                        vmin: int, vmax: int, default: int,
                        sender) -> QtWidgets.QWidget:
        """节能设置行：label 左 / 输入框右（Apple 设置的一行）。

        回车保存（写设备 + 存配置），失焦恢复原值。

        Qt 的 `editingFinished` 回车和失焦都会发，而我们要的是两条不同语义——
        用 `_just_committed` 标志区分：回车先置标志再提交，随后的 editingFinished
        看到标志就跳过。
        """
        row, hl = self._panel_row(host, pad_y=10)
        lbl = self._row_label(label_text, row)
        hl.addWidget(lbl, 0)
        hl.addStretch(1)

        entry = RoundedInput(row, int_only=True, font=FONT_SEG_VALUE, width=96)
        entry.setText(str(getattr(self.controller.cfg, key, default)))
        hl.addWidget(entry, 0)
        self.power_entries.append(entry)
        flag = {"just": False}

        def restore() -> None:
            entry.setText(str(getattr(self.controller.cfg, key, default)))

        def on_return() -> None:
            flag["just"] = True
            if not self.controller.connected:
                restore()
                self.set_status(f"未连接，无法设置 {label_text}")
                return
            try:
                v = max(vmin, min(vmax, int(entry.text())))
            except ValueError:
                restore()
                return
            entry.setText(str(v))
            old_cfg = getattr(self.controller.cfg, key, default)
            setattr(self.controller.cfg, key, v)

            def _revert() -> None:
                # 设备没采纳 → 配置也必须退回，否则 restore() 会把这个错值显示
                # 回输入框，且之后任何一次落盘都会把它写进 config.json。
                # **只在用户之后没再改过这一格时才退**：两行节能设置也都是
                # run_bg 一任务一线程，连改两次时前一条失败会把后一条已经
                # 设好的值盖回去（档位/轮询率早就带了同一道护栏）。
                if getattr(self.controller.cfg, key, default) != v:
                    return
                setattr(self.controller.cfg, key, old_cfg)
                restore()

            def _apply(_target) -> tuple[str, None]:
                sender(v)
                # 设备写入成功后再落盘配置；保存失败必须说出来，否则用户以为改好了
                try:
                    from ..core import config as cfgmod
                    cfgmod.save(self.controller.cfg)
                except Exception as e:  # noqa: BLE001
                    log.warning("config save failed", exc_info=True)
                    return (f"{label_text} 已生效，但配置保存失败({e})，"
                            f"重启后会回到 {old_cfg}", None)
                return (f"{label_text} 已设为 {v}", None)

            self._optimistic_write(
                # 鼠标已经换掉了：这一笔发出去就是去改另一只鼠标的设置
                gone_msg=f"已取消：{label_text} 未修改（鼠标已切换）",
                fail_msg="设置失败: {err}",
                apply=_apply,
                revert=_revert)

        def on_editing_finished() -> None:
            if flag["just"]:
                flag["just"] = False
                return
            restore()

        entry.returnPressed.connect(on_return)
        entry.editingFinished.connect(on_editing_finished)
        # 未连接时输入框被禁用 → 点击同样没有反馈，与轮询率段一致地给出提示
        guard = BlockedClickWatcher(
            row, [entry], f"未连接，无法设置 {label_text}")
        guard.blocked.connect(self.set_status)
        return row

    # ------------------------------------------------------------------
    # 后台写入的公共护栏
    # ------------------------------------------------------------------
    def _capture_target(self) -> tuple[int, object]:
        """GUI 线程（用户提交时）：记下这笔写入是排给哪台设备的。

        run_bg 是"一个任务一条线程"，彼此并发；而 switch_device 会整体换掉
        controller.dev。用户在切换前刚点下去的 DPI/轮询率/节能写入如果不再检查，
        就会静默落到**刚换上的那只鼠标**上——它被改了主人没打算改的设置。
        """
        return self.controller.dev_epoch, self.controller.dev

    def _device_gone(self, target: tuple[int, object]) -> bool:
        """后台线程：设备已被换掉 → 这一笔不许再发。"""
        epoch, dev = target
        return epoch != self.controller.dev_epoch or dev is not self.controller.dev

    def _save_note(self, err: str | None, ok_text: str, fallback: str) -> str:
        """把"设备已生效 / 配置没存上"两件事合成一句如实的状态文案。"""
        if err:
            log.warning("config save failed: %s", err)
            return f"{fallback}（配置未存上：{err}，重启后会回退）"
        return ok_text

    def _optimistic_write(self, gone_msg: str, fail_msg: str, apply, revert,
                          cleanup=None) -> None:
        """「界面先乐观更新 → 后台写设备 → 失败/换设备则回滚」的统一护栏。

        档位数字、切当前档、轮询率、节能两行**原本是同一套 5 步协议各写一遍**
        （约 140 行逐字同构）：乐观改 UI → `_capture_target()` → 后台判
        `_device_gone` → 失败回滚（且只在"用户之后没再改过"时才退）→ 写状态栏。
        最容易写错的并发护栏复制了四份，漏改一处就是界面长期显示一个设备根本
        没采纳的值。现在收敛到这里（2026-09-17 审查后重构）。

        参数：
          gone_msg  排队期间设备被换掉 → 回滚后显示这句
          fail_msg  写入抛异常 → 回滚后显示这句，用 `{err}` 占位错误
          apply(target) -> (状态文案, 成功后的 GUI 回调或 None)
                    后台线程执行；抛异常即视为"设备没采纳"
          revert()  GUI 线程执行的回滚（调用方自己判"用户有没有再改过"）
          cleanup() 可选：无论成败都要在 GUI 线程收尾（如恢复 hover）
        """
        target = self._capture_target()

        def _run() -> None:
            try:
                if self._device_gone(target):
                    self._post_to_main(revert)
                    self.set_status(gone_msg)
                    return
                try:
                    msg, on_ok = apply(target)
                except Exception as ex:  # noqa: BLE001
                    # 设备没接受这一版，界面也必须退回去：否则 UI 上挂着一个
                    # 设备从未采纳的值，下一次操作还会按这个错值下发。
                    self._post_to_main(revert)
                    self.set_status(fail_msg.format(err=ex))
                    return
                if on_ok is not None:
                    self._post_to_main(on_ok)
                if msg:
                    self.set_status(msg)
            finally:
                if cleanup is not None:
                    self._post_to_main(cleanup)

        self.run_bg(_run)

    # ------------------------------------------------------------------
    # 电量 / 设备轮询
    # ------------------------------------------------------------------
    def _closest_stage(self, x: int) -> int:
        # 与 LinkMonitor 同一份实现（状态机用它把读到的 DPI 映射成档位下标）：
        # 两边各写一遍时，任何一边改了判据就会出现"HUD 说第 3 档、面板高亮第 4 档"。
        return closest_stage(self.stages, x)

    # ---- LinkMonitor 注入回调（后台线程触发；除 _post_to_main 外均已在主线程）----

    def _read_dpi_once(self) -> tuple[int, int] | None:
        """读当前 DPI（LinkMonitor 线程内调用，不碰控件）。"""
        try:
            return self.controller.dev.get_dpi()
        except Exception:  # noqa: BLE001
            return None

    def _probe_once(self) -> bool:
        """单次端到端探测（轮询线程内调用，不碰控件）。

        句柄从未建立（首次启动/dongle 不在）时先 connect()；随后单次心跳
        （无线读电量 / 有线读固件版本）判定"鼠标本体是否响应"。无线鼠标关机时
        battery 必失败、dongle 却可枚举成功 → 返回 False，正确判为离线。
        句柄失效重开由离线分支的定期强制 connect 负责。
        """
        try:
            if self.controller.dev.handle is None:
                if not self.controller.connect():
                    return False
            # connect 之后重新取：切设备期间 controller.dev 已被整体换掉，
            # 拿旧快照心跳会判出一个假离线
            return self.controller.dev.heartbeat()
        except Exception:  # noqa: BLE001
            return False

    def _try_reopen(self) -> None:
        """句柄失效兜底：dongle 被拔/重插但句柄残留时强制 connect 重开。"""
        try:
            if self.controller.dev.handle is not None:
                self.controller.connect()
        except Exception:  # noqa: BLE001
            pass

    def _on_dpi_sync(self, idx: int, x: int) -> None:
        """物理按钮切档同步（GUI 线程回调）。idx/值去重后更新 UI + 弹 HUD。"""
        prev = self.active_idx
        if idx == prev:
            self._dpi_silent_sync = False      # 本来就对得上，没什么可同步的
            return
        # 一次性消费：读出来就地清掉。留着会让下一次"真的按了切档键"被误吞。
        silent = self._dpi_silent_sync
        self._dpi_silent_sync = False
        if abs(self.stages[idx] - x) >= 300:
            return
        self.active_idx = idx
        self._paint_active()
        if silent:
            # 刚切换过鼠标：这一轮只是把高亮搬到新鼠标的当前档，不是物理切档。
            # 播报"检测到物理按钮切档"+弹 HUD 会让人以为自己在鼠标上按了键。
            return
        self.set_status(
            f"检测到物理按钮切档: 第 {idx + 1} 档 ({self.stages[idx]} DPI)")
        # 托盘后台（主窗已隐藏）时**也要弹**：这个浮窗本来就是给"程序缩在托盘、
        # 用户在鼠标上按物理切档键"这个场景用的，按 isVisible() 挡掉等于废掉它
        # （2026-09-17 用户反馈）。只挡退出过程中与"收浮窗→隐藏"那一小段。
        if not self._quitting and not self._hud_suppressed:
            # 标题不再写"向上/向下"：鼠标底部那颗切档键是**单向循环**的（一个按钮
            # 逐档往上走、到顶回绕），所谓"方向"只是拿档位索引变大变小猜出来的——
            # 回绕那一下（第 5 档 → 第 1 档）会被猜成"向下"，与物理事实相反
            # （2026-09-17 用户指出）。改成直接报"第几档 / 共几档"：不含任何猜测，
            # "回绕到第 1 档"看着也合理。
            total = len(self.stages)
            self.hud.show(x, idx,
                          f"灵敏度档位 {idx + 1}/{total}" if total > 1
                          else "灵敏度档位 1")

    # ------------------------------------------------------------------
    # 诊断 / 设备（都是页面；原先是两个独立子窗口）
    # ------------------------------------------------------------------
    def refresh_diagnose(self) -> None:
        """诊断页刷新：后台收集 → 回 GUI 线程填文本。

        采集要做全量 SetupDi 枚举，必须留在后台线程（原版就是 run_bg）。
        `_diag_busy` 挡重复排队：诊断页没有"刷新"按钮，刷新时机就是切页，
        而用户可能来回点导航。
        """
        if self._diag_busy:
            return
        self._diag_busy = True
        self._page_diag.set_text("正在收集诊断信息…")

        def _do() -> None:
            try:
                text, detail = dlg.collect_diagnose(self.controller,
                                                    self.remapper)
            except Exception as e:  # noqa: BLE001
                text = f"诊断信息收集失败：{e}"
                detail = text
            # 页面只放导读结论；原始数据每次刷新记一条进 startup.log，
            # 排查问题（或发给 AI）要的是这一份
            log.info("[gui] diagnose detail:\n%s", detail)
            self._post_to_main(lambda t=text: self._fill_diagnose(t))

        self.run_bg(_do)

    def _fill_diagnose(self, text: str) -> None:
        self._diag_busy = False
        try:
            self._page_diag.set_text(text)
        except RuntimeError:  # 收尾竞态：控件已销毁
            pass

    def refresh_devices(self) -> None:
        """设备页刷新（做成页面之后必须"每次进来都刷新"）。

        内容会变（设备插拔、链路在线/离线、当前目标切换），原来是"点按钮 =
        打开并刷新"，页面化之后少了"打开"这个动作，刷新就得挂在切页上。
        """
        if self._devices_busy:
            return
        self._devices_busy = True
        self._page_devices.paint(None)     # 先画"正在扫描…"占位
        # 无参 paint 只画占位，必须跟上后台扫描，否则列表会永久停在占位文案上
        self.run_bg(self._scan_then_paint_devices)

    def _scan_then_paint_devices(self) -> None:
        try:
            devs = hw.scan_known_devices()
            pids = {d.pid for d in devs if d.known}
        except Exception:  # noqa: BLE001
            pids = set()
        self._post_to_main(lambda: self._paint_devices(pids))

    def _paint_devices(self, pids: set[int]) -> None:
        self._devices_busy = False
        self._dev_pids = set(pids)
        # 扫描识别到的已收录鼠标 → 并入「我的鼠标」记录（第二只插上来
        # 就该从「其它已知型号」挪过来，哪怕从没切换过）
        try:
            self.controller.note_detected(pids)
        except Exception:  # noqa: BLE001 — 记录失败不影响本页展示
            log.warning("note_detected failed", exc_info=True)
        try:
            self._page_devices.paint(pids)
        except RuntimeError:  # 收尾竞态：控件已销毁
            pass

    def _repaint_devices_cached(self) -> None:
        """用上次扫描到的 PID 集合**原地重绘**设备页（不重新扫 HID）。

        链路在线/离线变化时设备集合通常没变（接收器一直在），只需要把行首那颗
        圆点和状态词刷新过来；走完整 refresh_devices 会先画"正在扫描…"占位，
        一次插拔/唤醒闪一下，没必要。
        """
        try:
            self._page_devices.paint(self._dev_pids)
        except (RuntimeError, AttributeError):
            pass

    def use_device(self, pid: int) -> None:
        """切换当前鼠标：换 PID、清缓存、立即重连（GUI 线程入口）。

        切换与重连放同一个后台任务里顺序执行（两个 run_bg 是并发线程，拆开会有
        "还没换完就去连"的竞态）。
        """
        # 「切换」按钮不会置灰、列表要到 _do 末尾才重绘，所以连点两下就是两轮
        # switch_device()+connect() 叠在同一条 HID 句柄上；而且第一轮
        # _finish_switch() 会在第二轮还没跑完时就把 _switching_devices 打开，
        # 让旧鼠标排队的 on_link("online") 被新设备采纳——这个标志存在的意义
        # 就是挡这件事，被提前清掉等于没挡。
        if self._switching_devices:
            self.set_status("正在切换鼠标，请等上一次完成")
            return
        # 新鼠标的 DPI 与上一只无关：第一次上报只同步高亮，不报"物理切档"
        self._dpi_silent_sync = True
        # 在 GUI 线程同步停掉状态机：若留在后台任务里，stop 生效前旧 monitor
        # 线程可能刚好探测成功，把 on_link("online") 排进主线程队列——
        # use_device 返回后该回调执行，又会起线程读 HID，与切换并发
        self._switching_devices = True
        self._monitor.stop()
        self._link = None                    # 强制下一次 _set_link 生效
        self._set_link("offline")
        self.set_status("已切换目标鼠标，正在重连…")

        def _do() -> None:
            try:
                self.controller.switch_device(pid)
                try:
                    self.controller.connect()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                # 副标题读的是 cfg.pid，必须等 switch_device 真的换完再刷（在
                # GUI 线程刷会读到旧 PID，而且这是唯一一处切换时的刷新点，
                # 错过就永久是旧型号）
                self._post_to_main(self._refresh_subtitle)
                # 状态机里的 online 变量没发生过迁移，不重播就永远等不到下一次
                # "online"（新设备一次心跳就成功时不会判离线）→ 界面卡在未连接
                self._monitor.start()
                self._post_to_main(self._finish_switch)
                # "正在重连…"是手写状态，不是一次链路迁移：B 关机时 resync 报回的
                # "offline" 会被 _set_link 去重丢掉，不重播就永远停在"正在重连…"
                self._post_to_main(self._reannounce_link)
            # 扫描继续留在后台线程（这里本来就在 run_bg 里）：以前是
            # _post_to_main(…connected)，等于每次切设备都在 UI 线程上做一遍
            # 全量 SetupDi 枚举，窗口直接卡住几十到几百毫秒
            self._scan_then_paint_devices()

        self.run_bg(_do)

    def _finish_switch(self) -> None:
        """GUI 线程：解除切换抑制（此后 resync 排队的 on_link 才会被采纳）。"""
        self._switching_devices = False
        self._monitor.resync()

    # ------------------------------------------------------------------
    # 改键状态
    # ------------------------------------------------------------------
    def _refresh_remap_state(self) -> None:
        try:
            # **必须把错误原因同步给页面**：RemapPage 是在 _build_layout 里建的，
            # 那时 remapper.start() 还没跑、_remap_err 还是空串。字符串不可变，
            # 页面拿到的是构造那一刻的副本——不同步的话，钩子装失败时页面会显示
            # 笼统的"钩子没装上"，真正的原因（在 _remap_err 里）只进了日志。
            self.remap_page._remap_err = self._remap_err
            self.remap_page.refresh_state()
        except (AttributeError, RuntimeError):
            pass

    # 托盘 / 窗口生命周期
    # ------------------------------------------------------------------
    def _wire_tray(self) -> None:
        # 这三个回调跑在 pystray 线程 / 单实例监听线程里，一律经 _post_to_main
        # 调度：在那些线程里直接碰 Qt 控件是未定义行为（Qt 下可能直接崩，
        # 比 tkinter 抛 RuntimeError 更隐蔽）。
        def _show_window() -> None:
            # 单实例监听线程调用。**等 GUI 线程真的执行完再返回**：
            # 只 emit 就回话的话，GUI 线程卡死时 ACK 照样发出、第二实例硬退出，
            # 用户看到的仍是"双击没反应"——正是这条链路要消灭的东西。
            # 超时抛错 → single_instance 不回 ACK → 探测方重试、最终如实提示。
            ev = threading.Event()

            def _run() -> None:
                try:
                    self._raise_window()
                finally:
                    ev.set()

            self._post_to_main(_run)
            if not ev.wait(2.0):
                raise TimeoutError("GUI 线程 2s 内未处理唤醒请求（可能已卡死）")

        def _on_refresh_battery() -> None:
            self._post_to_main(self.refresh_battery)

        def _on_quit() -> None:
            self._post_to_main(self.quit_app)

        if self._listener is not None:
            start_listener(self._listener, _show_window)

        # on_done 可能在 tray-ready 后台线程触发：经桥回主线程再碰控件
        self.tray.start(_show_window, _on_quit, _on_refresh_battery,
                        on_done=lambda ok: self._post_to_main(
                            lambda: self._after_tray_ready(bool(ok))))

    def _after_tray_ready(self, ok: bool) -> None:
        if ok:
            # closeEvent 是**点击时读 tray.ok**（不是"绑定"）：就绪后关窗 = 收进托盘
            try:
                self.refresh_battery()
            except Exception:  # noqa: BLE001
                pass
        elif self._silent:
            # 静默启动 + 没有托盘 = 看不见也退不出的后台进程（只能任务管理器杀）。
            # 退回显示窗口，至少能正常关窗退出。
            self._apply_page_size_safe()
            self.show()
            QtCore.QTimer.singleShot(0, self._drop_stray_focus)
            self.set_status("托盘图标创建失败，已显示窗口：可直接关窗退出")

    def _raise_window(self) -> None:
        self._hud_suppressed = False
        # 隐藏期间可能已经切过页面/改过内容，唤出来之前按当前页重算一次尺寸
        self._apply_page_size_safe()
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        QtCore.QTimer.singleShot(0, self._drop_stray_focus)
        # 收窗时写的"程序已最小化到托盘…"不随唤醒消失：重播当前链路文案
        QtCore.QTimer.singleShot(0, self._reannounce_link)

    def _drop_stray_focus(self) -> None:
        """窗口被程序化显示/激活时，Qt 会把焦点发给 tab 序里第一个可聚焦控件
        （导航按钮全是 NoFocus → 就是"档 1"输入框）——文本插入符凭空出现，和
        "点空白收光标"（硬约束 12）同一家族的烦扰。singleShot(0) 排在激活的
        焦点分配之后执行。

        **只清没被用户改过的输入框**（isModified）：clearFocus 会触发
        editingFinished → restore，把"正打着一半的数字"静默回滚
        （2026-09-18 场景走查复现：编辑中点托盘图标 → 数字变回去、零提示）。
        用户点出来但没改过的空光标，清掉无损失。
        """
        try:
            w = self.focusWidget()
            if isinstance(w, QtWidgets.QLineEdit) and not w.isModified():
                w.clearFocus()
        except RuntimeError:
            # 收尾竞态：C++ 对象已销毁。本方法是 singleShot 的槽，槽里逃出的
            # 异常会被 PyQt 判成致命错误（直接 abort 进程），必须自己吞掉。
            log.debug("drop stray focus failed", exc_info=True)

    def _release_hud_suppression(self) -> None:
        """解禁 HUD（`_hide_to_tray` 后下一轮事件循环调用）。

        只挡住"收浮窗 → 隐藏主窗"之间已排队的回调；之后用户在托盘后台物理切档
        仍然要弹浮窗，所以不能长期置位。
        """
        self._hud_suppressed = False

    def _hide_to_tray(self) -> None:
        """点窗口 X → 隐藏到托盘（程序继续在后台运行）。"""
        self.remap_page.leave()      # 窗口都收走了，别再留着"等你按键"
        self._hud_suppressed = True  # 先置标志再销毁，堵住排队回调的重建窗口
        self.hud.destroy()
        # 这里原先要逐个 hide 诊断窗/可用设备窗（独立顶层窗口，主窗 hide() 管不到
        # 它们，不收就孤零零留在桌面上）。两页改成内嵌页面后**这类收尾全部消失** ——
        # 这也是把它们页面化的附带好处之一。
        self.hide()
        # 下一轮事件循环解禁：此后物理切档在托盘后台照样弹浮窗。
        # 用 singleShot(0) 而不是立刻复位——本Turn已排队的回调要先被挡掉。
        QtCore.QTimer.singleShot(0, self._release_hud_suppression)
        self.set_status("程序已最小化到托盘，右键托盘图标可退出")

    def _install_exit_hooks(self) -> None:
        """兜底收尾：绕开 `quit_app()` 的退出路径也要松开改键残留的按键。

        正常退出（托盘退出 / 关闭窗口）都走 `quit_app()`，里面有 `remapper.stop()`。
        但还有几条路径绕得过去：未捕获异常导致解释器直接收尾、Ctrl+C、
        以及 `taskkill`（不带 /F 时发 CTRL 事件）。这些路径下如果只靠 Rust 的
        `Drop`，合成出去的 DOWN 没有配对的 UP——目标键会**卡在按下态**（系统级，
        影响所有程序）。所以：
          · `atexit`：解释器正常收尾时补一次 `remapper.stop()`
            （它内部会 release-all，见 `rust/src/remap.rs`）；
          · `SIGINT` / `SIGTERM`：转成一次正常的 `quit_app()`。
        用 weakref：atexit 回调不该把整个窗口及其控件树留到进程结束。

        **覆盖不到的**：`taskkill /F` / 任务管理器"结束任务"走 TerminateProcess，
        进程内收不到任何通知，任何语言都拦不住。那种情况 Windows 会自动摘钩子
        （输入不会被锁），但已合成的 DOWN 无法补救。
        """
        ref = weakref.ref(self)

        def _release() -> None:
            w = ref()
            if w is None:
                return
            try:
                w.remapper.stop()      # release-all + 摘钩子
            except Exception:  # noqa: BLE001 — 收尾阶段不该再抛
                pass

        atexit.register(_release)

        def _on_signal(_signum, _frame) -> None:
            w = ref()
            if w is None:
                return
            try:
                w._post_to_main(w.quit_app)
            except Exception:  # noqa: BLE001
                pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                # 非主线程里装不了信号处理器：忽略，正常退出路径仍然有效
                pass

    def closeEvent(self, evt) -> None:  # noqa: N802
        """点 X：有托盘就藏进托盘，没有就真退出。

        托盘就绪改成了异步补等（tray.py）：启动后最长 ~2s 内 `tray.ok`
        还是 False，这期间点 X 会走"真退出"——窗口刚显示就关窗本就要退出，
        可接受；`quit_app` 会顺手 `tray.stop()`，不留孤儿图标。
        """
        if self.tray.ok:
            evt.ignore()
            self._hide_to_tray()
        else:
            evt.accept()
            self.quit_app()

    def quit_app(self) -> None:
        """退出：停状态机 / 摘钩子 / 关监听 / 收浮窗 / 关托盘 / 退事件循环。

        两处必须守住（2026-09-17 审查发现）：
        - **幂等**：托盘菜单"退出"与 closeEvent 都会走到这里，重复收尾等于重复
          等 `remapper.stop()`（最长 2s）再 join 一次托盘线程；
        - **每步都吞异常**：原来 `_monitor.stop()` 没有兜底，它一抛，后面的
          "摘钩子 / 关监听 / 收托盘"全部被跳过 —— 钩子留在系统里、托盘图标残留。
        收尾是同步的（摘钩子要等钩子线程确认、托盘要 join 线程），所以先把状态
        栏刷出来再动手，别让人以为点了没反应。
        """
        if self._quitting:
            return
        self._quitting = True    # 排队中的切档回调不许再建浮窗
        try:
            self.set_status("正在退出…（摘除改键钩子）")
            app_ = QtWidgets.QApplication.instance()
            if app_ is not None:
                app_.processEvents()      # 让上面那行字真的画出来
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._page_cur == "remap":
                self.remap_page.leave()
        except Exception:  # noqa: BLE001
            pass
        t0 = time.perf_counter()
        for label, fn in (
            ("monitor", self._monitor.stop),
            ("remapper", self.remapper.stop),   # 主动摘钩子，别等进程退出兜底
            ("listener", lambda: self._listener and self._listener.close()),
            ("hud", self.hud.destroy),
            ("device", self.controller.dev.close),
            ("tray", self.tray.stop),
        ):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log.debug("quit step %s failed: %s", label, e)
        log.info("[gui] 收尾完成，耗时 %.2fs", time.perf_counter() - t0)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()


# ===========================================================================
# 入口
# ===========================================================================
# Qt6 的 DPI 处理说明（**不需要**手工设置，所以这里没有对应的函数）：
# Tk 版必须调 `SetProcessDpiAwareness(2)`，否则窗口按 96dpi 渲染再被系统拉伸，
# 150% 屏上所有文字发虚。Qt6 **默认**就是 per-monitor-v2 感知（在 QApplication
# 构造时自行设置，若进程已设置过则跳过），而且坐标/字体全部按设备无关像素
# 自动换算。照搬 Tk 版那段 ctypes 会造成"谁先设"的歧义，更重要的是，一旦有人
# 在 Qt 路径里保留 Tk 那套 `_ui_scale` 手工乘算，就会变成双重放大。
# （曾经这里有一个只写日志的 `_configure_dpi()` 空函数来承载这段说明；
#   2026-09-17 按"代码服务于业务目标"删掉——说明进注释就够了。）


def run_gui(listener=None, silent: bool = False) -> None:
    """创建 Qt 应用与主窗口并进入事件循环（阻塞）。"""
    # 高 DPI 缩放（Qt6 里这些属性已废弃/默认开启，显式设置只为消除歧义）
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("HexWand")
    app.setWindowIcon(QtGui.QIcon(str(ICON_PATH)) if ICON_PATH.exists()
                      else QtGui.QIcon())

    from .theme import apply_theme
    apply_theme(app)

    # 关键：静默启动时窗口会被 hide()，若保持"最后一个窗口关闭就退出"，
    # 事件循环会**立刻结束**——程序起来了又马上没了，托盘也跟着消失。
    # Tk 版的 withdraw() 没这个问题（mainloop 照跑），Qt 必须显式关掉这个开关。
    app.setQuitOnLastWindowClosed(False)

    controller = Controller()
    win = MainWindow(controller, listener=listener, silent=silent)
    win._window_ref = win          # 防 GC：局部变量出栈后窗口不能被回收
    if not silent:
        win.show()
        win.raise_()
        win.activateWindow()
        # 同 _raise_window：启动显示也会把焦点白送给"档 1"输入框
        QtCore.QTimer.singleShot(0, win._drop_stray_focus)
    app.exec()
