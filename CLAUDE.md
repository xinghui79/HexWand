# CLAUDE.md

本文件为 AI 编码助手（Claude Code / WorkBuddy 等）与人类协作者提供本项目的工作指引。
改动代码前先读完「硬约束」一节——里面的每一条都是实际踩过坑换来的。

## 项目概述

HexWand：Windows 下的雷蛇/罗技无线鼠标控制工具（Qt6 界面）。
功能：DPI 档位、轮询率、电量、节能、软件层改键（低级鼠标钩子）、多设备切换、托盘常驻。

- 语言：Python 3.14（界面与胶水）+ Rust（帧层、低级鼠标钩子，经 PyO3 暴露）
- 界面：只用 PyQt6（`ui/qt.py` 是适配层，别处不许直接 import 绑定）。
  原先"先试 PySide6、失败回落 PyQt6"的双绑定兼容层已删：本机从未装过 PySide6
- 打包：PyInstaller（单文件 exe）

## 常用命令

```powershell
# 运行（开发）
.venv\Scripts\python.exe app.py

# 回归三件套（改完代码必跑）
.venv\Scripts\python.exe app.py --selftest        # 协议层 + 模拟设备 + Rust 帧层/钩子
.venv\Scripts\python.exe app.py --check-imports    # 全模块可加载（29 个）
.venv\Scripts\python.exe preview.py                # 免真机渲染四页截图到 screenshots/
#（另有 --check-gui：只验 Qt 平台插件，不建窗口；build.py 的冻结态冒烟是这三层）

# Rust 扩展（maturin，构建产物别放 OneDrive！）
$env:CARGO_TARGET_DIR = "$env:LOCALAPPDATA\Temp\HexWand_rust_build"
$env:VIRTUAL_ENV = "$PWD\.venv"   # **必须显式指项目 venv**：develop 只喂源码态运行；
D:\rc_build_venv\Scripts\python.exe -m maturin develop --release --manifest-path rust/Cargo.toml
#   打包 venv 走 `maturin build -o <tmp>` + `pip install --force-reinstall --no-deps <whl>`
#   （PyInstaller 从打包 venv 的 site-packages 收走扩展）——以上两步 build.py 自动做，
#   手动只跑 develop 会让打包 venv 留着旧产物（2026-09-17 踩过）。
#   打包 venv 若用 --system-site-packages 建，venv 内没有 console script，
#   需先 `pip install --ignore-installed --no-deps maturin` 装进 venv。

# 打包（用户说"打包"才执行）
双击 打包.bat    # 或 PYTHONIOENCODING=utf-8 D:/rc_build_venv/Scripts/python.exe build.py
#   build.py 顺序：pystray 补丁 + compileall → maturin（两个 venv 都装）→
#   源码态 selftest/check-imports → taskkill → PyInstaller → 新 exe 三层冒烟，
#   冒烟失败自动回滚旧 exe
#   HexWand.spec 的 EXE `icon` 指向 assets/icon.ico（不是根 icon.ico）
```

## 架构

```
app.py                入口 + CLI（--selftest / --check-imports / --scan / --silent）
hexwand/
  hardware/           设备层（不依赖 GUI）
    backends.py       HID 后端薄封装（实现全在 rust/src/hid.rs；唯一后端）
    frame/            razer.py（90 字节帧）+ hidpp.py（罗技 HID++）—— Rust 承载
    protocols.py      各型号私有协议（未收录 PID 拒绝收发）
    catalog.py        型号能力库（DEVICE_DB）
    device.py         MouseDevice：连接、探测、高层 API
    controller.py     device + config 胶水；set_button_action 等
    monitor.py        LinkMonitor：0.2s tick（在线心跳 2s / 离线探测 3s）+ 物理切档状态机
  core/               进程级设施（与设备、GUI 均无关）
    config.py         原子 JSON 持久化（%APPDATA%\HexWand，开发态在项目根）
                      **pid=0 = 未选择设备** 是长期不变量：connect 短路、
                      设备页不并入「我的鼠标」、副标题"未选择设备"三处依赖
    keyboard.py       键名↔VK、组合键解析（取键/合成在 Rust）
    autostart.py / single_instance.py / paths.py / log.py
  ui/                 Qt6 界面（唯一界面层）
    qt.py             绑定适配层：只用 PyQt6；枚举别名集中在此
    theme.py          色板 + 字体 + 全局 QSS
    window.py         主窗口装配 + 四页导航 + 状态机接线
                      （窗口尺寸拆 winsize.py、电量拆 battery.py，mixin 被继承）
    remap.py          键位页：矢量鼠标图 + 编号热点 + 动作编辑
    pages.py          设备页（我的鼠标/其它已知型号）+ 诊断页（人话摘要，
                      原始数据写 startup.log）（内嵌页面，非子窗口）
    hud.py / tray.py  DPI 浮窗 / pystray 托盘
rust/                 PyO3 工程（经 maturin 构建为 hexwand_rust 扩展 = 帧层 crc8/build_report + 改键钩子）
```

分层规则：`hardware/`、`core/` **绝不 import Qt**；跨线程一律经
`MainWindow.posted` 信号（QueuedConnection）回 GUI 线程，子线程不碰控件。
生产者线程已有三个：monitor 状态机、pystray（含托盘菜单回调）、tray-ready
（托盘异步就绪，`on_done` 可能在那条线上触发）。单实例监听线程的
`on_show` **允许短暂阻塞等 GUI 确认**（Event，上限 2s；超时不回 ACK，
让第二实例如实走 wake_failed）。

## 硬约束（每条都真实踩过坑）

1. **钩子回调里绝对不能调 SendInput**（Rust 的 `hook_proc` 同理）。
   低级钩子由装钩子线程同步调用，回调里再注入 = 全系统鼠标自锁，
   进程连 taskkill 都杀不掉。回调只做「查表 + 入队」，合成走 hw-inject 线程。
2. **钩子回调绝不跨 FFI 回调 Python**（PyO3 下等价于把 GIL 带回钩子线程）。
   映射表用**推模式**：Python 调 `set_map()` 推送，Rust 存 RwLock。
   状态通知（救援键/卡死暂停）走 `poll_events()` 轮询，不用回调。
3. **改 `device._simulate` 的模拟值必须同步 `app.py --selftest` 的断言**。
4. **窗口高度 = 所有页面 sizeHint 取 max，且量到连续两次相同就锁定**
   （`_target_win_h` 的 `_win_h_locked`）。切页当下量到的值偏小（布局惰性），
   不锁定就会真实 resize 两次 → Windows 以 frame 为基准重定位 → 窗口漂移。
   两个后续补充：锁在**屏幕指纹变化**（分辨率/缩放/DPI，`_screen_fingerprint`）
   时作废重算；任何 resize 都用 `_restore_top` 显式锚住左上角（singleShot(0)
   里修，同步读 y 是量不到的）。
5. **QSS 里不能写 `#` 开头的注释**（会被当 ID 选择器，把后续规则粘成永不匹配，
   静默失效无报错）。只认 `/* */`。
6. **改 QSS / 自绘控件后必须跑 preview.py 并看图**：整窗截图上几个像素的
   差异肉眼不可辨（"两个箭头"就是这么漏的），要用 dropdown 特写。
7. **Qt 枚举别名集中在 `ui/qt.py`**：其他文件不许 `from PyQt6 import ...`。
   错一个枚举就是 import 期崩，且报错不提示替代品。
8. **禁用态交互的验证必须走真实分发路径**（`QApplication.widgetAt` + `QTest`），
   `sendEvent` 直接投给"以为的接收者"会假通过。
9. **构建产物（`CARGO_TARGET_DIR`、PyInstaller workpath）不能落在项目/OneDrive 里**
   （本地增量写文件会被同步锁拖住）。已统一指到
   `%LOCALAPPDATA%\Temp\HexWand_rust_build` 与 `...\HexWand_build`（见 build.py）。
   **不要再往磁盘根目录散落 tmp 文件夹**。
10. **`HidP_GetCaps` 的返回值是 HIDP_STATUS，成功值 `HIDP_STATUS_SUCCESS`
   (0x00110000)，不是 0**。写成 `!= 0` 会让每次读取能力都被判失败 →
   `feature_len`/`usage_page` 恒为 None/0 → 选不出控制接口 → 鼠标永远「未连接」
   （2026-09-17 真机踩到；判定现在只在 `rust/src/hid.rs` 一处，那里有记录）。
   排查「未连接」的顺序：① 逐条集合打印 `get_feature_len`（控制接口应为
   `mi_00`、feat=91）→ ② 看回帧 status（0x02 成功 / 0x04 超时＝鼠标休眠或在关机
   状态，先让用户动一下鼠标）→ ③ 才怀疑协议字节。
11. **底层只有 Rust 一路，没有兜底**：HID 收发（`rust/src/hid.rs`）、帧层与
   改键钩子都在 `hexwand_rust` 里，它是**硬依赖**。原先的 ctypes 版后端与
   pyhidapi 兜底已按用户要求删除 —— 不要再"顺手加个回落"，那会让"装了旧扩展
   也能跑"重新变成可能。改 Rust 的对外符号要同步 `API_VERSION`（`app.py
   --selftest` 会断言），并确认扩展装进**项目 .venv 与打包 venv 两处**。
12. **"点空白处收掉输入框光标"只能对真正惰性的区域生效**（`MainWindow.eventFilter`
   + `_dismiss_edit_focus`）：判据是"点到的祖先链里有没有交互件"（手型光标或
   接受焦点），命中就一律不动焦点。两个必须记住的前提：
   - 一次点击会沿父链**冒泡多次**（实测 9 次），过滤器会被反复调用，判据只能看
     "点在哪"；
   - 档位段的「档 N」标签与输入框是**兄弟**，按"是否在输入框内部"判定会把
     "点整段就能改数字"的既有行为弄坏（2026-09-17 踩过并修回）。
   同族坑（2026-09-18 补）：程序化 `show()/activateWindow()`（托盘唤醒、启动显示）
   时 Qt 把焦点**白送**给 tab 序第一个可聚焦控件（导航全 NoFocus → "档 1"输入框），
   插入符凭空出现；`_drop_stray_focus` 在 `singleShot(0)` 里清它，但**绝不能清
   isModified 的输入框**——clearFocus 触发 editingFinished → restore，会把用户正打
   一半的数字静默回滚（编辑中点托盘图标，实测踩到）。

## 验证标准

- `--selftest`、`--check-imports`、`preview.py` 三道门全过才算完成。
- preview 日志里六处采样点的 `target=` 必须完全一致（窗口尺寸不随页面变）。
- `main_back.png` 与 `main.png` **不要求**字节一致（模拟设备中途连上会改变
  电量/轮询率显示），判断标准是尺寸一致 + 肉眼无残影。
- 真机改动（钩子/设备层）：跑 `_live_smoke.py` 式的四页轮转冒烟，脚本用完移出项目。

## 约定

- 注释、日志、界面文案一律中文；代码标识符英文。
- 新图片放 `assets/` 并同步 `HexWand.spec` 的 `datas`（缺了就是"源码能跑、
  打包后图没了"）；assets/ 只放运行时会读的文件，源素材/设计稿不进项目。
- 加新 Rust 函数：`#[pyfunction]` 后在 `hexwand_rust` pymodule 里注册；
  帧格式改动必须更新 `--selftest` 里的参考实现 oracle。
- 用户明确说"打包"才执行 build.py / 打包.bat。
