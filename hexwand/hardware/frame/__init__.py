"""各品牌的报文帧格式与命令号，按品牌分子模块：

  razer  雷蛇 90 字节帧（VID / 命令号 / 轮询率映射 / CRC / 组帧）
  hidpp  罗技 HID++ 帧（短/长报文 / 板载配置命令 / 组帧）

本文件只做 re-export，保持 ``from .frame import ...`` 的旧用法继续可用；
新增品牌只需在此加一行 ``from .<品牌> import *``。
"""

from __future__ import annotations

from .razer import *   # 雷蛇帧
from .hidpp import *   # 罗技帧
