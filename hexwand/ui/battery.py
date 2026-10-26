"""电量卡片与托盘 tooltip 的显示链路（**只显示，不判定在线/离线**）。

从 `window.py` 拆出来的 mixin（`MainWindow` 继承它）。
在线/离线由 `LinkMonitor` 的心跳统一判定，这里读到什么显示什么；离线期间
刻意不读设备，避免把"未连接"覆盖成一个看似存在的连接状态。
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

BATT_SAMPLES: int = 3                # 电量显示刷新时的多采样次数（中位数防毛刺）


class BatteryMixin:
    """依赖主窗口上的：`controller` / `tray` / `theme` / `_link` /
    `_batt_lbl` / `_batt_vbar` / `run_bg` / `_post_to_main`。"""

    def refresh_battery(self) -> None:
        """刷新电量显示 + 同步托盘 tooltip。**可在任意线程调用**。

        HID 读取（3 次采样 + 采样间隔）放到后台线程做：鼠标关机时单次
        get_battery 会走满 3 次重试（约 0.3s），3 次采样叠加能把主线程卡住
        近 1 秒（点托盘"刷新电量"时表现为窗口假死）。结果回主线程落地。
        """
        self.run_bg(self._read_battery_bg)

    def _read_battery_bg(self) -> None:
        """后台线程：多次采样取中位数，过滤偶发的错位响应。

        只负责"显示"，不参与在线/离线判定（状态由 LinkMonitor 心跳统一驱动）。
        纯有线型号（无电池）不读设备，直接显示"有线"。
        """
        if not self.controller.connected:
            # 离线不读：读成功会把"未连接"覆盖成 86% + 无线连接，读失败会覆盖成
            # "无线连接 · 电量未知"——两者都是在宣称一个不存在的连接。
            return
        if not self.controller.dev.has_battery:
            self._post_to_main(self._apply_wired)
            return
        samples: list[int] = []
        for _ in range(BATT_SAMPLES):
            try:
                lvl = self.controller.dev.get_battery()
                if lvl is not None and lvl >= 0:
                    samples.append(lvl)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.03)
        if not samples:
            log.warning("[battery] 读取失败：无有效采样")
            self._post_to_main(self._apply_battery_unknown)
            return
        samples.sort()
        lvl = samples[len(samples) // 2]      # 中位数：压制单次错位
        # get_battery() 已经统一换算成百分比 0-100（单位差异在协议层用
        # battery_max 声明，换算收在 device.get_battery 一处）。这里曾经再除
        # 一次 255——把"雷蛇返回裸 0-255"当成了所有品牌的事实，于是 HID++ 直接
        # 给的 85% 被算成 33%，低电量告警也跟着误报。
        pct = max(0, min(100, int(round(lvl))))
        self._post_to_main(lambda: self._apply_battery(pct))

    def _set_conn_state(self, label: str, tip: str | None = None) -> None:
        """连接方式文案的唯一写入点：只喂托盘 tooltip（界面上不再显示）。

        界面上原本在电量行还显示"无线连接/未连接"小字——与顶栏状态重复
        （用户 2026-09-17 反馈多余，已删）。这里不再额外存一份状态：托盘 tooltip
        每次都用传进来的 `tip/label` 现算，留个字段没人读只会变成误导。
        """
        self.tray.set_tooltip(f"HexWand · {tip or label}")

    def _apply_batt_text(self, text: str, color: str) -> None:
        try:
            self._batt_lbl.setText(text)
            self._batt_lbl.setStyleSheet(f"color: {color};")
            self._batt_vbar.set_text(text)
        except (AttributeError, RuntimeError):
            pass

    def _apply_wired(self) -> None:
        """GUI 线程：有线鼠标的电量卡片显示。

        有线型号不上报电量 —— 插着线就是供电状态，用户要求默认显示 100%
        （2026-09-17），不要再显示一个没有数值的"有线"。
        """
        if self._link != "online":
            return
        self._apply_batt_text("100%", self.theme.fg)
        # 托盘 tooltip 不带百分比：有线没有"电量"概念，显示"有线连接"即可
        # （无线才带 —— _apply_battery 里的 tip 有数字）。用户 2026-09-17 指正。
        self._set_conn_state("有线连接")

    def _apply_battery(self, pct: int) -> None:
        """GUI 线程：落地电量显示（控件只能在 GUI 线程改）。"""
        if self._link != "online":
            return          # 采样期间掉线：别让晚到的结果把"未连接"盖掉
        self._apply_batt_text(f"{pct}%", self.theme.fg)
        self._set_conn_state("无线连接", tip=f"无线连接({pct}%)")

    def _apply_battery_unknown(self) -> None:
        """GUI 线程：一次有效采样都没读到。

        界面和悬停必须一起改，否则悬停会一直挂着上一次成功的电量，
        出现"界面 -- / 悬停 86%"。
        """
        if self._link != "online":
            return
        # "--" 是"无值"占位，用暗色；否则上次成功读数的亮白会一直挂在占位符上
        self._apply_batt_text("--", self.theme.dim)
        self._set_conn_state("无线连接 · 电量未知", tip="无线连接(电量未知)")

    def _batt_display(self) -> None:
        """周期电量显示刷新（GUI 线程入口 → 后台读取）。只改显示，不影响状态。"""
        try:
            self.refresh_battery()
        except Exception:  # noqa: BLE001
            pass

