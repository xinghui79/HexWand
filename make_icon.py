"""图标生成：应用图标（icon.ico / icon.png）与托盘图标（icon_tray.png）。

设计（2026-09-10 重做）：极简扁平风 —— 靛紫对角渐变圆角方块 + 纯白鼠标剪影。
鼠标轮廓用超椭圆（n=3.0）圆角矩形、前脸收窄到 0.90，逼近真实鼠标俯视比例；
中缝与滚轮以「镂空」方式露出底色，形成清晰的左右键分区与滚轮识别点。
全部 4x 超采样绘制后 LANCZOS 缩小，边缘平滑。

应用图标与托盘图标**使用完全相同的比例**（两者必须一致，否则同一程序在
任务栏和托盘里看起来是两个大小）。托盘仍单独出一份 icon_tray.png，
由 tray.py 优先加载。

用法：python make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

# 设计基准尺寸（超采样）
S = 1024
# 主题色：与 UI 的 indigo/violet 一致
GRAD_TOP = (129, 140, 248)      # #818cf8
GRAD_BOTTOM = (109, 40, 217)    # #6d28d9
BODY_COLOR = (255, 255, 255)

# 圆角方块
RADIUS_RATIO = 0.225


# ------------------------------------------------------------------ 基础

def _mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _grad_diag(size: int, c1, c2) -> Image.Image:
    """对角（左上→右下）线性渐变。小图算完再双线性放大（线性函数放大无损）。"""
    base = 256
    img = Image.new("RGBA", (base, base))
    px = img.load()
    denom = 2 * (base - 1)
    for y in range(base):
        for x in range(base):
            px[x, y] = _mix(c1, c2, (x + y) / denom) + (255,)
    return img.resize((size, size), Image.Resampling.BILINEAR)


def _rounded_mask(size: int, box, radius: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


# ------------------------------------------------------------------ 鼠标几何

class _Geo:
    """鼠标俯视几何：超椭圆 n=3.0 圆角矩形，前脸收窄到 FRONT。"""

    N = 3.0          # 超椭圆指数（越大越接近圆角矩形）
    FRONT = 0.90     # 前脸宽度相对最大宽度的比例
    KINK = 0.50      # 收窄到最大宽度的位置（0=前端，1=尾端）

    def __init__(self, size: int,
                 w_ratio: float = 0.400, h_ratio: float = 0.620):
        self.S = size
        self.cx = size / 2
        self.w = size * w_ratio
        self.h = size * h_ratio
        self.ty = (size - self.h) / 2
        self.by = self.ty + self.h
        self.seam_w = self.w * 0.052
        self.wheel_w = self.w * 0.170
        self.wheel_h = self.h * 0.185
        self.wheel_top = self.ty + self.h * 0.115
        self.wheel_cy = self.wheel_top + self.wheel_h / 2
        self.seam_bot = self.wheel_cy

    def _half_width(self, t: float) -> float:
        u = 2.0 * t - 1.0
        base = max(0.0, 1.0 - abs(u) ** self.N) ** (1.0 / self.N)
        if t < self.KINK:
            taper = self.FRONT + (1 - self.FRONT) * (t / self.KINK) ** 0.85
        else:
            taper = 1.0 - 0.04 * ((t - self.KINK) / (1 - self.KINK)) ** 2
        return self.w / 2.0 * base * taper

    def body_mask(self) -> Image.Image:
        n = 360
        right, left = [], []
        for i in range(n + 1):
            t = i / n
            y = self.ty + t * self.h
            hw = self._half_width(t)
            right.append((self.cx + hw, y))
            left.append((self.cx - hw, y))
        m = Image.new("L", (self.S, self.S), 0)
        ImageDraw.Draw(m).polygon(right + list(reversed(left)), fill=255)
        return m

    def cut_mask(self) -> Image.Image:
        """镂空区：锥形收窄的中缝 + 滚轮。"""
        m = Image.new("L", (self.S, self.S), 0)
        d = ImageDraw.Draw(m)

        steps = 120
        span = self.seam_bot - self.ty
        for i in range(steps + 1):
            t = i / steps
            y = self.ty + span * t
            w = self.seam_w * (1.0 - 0.30 * t ** 1.2)
            d.rectangle((self.cx - w / 2, y, self.cx + w / 2,
                         y + span / steps + 1), fill=255)

        d.rounded_rectangle((self.cx - self.wheel_w / 2, self.wheel_top,
                             self.cx + self.wheel_w / 2,
                             self.wheel_top + self.wheel_h),
                            radius=self.wheel_w / 2, fill=255)
        return m


# ------------------------------------------------------------------ 绘制

def _draw(size: int) -> Image.Image:
    g = _Geo(size)
    body = g.body_mask()
    bg = _grad_diag(size, GRAD_TOP, GRAD_BOTTOM)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    radius = int(size * RADIUS_RATIO)
    img.paste(bg, (0, 0), _rounded_mask(size, (0, 0, size - 1, size - 1),
                                        radius))

    # 白色剪影
    img.paste(Image.new("RGBA", (size, size), BODY_COLOR + (255,)), (0, 0), body)

    # 中缝 + 滚轮：镂空露出底色
    cut = ImageChops.multiply(g.cut_mask(), body)
    img.paste(bg, (0, 0), cut)

    # 细描边提亮轮廓
    rim = Image.new("L", (size, size), 0)
    ImageDraw.Draw(rim).rounded_rectangle(
        (3, 3, size - 4, size - 4), radius=max(1, radius - 3),
        outline=255, width=max(2, size // 170))
    img.paste(Image.new("RGBA", (size, size), (255, 255, 255, 58)), (0, 0), rim)
    return img


def make_app_icon(size: int = 256) -> Image.Image:
    """应用图标：渐变圆角方块 + 白色鼠标剪影（中缝与滚轮镂空）。"""
    return _draw(S).resize((size, size), Image.Resampling.LANCZOS)


def make_tray_icon(size: int = 256) -> Image.Image:
    """托盘图标：与应用图标同一套比例，内容完全一致。

    历史上这里会把剪影放大 1.145 倍、圆角调小到 0.185，那是为旧版"瘦胶囊"
    鼠标在 16px 下保可辨而做的妥协。新版鼠标本身已加宽到 0.400×0.620，实测
    再放大 14.5% 只会让托盘与应用图标看起来是两个大小（剪影面积差 31%），
    且 16px 下反而更糊，故取消。

    仍然独立出一份文件而不是直接复用 icon.png：底色徽章是必需的——不能用
    「透明底 + 白色图案」，那样在浅色任务栏上会整块看不见。
    """
    return make_app_icon(size)


def main() -> None:
    # 图标统一写进项目根的 assets/（与 core/paths.py 的路径一致）
    out = Path(__file__).resolve().parent / "assets"
    out.mkdir(parents=True, exist_ok=True)

    make_app_icon(256).save(out / "icon.png")

    sizes = [256, 128, 64, 48, 32, 16]
    ico_images = [make_app_icon(s) for s in sizes]
    ico_images[0].save(out / "icon.ico", format="ICO",
                       sizes=[(s, s) for s in sizes],
                       append_images=ico_images[1:])

    make_tray_icon(256).save(out / "icon_tray.png")

    for name in ("icon.png", "icon.ico", "icon_tray.png"):
        print("saved:", out / name)


if __name__ == "__main__":
    main()
