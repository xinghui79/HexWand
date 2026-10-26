"""设备层：HID 后端、报文、协议库、型号库、设备控制、胶水与状态机。

对外门面（UI 只需 from hexwand import hardware as hw）：
  frame      报文帧格式 / 命令号 / 轮询率映射 / CRC（所有鼠标共用）
  protocols  各鼠标的私有协议（一个文件收齐所有型号，每鼠标一个条目；
             无通用兜底——未收录 PID 拒绝收发）
  catalog    已知型号能力库（DEVICE_DB、product_groups、pid→协议）
  backends   HID 后端（Rust 原生 hid.rs，唯一实现）
  device     MouseDevice：连接、探测、高层控制 API
  controller Controller：device + config 的胶水
  monitor    LinkMonitor：在线/离线心跳与物理切档状态机
  keymap     标准按钮 id / 鼠标图布局（键位页与钩子映射共用）
"""

from __future__ import annotations

from .backends import (HidDeviceInfo, RustHidBackend,
                       feature_len_of_path, get_backend, get_backend_summary,
                       scan_known_devices)
from .catalog import (DeviceInfo, DEVICE_DB, known_device, product_groups,
                      protocol_for)
from .controller import Controller
from .device import MouseDevice
from .monitor import LinkMonitor
from .protocols import (ProtocolProfile, all_protocols, all_vids,
                        get_protocol)

__all__ = [
    "HidDeviceInfo", "RustHidBackend",
    "get_backend", "get_backend_summary", "scan_known_devices",
    "feature_len_of_path",
    "DeviceInfo", "DEVICE_DB", "known_device", "product_groups",
    "protocol_for",
    "ProtocolProfile", "get_protocol", "all_protocols", "all_vids",
    "MouseDevice", "Controller", "LinkMonitor",
]
