"""罗技 HID++ 帧：与雷蛇完全不同的另一套帧（短 7 字节 / 长 20 字节）。

短报文 7 字节 / 长报文 20 字节，头 4 字节固定，其后是参数（短 3 / 长 16）：
  [0] report_id        0x10 短 / 0x11 长
  [1] device_index     设备序号（接收器后第 1 个设备）
  [2] feature_index    功能序号，连接后协商得出，不是固定常量
  [3] function | sw_id function 已含左移 4 位，低 4 位是 software id
响应为同格式；出错时 [2] 会回 0x8F。

板载配置（ONBOARD PROFILES {8100}）：罗技把 DPI / 轮询率放在这里，没有单独
指令，读写都得整块进行。本程序按 0x07 号配置格式解析（双轴 DPI + 抬起距离）；
命令 0x00「读配置描述」未实现（扇区大小/格式按已知值处理，不做运行时校验）。

短/长报文的选用与开源实现一致（2026-09-17 查证）：
  · Solaar `lib/logitech_receiver/base.py`：`HIDPP_SHORT_MESSAGE_ID=0x10`、
    `SHORT_MESSAGE_SIZE=7`、`HIDPP_LONG_MESSAGE_ID=0x11`、`_LONG_MESSAGE_SIZE=20`，
    组装是 `struct.pack("!BB18s", 0x11, devnumber, data)` —— **byte0 = report id**，
    缓冲区长度就是报文总长；短/长的判据是"载荷 > 5 字节就发长报文"
    （本文件 `len(params) <= 3` 是同一条规则的等价写法：4 字节头之外的载荷）。
  · libratbag `src/hidpp-generic.c` / Linux 内核 hidpp 驱动：同样是
    `REPORT_ID_HIDPP_SHORT 0x10`(7) / `REPORT_ID_HIDPP_LONG 0x11`(20)，
    并说明 HID++ 2.0（FAP）偏好长报文。

**未验证的传输假设（接上罗技真机先查这里）**：Solaar 与 libratbag 走的都是
**中断读写**（hidapi `write`/`read`、hidraw `write`/`read`），而本程序目前只实现
了 HID **Feature Report** 通道（`HidD_SetFeature`/`HidD_GetFeature`）。
若该型号的厂商集合（usage_page 0xff00-0xffff）把 0x10/0x11 声明为 Input/Output
报表而非 Feature 报表，`HidD_SetFeature` 会直接失败（`ERROR_INVALID_FUNCTION`），
此时需要在 backends 里补一条中断通道（`WriteFile`/`ReadFile` 同一句柄），
而不是改帧格式。
"""

from __future__ import annotations

# 帧层与雷蛇一样已 Rust 化（rust/src/lib.rs 的 build_hidpp_frame / crc_ccitt）：
# 纯函数、有 selftest 与独立 Python 参考实现逐字节比对。**hexwand_rust 是硬依赖**
# （与 frame/razer.py 同一条约定：缺 .pyd 程序起不来）。
import hexwand_rust as _rust

HIDPP_REPORT_SHORT = 0x10
HIDPP_REPORT_LONG = 0x11
HIDPP_SHORT_LEN = 7
HIDPP_LONG_LEN = 20
HIDPP_ERROR_INDEX = 0x8F          # 响应里出现它 = 设备报错

HIDPP_PAGE_ONBOARD_PROFILES = 0x8100
HIDPP_CMD_ONBOARD_GET_CURRENT = 0x40
HIDPP_CMD_ONBOARD_MEM_READ = 0x50
HIDPP_CMD_ONBOARD_WRITE_START = 0x60
HIDPP_CMD_ONBOARD_WRITE_DATA = 0x70
HIDPP_CMD_ONBOARD_WRITE_END = 0x80
HIDPP_PROFILE_SIZE = 256          # 一个配置扇区固定 256 字节


def build_hidpp_frame(device_index: int, feature_index: int, function: int,
                      params: bytes = b"", sw_id: int = 0x08) -> bytearray:
    """组 HID++ 帧：参数 ≤3 字节用短报文，否则用长报文。Rust 实现。

    function 是已左移 4 位的命令号（0x00 / 0x10 / 0x20），低 4 位留给 sw_id——
    写成 (function << 4) 会溢出、把功能号丢掉，所以只做掩码。
    返回 bytearray 保持与调用点（按下标改字节）的类型兼容。
    """
    return bytearray(_rust.build_hidpp_frame(
        device_index & 0xFF, feature_index & 0xFF, function & 0xFF,
        bytes(params), sw_id & 0x0F))


def crc_ccitt(data: bytes) -> int:
    """CRC-CCITT：初值 0xFFFF、多项式 0x1021、高位在前（非反射）。Rust 实现。"""
    return _rust.crc_ccitt(bytes(data))
