//! 原生 HID 后端（Windows）：用 `windows-sys` 直调 hid.dll / setupapi.dll / kernel32。
//!
//! 这是 Python `hardware/backends.py` 那段 ctypes 代码的 Rust 迁移版：迁移期间用
//! 真机 A/B 逐字节比对确认过两者等价，之后 ctypes 版与 pyhidapi 兜底都已删除 ——
//! **本文件现在是 HID 访问的唯一实现，没有回落分支**（见 backends.py 模块注释）。
//!
//! 移植时最容易出错、也最容易忘的三处（都是踩过的坑，别"顺手改"）：
//!   1. `HidP_GetCaps` 的返回值是 **HIDP_STATUS**，成功 = `0x00110000`，不是 0。
//!      写成 `!= 0` 会把每次读能力都判成失败 → feature_len/usage_page 恒 None/0 →
//!      选不出控制接口 → 鼠标永远「未连接」（2026-09-17 真机 root cause）。
//!   2. `HidD_Get/SetFeature` 的缓冲区长度必须**正好等于** FeatureReportByteLength，
//!      多一个字节都会被拒（ERROR_INVALID_PARAMETER）。
//!   3. `CreateFileW` 用 **access=0**（查询权限）：实测读写权限反而会被拒
//!      （某些集合返回 ERROR_ACCESS_DENIED=5），而 0 权限能正常读能力、也能收发
//!      feature report。

use std::ffi::c_void;

use pyo3::exceptions::{PyOSError, PyRuntimeError};
use pyo3::prelude::*;

use windows_sys::Win32::Devices::DeviceAndDriverInstallation::{
    SetupDiDestroyDeviceInfoList, SetupDiEnumDeviceInterfaces, SetupDiGetClassDevsW,
    SetupDiGetDeviceInterfaceDetailW, SP_DEVICE_INTERFACE_DATA,
    SP_DEVICE_INTERFACE_DETAIL_DATA_W,
};
use windows_sys::Win32::Devices::HumanInterfaceDevice::{
    HidD_FreePreparsedData, HidD_GetAttributes, HidD_GetFeature, HidD_GetPreparsedData,
    HidD_GetProductString, HidD_SetFeature, HidP_GetCaps, HIDD_ATTRIBUTES, HIDP_CAPS,
};
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::GetAsyncKeyState;

/// 雷蛇 90 字节帧的长度（与 Python 侧 frame.razer.REPORT_LEN 一致）。
const REPORT_LEN: usize = 90;

/// `HidP_GetCaps` 的成功值 —— **不是 0**（见模块头注释第 1 条）。
const HIDP_STATUS_SUCCESS: i32 = 0x0011_0000;

const DIGCF_PRESENT: u32 = 0x0000_0002;
const DIGCF_DEVICEINTERFACE: u32 = 0x0000_0010;

/// GUID_DEVINTERFACE_HID = {4D1E55B2-F16F-11CF-88CB-001111000030}
/// （自己声明而不用 windows-sys 的常量，免得版本间 feature 名有差异）
const GUID_DEVINTERFACE_HID: windows_sys::core::GUID = windows_sys::core::GUID {
    data1: 0x4D1E_55B2,
    data2: 0xF16F,
    data3: 0x11CF,
    data4: [0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30],
};

/// 一个已打开的 HID 句柄。存成 isize 而不是裸指针：`#[pyclass]` 要求 `Send`，
/// 指针不满足；而且 Python 侧本来也只当它是个不透明句柄。
///
/// 用**原子类型**而不是 `Cell`：`#[pyclass]` 还要求 `Sync`，而 `Cell` 不是
/// `Sync`（编译期直接拒）。原子同时给了"关闭并置零"的能力 —— `close()` 与
/// `Drop` 都必须**幂等**：重复关闭会去关一个可能已被系统复用的句柄号，
/// 那比泄漏更糟。
#[pyclass]
pub struct HidHandle {
    h: std::sync::atomic::AtomicIsize,
    /// 能力缓存：本句柄的 FeatureReportByteLength；**0 = 尚未读取**。
    /// 一轮 set+get 原本要读两次 `HidP_GetCaps`（每次都是 GetPreparsedData +
    /// GetCaps + FreePreparsedData 三次系统调用）。缓存在**句柄对象**上最稳：
    /// Windows 会复用句柄号，按句柄数值建表会命中上一台设备的过期能力值。
    /// 只存 feature_len——UsagePage 只有枚举路径用得到，而枚举走的是
    /// `caps_of()` 直接读，不经过这里（存了也从来没人取）。
    caps: std::sync::atomic::AtomicU32,
}

impl HidHandle {
    fn new(h: HANDLE) -> Self {
        Self {
            h: std::sync::atomic::AtomicIsize::new(h as isize),
            caps: std::sync::atomic::AtomicU32::new(0),
        }
    }

    fn raw(&self) -> HANDLE {
        self.h.load(std::sync::atomic::Ordering::Relaxed) as HANDLE
    }

    /// 关闭句柄并置零；返回"这次真的关了没有"（已关过 → false）。
    fn shutdown(&self) -> bool {
        let h = self.h.swap(0, std::sync::atomic::Ordering::SeqCst);
        if bad_handle(h as HANDLE) {
            return false;
        }
        unsafe { CloseHandle(h as HANDLE) };
        true
    }

    /// 该句柄的 Feature 报表长度；读一次之后命中缓存。
    fn feature_len(&self) -> Option<u16> {
        let cached = self.caps.load(std::sync::atomic::Ordering::Relaxed);
        if cached != 0 {
            return Some(cached as u16);
        }
        let c = caps_of(self.raw())?;
        let flen = c.FeatureReportByteLength;
        if flen == 0 {
            // 真读到了、但这接口没有 feature 报表。0 是"未缓存"哨兵，不能存它，
            // 否则每次收发都会重新去读一遍 caps。
            return None;
        }
        self.caps.store(flen as u32, std::sync::atomic::Ordering::Relaxed);
        Some(flen)
    }
}

impl Drop for HidHandle {
    /// 兜底释放：Python 侧若在异常路径上漏了 `close()`，句柄也不会漏到进程结束。
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// 原生 HID 后端：`hexwand/hardware/backends.py` 薄封装背后的唯一实现
/// （原先的 `HIDBackend` 抽象基类与 ctypes/pyhidapi 兜底都已按"不留兼容层"删除）。
#[pyclass]
pub struct HidBackend {}

impl Default for HidBackend {
    fn default() -> Self {
        Self::new()
    }
}

/// 句柄是否无效：NULL 或 INVALID_HANDLE_VALUE（-1）都算。
///
/// 注意 windows-sys 0.59 起 `HANDLE` 是 `*mut c_void`（不是 isize），
/// 所以这里用 `is_null()` + 指针转 isize 比较。
fn bad_handle(h: HANDLE) -> bool {
    h.is_null() || h as isize == INVALID_HANDLE_VALUE as isize
}

/// 设备信息集句柄是否无效。`HDEVINFO` 在 windows-sys 里是 **isize**
/// （和 HANDLE 那种指针类型不一样），失败值同样是 INVALID_HANDLE_VALUE = -1。
fn bad_devinfo(h: isize) -> bool {
    h == 0 || h == -1
}

fn last_err() -> u32 {
    unsafe { GetLastError() }
}

/// 取该句柄的 HIDP_CAPS；取不到返回 None（preparsed data 一定释放）。
fn caps_of(h: HANDLE) -> Option<HIDP_CAPS> {
    // PHIDP_PREPARSED_DATA 在 windows-sys 里就是 isize
    let mut pp: isize = 0;
    let ok = unsafe { HidD_GetPreparsedData(h, &mut pp) };
    if ok == 0 {
        return None;
    }
    let mut caps: HIDP_CAPS = unsafe { std::mem::zeroed() };
    let status = unsafe { HidP_GetCaps(pp, &mut caps) };
    unsafe { HidD_FreePreparsedData(pp) };
    // 成功值是 HIDP_STATUS_SUCCESS(0x00110000)，不是 0！
    if status != HIDP_STATUS_SUCCESS {
        return None;
    }
    Some(caps)
}

// 注：原来这里还有一个 `feature_len_of(h: HANDLE)`。能力值现在统一走
// `HidHandle::feature_len()`（带缓存），不再需要按裸句柄查的这一版，已删除。

/// 把 UTF-16（NUL 结尾）转成 String。
fn utf16_to_string(buf: &[u16]) -> String {
    let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    String::from_utf16_lossy(&buf[..end])
}

/// 取句柄的 Feature 报表长度；取不到就明确报错（**不再猜**）。
///
/// 曾经这里在"读不到 caps"时按载荷长度推断（小帧按整帧、雷蛇那档试 91 再试
/// 90）。那是 ctypes 时代防 `HidP_GetCaps` 失败的兜底：caps 判定修好并缓存
/// 之后它从未被触发，而猜错长度会让协议层解出一段**看似合规的错位报文**
/// ——比直接报错难查得多（2026-09-17 按"不留 fallback"删除）。
fn require_feature_len(handle: &HidHandle) -> PyResult<usize> {
    match handle.feature_len() {
        Some(f) if f > 0 => Ok(f as usize),
        _ => Err(PyRuntimeError::new_err(
            "读不到这个 HID 接口的 Feature 报表长度（HidP_GetCaps 失败，或此\
             接口根本没有 Feature 报表）——无法收发命令。",
        )),
    }
}

#[pymethods]
impl HidBackend {
    #[new]
    fn new() -> Self {
        Self {}
    }

    fn name(&self) -> &'static str {
        "native(rust hid)"
    }

    /// 枚举 HID 接口，返回 [(path, vid, pid, usage_page, name?)]。
    ///
    /// `vid` 给定时只返回该品牌的设备（None = 不过滤）。
    #[pyo3(signature = (vid = None))]
    fn enumerate(&self, vid: Option<u16>) -> Vec<(String, u16, u16, u16, Option<String>)> {
        let mut out: Vec<(String, u16, u16, u16, Option<String>)> = Vec::new();
        let hdev = unsafe {
            SetupDiGetClassDevsW(
                &GUID_DEVINTERFACE_HID,
                std::ptr::null(),
                std::ptr::null_mut(), // hwndParent（HWND 在 0.59 是 *mut c_void）
                DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
            )
        };
        if bad_devinfo(hdev) {
            return out;
        }
        let mut idx: u32 = 0;
        loop {
            let mut did: SP_DEVICE_INTERFACE_DATA = unsafe { std::mem::zeroed() };
            did.cbSize = std::mem::size_of::<SP_DEVICE_INTERFACE_DATA>() as u32;
            let ok = unsafe {
                SetupDiEnumDeviceInterfaces(
                    hdev,
                    std::ptr::null(),
                    &GUID_DEVINTERFACE_HID,
                    idx,
                    &mut did,
                )
            };
            if ok == 0 {
                break;
            }
            idx += 1;

            // 先问所需长度，再按真实长度分配（设备路径可能很长）
            let mut need: u32 = 0;
            unsafe {
                SetupDiGetDeviceInterfaceDetailW(
                    hdev,
                    &did,
                    std::ptr::null_mut(),
                    0,
                    &mut need,
                    std::ptr::null_mut(),
                )
            };
            if need == 0 {
                continue;
            }
            if need < 4 {
                continue;    // 连 cbSize 都装不下，后面切片会越界
            }
            let mut buf = vec![0u8; need as usize];
            // SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize：x64 上是 8，**x86 上是 6**
            // （MSDN 明文）。写死 8 在 32 位下会让每次枚举都失败、列表恒空
            // （2026-09-18 审查发现的埋雷——当前只出 x64 包，所以从未爆）。
            let cbsize: u32 = if cfg!(target_pointer_width = "64") { 8 } else { 6 };
            buf[0..4].copy_from_slice(&cbsize.to_ne_bytes());
            let ok = unsafe {
                SetupDiGetDeviceInterfaceDetailW(
                    hdev,
                    &did,
                    buf.as_mut_ptr() as *mut SP_DEVICE_INTERFACE_DETAIL_DATA_W,
                    need,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            };
            if ok == 0 {
                continue;
            }
            // DevicePath 紧跟在 cbSize(4 字节) 之后，且是结构体尾部起始的 UTF-16
            let path_words: Vec<u16> =
                buf[4..].chunks_exact(2).map(|c| u16::from_ne_bytes([c[0], c[1]])).collect();
            let path = utf16_to_string(&path_words);
            if path.is_empty() {
                continue;
            }

            let handle = unsafe {
                CreateFileW(
                    to_wide(&path).as_ptr(),
                    0, // access=0：查询权限（见模块头注释第 3 条）
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    0,
                    std::ptr::null_mut(), // hTemplateFile
                )
            };
            if bad_handle(handle) {
                continue;
            }
            let mut attrs: HIDD_ATTRIBUTES = unsafe { std::mem::zeroed() };
            attrs.Size = std::mem::size_of::<HIDD_ATTRIBUTES>() as u32;
            let got_attr = unsafe { HidD_GetAttributes(handle, &mut attrs) } != 0;
            let name = product_string(handle);
            let usage_page = caps_of(handle).map(|c| c.UsagePage).unwrap_or(0);
            unsafe { CloseHandle(handle) };

            if !got_attr {
                continue;
            }
            if let Some(want) = vid {
                if attrs.VendorID != want {
                    continue;
                }
            }
            out.push((
                path,
                attrs.VendorID,
                attrs.ProductID,
                usage_page,
                name,
            ));
        }
        unsafe { SetupDiDestroyDeviceInfoList(hdev) };
        out
    }

    /// 打开设备（access=0 + 共享读写，沿用原 ctypes 实现的行为）。
    fn open(&self, path: &str) -> PyResult<HidHandle> {
        let handle = unsafe {
            CreateFileW(
                to_wide(path).as_ptr(),
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                std::ptr::null(),
                OPEN_EXISTING,
                0,
                std::ptr::null_mut(), // hTemplateFile
            )
        };
        if bad_handle(handle) {
            return Err(PyOSError::new_err(format!(
                "无法打开设备（可能被官方鼠标软件独占，或需要管理员权限）LastError={}",
                last_err()
            )));
        }
        Ok(HidHandle::new(handle))
    }

    /// Feature Report 长度（含 report id）；读不到返回 None。
    fn feature_len(&self, handle: &HidHandle) -> Option<u16> {
        handle.feature_len()
    }

    /// 读一帧 Feature Report，返回 `length` 字节载荷（不含 report id）。
    ///
    /// 缓冲区长度必须**正好等于**设备的 FeatureReportByteLength（见模块头注释
    /// 第 2 条），所以尺寸只能问设备，不能拿调用方要的载荷长度去凑。
    #[pyo3(signature = (handle, length, report_id = 2))]
    fn get_feature(&self, handle: &HidHandle, length: usize, report_id: u8) -> PyResult<Vec<u8>> {
        let h = handle.raw();
        let flen = require_feature_len(handle)?;

        if flen < REPORT_LEN {
            // 小帧设备（HID++ 等）：缓冲区按设备长度，返回**整帧**
            // （HID++ 报文首字节就是 report id，协议层按 resp[0]=report id、
            // resp[2]=feature index 解码，去掉首字节会让字段整体左移）。
            let want = if length == 0 { flen } else { length.min(flen) };
            let mut buf = vec![0u8; flen];
            buf[0] = report_id;
            let ok = unsafe { HidD_GetFeature(h, buf.as_mut_ptr() as *mut c_void, flen as u32) };
            if ok == 0 {
                return Err(PyRuntimeError::new_err(format!(
                    "HidD_GetFeature 失败（缓冲区 {flen} 字节，需要与设备的 \
                     FeatureReportByteLength 一致，LastError={}）",
                    last_err()
                )));
            }
            return Ok(buf[..want].to_vec());
        }

        if flen == REPORT_LEN {
            // unnumbered
            let mut buf = vec![0u8; REPORT_LEN];
            let ok = unsafe {
                HidD_GetFeature(h, buf.as_mut_ptr() as *mut c_void, REPORT_LEN as u32)
            };
            if ok == 0 {
                return Err(PyRuntimeError::new_err(format!(
                    "HidD_GetFeature 失败（LastError={}）",
                    last_err()
                )));
            }
            return Ok(buf);
        }

        // numbered：先试给定 report_id，再试 0
        for rid in [report_id, 0u8] {
            let mut buf = vec![0u8; flen];
            buf[0] = rid;
            let ok = unsafe { HidD_GetFeature(h, buf.as_mut_ptr() as *mut c_void, flen as u32) };
            if ok != 0 {
                let end = (1 + REPORT_LEN).min(flen);
                return Ok(buf[1..end].to_vec());
            }
        }
        Err(PyRuntimeError::new_err(format!(
            "HidD_GetFeature 失败（LastError={}）",
            last_err()
        )))
    }

    /// 发一帧 Feature Report。`data` 是载荷（**含** report id 时由调用方保证
    /// 放在首字节，如雷蛇帧由 build_report 组好、HID++ 帧由 build_hidpp_frame 组好）。
    #[pyo3(signature = (handle, data, report_id = 2))]
    fn set_feature(&self, handle: &HidHandle, data: &[u8], report_id: u8) -> PyResult<()> {
        let h = handle.raw();
        let flen = require_feature_len(handle)?;

        if flen < REPORT_LEN {
            // 小帧设备：载荷原样放在缓冲区开头（首字节就是 report id），右侧补 0
            if data.len() > flen {
                return Err(PyRuntimeError::new_err(format!(
                    "载荷 {} 字节超过设备 Feature 长度 {flen}，拒绝发送",
                    data.len()
                )));
            }
            let mut buf = vec![0u8; flen];
            buf[..data.len()].copy_from_slice(data);
            let ok = unsafe {
                HidD_SetFeature(h, buf.as_ptr() as *const c_void, flen as u32)
            };
            if ok == 0 {
                return Err(PyRuntimeError::new_err(format!(
                    "HidD_SetFeature 失败（长度 {flen}，LastError={}）",
                    last_err()
                )));
            }
            return Ok(());
        }

        // 雷蛇及更大帧的设备：按设备自己报的长度发一帧
        let buf = build_set_buf(data, report_id, flen);
        let ok = unsafe { HidD_SetFeature(h, buf.as_ptr() as *const c_void, flen as u32) };
        if ok == 0 {
            return Err(PyRuntimeError::new_err(format!(
                "HidD_SetFeature 失败（LastError={}）",
                last_err()
            )));
        }
        Ok(())
    }

    fn close(&self, handle: &HidHandle) {
        // 幂等：`shutdown()` 关闭后把句柄置 0，重复调用与 `Drop` 都不会二次关闭
        handle.shutdown();
    }
}

/// 当前按下的所有虚拟键（1..=0xFF）——`ui/remap.py` 的取键轮询用。
///
/// 一次跨语言调用里跑完 255 次 `GetAsyncKeyState`：Python 侧原本每 tick 要发
/// 255 次 ctypes 调用（键位页取键间隔 20ms）。
#[pyfunction]
pub fn pressed_vks() -> Vec<u8> {
    let mut out = Vec::new();
    for vk in 1u16..=0xFF {
        let down = unsafe { GetAsyncKeyState(vk as i32) } as u16 & 0x8000 != 0;
        if down {
            out.push(vk as u8);
        }
    }
    out
}

/// 组写缓冲：flen == REPORT_LEN 时不编号（雷蛇 90 帧自带格式），
/// 否则前置 report id 再补零到 flen。
fn build_set_buf(data: &[u8], report_id: u8, flen: usize) -> Vec<u8> {
    if flen == REPORT_LEN {
        let mut buf = vec![0u8; REPORT_LEN];
        let n = data.len().min(REPORT_LEN);
        buf[..n].copy_from_slice(&data[..n]);
        return buf;
    }
    let mut buf = vec![0u8; flen];
    buf[0] = report_id;
    let n = data.len().min(REPORT_LEN);
    let end = (1 + n).min(flen);
    buf[1..end].copy_from_slice(&data[..end - 1]);
    buf
}

/// UTF-16（带 NUL 结尾）转换。
fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// 读产品名字符串（失败返回 None）。
fn product_string(h: HANDLE) -> Option<String> {
    let mut buf = vec![0u16; 256];
    let ok = unsafe {
        HidD_GetProductString(
            h,
            buf.as_mut_ptr() as *mut c_void,
            (buf.len() * 2) as u32,
        )
    };
    if ok == 0 {
        return None;
    }
    let s = utf16_to_string(&buf);
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<HidBackend>()?;
    module.add_class::<HidHandle>()?;
    module.add_function(wrap_pyfunction!(pressed_vks, module)?)?;
    Ok(())
}
