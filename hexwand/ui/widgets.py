"""基础控件：Card / PillButton / SegmentedControl / RoundedInput / Dropdown / VBar
/ StageSegment（DPI 档位段）/ ClickFrame / BlockedClickWatcher（禁用态点击提示）。

【本包的前身是一套 Tk/Tkinter 实现，已于 2026-09-16 移除（备份见
`%APPDATA%\\HexWand\\backup\\`）。下文提到的「Tk 版」都指那一版。】

**这一层是迁移中减掉代码最多的地方**：Tk 版文件头那 7 条血泪契约（z-order
tag_raise、configure 白名单、Canvas 默认 -width
10c、pack 请求高度级联、<Configure> 首帧竞态、disabledforeground 对比度、bind 转发）
全部是 tkinter 特有的坑，Qt 里不存在对应问题：

  · 圆角            → QSS `border-radius`，不需要 Canvas 多边形自绘
  · z-order         → Qt 子控件天然画在父控件之上，没有"装饰遮住内嵌控件"这回事
  · 首帧            → 布局系统在 show 之前就完成计算，不会出现"截到半成品"
  · configure 白名单 → 不存在（没有把多种控件塞进同一个 Canvas 的取巧做法）
  · 请求高度级联    → sizeHint / layout 自动处理

但**需求**要照搬（这些是产品要求，不是 tkinter 的怪癖）：
  1. 禁用态文字必须看得见  → QSS 每个 :disabled 都显式给 disabled_fg
  2. 主题里任何配色都过一遍对比度
  3. 圆角控件的内边距要有下限，别让内容贴边
"""

from __future__ import annotations

from .qt import (
    AlignmentFlag,
    CursorShape,
    MouseButton,
    QtCore,
    QtGui,
    QtWidgets,
    Signal,
    SizePolicy,
    WidgetAttribute,
    RenderHint,
)
from .theme import (
    DROPDOWN_ARROW_ZONE,
    FONT_BOLD_UI,
    FONT_SEG_VALUE,
    FONT_SMALL,
    FONT_UI,
    THEME,
    Theme,
)


def repolish(w: QtWidgets.QWidget) -> None:
    """动态属性变了之后强制重算样式。

    QSS 的属性选择器（`#pill[active="true"]`）只在控件**重新 polish** 时才重新
    匹配，光 setProperty 是不生效的——这是 Qt 样式表最常见的"改了没反应"。
    """
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def set_prop(w: QtWidgets.QWidget, name: str, value) -> None:
    if w.property(name) != value:
        w.setProperty(name, value)
        repolish(w)


# ===========================================================================
# 卡片
# ===========================================================================
class Card(QtWidgets.QFrame):
    """圆角卡片：可选标题行（左侧紫色小竖条）+ 内容区 `body`。

    内边距固定在 (18, 14)（与 Tk 版 RoundedCard 的 pad_x/pad_y 一致）。
    """

    def __init__(self, title: str | None = None, parent=None,
                 theme: Theme = THEME, pad: tuple[int, int] = (18, 14)) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        # WA_StyledBackground：QFrame 的子类要 QSS 画背景有时必须显式打开，
        # 不开的话 setObjectName("card") 的背景/圆角可能整个不画。
        self.setAttribute(WidgetAttribute.WA_StyledBackground, True)
        self._t = theme
        self._outer = QtWidgets.QVBoxLayout(self)
        self._outer.setContentsMargins(pad[0], pad[1], pad[0], pad[1])
        self._outer.setSpacing(8)

        if title:
            head = QtWidgets.QWidget(self)
            hl = QtWidgets.QHBoxLayout(head)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(8)
            bar = QtWidgets.QFrame(head)
            bar.setObjectName("accentBar")
            bar.setFixedSize(3, 14)
            hl.addWidget(bar, 0, AlignmentFlag.AlignVCenter)
            lbl = QtWidgets.QLabel(title, head)
            lbl.setObjectName("cardTitle")
            lbl.setFont(FONT_BOLD_UI)
            hl.addWidget(lbl, 0, AlignmentFlag.AlignVCenter)
            hl.addStretch(1)
            self.title_row = head
            self._outer.addWidget(head, 0)

        self.body = QtWidgets.QWidget(self)
        self.body_layout = QtWidgets.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        self._outer.addWidget(self.body, 1)


def caption(text: str, parent=None) -> QtWidgets.QLabel:
    """卡片里的小字说明（暗色，9pt）。"""
    lbl = QtWidgets.QLabel(text, parent)
    lbl.setObjectName("cardNote")
    lbl.setFont(FONT_SMALL)
    lbl.setWordWrap(True)
    return lbl


def hline(parent=None) -> QtWidgets.QFrame:
    """1px 分隔线（用 1px 高的 Frame，别用 QFrame.HLine：它在深色主题下是浅色描边）。"""
    f = QtWidgets.QFrame(parent)
    f.setObjectName("divider")
    f.setFixedHeight(1)
    f.setAttribute(WidgetAttribute.WA_StyledBackground, True)
    return f


# ===========================================================================
# 按钮
# ===========================================================================
class PillButton(QtWidgets.QPushButton):
    """圆角胶囊按钮。`active=True` 时填充主色（选中态/导航当前页）。"""

    def __init__(self, text: str, parent=None, width: int | None = None,
                 font: QtGui.QFont | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("pill")
        self.setFont(font or FONT_UI)
        self.setCursor(CursorShape.PointingHandCursor)
        self.setProperty("active", False)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        if width:
            self.setFixedWidth(width)
            self.setSizePolicy(SizePolicy.Fixed, SizePolicy.Fixed)
        else:
            self.setMinimumWidth(88)
            self.setSizePolicy(SizePolicy.Minimum, SizePolicy.Fixed)

    def set_active(self, active: bool) -> None:
        set_prop(self, "active", bool(active))


# ===========================================================================
# 分段控件（轮询率五档）
# ===========================================================================
class SegmentedControl(QtWidgets.QFrame):
    """圆角槽里放 N 个等宽按钮，选中项填充主色。

    `picked(value)` 只在用户点击时发（程序化 set_current 不发），
    免得"同步设备真值"被当成用户操作再回写一次设备。
    """

    picked = Signal(object)

    def __init__(self, parent=None, pad: tuple[int, int] = (8, 6)) -> None:
        super().__init__(parent)
        self.setObjectName("segSlot")
        self.setAttribute(WidgetAttribute.WA_StyledBackground, True)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(pad[0], pad[1], pad[0], pad[1])
        lay.setSpacing(4)
        self._lay = lay
        self._btns: list[tuple[PillButton, object]] = []
        self._current: object = None
        self._enabled = True

    def add(self, label: str, value, font: QtGui.QFont | None = None) -> PillButton:
        b = PillButton(label, self, font=font or FONT_BOLD_UI)
        b.setObjectName("segBtn")
        b.setSizePolicy(SizePolicy.Expanding, SizePolicy.Fixed)
        b.setMinimumWidth(0)
        b.clicked.connect(lambda _c=False, v=value: self._on_click(v))
        self._lay.addWidget(b, 1)
        self._btns.append((b, value))
        return b

    def _on_click(self, value) -> None:
        if not self._enabled:
            return
        self.picked.emit(value)

    def set_current(self, value) -> None:
        """只改高亮，不发信号（设备真值同步走这条）。"""
        self._current = value
        self._repaint()

    def set_buttons_enabled(self, enabled: bool) -> None:
        """未连接时禁用。

        注意按钮被 setEnabled(False) 后 `clicked` 根本不发——所以"未连接点了要
        提示"这类交互不能靠 clicked 兜底，必须走 set_buttons_enabled(False) 之外
        的路径：主窗口在段外面挂一个 `BlockedClickWatcher`（拦住落在禁用件上的
        点击再发 `blocked` 信号），由它去状态栏提示。
        """
        self._enabled = enabled
        for b, _v in self._btns:
            b.setEnabled(enabled)

    def buttons(self) -> list[tuple[PillButton, object]]:
        return list(self._btns)

    def _repaint(self) -> None:
        # 高亮**只**跟随 self._current，不再叠加 `and self._enabled`：
        # 未连接时段整体禁用，但当前档仍要看得出来（QSS 的
        # `#segBtn[active="true"]:disabled` 会给一个暗一档的高亮）。
        # 否则大字显示 500 Hz、下面一格高亮都没有，两处信息对不上
        # （用户 2026-09-16 截图指出）。是否真的在设备上生效由上层保证：
        # _set_link 离线时先把 poll_rate 重置为默认档再 _paint_poll。
        for b, v in self._btns:
            set_prop(b, "active", v == self._current)


class BlockedClickWatcher(QtCore.QObject):
    """未连接时"点了却没反应"的兜底：把原因说给用户听。

    Qt 里 `setEnabled(False)` 的控件不处理鼠标事件，所以"禁用态点击提示"没法走
    它自己的 `clicked` / `mousePressEvent` —— 得靠事件过滤器。

    **装在谁身上很关键**（踩过）：实测 `QApplication.widgetAt(点击位置)` 在禁用
    状态下返回的**就是那个被禁用的按钮本身**，而不是它的父容器 —— 也就是说 Qt
    把事件投递给了禁用控件（只是它不处理）。所以过滤器**必须装在每个 target
    自己身上**；只装在父容器上的话，真实点击一次都收不到（上一版就是这样，
    而且验证脚本用 sendEvent 直接发给父容器，绕过了真实分发，看着"通过了"
    其实用户那边没反应）。
    """

    blocked = Signal(object)      # 传出提示文案（构造时给定）

    def __init__(self, host: QtWidgets.QWidget, targets, msg: str) -> None:
        super().__init__(host)
        self._host = host
        self._targets = [w for w in targets if w is not None]
        self._msg = msg
        # 两处都装：target 自己（真实点击走这条），host（点在禁用控件的区域但
        # 事件被 Qt 判给了父容器时的兜底，按坐标判断）
        host.installEventFilter(self)
        for w in self._targets:
            w.installEventFilter(self)

    def eventFilter(self, obj, evt) -> bool:  # noqa: N802 — Qt 命名
        if evt.type() != QtCore.QEvent.Type.MouseButtonPress:
            return False
        if obj in self._targets:
            if not obj.isEnabled():
                self.blocked.emit(self._msg)
                return True        # 吃掉：这一下不再有别的响应
            return False
        if obj is self._host:
            pos = evt.position().toPoint() if hasattr(evt, "position") \
                else evt.pos()
            for w in self._targets:
                if w.isEnabled():
                    continue
                tl = w.mapTo(self._host, QtCore.QPoint(0, 0))
                if QtCore.QRect(tl, w.size()).contains(pos):
                    self.blocked.emit(self._msg)
                    return True
        return False


# ===========================================================================
# 输入框
# ===========================================================================
class RoundedInput(QtWidgets.QLineEdit):
    """圆角输入框。`int_only=True` 时只放行 ASCII 整数。

    Tk 版要自己写按键级校验函数（还得小心"校验函数抛异常会被静默降级"）；
    Qt 用 QIntValidator 一行搞定，且**同样**挡掉全角数字——QIntValidator
    按 QLocale 解析，非 ASCII 数字一律 Intermediate/Invalid。
    """

    def __init__(self, parent=None, int_only: bool = False,
                 align: AlignmentFlag = AlignmentFlag.AlignCenter,
                 font: QtGui.QFont | None = None, width: int | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("input")
        self.setFont(font or FONT_BOLD_UI)
        self.setAlignment(align)
        self.setProperty("hl", False)
        if int_only:
            v = QtGui.QIntValidator(0, 10 ** 9, self)
            self.setValidator(v)
        if width:
            self.setFixedWidth(width)

    def set_highlight(self, on: bool) -> None:
        set_prop(self, "hl", bool(on))


# ===========================================================================
# 下拉
# ===========================================================================
class Dropdown(QtWidgets.QComboBox):
    """深色下拉。

    不用 `::down-arrow` 的 QSS 三角 hack 来画箭头：那套 `width:0 + 透明边框`
    的 CSS 三角在 Qt 的样式表引擎里是"能用但不保证"的，一旦不生效就是个没有
    箭头的方框（用户看不出能点）。这里改成自绘：QSS 把系统箭头关掉，paintEvent
    末尾自己画一个三角，任何平台任何风格下都长一样。
    """

    picked = Signal(str)

    def __init__(self, parent=None, value: str = "", options: list[str] | None = None,
                 on_pick=None, width: int | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropdown")
        self.setFont(FONT_UI)
        self.setCursor(CursorShape.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMaxVisibleItems(12)
        self._on_pick = on_pick
        self._placeholder_index = -1
        self._suppress = True
        self._picked_text = ""
        if width:
            self.setFixedWidth(width)
        self.set_options(value, options or [])
        # activated → int（两套绑定都有；字符串重载在 PyQt6 里叫 textActivated）
        # 注意：activated 在"重选当前项"时也会发，且滚轮滚过收起的下拉框同样会
        # 改 currentIndex —— 改键页的下拉一变更动作就覆写配置，这两条误触发路径
        # 必须堵死（见 _on_activated 的去重与 wheelEvent 的忽略）。
        self.activated.connect(self._on_activated)

    def set_options(self, value: str, options: list[str]) -> None:
        self._suppress = True
        self.clear()
        self._placeholder_index = -1
        items = list(options)
        if value and value not in items:
            items = [value] + items
            self._placeholder_index = 0
        for it in items:
            self.addItem(it)
        idx = items.index(value) if value in items else 0
        self.setCurrentIndex(idx)
        self._picked_text = self.itemText(idx)
        self._suppress = False

    def wheelEvent(self, evt) -> None:  # noqa: N802 — Qt 命名
        # 收起状态下滚轮经过就改值：对"选一下就走"的下拉是误触源头，
        # 直接放行给外层滚动区域
        evt.ignore()

    def _on_activated(self, index: int) -> None:
        if self._suppress or index < 0:
            return
        if index == self._placeholder_index:
            return              # 占位项不算一次选择
        text = self.itemText(index)
        if text == self._picked_text:
            return              # 重选当前项不算一次变更
        self._picked_text = text
        if self._on_pick is not None:
            self._on_pick(text)
        else:
            self.picked.emit(text)

    def paintEvent(self, evt) -> None:  # noqa: N802 — Qt 命名
        super().paintEvent(evt)
        p = QtGui.QPainter(self)
        p.setRenderHint(RenderHint.Antialiasing, True)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(THEME.dim))
        w, h = 9, 5
        # 三角居中在右侧「箭头区」里。那个区域的宽度由 QSS 的 `::drop-down` 给出，
        # 值就是 theme.DROPDOWN_ARROW_ZONE —— 两处必须同值，否则自绘三角会偏离
        # 系统留出的位置，看着像"箭头飘在边上"。
        x = self.width() - DROPDOWN_ARROW_ZONE + (DROPDOWN_ARROW_ZONE - w) // 2
        y = (self.height() - h) // 2
        tri = QtGui.QPolygon([
            QtCore.QPoint(x, y),
            QtCore.QPoint(x + w, y),
            QtCore.QPoint(x + w // 2, y + h),
        ])
        p.drawPolygon(tri)
        p.end()


# ===========================================================================
# 可点击容器（DPI 档位段：整段都是切换热区）
# ===========================================================================
class ClickFrame(QtWidgets.QFrame):
    """点哪都算点击的容器。

    Tk 版要给"段框 + 标签"各绑一遍 `<Button-1>`（并且踩过"只绑了按钮导致
    圆角空白区点了没反应"的坑）。Qt 里鼠标事件会从子控件冒泡到父容器，给容器
    装一次就覆盖整段，不需要给每个子控件单独绑。

    hover 态交给 QSS 的 `:hover`（给它开 WA_Hover 即可），不用自己维护
    进入/离开标志再 repolish ——那既啰嗦又容易在"指针刚好移到子控件上"时抖动。
    """

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(CursorShape.PointingHandCursor)
        self.setAttribute(WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(WidgetAttribute.WA_Hover, True)

    def mousePressEvent(self, evt) -> None:  # noqa: N802
        if evt.button() == MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(evt)


# ===========================================================================
# 竖排电量条
# ===========================================================================
class VBar(QtWidgets.QWidget):
    """竖排电量条：圆角槽 + 从底部向上的圆角填充。

    取代 Tk 版的 Canvas + `rounded_rect()` 自绘（Qt 用 QPainter 直接画
    rounded rect，不用自己拼多边形）。颜色阈值与原版一致：
    >40% 绿 / 20-40% 黄 / ≤20% 红；"--"/非法值只画槽。
    """

    def __init__(self, parent=None, width: int = 12, theme: Theme = THEME) -> None:
        super().__init__(parent)
        self._t = theme
        self._text = "--"
        self.setFixedWidth(width)
        self.setSizePolicy(SizePolicy.Fixed, SizePolicy.Expanding)
        self.setMinimumHeight(40)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def paintEvent(self, evt) -> None:  # noqa: N802
        t = self._t
        p = QtGui.QPainter(self)
        p.setRenderHint(RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        r = 4.0
        # 槽用 border 色：card_in 与卡片底太接近，"空的部分"看不出来
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(t.border))
        p.drawRoundedRect(0, 0, w, h, r, r)

        pct = self._pct()
        if pct is None:
            p.end()
            return
        color = (t.good if pct > 40 else t.warn if pct > 20 else t.offline)
        fh = int(h * pct / 100)
        if pct > 0 and fh < 4:
            fh = 4      # 极低电量也不能空得和 "--" 一样，至少画一小截
        if fh > 0:
            p.setBrush(QtGui.QColor(color))
            p.drawRoundedRect(0, h - fh, w, fh, r, r)
        p.end()

    def _pct(self) -> int | None:
        try:
            return max(0, min(100, int(self._text.rstrip("%"))))
        except ValueError:
            return None


# ===========================================================================
# DPI 档位段
# ===========================================================================
class StageSegment(ClickFrame):
    """DPI 档位的一段：「档 N」标签 + 数字输入框，**整段都是切换热区**。

    原版要点（照搬）：段框与标签都响应点击，输入框留着编辑数字——旧版上下两个
    独立控件对齐，按钮空白处点不动的问题从结构上消除。

    Enter / 失焦的语义差异用 `_just_committed` 区分：
    Qt 的 `editingFinished` 在**回车和失焦时都会发**，而我们要的是"回车保存、
    失焦恢复原值"（Tk 版 `<Return>` / `<FocusOut>` 两条不同路径）。所以回车时
    先记标志再提交，随后的 editingFinished 看到标志就跳过。
    """

    activated = Signal(int)
    committed = Signal(int)
    blurred = Signal(int)

    def __init__(self, index: int, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("stageSeg")
        self.index = index
        self._just_committed = False
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        self.label = QtWidgets.QLabel(f"档 {index + 1}", self)
        self.label.setObjectName("stageSegLabel")
        self.label.setAlignment(AlignmentFlag.AlignCenter)
        # 段被拉高时（主面板高度按较高的键位页对齐，见 window._target_win_h）
        # 让「档 N + 输入框」这一组整体垂直居中，而不是标签被拉长、输入框掉到底部
        lay.addStretch(1)
        lay.addWidget(self.label, 0)
        self.input = RoundedInput(self, int_only=True, font=FONT_SEG_VALUE)
        self.input.setAlignment(AlignmentFlag.AlignCenter)
        lay.addWidget(self.input, 0)
        lay.addStretch(1)

        self.clicked.connect(lambda: self.activated.emit(self.index))
        self.input.returnPressed.connect(self._on_return)
        self.input.editingFinished.connect(self._on_editing_finished)

    def _on_return(self) -> None:
        self._just_committed = True
        self.committed.emit(self.index)

    def _on_editing_finished(self) -> None:
        if self._just_committed:
            self._just_committed = False
            return
        self.blurred.emit(self.index)

    # ---- 状态 ----
    def set_value(self, v: int) -> None:
        self.input.setText(str(v))

    def value(self) -> str:
        return self.input.text()

    def set_active(self, active: bool) -> None:
        for w in (self, self.label):
            set_prop(w, "active", bool(active))

    def set_hoverable(self, hoverable: bool) -> None:
        set_prop(self, "hoverable", bool(hoverable))

    def set_label_dimmed(self, dimmed: bool) -> None:
        set_prop(self.label, "dimmed", bool(dimmed))

    def set_input_enabled(self, enabled: bool) -> None:
        self.input.setEnabled(enabled)

    def highlight_input(self, on: bool) -> None:
        self.input.set_highlight(on)
