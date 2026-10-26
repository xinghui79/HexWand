# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['app.py'],
    pathex=[],
    # Rust 帧层 + 改键钩子（PyO3）的 hexwand_rust 是**硬依赖**（razer.py /
    # window.py 直接 import）。它由 maturin 编译并装进打包环境的 site-packages，
    # PyInstaller 通过 import 分析自动收走（不再手搓 .pyd 放进 binaries）。
    binaries=[],
    # 只读资源统一在 assets/ 下。这里必须逐个列出实际会被读到的文件：
    # 冻结态是 core/paths.py 的 resource() 去 sys._MEIPASS 找，少一个就是
    # "源码态能跑、打包后图标/看板娘全没了"。
    # 注意别把 assets/ 整个目录塞进来。2026-09-17 已清理：mouse_plan.png
    # （键位页鼠标图改矢量绘制）、mascot_full.png / mouse_plan_source.png
    # （源素材）都已移出项目，assets/ 里现在只剩这 4 个运行时文件。
    datas=[('assets/icon.ico', 'assets'),
           ('assets/icon.png', 'assets'),
           ('assets/icon_tray.png', 'assets'),
           ('assets/mascot.png', 'assets')],
    # Qt 绑定的 hooks 由 pyinstaller-hooks-contrib 自动处理（它会带 Qt 的平台插件，
    # 少了 platforms/qwindows.dll 会报 "could not find or load the Qt platform plugin"）。
    # pystray._win32 是动态 import 的，必须显式声明；hexwand_rust 兜底（通常自动收集）。
    hiddenimports=['pystray._win32', 'hexwand_rust'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除用不到的 Qt 模块：不排的话 PyQt6 整个拖进来，onefile 体积
    # 会从 20MB 级涨到 150MB 级、开机自启解包明显变慢。
    # （原先这里还排了一串 PySide6.*：Qt 绑定已收敛为 PyQt6 单路，那些条目是死配置。）
    excludes=['PyQt6.QtNetwork', 'PyQt6.QtQml', 'PyQt6.QtQuick',
              'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets',
              'PyQt6.QtMultimedia', 'PyQt6.QtSql', 'PyQt6.QtTest',
              'PyQt6.QtBluetooth', 'PyQt6.QtDesigner', 'PyQt6.QtPdf',
              'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HexWand',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 壳易被 Defender 拦截/拖慢开机自启的解压过程
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
)
