"""单实例：本地 TCP 127.0.0.1 固定端口。

设计：bind-first。OS 对同端口的 bind 是原子互斥——"先探测再 bind"方案
存在竞态窗口（两个实例几乎同时启动时探测互相错过、双双成为主实例）。
bind-first 由 OS 保证同端口只有一个持有者。

端口选择：必须避开 Windows 的临时端口段（默认 49152–65535，且管理员可以
把起点压得更低）。原来用的 51237 正落在这段里，于是**任何一个本机程序的
出站连接都可能已经占着这个号**：bind 失败的原因跟 HexWand 毫无关系，接着
唤醒握手等不到 "rc" 回执（对方是别的程序），跑满重试后返回 wake_failed，
`app.py` 只记一行日志就退出——用户看到的是"双击 exe 没反应"（README 故障
排查表里那条），既没提示也没日志可查。改到 49152 以下的固定端口后，只有
真的起了同名服务的机器才会撞上。

改端口的过渡期注意：老版本实例仍监听 51237，新实例在 40040 上 bind 成功
就会被判成主实例，短暂出现"两个实例并存"（双托盘、抢同一只鼠标）。升级
那一次先把旧进程退掉即可，之后又回到单实例。

流程：
  1) bind 端口：成功 → 本进程是主实例（约 1ms），start_listener 开始监听
  2) bind 失败（端口已被占用）→ 反复 wake 探测：
     发 "show" → 对端回 "rc" 回执 → 确认是同类实例 → 本进程退出
     （握手防误伤：第三方程序恰好占用端口时没有回执，不当作"已唤醒"）
  3) 唤醒不到 → wake_failed（app.py 如实提示后放弃启动）
  4) 主实例收到 "show" → 先触发 on_show 弹窗，**成功后才回 "rc" 回执**；
     recv 带超时，防止不说话的连接（端口扫描等）卡死唯一监听线程。
     （回执在后的原因：先回 ACK 时 on_show 一抛错，第二实例已按"唤醒成功"
     硬退出、主实例却什么都没弹——正是本模块要消灭的"双击没反应"）

不设 SO_REUSEADDR（Windows 下会允许重复绑定）。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Literal, Optional

log = logging.getLogger(__name__)

_SINGLE_HOST: str = "127.0.0.1"
# 必须 < 49152：落在 Windows 临时端口段里会被本机随机出站连接抢占（详见模块
# docstring）。换端口时同时更新 README 故障排查表里的那个数字。
_SINGLE_PORT: int = 40040

# bind 失败后的 wake 重试（每次 connect 自带超时，见 _wake_probe）
_BIND_FAIL_WAKE_PROBES: int = 12
_BIND_FAIL_WAKE_INTERVAL_S: float = 0.3
_LISTEN_BACKLOG: int = 4


def port() -> int:
    """单实例仲裁端口（app.py 的"没启动"提示文案用，别直接引用私有常量）。"""
    return _SINGLE_PORT


# 结果状态字面量
STATUS_ACQUIRED: Literal["acquired"] = "acquired"
STATUS_WAKE: Literal["wake"] = "wake"
STATUS_WAKE_FAILED: Literal["wake_failed"] = "wake_failed"


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------
class AcquireResult:
    """acquire_or_wake 的结果。

    status 取值：
      - "acquired"     本进程 bind 成功，是主实例
      - "wake"         探测到已有实例，已发送 wake 并应退出
      - "wake_failed"  端口被占且唤醒不到（残留/异常实例）
    """
    __slots__ = ("status", "server")

    def __init__(self, status: str, server: Optional[socket.socket] = None) -> None:
        self.status = status
        self.server = server

    @property
    def is_primary(self) -> bool:
        """只有 "acquired" 才是本进程持有端口；"wake_failed" 不是主实例
        （app.py 会据 status 提前退出，这里保持语义一致以免别处误用）。"""
        return self.status == STATUS_ACQUIRED


# ---------------------------------------------------------------------------
# 探测与获取
# ---------------------------------------------------------------------------
# 唤醒握手：第二实例发 b"show"，主实例回 b"rc"。有回执才能确认对端
# 是同类实例——第三方程序恰好占用端口时没有回执，不能当作"已唤醒"。
_WAKE_MSG: bytes = b"show"
_WAKE_ACK: bytes = b"rc"
_WAKE_IO_TIMEOUT_S: float = 0.5


def _wake_probe() -> bool:
    """尝试连接已有实例、发送唤醒消息并验证回执。确认是同类实例返回 True。"""
    try:
        with socket.create_connection(
                (_SINGLE_HOST, _SINGLE_PORT), timeout=_WAKE_IO_TIMEOUT_S) as s:
            s.settimeout(_WAKE_IO_TIMEOUT_S)
            s.sendall(_WAKE_MSG)
            s.shutdown(socket.SHUT_WR)
            ack = s.recv(4)
        return ack == _WAKE_ACK
    except OSError:
        return False


def acquire_or_wake() -> AcquireResult:
    """争抢唯一实例端口（bind-first，由 OS 原子仲裁）。

    先 bind：成功即主实例，不必先探测。顺序不能反过来——"先探测再 bind"在两
    个实例几乎同时启动时存在竞态窗口（互相探测都扑空、双双成为主实例），而
    bind 由 OS 保证同端口只有一个赢家。

    别误会成"探测会很慢"：慢的部分由 `_WAKE_IO_TIMEOUT_S` 掐掉。但"没人监听的
    回环端口立刻 WSAECONNREFUSED"这台机器上不成立——实测 RST 要 ~2s 才回来，
    所以每一发失败的探测都会跑满 0.5s 连接超时（另加 0.3s 间隔）。最坏情况
    （第三方占着端口又不回话）≈ 12 × 0.8s ≈ 9.6s 才返回 wake_failed，那段
    "双击没反应"的等待就是这个数；正常路径（老版本 HexWand 在监听）一次握手
    就完，实测 ~0.4ms。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind((_SINGLE_HOST, _SINGLE_PORT))
        srv.listen(_LISTEN_BACKLOG)
        log.info("listening (unique instance)")
        return AcquireResult(STATUS_ACQUIRED, srv)
    except OSError as e:
        # 竞态：另一实例刚 bind。等它 listen 就绪后唤醒它
        srv.close()
        log.info("port busy, wake retry: %s", e)
        for _ in range(_BIND_FAIL_WAKE_PROBES):
            if _wake_probe():
                log.info("existing instance found, wake and exit")
                return AcquireResult(STATUS_WAKE)
            time.sleep(_BIND_FAIL_WAKE_INTERVAL_S)
        # 端口被占且唤醒不到：可能是残留僵尸或别的程序占了这个端口
        log.warning("wake failed after %d probes; giving up starting",
                    _BIND_FAIL_WAKE_PROBES)
        return AcquireResult(STATUS_WAKE_FAILED)


def start_listener(server: socket.socket, on_show: Callable[[], None]) -> None:
    """后台线程监听端口，收到 'show' 时回调 on_show()（回调内部需自行调度到主线程）。"""

    def _loop() -> None:
        while True:
            if server.fileno() == -1:
                return                      # _quit 关掉了 socket，正常收尾
            try:
                conn, _ = server.accept()
            except OSError as e:
                # 单次 accept 出错不代表 socket 完了（中断/资源类错误都可能）。
                # 这里直接 break 就是永久死区：端口仍被占着、_wake_probe 仍返回
                # True → 第二个实例"唤醒成功"后直接退出，用户看到的是"双击 exe
                # 永远没反应"，而且日志里什么都没有。
                if server.fileno() == -1:
                    return
                log.warning("accept failed, retrying: %s", e)
                time.sleep(1.0)
                continue
            try:
                with conn:
                    conn.settimeout(2.0)    # 防不说话的连接（端口扫描等）卡死监听线程
                    data = conn.recv(16)
                    if data and _WAKE_MSG in data:
                        # **先做唤醒显示动作、成功后才回 ACK**。反过来（先 ACK）时
                        # on_show 一旦抛错：第二实例已按"唤醒成功"硬退出，
                        # 主实例却什么都没弹——正是本模块要消灭的"双击没反应"。
                        # 不回 ACK 则探测方重试，最终如实走 wake_failed 提示。
                        try:
                            on_show()
                        except Exception:  # noqa: BLE001 — 单连接错误不影响监听
                            log.warning("唤醒已收到但 on_show 失败：",
                                        exc_info=True)
                            continue
                        try:
                            conn.sendall(_WAKE_ACK)   # 握手回执：证明我是同类实例
                        except OSError:
                            pass
            except Exception:  # noqa: BLE001 — 单连接错误不影响监听
                log.warning("wake connection failed", exc_info=True)

    threading.Thread(target=_loop, daemon=True, name="single-instance").start()
