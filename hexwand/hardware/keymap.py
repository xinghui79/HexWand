"""按键布局：键位页在鼠标图上画出来、可点改的按键位置（按协议实例区分）。

软件层改键只拦截本机 OS 能识别的标准按键（左/右/中/侧键1/侧键2/滚轮上/滚轮下），
所以所有鼠标通用的就是这 7 个键；不同型号的差异（侧键数量、有无 DPI 键等）体现在
「这台鼠标在图上画哪些键」——由对应 ProtocolProfile.keymap_layout 决定本文件按
layout id 给出每个键的位置。新增一只鼠标只需在 protocols 指定 keymap_layout（或沿用
"standard"），本文件无需为每个型号写死；架构上即支持「换鼠标换按键图」。

坐标用相对比例（cx, cy 均为 0~1，相对鼠标平面图在界面上的显示矩形），
这样无论图片缩放、不同机型外形如何，热点都按比例贴合图片里的对应区域。
"""

# 标准软件层按键（id -> 默认标签）。固件层设备（后续可能支持）能暴露更多按键，
# 到时再加 layout id，不影响本列表。
STANDARD_BUTTONS: dict[str, str] = {
    "left": "左键",
    "right": "右键",
    "middle": "中键",
    "wheel_up": "滚轮上",
    "wheel_down": "滚轮下",
    "x1": "侧键 1",
    "x2": "侧键 2",
}

# 布局：layout id -> {button_id: (cx, cy)}（相对比例 0~1）
# standard：通用右手俯视鼠标——上方左/右主键，中央滚轮（含上滚/下滚/中键按下），
# 左侧上下两个侧键（X1/X2，对应 OS 的 XBUTTON1/2），符合常见游戏鼠标的侧键排布。
LAYOUTS: dict[str, dict[str, tuple[float, float]]] = {
    "standard": {
        # 坐标以「矢量鼠标本体矩形」为原点（cx 0=最左 1=最右，cy 0=前端 1=后端），
        # 按 remap.MouseMapView 的 **GPW3 模具**矢量图标定（2026-09-17 起键位页
        # 不再用位图，轮廓按 TechPowerUp 卡尺实测的前/腰/后宽度剖面绘制）：
        #   滚轮胶囊 y 0.100~0.225 → 上滚/中按/下滚三点 0.125 / 0.162 / 0.200；
        #   左右键盖区 x 0.06~0.44 与 0.56~0.94、y 0.05~0.35 → 主键点取区域中心
        #   (0.250 / 0.750, 0.170)；
        #   左侧侧键槽 y 0.310~0.410 / 0.430~0.530、x 0.028~0.116 → 槽中心
        #   (0.072, 0.360) / (0.072, 0.480)。
        "left":       (0.250, 0.170),
        "right":      (0.750, 0.170),
        "wheel_up":   (0.500, 0.125),
        "middle":     (0.500, 0.162),
        "wheel_down": (0.500, 0.200),
        "x1":         (0.072, 0.360),
        "x2":         (0.072, 0.480),
    },
}


def layout_for_pid(pid: int | None) -> dict[str, tuple[float, float]]:
    """按 PID 取该鼠标的按键布局（按键位置表）。未收录的 PID 用 standard。"""
    from .catalog import known_device
    from .protocols import get_protocol

    info = known_device(pid)
    layout_id = "standard"
    if info and info.protocol:
        proto = get_protocol(info.protocol)
        if proto is not None:
            # keymap_layout 是协议基类的字段，一定有值；写错名字应当直接 KeyError
            # 报出来，而不是悄悄退化成 standard 让人以为布局就是对的。
            layout_id = proto.keymap_layout or "standard"
    return LAYOUTS[layout_id]
