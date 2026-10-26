"""键盘取键：把一次物理按键翻译成键名，并支持 Ctrl+C 这类组合键。

取键一律走 **Windows 虚拟键码（VK）**：VK 描述"按了哪个物理键"，与输入法
状态无关（中文输入法组字时 Tk 那一路连 <KeyPress> 都收不到，keysym 会变成
"??"，完全不可靠）。本模块只剩"键名 ⇄ VK"的纯映射与组合键解析：**取键**在
Rust（`rust/src/hid.rs` 的 `pressed_vks`，直接读系统键状态），**合成按键**也在
Rust（`rust/src/remap.rs`，自带 VK→INPUT 的 EXTENDEDKEY 判定）。
"""

from __future__ import annotations

import hexwand_rust                      # 取键与合成按键都在 Rust（硬依赖）

# 修饰键：单独按下不算一次绑定（捕获时会从"新按下的键"里减掉本集合），
# 但要参与组合键名字。
# 注意 CapsLock / NumLock / ScrollLock **不在这里**：它们不参与组合键命名
# （MODIFIER_ORDER 里没有它们），早先却被并进了本集合，于是捕获时被一起减掉，
# 这三键永远绑不上、界面也不给任何提示。按住不放污染快照那件事，由
# `capture_start` 先记一份 `_prev_down` 已经处理了，不需要靠减法。
MODIFIER_ORDER: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("Ctrl", (0x11,)), ("Shift", (0x10,)), ("Alt", (0x12,)),
    ("Win", (0x5B, 0x5C)),
)
MODIFIER_VKS: frozenset[int] = frozenset(
    v for _, vs in MODIFIER_ORDER for v in vs
)
# 鼠标键：轮询系统键状态时会看到，但不属于"键盘按键"
MOUSE_VKS: frozenset[int] = frozenset((0x01, 0x02, 0x04, 0x05, 0x06))
# Esc：捕获时用来取消，不作为可绑定键
VK_ESCAPE: int = 0x1B

# VK → 键名
VK_NAMES: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause",
    0x14: "CapsLock", 0x90: "NumLock", 0x91: "ScrollLock",
    0x1B: "Esc", 0x20: "Space", 0x21: "PageUp", 0x22: "PageDown",
    0x23: "End", 0x24: "Home", 0x25: "Left", 0x26: "Up",
    0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen", 0x2D: "Insert",
    0x2E: "Delete", 0x5D: "Menu",
}
VK_NAMES.update({0x41 + i: chr(0x41 + i) for i in range(26)})   # A-Z
VK_NAMES.update({0x30 + i: chr(0x30 + i) for i in range(10)})   # 0-9
VK_NAMES.update({0x60 + i: f"Num{i}" for i in range(10)})       # 小键盘 0-9
VK_NAMES.update({0x70 + i: f"F{i + 1}" for i in range(24)})     # F1-F24
VK_NAMES.update({                                                # OEM 符号键
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
})
# 小键盘运算符：键名必须与主键盘的 - . / 区分开。以前这几个直接写成
# "*" "+" "-" "." "/"，而 0xBD/0xBE/0xBF 也叫 - . / ——NAME_TO_VK 反向建表时
# 同名只留第一个，于是绑定了小键盘减键、合成出来的却是主键盘的 "-"。
VK_NAMES.update({
    0x6A: "NumMul", 0x6B: "NumAdd", 0x6D: "NumSub",
    0x6E: "NumDec", 0x6F: "NumDiv",
})

# 组合键里修饰键的固定顺序与对应 VK（合成按键时按这个顺序按下、逆序抬起）
COMBO_MODIFIERS: tuple[tuple[str, int], ...] = (
    ("Ctrl", 0x11), ("Shift", 0x10), ("Alt", 0x12), ("Win", 0x5B),
)

# 键名 → VK（VK_NAMES 是 VK → 键名的反向；同名取第一个，如 Win 的左右键）
NAME_TO_VK: dict[str, int] = {}
for _vk, _name in VK_NAMES.items():
    NAME_TO_VK.setdefault(_name, _vk)
for _name, _vk in COMBO_MODIFIERS:
    NAME_TO_VK.setdefault(_name, _vk)

# 忽略大小写的别名表。手改 config.json 是 README 承诺支持的用法（坏值要能
# 逐字段回退，不能崩），但 "ctrl+c" 这种写法以前两头都查不到：修饰键表按
# 大小写敏感的精确名匹配、单字符只把主键转成大写 → parse_hotkey 返回空，
# 整条映射静默失效，界面上看着一切正常、按下去什么都不发生。
_NAME_BY_LOWER: dict[str, int] = {n.lower(): v for n, v in NAME_TO_VK.items()}
_MOD_VK_BY_LOWER: dict[str, int] = {
    n.lower(): v for n, v in COMBO_MODIFIERS}


def _name_to_vk(part: str) -> int:
    """键名 → VK：先精确匹配，再忽略大小写。认不出返回 0。"""
    return (NAME_TO_VK.get(part)
            or _NAME_BY_LOWER.get(part.lower(), 0))


def vk_name(vk: int) -> str:
    """虚拟键码 → 键名；表里没有返回空串。"""
    return VK_NAMES.get(vk, "")


def parse_hotkey(name: str) -> list[int]:
    """把键名翻成"按顺序要按下的 VK 列表"。

    "Ctrl+C" → [0x11, 0x43]；"F5" → [0x74]；认不出返回空列表。
    修饰键统一排在最前，顺序按 COMBO_MODIFIERS，与用户书写顺序无关。
    """
    parts = [p.strip() for p in (name or "").split("+") if p.strip()]
    if not parts:
        return []
    mods: list[int] = []
    key_vk = 0
    for part in parts:
        mod_vk = _MOD_VK_BY_LOWER.get(part.lower())
        if mod_vk is not None:
            if mod_vk not in mods:
                mods.append(mod_vk)
            continue
        vk = _name_to_vk(part)
        if not vk:
            return []                     # 有认不出的键名 → 整条不生效，别乱发按键
        if key_vk:
            # 第二个非修饰键 → **整条不生效**。以前是静默覆盖：手写 "Ctrl+A+B"
            # （或配置里的坏值）会被悄悄绑成 Ctrl+B，界面上显示正常、按下去却是
            # 别的东西（2026-09-17 审查发现）。宁可整条拒掉，也不装作绑成功。
            return []
        key_vk = vk
    if not key_vk:
        return []
    # 修饰键按固定顺序排在前面
    order = [vk for _, vk in COMBO_MODIFIERS if vk in mods]
    return order + [key_vk]


def pressed_vks() -> set[int]:
    """当前按下的所有虚拟键（0x01..0xFF），直接读系统键状态，与输入法无关。

    实现在 Rust（`rust/src/hid.rs`，一次调用跑完 255 次 GetAsyncKeyState）：
    键位页取键每 20ms 轮询一次，Python 侧那 255 次 ctypes 调用每 tick 都要付
    一遍。原 ctypes 实现已随"底层只留 Rust"删除，这里不再有回落分支。
    """
    return set(hexwand_rust.pressed_vks())


def combo_name(down: set[int], key_vk: int) -> str:
    """把「当前按住的修饰键 + 刚按下的键」拼成 Ctrl+Shift+A 这样的名字。"""
    parts = [n for n, vks in MODIFIER_ORDER if down & set(vks)]
    parts.append(vk_name(key_vk))
    return "+".join(p for p in parts if p)
