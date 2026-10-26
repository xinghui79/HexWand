"""后台状态机：在线/离线心跳判定 + DPI 物理切档同步 + 电量节拍。

设计目标：与 GUI 工具包完全解耦、行为可独立测试（本模块一行都不 import
界面相关的东西，换界面时不用动）。

副作用（探测设备、读 DPI）通过注入回调完成；事件（在线/离线切换、
物理切档、电量节拍）通过注入回调上报。UI 侧只负责把回调调度到 GUI 线程
（见 ui/window.py 的 _post_to_main）。

健壮性：主循环每个 tick 都有整体 try/except 兜底——回调之外的任何异常
（如档位表被写入非法值）只记日志、tick 继续，线程绝不静默死亡（线程一死，
心跳/切档同步/电量刷新全部停摆且无任何痕迹）。

状态迁移规则（单一心跳信号，杜绝 dongle 可达即"假连接"的误判）：
- 在线：每 2s 心跳（probe）。连续失败 LOST_HEARTBEATS 次 → 离线（约 6s）。
- 离线：每 3s 端到端探测。连续成功 ONLINE_HEARTBEATS 次 → 在线（鼠标
  开机且稳定才转，避免开机瞬间的假连接反复闪）。
- 句柄失效兜底：离线每 REOPEN_EVERY_PROBES 次探测触发 reopen_hint，
  提醒调用方重开 HID 句柄（dongle 拔/插后句柄可能残留失效）。

在线期间的 DPI 轮询走**独立节拍**（DPI_TICKS_ONLINE）：读一次 DPI 就是一次完整
HID 事务，调慢能省无线鼠标的电，但物理切档到 HUD 弹出之间的延迟会同步变长。
实测 5 tick（1s）时用户明确反馈"延迟太大"，故用 1 tick（0.2s，与基本节拍同速）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 时序常量（以 TICK_S=0.2s 为基本节拍；测试可 monkeypatch 加速）
# ---------------------------------------------------------------------------
TICK_S: float = 0.2              # 基本 tick 间隔
DPI_TICKS_ONLINE: int = 1       # 在线：每 1 tick（0.2s）读一次 DPI，检测物理切档
                                # （调大会降低 HID 频率但 HUD 弹出变慢，见模块 docstring）
PING_TICKS_ONLINE: int = 10     # 在线：每 10 tick（2s）一次心跳
PING_TICKS_OFFLINE: int = 15    # 离线：每 15 tick（3s）一次端到端探测
LOST_HEARTBEATS: int = 3        # 心跳连续失败 3 次 → 判离线（约 6s）
ONLINE_HEARTBEATS: int = 2      # 探测连续成功 2 次 → 判在线（防开机瞬间假连）
REOPEN_EVERY_PROBES: int = 5    # 离线每探测 5 次触发 reopen_hint
BATT_POLL_ROUNDS: int = 150     # 电量节拍：每 150 tick（30s）提示刷新一次

_STOP_JOIN_TIMEOUT: float = 0.3  # stop() 后等待线程结束的超时（秒）


def closest_stage(stages: list[int], x: int) -> int:
    """与 x 最接近的档位下标（可离线单测）。

    状态机（读到的 DPI → 档位下标）与主窗口（HUD 高亮那一档）共用这一份：
    两边各写一遍时，任一边改了判据就会出现"浮窗说第 3 档、面板高亮第 4 档"
    （2026-09-17 审查：window._closest_stage 曾是逐行复制品）。
    """
    best_i, best = 0, 10 ** 9
    for i, s in enumerate(stages):
        if abs(s - x) < best:
            best, best_i = abs(s - x), i
    return best_i


# ---------------------------------------------------------------------------
# 回调类型（Protocol：结构性类型，方便测试用 mock）
# ---------------------------------------------------------------------------
class _ProbeFn(Protocol):
    def __call__(self) -> bool: ...


class _ReadDpiFn(Protocol):
    def __call__(self) -> Optional[tuple[int, int]]: ...


class _OnLinkFn(Protocol):
    def __call__(self, target: str) -> None: ...


class _OnDpiFn(Protocol):
    def __call__(self, idx: int, x: int) -> None: ...


class _OnBatteryTickFn(Protocol):
    def __call__(self) -> None: ...


class _ReopenHintFn(Protocol):
    def __call__(self) -> None: ...


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
class LinkMonitor:
    """后台常驻状态机（单线程、daemon）。

    线程模型：所有回调都在本模块线程内直接调用——UI 侧务必把回调调度回
    GUI 主线程（控件不是线程安全的；Qt 下从子线程碰控件是未定义行为）。

    生命周期：start() 启动线程，stop() 置位并 join（超时上限
    `_STOP_JOIN_TIMEOUT`）；start() 幂等——join 超时后线程可能还在收尾，
    此时清位并复用那个活线程，绝不再起第二个，也绝不留下"没人跑"的空档。
    """

    def __init__(
        self,
        probe: _ProbeFn,
        read_dpi: _ReadDpiFn,
        on_link: _OnLinkFn,
        on_dpi: _OnDpiFn,
        on_battery_tick: _OnBatteryTickFn,
        stages: list[int],
        reopen_hint: Optional[_ReopenHintFn] = None,
    ) -> None:
        self._probe = probe
        self._read_dpi = read_dpi
        self._on_link = on_link
        self._on_dpi = on_dpi
        self._on_batt = on_battery_tick
        # 直接持有调用方的 list 引用（**不拷贝**）：UI 改档位是就地写
        # self.stages[i]，若在这里拷贝一份，档位表会悄悄过期 —— 曾导致物理
        # 切档算出的索引与 UI 实际档位错位。真要整体换一张表就重建 LinkMonitor。
        self._stages = stages
        self._reopen_hint = reopen_hint or (lambda: None)
        self._stop = threading.Event()
        self._resync = threading.Event()
        self._last_dpi: Optional[int] = None   # 上次已同步的 DPI x（seed_dpi 可外部灌）
        # 基线是否已建立。resync() 会作废它（上一只鼠标的 DPI 与新设备无关），
        # 作废后第一次成功读取仍照常上报一次，让 UI 的档位高亮跟上新鼠标。
        self._dpi_seeded = False
        self._thread: Optional[threading.Thread] = None
        # 生命周期锁：线程"看到 _stop 置位就退出"的判定与 start() 的
        # "清位 + 判活复用"必须在同一把锁下互斥。否则存在这样的窗口——
        # 旧线程已越过循环条件判定、正在收尾，is_alive() 却仍为 True，
        # start() 便"复用"了一个注定要死的线程，之后心跳/切档/电量节拍
        # 静默停摆到进程结束（2026-09-18 审查发现）。
        self._life = threading.Lock()
        self._exited = True       # 当前线程是否已在 _life 锁里登记"我要退出了"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动状态机线程（幂等：线程还在跑就复用它，不重复起线程）。

        **清位必须在判活之前**：`stop()` 的 join 只等 `_STOP_JOIN_TIMEOUT`
        （0.3s），而一轮 tick 可能卡在完整 HID 事务里（心跳 ≈0.24s、
        `reopen_hint` 里整次 `connect()` 可达 2s），join 超时返回时线程还活着。
        这时若因为"看起来仍在运行"就直接 return，`_stop` 会保持置位，那个旧
        线程跑完当轮就自行退出——本对象此后再没有线程，心跳 / 物理切档同步 /
        电量刷新静默停摆到进程结束，而且一行日志都没有。切换设备走的正是
        stop()→start()，所以这条路径不是理论风险。
        """
        with self._life:
            self._stop.clear()
            thr = self._thread
            if thr is not None and not self._exited and thr.is_alive():
                log.info("monitor 线程仍在收尾，清位后复用现有线程")
                return
            self._exited = False
            self._thread = threading.Thread(
                target=self._loop, name="link-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """请求退出并等待线程结束（超时 _STOP_JOIN_TIMEOUT）。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=_STOP_JOIN_TIMEOUT)

    def resync(self) -> None:
        """要求状态机立刻重新判定并播报一次在线状态。

        on_link 只在迁移沿上触发；调用方（切换设备）会把 UI 退回未连接，
        不重播就永远等不到下一次 "online"。这里**不是**简单重播旧结果——
        那只鼠标留下的 online=True 会让新设备被误判成已连接（要等 3 次心跳
        失败≈6s 才纠正），所以直接重新探测一次，按当下结果落地。

        顺带作废 DPI 基线：上一只鼠标的 DPI 值对新设备毫无意义，留着它会让
        新鼠标的第一次轮询被当成"和上次不同"→ 播报的其实是跨设备的错位。
        作废后第一次成功读取照常上报一次（UI 靠它把档位高亮跟上新鼠标），
        由调用方决定这一轮只同步数值、不弹切档提示。
        """
        self._last_dpi = None
        self._dpi_seeded = False
        self._resync.set()

    def seed_dpi(self, x: Optional[int]) -> None:
        """把"设备当前 DPI"设为已同步过的基线。

        转在线时由 UI 读到真值后调用。不灌的话，上线后第一轮 DPI 轮询一定和
        初值 None 不同 → 误报一次"检测到物理按钮切档"并弹 HUD。
        传 None（没读到真值）等于"基线仍未建立"，别把它当成一个真实读数。
        """
        self._last_dpi = x
        self._dpi_seeded = x is not None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        tick_state = {
            # 从阈值起算：第一个 tick 就探测一次，之后仍是每 PING_TICKS_OFFLINE
            # 一轮。从 0 起算的话，插着好的鼠标也要等 3s 才第一次探测、再等
            # ONLINE_HEARTBEATS 才转在线（约 6s 显示"未连接"），而切换设备走
            # resync() 是即时的——两条路径手感不一致，启动那 6s 看着像没认出设备。
            "tick": PING_TICKS_OFFLINE,
            "dpi_tick": 0,      # DPI 轮询独立计数（见 DPI_TICKS_ONLINE）
            "hb_ok": 0,         # 心跳连续成功计数（离线探测用）
            "hb_fail": 0,       # 心跳连续失败计数（在线用）
            "probe_cnt": 0,     # 离线探测次数（触发 reopen_hint 用）
            "batt_ticks": 0,
            "online": False,
        }
        while True:
            with self._life:
                if self._stop.is_set():
                    # 退出登记与 start() 的"清位+判活"同锁互斥：start() 若已
                    # 抢先把位清了，这里就会判"未停"继续跑；否则本行 return
                    # 之后 start() 一定能看到 _exited，不会再复用垂死线程。
                    self._exited = True
                    return
            # 整体兜底：任何回调外的异常（如 stages 被写入非法值导致
            # closest_stage 抛错）都不允许击穿这里——线程死了，心跳/切档
            # 同步/电量刷新就全部静默停摆，且线程死亡不留任何日志。
            try:
                self._tick_once(tick_state)
            except Exception:  # noqa: BLE001
                log.exception("monitor tick crashed; continuing")
            time.sleep(TICK_S)

    def _tick_once(self, st: dict) -> None:
        """单个 tick 的状态机逻辑；计数器装在 st 字典里跨 tick 存续。"""
        st["tick"] += 1
        tick = st["tick"]
        online = st["online"]

        if self._resync.is_set():
            # 调用方（切换设备）把 UI 退回未连接后要求重新播报。这里重探一次
            # 而不是重播旧值：旧鼠标留下的 online=True 会让新设备被误判已连接。
            self._resync.clear()
            online = self._safe_probe()
            st["hb_ok"] = st["hb_fail"] = 0
            st["probe_cnt"] = st["batt_ticks"] = 0   # 跨周期不复位会让 reopen/电量节奏错拍
            st["tick"] = 0
            tick = 0
            st["online"] = online
            self._safe_call(self._on_link, "online" if online else "offline")

        if online:
            # ---- 在线：DPI 轮询（独立节拍，当前 1 tick = 0.2s）----
            # 每次读 DPI 都是一整轮 HID 收发；这里调到 1 tick 是因为用户明确
            # 反馈过物理切档到 HUD 弹出之间延迟太大，调慢会等得明显。
            st["dpi_tick"] += 1
            if st["dpi_tick"] >= DPI_TICKS_ONLINE:
                st["dpi_tick"] = 0
                x: Optional[int] = None
                try:
                    r = self._read_dpi()
                    if r is not None:
                        x = r[0]
                except Exception:  # noqa: BLE001 — 读失败按无值处理
                    pass
                if x is not None and (x != self._last_dpi
                                      or not self._dpi_seeded):
                    self._last_dpi = x
                    self._dpi_seeded = True
                    self._safe_call(self._on_dpi,
                                    self.closest_stage(x), x)

            # 心跳：每 2s 一次
            if tick >= PING_TICKS_ONLINE:
                st["tick"] = 0
                if self._safe_probe():
                    st["hb_fail"] = 0
                else:
                    st["hb_fail"] += 1
                    if st["hb_fail"] >= LOST_HEARTBEATS:
                        st["hb_fail"] = 0
                        st["online"] = False
                        self._safe_call(self._on_link, "offline")
        else:
            # ---- 离线：每 3s 端到端探测 ----
            st["dpi_tick"] = 0   # 重新上线后从头计，避免立刻多打一轮 DPI
            if tick >= PING_TICKS_OFFLINE:
                st["tick"] = 0
                st["probe_cnt"] += 1
                # 连续多次探测失败可能句柄已失效（dongle 被拔/重插但
                # 句柄残留非 None）→ 定期提示调用方强制 connect 重开
                if st["probe_cnt"] % REOPEN_EVERY_PROBES == 0:
                    self._safe_call(self._reopen_hint)
                if self._safe_probe():
                    st["hb_ok"] += 1
                    if st["hb_ok"] >= ONLINE_HEARTBEATS:
                        st["hb_ok"] = 0
                        st["online"] = True
                        self._safe_call(self._on_link, "online")
                else:
                    st["hb_ok"] = 0

        # ---- 电量节拍（在线期间，30s 周期，只提示显示刷新）----
        if st["online"]:
            st["batt_ticks"] += 1
            if st["batt_ticks"] >= BATT_POLL_ROUNDS:
                st["batt_ticks"] = 0
                self._safe_call(self._on_batt)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def closest_stage(self, x: int) -> int:
        """返回与 x 最接近的档位下标（可离线单测）。"""
        return closest_stage(self._stages, x)

    def _safe_probe(self) -> bool:
        try:
            return bool(self._probe())
        except Exception as e:  # noqa: BLE001
            log.debug("probe failed: %s", e)
            return False

    def _safe_call(self, fn: Callable, *args) -> None:
        """后台线程调外部回调的兜底：异常一律吞（不允许击穿到 stderr）。"""
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.debug("callback %s failed", getattr(fn, "__name__", fn),
                      exc_info=True)
