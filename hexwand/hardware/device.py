"""MouseDevice：设备连接、(report_id, tx_id) 探测与高层控制 API。

本类不含任何型号差异字节：各鼠标的私有协议（tx_id、参数布局、能力开关）
全部在 protocols.py（一个文件收齐所有型号，每鼠标一个类），这里只按
self.proto 的方法收发。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any

from .backends import HidDeviceInfo, RustHidBackend, get_backend
from .catalog import DEVICE_DB, protocol_for
from .frame import (CLASS_DEVICE, CLASS_DPI, CLASS_POWER,
                    CMD_GET_BATTERY, CMD_GET_DPI_XY,
                    CMD_GET_FW, CMD_GET_POLL, CMD_SET_DPI_STAGES,
                    CMD_SET_DPI_XY, CMD_SET_IDLE_TIMEOUT,
                    CMD_SET_LOW_BATTERY, CMD_SET_POLL, CMD_SET_POLL_V2,
                    HIDPP_LONG_LEN, HIDPP_SHORT_LEN, REPORT_LEN, VID)
from .protocols import all_vids

log = logging.getLogger(__name__)


class MouseDevice:
    """单台鼠标的 HID 控制通道（线程安全：内部锁串行化命令）。

    品牌/型号差异全部在 self.proto（protocols.py），本类与之解耦。"""

    def __init__(self, pid: int | None = None, path: str | None = None,
                 tx_id: int | None = None, report_id: int | None = None,
                 simulation: bool = False) -> None:
        self.pid = pid
        self.path = path
        self.tx_id = tx_id
        self.simulation = simulation
        self.handle: Any = None
        self.handle_backend: RustHidBackend | None = None
        self.info: HidDeviceInfo | None = None
        self.name = "未知设备"
        # 串行化 HID 命令，避免轮询/写操作并发冲突。必须是 RLock：connect()
        # 整程持锁，内部 _adopt→probe→_send 会再进同一把锁。
        self._lock = threading.RLock()
        self.report_id = report_id if report_id is not None else 2
        self.feature_len: int | None = None  # 设备 Feature Report 长度(含 report id)，诊断用
        # 该 PID 的私有协议（protocols.py：tx_id 候选/兜底、参数编解码、能力开关）。
        # PID 未收录时为 None（没有通用兜底——未知型号拒绝收发，见 _require_proto）；
        # MouseDevice 创建后 pid 不再变。
        # for_connection()：协议实例是模块级单例，连接期状态（HID++ 协商出的
        # feature index、板载配置读缓存）必须每台一份，否则两只同型号鼠标会互相
        # 串用对方的协商结果与缓存。
        self.proto = protocol_for(pid)
        if self.proto is not None:
            self.proto = self.proto.for_connection()
        # (report_id, tx_id) 是否经过真机验证（探测成功）。只有验证过的组合才允许
        # 写进配置缓存——鼠标关机时探测必然失败，此时的兜底值不能当成结论持久化。
        # 也用作"本次连接要不要重新校验/探测 tx_id"的判据（见 _adopt）。
        self.tx_id_verified: bool = False
        # 连接类错误的去重键：重开以 3s 节拍跑，同一条错误刷屏会把线索埋掉
        self._last_err_key: str = ""

    # ---------- 型号能力（来自 DEVICE_DB；未收录 PID 无协议、拒绝收发） ----------
    @property
    def dbinfo(self):
        return DEVICE_DB.get(self.pid)

    def _warn_once(self, kind: str, msg: str) -> None:
        """同类错误**只在内容变化时**记一行 warning（其余降为 debug）。

        永久"未连接"必须留下线索（2026-09-18 审查：这类失败全在 debug 级，
        而日志阈值是 INFO → 零线索）；但连接/协商失败以 3s 节拍重试，
        每条都 warning 会在几分钟里把有用的行冲光。
        """
        key = f"{kind}: {msg}"
        if key != self._last_err_key:
            self._last_err_key = key
            log.warning(key)
        else:
            log.debug(key)

    def _require_proto(self):
        """返回协议条目；PID 未收录协议时明确拒绝（不拿别的协议去碰）。"""
        if self.proto is None:
            raise RuntimeError(
                f"PID 0x{self.pid or 0:04X} 未收录协议（protocols.py / "
                f"catalog.py），无法收发命令")
        return self.proto

    @property
    def has_battery(self) -> bool:
        """是否有电池：炼狱蝰蛇标准版等纯有线型号为 False。未知型号按有电池。"""
        info = self.dbinfo
        return info.battery if info is not None else True

    @property
    def dpi_max(self) -> int:
        info = self.dbinfo
        return info.dpi_max if info is not None else 30000

    # ---------- 连接 ----------
    def connect(self) -> bool:
        """建立/重开 HID 通道。持锁：切换设备的后台 connect 与状态机线程的
        定期重开可能同时进来，并发时后写者会覆盖 self.handle（前一个句柄泄漏，
        另一方还在往被覆盖的句柄上收发）。本对象内部命令共用这把锁，故用 RLock。
        """
        if self.simulation:
            self.name = "模拟设备 (Simulation)"
            return True
        if not self.pid:
            # 未选择目标鼠标（新装且没插受支持设备）：不必扫全品牌 VID，
            # 也不该留下"找不到 0x0000"这种误导性日志
            return False
        with self._lock:
            # 重连场景：先关闭可能残留的旧句柄（掉线后 handle 失效但未释放会泄漏）
            if self.handle is not None:
                try:
                    self.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                # 底层只留 Rust 一路（backends.get_backend 是唯一后端，缺扩展
                # 时它自己抛错），不再有"换个后端再试"的循环。
                if self._connect_via(get_backend()):
                    return True
                # 没抛异常但选不出控制接口（枚举为空 / 没有 feat 合格的集合）
                # 同样是"永久未连接"的常见真因，必须留线索
                self._warn_once(
                    "connect",
                    f"未找到 PID 0x{self.pid or 0:04X} 的可用控制接口"
                    f"（枚举为空或不满足帧长要求）")
            except Exception as e:  # noqa: BLE001
                self._warn_once("connect", f"connect failed: {e}")
            return False
    def _connect_via(self, backend: RustHidBackend) -> bool:
        # 枚举 VID：已收录 PID 用它协议条目的 vid（其他品牌由此接入）；
        # PID 未知时退而扫全部已收录品牌的 VID（反正无协议最终也收发不了）。
        vids = (self.proto.vid,) if self.proto is not None else all_vids()
        if self.path:
            candidates: list[HidDeviceInfo] = [
                HidDeviceInfo(path=self.path,
                              vid=self.proto.vid if self.proto is not None
                              else VID,
                              pid=self.pid or 0,
                              name=None, usage_page=0, interface=None)]
        else:
            candidates = [d for vid in vids for d in backend.enumerate(vid)
                          if self.pid is None or d.pid == self.pid]
        if not candidates:
            return False

        # 选择控制接口：
        # · 雷蛇式（needs_probe=True）：优先「Feature Report 长度 >= 90」的接口
        #   （雷蛇控制接口的特征，例如 Viper V3 极速版的 usage_page 是 0x0001
        #   但 feat=91）。
        # · HID++（needs_probe=False）：挑**厂商自定义集合**（usage_page
        #   0xff00-0xffff）且 Feature 长度正好等于短/长报文总长 7 / 20
        #   （含 report id）。依据：libratbag 用 `(usage_page & 0xff00) == 0xff00`
        #   认厂商集合、按 report_id 0x10/0x11 取长度；Solaar 的短/长常量同样是
        #   7 / 20。原来只有"feat>=90"一条判据，罗技永远匹配不上，只能退回
        #   "首个能打开的集合"——那多半是鼠标的输入集合，发帧自然没反应。
        #   **未经验证**：HID++ 这一支是照 libratbag / Solaar 推出来的，手上没有
        #   罗技设备可验（雷蛇那条 `feat>=90` 是实测的）。接上真机连不上时，
        #   先看这条判据选中了哪个集合、再看它是不是只支持中断读写。
        # 都没有就**连接失败**：曾经这里退回"首个能打开的接口"，那是为想象中的
        # "只走 output/input 通道的型号"留的退路，实际只会让程序连上一个发不出
        # 帧的集合、然后每 3 秒重试一次，用户看到的是永久"未连接"却查不出原因
        # （2026-09-17 按"不留兼容层/fallback"删除；响亮失败优于静默错连）。
        hidpp = self.proto is not None and not self.proto.needs_probe

        def _is_control(d: HidDeviceInfo, flen: int | None) -> bool:
            if flen is None:
                return False
            if hidpp:
                return ((d.usage_page & 0xFF00) == 0xFF00
                        and flen in (HIDPP_SHORT_LEN, HIDPP_LONG_LEN))
            return flen >= REPORT_LEN

        for d in candidates:
            try:
                handle = backend.open(d.path)
            except Exception:  # noqa: BLE001
                continue
            try:
                flen = backend.get_feature_len(handle)
            except Exception:  # noqa: BLE001
                flen = None
            if _is_control(d, flen):
                self._adopt(backend, d, handle, flen)
                return True
            # 不是控制接口：句柄必须当场关掉，否则每次重连都漏一个内核句柄
            backend.close(handle)
        return False

    def _adopt(self, backend: RustHidBackend, info: HidDeviceInfo,
               handle: Any, flen: int | None) -> None:
        self.handle = handle
        self.handle_backend = backend
        self.info = info
        if info.pid in DEVICE_DB:
            self.name = DEVICE_DB[info.pid].product
        self.feature_len = flen
        # HID++ 类协议（如罗技）：没有 tx_id 概念，改为连接后协商 feature index。
        # 协商必须在 probe 之前——HID++ 每条命令都要用协商出来的 index 寻址。
        if self.proto is not None and not self.proto.needs_probe:
            try:
                ok = bool(self.proto.negotiate(self._raw_exchange))
                if not ok:
                    # 协商**没有抛错**但没凑齐 feature 时，以前这里一声不响：
                    # 设备永远连不上、日志里零线索（"永久未连接"最难查的一类）。
                    self._warn_once(
                        "negotiate",
                        "feature 协商不完整（设备缺少本程序要用的 feature，"
                        "电量/DPI 会读不出来）")
            except Exception as e:  # noqa: BLE001
                ok = False
                self._warn_once("negotiate", f"feature negotiate failed: {e}")
            # HID++ 没有 tx_id 可探测，就把"寻址信息（feature index）已确认
            # 可用"记在同一个标志上。**必须置位**：`controller._cache_probe_result`
            # 只认这一个门（`if not dev.tx_id_verified: return`），不置位则配置
            # 永不回写、设备也永远不会被记进「我的鼠标」（2026-09-17 审查发现）。
            self.tx_id_verified = ok
            return                      # 跳过雷蛇式 tx_id 探测
        if self.tx_id_verified:
            return          # 本对象已验证过 tx_id，重连不再重复 HID 交互
        if self.tx_id is None:
            self.tx_id_verified = self.probe()
            return
        # 配置里的 tx_id 可能来自旧版本"探测失败也会写入兜底值"的行为，也可能
        # 是上次开机时鼠标关机、探测全败留下的 0x1F 兜底值。所以**每次连接**都
        # 重新校验，不通过就完整重探——本对象会被离线重连反复复用，只在首次
        # 连接校验会让错误 tx_id 永久留在进程里（鼠标后来开机也连不上）。
        if self._verify_tx(self.tx_id):
            self.tx_id_verified = True
        else:
            self.tx_id = None
            # 重连路径：复探只发一轮，别在鼠标关机时长时间占锁（见 probe docstring）
            self.tx_id_verified = self.probe(retries=1)

    @contextmanager
    def _batch(self):
        """把协议的**多步流程**（读/写整块板载配置）圈成一次持锁操作。

        `_raw_exchange` 每次收发各自取一次锁，多步流程（如罗技的板载配置要
        16 次往返）中间就留出了缝：`close()` / `connect()` 插进来会把句柄换掉或
        关掉，后半段要么读到 None、要么读到半新半旧的数据。整段持锁后互斥。
        锁是 RLock，内层再取不会自锁。
        """
        with self._lock:
            yield

    def _raw_exchange(self, req: bytes, report_id: int) -> bytearray | None:
        """原始收发一帧（给协议层的协商/特殊命令用）。

        与 _send 的区别：不经过 (cmd_class, cmd_id) 翻译，直接把调用方给的字节
        发出去——HID++ 协商 feature index 时还没有"命令号"这一层概念。
        失败返回 None，不抛（协商失败按"该 feature 不可用"处理，不影响连接）。
        """
        backend = self.handle_backend or get_backend()
        if self.handle is None:
            return None
        proto = self.proto
        with self._lock:
            try:
                backend.set_feature(self.handle, bytes(req), report_id)
                time.sleep(0.02)
                # 响应长度跟请求一致（HID++ 短帧 7 / 长帧 20），别写死 20：
                # 按 20 去读一条 7 字节短帧，要么被后端按错误尺寸拒掉，
                # 要么把后面无关字节当成参数解出来。
                resp = backend.get_feature(self.handle, len(req), report_id)
                if not resp:
                    return None
                # **错误帧必须当"没有响应"**：HID++ 出错时 resp[2] = 0x8F，
                # 后面的字段是错误上下文而不是数据。原样交给协议层的话，
                # negotiate 会把 0x8Fxx 当成 feature id 缓存下来，板载读写则会
                # 把错误报文当扇区数据（CRC 兜底只会报"校验不符"，把排查方向
                # 引到布局猜测上）。2026-10-26 审查补。
                if proto is not None and not proto.is_success(resp):
                    log.debug("raw exchange: 设备返回错误帧 %s",
                              bytes(resp[:6]).hex())
                    return None
                return bytearray(resp)
            except Exception as e:  # noqa: BLE001
                log.debug("raw exchange failed: %s", e)
                return None

    def _verify_tx(self, tx: int) -> bool:
        """用一次 GET_FW 校验给定的 tx_id 是否真的被设备接受。"""
        try:
            resp = self._send(CLASS_DEVICE, CMD_GET_FW, b"\x00\x00",
                              tx_id=tx, verify=False, retries=1)
            return self._require_proto().is_success(resp)
        except Exception:  # noqa: BLE001
            return False

    def probe(self, retries: int = 3) -> bool:
        """自动探测 (report_id, tx_id) 组合：按协议类给的候选顺序逐个试。

        探测成功（GET_FW 返回 resp[0]==0x02）即锁定该组合；都不成则退到协议类
        的 fallback_tx（毒蝰 V3 为 0x1F、炼狱蝰蛇标准版为 0xFF）——但**不置**
        tx_id_verified，调用方不应把兜底值当结论缓存。

        `retries` 是每条候选的重试次数。鼠标关机时整轮 probe 要
        report_id×tx_id×retries 次收发且全程持锁，所以**重连后的复探**用 1 次
        就够（首次连接才用 3 次求稳）。
        """
        if self.proto is None:
            return False            # 未收录协议：无从探测，交由上层按离线处理
        started_rid = self.report_id
        for rid in self.proto.report_id_candidates:
            self.report_id = rid
            for cand in self.proto.tx_candidates:
                try:
                    resp = self._send(CLASS_DEVICE, CMD_GET_FW, b"\x00\x00",
                                      tx_id=cand, verify=False, retries=retries)
                    if self.proto.is_success(resp):
                        self.tx_id = cand
                        return True
                except Exception:  # noqa: BLE001
                    continue
        self.report_id = started_rid        # 别把已缓存的 0（unnumbered）改成 2
        if self.tx_id is None:
            self.tx_id = self.proto.fallback_tx
        return False

    # ---------- 底层收发 ----------
    def _send(self, cmd_class: int, cmd_id: int, data: bytes = b"",
              tx_id: int | None = None, verify: bool = True,
              retries: int = 3) -> bytearray | None:
        """发一条命令。

        verify=True（默认）：返回 90 字节响应，失败一律抛异常（绝不为 None）。
        verify=False（探测用）：返回响应或 None，不抛。
        """
        if self.simulation:
            return self._simulate(cmd_class, cmd_id, data)
        self._require_proto()   # 未收录协议：别拿探测兜底值去碰未知设备
        with self._lock:
            return self._send_locked(cmd_class, cmd_id, data, tx_id, verify, retries)

    def _send_locked(self, cmd_class: int, cmd_id: int, data: bytes = b"",
                     tx_id: int | None = None, verify: bool = True,
                     retries: int = 3) -> bytearray | None:
        backend = self.handle_backend or get_backend()
        if self.handle is None:
            # 没句柄就别对着 NULL 把 retries 轮收发跑满（≈0.24s 白占锁），
            # 更不要抛出 "HidD_SetFeature 失败(LastError=6)" 这种把"未连接"
            # 说成"设备拒绝"的误导错
            raise RuntimeError("设备句柄未建立（未连接）")
        tx = tx_id if tx_id is not None else self.tx_id
        # 只有 None 才兜底成协议类的 fallback_tx：tx_id=0 是合法候选
        # （tx_candidates 末位），写成 `tx or fallback` 会把 0 悄悄换成兜底值，
        # 发出去的报文就不再是探测时验证过的那个事务 ID。
        proto = self.proto
        tx_val = tx if tx is not None else proto.fallback_tx
        req = proto.build_report(cmd_class, cmd_id, data, tx_val)
        req_bytes = bytes(req)
        # report id 与响应长度按协议走：雷蛇是探测出的 report_id + 90 字节固定帧；
        # HID++ 的 report id 就在报文首字节（0x10/0x11），响应 7 或 20 字节。
        rid = proto.report_id_for(req_bytes, self.report_id)
        exp_len = proto.resp_len_for(req_bytes)
        last: Exception | None = None
        for attempt in range(retries):
            # 每轮重置：只报**最后一次**尝试的异常。留着前一轮的会让"最后一次
            # 设备回了个非法帧"报成第一次的连接错误，把排查方向带偏。
            last = None
            try:
                backend.set_feature(self.handle, req_bytes, rid)
                time.sleep(0.02)
                resp = backend.get_feature(self.handle, exp_len, rid)
                if resp and len(resp) >= exp_len:
                    if not verify or proto.is_success(resp):
                        return bytearray(resp)
            except Exception as e:  # noqa: BLE001
                last = e
            if attempt + 1 < retries:       # 最后一次失败后没必要再等
                time.sleep(0.06)
        if verify:
            # 有真实异常就抛它；否则说明设备始终没给出合规响应（resp[0] != 0x02）
            # 或压根没响应。这里必须抛错——返回 None 会让调用方在 resp[9] 处炸出
            # 「'NoneType' object is not subscriptable」这种毫无信息量的报错。
            raise last or RuntimeError(
                f"设备无有效响应（class=0x{cmd_class:02X} cmd=0x{cmd_id:02X}）")
        # 探测模式：失败就是"没有响应"，返回 None（异常对象是 truthy，
        # 把它原样返回会让调用方的 `if resp and resp[0] == 0x02` 抛 TypeError）
        return None

    def _simulate(self, cmd_class: int, cmd_id: int, data: bytes) -> bytearray:
        # 必须按 (class, cmd_id) 分派：GET 类命令号会在不同 class 之间复用
        # （CMD_GET_DPI_XY 与 CMD_GET_POLL 都是 0x85），只看 cmd_id 会让后一支
        # 永远走不到。
        resp = bytearray(REPORT_LEN)
        resp[0] = 0x02  # success
        if cmd_id == CMD_GET_FW:
            resp[9], resp[10] = 1, 0
        elif cmd_class == CLASS_DPI and cmd_id == CMD_GET_DPI_XY:
            resp[9], resp[10] = 0x03, 0x20   # X = 800
            resp[11], resp[12] = 0x03, 0x20  # Y = 800（X、Y 连续排列）
        elif cmd_class == CLASS_DEVICE and cmd_id == CMD_GET_POLL:
            resp[8] = 0x01                    # 1000 Hz（POLL_TO_VAL: 1000=0x01）
        elif cmd_id == CMD_GET_BATTERY:
            resp[9] = 0xC9                    # 79%（0xC9=201，201/255≈78.8%）
        return resp

    # ---------- 高层 API ----------
    def heartbeat(self) -> bool:
        """在线心跳：单次"鼠标本体是否活着"的判定。

        无线型号读电量——dongle 可枚举但鼠标关机时电量必失败，能正确判离线；
        纯有线型号（无电池）读固件版本（GET_FW），插着线就应当响应。
        失败/异常一律 False（_send 在 verify=True 下失败是抛异常的，这里必须吃掉，
        否则"判离线"会变成"炸调用方"）。
        """
        try:
            if self.has_battery:
                return self.get_battery() >= 0
            resp = self._send(CLASS_DEVICE, CMD_GET_FW, b"\x00\x00")
            # 成功位由协议判断：雷蛇是 resp[0]==0x02，HID++ 是 feature index
            # 位不回 0x8F——把雷蛇的字节写死在这层，别的品牌永远判不出在线。
            return self._require_proto().is_success(resp)
        except Exception as e:  # noqa: BLE001
            log.debug("heartbeat failed: %s", e)
            return False

    def get_firmware(self) -> str:
        proto = self._require_proto()
        resp = self._send(CLASS_DEVICE, CMD_GET_FW, b"\x00\x00")
        return proto.decode_fw(resp)

    def get_dpi(self) -> tuple[int, int]:
        proto = self._require_proto()
        # 板载配置型设备（如罗技）：DPI 不在单独指令里，交给协议走多步流程。
        # 整段持锁（见 _batch）：多步中间不能让 close()/connect() 插进来。
        if proto.onboard_profiles:
            with self._batch():
                return proto.read_dpi(self._raw_exchange)
        resp = self._send(CLASS_DPI, CMD_GET_DPI_XY, b"\x00" * 7)
        return proto.decode_get_dpi(resp)

    def set_dpi(self, dpi: int, store: int = 1) -> None:
        proto = self._require_proto()
        v = int(dpi)
        if not (1 <= v <= self.dpi_max):
            log.warning("set_dpi: %s 超出范围 [1, %s]，已钳制（UI 层应先行校验）",
                        dpi, self.dpi_max)
            v = max(1, min(v, self.dpi_max))
        # 同上：板载配置型设备走协议自己的多步写入
        if proto.onboard_profiles:
            with self._batch():
                proto.write_dpi(self._raw_exchange, v)
            return
        self._send(CLASS_DPI, CMD_SET_DPI_XY, proto.encode_set_dpi(v, store))

    def get_poll(self) -> int | None:
        """读当前轮询率。

        只认协议映射里那几档（125/250/500/1000）；设备若真在 2000Hz
        （set_poll_v2 设进去的）这里读不出来，返回 None 而不是假装 500——
        否则界面会把 500 高亮成当前档位。
        """
        proto = self._require_proto()
        if proto.onboard_profiles:
            with self._batch():          # 多步流程整段持锁，见 get_dpi
                return proto.read_poll(self._raw_exchange)
        resp = self._send(CLASS_DEVICE, CMD_GET_POLL, b"\x00")
        return proto.decode_get_poll(resp)

    def set_poll(self, rate: int) -> None:
        proto = self._require_proto()
        # 板载配置型设备：轮询率同样走协议的多步写入（整段持锁）
        if proto.onboard_profiles:
            with self._batch():
                proto.write_poll(self._raw_exchange, rate)
            return
        self._send(CLASS_DEVICE, CMD_SET_POLL, proto.encode_set_poll(rate))

    def set_poll_v2(self, rate: int) -> None:
        """实验性：高轮询率（2000/4000/8000Hz），需 HyperPolling 接收器。

        仅协议声明 supports_poll_v2 的型号才发（纯有线入门型号发了也没意义）。
        """
        if not self._require_proto().supports_poll_v2:
            raise RuntimeError(f"{self.name} 不支持高轮询率(HyperPolling)命令")
        self._send(CLASS_DEVICE, CMD_SET_POLL_V2,
                   self.proto.encode_set_poll_v2(rate))

    def get_battery(self) -> int:
        """电量百分比（0-100）。

        单位在协议层声明（`battery_max`）：雷蛇的 `decode_battery` 给裸 0-255，
        HID++ 直接给 0-100。这里统一换算成百分比再往外走——曾经把各品牌的原始值
        直接抛给 UI、由显示处各自除 255，罗技的 85% 就被当成裸值算成 33%。

        响应是否有效**不在这层判断**：`_send(verify=True)` 已经按协议过
        `is_success` 和长度（见 `_send_locked`）。这里曾经另加一道
        `len(resp) < 10`，那是雷蛇 90 字节帧的形状：罗技 HID++ 的电量响应是
        7 字节短帧，于是每次都被判"电池响应无效"，而 `heartbeat()` 又拿
        `get_battery()` 当唯一存活信号 → 罗技鼠标永远转不了在线。
        """
        proto = self._require_proto()
        if not self.has_battery:
            raise RuntimeError("此型号为纯有线鼠标，无电量可读")
        resp = self._send(CLASS_POWER, CMD_GET_BATTERY, b"\x00\x00")
        raw = proto.decode_battery(resp)
        scale = proto.battery_max or 255
        return max(0, min(100, round(raw * 100 / scale)))

    def set_idle_timeout(self, minutes: int) -> None:
        """设置闲置进入睡眠时间(分钟, 1-15)。Power class 0x07 cmd 0x26。"""
        proto = self._require_proto()
        v = max(1, min(15, int(minutes)))
        self._send(CLASS_POWER, CMD_SET_IDLE_TIMEOUT,
                   proto.encode_idle_timeout(v))

    def set_low_battery_threshold(self, percent: int) -> None:
        """设置低电量阈值(%, 5-100)。Power class 0x07 cmd 0x25。"""
        proto = self._require_proto()
        v = max(5, min(100, int(percent)))
        self._send(CLASS_POWER, CMD_SET_LOW_BATTERY,
                   proto.encode_low_battery(v))

    def set_dpi_stages(self, stages: list[int], store: int = 1) -> list[int]:
        """写 5 档档位（需协议类声明 supports_dpi_stages），返回**真正写进去**的档位。

        炼狱蝰蛇标准版等型号不支持（设备不接受该命令）——直接抛"不支持"，
        由上层提示用户改用「档 N」直接设置当前 DPI。

        越界值在这里被钳进 [1, dpi_max]。钳过之后必须把最终值回传给调用方：
        否则上层（controller 存配置、UI 显示）会留着用户输入的那个设备根本
        存不下的数字，界面上"已保存 30000"、鼠标里其实是 dpi_max。
        命令布局固定是 5 档连排，不足 5 档时按最后一档补齐，别发短参数块。
        """
        if not self._require_proto().supports_dpi_stages:
            raise RuntimeError(
                f"{self.name} 不支持写 5 档档位命令，"
                f"可改用「档 N」直接设置当前 DPI")
        clamped: list[int] = []
        for s in stages[:5]:
            v = int(s)
            if not (1 <= v <= self.dpi_max):
                log.warning("set_dpi_stages: %s 超出范围 [1, %s]，已钳制", s,
                            self.dpi_max)
                v = max(1, min(v, self.dpi_max))
            clamped.append(v)
        if not clamped:
            raise RuntimeError("档位表为空，拒绝写入")
        while len(clamped) < 5:             # 补齐到命令布局的固定 5 档
            clamped.append(clamped[-1])
        self._send(CLASS_DPI, CMD_SET_DPI_STAGES,
                   self.proto.encode_dpi_stages(clamped, store))
        return clamped

    def close(self) -> None:
        """关闭 HID 句柄。持锁执行，避免与正在收发命令的线程（轮询/重连）
        并发关闭——Windows 下关闭进行中的句柄会使 in-flight 调用失败。"""
        with self._lock:
            if self.handle:
                try:
                    backend = self.handle_backend or get_backend()
                    backend.close(self.handle)
                except Exception:  # noqa: BLE001
                    pass
                self.handle = None
