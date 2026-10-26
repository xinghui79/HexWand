"""日志：stdlib logging 写 startup.log，同时带时间戳输出到控制台。

打包（windowed）模式下 sys.stdout 为 None，控制台 handler 会自动跳过。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

_FMT = "%(asctime)s [%(levelname).1s] %(message)s"
_DATEFMT = "%H:%M:%S"

_LOG_MAX_BYTES: int = 1024 * 1024   # startup.log 到 1MB 轮转
_LOG_BACKUPS: int = 3                # 留 startup.log.1/.2/.3，总共最多 ~4MB

_logger: logging.Logger | None = None
_SETUP_LOCK = threading.Lock()   # check-then-act 无锁时两线程会各挂一份 handler


class _RobustRotatingHandler(logging.handlers.RotatingFileHandler):
    """rollover 失败也不让文件日志永久停更。

    父类 doRollover 的顺序是「关 stream → rename → 重开」。第二个实例（或
    OneDrive/杀软）握着同一文件时 rename 抛 PermissionError，异常被
    BaseRotatingHandler.emit 整个兜进 handleError 吞掉，而 stream 已经关掉
    ——**轮转失败一次，之后的日志就再也进不了文件**（实测复现，2026-09-18
    审查）。这里兜住异常并确保 stream 重新打开：本轮不轮转了，继续往原文件
    追加，下次超上限再试。
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    pass   # 连重开都失败就是目录整体不可用，与挂不上 handler 同格


def setup_logging(log_path: Path, *, level: int = logging.INFO) -> logging.Logger:
    """初始化根 logger。

    日志**一律追加**，绝不用 mode="w" 打开：Windows 允许同一文件被两个进程同时
    打开，第二个实例（"双击 exe 唤醒已有实例"这条最常见的路径）一旦以 w 打开
    startup.log，主实例 handler 的记录偏移就被甩到 EOF 之后，它下一次写入会把
    中间一段填成 NUL，整份日志变成不可读的夹心。要清日志请手动删文件。
    """
    with _SETUP_LOCK:
        return _setup_locked(log_path, level)


def _setup_locked(log_path: Path, level: int) -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("hexwand")
    logger.setLevel(level)
    logger.propagate = False

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 轮转：本程序是常驻托盘进程，每次链路变化/状态写入都可能记一行，
        # 用普通 FileHandler 的话 startup.log 只会一直长（自启装上之后是按年计）。
        # 仍是 mode="a"：绝不用 "w"（见上面 docstring 里那两段 handler 偏移互相
        # 踩成 NUL 夹心的教训）。第二个实例握着同一个文件时 rollover 的 rename
        # 会失败——_RobustRotatingHandler 保证失败后 stream 重开、继续追加，
        # 不会像标准 RotatingFileHandler 那样关流后永久停更。
        fh = _RobustRotatingHandler(
            log_path, mode="a", maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUPS, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        logger.addHandler(fh)
    except OSError:
        pass

    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        logger.addHandler(sh)

    # 一个 handler 都没挂上（%APPDATA 被同步锁占住 + windowed 无控制台）时
    # **不缓存**：缓存了就直接定死零 handler，界面到处写"详见 startup.log"
    # 却永远没有那份文件。留 None 让下一次 get_logger() 再试（低频、可接受）。
    if logger.handlers:
        _logger = logger
    return logger


def get_logger() -> logging.Logger:
    """返回已初始化的 logger；未初始化时自动按默认路径初始化。"""
    if _logger is None:
        from .paths import LOG_PATH
        # 可能仍无 handler（_setup_locked 不缓存零 handler 的logger），
        # 下次调用还会再试——所以这里不能 assert。
        return setup_logging(LOG_PATH)
    return _logger
