"""雷蛇标准 90 字节帧：帧布局、命令号、轮询率映射、CRC。

帧结构（固定 90 字节，索引从 0 开始）：
  [0]   status          (新命令填 0x00)
  [1]   transaction_id  (事务 ID，不同型号不同，常见 0x1F / 0xFF / 0x3F)
  [2-3] remaining_packets (大端, 通常 0x0000)
  [4]   protocol_type   (标准协议填 0x00)
  [5]   data_size       (参数区有效字节数)
  [6]   command_class   (子系统: 0x00 设备 / 0x04 DPI / 0x07 电源)
  [7]   command_id      (具体动作; GET 类会在高位置 1, 如 0x85 = 0x05|0x80)
  [8..87] arguments    (参数, 最多 80 字节)
  [88]  crc             (XOR(report[2..87]))
  [89]  reserved        (0x00)

传输：通过 HID Feature Report 发送/接收（report 编号由 frame 之外的
report_id 决定，见 device.py 的 probe）。

本模块只放"报文长什么样"：帧布局、命令号、校验算法。雷蛇各型号的差异
（tx_id 候选、能力开关）在 protocols.py 的 RazerProfile 实例上；命令怎么
收发、结果怎么解释在各品牌协议类里。型号能力库见 catalog.py。
"""

from __future__ import annotations

# 帧层已 Rust 化（2026-09-17，rust/src/lib.rs）：crc8 / build_report 由
# hexwand_rust.pyd 提供。**这是硬依赖** —— 没有 .pyd 程序无法启动（导入即失败），
# 构建/刷新方法见 rust/Cargo.toml 顶部注释。帧格式的文档说明同样适用于 Rust 实现，
# 两者由 --selftest 的用例锁死（用例里带一份独立参考实现做逐字节比对）。
import hexwand_rust as _rust

VID = 0x1532   # 雷蛇的 USB Vendor ID（默认帧配套）。其他品牌的 VID 不在这里，
               # 在 protocols.py 各协议条目的 vid 字段；扫描按 all_vids() 集合进行
REPORT_LEN = 90

# ---- class 0x00 —— 设备信息 ----
CLASS_DEVICE = 0x00
CMD_GET_FW = 0x81
CMD_SET_POLL = 0x05
CMD_GET_POLL = 0x85

# ---- class 0x04 —— DPI ----
CLASS_DPI = 0x04
CMD_SET_DPI_XY = 0x05
CMD_GET_DPI_XY = 0x85
CMD_SET_DPI_STAGES = 0x06

# ---- class 0x07 —— 电源/电量 ----
CLASS_POWER = 0x07
CMD_GET_BATTERY = 0x80
CMD_SET_IDLE_TIMEOUT = 0x26        # 闲置进入睡眠时间（分钟）
CMD_SET_LOW_BATTERY = 0x25         # 低电量阈值（%）

CMD_SET_POLL_V2 = 0x40             # 实验性：2000/4000/8000 Hz（需 HyperPolling 接收器）

# 轮询率映射：指令值 = 1000 / Hz（雷蛇与罗技板载共用同一套 Hz 约定）
POLL_TO_VAL: dict[int, int] = {125: 0x08, 250: 0x04, 500: 0x02, 1000: 0x01}
VAL_TO_POLL: dict[int, int] = {v: k for k, v in POLL_TO_VAL.items()}


def crc8(report: bytearray) -> int:
    """CRC = XOR(report[2..87])，写入 report[88]。Rust 实现。"""
    return _rust.crc8(bytes(report))


def build_report(cmd_class: int, cmd_id: int, data: bytes = b"",
                 tx_id: int = 0x1F) -> bytearray:
    """构造 90 字节命令报文。data 最多 80 字节（rep[8..87]）。Rust 实现。"""
    # device.py 会改写 rep[0]、按索引读字段，转回 bytearray 保持类型兼容
    return bytearray(_rust.build_report(cmd_class & 0xFF, cmd_id & 0xFF,
                                        bytes(data), tx_id & 0xFF))
