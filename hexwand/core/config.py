"""配置：AppConfig 数据类 + JSON 持久化。

开发态写在项目根目录 config.json；打包后写 %APPDATA%/HexWand/config.json。
不认识的字段在加载时丢弃（"不留兼容层"，见 load() 的 docstring）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import app_data_dir, project_dir

log = logging.getLogger("hexwand")


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def config_path() -> Path:
    """打包态 → %APPDATA%/HexWand/config.json；开发态 → 项目根 config.json。"""
    if getattr(sys, "frozen", False):
        return app_data_dir() / "config.json"
    return project_dir() / "config.json"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    """用户配置。字段顺序与默认值是用户首次启动的初始状态。"""
    pid: int = 0                                   # 0 = 未选择设备（首启由 autodetect 自动认领）
    tx_id: int | None = None                         # None → 启动时自动探测
    report_id: int = 2                               # 雷蛇 Feature Report 编号
    simulation: bool = False                         # 无设备时模拟运行
    stages: list[int] = field(
        default_factory=lambda: [400, 800, 1600, 3200, 6400])
    poll_rate: int = 500                             # 与 UI 的 DEFAULT_POLL_RATE 一致
    idle_minutes: int = 5                            # 闲置进入睡眠（分钟）
    low_battery_pct: int = 20                        # 低电量阈值（%）
    devices: list[int] = field(default_factory=list)  # 我的鼠标（PID，最近在前）
    keymap: dict = field(default_factory=dict)   # 全局按键映射：{button_id: action}（对所有鼠标生效）


# ---------------------------------------------------------------------------
# 值清洗：损坏 / 手改的 config.json 不应让程序崩溃。
# 逐字段做**类型 + 取值范围**护栏，清洗失败一律回退该字段的默认值
# （保留其余有效配置）。
# ---------------------------------------------------------------------------
def _as_int(v: Any, default: int) -> int:
    if isinstance(v, bool):          # bool 是 int 子类，但语义上不是数值
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_ranged(v: Any, default: int, lo: int, hi: int) -> int:
    """int 化后**钳进** [lo, hi]（坏值先回退 default）。

    有些字段只做 int 化是不够的：负 PID、report_id=999 会被原样写进
    MouseDevice（PID 为负时 UI 说"未选择设备"、状态栏却说"每 3 秒探测"；
    report_id 超过一字节后在后端被 & 0xFF 变成另一个编号）。这些数值都有明确
    的物理取值范围，越界就是配置坏了，钳到边界比留着一个能跑但说不通的值好。
    """
    return max(lo, min(hi, _as_int(v, default)))


def _as_optional_u8(v: Any) -> int | None:
    """tx_id：None 表示"启动时自动探测"；非法值同样回 None；越界钳进 u8。"""
    if v is None:
        return None
    n = _as_int(v, -1)
    return None if n < 0 else min(0xFF, n)


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return default


def _as_pids(v: Any) -> list[int] | None:
    """清洗 devices（PID 列表）：非负整数，去重保序。"""
    if isinstance(v, (str, bytes)) or not hasattr(v, "__iter__"):
        return None
    out: list[int] = []
    for s in v:
        if isinstance(s, bool):
            continue
        i = _as_int(s, -1)
        if i >= 0 and i not in out:
            out.append(i)
    return out or None


# 档位值的支持范围：覆盖已收录型号的最大 DPI（44000），只当"配置里写的数
# 还像不像 DPI"的护栏；真正的上限由设备按型号 dpi_max 再钳一次。
STAGE_MIN: int = 1
STAGE_MAX: int = 50000
STAGE_SLOTS: int = 5            # 硬件模型固定 5 档（协议命令也是 5 档连排）


def _as_stages(v: Any) -> list[int] | None:
    """清洗 stages：元素 int 化后**钳进** [STAGE_MIN, STAGE_MAX]，最多 5 档。

    越界是钳制而不是丢弃：丢元素会让剩下几档整体错位（第 3 档跑到第 2 档的
    位子上），比一个偏大的数字更难解释；钳制至少保证 5 个槽位一一对应。
    不钳的话坏值会一路走到 UI：`[-500, 999999]` 里 "-" 被输入框的校验拒掉，
    那一档的框就凭空显示成空白，用户看不出是配置坏了。
    空/非法返回 None（用默认）。
    """
    if isinstance(v, (str, bytes)) or not hasattr(v, "__iter__"):
        return None
    out: list[int] = []
    for s in v:
        if isinstance(s, bool):
            continue
        try:
            n = int(s)
        except (TypeError, ValueError):
            continue
        out.append(max(STAGE_MIN, min(STAGE_MAX, n)))
        if len(out) == STAGE_SLOTS:        # 只认前 5 档，多的写不进硬件
            break
    return out or None


def _clean_action(act: Any) -> dict | None:
    """清洗单条动作：{type, [key|button]}。未知 type 或结构非法返回 None。

    "default"（不改这只键）必须放行：键位页有 default/none/key/mouse 四态并
    会存 `{"type": "default"}`，早先的白名单少了它，于是这条配置在读取时被
    当成非法条目丢掉——今天恰好和"没有这条映射"等效才没出问题，但配置无法
    原样往返，以后谁区分"显式设成默认"与"从没配过"就会踩到。
    """
    if not isinstance(act, dict):
        return None
    atype = act.get("type")
    if atype not in ("default", "none", "key", "mouse"):
        return None
    out: dict = {"type": atype}
    if atype == "key":
        out["key"] = act["key"] if isinstance(act.get("key"), str) else ""
    elif atype == "mouse":
        out["button"] = act["button"] if isinstance(act.get("button"), str) else ""
    return out


def _as_keymap_global(v: Any) -> dict | None:
    """清洗全局映射：{button_id: action}；非法条目丢弃，空/非法回退 None（用默认 {}）。

    旧版还有两种形态（{pid:桶} 与 list[规则]），只出现在老配置文件里，
    由 load() 的迁移逻辑处理，不在这个清洗器里兼容。
    """
    if not isinstance(v, dict):
        return None
    out: dict = {}
    for bid, act in v.items():
        if not isinstance(bid, str) or not bid:
            continue
        ca = _clean_action(act)
        if ca is not None:
            out[bid] = ca
    return out or None


# 字段名 -> 清洗函数(value, default) -> 清洗后的值
_FIELD_CLEANERS: dict[str, Callable[[Any, Any], Any]] = {
    # 取值范围与设备/界面的实际约束一致（见 _as_ranged 的说明）
    "pid": lambda v, d: _as_ranged(v, d, 0, 0xFFFF),
    "report_id": lambda v, d: _as_ranged(v, d, 0, 0xFF),
    "poll_rate": _as_int,          # 界面只认 POLL_OPTIONS，坏值会退回默认档
    "idle_minutes": lambda v, d: _as_ranged(v, d, 1, 15),
    "low_battery_pct": lambda v, d: _as_ranged(v, d, 5, 100),
    "tx_id": lambda v, d: _as_optional_u8(v),
    "simulation": _as_bool,
    "stages": lambda v, d: _as_stages(v) or d,
    "devices": lambda v, d: _as_pids(v) or d,
    "keymap": lambda v, d: _as_keymap_global(v) or d,
}


# ---------------------------------------------------------------------------
# 加载 / 保存
# ---------------------------------------------------------------------------
def load(path: Path | str | None = None) -> AppConfig:
    """加载配置：坏值回退该字段默认；**不认识的字段直接丢弃**。

    曾经这里有两代 keymap 的迁移（`keymap_by_pid` 按设备分桶、`keymap` 是
    [{button, action}] 列表）和 `_extra` 未知字段保留。都按"不留兼容层"删掉了：
    程序是单人自用，config.json 早已是全局 dict 形态，留着迁移层只是让 load()
    多出几十行永不执行的分支。
    """
    p = Path(path) if path else config_path()
    raw: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):     # 顶层非 dict（损坏的 [] / "x"）直接丢弃
                raw = data
            else:
                log.error("config.json 顶层不是对象（%s），本次按全默认值启动；"
                          "文件将保持原样直到下一次保存覆盖：%s",
                          type(data).__name__, p)
        except OSError as e:
            log.error("config.json 读取失败（%s），本次按全默认值启动：%s", e, p)
        except ValueError as e:            # ValueError 覆盖 JSONDecodeError 与
            log.error("config.json 损坏/非 UTF-8（%s），本次按全默认值启动：%s",
                      e, p)                # UnicodeDecodeError（文件含非 UTF-8 字节）

    cfg = AppConfig()                       # 先取全部默认值
    for name, clean in _FIELD_CLEANERS.items():
        if name in raw:
            try:
                setattr(cfg, name, clean(raw[name], getattr(cfg, name)))
            except Exception as e:  # noqa: BLE001 — 单字段清洗失败不影响其余配置
                log.warning("config 字段 %s 清洗失败（保留默认值）: %s", name, e)
    return cfg


# 同进程内多线程并发 save 的互斥（UI 后台线程 + 状态机线程都可能保存配置）
_SAVE_LOCK = threading.Lock()
_REPLACE_DELAYS = (0.0, 0.05, 0.15, 0.4)       # os.replace 的退避重试


def save(cfg: AppConfig, path: Path | str | None = None) -> None:
    """原子写：先写唯一临时文件再 os.replace。

    唯一临时名只挡住 tmp 互相覆盖；`os.replace` 打同一目标在 Windows 上仍会
    偶发 PermissionError(WinError 5)——并发写、或任何进程以不含 FILE_SHARE_DELETE
    的句柄持有 config.json（开发态它就在 OneDrive 目录里，同步引擎正是这种持有者）。
    所以带退避重试；全部失败则抛出，由调用方如实告诉用户"没保存上"。
    """
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    with _SAVE_LOCK:
        # 快照必须在锁内：monitor 线程与 UI 线程会并发改 cfg 并 save，
        # 锁外序列化可能把"新 pid + 旧 tx_id"这类撕裂状态写进文件
        data = asdict(cfg)
        text = json.dumps(data, ensure_ascii=False, indent=2)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                # 落到盘上再 replace：否则断电/同步引擎可能让"成功"的替换
                # 背后站着 0 字节或半写文件（读回来就是上面那条损坏日志）
                f.flush()
                os.fsync(f.fileno())
            for i, delay in enumerate(_REPLACE_DELAYS):
                if delay:
                    time.sleep(delay)
                try:
                    os.replace(tmp_name, p)
                    return
                except OSError:
                    if i == len(_REPLACE_DELAYS) - 1:
                        raise
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

