"""键位改键页：鼠标平面图 + 编号热点 + 按键清单 + 动作编辑器。

本包的前身是一套 Tk/Tkinter 实现，已于 2026-09-16 移除（备份见
`%APPDATA%\\HexWand\\backup\\`）。下文提到的「Tk 版」都指那一版，
用来记录当年踩过的坑与对应做法 —— 那些**需求**仍在，只是坑本身在 Qt 里不存在了。

业务逻辑（动作类型、映射表读写、捕获规则、暂停语义）逐条照搬，只换实现手段：

  1. **热点不再是 Canvas item**。Tk 版要靠 `create_image` + `tag_bind("hot")`
     再从句柄反查 tags 才知道点中的是哪个键，还踩过"不能给 (tag, bid) 元组绑定"
     的坑；这里 MouseMapView 自己算最近热点，命中判定就是一句距离比较。
  2. **热点不再需要 4 倍超采样**。Tk 的 `create_oval` 不做平滑，圆环边缘一圈锯齿，
     所以旧版用 PIL 画 4x 再 LANCZOS 缩回来，还得小心 PhotoImage 被 GC 画布变空；
     Qt 的 QPainter 开 Antialiasing 直接就是平滑的，那段代码整套删掉。
  3. **窗口高度不再跳**。Tk 版要"把编辑区按最高变体冻结高度 + 手工算窗口高度"
     才压住"点一下下拉窗口抖一下"；Qt 里给编辑区 setFixedHeight 即可，
     窗口尺寸由 MainWindow 统一按页面 sizeHint 决定。
"""

from __future__ import annotations

import logging
import time

from ..core import keyboard as kb
from ..hardware import keymap as keymap_mod
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
from .theme import FONT_SMALL, FONT_UI, THEME, font
from .widgets import (
    Card,
    ClickFrame,
    Dropdown,
    PillButton,
    caption,
    hline,
    repolish,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量（与 Tk 版 window.py 同名同值，便于并排比对）
# ---------------------------------------------------------------------------
ACTION_TYPES: list[tuple[str, str]] = [
    ("default", "恢复默认"), ("none", "禁用"),
    ("key", "键盘按键"), ("mouse", "鼠标按键"),
]
_ACTION_IDS = {aid: lbl for aid, lbl in ACTION_TYPES}
_ACTION_LABELS = {lbl: aid for aid, lbl in ACTION_TYPES}
# 动作类型 → 它存放取值的字段名（类型名与字段名不总同名：鼠标类型存 button）
_VALUE_FIELD: dict[str, str] = {"key": "key", "mouse": "button"}

# 按键在界面上的固定顺序（＝鼠标热点上的编号，也是清单的行序）
REMAP_ORDER: tuple[str, ...] = (
    "left", "right", "middle", "wheel_up", "wheel_down", "x1", "x2",
)

CAPTURE_MAX_S: float = 10.0     # 按键捕获最长等待，防"点了不按"时轮询无限跑
CAPTURE_POLL_MS: int = 20

# 鼠标图尺寸全部是**逻辑像素**。Qt 会按系统缩放自动换算成物理像素，所以这里
# 不乘任何 scale —— Tk 版那几个 `int(x * _ui_scale)` 在 Qt 下必须删掉，
# 留着就是双重放大（150% 屏上会变成 1.5×1.5=2.25 倍）。
REMAP_CANVAS_W: int = 300
REMAP_CANVAS_H: int = 384
REMAP_PAD: int = 20             # 四周留白（产品图上本体两侧也留了约 1/4 宽度）
HOT_R_MAIN: int = 10
HOT_R_WHEEL: int = 6            # 滚轮三个热点间距只有 0.04h≈15px，半径再大就叠了

# 鼠标本体宽高比：**GPW3（罗技 PRO X SUPERLIGHT 2）模具** 63.5mm 宽 / 125mm 长。
_BODY_ASPECT: float = 0.508

# GPW3 轮廓的宽度剖面：**从官方产品图逐行提取**的半宽比例（0=前缘，1=后缘，
# 相对最宽处归一化），共 129 个控制点。提取与平滑流程（可复现）：
#   二值化(gray<140) → 形态学闭运算(填掉滚轮/按键缝/G logo 的内部孔洞)
#   → **先裁掉截图自身的黑边**（否则闭运算把边框和鼠标连成一片）
#   → 逐行取最左/最右暗像素、中线取中位数 → **只用右半宽镜像**
#     （左侧有侧键凸起，实测左右差 2.8px 恰在侧键那一段）
#   → 5 点加权平滑三遍（逐行数据有 ±1px 抖动，段数越细越会把抖动放大成波纹）
#   → 重采样为 129 个控制点。
# 量得的宽高比 0.505 ≈ 官方 63.5/125 = 0.508；形状与 TechPowerUp 卡尺实测一致
# （前 60.8 / 腰 59.0 / 后 62.7mm → 0.970/0.941/1.000，本表 0.974/0.940/1.000）。
# 渲染时相邻控制点之间再用 Catmull-Rom 细分 4 段 → **每侧 512 段、整圈 1024 段**。
_GPW3_HALF: tuple[float, ...] = (
    0.000, 0.309, 0.407, 0.487, 0.554, 0.614, 0.667, 0.714,
    0.754, 0.790, 0.818, 0.842, 0.859, 0.865, 0.888, 0.916,
    0.928, 0.938, 0.947, 0.953, 0.957, 0.962, 0.966, 0.966,
    0.970, 0.974, 0.974, 0.974, 0.974, 0.974, 0.974, 0.974,
    0.974, 0.974, 0.974, 0.973, 0.968, 0.966, 0.966, 0.966,
    0.966, 0.966, 0.963, 0.958, 0.957, 0.957, 0.957, 0.957,
    0.957, 0.957, 0.957, 0.957, 0.956, 0.950, 0.949, 0.949,
    0.949, 0.949, 0.946, 0.941, 0.940, 0.940, 0.940, 0.940,
    0.940, 0.940, 0.940, 0.940, 0.941, 0.945, 0.949, 0.949,
    0.954, 0.957, 0.959, 0.965, 0.966, 0.971, 0.974, 0.975,
    0.981, 0.983, 0.984, 0.989, 0.991, 0.992, 0.994, 0.999,
    1.000, 1.000, 1.000, 1.000, 1.000, 1.000, 1.000, 1.000,
    0.996, 0.992, 0.991, 0.986, 0.982, 0.977, 0.971, 0.963,
    0.955, 0.946, 0.938, 0.927, 0.915, 0.902, 0.887, 0.871,
    0.855, 0.837, 0.818, 0.795, 0.771, 0.747, 0.719, 0.688,
    0.657, 0.621, 0.582, 0.540, 0.490, 0.433, 0.365, 0.276,
    0.000,
)
# 相邻控制点之间插值几段（4 → 每侧 128×4＝512 段，整圈 1024 段）
_BODY_SUBDIV: int = 4

EDITOR_WRAP_W: int = 244        # 编辑区说明文字宽度（原版同名常量）
_ACTION_DD_W: int = 150         # 动作/目标下拉宽度
CARD_R_W: int = 300             # 右侧按键功能卡宽度（原版同值）


def button_no(bid: str) -> int:
    """按键的显示编号（1 起）。未收录进 REMAP_ORDER 的排到最后，不抛异常。"""
    return REMAP_ORDER.index(bid) + 1 if bid in REMAP_ORDER else len(REMAP_ORDER)


def action_summary(act: dict | None) -> str:
    """把一条映射压成清单里显示的短语。

    没配置过、恢复了默认、或选了类型但值还没填（键盘没录键名 / 鼠标没选目标），
    清单里一律显示「默认」——反正都还没真正接管这个按键。带「键盘 / 鼠标」前缀
    是为了让「鼠标 中键」这种值一眼能看出是映射目标，而不是按键名。
    """
    if not act:
        return "默认"
    atype = act.get("type", "none")
    if atype == "key":
        key = act.get("key", "")
        return f"键盘 {key}" if key else "默认"
    if atype == "mouse":
        btn = act.get("button", "")
        if not btn:
            return "默认"
        return "鼠标 " + keymap_mod.STANDARD_BUTTONS.get(btn, btn)
    if atype == "none":
        return "禁用"
    return "默认"


# ===========================================================================
# 鼠标平面图 + 热点
# ===========================================================================
class MouseMapView(QtWidgets.QWidget):
    """矢量对称鼠标俯视图 + 7 个带编号的可点热点。

    2026-09-17 用户反馈位图线稿"有瑕疵"，要求"最顶级的对称鼠标俯视图"——
    与其修一张位图，不如**用 QPainter 画矢量**：轮廓贝塞尔对称、任意缩放
    不糊、配色直接走主题（位图时代的"裁边 + 降透明度 + 内存 PNG"那套
    Pillow 处理整套删除）。本体宽高比 0.508（`_BODY_ASPECT`，与 GPW3 官方
    65/63.5mm 对齐），热点坐标按矢量图重新标定（keymap.LAYOUTS["standard"]），
    不再是"贴着某张图实测"的数值。
    """

    hotspot_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("mouseMap")
        self.setAttribute(WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(REMAP_CANVAS_W, REMAP_CANVAS_H)
        self.setMouseTracking(True)
        self._layout: dict[str, tuple[float, float]] = {}
        self._selected: str | None = None
        self._geom: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._hit_cache: dict[str, tuple[float, float, float]] = {}
        # 轮廓路径缓存：1024 段、约 2000 个点，悬停时的高频重绘不该反复重建
        # （路径只跟本体尺寸有关，尺寸不变就一直复用）
        self._body: QtGui.QPainterPath | None = None
        self._body_key: tuple[int, int] | None = None

    # ---------- 数据 ----------
    def set_layout(self, layout: dict[str, tuple[float, float]]) -> None:
        self._layout = dict(layout or {})
        self._recompute()
        self.update()

    def set_selected(self, bid: str | None) -> None:
        if bid != self._selected:
            self._selected = bid
            self.update()

    # ---------- 矢量鼠标 ----------
    @staticmethod
    def _emit_catmull(path: QtGui.QPainterPath,
                      pts: list[tuple[float, float]], sub: int) -> None:
        """把控制点序列用 Catmull-Rom 样条细分后 lineTo 进路径。

        起点必须已经在 path 上（调用方 moveTo 过）。两个端点用**反射虚拟点**
        （P0' = 2P0 - P1）来定切线 —— 否则端点处的切线会硬拐，顶部/尾部出尖角。
        """
        n = len(pts) - 1
        for i in range(n):
            if i > 0:
                p0 = pts[i - 1]
            else:
                p0 = (2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1])
            p1, p2 = pts[i], pts[i + 1]
            if i + 2 <= n:
                p3 = pts[i + 2]
            else:
                p3 = (2 * pts[n][0] - pts[n - 1][0],
                      2 * pts[n][1] - pts[n - 1][1])
            for s in range(1, sub + 1):
                u = s / sub
                u2 = u * u
                u3 = u2 * u
                path.lineTo(
                    0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * u
                           + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * u2
                           + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * u3),
                    0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * u
                           + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * u2
                           + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * u3))

    @staticmethod
    def _body_path(w: float, h: float) -> QtGui.QPainterPath:
        """GPW3 模具的俯视轮廓：129 个控制点 + Catmull-Rom 细分 → 整圈 1024 段。

        控制点来自产品图逐行提取（见 `_GPW3_HALF`），相邻点之间插 4 段，
        每侧 128×4 = **512 段**、左右镜像后整圈 **1024 段**。
        用户原话"像微积分那样，一条边是很多段连续拼凑出来，只要够多线条就光滑"：
        段数到这个量级，抗锯齿下与真曲线已无法区分，而形状仍完全由实测数据决定。
        """
        n = len(_GPW3_HALF) - 1
        right = [((0.50 + 0.50 * hw) * w, (i / n) * h)
                 for i, hw in enumerate(_GPW3_HALF)]
        path = QtGui.QPainterPath()
        path.moveTo(*right[0])
        MouseMapView._emit_catmull(path, right, _BODY_SUBDIV)
        # 左半：右半镜像（x → w - x），去掉后端顶点避免重复
        left = [(w - x, y) for x, y in reversed(right[:-1])]
        MouseMapView._emit_catmull(path, left, _BODY_SUBDIV)
        path.closeSubpath()
        return path

    def _draw_mouse(self, p: QtGui.QPainter, dx: int, dy: int,
                    dw: int, dh: int) -> None:
        """画矢量鼠标本体：轮廓 + 分体式按键盖 + 滚轮 + 左侧侧键。

        细节照 GPW3 产品图：
          · **分体式主键盖**占前半段（到 y≈0.42），中缝一条到底，盖的下缘是
            **向下凸的弧**（左右各一条，中间给滚轮让位）——不是一条直线；
          · 滚轮短而窄（y 0.10~0.225），中间一道滚花线；
          · 侧键只有**左侧**一对（GPW3 就是单侧双键）；
          · 中央偏下一颗状态灯，右侧一条极淡的握持棱线（它唯一的非对称处）。
        """
        t = THEME
        outline = QtGui.QColor(t.border)
        p.save()
        p.translate(dx, dy)

        # 本体：用竖向渐变模拟 GPW 的曲面（顶面受光、两侧与尾部转暗），
        # 纯色填充会显得像一块"贴纸"，这是产品感的关键一笔
        grad = QtGui.QLinearGradient(0.0, 0.0, 0.0, float(dh))
        top = QtGui.QColor(t.card_in).lighter(118)
        bottom = QtGui.QColor(t.card_in).darker(112)
        grad.setColorAt(0.0, top)
        grad.setColorAt(0.45, QtGui.QColor(t.card_in))
        grad.setColorAt(1.0, bottom)
        p.setPen(QtGui.QPen(outline, 1.4))
        p.setBrush(QtGui.QBrush(grad))
        if self._body_key != (dw, dh):
            self._body = self._body_path(dw, dh)
            self._body_key = (dw, dh)
        assert self._body is not None
        p.drawPath(self._body)

        p.setPen(QtGui.QPen(outline, 1.1))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        # 左右键盖中缝（到盖的下缘为止）
        p.drawLine(QtCore.QLineF(0.50 * dw, 0.020 * dh,
                                 0.50 * dw, 0.425 * dh))

        # 按键盖下缘：两条平缓下凸的弧，中间给滚轮让位（实图弧度很浅，别画深了）
        seam = QtGui.QPainterPath()
        seam.moveTo(0.055 * dw, 0.300 * dh)
        seam.quadTo(0.280 * dw, 0.390 * dh, 0.452 * dw, 0.315 * dh)
        seam.moveTo(0.945 * dw, 0.300 * dh)
        seam.quadTo(0.720 * dw, 0.390 * dh, 0.548 * dw, 0.315 * dh)
        p.drawPath(seam)

        # 滚轮（短胶囊 + 一道滚花线）
        wheel_w = 0.075 * dw
        wheel_top, wheel_h = 0.100 * dh, 0.125 * dh
        p.setBrush(QtGui.QColor(t.card))
        p.drawRoundedRect(QtCore.QRectF(0.50 * dw - wheel_w / 2, wheel_top,
                                        wheel_w, wheel_h),
                          wheel_w / 2, wheel_w / 2)
        p.drawLine(QtCore.QLineF(0.50 * dw - wheel_w / 2 + 1,
                                 wheel_top + wheel_h / 2,
                                 0.50 * dw + wheel_w / 2 - 1,
                                 wheel_top + wheel_h / 2))

        # 左侧两个侧键（GPW3 单侧双键）
        p.setBrush(QtGui.QColor(t.card))
        for sy in (0.310, 0.430):
            p.drawRoundedRect(QtCore.QRectF(0.028 * dw, sy * dh,
                                            0.088 * dw, 0.100 * dh), 3, 3)

        # 右侧握持棱线（GPW 系列唯一的非对称特征，画得很淡）
        p.setPen(QtGui.QPen(outline, 1.0))
        p.drawLine(QtCore.QLineF(0.945 * dw, 0.400 * dh,
                                 0.945 * dw, 0.580 * dh))

        # 中央偏下的状态灯（产品图里那颗绿点，位于按键盖与尾部之间）
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        lamp = QtGui.QColor(t.good)
        lamp.setAlpha(170)
        p.setBrush(lamp)
        p.drawEllipse(QtCore.QPointF(0.50 * dw, 0.475 * dh), 2.4, 2.4)
        p.restore()

    def _recompute(self) -> None:
        """算一遍鼠标本体矩形与热点像素坐标（换选中项时不需要重算）。"""
        cw, ch = self.width(), self.height()
        pad = REMAP_PAD
        body_h = ch - pad * 2
        body_w = int(body_h * _BODY_ASPECT)
        if body_w > cw - pad * 2:      # 极窄画布：以宽度为准反推高度
            body_w = cw - pad * 2
            body_h = int(body_w / _BODY_ASPECT)
        dx = (cw - body_w) // 2
        dy = pad + (ch - pad * 2 - body_h) // 2
        self._geom = (dx, dy, body_w, body_h)
        self._hit_cache = {}
        for bid, (rcx, rcy) in self._layout.items():
            cx, cy = dx + rcx * body_w, dy + rcy * body_h
            # +3px 容差：指针不用精确压在圆点里
            self._hit_cache[bid] = (cx, cy, self._radius_for(bid) + 3.0)

    @staticmethod
    def _radius_for(bid: str) -> int:
        # 滚轮竖排三个热点，圆点收小一圈才不叠在一起
        return HOT_R_WHEEL if bid in ("middle", "wheel_up", "wheel_down") \
            else HOT_R_MAIN

    # ---------- 绘制 ----------
    def paintEvent(self, evt) -> None:  # noqa: N802
        t = THEME
        p = QtGui.QPainter(self)
        p.setRenderHint(RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QtGui.QColor(t.card))

        self._draw_mouse(p, *self._geom)

        if not self._layout:
            p.setPen(QtGui.QColor(t.dim))
            p.setFont(FONT_SMALL)
            p.drawText(self.rect(), AlignmentFlag.AlignCenter,
                       "当前鼠标无可用按键")
            p.end()
            return

        for bid, (cx, cy, _tol) in self._hit_cache.items():
            r = self._radius_for(bid)
            sel = (bid == self._selected)
            p.setPen(QtGui.QPen(QtGui.QColor(t.accent_light if sel
                                             else t.border),
                                2.0 if sel else 1.0))
            # 热点画在鼠标本体（card_in）上：未选中填 bg（最暗，凹陷感）——
            # 填 card_in 会和本体融在一起"隐身"（本体底色升级后踩过）
            p.setBrush(QtGui.QColor(t.accent if sel else t.bg))
            p.drawEllipse(QtCore.QPointF(cx, cy), float(r), float(r))
            p.setPen(QtGui.QColor(t.on_accent if sel else t.dim))
            p.setFont(font(8) if bid in ("middle", "wheel_up", "wheel_down")
                      else FONT_SMALL)
            p.drawText(QtCore.QRectF(cx - r - 5, cy - r - 1,
                                     2 * r + 10, 2 * r + 2),
                       AlignmentFlag.AlignCenter, str(button_no(bid)))
        p.end()

    # ---------- 交互 ----------
    def _hit(self, pos: QtCore.QPoint) -> str | None:
        best, best_d = None, 1e9
        for bid, (cx, cy, tol) in self._hit_cache.items():
            d = ((pos.x() - cx) ** 2 + (pos.y() - cy) ** 2) ** 0.5
            if d <= tol and d < best_d:
                best, best_d = bid, d
        return best

    def mouseMoveEvent(self, evt) -> None:  # noqa: N802
        over = self._hit(evt.position().toPoint()) is not None
        self.setCursor(CursorShape.PointingHandCursor if over
                       else CursorShape.ArrowCursor)

    def mousePressEvent(self, evt) -> None:  # noqa: N802
        if evt.button() != MouseButton.LeftButton:
            return
        bid = self._hit(evt.position().toPoint())
        if bid:
            self.hotspot_clicked.emit(bid)


# ===========================================================================
# 按键清单一行
# ===========================================================================
class _ListRow(ClickFrame):
    row_clicked = Signal(str)

    def __init__(self, bid: str, index: int, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("listRow")
        self._bid = bid
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 8, 4)
        lay.setSpacing(10)
        self._num = QtWidgets.QLabel(str(index), self)
        self._num.setObjectName("listNum")
        self._num.setFont(FONT_SMALL)
        self._num.setFixedWidth(22)
        self._num.setAlignment(AlignmentFlag.AlignCenter)
        lay.addWidget(self._num, 0)
        self._name = QtWidgets.QLabel(
            keymap_mod.STANDARD_BUTTONS.get(bid, bid), self)
        self._name.setObjectName("listName")
        self._name.setFont(FONT_UI)
        lay.addWidget(self._name, 0)
        lay.addStretch(1)
        self._val = QtWidgets.QLabel("", self)
        self._val.setObjectName("listVal")
        self._val.setFont(FONT_SMALL)
        lay.addWidget(self._val, 0)
        self.clicked.connect(lambda: self.row_clicked.emit(self._bid))

    def refresh(self, summary: str, selected: bool) -> None:
        self._val.setText(summary)
        for w in (self, self._num, self._name, self._val):
            if bool(w.property("sel")) != selected:
                w.setProperty("sel", selected)
                repolish(w)


# ===========================================================================
# 键位页
# ===========================================================================
class RemapPage(QtWidgets.QWidget):
    """键位改键页。`status(str)` 把操作结果抛给主窗口的状态栏。"""

    status = Signal(str)
    remapped = Signal()          # 映射表变了（主窗口据此把新表推给 Rust 钩子）

    def __init__(self, controller, remapper, remap_err: str = "", parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.remapper = remapper
        self._remap_err = remap_err
        self._selected: str | None = None
        self._capturing: str | None = None
        self._prev_down: set[int] = set()
        self._capture_deadline = 0.0
        self._capture_lbl: ClickFrame | None = None
        self._capture_txt: QtWidgets.QLabel | None = None
        self._rows: dict[str, _ListRow] = {}
        self._measuring = False
        self._editor_h = 0

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(CAPTURE_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_keys)

        self._build()
        self.rebuild_mouse()
        self.rebuild_list()
        self.reserve_editor_height()
        self.refresh_state()

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(8)
        root.addLayout(body, 1)

        # 左：鼠标图
        card_l = Card("按键位置", self)
        body.addWidget(card_l, 1)
        self.map_view = MouseMapView(card_l.body)
        self.map_view.hotspot_clicked.connect(self.select_button)
        card_l.body_layout.addWidget(self.map_view, 0, AlignmentFlag.AlignCenter)
        card_l.body_layout.addStretch(1)
        self.card_l = card_l

        # 右：清单 + 编辑区（固定宽度）
        card_r = Card("按键功能", self)
        card_r.setFixedWidth(CARD_R_W)
        body.addWidget(card_r, 0)
        self.card_r = card_r

        self._list_holder = QtWidgets.QWidget(card_r.body)
        self._list_lay = QtWidgets.QVBoxLayout(self._list_holder)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(4)
        card_r.body_layout.addWidget(self._list_holder, 0)
        card_r.body_layout.addWidget(hline(card_r.body), 0)

        self._editor = QtWidgets.QWidget(card_r.body)
        self._editor_lay = QtWidgets.QVBoxLayout(self._editor)
        self._editor_lay.setContentsMargins(0, 0, 0, 0)
        self._editor_lay.setSpacing(4)
        card_r.body_layout.addWidget(self._editor, 0)
        card_r.body_layout.addStretch(1)

        # 底部一行：改键是否生效 + 暂停/恢复
        foot = QtWidgets.QWidget(self)
        fl = QtWidgets.QHBoxLayout(foot)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(10)
        self._state_lbl = QtWidgets.QLabel("", foot)
        self._state_lbl.setFont(FONT_SMALL)
        self._state_lbl.setWordWrap(True)
        fl.addWidget(self._state_lbl, 1)
        self._toggle_btn = PillButton("暂停改键", foot, width=92)
        self._toggle_btn.clicked.connect(self.toggle_remap)
        fl.addWidget(self._toggle_btn, 0)
        root.addWidget(foot, 0)
        self._footer = foot

    # ------------------------------------------------------------------
    # 清单
    # ------------------------------------------------------------------
    def rebuild_list(self) -> None:
        """重建 7 行（首次装配 / 换设备时调用；只刷高亮走 refresh_list）。"""
        for r in list(self._rows.values()):
            self._list_lay.removeWidget(r)
            r.setParent(None)
            r.deleteLater()
        self._rows = {}
        for i, bid in enumerate(REMAP_ORDER, start=1):
            row = _ListRow(bid, i, self._list_holder)
            row.row_clicked.connect(self.select_button)
            self._list_lay.addWidget(row, 0)
            self._rows[bid] = row
        self.refresh_list()

    def refresh_list(self) -> None:
        km = self.controller.get_keymap()
        for bid, row in self._rows.items():
            row.refresh(action_summary(km.get(bid)), bid == self._selected)

    # ------------------------------------------------------------------
    # 鼠标图
    # ------------------------------------------------------------------
    def rebuild_mouse(self) -> None:
        """按当前目标鼠标重画（换设备 / 进页面时调用）。"""
        layout = keymap_mod.layout_for_pid(self.controller.cfg.pid)
        self.map_view.set_selected(self._selected)
        self.map_view.set_layout(layout)

    def select_button(self, bid: str) -> None:
        self._selected = bid
        self.map_view.set_selected(bid)     # 只重画热点，底图不动
        self.refresh_list()
        self.build_editor(bid)

    # ------------------------------------------------------------------
    # 编辑区
    # ------------------------------------------------------------------
    def reserve_editor_height(self) -> None:
        """把编辑区按"最高的那种动作"冻结成固定高度。

        坑（原版注释里记着的）：编辑区随内容撑合的话，四种动作高度不同
        （「键盘按键」多一个录入框、「鼠标按键」多一个下拉），选一下动作整个
        窗口就高度跳变——用户看到的是"我点了个下拉，窗口抖一下"。所以量出最大
        高度后 setFixedHeight，之后无论选哪种动作页面 sizeHint 恒定。

        **必须量"整个编辑区"，不能只量取值区**（这个我第一版写错过）：编辑区
        除了取值区还有 标题 + "动作"标签 + 动作下拉 + 说明行，只按取值区的高度
        冻结会把固定高度定小，于是所有控件被压叠在一起（截图里能看到"恢复默认"
        下拉压着下面两行说明）。Tk 版量的就是 `_remap_editor.winfo_reqheight()`，
        即整块，这里照做。
        """
        tallest = 0
        self._measuring = True
        try:
            for atype, act in (("default", {}), ("none", {}),
                               ("key", {"key": ""}), ("mouse", {"button": ""})):
                self.build_editor("left", atype_override=atype, act_override=act)
                self._editor.adjustSize()
                tallest = max(tallest, self._editor.sizeHint().height())
        finally:
            self._measuring = False
        # 冻结值必须 >1：量测拿不到有效尺寸时宁可退回自然撑合，也不能压成 1 像素
        self._editor_h = tallest if tallest > 1 else 0
        self.show_editor_placeholder()

    def _forget_capture_box(self) -> None:
        """取值区即将重建，旧的捕获框引用先作废（否则会 setProperty 到已销毁对象）。"""
        self._capture_lbl = None
        self._capture_txt = None

    def _clear(self, lay: QtWidgets.QLayout) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _apply_editor_freeze(self) -> None:
        if self._measuring or self._editor_h <= 0:
            return
        self._editor.setFixedHeight(self._editor_h)
        self._editor.setSizePolicy(SizePolicy.Preferred, SizePolicy.Fixed)

    def show_editor_placeholder(self) -> None:
        self.capture_end()
        self._forget_capture_box()
        self._clear(self._editor_lay)
        # 一句话、不带句号：整句交给自动折行会折在词中间，而且行宽一旦超过编辑区
        # 那个（按各状态量出来的）冻结宽度就会被卡片切掉
        lbl = QtWidgets.QLabel("点上面的按键行即可修改它的功能", self._editor)
        lbl.setObjectName("cardNote")
        lbl.setFont(FONT_SMALL)
        lbl.setWordWrap(True)
        self._editor_lay.addWidget(lbl, 0)
        self._editor_lay.addStretch(1)
        self._apply_editor_freeze()

    def build_editor(self, bid: str, atype_override: str | None = None,
                     act_override: dict | None = None) -> None:
        """重建整个编辑区。

        `atype_override` / `act_override` 只给 `reserve_editor_height` 的量测用：
        它要按每种动作各渲染一遍量高度，而不必真的改配置。
        """
        self.capture_end()
        self._forget_capture_box()
        self._clear(self._editor_lay)
        if atype_override is None:
            act = self.controller.get_keymap().get(bid)
            # 没配过的按键＝保持出厂功能，对应下拉里的「恢复默认」
            atype = act.get("type", "default") if act else "default"
        else:
            atype = atype_override
            act = dict(act_override or {})

        title = QtWidgets.QLabel(
            "编辑：" + keymap_mod.STANDARD_BUTTONS.get(bid, bid), self._editor)
        title.setFont(FONT_UI)
        self._editor_lay.addWidget(title, 0)

        cap = QtWidgets.QLabel("动作", self._editor)
        cap.setObjectName("cardNote")
        cap.setFont(FONT_SMALL)
        self._editor_lay.addWidget(cap, 0)

        dd = Dropdown(self._editor, _ACTION_IDS.get(atype, ACTION_TYPES[0][1]),
                      [lbl for _, lbl in ACTION_TYPES],
                      on_pick=lambda lbl, b=bid: self.on_type_change(b, lbl),
                      width=_ACTION_DD_W)
        self._editor_lay.addWidget(dd, 0, AlignmentFlag.AlignLeft)

        note = QtWidgets.QLabel("下拉切换功能 · 改动即时生效", self._editor)
        note.setObjectName("cardNote")
        note.setFont(FONT_SMALL)
        self._editor_lay.addWidget(note, 0)

        self._editor_lay.addWidget(
            self._make_value_zone(self._editor, bid, atype, act or {}), 0)
        self._editor_lay.addStretch(1)
        self._apply_editor_freeze()

    def _make_value_zone(self, parent, bid: str, atype: str,
                         act: dict) -> QtWidgets.QWidget:
        """按动作类型造取值区（也用于 reserve_editor_height 的量测）。"""
        zone = QtWidgets.QWidget(parent)
        zl = QtWidgets.QVBoxLayout(zone)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.setSpacing(4)

        if atype == "key":
            # 两行各说一件事，用显式换行控制断行位置（自动折行会把句子切得难看）
            hint = QtWidgets.QLabel(
                "点下方框，直接按键盘录入\n组合键：按住 Ctrl / Shift 再按主键",
                zone)
            hint.setObjectName("cardNote")
            hint.setFont(FONT_SMALL)
            zl.addWidget(hint, 0)

            cur = act.get("key", "")
            box = ClickFrame(zone)
            box.setObjectName("captureBox")
            box.setFixedWidth(EDITOR_WRAP_W)
            bl = QtWidgets.QVBoxLayout(box)
            bl.setContentsMargins(8, 6, 8, 6)
            txt = QtWidgets.QLabel(cur or "点这里，再按键盘上的键", box)
            txt.setAlignment(AlignmentFlag.AlignCenter)
            txt.setFont(FONT_UI)
            bl.addWidget(txt)
            box.setProperty("empty", not bool(cur))
            box.clicked.connect(lambda b=bid: self.capture_start(b))
            zl.addWidget(box, 0, AlignmentFlag.AlignLeft)
            if not self._measuring:
                self._capture_lbl = box
                self._capture_txt = txt
        elif atype == "mouse":
            cap = QtWidgets.QLabel("映射到的鼠标键", zone)
            cap.setObjectName("cardNote")
            cap.setFont(FONT_SMALL)
            zl.addWidget(cap, 0)
            others = [lbl for b, lbl in keymap_mod.STANDARD_BUTTONS.items()
                      if b != bid]
            bid_cur = act.get("button", "")
            # 空目标 = 还没选，不能显示成第一项
            cur = keymap_mod.STANDARD_BUTTONS.get(bid_cur, "") if bid_cur \
                else "选择映射"
            zl.addWidget(
                Dropdown(zone, cur, others,
                         on_pick=lambda lbl, b=bid:
                         self.pick_mouse_target(b, lbl),
                         width=_ACTION_DD_W),
                0, AlignmentFlag.AlignLeft)
        elif atype == "none":
            zl.addWidget(caption("禁用后该按键不触发任何功能", zone), 0)
        else:  # default：不改这只键，恢复鼠标原本的功能
            zl.addWidget(caption("恢复这只键原本的功能，不做任何改键", zone), 0)
        return zone

    # ------------------------------------------------------------------
    # 动作读写
    # ------------------------------------------------------------------
    def on_type_change(self, bid: str, label: str) -> None:
        self.capture_end()
        atype = _ACTION_LABELS.get(label, "default")
        if atype == "default":        # 恢复默认 = 直接把这只键的配置删掉
            self._note_save_error(self.controller.clear_button_action(bid))
        else:
            act: dict = {"type": atype}
            if atype == "key":
                act["key"] = ""       # 空键名 = 半成品：清单显示「默认」，钩子放行
            elif atype == "mouse":
                # 不预填候选第一项（否则一切换类型就"自动改成左键"了）：
                # 空目标 = 半成品，用户在下拉里点了才算改
                act["button"] = ""
            self._note_save_error(self.controller.set_button_action(bid, act))
        # 图上热点只表示"选中哪颗键"，跟映射内容无关 → 不用重画鼠标图
        self.refresh_list()
        self.build_editor(bid)
        self.remapped.emit()

    def on_value_change(self, bid: str, atype: str, value: str) -> None:
        """改动作参数（键名 / 鼠标目标）。

        atype 是动作类型，字段名由 _VALUE_FIELD 决定（key→"key"、mouse→"button"）
        ——两者不总同名，直接 act[atype] 会把鼠标目标写进一个不存在的 "mouse"
        字段。写值前先把另一种取值字段摘掉，避免一条动作里同时留着 key 与 button
        （清洗与钩子只认自己类型的字段）。
        """
        field = _VALUE_FIELD.get(atype, atype)
        km = self.controller.get_keymap()
        act = dict(km.get(bid) or {})
        act["type"] = atype
        act[field] = value
        for other in _VALUE_FIELD.values():
            if other != field:
                act.pop(other, None)
        self._note_save_error(self.controller.set_button_action(bid, act))
        self.refresh_list()
        self.remapped.emit()

    def _note_save_error(self, err: str | None) -> None:
        """落盘失败就吱一声（成功保持安静，与原来的行为一致）。

        配置目录在 OneDrive 同步盘里时 `os.replace` 会偶发 PermissionError：
        静默吞掉的话界面显示"已保存"、重启后改动却不见了，用户完全无从察觉
        （2026-09-17 审查发现：set/clear 的返回值从来没人接）。
        """
        if err:
            self.status.emit(
                f"改动已生效，但配置写入失败（{err}）——重启后可能丢失")

    @staticmethod
    def _button_id_from_label(label: str) -> str:
        for b, lbl in keymap_mod.STANDARD_BUTTONS.items():
            if lbl == label:
                return b
        return ""

    def pick_mouse_target(self, bid: str, label: str) -> None:
        """下拉里选好「映射到哪只鼠标键」后写入。

        动作类型固定传 "mouse"（不是 "button"）：钩子与配置清洗都按
        type ∈ {none, key, mouse} 认，传成 "button" 会让这条映射既显示成
        「默认」、又不生效、还会在下次读配置时被丢掉。
        """
        self.on_value_change(bid, "mouse", self._button_id_from_label(label))
        self.build_editor(bid)

    # ------------------------------------------------------------------
    # 键盘捕获（轮询系统键状态，不依赖按键事件）
    # ------------------------------------------------------------------
    def capture_start(self, bid: str) -> None:
        """点捕获框 → 进入「等你按一个键」状态。

        取键方式是**轮询系统键状态**（kb.pressed_vks）：关着输入法时按键事件也
        能用，但开着中文输入法时字符类按键的键名不可靠、甚至收不到事件，只有
        系统键状态是准的；顺带还能读到 Ctrl/Shift，所以组合键也能记下来。
        Tk 版用 `after(20ms)` 链，这里换成 QTimer（同 20ms 节拍）。
        轮询有 CAPTURE_MAX_S 上限：用户点了捕获框却不按键时，20ms 一轮、每轮查
        255 个键会一直跑下去（切页面、缩到托盘都不停）。
        """
        self.capture_end()             # 已有捕获链就先停：否则两条链并行轮询
        box = self._capture_lbl
        if box is not None:
            # 编辑区随时可能被重建（换按键 / 占位态），引用可能已失效
            try:
                box.setProperty("armed", True)
                box.setProperty("empty", False)
                repolish(box)
                if self._capture_txt is not None:
                    self._capture_txt.setText("按下键盘按键…")
            except RuntimeError:
                # 控件已析构：提示刷不出来，"静默捕获"比不捕获更坏
                # （随手敲键盘就改映射），直接放弃这次捕获
                self._capture_lbl = None
                self._capture_txt = None
                self._capturing = None
                return
        self._capturing = bid
        self._prev_down = kb.pressed_vks()   # 快照：已经按住不放的键不算新按下
        self._capture_deadline = time.monotonic() + CAPTURE_MAX_S
        self._poll_timer.start()

    def capture_end(self) -> None:
        """退出捕获状态，停掉键状态轮询（只改状态，不碰控件内容）。"""
        self._poll_timer.stop()
        self._capturing = None
        box = self._capture_lbl
        if box is not None:
            try:
                box.setProperty("armed", False)
                repolish(box)
            except RuntimeError:
                pass

    def _poll_keys(self) -> None:
        """轮询「有没有新按下的键」，命中就记成键名并结束捕获。

        修饰键、鼠标键都不算一次绑定；Esc 放弃；表里没有的键当没按、继续等；
        超过 CAPTURE_MAX_S 自动放弃（并复原编辑区）。
        """
        bid = self._capturing
        if not bid:
            self._poll_timer.stop()
            return
        if time.monotonic() > self._capture_deadline:
            self.build_editor(bid)          # 内部会 capture_end
            return
        # 取键轮询的是**全局**键状态：Alt-Tab 到别的窗口再敲键盘，那一下既进了
        # 目标程序又会被这里录成映射。窗口一失焦就放弃本次捕获。
        win = self.window()
        if win is not None and not win.isActiveWindow():
            self.build_editor(bid)          # 内部会 capture_end
            return
        down = kb.pressed_vks()
        fresh = sorted(down - self._prev_down - kb.MODIFIER_VKS - kb.MOUSE_VKS)
        self._prev_down = down
        vk = fresh[0] if fresh else 0
        if vk == kb.VK_ESCAPE:              # Esc = 放弃本次捕获，保留原值
            self.build_editor(bid)
            return
        name = kb.combo_name(down, vk) if kb.vk_name(vk) else ""
        if not name:                        # 还没按键 / 键名认不出 → 继续等
            return
        self.on_value_change(bid, "key", name)
        self.build_editor(bid)

    # ------------------------------------------------------------------
    # 暂停 / 恢复
    # ------------------------------------------------------------------
    def refresh_state(self) -> None:
        """刷新底部状态文案（救命组合键会在后台暂停/恢复，所以要能被动刷新）。"""
        t = THEME
        r = self.remapper
        # 钩子没装上时"暂停/恢复"无从谈起：按钮必须禁用，
        # 否则点下去只是静默翻转一个没人执行的开关（用户以为暂停成功了）
        self._toggle_btn.setEnabled(r.active)
        if not r.active:
            self._state_lbl.setText("改键未生效：" + (self._remap_err or "钩子没装上"))
            self._state_lbl.setStyleSheet(f"color: {t.warn};")
            self._toggle_btn.setText("暂停改键")
        elif r.enabled:
            # 措辞要点（照搬原版）：① 点明是"软件层"（与顶部的设备连接状态无关，
            # 鼠标离线也照样生效，避免"离线+已生效"的困惑）；② 说清退出程序就失效；
            # ③ 救命组合键常驻显示——万一把左键禁用了，鼠标点不了任何东西，
            #    这是唯一的键盘逃生口（暂停/恢复双向都能按，也可用右边的按钮）；
            # ④ "关闭后暂停"要和"映射保留"一起说清：退出只是摘钩子让按键回原样，
            #    配置留在 config.json 里，重开自动恢复。
            self._state_lbl.setText(
                "软件层改键生效中 · 对所有鼠标生效 · 紧急暂停 Ctrl+Alt+Shift+X"
                " · 关闭后暂停，重开自动恢复")
            self._state_lbl.setStyleSheet(f"color: {t.good};")
            self._toggle_btn.setText("暂停改键")
        else:
            # 两种状态的文案都带快捷键：它现在是双向切换（暂停/恢复都能按）
            self._state_lbl.setText(
                "改键已暂停 · 按键恢复原样 · 按 Ctrl+Alt+Shift+X 恢复")
            self._state_lbl.setStyleSheet(f"color: {t.warn};")
            self._toggle_btn.setText("恢复改键")

    def toggle_remap(self) -> None:
        self.remapper.set_enabled(not self.remapper.enabled)
        self.refresh_state()

    def focus_first(self) -> None:
        """进入本页时的复位（原版 _show_page("remap") 那一段）。"""
        self._selected = None
        self.rebuild_mouse()          # 换设备后要看到该鼠标的映射
        self.rebuild_list()
        self.show_editor_placeholder()

    def leave(self) -> None:
        """离开本页：收掉"等你按键"状态。

        不收的话轮询会在后台继续跑，而且之后随便按一下键都会悄悄改写那只按键的
        映射（原版注释里写明的后果）。
        """
        self.capture_end()
