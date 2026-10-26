"""Controller：UI 与协议层之间的胶水层。

职责：
- 持有 MouseDevice 与 AppConfig 实例
- 把探测结果回写配置缓存（下次启动跳过 probe，省 ~0.3s + 8 次 HID 交互）
- 提供 DPI / 轮询率 / 电源参数的"写设备 + 存配置"组合操作

注意：`connect()` **不**写 `self.connected`——只代表 dongle 链路可达。
"鼠标在线"由 UI 状态机（LinkMonitor 心跳）经 `_set_link("online")` 统一写入，
避免 dongle 在但鼠标关机时被误判为在线。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from ..core import config as cfgmod
from .backends import scan_known_devices
from .catalog import DEVICE_DB
from .device import MouseDevice
from .frame import POLL_TO_VAL
from ..core.config import AppConfig

log = logging.getLogger(__name__)


class Controller:
    """设备层 + 配置的胶水层，供 UI 调用。

    「我的鼠标」的两条维护路径：构造时 autodetect（首启/换设备自动认领第一只
    已收录鼠标）；设备页刷新时 note_detected（扫描见到的已收录鼠标并入记录）。
    """

    def __init__(self, cfg: Optional[AppConfig] = None) -> None:
        self.cfg = cfg or cfgmod.load()
        self.dev = MouseDevice(
            pid=self.cfg.pid,
            tx_id=self.cfg.tx_id,
            report_id=self.cfg.report_id,
            simulation=self.cfg.simulation,
        )
        # 镜像当前在线状态（由 UI 状态机维护；本类只读不写）
        self.connected: bool = False
        # 设备序号：每次 switch_device 递增。UI 的写操作是"主线程排队 → 后台线程
        # 执行"（run_bg 一个任务一条线程），排队时捕获、执行后比对，就能发现
        # "这条 DPI 写入排的是上一只鼠标"——否则设置会静默落到刚换上的新鼠标上。
        self.dev_epoch: int = 0
        # 改键映射的权威快照，只在写入时整体替换（从不就地改）：低级鼠标钩子在
        # 它的线程上每个鼠标事件都要读一次，就地增删会让那次拷贝抛
        # "dictionary changed size during iteration"。
        self._keymap: dict = dict(self.cfg.keymap)
        self.cfg.keymap = self._keymap
        # cfg.devices 有两个线程的写者：设备页刷新（GUI 线程 note_detected）与
        # 连接成功回写（monitor/切换后台线程经 _cache_probe_result → _remember_device）
        self._devs_lock = threading.Lock()
        self.autodetect()

    # ------------------------------------------------------------------
    # 设备自动识别 / 切换
    # ------------------------------------------------------------------
    def autodetect(self) -> Optional[int]:
        """配置里的 PID 没接入时，改用当前接入的第一只受支持鼠标。

        场景：首次启动（pid=0，没有配置）插着受支持鼠标 → 直接认领它；
        插了新的炼狱蝰蛇（有线）但 config 还是毒蝰的 PID；或毒蝰接收器
        没插、只有有线鼠在。两边都在时**优先用配置里的那只**（最近使用的）。
        返回实际使用的 PID（未变则 None）。
        """
        if self.dev.simulation:
            return None
        try:
            devs = scan_known_devices()
        except Exception:  # noqa: BLE001 — 扫描失败保持原配置
            return None
        known = [d for d in devs if d.known]
        if any(d.pid == self.cfg.pid for d in known):
            return None
        # 按 PID 排序取第一只：SetupDi 枚举顺序不保证跨启动稳定，多鼠并插时
        # "认领哪只"必须可预期
        for d in sorted(known, key=lambda x: x.pid):
            if d.pid in DEVICE_DB:
                self.switch_device(d.pid, save=True)
                return d.pid
        return None

    def switch_device(self, pid: int, save: bool = True) -> None:
        """切换目标鼠标：换 PID 并清掉旧设备的 (tx_id, report_id) 缓存。

        tx_id 是每台设备各自探测出来的（毒蝰 0x1F、炼狱蝰蛇 0xFF），
        带着旧值去连新设备会先失败一轮校验，直接清掉让它重新探测最稳。
        """
        self.cfg.pid = pid
        self.cfg.tx_id = None
        # 写回默认 2 而不是 None：report_id 字段声明是 int，落盘 null 再读回来
        # 又会被清洗成 2，绕一圈没有区别（MouseDevice 里 None 即 2，probe 也是
        # 先试 2 再试 0）。真正需要清掉的是 tx_id。
        self.cfg.report_id = 2
        self._remember_device(pid)
        if save:
            self._save_cfg()
        # 换对象前先关掉旧句柄：不关就是白泄漏一个内核句柄，而且旧设备一旦被
        # 丢弃就再没人能 close 它（UI 里所有引用都走 self.controller.dev）。
        try:
            self.dev.close()
        except Exception:  # noqa: BLE001
            log.debug("close old device failed", exc_info=True)
        self.dev = MouseDevice(
            pid=pid, tx_id=self.cfg.tx_id, report_id=self.cfg.report_id,
            simulation=self.cfg.simulation)
        self.dev_epoch += 1
        self.connected = False

    # ------------------------------------------------------------------
    # 连接（注意：本方法不写 self.connected）
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """建立/重开 HID 通道（dongle 可达即 True）。"""
        ok = self.dev.connect()
        if ok:
            self._cache_probe_result()
        return ok

    def _cache_probe_result(self) -> None:
        """回写探测结果到 config：下次启动直接复用，跳过 probe。

        **只缓存经真机验证过的结果**。鼠标关机时 dongle 仍可枚举成功，探测必然
        失败，此时 device 会兜底填 tx_id=0x1F；若把这个猜测写进配置，下次启动
        会因为 tx_id 非空而跳过探测，一旦真机 tx_id 不是该值就永久连不上。
        """
        if not self.dev.tx_id_verified:
            return
        changed = False
        # 判"有没有 tx_id"必须显式比 None：tx_id=0 是合法候选，用真值判断会把
        # 这类型号永远排除在缓存之外（每次启动都重跑一轮 probe）。
        if self.dev.tx_id is not None and self.cfg.tx_id != self.dev.tx_id:
            self.cfg.tx_id = self.dev.tx_id
            changed = True
        if self.cfg.report_id != self.dev.report_id:
            self.cfg.report_id = self.dev.report_id
            changed = True
        if self._remember_device(self.dev.pid or self.cfg.pid):
            changed = True
        if changed:
            self._save_cfg()

    def note_detected(self, pids) -> None:
        """扫描**看到**的已收录鼠标 → 并入「我的鼠标」记录（可多只）。

        第二只受支持的鼠标插上来，就该从「其它已知型号」挪进「我的鼠标」，
        哪怕从没切换过——"我的"表达的是"这些鼠标在我手上"，不是"我连过"。
        与 _remember_device 相反，被动检测**追加到队尾**：仅仅插上一只新鼠标
        不该打乱用户真正在用的那批的"最近在前"顺序。被 8 上限截掉的**不算变化**
        （否则每次进设备页都白落一次盘）。
        """
        with self._devs_lock:
            old = list(self.cfg.devices)
            new = old[:]
            for pid in sorted(pids):
                if pid and pid not in new and pid in DEVICE_DB:
                    new.append(pid)
            new = new[:8]
            if new != old:
                self.cfg.devices = new
                self._save_cfg()

    def _remember_device(self, pid: Optional[int]) -> bool:
        """把 pid 记到「我的鼠标」使用记录最前（去重、上限 8）。返回是否有变化。

        两个调用点：连接成功并验证过 tx_id 之后（_cache_probe_result），以及
        用户在「设备」页里主动点「切换」（switch_device）。后者即使还没连上
        也记——那是用户明确表达"这只鼠标是我的"，比等一次成功连接更符合直觉。
        设备页据此区分「我的鼠标」和「其它已知型号」。
        """
        if not pid or pid not in DEVICE_DB:
            return False
        with self._devs_lock:
            devs = [p for p in self.cfg.devices if p != pid]
            devs.insert(0, pid)
            devs = devs[:8]
            if devs == self.cfg.devices:
                return False
            self.cfg.devices = devs
            return True

    def _save_cfg(self) -> str | None:
        """保存配置。成功返回 None，失败返回原因字符串（**不外抛**）。

        不外抛是对的：设备操作已经生效了，落盘失败不该被上层误报成"设置失败"。
        但**静默吞掉**是错的——界面会说"轮询率已设为 1000 Hz"，config.json 里
        还是旧值，重启就打回原形。本项目常年待在 OneDrive 同步目录里，
        `config.save` 自己的注释就写明 `os.replace` 会被同步引擎占住而抛
        PermissionError，这是高概率事件，不是理论风险。所以把原因回给调用方，
        由界面如实显示"已生效但没存上"。
        """
        try:
            cfgmod.save(self.cfg)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("config save failed", exc_info=True)
            return f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # 写设备 + 存配置
    # ------------------------------------------------------------------
    def set_dpi_stages(self, stages: list[int]) -> tuple[list[int], str | None]:
        """写 5 档 DPI 到设备并保存到配置。

        返回 `(设备真正接受的档位, 落盘失败原因或 None)`。
        必须用第一个返回值刷新界面与缓存：越界值在设备层被钳制过（只记一条
        warning、不抛错），留住用户输入的原值就会显示一个鼠标根本存不下的
        数字，下次点「档 N」还会按那个错值下发当前 DPI。
        """
        written = self.dev.set_dpi_stages([int(s) for s in stages[:5]])
        self.cfg.stages = list(written)
        return written, self._save_cfg()

    def set_poll(self, rate: int) -> str | None:
        """设置轮询率：rate in POLL_TO_VAL 用 set_poll，否则 set_poll_v2。

        返回落盘失败原因（设备已生效）或 None。
        """
        rate = int(rate)
        if rate in POLL_TO_VAL:
            self.dev.set_poll(rate)
        else:
            self.dev.set_poll_v2(rate)
        self.cfg.poll_rate = rate
        return self._save_cfg()

    def get_keymap(self) -> dict:
        """全局按键映射：{button_id: action}。

        一份映射对所有鼠标生效（包括未收录型号）——低级钩子在系统层只认
        标准按键，与品牌/型号/是否在线无关；本映射表实时被钩子读取。

        返回的是**共享快照对象，请勿就地修改**。低级鼠标钩子在自己的线程上
        每个鼠标事件都要取一次映射：以前这里是 `dict(self.cfg.keymap)` 现拷，
        与主线程的就地增删并发会抛 "dictionary changed size during iteration"，
        而那异常被钩子回调整个吞掉，表现为"这一次改键莫名没生效"。写入一律
        整体替换（见 set_button_action / clear_button_action），所以读到的
        dict 永不被就地改动，属性赋值本身在 CPython 下是原子的。
        """
        return self._keymap

    def set_button_action(self, button_id: str, action: dict) -> str | None:
        """设置单个按键的映射动作（点键即改，覆盖旧值，全局生效）。

        返回落盘失败原因或 None。
        """
        self._keymap = {**self._keymap, button_id: dict(action)}
        self.cfg.keymap = self._keymap
        return self._save_cfg()

    def clear_button_action(self, button_id: str) -> str | None:
        """清除单个按键的映射（恢复该键出厂功能）。有删除才存盘。

        返回**落盘失败原因**或 None（与 `set_button_action` 同约定）。原来返回
        bool 并把 `_save_cfg()` 的失败原因丢掉 —— 界面会说"已恢复默认"，可配置
        目录在 OneDrive 同步盘里、`os.replace` 偶发 PermissionError 是常事，
        重启后映射又回来了（2026-09-17 审查发现）。键本来就不在表里 = 无事可做，
        算成功（返回 None）。
        """
        if button_id not in self._keymap:
            return None
        km = dict(self._keymap)
        del km[button_id]
        self._keymap = km
        self.cfg.keymap = km
        return self._save_cfg()

    # ------------------------------------------------------------------
    # 读设备（容错：失败返回 None）
    # ------------------------------------------------------------------
    def get_dpi(self) -> Optional[tuple[int, int]]:
        try:
            return self.dev.get_dpi()
        except Exception:  # noqa: BLE001
            return None

    def get_poll(self) -> Optional[int]:
        try:
            return self.dev.get_poll()
        except Exception:  # noqa: BLE001
            return None

    def get_info(self) -> str:
        """诊断详情文本（进 startup.log）：未连接 / 在线时显示设备名+固件+事务ID+PID。"""
        if not self.connected:
            return "未连接设备" + ("（模拟模式）" if self.dev.simulation else "")
        try:
            fw = self.dev.get_firmware()
        except Exception:  # noqa: BLE001
            fw = "?"
        sim = " [模拟]" if self.dev.simulation else ""
        return (f"{self.dev.name}{sim}\n"
                f"固件: {fw}  |  事务ID: 0x{self.dev.tx_id or 0:02X}  |  "
                f"PID: 0x{self.dev.pid or 0:04X}")
