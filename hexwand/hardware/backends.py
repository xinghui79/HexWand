"""HID 后端：唯一实现是 Rust 原生后端（`rust/src/hid.rs`）。

- HidDeviceInfo：枚举结果
- RustHidBackend：windows-sys 直调 hid.dll / setupapi / kernel32 的实现

2026-09-17：**只保留 Rust 一路**。同一套系统调用原本还有一份手写 ctypes 版
（WinHidBackend，约 400 行结构体 + argtypes 声明）和一份 pyhidapi 兜底版
（PyHidBackend），两者都已删除。理由是真机 A/B 比对显示两版逐字节等价，
而"两套实现要同步维护"的成本更高——`hexwand_rust` 本来就是硬依赖
（改键钩子 MouseRemapper 也在里面，没有扩展程序根本跑不起来）。

曾经这里还有一个 `HIDBackend` 抽象基类（6 个 NotImplementedError，唯一的
作用是给 device.py 当类型标注）。已删除：只有一个实现时它只是"看起来更
分层"的空壳，类型标注直接写 `RustHidBackend` 即可（2026-09-17 审查）。

枚举结果统一为 HidDeviceInfo。Feature Report 的 numbered/unnumbered 判定、
`HidP_GetCaps` 的 **HIDP_STATUS_SUCCESS(0x00110000)** 判定都在 Rust 侧，
见 `rust/src/hid.rs` 顶部注释（"成功值不是 0"那条真机教训记在那里）。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .catalog import is_known_device
from .protocols import all_vids

log = logging.getLogger(__name__)


# =========================================================
# 数据结构
# =========================================================
@dataclass
class HidDeviceInfo:
    path: str
    vid: int
    pid: int
    name: str | None
    usage_page: int
    interface: int | None

    @property
    def known(self) -> bool:
        """这台设备是否收录在设备库里——按 (VID, PID) 判。

        只看 PID 不够：PID 由各厂商自己编号，雷蛇与罗技可以撞同一个数字，撞上
        就把别的品牌的设备认成"已知型号"、拿另一套协议字节去收发。
        """
        return is_known_device(self.vid, self.pid)


# Windows 的 HID 接口路径形如
#   \\?\hid#vid_1532&pid_00b8&mi_00#8&2f5a1b3&0&0000#{4d1e55b2-...}
# `mi_00` 就是接口号；非复合设备没有这一段（返回 None）。
_MI_RE = re.compile(r"mi_([0-9A-Fa-f]{2})")


def _interface_from_path(path: str) -> int | None:
    """从设备接口路径取 `mi_XX` 编号；没有该段返回 None。"""
    m = _MI_RE.search(path or "")
    return int(m.group(1), 16) if m else None


# =========================================================
# Rust 原生后端（rust/src/hid.rs）
# =========================================================
class RustHidBackend:
    """Rust 版原生 HID 后端（唯一实现，没有兜底）。

    方法语义与 Rust 侧逐条对应：access=0 打开、缓冲区长度取设备声明的
    FeatureReportByteLength、小帧返回**整帧**（首字节是 report id）。

    `hexwand_rust` 缺失时直接抛 RuntimeError——不再有 ctypes 回落。缺扩展
    意味着改键钩子也没有，程序本来就不完整，早点报错比带着半个功能跑更好。
    """

    name = "native(rust hid)"

    def __init__(self) -> None:
        import hexwand_rust                      # 硬依赖：缺扩展就到此为止
        self._b = hexwand_rust.HidBackend()

    def enumerate(self, vid: int | None = None) -> list[HidDeviceInfo]:
        # 接口号（mi_XX）不在 Rust 的返回值里，但它就在设备路径中；以前这里
        # 固定填 None，`--scan` 只能打印 "interface: None"，而排查"选不出控制
        # 接口"时第一个要看的就是这个编号（控制接口应为 mi_00）。
        return [HidDeviceInfo(path=p, vid=v, pid=pid, name=nm,
                              usage_page=up, interface=_interface_from_path(p))
                for (p, v, pid, up, nm) in self._b.enumerate(vid)]

    def open(self, path: str) -> Any:
        return self._b.open(path)

    def get_feature(self, handle: Any, length: int, report_id: int = 2) -> bytes:
        return bytes(self._b.get_feature(handle, int(length), report_id & 0xFF))

    def set_feature(self, handle: Any, data: bytes, report_id: int = 2) -> bool:
        self._b.set_feature(handle, bytes(data), report_id & 0xFF)
        return True                             # 失败在 Rust 侧抛异常

    def get_feature_len(self, handle: Any) -> int | None:
        return self._b.feature_len(handle)

    def close(self, handle: Any) -> None:
        self._b.close(handle)


# =========================================================
# 后端获取与设备扫描
# =========================================================
_backend: RustHidBackend | None = None


def get_backend() -> RustHidBackend:
    """返回 HID 后端（懒加载、缓存）。

    构造只 import 扩展并实例化一次；连接失败重试路径每 15s 会调这里，
    不缓存会反复走 import 查找。缺扩展时 RuntimeError 直接上抛，由调用方
    （device.connect / 诊断页）自行处理并展示。
    """
    global _backend
    if _backend is None:
        _backend = RustHidBackend()
    return _backend


def scan_known_devices() -> list[HidDeviceInfo]:
    """枚举所有已收录品牌（VID 集合来自 protocols.all_vids）的 HID 设备。

    枚举失败返回空列表（UI 的"正在扫描"流程不该被一次系统调用失败打断），
    细节记 debug 日志。新品牌只要在 protocols.py 登记了 vid，这里无需改动。
    """
    try:
        backend = get_backend()
        return [d for vid in all_vids() for d in backend.enumerate(vid)]
    except Exception as e:  # noqa: BLE001
        log.debug("scan_known_devices failed: %s", e)
        return []


def feature_len_of_path(path: str) -> int | None:
    """诊断用：打开一次接口读 Feature 报表长度（含 report id）；失败返回 None。

    为什么不复用 `scan_known_devices()`：它只返回枚举信息。而"选不出控制接口"
    的排查第一顺位就是**逐条集合看 feature 长度**（雷蛇控制接口应为 91，
    见 CLAUDE.md 的排查顺序）。这里只读能力、不发任何报文，句柄当场关掉。
    """
    try:
        backend = get_backend()
    except Exception:  # noqa: BLE001 — 扩展缺失时按"读不到"显示
        return None
    try:
        handle = backend.open(path)
    except Exception:  # noqa: BLE001
        return None
    try:
        return backend.get_feature_len(handle)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            backend.close(handle)
        except Exception:  # noqa: BLE001
            pass


def get_backend_summary() -> tuple[str, int, list[tuple[str, int]]]:
    """供诊断：返回 (后端名, 全部 HID 设备数, [(vid_hex, count), ...])。

    任何一步失败都如实写进条目里返回，不抛异常——诊断页要显示的就是失败原因。
    """
    try:
        backend = get_backend()
    except Exception as e:  # noqa: BLE001
        return ("unavailable", 0, [(f"后端不可用：{e}", 0)])
    try:
        devs = backend.enumerate(None)
    except Exception as e:  # noqa: BLE001
        return (backend.name, 0, [(f"枚举失败：{e}", 0)])
    vids = Counter(d.vid for d in devs)
    items = [(f"0x{v:04X}", c) if isinstance(v, int) else (str(v), c)
             for v, c in vids.items()]
    return (backend.name, len(devs), items)
