"""已知鼠标型号库：能力、连接方式、DPI 上限（由 protocols 自动派生）。

DEVICE_DB 不再手写：import 时由 protocols._REGISTRY 的每个协议实例展开——
每个 (协议实例.pids 中的 PID) 生成一条 DeviceInfo（conn/battery/dpi_max/
product/protocol 全部取自该协议实例）。因此**新增一只鼠标只需在 protocols.py
加一个 ProtocolProfile 实例**（带 pids 与设备元数据），本文件无需改动，UI 的
支持列表、电量卡片、DPI 上限校验会自动跟上。
"""

from __future__ import annotations

from dataclasses import dataclass

from .keymap import LAYOUTS
from .protocols import ProtocolProfile, get_protocol, all_protocols


@dataclass(frozen=True)
class DeviceInfo:
    """已知型号库条目（由 protocols 的协议实例自动生成，字段含义见那里）。

    battery          是否有电池：无线型号 True；纯有线型号 False（无电量卡片、
                     在线心跳改用 GET_FW 而非读电量）。
    conn             连接方式（展示用，可多选）："2.4G" / "蓝牙" / "有线"。
    dpi_max          官方最大 DPI（写档位时的上限校验）。
    product          型号名：设备页显示它，也是聚合键（同一只鼠标的
                     有线/无线多个 PID 共用，会被合成一行）。
    protocol         protocols.py 里的协议条目 key（该鼠标的私有协议字节）；
                     同一协议族的多个 PID 填同一个 key。未收录的 PID 不兜底，
                     protocol_for 返回 None，device 拒绝收发。
    """

    pid: int
    conn: tuple[str, ...] = ("2.4G",)
    battery: bool = True
    dpi_max: int = 30000
    product: str = ""
    protocol: str = ""
    vid: int = 0            # 该 PID 归属的品牌 VID（见 DEVICE_DB 的撞号校验）


def _build_device_db() -> dict[int, DeviceInfo]:
    """从 protocols 的注册表自动展开：每个协议的每个 PID 生成一条 DeviceInfo。

    新增鼠标只需在 protocols.py 加一个 ProtocolProfile 实例（带 pids 与元数据），
    这里无需改动；DEVICE_DB 在 import 时自动包含新 PID。

    **PID 单独作键是有前提的：全局不能撞号。** PID 由各厂商自己编号，雷蛇与罗技
    完全可能用上同一个数字，而本库按裸 PID 建索引——撞上时后写入的协议会静默顶掉
    先写入的，于是拿罗技的 HID++ 帧去敲一只雷蛇（或反之），错得毫无痕迹。这张表
    是 import 时从代码里的声明派生出来的，撞号属于开发期错误，所以直接在这里
    抛出来：`app.py --check-imports` 会 import 到本模块，build.py 的冒烟步骤
    因此必然拦下，不会带着错表发布。
    """
    db: dict[int, DeviceInfo] = {}
    for _p in all_protocols().values():
        # keymap_layout 决定键位页画哪套按键图；写错名字时 `layout_for_pid`
        # 会 KeyError，而那时已经是构造界面的中途——用户看到的是"程序打不开"。
        # 在 import 期就拦住：`--check-imports` 会 import 本模块，打包冒烟因此
        # 必然发现，不需要真机、也不用等用户点进键位页。
        if _p.keymap_layout not in LAYOUTS:
            raise RuntimeError(
                f"协议 {_p.key!r} 的 keymap_layout={_p.keymap_layout!r} 未登记："
                f"hardware/keymap.py 里只有 {', '.join(sorted(LAYOUTS))}。"
                f"键位页会在启动时 KeyError，请改正 protocols.py 或先登记布局。")
        for _pid in _p.pids:
            hit = db.get(_pid)
            if hit is not None:
                # **任何**重复都要拦，不只是跨品牌：DEVICE_DB 按裸 PID 建索引，
                # 同一 VID 下两个型号用同一个 PID（例如同一接收器 ID 被复制粘贴
                # 到两个协议条目）同样会静默顶掉，且更难发现。
                # 2026-09-17 审查：原来只判 `hit.vid != _p.vid`，与本文档
                # "撞号必抛"的承诺不符。
                raise RuntimeError(
                    f"设备库 PID 重复：0x{_pid:04X} 同时属于 "
                    f"{hit.product}(VID 0x{hit.vid:04X}) 与 "
                    f"{_p.product}(VID 0x{_p.vid:04X})。DEVICE_DB 按裸 PID 建索引，"
                    f"重复会把设备派到另一条协议上收发，必须改键设计后再收录")
            db[_pid] = DeviceInfo(
                pid=_pid, conn=_p.conn, battery=_p.battery,
                dpi_max=_p.dpi_max, product=_p.product, protocol=_p.key,
                vid=_p.vid)
    return db


DEVICE_DB: dict[int, DeviceInfo] = _build_device_db()


def is_known_device(vid: int | None, pid: int | None) -> bool:
    """(VID, PID) 是否收录在设备库里。

    判"认不认识这台设备"要用 VID+PID，不能只看 PID（见 _build_device_db 的
    撞号说明）：只判 PID 时，另一品牌恰好撞上这个号就会被当成"已知型号"，
    进而被 `_connect_via` 按已收录协议去尝试收发。
    """
    info = DEVICE_DB.get(pid) if pid is not None else None
    return info is not None and (vid is None or info.vid == vid)


def known_device(pid: int | None) -> DeviceInfo | None:
    """按 PID 查设备库；未知返回 None。"""
    if pid is None:
        return None
    return DEVICE_DB.get(pid)


def protocol_for(pid: int | None) -> ProtocolProfile | None:
    """按 PID 取该鼠标的协议条目；未知/未填协议返回 None（不兜底）。"""
    info = known_device(pid)
    if info is None or not info.protocol:
        return None
    return get_protocol(info.protocol)


def product_groups() -> list[dict]:
    """按**物理型号**聚合设备库：同一只鼠标的有线/无线多个 PID 合成一组。

    返回 [{product, pids, modes, dpi_max}, ...]，保持库内声明顺序。
    modes 按 ("2.4G", "蓝牙", "有线") 的固定顺序规范化。
    """
    order: list[str] = []
    groups: dict[str, dict] = {}
    for pid, info in DEVICE_DB.items():
        key = info.product
        if key not in groups:
            order.append(key)
            groups[key] = {"product": key, "pids": [], "conns": [],
                           "dpi_max": info.dpi_max}
        g = groups[key]
        g["pids"].append(pid)
        # 同一型号的有线/无线版本 DPI 上限可能不同，取最大：设备页显示的是
        # "这型号能到多少"，用先声明的那个 PID 的值会随 PID 顺序变。
        g["dpi_max"] = max(g["dpi_max"], info.dpi_max)
        for m in info.conn:
            if m not in g["conns"]:
                g["conns"].append(m)
    out: list[dict] = []
    for key in order:
        g = groups[key]
        modes = [m for m in ("2.4G", "蓝牙", "有线") if m in g["conns"]]
        out.append({"product": key, "pids": g["pids"],
                    "modes": " / ".join(modes), "dpi_max": g["dpi_max"]})
    return out
