"""应用运行环境：路径、日志、配置、开机自启、单实例、键盘取键。

与设备/界面无关的"进程级"设施都收在这里。各子模块保持独立可 import：
  paths           开发态 / PyInstaller 冻结态的路径解析
  log             stdlib logging 初始化
  config          AppConfig + JSON 持久化
  autostart       HKCU Run 注册表
  single_instance 127.0.0.1 固定端口的唯一实例
  keyboard        键盘取键：VK↔键名、组合键解析、系统键状态轮询

（软件层改键 2026-09-17 起由 rust/ 里的 PyO3 模块 hexwand_rust 提供：
  hexwand_rust.MouseRemapper —— 原先的 core/mouse_hook.py 已删除。）
"""

from __future__ import annotations

__all__ = ["paths", "log", "config", "autostart", "single_instance",
           "keyboard"]
