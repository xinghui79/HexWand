"""外观基元：色板 + 字体 + 全局 QSS。

【本包的前身是一套 Tk/Tkinter 实现，已于 2026-09-16 移除（备份见
`%APPDATA%\\HexWand\\backup\\`）。下文提到的「Tk 版」都指那一版，作用是留下
当年的坑与对应做法，不是指项目里还留着那份代码。】

角色与 Tk 版的 `style.py` 完全不同：
- Tk 版里 `rounded_rect()` 要用 Canvas 多边形自绘圆角（tkinter 不支持）；
  Qt 的 QSS 原生支持 `border-radius`，**这个函数在新架构里不存在了**。
- Tk 版主题是"值 + 自绘函数"，Qt 版是"值 + 一张全局样式表"。所有控件的外观
  收敛在 `build_qss()` 一处，控件代码里不再出现颜色字面量。

色值与 Tk 版逐字段对齐（`Theme` 字段名保持一致），方便两边并排比对回归差异。

对比度硬规则（照搬 Tk 版的教训）：任何 ink 配 card_in 时亮度差要够，
特别是 disabled 态——QSS 里**每个** `:disabled` 都必须显式写颜色，
Qt 默认的禁用灰在深色底上几乎看不见。
"""

from __future__ import annotations

from dataclasses import dataclass

from .qt import QtGui, QtWidgets


@dataclass(frozen=True)
class Theme:
    # === surface（层叠背景：下→上）====================================
    bg:         str = "#0b0d12"   # 窗口底色（最下层）
    card:       str = "#14171f"   # 卡片底色（电量 / 轮询 / DPI / 节能）
    card_in:    str = "#1a1e28"   # 卡片内嵌：输入框底、分段控件槽

    # === ink（前景色：文字/图标）======================================
    fg:          str = "#eef0f4"  # 主文字（亮白）
    dim:         str = "#8b93a5"  # 次要文字 / 未激活按钮文字
    disabled_fg: str = "#aab2c0"  # 禁用态文字（在 card_in 上对比度足够，必可见）
    on_accent:   str = "#0b0d12"  # 主色按钮上的文字（紫底深字）

    # === border（描边/分隔）==========================================
    border: str = "#262b38"

    # === role（语义色：状态指示）=====================================
    accent:       str = "#7c6cff"  # 主色（紫罗兰）
    accent_light: str = "#a79bff"  # 主色亮态：悬浮
    accent_dim:   str = "#2c2a4a"  # 主色暗底：未选中 hover
    active:       str = "#7c6cff"  # 按钮激活态（语义别名 = accent）
    good:         str = "#34d399"  # 健康 / 在线（绿）
    warn:         str = "#fbbf24"  # 警告（黄）
    offline:      str = "#f87171"  # 离线/红点（红）
    hud_bar_off:  str = "#2a2a2a"  # HUD 背景条（未填充段）
    statusbar_bg: str = "#07080c"  # 状态栏底色


THEME = Theme()

# 字体族：Segoe UI 系列在 Win10/11 上必装。"Segoe UI Variable Display" 是
# Win11 的新字体，Win10 上没有——Qt 找不到会自动回落到系统默认，不会报错，
# 所以这里可以放心写。
_FAMILY = "Segoe UI"
_FAMILY_TITLE = "Segoe UI Variable Display"
_FAMILY_MONO = "Consolas"


def font(size: int, bold: bool = False, family: str = _FAMILY,
         pixel: bool = False) -> QtGui.QFont:
    """按磅值（默认）或像素值造字体。

    Qt 的磅值会跟系统 DPI 自动缩放，与 Tk 的 `("Segoe UI", 10)` 语义一致；
    HUD 那种"必须锁死物理像素"的场景传 `pixel=True`，走 setPixelSize。
    """
    f = QtGui.QFont(family)
    if pixel:
        f.setPixelSize(max(1, int(size)))
    else:
        f.setPointSize(int(size))
    f.setBold(bold)
    return f


# 常用字体（与 Tk 版 Theme 的 font_* 字段一一对应）
FONT_UI = font(10)
FONT_TITLE = font(17, bold=True, family=_FAMILY_TITLE)
FONT_BIG = font(40, bold=True, family=_FAMILY_TITLE)   # 电量 hero 数字
FONT_POLL_BIG = font(24, bold=True, family=_FAMILY_TITLE)   # 轮询率卡的当前值大字
FONT_SMALL = font(9)
FONT_MONO = font(10, family=_FAMILY_MONO)
FONT_BOLD_SMALL = font(9, bold=True)
FONT_BOLD_UI = font(10, bold=True)
FONT_HEAD_META = font(12, bold=True)   # 顶栏右侧的设备名/连接状态（10pt 太小，用户反馈）
FONT_LABEL = font(12)                  # 主面板分组行的标题（电量/DPI 档位/轮询率/节能，
                                       # 五行必须同规格 —— 单改一行会参差，用户反馈）
FONT_SEG_VALUE = font(12, bold=True)     # 轮询率/档位数字

# 下拉框右侧"箭头区"宽度（逻辑像素）。**唯一事实源**：
#   - QSS 的 `#dropdown::drop-down { width: ... }`（系统留白 + 点击弹出命中区）
#   - widgets.Dropdown.paintEvent 里自绘三角的水平定位
# 两处必须同值，否则三角会偏离系统留出的区域（曾经各写一个数，视觉上就在飘）。
# 也不能设 0：非 editable 的 QComboBox 靠命中 drop-down 子控件区域才弹下拉。
DROPDOWN_ARROW_ZONE = 24


def build_qss(t: Theme = THEME) -> str:
    """全局样式表。所有控件外观集中在这里，改主题只动本函数与色值。"""
    return f"""
/* ---------- 基础 ---------- */
/* 注意：**不要**给裸 `QWidget` 设 background。Qt 里 QSS 的 background 不继承，
   但会**命中每一个 QWidget 子类实例**——一旦给裸 QWidget 上底色，卡片内那些
   用来分组的空 QWidget 容器就会各自画出一块比卡片更深的底色，深色主题下
   看着像卡片被挖了几个洞。底色只给顶层窗口（#root），其余容器保持透明。 */
QWidget {{ font-family: "{_FAMILY}"; }}
#root, #page {{ background: {t.bg}; }}
QLabel {{ color: {t.fg}; }}
QToolTip {{
    background: {t.card}; color: {t.fg};
    border: 1px solid {t.border}; padding: 4px 6px;
}}

/* ---------- 卡片 ---------- */
#card {{
    background: {t.card};
    border: 1px solid {t.border};
    border-radius: 14px;
}}
#cardTitle {{ color: {t.fg}; font-weight: bold; font-size: 10pt; }}
#cardNote  {{ color: {t.dim}; font-size: 9pt; }}
#accentBar {{ background: {t.accent}; border-radius: 1px; }}
#divider   {{ background: {t.border}; }}

/* ---------- 胶囊按钮（普通 / 激活 / 悬停）---------- */
#pill {{
    background: {t.card};
    border: 1px solid {t.border};
    border-radius: 14px;
    color: {t.dim};
    padding: 6px 14px;
    min-height: 18px;
}}
#pill:hover:enabled {{ background: {t.card_in}; color: {t.fg}; }}
#pill:pressed:enabled {{ background: {t.accent_dim}; color: {t.fg}; }}
#pill[active="true"] {{
    background: {t.accent}; border-color: {t.accent}; color: {t.on_accent};
}}
#pill[active="true"]:hover:enabled {{ background: {t.accent_light}; }}
#pill:disabled {{ background: {t.card}; color: {t.disabled_fg}; }}
/* 未连接时轮询率段整体禁用，但**当前档仍要看得出来**：
   `#pill:disabled` 与 `#pill[active="true"]` 同优先级、按出现顺序后者被前者盖掉，
   于是"500 Hz 大字亮着、下面一格高亮都没有"（用户 2026-09-16 截图问过）。
   用 accent_dim 而不是 accent：禁用态的高亮要比可交互时暗一档。 */
#pill[active="true"]:disabled {{
    background: {t.accent_dim}; border-color: {t.accent_dim}; color: {t.fg};
}}

/* ---------- 分段控件 ---------- */
#segSlot {{
    background: {t.card_in};
    border-radius: 12px;
}}
#segBtn {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {t.dim};
    padding: 5px 2px;
}}
#segBtn[active="false"]:hover:enabled {{
    background: {t.accent_dim};
    color: {t.fg};
}}
#segBtn[active="true"] {{
    background: {t.accent};
    color: {t.on_accent};
}}
#segBtn:disabled {{ color: {t.disabled_fg}; }}
/* 轮询率段的按钮 objectName 是 segBtn（见 widgets.SegmentedControl.add），
   上面的 #pill[active="true"]:disabled 够不到它们：禁用态的当前档在这里
   压暗一档（accent_dim），与 pill 那条同一设计。 */
#segBtn[active="true"]:disabled {{
    background: {t.accent_dim}; border-color: {t.accent_dim}; color: {t.fg};
}}

/* ---------- DPI 档位段（整段是切换热区，输入框留给编辑）---------- */
#stageSeg {{
    background: {t.card_in};
    border-radius: 8px;
}}
#stageSeg[active="true"] {{
    background: {t.accent};
}}
/* 悬停提亮只在"未选中且已连接"时生效：QSS 没法读 controller 状态，
   所以离线时由 Python 把 hover 属性也置 false（见 StageSegment.set_hoverable） */
#stageSeg[active="false"][hoverable="true"]:hover {{
    background: {t.accent_dim};
}}
#stageSegLabel {{
    color: {t.dim};
    font-size: 9pt;
    font-weight: bold;
}}
#stageSegLabel[active="true"] {{ color: {t.on_accent}; }}
#stageSegLabel[dimmed="true"] {{ color: {t.disabled_fg}; }}

/* ---------- 输入框 ---------- */
#input {{
    background: {t.card_in};
    border: 1px solid {t.border};
    border-radius: 10px;
    color: {t.fg};
    padding: 5px 8px;
    selection-background-color: {t.accent};
    selection-color: {t.on_accent};
}}
#input[hl="true"] {{ border-color: {t.accent}; color: {t.accent_light}; }}
#input:focus {{ border-color: {t.accent}; }}
#input:disabled {{
    background: {t.card_in};
    border-color: {t.border};
    color: {t.disabled_fg};
}}

/* ---------- 下拉（自绘箭头，不用系统图元：深浅主题下颜色不可控）---------- */
#dropdown {{
    background: {t.card_in};
    border: 1px solid {t.border};
    border-radius: 10px;
    color: {t.fg};
    padding: 4px 6px 4px 10px;
    min-height: 18px;
}}
#dropdown:focus {{ border-color: {t.accent}; }}

/* 箭头**不在这里画**：网上流传的 `width:0;height:0` + 透明边框那个 CSS 三角写法，
   Qt 的样式表引擎**不支持** —— 它会把 subcontrol 的边框盒整个填成一块 8×5 的小
   方块（用户截图问过）。这里只负责把系统原生箭头**彻底关掉**，真正的三角由
   widgets.Dropdown.paintEvent 用 QPainter 画（宽度与下面的
   DROPDOWN_ARROW_ZONE 一致）。 */
#dropdown::drop-down {{
    border: none;
    background: transparent;
    /* 不能设 0：非 editable 的 QComboBox 要靠命中这个子控件区域才弹出下拉，
       压成 0 宽就没法点开了。它同时给自绘三角留出了位置。 */
    width: {DROPDOWN_ARROW_ZONE}px;
}}
#dropdown::down-arrow {{
    image: none;            /* 关键：不指定图像，系统箭头就不画 */
    width: 0px;
    height: 0px;
}}

/* ---------------------------------------------------------------------------
   警告：QSS 里**绝不能写以 # 开头的注释行**（这里踩过：用户反馈"怎么有 2 个箭头"）。
   QSS 只认斜杠星号一种注释；以 # 开头的行会被解析器当成 ID 选择器，于是它和紧随
   其后的规则被**粘成一个永不匹配的选择器**，那些规则就静默失效了。
   上面两条正是这样被吃掉过一遍 —— 原生箭头没被关掉，和自绘三角叠成了两个箭头。
   --------------------------------------------------------------------------- */
#dropdown QAbstractItemView {{
    background: {t.card};
    border: 1px solid {t.border};
    outline: none;
    color: {t.fg};
    selection-background-color: {t.accent_dim};
    selection-color: {t.fg};
    padding: 4px;
}}

/* ---------- 捕获框（键盘录入）---------- */
#captureBox {{
    background: {t.card_in};
    border: 1px solid {t.border};
    border-radius: 8px;
    color: {t.fg};
    padding: 8px;
}}
#captureBox[armed="true"] {{
    background: {t.accent_dim};
    color: {t.fg};
    border-color: {t.accent};
}}
#captureBox[empty="true"] {{ color: {t.dim}; }}
#captureBox:hover {{ border-color: {t.accent}; }}

/* ---------- 状态栏 ---------- */
#statusBar {{ background: {t.statusbar_bg}; }}
#statusText {{ color: {t.dim}; font-size: 9pt; }}
#statusDot {{ color: {t.dim}; }}

/* ---------- 键位清单行 ---------- */
#listRow {{ background: {t.card}; border-radius: 6px; }}
#listRow:hover {{ background: {t.card_in}; }}
#listRow[sel="true"] {{ background: {t.accent_dim}; }}
#listNum {{
    background: {t.card_in}; border-radius: 4px;
    color: {t.dim}; font-size: 9pt; padding: 2px 4px;
}}
#listNum[sel="true"] {{ background: {t.accent}; color: {t.on_accent}; }}
#listName {{ color: {t.fg}; font-size: 10pt; }}
#listName[sel="true"] {{ color: {t.accent}; }}
#listVal {{ color: {t.dim}; font-size: 9pt; }}
#listVal[sel="true"] {{ color: {t.accent}; }}

/* ---------- 滚动条（深色细条，别用系统默认的浅色）---------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t.border}; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.dim}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {t.border}; border-radius: 5px; min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ---------- 诊断页 ---------- */
#diagText {{
    background: {t.card};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: 8px;
    padding: 8px;
}}

/* ---------- 键位页鼠标图 ---------- */
#mouseMap {{
    background: {t.card};
    border: none;
}}
"""


def apply_theme(app: QtWidgets.QApplication) -> Theme:
    """把全局 QSS 与应用字体挂到 QApplication 上（只做一次，在 run_gui 里调）。"""
    app.setStyleSheet(build_qss(THEME))
    app.setFont(FONT_UI)
    return THEME
