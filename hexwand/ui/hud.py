"""DPI HUD：按鼠标物理 DPI 切档时，屏幕右下角弹出的悬浮提示。

【本包的前身是一套 Tk/Tkinter 实现，已于 2026-09-16 移除（备份见
`%APPDATA%\\HexWand\\backup\\`）。下文提到的「Tk 版」都指那一版。】

行为与那一版逐条对齐：
  · 无边框 / 置顶 / 不进任务栏 / 不抢焦点
  · 淡入 0.32×12ms → 停留 1500ms → 淡出 → 自毁
  · 显示期间又来了新的切档：复用同一个窗口，只刷新数字与进度条（不重放淡入，
    避免闪烁）；若上一次淡入还没走完，先把淡入补完（否则会永久停在半透明）
  · **尺寸用逻辑像素**（Qt6 的 setFixedSize/setPixelSize 都是设备无关单位）：
    系统缩放 100%/200% 下浮窗与窗口其它部分一样整体等比缩放，比例恒定
  · 数字变长 / 文案变长时自动收字号（QFontMetrics 量宽），永远压不到看板娘

Tk 版用 `after` 链做动画与延迟，这里换成 QTimer；`destroy()` 要连**排着队还没
执行**的回调一起取消——Tk 版踩过"收窗之后排在队里的 _show 又建了个孤儿浮窗"
的坑，Qt 里对应的是"single-shot 定时器在窗口销毁后触发"。
"""

from __future__ import annotations

import logging

from ..core.paths import MASCOT_PATH
from .qt import (
    AlignmentFlag,
    QtCore,
    QtGui,
    QtWidgets,
    SizePolicy,
    WidgetAttribute,
    WindowType,
    TransformationMode,
)
from .theme import THEME, font

log = logging.getLogger(__name__)

_FADE_STEP = 0.32
_FADE_INTERVAL_MS = 12
_HOLD_MS = 1500
_MAX_ALPHA = 1.0        # 必须完全不透明：0.95 时桌面会透出来约 5%

# 整体缩放：下面的尺寸/字号全部由「基准值 × _SCALE」算出，想整体放大缩小
# 只改这一个数。所有字号都是**像素**（setPixelSize），所以缩放后各元素比例恒定，
# 不会出现"字变大了但窗口没变"。
# 2026-09-16 用户反馈偏大，从 1.0 调到 0.82（430→352 宽）。
_SCALE = 0.82
_WIN_W = int(430 * _SCALE)
_WIN_H = int(150 * _SCALE)
_MASCOT_PX = int(152 * _SCALE)
_LEFT_PAD = int(22 * _SCALE)
_BAR_RIGHT_PAD = int(178 * _SCALE)   # 进度条右留白，给看板娘让位

_TITLE_PX = int(20 * _SCALE)         # 动作文案
_NUM_PX = int(63 * _SCALE)           # 大号 DPI 数字
_DPI_LBL_PX = int(22 * _SCALE)       # "DPI" 单位
_FIT_MIN_PX = max(9, int(13 * _SCALE))
_DPI_LBL_W = int(52 * _SCALE)        # "DPI" 单位实测宽度
_BAR_SEG_H = max(4, int(6 * _SCALE))
_BAR_SEG_GAP = max(3, int(4 * _SCALE))

# 贴角留白（距屏幕可用区右下角；availableGeometry 已经排除了任务栏）
_MARGIN_R = int(30 * _SCALE)
_MARGIN_B = int(60 * _SCALE)


def _left_width() -> int:
    """左侧文案实测可用宽度（到进度条右端为止）。"""
    return _WIN_W - _BAR_RIGHT_PAD - _LEFT_PAD


def _fit_font(text: str, px: int, max_w: int) -> QtGui.QFont:
    """像素字号固定，但超过可用宽度时逐像素收小，绝不压到看板娘。"""
    f = font(px, bold=True, pixel=True)
    if max_w <= 0:
        return f
    size = px
    fm = QtGui.QFontMetrics(f)
    while size > _FIT_MIN_PX and fm.horizontalAdvance(text) > max_w:
        size -= 1
        f.setPixelSize(size)
        fm = QtGui.QFontMetrics(f)
    if fm.horizontalAdvance(text) > max_w:
        log.debug("hud text too wide even at min size: %r", text)
    return f


def _load_mascot() -> QtGui.QPixmap | None:
    """把看板娘 PNG 读成 QPixmap；失败返回 None（不显示图，其余照常）。"""
    try:
        if not MASCOT_PATH.exists():
            log.warning("mascot image not found: %s", MASCOT_PATH)
            return None
        pm = QtGui.QPixmap(str(MASCOT_PATH))
        if pm.isNull():
            log.warning("mascot image decode failed: %s", MASCOT_PATH)
            return None
        return pm.scaled(_MASCOT_PX, _MASCOT_PX,
                         QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                         TransformationMode.SmoothTransformation)
    except Exception as e:  # noqa: BLE001
        log.warning("mascot image load failed: %s", e)
        return None


class DpiHud:
    """复用单个浮窗：窗口已在显示时只刷新数字、标题与进度条，避免闪烁。

    **必须在 GUI 主线程调用 `show()`/`destroy()`**（由 MainWindow 的跨线程桥调度）。
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None,
                 stage_count: int = 5) -> None:
        """stage_count = 进度条的段数，由调用方（MainWindow）传档位数。

        以前这里写死 `range(5)`、注释里说"与 STAGE_COUNT 一致"：档位数改了要
        改两处，漏改就是进度条段数与档位数静默错位。现在唯一事实源仍然是
        window.py 的 STAGE_COUNT，只是**传进来**而不是各自抄一遍。
        """
        self._parent = parent
        self._stage_count = max(1, int(stage_count))
        self._win: QtWidgets.QWidget | None = None
        self._num_lbl: QtWidgets.QLabel | None = None
        self._title_lbl: QtWidgets.QLabel | None = None
        self._seg_widgets: list[QtWidgets.QFrame] = []
        self._stage: int = 0
        self._alpha: float = 0.0
        self._mascot_pm: QtGui.QPixmap | None = None
        self._mascot_loaded = False
        # 最近一次摆放的位置 (x, y, w, h)：给预览脚本与日志用，便于确认
        # "它到底弹到哪块屏幕的哪个角"
        self._last_geo: tuple[int, int, int, int] | None = None
        # 三个定时器分工：淡入/淡出用 12ms 循环，停留用 single-shot
        self._fade_timer = QtCore.QTimer()
        self._fade_timer.setInterval(_FADE_INTERVAL_MS)
        self._fade_timer.timeout.connect(self._on_fade_tick)
        self._hold_timer = QtCore.QTimer()
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_fade_out)
        self._fading_out = False

    # ---------- 公共 ----------
    def show(self, x_dpi: int, stage_index: int, title: str) -> None:
        """显示/刷新浮窗（GUI 主线程）。title = 顶部那行文案，由调用方给。"""
        try:
            self._hold_timer.stop()
            # 复用已有窗口：刷新数字 + 标题 + 进度条，不重放淡入（避免闪烁）。
            # 标题也必须刷：连着切两档时窗口是复用的，但"第 3 档"要变成"第 4 档"
            # （2026-09-17 用户反馈：只刷了数字，文案停在上一次）。
            if self._win is not None and self._num_lbl is not None:
                self._set_num(x_dpi)
                self._set_title(title)
                self._stage = stage_index
                self._paint_bar()
                if self._alpha < _MAX_ALPHA:
                    # 上一次淡入没走完就被新的切档打断：先把淡入补完
                    self._start_fade_in()
                else:
                    self._hold_timer.start(_HOLD_MS)
                return
            self._create(x_dpi, stage_index, title)
        except Exception:  # noqa: BLE001 — 浮窗失败绝不能影响主界面
            log.warning("HUD show failed", exc_info=True)

    def destroy(self) -> None:
        """收掉浮窗与全部待执行定时器。"""
        self._hold_timer.stop()
        self._fade_timer.stop()
        self._fading_out = False
        if self._win is not None:
            try:
                self._win.hide()
                self._win.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        self._win = None
        self._num_lbl = None
        self._title_lbl = None
        self._seg_widgets = []
        self._alpha = 0.0

    # ---------- 内部 ----------
    def _set_num(self, x_dpi: int) -> None:
        """刷新大数字（含字号自适应：数值变长时自动收字号，不挤看板娘）。"""
        if self._num_lbl is None:
            return
        txt = str(x_dpi)
        self._num_lbl.setText(txt)
        self._num_lbl.setFont(
            _fit_font(txt, _NUM_PX, _left_width() - 8 - _DPI_LBL_W))

    def _set_title(self, text: str) -> None:
        """刷新顶部标题（文案随档位变，字号跟着自适应）。

        必须像 `_set_num` 一样在**复用分支**里调用：窗口复用不代表内容可以不动。
        """
        if self._title_lbl is None:
            return
        if self._title_lbl.text() != text:
            self._title_lbl.setText(text)
        self._title_lbl.setFont(_fit_font(text, _TITLE_PX, _left_width()))

    def _create(self, x_dpi: int, stage_index: int, title: str) -> None:
        t = THEME
        win = QtWidgets.QWidget(None)
        win.setObjectName("root")
        win.setAttribute(WidgetAttribute.WA_StyledBackground, True)
        win.setAttribute(WidgetAttribute.WA_ShowWithoutActivating, True)
        # Tool：不进任务栏。Frameless：无边框。StaysOnTop：浮在最前。
        win.setWindowFlags(
            WindowType.FramelessWindowHint
            | WindowType.WindowStaysOnTopHint
            | WindowType.Tool
        )
        win.setWindowTitle("HexWand HUD")

        outer = QtWidgets.QHBoxLayout(win)
        outer.setContentsMargins(_LEFT_PAD, 18, 0, 0)
        outer.setSpacing(0)

        left = QtWidgets.QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)

        title_lbl = QtWidgets.QLabel(title, win)
        title_lbl.setFont(_fit_font(title, _TITLE_PX, _left_width()))
        title_lbl.setStyleSheet(f"color: {t.accent};")
        left.addWidget(title_lbl, 0, AlignmentFlag.AlignLeft)
        self._title_lbl = title_lbl

        num_row = QtWidgets.QHBoxLayout()
        num_row.setContentsMargins(0, 0, 0, 0)
        num_row.setSpacing(8)
        self._num_lbl = QtWidgets.QLabel(str(x_dpi), win)
        self._num_lbl.setStyleSheet("color: #ffffff;")
        self._set_num(x_dpi)
        num_row.addWidget(self._num_lbl, 0, AlignmentFlag.AlignBottom)
        unit = QtWidgets.QLabel("DPI", win)
        unit.setFont(font(_DPI_LBL_PX, bold=True, pixel=True))
        unit.setStyleSheet(f"color: {t.dim};")
        num_row.addWidget(unit, 0, AlignmentFlag.AlignBottom)
        num_row.addStretch(1)
        left.addLayout(num_row)

        # 进度条：右端留出看板娘的位置
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(_BAR_SEG_GAP)
        self._stage = stage_index
        self._seg_widgets = []
        # 段数由构造参数给（= 主面板的档位数）：只画不交互，数目对不上会误导
        # "现在第几档"。
        for _ in range(self._stage_count):
            seg = QtWidgets.QFrame(win)
            seg.setFixedHeight(_BAR_SEG_H)
            seg.setSizePolicy(SizePolicy.Expanding, SizePolicy.Fixed)
            bar.addWidget(seg, 1)
            self._seg_widgets.append(seg)
        bar_holder = QtWidgets.QWidget(win)
        bar_holder.setLayout(bar)
        bar_holder.setFixedWidth(_left_width())
        left.addWidget(bar_holder, 0, AlignmentFlag.AlignLeft)
        left.addStretch(1)

        left_holder = QtWidgets.QWidget(win)
        left_holder.setLayout(left)
        left_holder.setFixedWidth(_left_width())
        outer.addWidget(left_holder, 0, AlignmentFlag.AlignTop)

        outer.addStretch(1)

        # 看板娘：右下贴角，图片自身的左/上渐隐让它像从暗处浮出来
        mascot = QtWidgets.QLabel(win)
        mascot.setFixedSize(_MASCOT_PX, _MASCOT_PX)
        if not self._mascot_loaded:
            self._mascot_pm = _load_mascot()
            self._mascot_loaded = True
        if self._mascot_pm is not None:
            mascot.setPixmap(self._mascot_pm)
        outer.addWidget(mascot, 0,
                        AlignmentFlag.AlignRight | AlignmentFlag.AlignBottom)

        win.setFixedWidth(_WIN_W)
        # 高度按内容自适应（pack 空间不够会"直接丢掉后面的控件"，布局不会——但
        # 高度算错仍会把进度条挤出去，所以显式取 sizeHint 与下限的最大值）
        win.adjustSize()
        h = max(_WIN_H, win.sizeHint().height(), _MASCOT_PX)
        win.setFixedSize(_WIN_W, h)

        self._paint_bar()
        self._place(win, h)

        self._win = win
        self._alpha = 0.0
        win.setWindowOpacity(0.0)
        win.show()
        self._start_fade_in()

    def _place(self, win: QtWidgets.QWidget, h: int) -> None:
        """贴到"鼠标所在那块屏幕"的右下角。

        **为什么不用 primaryScreen**：这个浮窗是给鼠标切档用的，用户很可能在
        副屏上操作（多显示器很常见）。固定贴主屏的话，浮窗会跑到另一块屏幕上，
        看起来就跟"根本没弹出来"一样。所以优先取鼠标所在屏幕，取不到再回落主屏。

        坐标细节：`QRect.right()/bottom()` 是**最右/最下那一像素的索引**，
        不是"边界外的坐标"，所以 `right() - 宽` 得到窗口左上角 x，窗口右缘
        正好落在 right() 上，再减一个贴角留白。
        `availableGeometry()` 已经排除了任务栏，所以不用再躲托盘。
        """
        screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:  # pragma: no cover — 无屏幕时兜底
            win.move(0, 0)
            return
        avail = screen.availableGeometry()
        x = avail.right() - _WIN_W - _MARGIN_R
        y = avail.bottom() - h - _MARGIN_B
        # 别越过屏幕左上角（小屏 + 浮窗相对偏大时）
        x = max(avail.left(), x)
        y = max(avail.top(), y)
        # 再用**完整屏幕几何**夹一次右下角：availableGeometry 在部分缩放/多屏组合下
        # 会比真实屏幕多出几个像素（本机实测 avail 是 y=40..871，而屏幕只有 0..863，
        # 下沿溢出 8px）。现在留白够大所以看不出来，但哪天把留白调小、或者用户把任务栏
        # 挪到下方，浮窗就会被推出屏幕下沿。
        full = screen.geometry()
        x = min(x, full.right() - _WIN_W)
        y = min(y, full.bottom() - h)
        # 记下来供诊断/日志查看，省得"它到底弹哪去了"要靠猜
        self._last_geo = (x, y, _WIN_W, h)
        log.debug("hud placed at (%d,%d) %dx%d on screen %s, avail=%s",
                  x, y, _WIN_W, h, screen.name(), avail)
        win.move(x, y)

    def _paint_bar(self) -> None:
        """按当前档位刷新 5 段进度条颜色。"""
        t = THEME
        for i, seg in enumerate(self._seg_widgets):
            color = t.accent if i <= self._stage else t.hud_bar_off
            seg.setStyleSheet(
                f"background: {color}; border-radius: "
                f"{_BAR_SEG_H // 2}px;")

    # ---------- 淡入淡出 ----------
    def _start_fade_in(self) -> None:
        self._fading_out = False
        if not self._fade_timer.isActive():
            self._fade_timer.start()

    def _start_fade_out(self) -> None:
        self._fading_out = True
        if not self._fade_timer.isActive():
            self._fade_timer.start()

    def _on_fade_tick(self) -> None:
        win = self._win
        if win is None:
            self._fade_timer.stop()
            return
        if not self._fading_out:
            alpha = min(_MAX_ALPHA, self._alpha + _FADE_STEP)
            self._alpha = alpha
            try:
                win.setWindowOpacity(alpha)
            except Exception:  # noqa: BLE001 — 窗口可能已销毁
                self._fade_timer.stop()
                return
            if alpha >= _MAX_ALPHA:
                self._fade_timer.stop()
                self._hold_timer.start(_HOLD_MS)
            return
        alpha = max(0.0, self._alpha - _FADE_STEP)
        self._alpha = alpha
        try:
            win.setWindowOpacity(alpha)
        except Exception:  # noqa: BLE001
            self._fade_timer.stop()
            return
        if alpha <= 0.0:
            self._fade_timer.stop()
            self.destroy()
