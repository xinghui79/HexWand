# -*- coding: utf-8 -*-
"""HexWand 一键打包（Windows）。

封装完整打包流程，规避所有已知坑：
  0. 源码 compileall 语法检查（这一步不 import 项目代码，扩展未构建时也能先跑）
  0.5 用 maturin 把 rust/ 编译成 hexwand_rust 扩展并**两个环境都装**：
      develop（显式 VIRTUAL_ENV=项目 .venv，喂源码态/预览）+
      build 出 wheel 后 pip --force-reinstall 进打包 venv
      （不再手搓 .pyd；PyInstaller 随后从打包 venv 的 site-packages 收走）
  0.6 运行时自检：--selftest + --check-imports（都会 import hexwand_rust，
      必须排在上一步之后——顺序反了，新环境里第一步就 ImportError）
  1. taskkill 停掉运行中的 HexWand.exe（否则 exe 被占用无法覆盖）
  2. PyInstaller 输出到 %LOCALAPPDATA%\\Temp\\HexWand_build（避开 OneDrive 同步锁，
     且不在 D 盘根上留文件夹）
  3. 复制回项目 dist/（先备份旧 exe，冒烟失败自动回滚）
  4. 新 exe 三层冒烟：--selftest / --check-imports / --check-gui
     （各须退出码 0；selftest 还断言扩展 API_VERSION，旧产物当场报错）
  5. 清理临时目录（成功路径；cargo 缓存 HexWand_rust_build 保留以复用编译）

用法：双击「打包.bat」，或命令行
  "D:/rc_build_venv/Scripts/python.exe" build.py
打包环境需额外安装：pyinstaller / pystray / Pillow / maturin
（maturin 负责编译 rust/，见根目录 pyproject.toml）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = r"D:/rc_build_venv/Scripts/python.exe"   # 唯一可用打包环境（系统 python 3.14.3）
# 打包临时目录：放在 %LOCALAPPDATA%\Temp 下（避开 OneDrive 同步锁，也不在 D 盘根
# 散落文件夹）。LOCALAPPDATA 缺失时 gettempdir() 本身已是临时目录，不要再拼 Temp。
_local_appdata = os.environ.get("LOCALAPPDATA")
_TMP_BASE = (os.path.join(_local_appdata, "Temp") if _local_appdata
             else tempfile.gettempdir())
TMP_DIR = os.path.join(_TMP_BASE, "HexWand_build")
RUST_TARGET_DIR = os.path.join(_TMP_BASE, "HexWand_rust_build")
SPEC = os.path.join(ROOT, "HexWand.spec")
DIST_EXE = os.path.join(ROOT, "dist", "HexWand.exe")
BACKUP_EXE = DIST_EXE + ".bak"

# pystray 托盘菜单弹出方向补丁：原库用 TPM_RIGHTALIGN|BOTTOMALIGN（菜单右下角
# 对齐鼠标、向左上弹出）——在屏幕右下角托盘处看起来"菜单出现在左侧"。
# 改为 LEFTALIGN|TOPALIGN（左上角对齐鼠标、向右下弹出）= 主流行为。
# venv 重建 / pip 重装 pystray 会还原此补丁，因此每次打包前自动检查并应用。
# 两个环境都要打：只打打包环境的话，run.bat 跑的开发版托盘菜单方向仍是老样子。
PYSTRAY_WIN32S = [
    r"D:/rc_build_venv/Lib/site-packages/pystray/_win32.py",
    os.path.join(ROOT, ".venv", "Lib", "site-packages", "pystray", "_win32.py"),
]
PYSTRAY_PATCH_OLD = "win32.TPM_RIGHTALIGN | win32.TPM_BOTTOMALIGN"
PYSTRAY_PATCH_NEW = "win32.TPM_LEFTALIGN | win32.TPM_TOPALIGN"


def ensure_pystray_patch() -> None:
    """保证每个环境里的 pystray 菜单方向补丁都在位（幂等）。"""
    for path in PYSTRAY_WIN32S:
        if not os.path.exists(path):
            continue                # 该环境没装 pystray（例如 .venv 只做开发用）
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        if PYSTRAY_PATCH_NEW in src:
            continue                # 已在位
        if PYSTRAY_PATCH_OLD in src:
            src = src.replace(PYSTRAY_PATCH_OLD, PYSTRAY_PATCH_NEW)
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            log(f"已应用 pystray 菜单方向补丁: {path}")
        else:
            print(f"[警告] pystray 源码与补丁模式不匹配（可能已升级），请人工检查 "
                  f"{path}", flush=True)

PASS_MARK = "SELFTEST PASS"
IMPORTS_MARK = "IMPORTS OK"

# (步骤名, 传给 venv python 的参数, 输出里必须出现的标记；None=不校验文本)
#
# 分成两组：**语法组不 import 任何项目代码**，所以可以在原生扩展还没构建时先跑；
# 运行时组会 import hexwand_rust（frame/razer.py 是硬依赖），必须排在
# build_rust_ext() 之后 —— 原来三组一起跑在构建之前，扩展没装进环境时第一步就
# ImportError，永远走不到构建那一步（2026-09-17 审查发现）。
SYNTAX_STEPS = [
    ("源码编译检查",
     ["-m", "compileall", "-q", "-f",
      os.path.join(ROOT, "app.py"), os.path.join(ROOT, "hexwand")], None),
]
RUNTIME_STEPS = [
    ("源码 selftest", ["app.py", "--selftest"], PASS_MARK),
    # selftest 不 import ui/pystray/PIL，"GUI 一启动就崩"的包也能过上一道门
    ("依赖与模块可加载", ["app.py", "--check-imports"], IMPORTS_MARK),
]


def log(msg: str) -> None:
    print("== " + msg, flush=True)


def _decode(raw: bytes) -> str:
    """解码子进程输出：windowed exe 的中文输出编码随环境（UTF-8/GBK 都见过），
    逐个尝试，全失败用 replace 兜底（PASS_MARK 是 ASCII，不受影响）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_cmd(cmd: list[str], cwd: str = ROOT, timeout: int = 180,
            check_text: str | None = None, env: dict | None = None) -> None:
    """跑一条子进程命令；可选校验输出包含指定文本。失败抛 SystemExit。

    `env` 给了就整份替换子进程的环境（maturin 靠 `VIRTUAL_ENV` 决定装到哪）。
    输出解码见 `_decode`：windowed exe 的中文输出编码随环境（UTF-8/GBK 都见过），
    而 PASS_MARK 是 ASCII，兜底也不会误判。
    （原先 `run_py` 与 `run_exe` 是逐行复制的两份，只有命令前缀不同——
     2026-09-17 审查后合并成一个。）
    """
    log("执行: " + " ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired:
        print(f"[错误] 命令超时({timeout}s): {cmd}", flush=True)
        raise SystemExit(1)
    out = _decode((r.stdout or b"")) + _decode((r.stderr or b""))
    if out:
        # 原来只在 stdout 非空时回显：compileall / PyInstaller 的报错常常只走
        # stderr，那时这里就只剩一个退出码，失败原因全丢了
        sys.stdout.write(out[-2000:])
    if r.returncode != 0:
        print(f"[错误] 命令失败，退出码 {r.returncode}: {cmd}", flush=True)
        raise SystemExit(1)
    if check_text and check_text not in out:
        print(f"[错误] 输出中未找到预期文本「{check_text}」", flush=True)
        raise SystemExit(1)


def run_py(args: list[str], cwd: str = ROOT, timeout: int = 180,
           check_text: str | None = None, env: dict | None = None) -> None:
    """用打包环境 python 跑命令。"""
    run_cmd([VENV_PY] + args, cwd=cwd, timeout=timeout,
            check_text=check_text, env=env)


def run_exe(args: list[str], cwd: str = ROOT, timeout: int = 180,
            check_text: str | None = None) -> None:
    """直接运行打包后的 exe（windowed 程序；不能用 python 去解释它）。"""
    run_cmd(args, cwd=cwd, timeout=timeout, check_text=check_text)


def build_rust_ext() -> None:
    """用 maturin 编译 rust/，并**装进两个环境**。

    为什么必须装两处（2026-09-17 踩到并修）：
      · **打包 venv（本进程的解释器）**：PyInstaller 的 import 分析只看得到
        它自己的 site-packages。只装项目 .venv 的话，分析会**静默回退**到项目根
        那份手搓的 `hexwand_rust.pyd` —— 实测 bundled 进 exe 的是几小时前的旧扩展，
        当天所有 Rust 改动完全没生效，而 selftest 照样 PASS（旧代码也打同样的日志）。
        所以这里用 `maturin build` 出 wheel 再 `pip install` 进本环境。
      · **项目 .venv**：源码态 `python app.py` / `preview.py` 用的是它。
        `maturin develop` 默认挑项目目录里的 .venv，正合适。

    Rust 侧的 `API_VERSION` 与 `app.py --selftest` 的断言是这条链路的兜底：
    任何一处装成旧产物，selftest 会当场失败而不是静默跑旧代码。
    """
    if shutil.which("cargo") is None:
        print("[错误] 未找到 cargo / Rust 工具链，无法构建原生扩展 hexwand_rust。",
              flush=True)
        print("       请先安装 Rust (https://rustup.rs) 再打包。", flush=True)
        raise SystemExit(1)
    # 构建产物指到本机临时目录，避开 OneDrive 同步锁（cargo 读 CARGO_TARGET_DIR）
    os.environ["CARGO_TARGET_DIR"] = RUST_TARGET_DIR
    manifest = os.path.join(ROOT, "rust", "Cargo.toml")
    run_py(["-m", "maturin", "--version"], cwd=ROOT, timeout=60)

    # 1) 装进项目 .venv（源码态/预览脚本用）
    #    **必须显式指 VIRTUAL_ENV**：run_py 用的是打包 venv 的解释器，不指的话
    #    maturin 会把扩展装进打包 venv，项目 .venv 拿到的还是旧产物 ——
    #    那正是"改了 Rust、源码态跑的还是老代码"的老坑（2026-09-17 踩过）。
    project_venv = os.path.join(ROOT, ".venv")
    if os.path.isdir(project_venv):
        env = dict(os.environ)
        env["VIRTUAL_ENV"] = project_venv
        env["CARGO_TARGET_DIR"] = RUST_TARGET_DIR
        run_py(["-m", "maturin", "develop", "--release",
                "--manifest-path", manifest], cwd=ROOT, timeout=600, env=env)
    else:
        print("[注意] 没找到项目 .venv，跳过源码态扩展安装（只装打包 venv）。",
              flush=True)

    # 2) 装进打包 venv（PyInstaller 从这里收走）
    wheel_dir = os.path.join(TMP_DIR, "wheel")
    shutil.rmtree(wheel_dir, ignore_errors=True)
    os.makedirs(wheel_dir, exist_ok=True)
    run_py(["-m", "maturin", "build", "--release",
            "--manifest-path", manifest, "-o", wheel_dir],
           cwd=ROOT, timeout=600)
    wheels = [os.path.join(wheel_dir, f) for f in os.listdir(wheel_dir)
              if f.endswith(".whl")]
    if not wheels:
        print(f"[错误] maturin 没有产出 wheel：{wheel_dir}", flush=True)
        raise SystemExit(1)
    # --force-reinstall：确保旧扩展被换掉（否则 pip 认为版本号没变就跳过）
    run_py(["-m", "pip", "install", "--force-reinstall", "--no-deps", wheels[0]],
           cwd=ROOT, timeout=300)


def main() -> None:
    print("=" * 56, flush=True)
    print("  HexWand 一键打包", flush=True)
    print("=" * 56, flush=True)

    # selftest 全部是 assert：-O / PYTHONOPTIMIZE 下断言被整体剥掉，零检查
    # 也照样打印 SELFTEST PASS，这道门就形同虚设了
    if not __debug__ or os.environ.get("PYTHONOPTIMIZE"):
        print("[错误] 不能在 -O / PYTHONOPTIMIZE 下打包：selftest 的断言会被剥掉，"
              "冒烟等于没跑。", flush=True)
        raise SystemExit(1)

    if not os.path.exists(VENV_PY):
        print(f"[错误] 打包环境不存在：{VENV_PY}", flush=True)
        print("       需要先用系统 Python 3.14 建 venv 并安装 pyinstaller / "
              "pystray / Pillow（见项目笔记 MEMORY.md 打包流程）。", flush=True)
        raise SystemExit(1)

    # 0. 语法检查（纯 compileall，不 import 项目代码）+ pystray 补丁
    ensure_pystray_patch()
    for _name, args, mark in SYNTAX_STEPS:
        run_py(args, check_text=mark)

    # 0.5 构建 Rust 原生扩展（maturin develop → 装进打包环境）。
    #     必须在运行时自检之前：selftest / check-imports 都会 import hexwand_rust。
    build_rust_ext()

    # 0.6 运行时自检（selftest + 全模块可加载）
    for _name, args, mark in RUNTIME_STEPS:
        run_py(args, check_text=mark)

    # 1. 停掉运行中的程序
    log("停止运行中的 HexWand.exe（若在运行）")
    # taskkill 按镜像名打（这台机器上就这一个 HexWand.exe），但要带 timeout：
    # 万一卡在杀进程上，整个打包就挂死了
    try:
        subprocess.run(["taskkill", "/F", "/IM", "HexWand.exe"],
                       capture_output=True, timeout=30)   # 没在运行时报错，忽略
    except subprocess.TimeoutExpired:
        print("[警告] taskkill 30s 没返回，继续打包（后续覆盖 exe 若失败会回滚）。",
              flush=True)

    # 2. PyInstaller（临时目录在 %LOCALAPPDATA%\Temp 下）
    #    从这里到收尾整体 try/finally：中途任何 SystemExit/异常都要把临时目录
    #    清掉（docstring 承诺过"清理临时目录"，原来只在成功路径执行，失败时会把
    #    整棵 PyInstaller 工作树留在 Temp 里）。
    try:
        if os.path.exists(TMP_DIR):
            shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        run_py(["-m", "PyInstaller", SPEC, "--noconfirm",
                "--distpath", os.path.join(TMP_DIR, "dist"),
                "--workpath", os.path.join(TMP_DIR, "build")],
               timeout=300)

        new_exe = os.path.join(TMP_DIR, "dist", "HexWand.exe")
        if not os.path.exists(new_exe):
            print(f"[错误] 打包产物缺失：{new_exe}", flush=True)
            raise SystemExit(1)

        # 3. 备份旧 exe → 复制新 exe
        #    **复制失败也必须回滚**：copy2 是"先截断目标再写"，中途失败会留下半截
        #    的 exe —— 比冒烟失败更需要把旧文件还原回去（2026-09-17 审查发现：
        #    原来这条路径只 raise，备份等于白做）。
        log("备份旧 exe 并复制新 exe 到 dist/")
        if os.path.exists(BACKUP_EXE):
            os.remove(BACKUP_EXE)
        if os.path.exists(DIST_EXE):
            shutil.copy2(DIST_EXE, BACKUP_EXE)

        def _rollback(why: str) -> None:
            if os.path.exists(BACKUP_EXE):
                shutil.copy2(BACKUP_EXE, DIST_EXE)
                print(f"[回滚] {why}，已恢复旧 exe。", flush=True)
            else:
                print(f"[注意] 没有可回滚的旧 exe（首次打包？）：{why}；"
                      f"新 exe 可能不完整，请不要使用 {DIST_EXE}。", flush=True)

        try:
            shutil.copy2(new_exe, DIST_EXE)
        except OSError as e:
            print(f"[错误] 复制 exe 失败（可能仍被占用）: {e}", flush=True)
            _rollback("复制新 exe 失败")
            raise SystemExit(1)

        # 4. 新 exe 冒烟（三层：协议层 / 模块可加载 / Qt 平台插件能加载）
        log("新 exe 冒烟（selftest + 模块可加载 + Qt 平台插件）")
        try:
            run_exe([DIST_EXE, "--selftest"], timeout=120, check_text=PASS_MARK)
            # 冻结态缺 hiddenimport 只有真去 import 那个模块才暴露；selftest 不碰 ui
            run_exe([DIST_EXE, "--check-imports"], timeout=120,
                    check_text=IMPORTS_MARK)
            # 再狠一层：真去构造一次 QApplication —— 缺 plugins/platforms/
            # qwindows.dll 只会在这一步炸，而那时 console=False 的 exe 一点输出
            # 也没有，用户看到的就是"双击没反应"（2026-09-17 审查）。
            run_exe([DIST_EXE, "--check-gui"], timeout=120,
                    check_text="GUI OK")
        except SystemExit:
            _rollback("新 exe 冒烟失败")
            raise

        # 5. 成功收尾：备份不再需要（冒烟已过）
        if os.path.exists(BACKUP_EXE):
            os.remove(BACKUP_EXE)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    size = os.path.getsize(DIST_EXE)
    print("=" * 56, flush=True)
    print(f"  打包完成 ✔  dist\\HexWand.exe  ({size:,} 字节)", flush=True)
    print("=" * 56, flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 未预期异常: {e}", flush=True)
        raise SystemExit(1)
