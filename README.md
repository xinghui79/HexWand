# HexWand

鼠标的本地控制工具：不依赖厂商官方软件，直接通过 HID 协议控制，带系统托盘常驻。

内置设备库，插上即自动识别，多只鼠标可切换：

- 雷蛇（完整控制）：毒蝰 V3 极速版 / 毒蝰 V3 Pro / 炼狱蝰蛇标准版
- 罗技（识别 + 读电量/固件 + 读写 DPI 与轮询率）：G PRO X SUPERLIGHT 2（GPW 三代）

![界面](assets/icon.png)

## 功能

- **DPI 档位**：5 档档位表，点「档 N」立即切换；直接改数字回车保存该档；
  按鼠标上的物理 DPI 键循环切档时，界面同步高亮 + 右下角浮窗显示当前 DPI
- **轮询率**：125 / 250 / 500 / 1000 / 2000 / 4000 / 8000 Hz（2000Hz 及以上
  需要 HyperPolling 接收器配合；协议层读不到的档位会如实提示而不是假装默认值）
- **电量**：大字号百分比 + 电量条（颜色随电量变化），同步到托盘悬停提示；
  多次采样取中位数，防止单次错位响应造成显示毛刺
- **节能**：闲置进入睡眠时间（分钟）、低电量阈值（%）
- **多设备**：首次启动不预设任何型号（右上角显示「未选择设备」）；插上已收录的
  鼠标后**启动时自动认领**第一只，运行中换插需在「设备」页点「切换」。
  扫描识别到的已收录鼠标都会并入「我的鼠标」（可多只，上限 8）
- **诊断**：人话体检摘要（驱动 / 鼠标 / 连接 / 改键，异常才附下一步）；
  后端、逐接口扫描等原始数据写入 startup.log 供排查
- 托盘常驻；托盘就绪时关窗收进托盘；开机自启（写注册表 Run 键）
- 单实例：重复启动只会唤醒已有实例的窗口

界面分四页（顶栏下方导航切换）：**主面板 / 键位 / 设备 / 诊断**。
四页共用同一个窗口尺寸，切页只换内容、窗口不动。

## 运行

双击 `run.bat`（自动找 Python、建 venv、装依赖、启动）。
崩溃时会打印 `%APPDATA%\HexWand\crash.log` 的内容。

命令行：

```
python app.py             # 打开控制窗口
python app.py --silent    # 静默启动：不弹窗，只留托盘（开机自启专用；
                          #   托盘起不来时退回显示窗口，避免看不见也退不出）
python app.py --scan      # 列出本机所有已收录品牌的 HID 设备
python app.py --selftest  # 协议层自检（无 GUI，不碰真机）
python app.py --check-imports   # 全模块可加载
python app.py --check-gui       # Qt 平台插件可加载（不建窗口）
```

需要 Python 3.10+（开发环境 3.14），Windows 10/11。依赖见 `requirements.txt`：
运行期必需 pystray / Pillow，界面需要 **PyQt6**（当前唯一支持的 Qt 绑定；
`ui/qt.py` 是适配层，UI 包只从那里取 Qt 名字，所以将来换绑定只改那一个文件。
原先"先试 PySide6、失败回落 PyQt6"的分支已删——本机从未装过 PySide6，
那条兼容层从未被执行）。没装 PyQt6 时程序起不来——会弹一个原生提示框说明
原因并写进 `startup.log`，不静默退出。

改完界面想看图、又不想让程序接管系统鼠标按键，可以跑 `preview.py`
（把钩子与设备都打了桩，只出截图，输出到 `screenshots/`）。

## 打包

双击 `打包.bat`，或手动：

```
set PYTHONIOENCODING=utf-8     # 必设：build.py 会原样打印 maturin 的输出，
                               # 里面有 ✏️ 一类字符，GBK 控制台会当场崩
D:\rc_build_venv\Scripts\python.exe build.py
```

原生扩展（rust/）用 **maturin** 构建，不再手搓 `.pyd`。`build.py` 的
`build_rust_ext()` 把 `hexwand_rust` 装进**两个环境**（只装一处会出事故，见下）：

1. **项目 `.venv`**（`maturin develop`，显式指 `VIRTUAL_ENV`）——源码态
   `python app.py` / `preview.py` 用它；
2. **打包 venv**（`maturin build` 出 wheel 再 `pip install`）——PyInstaller 的
   import 分析只看得到它自己的 site-packages，随后自动收走扩展。

为什么要两处：曾经只装项目 .venv，而 PyInstaller 跑在打包 venv 里找不到扩展，
就**静默回退**到项目根那份手搓的旧 `hexwand_rust.pyd`——实测 exe 里跑的是几
小时前的旧代码，而 selftest 一路 PASS。兜底是 Rust 侧的 `API_VERSION`（当前 5，
由 `app.py --selftest` 断言）：任何一处装成旧产物都会当场报错。
打包环境（`D:\rc_build_venv`）需装：`pyinstaller` / `pystray` / `Pillow` /
`maturin`（见 `requirements.txt` 说明）。

产物在 `dist/HexWand.exe`（onefile）。开机自启的注册表项指向这个文件，
重新打包后路径不变、无需重写。

## 架构

```
app.py                 入口：CLI 子命令 / 单实例仲裁 / 崩溃日志
preview.py             界面预览：桩掉钩子与设备，只出截图到 screenshots/
make_icon.py           重新生成 assets/ 里的图标
build.py / 打包.bat    一键打包（含冒烟与回滚）
HexWand.spec           PyInstaller 配置（资源清单 / Qt 排除项）
assets/                只读图片资源：icon.ico / icon.png / icon_tray.png / mascot.png
screenshots/           preview.py 的输出（可随时重新生成）
hexwand/
  hardware/            设备层（不依赖 GUI）
    backends.py        HID 后端：枚举/打开/Feature 收发的**薄封装**
                       （实际实现在 rust/src/hid.rs，随 hexwand_rust 构建；
                        已无 ctypes 版与 pyhidapi 兜底——底层只剩 Rust 一路）
    device.py          连接、(report_id, tx_id) 探测、高层控制 API
    controller.py      UI 胶水：连接/切换设备、写设备+存配置组合操作
    monitor.py         后台状态机：在线/离线心跳、DPI 轮询同步、电量节拍
    protocols.py    协议库：基类（不含品牌字节）+ RazerProfile / HidppProfile
    catalog.py      设备库：按协议库自动派生每个 PID 的条目
    keymap.py       按键布局：每个按键在鼠标图上的位置（按设备取布局）
    frame/          报文帧格式与命令号，按品牌分模块
        razer.py     雷蛇标准 90 字节帧（VID / 命令号 / 轮询率映射 / CRC / 组帧）
        hidpp.py     罗技 HID++ 帧（短/长报文 / 板载配置命令 / 组帧）
  core/               与硬件和 UI 都无关的运行期设施
    config.py          AppConfig 数据类 + 原子 JSON 持久化（坏配置容错）
    keyboard.py        键盘取键：VK↔键名、组合键解析、系统键状态轮询（绕开输入法）
    single_instance.py 单实例仲裁（bind-first + 唤醒握手）
    # 注意：软件层改键（低级鼠标钩子 + SendInput 合成 + 救命组合键）已整体迁入
    #       rust/（PyO3），由 maturin 编译为 hexwand_rust 扩展，window.py 直接
    #       import；core/mouse_hook.py 已删除。
    autostart.py       开机自启（注册表 Run 键）
    paths.py / log.py  paths.py 负责解析 assets/ 下的只读资源（源码态/冻结态统一）
  ui/                  Qt6 界面（唯一的界面层）
    qt.py              绑定适配层：只用 PyQt6；枚举别名集中在此
    theme.py           色板 + 字体 + 全局 QSS（圆角走 border-radius，不自绘）
    widgets.py         Card / PillButton / SegmentedControl / RoundedInput /
                       Dropdown / VBar / StageSegment
    window.py          主窗口装配 + 四页导航 + 卡片布局 + 状态机接线 + 跨线程调度桥
                       （窗口尺寸与电量两块拆在同目录 winsize.py / battery.py，
                        以 mixin 形式被 MainWindow 继承）
    winsize.py         窗口几何：宽度固定、高度四页取最大、锁定防漂移
    battery.py         电量卡片与托盘 tooltip（只显示，不判定在线/离线）
    remap.py           键位页：鼠标图热点（QPainter 自绘）+ 清单 + 动作编辑 + 捕获
    pages.py           设备页（我的鼠标 / 其它已知型号 + 切换）
                       + 诊断页（人话摘要；原始数据写 startup.log）
                       —— 都是**内嵌页面**，不是子窗口；sizeHint 固定，不把窗口顶高
    hud.py             物理 DPI 键触发的右下角浮窗（无边框 + 置顶 + 淡入淡出）
    tray.py            pystray 封装（与 GUI 工具包无关，从 Tk 时代原样留用；
                       就绪判定 0.3s 快路径 + 后台补等，结果经回调回主线程）
```

要点：

- 界面只有一套（Qt6）。`hardware/` 与 `core/` 与 GUI 工具包**完全解耦**，
  换界面不用动它们；`hardware/monitor.py` 甚至设计成可脱离界面独立测试
- 所有设备 I/O 在后台线程；跨线程回调用 `MainWindow.posted` 信号
  （QueuedConnection）调度回 GUI 线程——子线程里绝不碰控件
- 高 DPI 屏（125%/150% 缩放）下文字清晰：Qt6 默认就是 per-monitor-v2 感知，
  坐标与字体按设备无关像素自动换算（**代码里没有任何手工 `* scale`**）
- 配置写在 `%APPDATA%\HexWand\config.json`（开发态在项目根目录），
  损坏/手改不会导致崩溃，逐字段回退默认值
- 窗口高度**固定取四个页面里最高的那个**，切页不改变窗口尺寸（否则会出现
  "窗口还没缩回去、下半截残留上一页内容"的中间态）。"设备""诊断"两页的
  `sizeHint` 是固定值，内容超出在页内滚动 —— 不让长列表/长文本把窗口顶高

## 键位页 / 软件层改键

鼠标图、编号热点、按键清单、动作编辑（恢复默认 / 禁用 / 键盘按键 / 鼠标按键）
都已可用，映射存进 `config.json` 的 `keymap`（**全局一份**，对所有鼠标生效——
包括未收录进设备库的型号，因为软件层钩子只认系统标准按键，与品牌无关）。

**改键已经真正生效**：`rust/src/remap.rs`（PyO3，经 maturin 编进
`hexwand_rust`）在专用线程上装一个系统级低级鼠标钩子（`WH_MOUSE_LL`）拦下物理
按键，命中映射就吃掉原事件、在另一个线程用 `SendInput` 合成目标动作；映射表实时
读取，界面上改完立刻生效，不用重启。键位页底部有状态提示和「暂停改键」按钮。
（`core/mouse_hook.py` 是 Tk 时代的实现，**已删除**——钩子与帧层一起在 rust/ 里。）

几条要知道的限制：

- **所有鼠标共用一份映射**：低级钩子是全系统范围的，Windows 不告诉事件来自
  哪只物理鼠标，所以映射就是全局一份——换鼠标、插未收录的型号都照常生效。
  要按设备区分得再做 Raw Input 处理。
- **救命组合键**：万一把左键映射成禁用，鼠标就点不动了。按一下
  **Ctrl+Alt+Shift+X** 暂停改键（所有按键原样放行），再按一下恢复——双向
  切换；键位页右下角的「暂停改键 / 恢复改键」按钮是同一件事。
- 钩子回调跑在装钩子的线程上，必须快进快出、不能碰任何 UI 控件（Qt 下从子线程
  碰控件是未定义行为，比当年 Tk 抛异常更隐蔽）；程序退出时主动摘钩子。异常退出
  的兜底：解释器收尾（atexit）与 Ctrl+C / taskkill（不带 /F）都会补一次
  `remapper.stop()`——它会把残留的按键合成 UP（release-all）再摘钩子。
  **`taskkill /F` / 任务管理器"结束任务"拦不住**（TerminateProcess 进程内收不到
  任何通知）：Windows 会自动摘钩子、输入不会锁，但已按下的合成键无法补救。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 未检测到设备 | 切到「诊断」页看体检摘要；逐接口扫描等原始数据在 `%APPDATA%\HexWand\startup.log`（每次刷新诊断记一条）；确认接收器插好、鼠标开机；关掉厂商的官方鼠标软件（会独占 HID） |
| 提示需要管理员权限 | 右键以管理员身份运行 |
| 鼠标关机/休眠 | 右上角状态词转「未连接」（型号名保留），后台每 3 秒探测，动鼠标自动重连 |
| 双击 exe 没反应 | 会弹提示说明原因（不再静默退出）：唯一实例端口被占用且唤醒不到已有实例，或 Qt 绑定缺失。端口是 **40040**（刻意选在 Windows 临时端口段 49152–65535 以下，否则本机任意程序的出站连接都可能抢占它）；先看托盘里是否已有 HexWand，再确认没有别的服务占 40040 |

## 图片资源（assets/）

所有图片集中在一个目录，`core/paths.py` 负责按源码态 / 冻结态分别解析：

| 文件 | 用途 |
| --- | --- |
| `icon.ico` | 应用图标（exe 图标 + 窗口图标），多尺寸打包进一个 ico |
| `icon.png` | 应用图标 PNG（README、外部引用） |
| `icon_tray.png` | 托盘图标（独立一份：托盘需要带底色徽章，浅色任务栏上才看得见） |
| `mascot.png` | DPI 浮窗右侧的看板娘（透明底，左/上渐隐已烘焙进图片） |

`make_icon.py` 重新生成前三个（Pillow，4x 超采样）。其余设计稿/源素材
（mouse_plan*、mascot_full）2026-09-17 已移出项目，不再放在 assets/ 里。

**加新图片时记得同步 `HexWand.spec` 的 `datas`** —— 冻结态是去 `sys._MEIPASS`
里找的，漏了就是"源码态能跑、打包后图没了"。assets/ 里只放运行时会读的文件，
源素材/设计稿不进项目，免得白白撑大 exe。
