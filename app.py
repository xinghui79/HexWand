"""鼠标本地控制程序（入口）：雷蛇可完整控制，罗技支持识别与读写。

设备库收录多款常见型号（见 hardware/catalog.py 的 DEVICE_DB）。

用法：
  python app.py              打开控制窗口
  python app.py --silent     静默启动：不弹窗，只留托盘（开机自启专用）
  python app.py --scan       列出已收录品牌的 HID 设备（无 GUI）
  python app.py --selftest   协议层与模拟逻辑自检（无 GUI）
  python app.py --check-imports   逐个 import 包内模块，验证依赖齐全（无 GUI）
  python app.py --check-gui       构造一次 QApplication，验证 Qt 平台插件在（无窗口）

界面是 Qt6（hexwand/ui/），只依赖 core/hardware；后两者与 GUI 工具包完全解耦。
图片资源统一放在项目根的 assets/（见 core/paths.py）。
"""

from __future__ import annotations

import os
import struct
import sys
import traceback

from hexwand.core.paths import CRASH_PATH, LOG_PATH
from hexwand.core.log import get_logger, setup_logging
# 帧层的常量与工具（自检用；帧格式见 hardware/frame/razer.py 与 hidpp.py）
from hexwand.hardware.frame import (REPORT_LEN, VID, build_hidpp_frame,
                                    build_report, crc8)


# CLI 子命令：flag 收集进下面的 frozenset，再到 _handle_cli_flag 里加一个分支
_SCAN_FLAGS: frozenset[str] = frozenset({"--scan", "-s"})
_SELFTEST_FLAGS: frozenset[str] = frozenset({"--selftest", "-t"})
_IMPORT_CHECK_FLAGS: frozenset[str] = frozenset({"--check-imports"})
# 冻结态专用：真的把 QApplication 建起来（= 验证 Qt 平台插件确实在包里）。
# 它比 --check-imports 更狠一层：`import PyQt6` 成功并不代表
# plugins/platforms/qwindows.dll 在包里，而缺它的报错发生在 QApplication 构造那
# 一刻 —— 那时 console=False 的 exe 没有任何输出，用户只看到"双击没反应"。
_GUI_CHECK_FLAGS: frozenset[str] = frozenset({"--check-gui"})
# 所有"无 GUI 的 CLI 子命令"：崩溃时只打 stderr，不弹模态框
_CLI_FLAGS: frozenset[str] = (_SCAN_FLAGS | _SELFTEST_FLAGS
                              | _IMPORT_CHECK_FLAGS | _GUI_CHECK_FLAGS)


def cmd_scan() -> None:
    """CLI：扫描已收录品牌（各协议条目 VID）的 HID 设备。"""
    log = get_logger()
    log.info("[scan] start")
    from hexwand import hardware as hw
    try:
        devs = hw.scan_known_devices()
    except Exception as e:  # noqa: BLE001
        log.error("[scan] failed: %s", e)
        print(f"扫描失败：{e}")
        print("请先安装依赖：运行 run.bat。")
        return
    if not devs:
        print("未找到任何已收录品牌的 HID 设备。")
        print("请确认：1) 鼠标/接收器已插入 USB；2) 已关闭官方鼠标软件或相关后台服务；"
              "3) 必要时以管理员身份运行。")
        return
    print(f"找到 {len(devs)} 个已收录品牌的 HID 接口：")
    for d in devs:
        print("-" * 60)
        print(f"  名称      : {d.name or '（无产品名）'}")
        print(f"  PID       : 0x{d.pid:04X}  {'[已知型号]' if d.known else '[未知型号]'}")
        # feat = Feature 报表长度（含 report id）。这是"选不出控制接口"时的
        # 第一顺位判据：雷蛇控制接口通常是 mi_00 + feat=91（见 CLAUDE.md）。
        feat = hw.feature_len_of_path(d.path)
        print(f"  usage_page: 0x{d.usage_page:04X}  interface: {d.interface}"
              f"  feat: {feat if feat is not None else '无 Feature 报表'}")
        print(f"  path      : {d.path}")


def _selftest_link_monitor() -> None:
    """LinkMonitor 纯逻辑回归（不需要 GUI，也不碰真机）。

    覆盖两个曾经踩过的坑：
      1. 档位表被拷贝 → UI 改档位后物理切档同步算出错位索引
      2. 离线 → 连续探测成功 → 在线的迁移 + DPI 变化回调
    """
    import time as _time
    from hexwand.hardware import monitor as lm

    saved = (lm.TICK_S, lm.PING_TICKS_OFFLINE, lm.ONLINE_HEARTBEATS,
             lm.LOST_HEARTBEATS, lm.PING_TICKS_ONLINE)
    lm.TICK_S = 0.01
    lm.PING_TICKS_OFFLINE = 2
    lm.ONLINE_HEARTBEATS = 2
    lm.LOST_HEARTBEATS = 2
    lm.PING_TICKS_ONLINE = 2
    try:
        link_events: list[str] = []
        dpi_events: list[tuple[int, int]] = []
        stages = [400, 800, 1600, 3200, 6400]
        mon = lm.LinkMonitor(
            probe=lambda: True,
            read_dpi=lambda: (1000, 1000),
            on_link=link_events.append,
            on_dpi=lambda i, x: dpi_events.append((i, x)),
            on_battery_tick=lambda: None,
            stages=stages)

        # (1) 档位表必须与调用方共享：UI 是就地改 self.stages[i]
        stages[4] = 1000
        assert mon.closest_stage(1000) == 4, mon.closest_stage(1000)

        # (2) 离线 → 在线 → DPI 同步回调
        mon.start()
        deadline = _time.time() + 3.0
        while _time.time() < deadline and (not link_events or not dpi_events):
            _time.sleep(0.02)
        mon.stop()
        assert link_events and link_events[0] == "online", link_events
        assert dpi_events and dpi_events[0] == (4, 1000), dpi_events
    finally:
        (lm.TICK_S, lm.PING_TICKS_OFFLINE, lm.ONLINE_HEARTBEATS,
         lm.LOST_HEARTBEATS, lm.PING_TICKS_ONLINE) = saved


def _selftest_tx_recovery() -> None:
    """`_adopt` 的 tx_id 自愈回归（替身 `_send`，不碰真机、不需要 GUI）。

    曾经的坑：`_adopt` 用一次性标记记"本进程已校验过配置带来的 tx_id"。开机时
    鼠标关机 → 校验失败 + 探测全败 → 留下兜底 tx_id=0x1F 却标记成已校验，此后
    同一进程里鼠标开机也再不会重新探测：tx_id 不是 0x1F 的型号（炼狱蝰蛇固定
    0xFF 等）就永久连不上，只能重启程序。
    """
    from hexwand import hardware as hw
    from hexwand.hardware.backends import HidDeviceInfo

    dev = hw.MouseDevice(pid=0x00C1, tx_id=0xFF)   # 配置里带来的旧 tx_id
    answered = {"ok": False}                       # False = 鼠标没开机
    sent: list[int | None] = []

    def fake_send(cmd_class: int, cmd_id: int, data: bytes = b"",
                  tx_id: int | None = None, verify: bool = True,
                  retries: int = 3) -> bytearray | None:
        sent.append(tx_id)
        if not answered["ok"]:
            if verify:
                raise RuntimeError("设备无响应")
            return None
        resp = bytearray(REPORT_LEN)
        resp[0] = 0x02                             # 0x02 = 成功
        return resp

    dev._send = fake_send                          # 实例属性遮蔽方法

    class _Stub:
        name = "stub"

        def get_feature_len(self, _h):
            return None

    info = HidDeviceInfo(path="stub", vid=VID, pid=0x00C1,
                         name=None, usage_page=0, interface=None)

    # (1) 鼠标关机：1 次校验 + 2 轮 report_id × 全部候选探测都失败 → 不算已验证
    dev._adopt(_Stub(), info, None, None)
    assert not dev.tx_id_verified, "探测全败却把 tx_id 标成已验证"
    assert dev.tx_id == 0x1F, dev.tx_id            # 停在最保守的兜底值
    assert len(sent) == 1 + 2 * len(dev.proto.tx_candidates), len(sent)

    # (2) 鼠标开机后重连：必须重新校验（旧代码在此直接早退，永远不自愈）
    answered["ok"] = True
    sent.clear()
    dev._adopt(_Stub(), info, None, None)
    assert dev.tx_id_verified, "重连后没有重新校验 tx_id（不会自愈）"
    assert sent == [0x1F], sent                    # 一次校验就过，不必再全量探测

    # (3) 验证通过之后不再重复花 HID 交互
    sent.clear()
    dev._adopt(_Stub(), info, None, None)
    assert sent == [], sent


def _selftest_hidpp_negotiate() -> None:
    """HID++ feature 协商回归（替身收发，不碰真机）。

    曾经的坑（2026-10-26 审查发现）：`negotiate` 的"要用的 feature"只有电量与
    固件，且一旦凑齐就 break。板载配置 0x8100 的 index 由设备随便排，只要它排在
    这两个之后，就永远轮不到枚举 → `_onboard_index()` 报"该设备不支持板载配置
    功能"，罗技的 DPI / 轮询率整条路径全废。这里用一张**故意把 0x8100 排最后**
    的 feature 表锁死该行为。
    """
    from hexwand.hardware.protocols import HidppProfile

    def _fake(features: dict[int, int]):
        """极简 HID++ 设备：只实现 ROOT.getFeature / getCount / getFeatureID。"""
        def exchange(req: bytes, _report_id: int) -> bytearray | None:
            req = bytes(req)
            fi, func = req[2], req[3] & 0xF0
            if fi == 0x00 and func == 0x00:        # ROOT.getFeature(0x0001)
                return bytearray([0x10, 1, 0x00, func | 0x08, 0x01])
            if fi == 0x01 and func == 0x00:        # FEATURE_SET.getCount
                return bytearray([0x10, 1, 0x01, func | 0x08, len(features)])
            if fi == 0x01 and func == 0x10:        # FEATURE_SET.getFeatureID
                fid = features.get(req[4], 0)
                return bytearray([0x10, 1, 0x01, func | 0x08,
                                  (fid >> 8) & 0xFF, fid & 0xFF])
            return None
        return exchange

    proto = HidppProfile(key="_selftest_hidpp", product="自检", pids=[])
    assert proto.negotiate(_fake({1: 0x0003, 2: 0x1004, 3: 0x8100})), \
        "HID++ 协商失败（feature 齐全）"
    assert proto._onboard_index() == 3, \
        f"板载配置 feature 未被枚举：{proto._feat_index}"

    # 设备真的没有板载配置时必须如实返回 False（而不是"连上了但什么都读不了"）
    missing = HidppProfile(key="_selftest_hidpp_missing", product="自检", pids=[])
    assert not missing.negotiate(_fake({1: 0x0003, 2: 0x1004})), \
        "缺少板载配置 feature 时协商却报告成功"
    print("HID++ feature negotiation verified (onboard included).")


def cmd_selftest() -> None:
    """CLI：模拟模式自检（协议层 + 模拟设备 + 状态机纯逻辑）。"""
    log = get_logger()
    log.info("[selftest] start")
    from hexwand import hardware as hw
    # 无通用兜底协议后，模拟设备也必须挂在一只已收录的鼠标上（毒蝰 V3 极速版）
    dev = hw.MouseDevice(pid=0x00B8, simulation=True)
    dev.connect()
    assert dev.get_dpi() == (800, 800), dev.get_dpi()
    dev.set_dpi(1600)
    # 模拟设备的返回值与 device._simulate 里那组常量**必须同步**：
    # 2026-09-17 把模拟值改成 1000Hz / 79% 时漏了这里，selftest 直接断言失败。
    assert dev.get_poll() == 1000
    dev.set_poll(1000)
    # 未收录速率必须响亮报错：旧实现静默兜底成 500Hz，等于替用户编一个轮询率
    try:
        dev.set_poll(2000)
        raise AssertionError("set_poll(2000) 没有拒绝（不该静默兜底）")
    except ValueError:
        pass
    # get_battery() 一律返回百分比 0-100（模拟设备存的是雷蛇裸值 0xC9=201）
    assert dev.get_battery() == 79, dev.get_battery()

    # ---- 帧层用例：用例内带一份**独立参考实现**（test oracle，与运行时无关），
    # 逐字节比对锁死帧格式 —— 以后改 rust/src/lib.rs 时跑一遍就知道有没有改坏。
    import hexwand_rust as _rust

    # 陈旧扩展必须当场拦下：扩展同时装在项目 .venv 与打包 venv，项目根还曾有过
    # 手搓的 .pyd 会遮蔽它们 —— 2026-09-17 出过"exe 带着几小时前的旧扩展、
    # Rust 改动完全没生效"却一路绿灯的事故。版本戳与 rust/src/lib.rs 的
    # API_VERSION 成对维护：**改/加对外符号就一起 +1**。
    assert getattr(_rust, "API_VERSION", 0) == 5, (
        f"hexwand_rust 是旧产物（API_VERSION="
        f"{getattr(_rust, 'API_VERSION', '缺失')}，期望 5）："
        f"{getattr(_rust, '__file__', '?')}。请重跑构建/打包。")

    def _ref_frame(cls, cid, payload, tx):
        rep = bytearray(90)
        rep[1] = tx & 0xFF
        rep[5] = len(payload) & 0xFF
        rep[6] = cls & 0xFF
        rep[7] = cid & 0xFF
        for i, b in enumerate(payload[:80]):
            rep[8 + i] = b & 0xFF
        c = 0
        for i in range(2, 88):
            c ^= rep[i]
        rep[88] = c & 0xFF
        return bytes(rep)

    cases = [
        (0x04, 0x05, b"\x01\x03\x20\x00\x03\x20\x00", 0x1F),
        (0x00, 0x81, b"", 0xFF),
        (0x07, 0x80, bytes(range(80)), 0x3F),      # 满 80 字节参数区
        (0x00, 0x05, b"\x01", 0x1F),               # 轮询率单字节
    ]
    for cls, cid, payload, tx in cases:
        got = bytes(build_report(cls, cid, payload, tx))
        want = _ref_frame(cls, cid, payload, tx)
        assert got == want, f"帧与参考实现不一致: class={cls:#x} id={cid:#x}"
        assert _rust.crc8(got) == want[88], "CRC 不一致"
    print("Rust frame layer verified against reference impl "
          f"({len(cases)} cases).")

    # ---- HID++（罗技）帧层同样比对：短 7 / 长 20、首字节就是 report id ----
    # 参考实现写在用例里（与运行时无关），锁死"参数 ≤3 字节发短报文"这条判据。
    def _ref_hidpp(dev_idx, feat_idx, func, params, sw_id):
        rep = bytearray(7 if len(params) <= 3 else 20)
        rep[0] = 0x10 if len(params) <= 3 else 0x11
        rep[1] = dev_idx & 0xFF
        rep[2] = feat_idx & 0xFF
        rep[3] = (func & 0xF0) | (sw_id & 0x0F)
        for i, b in enumerate(params[:len(rep) - 4]):
            rep[4 + i] = b & 0xFF
        return bytes(rep)

    def _ref_ccitt(data):
        crc = 0xFFFF
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = (((crc << 1) ^ 0x1021) & 0xFFFF
                       if crc & 0x8000 else (crc << 1) & 0xFFFF)
        return crc

    hidpp_cases = [
        (0x01, 0x00, 0x00, struct.pack("!H", 0x0001), 0x08),   # ROOT.getFeature
        (0x01, 0x05, 0x10, b"\x03", 0x08),                     # 单字节参数
        (0x01, 0x05, 0x50, struct.pack("!HH", 0x0000, 0x0010), 0x08),  # 4 字节→长报文
        (0x01, 0x05, 0x40, bytes(range(16)), 0x0F),            # 满参数区 + sw_id
        (0xFF, 0x00, 0x00, b"", 0x08),                         # 接收器自身（空参数）
    ]
    for di, fi, func, prm, sw in hidpp_cases:
        got = bytes(build_hidpp_frame(di, fi, func, prm, sw))
        want = _ref_hidpp(di, fi, func, prm, sw)
        assert got == want, f"HID++ 帧与参考实现不一致: feat={fi:#x} func={func:#x}"
        assert _rust.crc_ccitt(got) == _ref_ccitt(want), "CRC-CCITT 不一致"
    print("Rust HID++ frame layer verified against reference impl "
          f"({len(hidpp_cases)} cases).")

    # ---- VK 轮询（Rust 唯一实现）：可取、值域合法 ----
    # 原先这里拿 ctypes 实现做逐样本对照；"底层只留 Rust"之后对照对象已删，
    # 改为形状检查——它同时保证冻结态扩展导出了 pressed_vks。
    _down = set(_rust.pressed_vks())
    assert _down <= set(range(0x01, 0x100)), f"VK 轮询返回非法键码: {_down}"
    print(f"Rust VK polling OK ({len(_down)} key(s) down).")

    # ---- Rust 原生 HID 后端可构造（冻结态扩展 API 是否齐全）----
    # 只构造、不做任何设备 I/O：`RustHidBackend.__init__` 仅 import 扩展并实例化。
    from hexwand.hardware.backends import RustHidBackend
    assert RustHidBackend().name == "native(rust hid)", "Rust HID 后端不可用"
    print("Rust native HID backend available (no device I/O performed).")

    # ---- Rust 鼠标钩子冒烟：真装真摘（不点鼠标、不合成输入）----
    from hexwand_rust import MouseRemapper as _RustRemapper
    r = _RustRemapper()
    err = r.start()
    assert err == "", err
    assert r.active, "Rust 钩子未装上"
    r.set_map(["left"], [0], [[]])       # 左键禁用（验证映射推送通路）
    r.set_enabled(False)
    r.set_enabled(True)
    r.stop()
    assert not r.active, "Rust 钩子未摘除"
    print("Rust mouse hook verified: start/set_map/stop OK.")
    # 雷蛇帧的组帧与 CRC（见 hardware/frame/razer.py，其他品牌各有自己的帧）
    rep = build_report(0x04, 0x05, b"\x01\x03\x20\x00\x03\x20\x00")
    assert len(rep) == 90, len(rep)
    assert crc8(rep) == rep[88], "CRC 不一致"
    rep[10] ^= 0xFF
    assert crc8(rep) != rep[88], "CRC 未随数据变化"
    _selftest_link_monitor()
    _selftest_tx_recovery()
    _selftest_hidpp_negotiate()
    print("SELFTEST PASS —— 协议层与模拟逻辑正常。")


def cmd_check_imports() -> None:
    """CLI：把包内每个模块 import 一遍（不建窗口、不起托盘）。

    存在的意义：`--selftest` 只打协议层，完全不 import ui/pystray/PIL，所以
    "GUI 一启动就崩"或冻结态缺 hiddenimport 的包也能通过冒烟。这一步专门补这个洞。
    """
    import importlib
    import pkgutil

    import hexwand

    failed: list[str] = []
    count = 0
    for mod in pkgutil.walk_packages(hexwand.__path__, "hexwand."):
        count += 1
        try:
            importlib.import_module(mod.name)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{mod.name}: {e}")
    if failed:
        print("IMPORTS FAILED")
        for line in failed:
            print("  " + line)
        raise SystemExit(1)
    print(f"IMPORTS OK —— {count} 个模块全部可加载。")


def cmd_check_gui() -> None:
    """CLI：冻结态冒烟——把 QApplication 真的建起来。

    只 import 是不够的：缺 Qt 平台插件（`plugins/platforms/qwindows.dll`）时
    `import PyQt6.QtWidgets` 照样成功，报错发生在 **QApplication 构造那一刻**。
    这里不创建窗口、不显示任何东西（构造完即退出），打包流程里可以安全地跑。
    """
    from hexwand.ui.qt import QtWidgets

    app = QtWidgets.QApplication([])
    if app is None:                       # pragma: no cover — 理论上不会发生
        raise SystemExit("QApplication 构造失败")
    print("GUI OK —— Qt 平台插件可加载（未创建任何窗口）。")


def _handle_cli_flag(flag: str) -> bool:
    """若 flag 是已知 CLI 子命令则执行并返回 True，否则返回 False。"""
    if flag in _SCAN_FLAGS:
        cmd_scan()
        return True
    if flag in _SELFTEST_FLAGS:
        cmd_selftest()
        return True
    if flag in _IMPORT_CHECK_FLAGS:
        cmd_check_imports()
        return True
    if flag in _GUI_CHECK_FLAGS:
        cmd_check_gui()
        return True
    return False


def main() -> None:
    log = setup_logging(LOG_PATH)
    log.info("=== HexWand start ===")
    log.info("Python: %s", sys.version.replace("\n", " "))

    silent = "--silent" in sys.argv

    # CLI 子命令（无 GUI）
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            if _handle_cli_flag(arg):
                return

    # 单实例：先争抢唯一实例端口；已有实例在跑则唤醒它并退出本进程
    from hexwand.core import single_instance
    result = single_instance.acquire_or_wake()
    if result.status == "wake_failed":
        # 端口被占用且唤醒不到：可能是残留僵尸或异常实例。强行再起会与
        # 占用者抢设备/双托盘，直接退出更安全（原因已记入 startup.log）。
        log.warning("单实例端口被占用且唤醒失败，放弃启动（避免多实例冲突）")
        # 必须给看得见的原因：以前这里只写日志就 return，用户双击 exe 看到的是
        # "什么都没发生"，而且没有任何东西可查（README 故障排查表里那条）。
        # --silent（开机自启）下不弹：没人点的模态框会把登录流程挂在后台。
        if not silent:
            _show_notice(
                "HexWand 没有启动",
                f"唯一实例端口 {single_instance.port()} 已被其它程序占用，"
                f"且唤醒已有实例失败。\n\n"
                f"可能原因：HexWand 已在运行（看系统托盘），或该端口被别的程序占了。\n"
                f"详见 {LOG_PATH}")
        return
    if not result.is_primary:
        # 唤醒路径硬退出：正常 return 后解释器收尾在极端情况下会被残留句柄/
        # 文件锁卡住（实测出现过不退出的第二实例僵尸）。唤醒者没有任何需要
        # 保存的状态，直接退最稳。
        log.info("wake done, hard-exit")
        os._exit(0)

    # 启动 Qt6 界面。缺 Qt / Qt 装坏 / 平台插件异常时不硬崩：
    # 记日志 + 弹一个原生提示框，把原因说清楚（以前是静默崩溃，用户只看到
    # "双击了没反应"）。
    try:
        from hexwand.ui import run_gui as run_qt_gui
        from hexwand.ui.qt import app_version_text
    except Exception as e:  # noqa: BLE001 — 缺绑定 / 装坏 / Qt 平台插件异常
        reason = str(e).replace("\n", " ")
        log.error("Qt6 界面不可用：%s", reason)
        if not silent:
            _show_notice(
                "HexWand 没有启动",
                "Qt6 界面无法加载，程序退出。\n\n"
                f"原因：{reason}\n\n"
                "装了 Qt 绑定吗？本项目只用 PyQt6：\n"
                "  .venv\\Scripts\\python.exe -m pip install PyQt6\n\n"
                f"详见 {LOG_PATH}")
        raise SystemExit(1)

    log.info("UI: Qt6 via %s", app_version_text())
    run_qt_gui(listener=result.server, silent=silent)


def _show_notice(title: str, text: str) -> None:
    """普通提示框（不是崩溃，只是要把"我为什么没启动"说清楚）。

    走 Win32 原生 MessageBox：比"为了弹一个框而初始化一整套 GUI 工具包"可靠
    得多——这条路径正是在"GUI 起不来"时才走到的，这时候去 import 一个装都没装的
    Qt 是自相矛盾的。非 Windows 上退化为打印（本项目只支持 Windows）。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, str(text), str(title), 0x00000030)   # MB_ICONWARNING
            return
        except Exception:  # noqa: BLE001 — 提示框失败不该掀翻退出流程
            pass
    print(f"[{title}] {text}")


def _report_crash(tb: str, show_dialog: bool = True) -> None:
    """崩溃兜底：写 crash.log + 弹一个模态框（GUI 启动路径）。"""
    try:
        get_logger().error("CRASH: %s", tb)
        CRASH_PATH.write_text("CRASH TRACEBACK\n" + tb, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not show_dialog:
        # CLI 子命令（--selftest / --scan）不弹模态框：build.py 的 exe 冒烟步骤
        # 会被一个没人点的对话框卡到超时，看起来像"打包卡死"。
        print(tb)
        return
    msg = "启动时出错，详见 crash.log / startup.log：\n\n" + tb[-1500:]
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, msg, "程序崩溃", 0x00000010)          # MB_ICONERROR
            return
        except Exception:  # noqa: BLE001
            pass
    print(tb)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        # 必须给出非 0 退出码：否则崩溃兜底走完就 return，build.py 的冒烟步骤
        # 和 run.bat 都无法从退出码分辨"启动就崩"。
        # --silent（开机自启）同样不弹模态框：与 wake_failed 路径同一条教训——
        # 没人点的对话框会把登录流程挂在后台。
        cli_only = any(a in _CLI_FLAGS for a in sys.argv[1:])
        _report_crash(traceback.format_exc(),
                      show_dialog=not (cli_only or "--silent" in sys.argv))
        sys.exit(1)
