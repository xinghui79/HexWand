"""两个内嵌页面：设备（我的鼠标/其它已知型号）+ 诊断（人话摘要，原始数据进日志）。

【本包的前身是一套 Tk/Tkinter 实现，已于 2026-09-16 移除（备份见
`%APPDATA%\\HexWand\\backup\\`）。下文提到的「Tk 版」都指那一版。】

2026-09-16 用户要求：把它们从"顶栏按钮 + 独立弹窗"改成导航里的两个页面
（主面板 / 键位 / 设备 / 诊断，共 4 页）。因此：

- `QDialog` → `QWidget`，塞进 QStackedWidget；单例引用、`raise_()`/
  `activateWindow()`、关闭与销毁那套窗口管理**全部删除**（不再有子窗口）；
- 各类都**覆写 `sizeHint()` 报一个固定的偏好高度，不跟着内容长**。原因：窗口高度
  是"所有页面取较大者"（`window._target_win_h`），若让诊断页（异常文案会变长）或
  设备列表（十几行）参与竞争，整个窗口会被撑高，主面板随之多出一大片空白。
  内容超出可视区就在页内滚动。
- 页面外边距与主面板/键位页一致（24, 0, 24, 12），切页时左右不跳。

Tk 版用 `tk.Toplevel` + `scrolledtext.ScrolledText` + 手工 pack 每行；
Qt 里换成 `QPlainTextEdit` / `QScrollArea`，列表行用布局管理器排，
"正在扫描…占位 → 后台扫完回主线程重画"这套流程（原版的关键正确性点）原样保留。
"""

from __future__ import annotations

import logging

from .. import hardware as hw
from ..core.paths import LOG_PATH
from .qt import (
    FrameShape,
    QtCore,
    QtGui,
    QtWidgets,
    WidgetAttribute,
)
from .theme import FONT_BOLD_SMALL, FONT_MONO, FONT_SMALL, THEME, font
from .widgets import PillButton


log = logging.getLogger(__name__)


def _fix_bg(w: QtWidgets.QWidget, color: str) -> None:
    """把一个容器的底色固定成指定颜色（改调色板，不靠 QSS）。

    为什么要手动刷调色板：`QAbstractScrollArea` 的 viewport 背景角色是
    `QPalette.Base`，系统浅色主题下就是**白色**。本项目**故意**没有给裸
    `QWidget` 设全局背景（给裸 QWidget 设 background 会命中卡片内每一个用来
    分组的空控件，各自画出一块深色，像挖了几个洞），于是滚动区会露出一条白底
    带子，上面本来就很暗的小字（#8b93a5）在白底上直接看不清。

    坑：只调 `setAutoFillBackground(False)` **不管用** —— 风格仍会按 viewport
    的背景角色去填。必须把该角色的颜色本身换成项目底色。
    """
    w.setAutoFillBackground(True)
    pal = w.palette()
    pal.setColor(w.backgroundRole(), QtGui.QColor(color))
    w.setPalette(pal)


# ===========================================================================
# 诊断
# ===========================================================================
def collect_diagnose(controller, remapper=None) -> tuple[str, str]:
    """收集诊断信息（纯逻辑，不碰控件），返回 (页面文本, 日志详情)。

    页面上只留**使用者看得懂的体检结论**：驱动、是否找到鼠标、连接、改键，
    正常一行带过，异常才给一句"为什么 + 下一步"。后端名/VID 直方图/逐接口
    枚举这些是给 AI 与日志排查的原始数据，去重后写进 startup.log ——
    点一次"诊断"刷新记一条（见 window.refresh_diagnose）。
    """
    summary: list[str] = []
    detail: list[str] = []

    # ---- 1) 驱动扩展 ----
    try:
        bname, total, vids = hw.get_backend_summary()
    except Exception as e:  # noqa: BLE001
        bname, total, vids = f"出错: {e}", 0, []
    detail += ["==== HID 后端 ====",
               f"后端: {bname}",
               f"系统 HID 设备数: {total}"]
    if vids:
        detail.append("各 VID: " + ", ".join(f"{v} x{c}" for v, c in vids))
    if (bname == "unavailable" or "出错" in bname
            or any(c == 0 and ("失败" in v or "不可用" in v)
                   for v, c in vids)):
        reason = next((v for v, c in vids if c == 0 and
                       ("失败" in v or "不可用" in v)), bname)
        summary.append(f"驱动扩展: 异常 —— {reason}（重装或重新打包）")
    else:
        summary.append(f"驱动扩展: 正常（{bname}）")

    # ---- 2) 是否找得到鼠标（按物理设备去重：同一只鼠标会占多个 HID 接口） ----
    scanned = ""
    try:
        devs = hw.scan_known_devices()
    except Exception as e:  # noqa: BLE001
        devs = []
        scanned = f"扫描出错 —— {e}"

    def _label(d) -> str:
        """设备显示名：产品字符串缺失时退回型号库名称/PID——别打印 "None"。"""
        if d.name:
            return d.name
        info = hw.known_device(d.pid)
        return info.product if info is not None else f"PID 0x{d.pid:04X}"

    known_devs = [d for d in devs if d.known]
    groups: dict[tuple[str, int], int] = {}
    for d in known_devs:
        key = (_label(d), d.pid)
        groups[key] = groups.get(key, 0) + 1
    detail += ["", "==== 已收录品牌设备扫描（按接口逐条）===="]
    if not devs:
        detail.append("未找到任何已收录品牌的 HID 设备。")
    for d in devs:
        # feat = Feature 报表长度（含 report id）：排查"选不出控制接口"时
        # 第一顺位要看它（雷蛇控制接口应为 91，见 CLAUDE.md 的排查顺序）。
        # 只对已收录型号读，避免把机器上所有 HID 接口都开一遍。
        feat = hw.feature_len_of_path(d.path) if d.known else None
        mi = f"  mi=0x{d.interface:02X}" if d.interface is not None else ""
        detail.append(f"  {_label(d)}  PID=0x{d.pid:04X}  "
                      f"usage_page=0x{d.usage_page:04X}{mi}  "
                      f"feat={feat if feat is not None else '?'}  "
                      f"{'已知' if d.known else '未知'}")
    if scanned:
        summary.append(f"鼠标: {scanned}")
    elif not groups:
        if devs:
            summary.append("鼠标: 未找到 —— 只看到该品牌的其它设备（键盘/未收录型号等）")
        else:
            summary.append("鼠标: 未找到 —— 检查接收器/USB 是否插好，"
                           "或动一下鼠标让它醒来")
    else:
        parts = [f"{name} ×{cnt} 个接口"
                 for (name, _pid), cnt in groups.items()]
        summary.append("鼠标: 已找到 —— " + "；".join(parts))

    # ---- 3) 连接状态 ----
    detail += ["", "==== 连接状态 ===="]
    try:
        detail.append(controller.get_info())
    except Exception as e:  # noqa: BLE001
        detail.append(f"状态读取出错: {e}")
    try:
        detail.append(f"Report ID: {controller.dev.report_id}")
        if controller.dev.feature_len:
            detail.append(f"Feature Report 长度: {controller.dev.feature_len} 字节")
    except Exception as e:  # noqa: BLE001
        detail.append(f"连接细节: {e}")
    try:
        if controller.connected:
            dev = controller.dev
            try:
                fw = dev.get_firmware()
            except Exception:  # noqa: BLE001
                fw = "?"
            # 模拟模式下 dev.name 本身就是"模拟设备 (Simulation)"，不再叠标记
            summary.append(f"连接: 已连接 —— {dev.name} · "
                           f"固件 {fw} · PID 0x{dev.pid or 0:04X}")
        else:
            summary.append("连接: 未连接 —— 鼠标休眠/关机时不回话，"
                           "动一下鼠标，3 秒一轮自动重试")
    except Exception as e:  # noqa: BLE001
        summary.append(f"连接: 状态读取失败 —— {e}")
    # 这里**不列"UI 工具包 / Qt 绑定版本"**：那是排查界面问题的信息，不是设备
    # 诊断的一部分（用户 2026-09-16 反馈多余）。需要时看 startup.log —— 每次启动
    # 都会记一行 "UI: Qt6 via PyQt6 (Qt 6.11.0)"。

    # ---- 4) 改键钩子 ----
    if remapper is not None:
        detail += ["", "==== 改键钩子 ===="]
        try:
            detail.append(f"钩子已装: {remapper.active}    改键启用: {remapper.enabled}")
            detail.append(f"累计丢弃事件: {remapper.dropped} 次"
                          f"    卡死自动暂停: {remapper.stalls} 次")
            if not remapper.active:
                summary.append("改键: 未生效 —— 钩子没装上（见启动日志），"
                               "其余功能不受影响")
            elif not remapper.enabled:
                summary.append("改键: 已暂停 —— 按键恢复原样，"
                               "Ctrl+Alt+Shift+X 或键位页按钮可恢复")
            else:
                summary.append(f"改键: 已生效 —— 丢事件 {remapper.dropped} 次 · "
                               f"卡死暂停 {remapper.stalls} 次（正常应一直是 0）")
        except Exception as e:  # noqa: BLE001
            summary.append(f"改键: 状态读取出错 —— {e}")

    summary += ["", "原始数据已写入日志（排查问题发这一份）：", str(LOG_PATH)]
    return "\n".join(summary), "\n".join(detail)


class DiagnosePage(QtWidgets.QWidget):
    """诊断结果页（主窗内的一页）。

    内容是 `collect_diagnose` 的**人话摘要**：健康时一行一条，异常的那条才加长
    （原始数据不在这页，见 startup.log）。等宽字体，`QPlainTextEdit` 直接铺满
    页面：`#diagText` 的
    QSS 已经给了卡片底色 + 圆角 + 描边，所以外面**不再套 Card**（避免卡片套卡片）。

    尺寸相关的一大段代码（`_fit_to_content` / `_grow_to_fit` / 各类 padding 补偿）
    随着"窗口按内容撑高"这个需求一起删掉了 —— 页面高度由主窗口统一决定，
    高度不够时 `QPlainTextEdit` 自己出滚动条。
    """

    # 偏好高度：**必须小于键位页**，否则会把窗口顶高（见模块 docstring）。
    _HINT_H = 300

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 0, 24, 12)
        lay.setSpacing(0)
        self.text = QtWidgets.QPlainTextEdit(self)
        self.text.setObjectName("diagText")
        self.text.setFont(FONT_MONO)
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(
            QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.text, 1)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 — Qt 命名
        return QtCore.QSize(480, self._HINT_H)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 — Qt 命名
        return QtCore.QSize(200, 120)

    def set_text(self, text: str) -> None:
        """填内容，并尽量保住原来的滚动位置（切回该页时不会跳回顶部）。"""
        bar = self.text.verticalScrollBar()
        pos = bar.value()
        self.text.setPlainText(text)
        bar.setValue(min(pos, bar.maximum()))


# ===========================================================================
# 设备（原「可用设备」）
# ===========================================================================
class DevicesPage(QtWidgets.QWidget):
    """设备页：型号库列表（「我的鼠标」/「其它已知型号」两段），
    接入状态一目了然，多台同时接入时可切换。

    与窗口版的差别：没有标题栏、没有固定宽度、**删掉了 `_fit_height()`**
    （那是为了"窗口按内容自动撑高"服务的，页面化之后反而会把整窗撑高）。
    """

    _HINT_H = 340

    def __init__(self, mw) -> None:
        super().__init__(mw)
        self.mw = mw
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 0, 24, 12)
        lay.setSpacing(8)

        # 页面名已经在导航里显示了（「设备」），这里不再重复大标题，只留一句说明
        desc = QtWidgets.QLabel(
            "插上设备后自动识别并连接；多只同时接入时，可在下面点「切换」"
            "更换当前鼠标。", self)
        desc.setObjectName("cardNote")
        desc.setFont(FONT_SMALL)
        desc.setWordWrap(True)
        lay.addWidget(desc, 0)

        self._body = QtWidgets.QWidget(self)
        self._body_lay = QtWidgets.QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(8)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(FrameShape.NoFrame)
        # 视口/内容容器都刷成项目底色（见 _fix_bg 的说明）
        _fix_bg(scroll.viewport(), THEME.bg)
        _fix_bg(self._body, THEME.bg)
        # **不设 `AdjustToContents`**：那是"按内容撑大自己"的策略，页面化之后会
        # 顺着 sizeHint 把整个窗口顶高（内容多时尤其明显）。页面高度归主窗口管，
        # 内容超出就出滚动条。
        scroll.setWidget(self._body)
        lay.addWidget(scroll, 1)
        self._scroll = scroll

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 — Qt 命名
        return QtCore.QSize(480, self._HINT_H)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 — Qt 命名
        return QtCore.QSize(200, 140)

    def paint(self, connected_pids: set[int] | None = None) -> None:
        """重建型号列表。connected_pids=None 表示先占位，后台扫完再刷。

        列表分两段：
        - 我的鼠标：按使用记录（config.devices，最近在前），当前不在记录里的
          目标设备也并入；这是用户实际拥有的。
        - 其它已知型号：库里剩余型号，暗色紧凑展示（PID 来自设备库，标题不替它
          下结论，也不冒充"受支持"）。
        同一物理型号的有线/无线多个 PID 在 product_groups() 里已合并成一行。
        """
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if connected_pids is None:
            lbl = QtWidgets.QLabel("正在扫描 HID 设备…", self._body)
            lbl.setObjectName("cardNote")
            lbl.setFont(FONT_SMALL)
            self._body_lay.addWidget(lbl, 0)
            self._body_lay.addStretch(1)
            return

        groups = hw.product_groups()
        cur = self.mw.controller.cfg.pid
        # pid=0 = 还没选择目标（新装）：不能把 0 当型号并进"我的鼠标"
        my_pids = set(self.mw.controller.cfg.devices) | ({cur} if cur else set())
        mine = [g for g in groups if set(g["pids"]) & my_pids]
        others = [g for g in groups if g not in mine]

        self._body_lay.addWidget(self._section("我的鼠标"), 0)
        if not mine:
            lbl = QtWidgets.QLabel(
                "还没有鼠标——插上已收录的型号会自动出现在这里", self._body)
            lbl.setObjectName("cardNote")
            lbl.setFont(FONT_SMALL)
            self._body_lay.addWidget(lbl, 0)
        for g in mine:
            self._body_lay.addWidget(
                self._device_row(g, connected_pids, cur, dim=False), 0)

        if others:
            self._body_lay.addWidget(self._section("其它已知型号"), 0)
            for g in others:
                self._body_lay.addWidget(
                    self._device_row(g, connected_pids, cur, dim=True), 0)
        self._body_lay.addStretch(1)

    def _section(self, text: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(text, self._body)
        lbl.setFont(FONT_BOLD_SMALL)
        lbl.setStyleSheet(f"color: {THEME.dim};")
        return lbl

    def _device_row(self, group: dict, connected_pids: set[int],
                    cur: int, dim: bool) -> QtWidgets.QWidget:
        """一行型号：名称 + 连接方式/状态，已接入且非当前的可点「切换」。"""
        t = THEME
        fg = t.fg if not dim else t.dim
        row = QtWidgets.QFrame(self._body)
        row.setObjectName("card")
        row.setAttribute(WidgetAttribute.WA_StyledBackground, True)
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(8)

        pids = set(group["pids"])
        is_cur = cur in pids
        connected = bool(pids & connected_pids)

        info = QtWidgets.QWidget(row)
        il = QtWidgets.QVBoxLayout(info)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(2)
        name_row = QtWidgets.QWidget(info)
        nl = QtWidgets.QHBoxLayout(name_row)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(8)
        name = QtWidgets.QLabel(group["product"], name_row)
        name.setFont(font(11, bold=True))
        name.setStyleSheet(f"color: {fg};")
        nl.addWidget(name, 0)
        tag = " · 当前使用" if is_cur else ""
        if is_cur:
            tag += " · 在线" if self.mw.link_state == "online" else " · 未连接"
        elif connected:
            tag = " · 已接入"
        if is_cur:
            tag_fg = t.good if self.mw.link_state == "online" else t.offline
        elif connected:
            tag_fg = t.accent
        else:
            tag_fg = t.dim
        tag_lbl = QtWidgets.QLabel(tag, name_row)
        tag_lbl.setFont(FONT_BOLD_SMALL)
        tag_lbl.setStyleSheet(f"color: {tag_fg};")
        nl.addWidget(tag_lbl, 0)
        nl.addStretch(1)
        il.addWidget(name_row, 0)
        sub = QtWidgets.QLabel(f"{group['modes']} · {group['dpi_max']} DPI", info)
        sub.setObjectName("cardNote")
        sub.setFont(FONT_SMALL)
        il.addWidget(sub, 0)
        lay.addWidget(info, 1)

        if connected and not is_cur:
            btn = PillButton("切换", row, width=74)
            pid = self._pick_pid(group, connected_pids)
            btn.clicked.connect(lambda _c=False, p=pid: self.mw.use_device(p))
            lay.addWidget(btn, 0)
        elif is_cur:
            # 圆点表达的是**链路状态**，不是"接收器插着没"：无线鼠标休眠/关机时
            # 接收器照样能被枚举到（connected_pids 只看得到在不在），只按它上色
            # 这颗点就永远是绿的（2026-09-17 用户反馈）。判据顺序与行标签同源：
            # **link_state 优先**——否则扫描缓存与链路不一致时，会出现
            # 标签"在线"、点却灰着的自相矛盾（2026-09-18 preview 暴露）。
            if self.mw.link_state == "online":
                color, tip = t.good, "已连接"
            elif connected:
                color, tip = t.offline, "未连接（鼠标休眠或已关机）"
            else:
                color, tip = t.dim, "未接入（没有检测到这只鼠标）"
            dot = QtWidgets.QLabel("●", row)
            dot.setStyleSheet(f"color: {color};")
            dot.setToolTip(tip)
            lay.addWidget(dot, 0)
        return row

    @staticmethod
    def _pick_pid(group: dict, connected_pids: set[int]) -> int:
        """从型号组里挑一个 PID：优先当前接入的；都不接入选组内第一个。"""
        for p in group["pids"]:
            if p in connected_pids:
                return p
        return group["pids"][0]
