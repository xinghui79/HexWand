"""各品牌各鼠标的私有协议库：一个文件收齐所有型号，每只鼠标一个条目。

与 frame / catalog.py 的分工：
  frame        报文帧格式与命令号，按品牌分模块（frame.razer 雷蛇 90 字节帧、
               frame.hidpp 罗技 HID++ 帧）；只放"报文长什么样"，不含型号差异。
  本文件       讲"这只鼠标自己的字节"——USB VID、事务 ID（tx_id）候选与兜底
               值、各命令的参数编码（请求字节布局）与响应解码（字节 → 物理
               值）、能力开关（是否支持写 5 档档位 / HyperPolling 等）；组帧
               统一调 frame 里的 build_report / build_hidpp_frame，不在本文件重写。
  catalog.py   讲"这台设备能干什么 + 用哪个协议条目"（pid → protocol 键）。
  device.py    只按协议条目的方法收发，自身不再出现任何品牌/型号差异字节。

没有"通用兜底协议"：未收录的 PID 一律视为未知，device.py 会拒绝收发
（宁可明确报"未收录"，也不拿雷蛇标准协议去碰其他品牌）。

新增一只鼠标（雷蛇 / 罗技：只改本文件；新品牌：先写品牌协议类，再写实例）：
  1. 选（或写）品牌协议类——基类 ProtocolProfile 只装字段与元数据，**不含任何
     品牌字节**，字节都在品牌子类里：
       - 雷蛇  → RazerProfile（已有，雷蛇型号一律用它）
       - 罗技  → HidppProfile（已有，HID++ 4.2）
       - 新品牌 → 自建子类，实现 build_report / is_success / 各 encode_* /
                 decode_*，vid 填该品牌 VID，并注明字节来源（抓包 / 开源项目）。
     （同品牌的新型号不需要新类，直接实例化已有的品牌类即可。）
  2. 实例化，带齐该鼠标的元数据：
       - key / name          协议标识与型号展示名
       - pids                该鼠标的全部通信 PID（一只鼠标可能占用多个 PID，
                             如有线直连 + 2.4G 接收器各一个）
       - vid / conn / battery / dpi_max / product  见类 docstring 的设备元数据段
       - 有参数差异（tx_candidates / fallback_tx / 能力开关）直接改字段。
  3. 定义即注册：实例是模块级变量，_REGISTRY 在 import 时自动收集本模块所有
     ProtocolProfile 实例（无需手动加进任何列表）；catalog.DEVICE_DB 也据此
     自动展开每个 PID 的 DeviceInfo，UI 支持列表/电量卡片/DPI 上限校验自动跟上。
"""

from __future__ import annotations

import copy
import logging
import struct
import time
from dataclasses import dataclass, field

from .frame import (CLASS_DEVICE, CLASS_POWER, CMD_GET_BATTERY, CMD_GET_FW,
                    HIDPP_CMD_ONBOARD_GET_CURRENT, HIDPP_CMD_ONBOARD_MEM_READ,
                    HIDPP_CMD_ONBOARD_WRITE_DATA, HIDPP_CMD_ONBOARD_WRITE_END,
                    HIDPP_CMD_ONBOARD_WRITE_START, HIDPP_ERROR_INDEX,
                    HIDPP_LONG_LEN, HIDPP_PAGE_ONBOARD_PROFILES,
                    HIDPP_PROFILE_SIZE, HIDPP_REPORT_LONG, HIDPP_REPORT_SHORT,
                    HIDPP_SHORT_LEN, POLL_TO_VAL, VAL_TO_POLL,
                    build_hidpp_frame, build_report, crc_ccitt)

log = logging.getLogger(__name__)


@dataclass
class ProtocolProfile:
    """单只（一族）鼠标的完整私有协议。

    key                    协议标识，catalog.DeviceInfo.protocol 引用它
                           （类名/变量名只是容器，真正对外的是 key）。
    product                型号名（必填）：设备页显示它，也是聚合键，
                           把同型号的有线/无线多个 PID 合成一行。
    vid                    USB Vendor ID（必填，基类不给品牌默认值）：品牌
                           子类提供默认（如 RazerProfile=0x1532），或实例化
                           时显式给定；扫描/枚举按已收录 VID 集合进行。

    needs_probe            是否需要 (report_id, tx_id) 探测。True 的实例必须
                           给出 tx_candidates / fallback_tx（型号差异，写在
                           实例上；report_id 候选可由品牌类给默认）；
                           False（如 HID++）连接后走协商，不用这些字段。

    能力开关（False 时 device.py 对应方法直接抛"不支持"）：
      supports_dpi_stages    是否支持 CMD_SET_DPI_STAGES 写 5 档档位
      supports_poll_v2       是否支持 CMD_SET_POLL_V2 高轮询率（HyperPolling）

    设备元数据（catalog 据此自动展开 DEVICE_DB；一只鼠标一个实例，pids 是该
    鼠标的全部通信地址，其余为该型号共享的展示/能力信息）：
      pids        该鼠标的全部通信 PID（list[int]）
      conn        连接方式（"2.4G" / "蓝牙" / "有线"，可多项）
      battery     是否有电池（无线 True / 纯有线 False）
      battery_max decode_battery() 返回值的满量程：雷蛇是裸 0-255，
                  HID++ 直接给 0-100 百分比。device 层据此统一换算成
                  百分比再交给 UI——让每个显示处各自猜单位必然出错
                  （曾经 window.py 拿 0-100 的读数再除 255，85% 显示成 33%）。
      dpi_max     官方最大 DPI（写档位上限校验）

    build_report / is_success / encode_* / decode_* **本类不实现任何品牌字节**
    （调用即抛 NotImplementedError）：雷蛇字节在 RazerProfile、罗技 HID++ 在
    HidppProfile，新品牌自建子类。漏实现只会明确报错，不会误发别的品牌的报文。
    """

    key: str
    product: str           # 必填：型号名（列表显示它，也是聚合键，不能留空）
    # 探测参数（必填）：tx 候选与兜底因型号而异，由实例声明；基类不含品牌值
    tx_candidates: list[int]         # probe() 依次尝试的事务 ID
    fallback_tx: int                 # 探测全败时的兜底（绝不写成"已验证"结论）
    report_id_candidates: list[int]  # probe() 依次尝试的 Report 编号
    vid: int               # 必填：品牌 USB VID（基类不含任何品牌默认值）
    report_len: int        # 必填：响应报文总长（雷蛇 90 / HID++ 20）

    needs_probe: bool = True   # 是否需要 (report_id, tx_id) 探测（HID++ 为 False）
    # DPI / 轮询是否放在"板载配置"里、没有单独指令（罗技 {8100} 是这种）：
    # True 时 device 层的 get_dpi/set_dpi/get_poll/set_poll 交给协议的
    # read_dpi / write_dpi / read_poll / write_poll 走多步流程。
    # 以前这里没有这个开关，device 用 hasattr 探测那四个方法——方法拼错或漏实现
    # 时探测不到，整条板载路径会被**静默跳过**（2026-09-17 审查发现）。
    onboard_profiles: bool = False

    supports_dpi_stages: bool = True
    supports_poll_v2: bool = True

    # ---------- 设备元数据（catalog 据此自动展开 DEVICE_DB） ----------
    pids: list[int] = field(default_factory=list)   # 该型号的全部通信 PID
    conn: tuple[str, ...] = ("2.4G",)                # 连接方式（展示用）
    battery: bool = True                             # 是否有电池（无线 True / 纯有线 False）
    battery_max: int = 255             # decode_battery 的满量程（雷蛇裸 0-255）
    dpi_max: int = 30000                             # 官方最大 DPI（写档位上限校验）
    keymap_layout: str = "standard"                 # 键位页用的按键布局 id（见 hardware/keymap.py）

    # ---------- 品牌字节：基类一律不实现 ----------
    # 基类只是"字段 + 元数据"的容器，**不含任何品牌的字节**。各品牌自建子类：
    # 雷蛇见 RazerProfile、罗技见 HidppProfile，新品牌再写一个。
    # 这样新增品牌时若漏写某个方法，会明确得到"未实现"，而不是悄悄拿雷蛇的
    # 字节去碰别的设备——那正是本文件最忌讳的事（见文件头 docstring）。
    def _unimplemented(self, what: str) -> NotImplementedError:
        return NotImplementedError(
            f"{type(self).__name__} 未实现 {what}：基类不含任何品牌字节，"
            f"请在品牌子类（RazerProfile / HidppProfile / 新建）里实现")

    # ---------- 连接期状态 ----------
    def reset_conn(self) -> None:
        """清掉只属于"本次连接"的缓存，由用到连接期状态的子类重写。"""

    def for_connection(self) -> "ProtocolProfile":
        """返回专属于一台设备 / 一次连接的协议副本。

        实例是模块级单例（`_REGISTRY` 定义即注册），型号元数据共享没问题，
        **连接期状态不能共享**：HID++ 的 feature index 是每次连接协商出来的、
        DPI/轮询还有 0.5s 的 TTL 读缓存——两只同型号鼠标，或切换设备时在途的
        读写会共用同一份，把 A 的读数答给 B、或被 B 的重连清空掉 A 的协商结果。
        所以 MouseDevice 取协议一律先要一份自己的副本。
        """
        clone = copy.copy(self)       # 浅拷贝：只共享不可变的型号元数据
        clone.reset_conn()
        return clone

    # ---------- 报文寻址：雷蛇式默认值，HID++ 子类按首字节改写 ----------
    def report_id_for(self, req: bytes, default: int) -> int:
        """这一帧用的 report id。

        `default` 是**连接探测出来的**设备 report id（`MouseDevice.report_id`）——
        它不是协议的属性（同一协议在不同鼠标上探测结果可能不同），所以由调用方
        传进来。雷蛇直接用它；HID++ 的 report id 就在报文首字节（0x10/0x11），
        忽略 default。
        """
        return default

    def resp_len_for(self, req: bytes) -> int:
        """这一帧期望的响应长度。雷蛇固定 report_len（90）；HID++ 短 7 / 长 20。"""
        return self.report_len

    # ---------- 板载配置型协议（onboard_profiles=True）必须实现 ----------
    def negotiate(self, raw_exchange) -> bool:
        """连接后协商（HID++ 要问出 feature index）。"""
        raise self._unimplemented("negotiate")

    def read_dpi(self, raw_exchange) -> tuple[int, int]:
        raise self._unimplemented("read_dpi")

    def write_dpi(self, raw_exchange, dpi: int) -> None:
        raise self._unimplemented("write_dpi")

    def read_poll(self, raw_exchange) -> int | None:
        raise self._unimplemented("read_poll")

    def write_poll(self, raw_exchange, rate: int) -> None:
        raise self._unimplemented("write_poll")

    def build_report(self, cmd_class: int, cmd_id: int, data: bytes = b"",
                     tx_id: int = 0x1F) -> bytearray:
        """构造完整命令报文——由品牌子类实现。"""
        raise self._unimplemented("build_report")

    def is_success(self, resp: bytearray | bytes) -> bool:
        """响应是否表示成功——由品牌子类实现。"""
        raise self._unimplemented("is_success")

    def decode_fw(self, resp: bytearray) -> str:
        raise self._unimplemented("decode_fw")

    def encode_set_dpi(self, dpi: int, store: int = 1) -> bytes:
        raise self._unimplemented("encode_set_dpi")

    def decode_get_dpi(self, resp: bytearray) -> tuple[int, int]:
        raise self._unimplemented("decode_get_dpi")

    def encode_dpi_stages(self, stages: list[int], store: int = 1) -> bytes:
        raise self._unimplemented("encode_dpi_stages")

    def encode_set_poll(self, rate: int) -> bytes:
        raise self._unimplemented("encode_set_poll")

    def decode_get_poll(self, resp: bytearray) -> int | None:
        raise self._unimplemented("decode_get_poll")

    def encode_set_poll_v2(self, rate: int) -> bytes:
        raise self._unimplemented("encode_set_poll_v2")

    def decode_battery(self, resp: bytearray) -> int:
        raise self._unimplemented("decode_battery")

    def encode_idle_timeout(self, minutes: int) -> bytes:
        raise self._unimplemented("encode_idle_timeout")

    def encode_low_battery(self, percent: int) -> bytes:
        raise self._unimplemented("encode_low_battery")


# ---------------------------------------------------------------------------
# 雷蛇标准协议 —— 所有雷蛇型号共用的字节实现
# ---------------------------------------------------------------------------
# 字节布局经真机核对（毒蝰 V3 极速版逐条实测）。各型号的差异（tx_id 候选、
# 能力开关）不在这里，写在各自实例的字段上。
# ---------------------------------------------------------------------------
@dataclass
class RazerProfile(ProtocolProfile):
    """雷蛇标准协议（90 字节帧 + 命令号 + CRC）：雷蛇型号一律实例化本类。"""

    # 雷蛇品牌通用默认：VID、帧长、Report 编号候选（雷蛇机制各型号一致）。
    # tx_candidates / fallback_tx 因型号而异（毒蝰 0x1F、炼狱蝰蛇 0xFF），
    # 由各实例显式声明，不在这里设默认。
    vid: int = 0x1532
    report_len: int = 90
    report_id_candidates: list[int] = field(default_factory=lambda: [2, 0])

    # ---------- 报文帧（雷蛇标准 90 字节帧，见 frame.razer） ----------
    def build_report(self, cmd_class: int, cmd_id: int, data: bytes = b"",
                     tx_id: int = 0x1F) -> bytearray:
        """构造完整命令报文（雷蛇标准 90 字节帧，见 frame.razer）。"""
        return build_report(cmd_class, cmd_id, data, tx_id)

    def is_success(self, resp: bytearray | bytes) -> bool:
        """设备响应是否表示成功（雷蛇标准：resp[0]==0x02）。"""
        return bool(resp) and resp[0] == 0x02

    # ---------- 固件版本 ----------
    def decode_fw(self, resp: bytearray) -> str:
        """CMD_GET_FW 响应 → 版本号字符串（resp[9]=主，resp[10]=次）。"""
        return f"{resp[9]}.{resp[10]}"

    # ---------- DPI（class 0x04） ----------
    def encode_set_dpi(self, dpi: int, store: int = 1) -> bytes:
        """CMD_SET_DPI_XY 参数：[store, X_hi, X_lo, Y_hi, Y_lo, 0x00, 0x00]。

        注意 X、Y 之间没有 0x00 占位（老款布局才有），否则 Y 会被设备解析成
        [0x00, Y_hi] = dpi>>8 的垃圾值。
        """
        v = int(dpi)
        return bytes([store & 0xFF,
                      (v >> 8) & 0xFF, v & 0xFF,
                      (v >> 8) & 0xFF, v & 0xFF,
                      0x00, 0x00])

    def decode_get_dpi(self, resp: bytearray) -> tuple[int, int]:
        """CMD_GET_DPI_XY 响应 → (x, y)。

        resp[8]=store 档位回显；X = resp[9..10]，Y = resp[11..12]，各 16 位
        大端、连续排列（中间无占位字节，与 encode_set_dpi 对称）。
        """
        x = (resp[9] << 8) | resp[10]
        y = (resp[11] << 8) | resp[12]
        return x, y

    def encode_dpi_stages(self, stages: list[int], store: int = 1) -> bytes:
        """CMD_SET_DPI_STAGES 参数：[store, 档1_hi, 档1_lo, … 档5_lo]（5 档连排）。"""
        data = bytearray([store & 0xFF])
        for v in stages[:5]:
            v = int(v)
            data += bytes([(v >> 8) & 0xFF, v & 0xFF])
        return bytes(data)

    # ---------- 轮询率（class 0x00） ----------
    def encode_set_poll(self, rate: int) -> bytes:
        """CMD_SET_POLL 参数：指令值 = 1000 / Hz（125→0x08 … 1000→0x01）。

        不认识的速率**报错**：静默兜底成某个指令值等于替用户编一个轮询率，
        与 get_poll"读不到就说读不到"的口径矛盾（2026-09-18 审查发现）。
        """
        if rate not in POLL_TO_VAL:
            raise ValueError(f"不支持的轮询率 {rate} Hz（可选: "
                             f"{'/'.join(map(str, sorted(POLL_TO_VAL)))}）")
        return bytes([POLL_TO_VAL[rate]])

    def decode_get_poll(self, resp: bytearray) -> int | None:
        """CMD_GET_POLL 响应 → Hz；resp[8] 不在映射里（如 2000Hz 档）返回 None。"""
        return VAL_TO_POLL.get(resp[8])

    def encode_set_poll_v2(self, rate: int) -> bytes:
        """CMD_SET_POLL_V2 参数：16 位大端 Hz（2000/4000/8000，需 HyperPolling）。"""
        return bytes([(rate >> 8) & 0xFF, rate & 0xFF])

    # ---------- 电源（class 0x07） ----------
    def decode_battery(self, resp: bytearray) -> int:
        """CMD_GET_BATTERY 响应 → 电量。resp[8]=参数回显，resp[9]=原始电量 0-255。"""
        return resp[9]

    def encode_idle_timeout(self, minutes: int) -> bytes:
        """CMD_SET_IDLE_TIMEOUT 参数：[0x00, 0x00, 分钟, 0x00, 0x00]。"""
        return bytes([0x00, 0x00, minutes & 0xFF, 0x00, 0x00])

    def encode_low_battery(self, percent: int) -> bytes:
        """CMD_SET_LOW_BATTERY 参数：[0x00, 0x00, 阈值%, 0x00]。"""
        return bytes([0x00, 0x00, percent & 0xFF, 0x00])


# ---------------------------------------------------------------------------
# 毒蝰 V3 系列 —— 毒蝰 V3 极速版 (HyperSpeed) / 毒蝰 V3 Pro
# ---------------------------------------------------------------------------
# 通信地址（USB VID:PID，VID 统一 0x1532，见 frame.razer）：
#   0x00B8  Viper V3 HyperSpeed —— 2.4G 接收器（真机验证）
#   0x00C0  Viper V3 Pro —— 有线直连
#   0x00C1  Viper V3 Pro —— 2.4G 接收器
# HID 接口选择：控制接口的特征是 Feature Report 长度 >= 90
# （HyperSpeed 的 usage_page 是 0x0001 但 feat=91，见 device._connect_via）。
# 事务 ID：本系列用 0x1F（探测顺序的首位即命中）。
# 字节布局与雷蛇标准协议一致（RazerProfile 的方法即本系列的实测结果）：
#   SET_DPI   [store, X_hi, X_lo, Y_hi, Y_lo, 0x00, 0x00]   （00B8 真机验证）
#   GET_DPI   X=resp[9..10]、Y=resp[11..12] 连排             （00B8 真机验证）
#   BATTERY   resp[9]=原始电量 0-255（0xAF≈69%）             （00B8 真机验证）
#   POLL      指令值 = 1000/Hz
# 高轮询率：2.4G 接收器最高 8000Hz；本程序 500/1000 走标准命令，2000 走
# CMD_SET_POLL_V2 实验命令（未实测、且读不回，GET_POLL 只认 125-1000）。
VIPER_V3_HS = RazerProfile(
    key="viper_v3_hs",
    pids=[0x00B8],
    conn=("2.4G",), battery=True, dpi_max=30000,
    product="毒蝰 V3 极速版",
    tx_candidates=[0x1F, 0xFF, 0x3F, 0x00],   # 本系列 0x1F 首位即命中
    fallback_tx=0x1F,
)

VIPER_V3_PRO = RazerProfile(
    key="viper_v3_pro",
    pids=[0x00C0, 0x00C1],
    conn=("有线", "2.4G"), battery=True, dpi_max=35000,
    product="毒蝰 V3 Pro",
    tx_candidates=[0x1F, 0xFF, 0x3F, 0x00],   # 本系列 0x1F 首位即命中
    fallback_tx=0x1F,
)


# ---------------------------------------------------------------------------
# 炼狱蝰蛇 标准版 —— DeathAdder Essential（含 2021 / 白色版）
# ---------------------------------------------------------------------------
# 通信地址（USB VID:PID，VID 统一 0x1532）：
#   0x006E  DeathAdder Essential
#   0x0098  DeathAdder Essential (2021)
#   0x0071  DeathAdder Essential (White Edition)
# 三个 PID 是同型号的不同批次 / 配色版本。
# 事务 ID：固定 0xFF（探测自动命中）——兜底值也取 0xFF，与毒蝰不同。
# 字节布局：DPI / 轮询与毒蝰 V3 同一套报文（RazerProfile 的方法直接适用）；
# 无 DPI 键，档位全由软件设置。
# 能力差异：
#   - 无电池（纯有线）：catalog.py 里 battery=False，心跳改用 GET_FW；
#   - 不支持写 5 档档位（设备不接受该命令）→ supports_dpi_stages=False，
#     device.set_dpi_stages 直接抛"不支持"，UI 引导改用「档 N」；
#   - 不支持 HyperPolling（纯有线 6400 DPI 入门型号）→ supports_poll_v2=False。
DEATHADDER_ESSENTIAL = RazerProfile(
    key="deathadder_essential",
    pids=[0x006E, 0x0098, 0x0071],
    conn=("有线",), battery=False, dpi_max=6400,
    product="炼狱蝰蛇 标准版",
    tx_candidates=[0xFF, 0x1F, 0x3F, 0x00],
    fallback_tx=0xFF,
    supports_dpi_stages=False,
    supports_poll_v2=False,
)


# ---------------------------------------------------------------------------
# 罗技 G PRO X SUPERLIGHT 2（GPW 三代）—— HID++ 4.2
# ---------------------------------------------------------------------------
# 通信地址：接收器 USB 046D:C54D（已登记）。鼠标本体另有 usbid C09B /
#           WPID 40A9，有线 PID 未核实，暂不登记。
# 协议模型与雷蛇不同（详见 HidppProfile）：feature index 需连接后协商。
# DPI 与轮询率没有单独指令，都放在板载配置 {8100} 里，读写都按整块进行。
# 已实现：识别、读电量/固件、读写 DPI 与轮询率（写入前做 CRC 自检）。
# 限制：轮询率仅 125/250/500/1000，更高档位编码待确认。
# ---------------------------------------------------------------------------
@dataclass
class HidppProfile(ProtocolProfile):
    """HID++ 4.2 协议基类（罗技等）：feature index 连接后协商，帧与雷蛇不同。

    与雷蛇式协议的本质差异：
      - 帧：短报文 report_id=0x10（7 字节）/ 长报文 0x11（20 字节）；头 4 字节
        = [report_id, device_index, feature_index, function | sw_id]（function
        本身已含左移 4 位，低 4 位是 software id），其后是参数（短 3 / 长 16）。
      - 功能靠 feature 寻址，而 feature index **不是固定常量**，必须连接后经
        ROOT(0x0000) → FEATURE SET(0x0001) 协商枚举出来（见 negotiate）。
      - 因此本类不需要雷蛇式 tx_id 探测（needs_probe=False）。

    已实现的标准 feature：
      0x1004 UNIFIED BATTERY   func 0x10 → 电量%（响应 param0，0-100）
      0x0003 DEVICE FW VERSION func 0x10 → 名称 + 主/次版本
    DPI / 轮询率都放在板载配置 {8100} 里（没有单独的指令），见下方板载配置段：
    读写均已实现（写入前 CRC 自检）；轮询率仅支持 125/250/500/1000，更高档位待确认。

    **未在真机验证**（本项目手上只有雷蛇设备）：帧格式与短/长报文判据已对照
    Solaar / libratbag / Linux 内核 hidpp 三处实现核对（见 frame/hidpp.py 顶部），
    但传输通道是 feature report —— 两家参考实现走的都是中断读写。接上真机若
    `HidD_SetFeature` 报 ERROR_INVALID_FUNCTION（LastError=1），说明该集合把
    0x10/0x11 声明成了 Input/Output 报表，需要给 backends 补一条中断通道。
    """

    vid: int = 0x046D          # 罗技品牌默认 VID（对应 RazerProfile 的 0x1532）
    sw_id: int = 0x08          # software id（报文头 byte3 低 4 位）
    dev_index: int = 0x01      # 设备索引（接收器后第 1 个设备；接收器自身 0xFF）
    needs_probe: bool = False  # HID++ 无 tx_id 概念，跳过雷蛇式探测
    # DPI/轮询不在指令里，走板载配置 {8100} 的多步读写（基类那四个方法在此实现）
    onboard_profiles: bool = True
    report_len: int = 20       # 长报文长度（短报文 7，见 resp_len_for）
    # 探测参数为基类必填字段；HID++ 不用探测，重声明为中性空值
    tx_candidates: list[int] = field(default_factory=list)
    report_id_candidates: list[int] = field(default_factory=list)
    fallback_tx: int = 0
    _feat_index: dict[int, int] = field(default_factory=dict)
    _cache: dict[str, tuple[float, object]] = field(default_factory=dict)
    battery_max: int = 100   # UNIFIED BATTERY 直接给百分比，不是雷蛇式裸值

    def reset_conn(self) -> None:
        """清掉本次连接的协商结果与读缓存（见 for_connection）。

        `_feat_index` 是 negotiate 按设备逐一枚举出来的，`_cache` 是 0.5s 的
        板载配置读缓存：两者都属于"这一台设备、这一次连接"，跟着模块级单例
        共享就会跨设备串味。
        """
        self._feat_index = {}
        self._cache = {}

    # (cmd_class, cmd_id) → (feature_id, function, params)
    # 雷蛇的命令常量在 build_report 里被翻译成 HID++ 的 (feature, function)。
    def feature_commands(self) -> dict[tuple[int, int], tuple[int, int, bytes]]:
        """本型号支持的「雷蛇命令 → HID++ feature/function」映射。

        参数长度决定**帧长**（≤3 字节发短报文 7、否则发长报文 20，见
        frame.hidpp），而响应长度是跟着请求长度走的（`resp_len_for`）。所以凡是
        需要读的字段超出短报文能装下的范围，就必须用长报文请求：
        - FW 版本要读 `resp[5:12]`（需要 ≥12 字节），短报文只有 7 字节 → 恒失败。
          旧写法参数只有 1 字节，于是"请求 7 字节、却要求 12 字节响应"，
          `decode_fw` 永远抛错（2026-09-17 审查发现）。故给 4 字节参数强制长报文。
        """
        return {
            (CLASS_POWER, CMD_GET_BATTERY): (0x1004, 0x10, b""),
            # 参数只用于"凑成长报文"：HID++ 的实体查询不需要真载荷，但必须够 4 字节
            (CLASS_DEVICE, CMD_GET_FW): (0x0003, 0x10, b"\x00\x00\x00\x00"),
        }

    # ---------- 组帧 ----------
    def _frame(self, feature_index: int, function: int,
               params: bytes = b"") -> bytearray:
        """组 HID++ 帧（帧布局见 frame.hidpp.build_hidpp_frame）。

        这里只补上本实例的 device_index 与 software id——帧长、report id、
        function 掩码等规则都在 frame.hidpp，避免两处各写一份。
        """
        return build_hidpp_frame(self.dev_index, feature_index, function,
                                 params, self.sw_id)

    def build_report(self, cmd_class: int, cmd_id: int, data: bytes = b"",
                     tx_id: int = 0x1F) -> bytearray:
        """把雷蛇式 (cmd_class, cmd_id) 翻译成 HID++ (feature, function) 帧。"""
        cmd = self.feature_commands().get((cmd_class, cmd_id))
        if cmd is None:
            raise NotImplementedError(
                f"该 HID++ 设备不支持此命令（class=0x{cmd_class:02X} "
                f"cmd=0x{cmd_id:02X}）：DPI/轮询走板载配置的多步流程，"
                f"不走这条单帧路径")
        feature_id, function, params = cmd
        index = self._feat_index.get(feature_id)
        if index is None:
            raise RuntimeError(
                f"feature 0x{feature_id:04X} 未协商（设备未连接或不支持该功能）")
        return self._frame(index, function, params)

    def report_id_for(self, req: bytes, _default: int) -> int:
        """HID++ 的 report id 就在报文首字节（短 / 长），不用探测出来的那个。"""
        return req[0] if req else HIDPP_REPORT_SHORT

    def resp_len_for(self, req: bytes) -> int:
        """短报文回 7 字节、长报文回 20 字节。"""
        return (HIDPP_SHORT_LEN if (req and req[0] == HIDPP_REPORT_SHORT)
                else HIDPP_LONG_LEN)

    def is_success(self, resp: bytearray | bytes) -> bool:
        """HID++ 出错时 feature index 位置会回 0x8F。"""
        if not resp or len(resp) < 3:
            return False
        return resp[2] != HIDPP_ERROR_INDEX

    # ---------- 连接后协商 ----------
    def negotiate(self, raw_exchange) -> bool:
        """经 ROOT → FEATURE SET 枚举出 feature 的 index（HID++ 必做）。

        raw_exchange(req_bytes, report_id) -> response|None，由 MouseDevice 提供。

        **只找本程序用得到的 feature，凑齐就停**：设备多的型号有几十上百个
        feature，逐个问一遍每个都是一次 HID 往返，而这段是在 `connect()` 的锁里
        跑的（调用链见 `device._adopt`）—— 最坏能把界面和状态机一起卡住好几秒
        （2026-09-17 审查发现）。
        """
        self._feat_index = {0x0000: 0x00}      # ROOT 的 index 恒为 0
        self._cache.clear()                    # 重连/换设备后旧读数一律作废
        # **必须把板载配置 0x8100 也算进"要用的 feature"**：它的 index 由设备
        # 决定（不同型号/固件都不一样），而下面一旦把 wanted 凑齐就 break。
        # 以前 wanted 只有电量/固件，于是在 0x8100 排在它们后面的设备上永远
        # 枚举不到 → `_onboard_index()` 报"该设备不支持板载配置功能"，DPI 与
        # 轮询率整条路径全废，看起来还像是设备不支持（2026-10-26 审查发现）。
        wanted = {
            fid for (cls, cid), (fid, _func, _prm) in self.feature_commands().items()
            # 纯有线型号没有电池：别把"读电量"当成必需 feature
            if not (cls == CLASS_POWER and cid == CMD_GET_BATTERY
                    and not self.battery)
        }
        if self.onboard_profiles:
            wanted.add(HIDPP_PAGE_ONBOARD_PROFILES)
        # 1) ROOT.getFeature(0x0001) → FEATURE SET 自己的 index
        resp = raw_exchange(bytes(self._frame(0x00, 0x00,
                                              struct.pack("!H", 0x0001))),
                            HIDPP_REPORT_SHORT)
        if resp is None or len(resp) < 5:
            return False
        fs_index = resp[4]
        if not fs_index:
            return False
        self._feat_index[0x0001] = fs_index
        if not wanted:
            return True
        # 2) FEATURE SET.getCount（func 0x00）→ feature 总数
        resp = raw_exchange(bytes(self._frame(fs_index, 0x00)), HIDPP_REPORT_SHORT)
        if resp is None or len(resp) < 5:
            return False
        count = resp[4]
        # 3) 逐个 index 问出 feature id（func 0x10，参数 = index）
        for index in range(1, count + 1):
            resp = raw_exchange(
                bytes(self._frame(fs_index, 0x10, bytes([index]))), HIDPP_REPORT_SHORT)
            if resp is None or len(resp) < 6:
                continue
            feature_id = (resp[4] << 8) | resp[5]
            self._feat_index.setdefault(feature_id, index)
            if wanted <= set(self._feat_index):
                break                      # 要用的都齐了，别再一个个白问
        # 缺任何一个要用的 feature 都必须如实返回 False：调用方（device._adopt）
        # 把这个返回值当"寻址信息已确认可用"写进 tx_id_verified，进而决定要不要
        # 把设备记进「我的鼠标」。以前这里无条件 True，于是协商只成功一半的设备
        # 也会被当成已连接，之后每次读电量/DPI 都失败。
        missing = wanted - set(self._feat_index)
        if missing:
            log.debug("HID++ 协商缺少 feature：%s",
                      ", ".join(f"0x{f:04X}" for f in sorted(missing)))
        return not missing

    # ---------- 解码（标准 feature，公开规范） ----------
    def decode_battery(self, resp: bytearray) -> int:
        """UNIFIED BATTERY(0x1004) func 0x10：param0 = 电量百分比(0-100)。"""
        if not resp or len(resp) < 5:
            raise RuntimeError(f"电池响应无效: {resp!r}")
        return resp[4]

    def decode_fw(self, resp: bytearray) -> str:
        """DEVICE FW VERSION(0x0003)：名称(3) + 主/次版本 + build。

        ⚠ 字段偏移是按公开规范写的，**从未在罗技真机上核对过**（本机没有罗技
        设备）。请求侧已强制长报文（见 `feature_commands` 的注释），所以 12 字节
        下限本身是自洽的；接上真机后第一件事是拿真实回帧核对这个偏移。
        """
        if not resp or len(resp) < 12:
            raise RuntimeError(
                f"固件响应无效（需要 ≥12 字节，实际 "
                f"{len(resp) if resp else 0} 字节）: {resp!r}")
        _name, major, minor, _build = struct.unpack("!3sBBH", bytes(resp[5:12]))
        return f"{major:02X}.{minor:02X}"

    # ---------- 板载配置（ONBOARD PROFILES）----------
    # 本型号把 DPI / 轮询率都放在板载配置里，没有单独的设置指令，所以要
    # 「整块读出 → 改字段 → 算校验 → 整块写回」。
    # 帧布局、页号（0x8100）与各命令号统一在 frame.hidpp，这里不重复定义。

    @staticmethod
    def _crc_ccitt(data: bytes) -> int:
        """CRC-CCITT：初值 0xFFFF，多项式 0x1021，高位在前（非反射）。

        Rust 实现（frame.hidpp.crc_ccitt → rust/src/lib.rs），selftest 里带
        独立 Python 参考实现做逐值比对。
        """
        return crc_ccitt(data)

    def _onboard_index(self) -> int:
        fi = self._feat_index.get(HIDPP_PAGE_ONBOARD_PROFILES)
        if fi is None:
            raise RuntimeError("该设备不支持板载配置功能")
        return fi

    def _read_sector(self, raw_exchange, index: int) -> bytearray:
        """读一个配置扇区：每次 16 字节，共 16 次（扇区号与偏移均为大端）。"""
        fi = self._onboard_index()
        sector = bytearray(HIDPP_PROFILE_SIZE)
        for off in range(0, HIDPP_PROFILE_SIZE, 16):
            req = self._frame(fi, HIDPP_CMD_ONBOARD_MEM_READ,
                              struct.pack("!HH", index, off))
            resp = raw_exchange(bytes(req), HIDPP_REPORT_LONG)
            if resp is None or len(resp) < 20:
                raise RuntimeError(f"配置读取失败（偏移 {off}）")
            sector[off:off + 16] = resp[4:20]
        return sector

    def _crc_ok(self, sector: bytearray) -> bool:
        """校验读到的配置：本地 CRC 与扇区末尾记录的 CRC 是否一致。

        这是写入前的**自检**：算法或布局若不符，这里就会失败，从而拒绝写入，
        避免把错误数据写进板载配置。
        """
        stored = struct.unpack("!H", bytes(sector[HIDPP_PROFILE_SIZE - 2:]))[0]
        return self._crc_ccitt(bytes(sector[:HIDPP_PROFILE_SIZE - 2])) == stored

    def _current_profile(self, raw_exchange) -> int:
        """当前生效的配置序号（设备从 1 开始计数，这里换算成 0 起）。"""
        fi = self._onboard_index()
        resp = raw_exchange(
            bytes(self._frame(fi, HIDPP_CMD_ONBOARD_GET_CURRENT)), HIDPP_REPORT_SHORT)
        if resp is None or len(resp) < 5:
            raise RuntimeError("读取当前配置序号失败")
        return max(0, resp[4] - 1)

    def _write_sector(self, raw_exchange, index: int, sector: bytearray) -> bool:
        """写回一个配置扇区：起始(0x60) → 数据(0x70 ×16) → 结束(0x80)。"""
        fi = self._onboard_index()
        if not self._crc_ok(sector):
            return False
        start = self._frame(fi, HIDPP_CMD_ONBOARD_WRITE_START,
                            struct.pack("!HHH", index, 0, HIDPP_PROFILE_SIZE))
        if raw_exchange(bytes(start), HIDPP_REPORT_LONG) is None:
            return False
        for off in range(0, HIDPP_PROFILE_SIZE, 16):
            data = self._frame(fi, HIDPP_CMD_ONBOARD_WRITE_DATA,
                               bytes(sector[off:off + 16]))
            if raw_exchange(bytes(data), HIDPP_REPORT_LONG) is None:
                return False
        end = self._frame(fi, HIDPP_CMD_ONBOARD_WRITE_END)
        return raw_exchange(bytes(end), HIDPP_REPORT_SHORT) is not None

    # ---------- 多步操作（供 MouseDevice 调用，见 device.get_dpi 等）----------
    # 读一次配置要十几轮 HID 往返（约 0.3 秒），而后台状态机每 0.2 秒就要读
    # 一次 DPI 来检测物理切档——读取比节拍还慢会把线程占满、连心跳一起拖慢。
    # 故缓存一小段时间：代价是切档检测最多延迟 CACHE_TTL 秒，换来心跳不被拖垮。
    CACHE_TTL = 0.5

    def _cached(self, key: str, loader):
        """带 TTL 的读取缓存（写入后必须 _invalidate 失效）。"""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self.CACHE_TTL:
            return hit[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def _invalidate(self, key: str) -> None:
        """写入后清掉对应缓存，避免读到改写前的旧值。"""
        self._cache.pop(key, None)

    @staticmethod
    def _cur_stage(sector) -> int:
        """板载扇区里的「当前档位」下标（偏移 3，0-4）。

        越界说明扇区读歪了（偏移猜错 / 设备格式不同）。这时**不能**当成最近的一
        档凑数：那会把第 5 档的值报成当前 DPI、还会往错误的地址写回。如实报错。
        """
        i = sector[3]
        if i > 4:
            raise RuntimeError(
                f"板载配置的当前档位值异常（偏移 3 = {i}，应为 0-4）："
                f"无法确定当前 DPI，已拒绝读写")
        return i

    def read_dpi(self, raw_exchange) -> tuple[int, int]:
        """读当前档位的 DPI（x, y）。"""
        return self._cached("dpi", lambda: self._read_dpi_now(raw_exchange))

    def _read_dpi_now(self, raw_exchange) -> tuple[int, int]:
        sector = self._read_sector(raw_exchange, self._current_profile(raw_exchange))
        if not self._crc_ok(sector):
            raise RuntimeError("板载配置校验不符：无法确认当前 DPI")
        base = 4 + 5 * self._cur_stage(sector)
        return (struct.unpack_from("<H", sector, base)[0],
                struct.unpack_from("<H", sector, base + 2)[0])

    def write_dpi(self, raw_exchange, dpi: int) -> None:
        """设置当前档位的 DPI（x、y 同值）。

        写入前先做 CRC 自检：对不上就抛错，绝不带错误数据写回。
        """
        # 配置序号只问一次：既省一轮往返，也避免读与写用到不同的序号
        index = self._current_profile(raw_exchange)
        sector = self._read_sector(raw_exchange, index)
        if not self._crc_ok(sector):
            raise RuntimeError(
                "板载配置校验不符，已拒绝写入（避免写坏鼠标内的配置）")
        i = self._cur_stage(sector)
        base = 4 + 5 * i
        v = max(100, min(int(dpi), self.dpi_max))
        struct.pack_into("<HH", sector, base, v, v)
        crc = self._crc_ccitt(bytes(sector[:HIDPP_PROFILE_SIZE - 2]))
        struct.pack_into("!H", sector, HIDPP_PROFILE_SIZE - 2, crc)
        if not self._write_sector(raw_exchange, index, sector):
            raise RuntimeError("写入板载配置失败")
        self._invalidate("dpi")

    # 板载扇区里轮询率字段的合法取值：存的是「1000/Hz」（125→8、250→4、500→2、
    # 1000→1）。用来甄别"读到的字节到底是不是轮询率"。
    # 不写注解：本类是 dataclass，注解会变成字段（它们是常量，不是每实例状态）。
    # 只声明一次 Hz，编码值由它推导——两处各写一遍时改档位必然漏改一处。
    POLL_RATES = (125, 250, 500, 1000)          # 无注解：加了注解就变成 dataclass 字段
    POLL_CODES = frozenset(1000 // r for r in POLL_RATES)

    def read_poll(self, raw_exchange) -> int | None:
        """读当前轮询率：偏移 0 无线、偏移 1 有线，两项都存「1000/Hz」。

        本型号主要走无线接收器，优先用无线那一项；为 0（未设）则退回有线。

        TODO(需真机核对)：扇区内各字段的偏移是按"0x07 号配置格式"推出来的，
        而本程序没实现命令 0x00「读配置描述」（见 frame/hidpp.py），所以没有
        运行时校验。有罗技真机时请核对 0/1/2 三个字节：若 byte 0 其实是格式号，
        无线/有线就该往后挪到 1/2，DPI 段的 base 也要跟着复核。
        在此之前，这里只认 POLL_CODES 里的值，其余一律按"读不出"处理，
        绝不拿 1000//任意字节 编一个轮询率出来。
        """
        return self._cached("poll", lambda: self._read_poll_now(raw_exchange))

    def _read_poll_now(self, raw_exchange) -> int | None:
        sector = self._read_sector(raw_exchange, self._current_profile(raw_exchange))
        if not self._crc_ok(sector):
            raise RuntimeError("板载配置校验不符：无法确认当前轮询率")
        for off in (0, 1):
            raw = sector[off]
            if raw in self.POLL_CODES:
                return 1000 // raw
        return None                       # 值不像轮询码 → 如实报"读不出"

    def write_poll(self, raw_exchange, rate: int) -> None:
        """设置轮询率（写入前同样做 CRC 自检）。

        无线（偏移 0）与有线（偏移 1）写同一个值：无法确知当前走接收器还是
        直连，只写一项会出现"设了没生效"。
        仅支持 125/250/500/1000——更高档位在该字段的编码待确认。
        """
        if rate not in self.POLL_RATES:
            raise RuntimeError(f"暂不支持 {rate}Hz：该档位在板载配置里的编码待确认")
        index = self._current_profile(raw_exchange)   # 同样只问一次
        sector = self._read_sector(raw_exchange, index)
        if not self._crc_ok(sector):
            raise RuntimeError(
                "板载配置校验不符，已拒绝写入（避免写坏鼠标内的配置）")
        # 落笔前再确认一次"这两个字节确实是轮询率"：偏移布局只是推断（见
        # read_poll 的 TODO），而写完会重算 CRC——真把格式号之类的字节改了，
        # 校验和反而变成"自洽"，事后谁都看不出来。只允许 0（未设）或合法码。
        for off in (0, 1):
            cur = sector[off]
            if cur and cur not in self.POLL_CODES:
                raise RuntimeError(
                    f"板载配置偏移 {off} 的当前值 {cur} 不是合法的轮询码"
                    f"（{'/'.join(map(str, sorted(self.POLL_CODES)))}），"
                    f"说明该型号的扇区布局与本实现假设不符，已拒绝写入")
        raw = 1000 // rate
        sector[0] = raw      # 无线
        sector[1] = raw      # 有线
        crc = self._crc_ccitt(bytes(sector[:HIDPP_PROFILE_SIZE - 2]))
        struct.pack_into("!H", sector, HIDPP_PROFILE_SIZE - 2, crc)
        if not self._write_sector(raw_exchange, index, sector):
            raise RuntimeError("写入板载配置失败")
        self._invalidate("poll")

    def encode_set_poll_v2(self, rate: int) -> bytes:
        raise NotImplementedError(
            f"暂不支持 {rate}Hz：高轮询率档位在本型号板载配置里的编码待确认")


GPW3_SUPERLIGHT2 = HidppProfile(
    key="gpw3_superlight2",
    pids=[0xC54D],              # LIGHTSPEED 接收器（已核实）
    conn=("2.4G",), battery=True, dpi_max=44000,   # 官方 100-44000 DPI
    product="G PRO X SUPERLIGHT 2",
    supports_dpi_stages=False,  # 不适用：雷蛇的 5 档位模型，罗技走单值板载配置
    supports_poll_v2=False,     # 不适用：poll_v2 是雷蛇 HyperPolling 专用路径
)


# ---------------------------------------------------------------------------
# 注册表：key → 协议条目（catalog.DeviceInfo.protocol 引用这里的 key）。
# 没有兜底条目：查不到 key 就是没有收录，device.py 拒绝收发。
# ---------------------------------------------------------------------------
def _collect_registry() -> dict[str, ProtocolProfile]:
    """import 时自动收集本模块所有 ProtocolProfile 实例（定义即注册）。

    这样新增一只鼠标只需加一个模块级实例，无需手动加进任何列表；也避免
    出现"实例定义了却忘加进 _REGISTRY"导致扫得到 PID 却连不上的情况。
    只收集实例（ProtocolProfile 类本身不被 isinstance 命中）。
    """
    reg: dict[str, ProtocolProfile] = {}
    for _obj in list(globals().values()):
        if isinstance(_obj, ProtocolProfile):
            reg[_obj.key] = _obj
    return reg


_REGISTRY: dict[str, ProtocolProfile] = _collect_registry()


def get_protocol(key: str | None) -> ProtocolProfile | None:
    """按 key 取协议条目；key 为空或未注册返回 None（未知协议，不兜底）。"""
    if not key:
        return None
    return _REGISTRY.get(key)


def all_protocols() -> dict[str, ProtocolProfile]:
    """全部已注册协议（key → 条目），诊断/展示用。"""
    return dict(_REGISTRY)


def all_vids() -> list[int]:
    """全部已收录品牌的 USB VID（去重）。设备扫描/枚举按这个集合过滤——
    新品牌在本文件登记 vid 后即自动被扫到，无需改 backends/device。"""
    return sorted({p.vid for p in _REGISTRY.values()})
