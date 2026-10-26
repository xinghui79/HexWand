//! 软件层改键：系统级低级鼠标钩子 + SendInput 合成（`core/mouse_hook.py` 的
//! Rust 移植，2026-09-17）。
//!
//! 几条硬约束与 Python 版逐条对应（改之前先读）：
//!
//! 1. **钩子回调里绝对不能调 SendInput**——回调只做「查表 + 入队」，合成由
//!    hw-inject 线程执行。回调由 Windows 在装钩子的线程上**同步**调用，
//!    回调里再注入 = 自锁（全系统鼠标卡死，进程都杀不掉）。
//! 2. **装钩子的线程必须一直在 GetMessage**——否则全系统输入都要等它。
//! 3. **自己合成的事件要放行**——靠 `LLMHF_INJECTED` 标记，否则无限递归。
//! 4. **救命开关** Ctrl+Alt+Shift+X 轮询，按一下在暂停/恢复之间切换。
//! 5. **映射表由 Python 推送**（`set_map`，键名→VK 的解析在 Python 侧完成），
//!    钩子回调**绝不跨 FFI 回调 Python**——那是把 GIL 问题带回来的歧途。
//!    界面上的状态变化（救援键切换 / 合成卡死自动暂停）放进事件队列，
//!    由 Python 侧 `poll_events()` 定时取。

use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::thread;
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{GetLastError, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE,
    KEYBDINPUT, KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, MOUSEINPUT,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, PeekMessageW,
    PostThreadMessageW, SetWindowsHookExW, TranslateMessage,
    UnhookWindowsHookEx, LLMHF_INJECTED, MSLLHOOKSTRUCT, MSG, WH_MOUSE_LL,
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP,
    WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_QUIT, WM_RBUTTONDOWN, WM_RBUTTONUP,
    WM_XBUTTONDOWN, WM_XBUTTONUP,
};

const WHEEL_DELTA: u32 = 120;

const XBUTTON1: u32 = 0x0001;
const XBUTTON2: u32 = 0x0002;

/// 可映射的鼠标目标（顺序即 `set_map` 里 kind=2 的鼠标索引编码；
/// 成对维护的是 UI 侧 `window.py` 的 `_RUST_MOUSE_IDX`（按名字映射，
/// 与 keymap.py 的 dict 顺序无关），改这里的顺序要一起改那张表）
const MOUSE_TARGETS: [&str; 7] = [
    "left", "right", "middle", "x1", "x2", "wheel_up", "wheel_down",
];

/// 救命组合键：Ctrl+Alt+Shift+X
const RESCUE_VKS: [i32; 4] = [0x11, 0x12, 0x10, 0x58];
const RESCUE_POLL_MS: u64 = 200;
const INJECT_QUEUE_MAX: usize = 32;
const INJECT_STALL: Duration = Duration::from_secs(3);
const HOOK_READY_TIMEOUT: Duration = Duration::from_secs(5);
const STOP_UNHOOK_TIMEOUT: Duration = Duration::from_secs(2);   // stop() 等摘钩完成的上限

/// 合成按键时要带 KEYEVENTF_EXTENDEDKEY 的 VK，否则方向键/小键盘会被当数字键。
/// （Python 侧曾有个同名常量 `core/keyboard.EXTENDED_VKS`，随合成键整体迁到
/// Rust 后已删除——这条注释不再指向它。）
const EXTENDED_VKS: [u16; 15] = [
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B,
    0x5C, 0x6F, 0x90,
];

#[derive(Clone, Copy, PartialEq)]
enum Phase {
    Down,
    Up,
    Wheel,
}

#[derive(Clone)]
enum Action {
    Disable,
    Key(Vec<u16>),
    Mouse(&'static str),
}

#[derive(Clone)]
enum Job {
    Key(Vec<u16>),
    Mouse(&'static str, bool),
}

/// 钩子回调访问的全局状态（回调是系统级函数指针，不能捕获环境）
struct Shared {
    map: RwLock<HashMap<String, Action>>,
    enabled: AtomicBool,
    alive: AtomicBool,
    hook_installed: AtomicBool,
    hook_tid: AtomicU32,
    dropped: AtomicU64,
    stalls: AtomicU64,
    err: Mutex<String>,
    events: Mutex<Vec<bool>>,
    inject_tx: Mutex<Option<SyncSender<Option<Job>>>>,
    inflight: Mutex<Option<Instant>>,
    /// 已被合成按下、但**尚未抬起**的鼠标键目标（由注入线程维护）。
    ///
    /// 按下与抬起是两个独立 job：队列满丢一个、3s 卡死自动暂停、退出收尾，都会
    /// 让"按下"发出去而"抬起"永远不来 —— 那个键就卡在按下态，是**系统级**的
    /// （所有程序都受影响）。三条路径都要有兜底（2026-09-17 审查发现）：
    /// 卡死与收尾走 release_held；队列满时钩子回调里不能注入，由注入线程
    /// （up_pending，主补偿）与 rescue 线程（dropped_up 计数变化，兜底）
    /// 代为 release_held（2026-09-18 补齐）。
    held: Mutex<Vec<&'static str>>,
    /// "抬起" job 入队失败的次数（钩子回调只计数，补偿由 rescue 线程做）
    dropped_up: AtomicU64,
    /// 有"抬起"被丢的待偿标志：注入线程**处理完每个 job 后**检查并 release_held。
    /// 只靠 rescue 的计数变化不够——丢 UP 时 DOWN 可能还排在队列里，
    /// rescue 先跑一步会对着空表补偿，随后 DOWN 注入、卡键再没人管。
    up_pending: AtomicBool,
    /// 钩子线程摘钩完成的通知（stop() 用它等待"钩子确实摘掉了"）
    unhook_rx: Mutex<Option<Receiver<()>>>,
}

impl Shared {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            map: RwLock::new(HashMap::new()),
            enabled: AtomicBool::new(true),
            alive: AtomicBool::new(false),
            hook_installed: AtomicBool::new(false),
            hook_tid: AtomicU32::new(0),
            dropped: AtomicU64::new(0),
            stalls: AtomicU64::new(0),
            err: Mutex::new(String::new()),
            events: Mutex::new(Vec::new()),
            inject_tx: Mutex::new(None),
            inflight: Mutex::new(None),
            held: Mutex::new(Vec::new()),
            dropped_up: AtomicU64::new(0),
            up_pending: AtomicBool::new(false),
            unhook_rx: Mutex::new(None),
        })
    }

    fn set_enabled(&self, on: bool) {
        if self.enabled.swap(on, Ordering::SeqCst) != on {
            self.events.lock().unwrap().push(on);
        }
    }
}

static SHARED: OnceLock<Arc<Shared>> = OnceLock::new();

fn vk_down(vk: i32) -> bool {
    unsafe { (GetAsyncKeyState(vk) as u16 & 0x8000) != 0 }
}

fn button_of(msg: u32, mouse_data: u32) -> Option<(&'static str, Phase)> {
    match msg {
        WM_LBUTTONDOWN => Some(("left", Phase::Down)),
        WM_LBUTTONUP => Some(("left", Phase::Up)),
        WM_RBUTTONDOWN => Some(("right", Phase::Down)),
        WM_RBUTTONUP => Some(("right", Phase::Up)),
        WM_MBUTTONDOWN => Some(("middle", Phase::Down)),
        WM_MBUTTONUP => Some(("middle", Phase::Up)),
        WM_XBUTTONDOWN | WM_XBUTTONUP => {
            let which = (mouse_data >> 16) & 0xFFFF;
            let bid = match which {
                XBUTTON1 => "x1",
                XBUTTON2 => "x2",
                _ => "",
            };
            if bid.is_empty() {
                None
            } else {
                Some((
                    bid,
                    if msg == WM_XBUTTONDOWN {
                        Phase::Down
                    } else {
                        Phase::Up
                    },
                ))
            }
        }
        WM_MOUSEWHEEL => {
            // mouseData 高 16 位是有符号的滚轮格数
            let delta = (mouse_data >> 16) as u16 as i16;
            Some((
                if delta > 0 { "wheel_up" } else { "wheel_down" },
                Phase::Wheel,
            ))
        }
        _ => None,
    }
}

fn enqueue(shared: &Shared, job: Job) -> Result<(), mpsc::TrySendError<Option<Job>>> {
    let guard = shared.inject_tx.lock().unwrap();
    match guard.as_ref() {
        Some(tx) => tx.try_send(Some(job)),
        None => Err(mpsc::TrySendError::Full(Some(job))),
    }
}

/// 按映射决定是否吃掉事件。返回 true = 已吃掉（钩子返回 1）。
fn handle(shared: &Shared, msg: u32, mouse_data: u32) -> bool {
    let Some((bid, phase)) = button_of(msg, mouse_data) else {
        return false;
    };
    let action = shared.map.read().unwrap().get(bid).cloned();
    let Some(action) = action else { return false };
    match action {
        Action::Disable => true, // 禁用：按下抬起都吃掉
        Action::Key(vks) => {
            // 键盘动作按"一次点击"处理：按下时发一整组，抬起只吃掉事件。
            // 空键名（半成品）在 Python 推送时就被过滤掉了，不会进表。
            if phase != Phase::Up && enqueue(shared, Job::Key(vks)).is_err() {
                shared.dropped.fetch_add(1, Ordering::SeqCst);
            }
            true
        }
        Action::Mouse(target) => {
            let is_wheel = target == "wheel_up" || target == "wheel_down";
            let jobs: Vec<Job> = if is_wheel {
                // 滚轮目标只有"一发"：按下时滚一格，抬起吃掉
                if phase != Phase::Up {
                    vec![Job::Mouse(target, false)]
                } else {
                    vec![]
                }
            } else if phase == Phase::Up {
                vec![Job::Mouse(target, true)]
            } else if phase == Phase::Down {
                vec![Job::Mouse(target, false)]
            } else {
                // 源是滚轮：补一对按下+抬起
                vec![Job::Mouse(target, false), Job::Mouse(target, true)]
            };
            for job in jobs {
                let is_up = matches!(job, Job::Mouse(_, true));
                if enqueue(shared, job).is_err() {
                    shared.dropped.fetch_add(1, Ordering::SeqCst);
                    if is_up {
                        // 丢的是"抬起"：DOWN 已经/将会注入，缺这一下键就卡住。
                        // 回调里不能 SendInput，留给注入线程与 rescue 代偿。
                        shared.dropped_up.fetch_add(1, Ordering::SeqCst);
                        shared.up_pending.store(true, Ordering::SeqCst);
                    }
                }
            }
            true
        }
    }
}

unsafe extern "system" fn hook_proc(code: i32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    let Some(shared) = SHARED.get() else {
        return CallNextHookEx(std::ptr::null_mut(), code, wp, lp);
    };
    if code < 0 || !shared.enabled.load(Ordering::SeqCst) {
        return CallNextHookEx(std::ptr::null_mut(), code, wp, lp);
    }
    let msg = wp as u32;
    if msg == WM_MOUSEMOVE {
        return CallNextHookEx(std::ptr::null_mut(), code, wp, lp);
    }
    let info = &*(lp as *const MSLLHOOKSTRUCT);
    if info.flags & LLMHF_INJECTED != 0 {
        // 自己合成的事件放行，不然无限递归
        return CallNextHookEx(std::ptr::null_mut(), code, wp, lp);
    }
    // 回调里绝不能让 panic 冒出去：宁可这次放行，也不能把钩子搞挂
    let eaten = std::panic::catch_unwind(|| handle(shared, msg, info.mouseData))
        .unwrap_or(false);
    if eaten {
        return 1;
    }
    CallNextHookEx(std::ptr::null_mut(), code, wp, lp)
}

fn hook_loop(shared: Arc<Shared>, ready: Sender<()>, unhook_tx: Sender<()>) {
    unsafe {
        let mut msg: MSG = std::mem::zeroed();
        // 先 Peek 一次把本线程的消息队列建出来：否则 stop() 的
        // PostThreadMessageW 会因"线程还没有消息队列"而失败
        PeekMessageW(&mut msg, std::ptr::null_mut(), 0x0400, 0x0400, 0);
        let hook = SetWindowsHookExW(WH_MOUSE_LL, Some(hook_proc), std::ptr::null_mut(), 0);
        if hook.is_null() {
            *shared.err.lock().unwrap() =
                format!("装鼠标钩子失败（错误码 {}）", GetLastError());
            // 不写 hook_tid：本线程马上退出，留着一个会被系统回收复用的 TID，
            // 只会让后来的 stop() 把 WM_QUIT 投进别人线程的消息队列
            let _ = ready.send(());
            // 顺带叫注入线程收工：它此刻正阻塞在 recv() 上，不通知就会一直挂着
            // （要等下次 start() 换掉发送端才会自然回收）。与 stop() 一样，
            // 先把发送端克隆出来解锁，再用非阻塞 try_send。
            let tx = {
                let guard = shared.inject_tx.lock().unwrap();
                guard.as_ref().cloned()
            };
            if let Some(tx) = tx {
                let _ = tx.try_send(None);
            }
            let _ = unhook_tx.send(());      // 失败路径也放行 stop() 的等待
            shared.alive.store(false, Ordering::SeqCst);   // start() 置过位，这里收掉
            return;
        }
        shared.hook_tid.store(GetCurrentThreadId(), Ordering::SeqCst);
        shared.hook_installed.store(true, Ordering::SeqCst);
        let _ = ready.send(());

        let mut rc: i32 = 1;
        while shared.alive.load(Ordering::SeqCst) {
            rc = GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0);
            if rc <= 0 {
                break;
            }
            TranslateMessage(&mut msg);
            DispatchMessageW(&mut msg);
        }
        // rc<0：GetMessage 出错。以前这跟正常退出走同一条路——改键静默失效，
        // 用户只能靠鼠标失灵来发现。要留下来看得见 + 通知界面刷新。
        if rc < 0 && shared.alive.load(Ordering::SeqCst) {
            *shared.err.lock().unwrap() = format!(
                "鼠标钩子消息循环出错（LastError={}），改键已停止",
                GetLastError()
            );
            shared.set_enabled(false);
        }
        // 本线程即将结束：alive 一并清零，否则 rc<0 路径下 rescue_loop 的
        // `while alive` 永远成立，线程泄漏到进程结束（2026-09-18 审查发现）。
        shared.alive.store(false, Ordering::SeqCst);
        // 摘钩子交给钩子线程自己：在别的线程直接 Unhook 会与正在执行的回调互等
        UnhookWindowsHookEx(hook);
        shared.hook_installed.store(false, Ordering::SeqCst);
        // TID 随线程一起作废（Windows 会回收并复用线程 id；quit_app 与
        // atexit 会各调一次 stop()，第二次绝不能把 WM_QUIT 投给复用的 id）
        shared.hook_tid.store(0, Ordering::SeqCst);
        let _ = unhook_tx.send(());
    }
}

/// 记录"这个目标键现在是按下还是抬起"（滚轮没有按下态，不入表）。
fn track_held(shared: &Shared, bid: &'static str, up: bool) {
    if bid == "wheel_up" || bid == "wheel_down" {
        return;
    }
    let mut held = shared.held.lock().unwrap();
    if up {
        held.retain(|t| *t != bid);
    } else if !held.contains(&bid) {
        held.push(bid);
    }
}

/// 松开所有"已按下但没抬起"的目标键。幂等（释放后表即清空）。
///
/// 这是"卡键"的唯一解药：合成出去的是**真实系统输入**，不补一个 UP，
/// 那个键就真的一直按着（所有程序都受影响）。
fn release_held(shared: &Shared) {
    let held: Vec<&'static str> = std::mem::take(&mut *shared.held.lock().unwrap());
    for bid in held {
        send_mouse(bid, true);
    }
}

fn inject_loop(shared: Arc<Shared>, rx: Receiver<Option<Job>>) {
    while let Ok(msg) = rx.recv() {
        match msg {
            None => break,                  // 退出信标
            Some(job) => {
                *shared.inflight.lock().unwrap() = Some(Instant::now());
                match job {
                    Job::Key(vks) => send_key(&vks),
                    Job::Mouse(bid, up) => {
                        track_held(&shared, bid, up);
                        send_mouse(bid, up);
                    }
                }
                *shared.inflight.lock().unwrap() = None;
                if shared.up_pending.swap(false, Ordering::SeqCst) {
                    // 队列满丢过"抬起"：此刻刚注入的 DOWN 已记入 held，
                    // 在这里代偿才不会对着空表补偿（rescue 的计数补偿是兜底）
                    release_held(&shared);
                }
            }
        }
    }
    // 退出前松开仍按着的目标键（收到信标、或发送端全部释放都会走到这里）
    release_held(&shared);
}

fn rescue_loop(shared: Arc<Shared>) {
    let mut was_pressed = false;
    let mut last_dropped_up = shared.dropped_up.load(Ordering::SeqCst);
    while shared.alive.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(RESCUE_POLL_MS));
        if !shared.alive.load(Ordering::SeqCst) {
            return;
        }
        // 队列满丢过"抬起"→ 本线程代偿松开残留键（200ms 节拍最多让键多按
        // 一小会儿，远好于永久卡住；重复的 UP 对系统无害）。
        let dup = shared.dropped_up.load(Ordering::SeqCst);
        if dup != last_dropped_up {
            last_dropped_up = dup;
            release_held(&shared);
        }
        let pressed = RESCUE_VKS.iter().all(|&vk| vk_down(vk));
        // 组合键按住会连续多轮轮询到 true，只在"松开→按下"的边沿切一次
        if pressed && !was_pressed {
            let on = shared.enabled.load(Ordering::SeqCst);
            shared.set_enabled(!on);
        }
        was_pressed = pressed;
        // 合成卡死兜底：SendInput 超时未返回就自动暂停（保持鼠标可用）
        let stale = { *shared.inflight.lock().unwrap() };
        if let Some(since) = stale {
            if since.elapsed() >= INJECT_STALL {
                *shared.inflight.lock().unwrap() = None;
                shared.stalls.fetch_add(1, Ordering::SeqCst);
                shared.set_enabled(false);
                // 卡住的注入线程多半正吊在 SendInput 里，它自己不会再去补 UP ——
                // 这里代它松开已合成的键。此时"跨线程再调 SendInput"的风险是
                // 可接受的：反正注入线程已经卡死，总好过让某个键一直按着。
                release_held(&shared);
            }
        }
    }
}

fn kb_input(vk: u16, mut flags: u32) -> INPUT {
    if EXTENDED_VKS.contains(&vk) {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: 0,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

fn send_key(vks: &[u16]) {
    if vks.is_empty() {
        return;
    }
    // 修饰键按下 → 主键按下抬起 → 修饰键抬起
    let mut inputs: Vec<INPUT> = Vec::with_capacity(vks.len() * 2);
    for &vk in vks {
        inputs.push(kb_input(vk, 0));
    }
    for &vk in vks.iter().rev() {
        inputs.push(kb_input(vk, KEYEVENTF_KEYUP));
    }
    unsafe {
        SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<INPUT>() as i32);
    }
}

fn send_mouse(bid: &str, up: bool) {
    let (flags, data): (u32, u32) = match bid {
        "left" => (if up { MOUSEEVENTF_LEFTUP } else { MOUSEEVENTF_LEFTDOWN }, 0),
        "right" => (if up { MOUSEEVENTF_RIGHTUP } else { MOUSEEVENTF_RIGHTDOWN }, 0),
        "middle" => (if up { MOUSEEVENTF_MIDDLEUP } else { MOUSEEVENTF_MIDDLEDOWN }, 0),
        "x1" => (if up { MOUSEEVENTF_XUP } else { MOUSEEVENTF_XDOWN }, XBUTTON1),
        "x2" => (if up { MOUSEEVENTF_XUP } else { MOUSEEVENTF_XDOWN }, XBUTTON2),
        "wheel_up" => (MOUSEEVENTF_WHEEL, WHEEL_DELTA),
        "wheel_down" => (MOUSEEVENTF_WHEEL, (WHEEL_DELTA as i32).wrapping_neg() as u32),
        _ => return,
    };
    let inp = INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: 0,
                dy: 0,
                mouseData: data,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    unsafe {
        SendInput(1, [inp].as_ptr(), std::mem::size_of::<INPUT>() as i32);
    }
}

// ===========================================================================
// PyO3 封装
// ===========================================================================

use pyo3::exceptions::PyValueError;

#[pyclass]
pub struct MouseRemapper {
    inner: Arc<Shared>,
}

#[pymethods]
impl MouseRemapper {
    #[new]
    fn new() -> Self {
        Self { inner: Shared::new() }
    }

    /// 推送映射表（键名→VK 的解析在 Python 侧完成）。
    /// bids/kinds/data 三列对齐：kind 0=禁用 1=键盘(data=vk 列表) 2=鼠标(data=[目标索引])。
    fn set_map(
        &self,
        bids: Vec<String>,
        kinds: Vec<u8>,
        data: Vec<Vec<isize>>,
    ) -> PyResult<()> {
        if bids.len() != kinds.len() || bids.len() != data.len() {
            return Err(PyValueError::new_err("set_map: 三列长度不一致"));
        }
        let mut m: HashMap<String, Action> = HashMap::new();
        for ((bid, kind), d) in bids.into_iter().zip(kinds).zip(data) {
            match kind {
                0 => {
                    m.insert(bid, Action::Disable);
                }
                1 => {
                    // 非法 VK 必须**报错**：`as u16` 会把负数/超范围值回绕成
                    // 65535 之类的垃圾键码，注入出去的是别家的键（2026-09-18 审查）
                    let mut vks: Vec<u16> = Vec::with_capacity(d.len().min(8));
                    for v in d.into_iter().take(8) {
                        let Ok(vk) = u16::try_from(v) else {
                            return Err(PyValueError::new_err(format!(
                                "set_map: 键码超出 u16 范围: {v}（按键 {bid}）")));
                        };
                        vks.push(vk);
                    }
                    if !vks.is_empty() {
                        m.insert(bid, Action::Key(vks));
                    }
                    // 空 vk 列表＝半成品映射，不进表（原事件放行）
                }
                2 => match d.first().map(|v| *v as usize) {
                    Some(idx) if idx < MOUSE_TARGETS.len() => {
                        m.insert(bid, Action::Mouse(MOUSE_TARGETS[idx]));
                    }
                    _ => {}
                },
                _ => {}
            }
        }
        *self.inner.map.write().unwrap() = m;
        Ok(())
    }

    /// 装钩子。成功返回空串，失败返回原因。
    fn start(&self, py: Python<'_>) -> String {
        if self.inner.hook_installed.load(Ordering::SeqCst) {
            return String::new();
        }
        // stop() 会把 enabled 清零；这里必须复位，否则 stop→start 之后
        // active==True 而钩子全程放行，改键静默失效（2026-09-18 审查发现）。
        self.inner.set_enabled(true);
        self.inner.err.lock().unwrap().clear();
        self.inner.alive.store(true, Ordering::SeqCst);
        // 钩子回调只认 SHARED 里的那一份状态，而 OnceLock 只接受首个实例。
        // 同一个进程里再造一个 MouseRemapper 时，它的 set_map 会写进自己的
        // inner，回调却读旧的 —— 改键静默失效。宁可报错也别装成功。
        // （同一个实例 stop 后再 start 是允许的：ptr_eq 为真。）
        match SHARED.get() {
            Some(existing) if !Arc::ptr_eq(existing, &self.inner) => {
                *self.inner.err.lock().unwrap() =
                    "改键钩子已被本进程的另一个实例占用（只允许一个 MouseRemapper）"
                        .into();
                self.inner.alive.store(false, Ordering::SeqCst);
                return self.inner.err.lock().unwrap().clone();
            }
            Some(_) => {}
            None => {
                let _ = SHARED.set(self.inner.clone());
            }
        }

        let (ready_tx, ready_rx) = mpsc::channel::<()>();
        let (unhook_tx, unhook_rx) = mpsc::channel::<()>();
        *self.inner.unhook_rx.lock().unwrap() = Some(unhook_rx);
        let (itx, irx) = mpsc::sync_channel::<Option<Job>>(INJECT_QUEUE_MAX);
        *self.inner.inject_tx.lock().unwrap() = Some(itx);

        let s_inject = self.inner.clone();
        let _ = thread::Builder::new()
            .name("hw-inject".into())
            .spawn(move || inject_loop(s_inject, irx));
        let s_hook = self.inner.clone();
        let _ = thread::Builder::new()
            .name("hw-mouse-hook".into())
            .spawn(move || hook_loop(s_hook, ready_tx, unhook_tx));

        // 等就绪最多 5s：**不能持着 GIL 等**——PyO3 同步方法默认全程握着 GIL，
        // 那会把 GUI/心跳等所有 Python 线程一起冻住（2026-09-18 审查发现）。
        let ready_timeout =
            py.detach(move || ready_rx.recv_timeout(HOOK_READY_TIMEOUT).is_err());
        if ready_timeout
            && !self.inner.hook_installed.load(Ordering::SeqCst)
        {
            *self.inner.err.lock().unwrap() =
                "装鼠标钩子失败（回调线程超时未就绪）".into();
        }
        if self.inner.hook_installed.load(Ordering::SeqCst) {
            let s_watch = self.inner.clone();
            let _ = thread::Builder::new()
                .name("hw-rescue".into())
                .spawn(move || rescue_loop(s_watch));
            String::new()
        } else {
            self.inner.err.lock().unwrap().clone()
        }
    }

    /// 摘钩子并收尾：发 WM_QUIT / 退出信标，然后**等钩子线程确认摘除**。
    ///
    /// 摘钩子必须由钩子线程自己做（在别的线程 Unhook 会与正在执行的回调互等），
    /// 而这天然是异步的——selftest 那种 stop 后立刻查 active 的用法会撞上竞态。
    /// 所以等 `unhook_rx`（上限 2s，等不到就放弃：Windows 在进程退出时会兜底）。
    fn stop(&self, py: Python<'_>) {
        self.inner.enabled.store(false, Ordering::SeqCst);
        self.inner.alive.store(false, Ordering::SeqCst);
        let tid = self.inner.hook_tid.load(Ordering::SeqCst);
        if tid != 0 {
            unsafe {
                PostThreadMessageW(tid, WM_QUIT, 0, 0);
            }
        }
        // 先把发送端**克隆出来并解锁**再投递退出信号。
        // 不能写成 `if let Some(tx) = self.inner.inject_tx.lock().unwrap().as_ref()`：
        // Rust 2021 里 `if let` 的临时值活到整个语句结束，那样会**持着锁**调用
        // 阻塞式 `send` —— 队列满（正是 SendInput 卡死时会发生的事）就永久卡住，
        // 连下面等摘钩子的 `recv_timeout` 都执行不到：钩子摘不干净、进程也退不掉。
        // 改用 `try_send`：队列满就放弃投递（那条 inject 线程随进程退出被回收）。
        let tx = {
            let guard = self.inner.inject_tx.lock().unwrap();
            guard.as_ref().cloned()
        };
        if let Some(tx) = tx {
            // 队列满（正是 SendInput 卡死时会发生的事）→ 信标进不去，注入线程也就
            // 走不到"退出前松开按键"那一步，于是由本线程兜底释放。
            if tx.try_send(None).is_err() {
                release_held(&self.inner);
            }
        }
        let rx = self.inner.unhook_rx.lock().unwrap().take();
        if let Some(rx) = rx {
            // 同 start()：等摘钩最多 2s，这段必须放开 GIL；到不到回执都不影响收尾
            let _ = py.detach(move || rx.recv_timeout(STOP_UNHOOK_TIMEOUT));
        }
    }

    /// 排空状态事件队列（每项 = enabled 快照）。Python 侧定时轮询。
    fn poll_events(&self) -> Vec<bool> {
        std::mem::take(&mut *self.inner.events.lock().unwrap())
    }

    /// 暂停 / 恢复改键（暂停时一律放行原始事件）。
    fn set_enabled(&self, on: bool) {
        self.inner.set_enabled(on);
    }

    #[getter]
    fn active(&self) -> bool {
        self.inner.hook_installed.load(Ordering::SeqCst)
    }

    #[getter]
    fn enabled(&self) -> bool {
        self.inner.enabled.load(Ordering::SeqCst)
    }

    #[getter]
    fn dropped(&self) -> u64 {
        self.inner.dropped.load(Ordering::SeqCst)
    }

    #[getter]
    fn stalls(&self) -> u64 {
        self.inner.stalls.load(Ordering::SeqCst)
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<MouseRemapper>()?;
    Ok(())
}
