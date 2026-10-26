"""窗口尺寸：宽度固定、高度取四页最大值，并锁住防漂移。

从 `window.py` 拆出来的 mixin（`MainWindow` 继承它）。搬家未改逻辑，只在
文件头补了模块说明；方法本身的坑与理由都保留在原注释里。

对外还挂着三个常量和一个量测函数（`preview.py` 直接 import
`_measure_page_h`，`window.py` 也经本模块重新导出它们）。
"""

from __future__ import annotations

import logging

from .qt import QtCore, QtGui, QtWidgets

log = logging.getLogger(__name__)

# 窗口尺寸（**逻辑像素**，Qt 自动按系统缩放换算；原版这几个数都还乘过 _ui_scale）
WIN_W: int = 700
WIN_H_MAIN: int = 488
WIN_H_MIN: int = 448


def _measure_page_h(page: QtWidgets.QWidget | None) -> int:
    """量一个页面的内容高度。**必须先把它的布局激活再量。**

    坑（2026-09-16 踩到，用户反馈"页面 1 和 2 高度还是不一致"）：
    `QStackedWidget` 里**非当前页**的布局是惰性的、尚未 activate，此时
    `sizeHint()` 会给一个偏大的估算值（未压缩的边距/间距全加进去）；等它变成
    当前页、真正布局完之后，同一个页面又量出更小的值。于是"取两页较大者"算出来
    的窗口高度在切页后会变 —— 实测主面板阶段 676、键位页阶段 649，差 27px，
    看起来就是两个页面高度不一样。先 activate 一次，两次就量到同一个数了。
    """
    if page is None:
        return 0
    lay = page.layout()
    if lay is not None:
        lay.activate()
    return page.sizeHint().height()


class WindowSizeMixin:
    """窗口几何：只依赖 `self._pages` / `self._head` / `self._navrow` /
    `self._statusbar` / `self._win_h_*` 这几个约定属性。"""

    def _apply_page_size(self) -> None:
        """窗口尺寸：宽度固定，高度**四个页面取较大者 —— 不随页面切换变化**。

        为什么不按页分别算（原实现）：切页会让窗口高矮跳 185px，一来视觉突兀，
        二来实测出现过"从键位页回主面板时窗口还没缩回去、下半截残留着键位页内容"
        的中间态（用户 2026-09-16 截图抓到）。高度恒定就根本没有这个中间态，
        也省掉"页面切换 → 发一次 resize → 重排"这条链路。

        **坑（早先踩过，别再踩）**：不能直接取 `self.layout().sizeHint().height()`。
        QStackedWidget 会把它算成所有页面的最大值 —— 那正好是"每页各自算"的反面，
        也就是现在**想要**的行为；但依赖它太隐晦，所以这里显式对每个页面各量一次
        取 max（见 `_target_win_h`）。

        "设备"和"诊断"两页的 `sizeHint` 是**固定值**（见 pages 模块 docstring），
        不随内容增长 —— 它们的文本/列表可能很长，若参与竞争会把整窗撑高。

        代价是主面板会高出内容所需：富余高度交给页面布局吸收
        （`_build_main_page` 的 inset grouped 容器里由 DPI 行 `stretch 1`
        吃富余、整行长高），而不是底部留一片空白。
        """
        self._set_window_size(self._target_win_h())

    def _set_window_size(self, h: int) -> None:
        """设窗口尺寸，并**显式保持左上角不动**。

        坑（用户 2026-09-17 反馈"切页窗口大小没变，但整体上移了一点"）：
        本机实测切页时 `pos.y` 从 129 漂到 102（上移 27px），之后不回位。
        Qt/Windows 调整 top-level 窗口几何时是以**含边框的 frame** 为基准的，
        哪怕最终宽度高度都没变，反复 `setFixedSize` 也会把窗口挪一下。
        两条对策一起上：
          1. 尺寸没变就干脆不动（最常见的切页情形）；
          2. 真的要改就先把 y 记下来，改完挪回去（最小/最大化时不插手）。
        """
        if self.width() == WIN_W and self.height() == h:
            return
        # 记下顶边：Qt/Windows 是在**之后**（下一轮事件循环）才真正移动窗口的，
        # 所以这里只记，恢复放到 singleShot(0) 里做（见 `_restore_top`）。
        if self.isVisible() and not self.isMinimized() \
                and not self.isMaximized():
            self._keep_y = self.geometry().y()
        self.setFixedSize(WIN_W, h)
        QtCore.QTimer.singleShot(0, self._restore_top)

    def _restore_top(self) -> None:
        """把 resize 之后被平台层挪走的窗口挪回原顶边（见 `_set_window_size`）。"""
        y = self._keep_y
        if y is None:
            return
        try:
            if (self.isVisible() and not self.isMinimized()
                    and not self.isMaximized()
                    and self.geometry().y() != y):
                self.move(self.geometry().x(), y)
        except RuntimeError:      # 收尾竞态：C++ 对象已销毁
            pass

    def _target_win_h(self) -> int:
        """chrome（顶栏+导航+状态栏）+ 各页面里最高的那个内容高度。

        **锁定机制**（用户 2026-09-17 反馈"切页后窗口整体上移了一点"）：
        切页当下量到的高度是**偏小的**（布局惰性，实测 627 vs 稳定后 654），
        于是切页会真实 resize 一次、下一轮复核又 resize 回来 —— Qt/Windows 调整
        top-level 几何时以**含边框的 frame** 为基准，这一来一回就把窗口挪走
        （实测上移 27px，正好是两次高度的差；而且 `setFixedSize` 同步返回时
        Qt 报告的 y 还没变，移动是之后才发生的，所以同步里校正无效）。
        窗口高度本来就不该随页面变化，所以**量到连续两次相同就锁住**，
        之后一律复用：既消除抖动，也省掉每次切页的重算。
        """
        if self._win_h_locked and self._screen_fingerprint() == self._win_h_fp:
            return self._win_h_locked
        lay = self.layout()
        if lay is not None:
            lay.activate()
        heights = ({k: _measure_page_h(p) for k, p in self._pages.items()}
                   if getattr(self, "_pages", None) else {})
        page_h = max(heights.values()) if heights else 0
        if page_h <= 1:                 # pragma: no cover — 量测失败时退回默认
            page_h = WIN_H_MAIN - self._chrome_h()
        h = max(int(self._chrome_h() + page_h), WIN_H_MIN)
        log.debug("win size: chrome=%d %s -> h=%d",
                  self._chrome_h(), heights, h)
        if self._win_h_prev == h:
            self._win_h_locked = h      # 连续两次一致 = 布局已稳定，锁定
            self._win_h_fp = self._screen_fingerprint()   # 连同当时的屏幕环境
        self._win_h_prev = h
        return h

    def _screen_fingerprint(self) -> tuple:
        """当前屏幕环境指纹：分辨率 + 缩放比 + 逻辑 DPI。

        窗口高度一旦锁定就不再重算（见 `_target_win_h` 的说明），但锁定值只对
        "当时的屏幕环境"成立：换显示器、改系统缩放、改字体大小都会让内容高度
        变化，锁着旧值就会把页面底部切掉（2026-09-17 审查发现锁定**没有任何
        失效条件**）。所以量测前先比指纹，变了就作废重来。
        """
        try:
            # 优先取窗口**当前所在**的屏幕：多显示器下主/副屏的缩放与 DPI 可以
            # 不同，只看主屏的话把窗口拖到副屏后锁定值不会失效，页面底部会被裁。
            scr = self.screen()
            if scr is None:
                scr = QtGui.QGuiApplication.primaryScreen()
            if scr is None:
                return ()
            g = scr.geometry()
            return (g.width(), g.height(),
                    round(scr.devicePixelRatio(), 3),
                    round(scr.logicalDotsPerInch(), 2))
        except Exception:  # noqa: BLE001 — 取不到就当"环境未变"
            return ()

    def _chrome_h(self) -> int:
        """顶栏 + 导航行 + 状态栏的自然高度之和。

        用 sizeHint 而不是 height()：后者是**当前**高度，窗口还没显示/还没布局
        时是 0 或默认值，会让算出来的窗口高度忽大忽小。
        """
        total = 0
        for attr in ("_head", "_navrow", "_statusbar"):
            w = getattr(self, attr, None)
            if w is not None:
                total += w.sizeHint().height()
        return total


    def _apply_page_size_safe(self) -> None:
        """切页面/唤窗口后重算尺寸，并在事件循环的**下一轮再算一次**。

        包一层 try：收尾竞态（控件已销毁）不该把异常抛进事件循环。
        为什么要再算一次：Qt 的布局是惰性的，切页当下量到的高度是"尚未完成
        布局"的值（本机实测 remap 页 496，稳定后 523 → 窗口 649 vs 676）。
        不校正的话窗口就停在小值上，两个页面又变成不一样高——这正是用户反复
        看到的现象。配合同一轮内的即时计算，切换时不会出现可见的跳动。
        """
        try:
            self._apply_page_size()
        except RuntimeError:
            return
        QtCore.QTimer.singleShot(0, self._apply_page_size_later)

    def _apply_page_size_later(self) -> None:
        """布局稳定后的复核（见 `_apply_page_size_safe`）。"""
        try:
            self._apply_page_size()
        except RuntimeError:
            pass

