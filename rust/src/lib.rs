//! HexWand 的 Rust 帧层与底层（PyO3 试水切片，2026-09-17）。
//!
//! 目前移植了雷蛇 90 字节帧与罗技 HID++ 帧的**组帧 + CRC**——纯函数，行为由
//! `app.py --selftest` 里的独立参考实现（oracle）逐字节对拍兜底：
//!   · `frame/razer.py` / `frame/hidpp.py` 现在只是转发到这里的薄封装，
//!     用它们当对拍对象等于自己跟自己比（曾经这么写过，是假通过）；
//!   · 参考实现写在 selftest 里，是**验证工具**，不算重复实现。
//!
//! 注意：crc8 的 XOR 范围是 rep[2..88]（含 87、不含 88），与参考实现的
//! `range(2, 88)` 严格一致；输入短于 88 字节时参考实现会 IndexError，
//! 这里选择把越界位当 0 跳过（只影响非正常调用，正常路径恒为 90 字节帧）。

use pyo3::prelude::*;

mod hid;
mod remap;

const REPORT_LEN: usize = 90;

/// 原生扩展的 API 版本。**改/加任何对外符号、或改了扩展内部行为就 +1**，
/// 并同步 `app.py --selftest` 里的断言。
///
/// 为什么要有这个：扩展装在两处（项目 .venv 给源码跑、打包 venv 给 PyInstaller），
/// 项目根还曾经躺着一份手搓的旧 .pyd 会**遮蔽**它们。2026-09-17 就出过一次
/// "exe 里带的是几小时前的旧扩展、Rust 改动没生效"却一路绿灯的事故 ——
/// 有了版本戳，陈旧产物会在 selftest 里当场报错，而不是静默跑旧代码。
///
/// 3 = 改键补 release-all（stop/stall/队列满时松开残留按键）；`HidHandle` 加
///     `Drop` 且 `close()` 幂等；caps 缓存进句柄（每次收发少 2 次 caps 读取）。
/// 4 = 删掉"读不到 caps 就手工猜报文长度"的兜底、caps 缓存只存 feature_len
///     （行为变化：猜不出长度时明确报错，不再试发）。
/// 5 = 改键行为修复：start() 复位 enabled；start/stop 等待期放开 GIL；
///     队列满丢"抬起"由注入线程(up_pending)+rescue(计数变化)代偿 release_held；
///     set_map 拒绝越界键码；钩子线程退出清 TID/alive。HID 枚举 cbSize
///     按指针宽度（x86 埋雷）、detail 长度下限防护。
const API_VERSION: u32 = 5;

/// CRC = XOR(report[2..88])，写入 report[88]。
#[pyfunction]
fn crc8(report: &[u8]) -> u8 {
    let mut c: u8 = 0;
    for i in 2..88 {
        if i < report.len() {
            c ^= report[i];        // XOR 不会溢出，不需要 wrapping 系列
        }
    }
    c
}

/// 构造 90 字节命令报文。data 最多 80 字节（rep[8..88]）。
#[pyfunction]
#[pyo3(signature = (cmd_class, cmd_id, data = None, tx_id = 0x1F))]
fn build_report(cmd_class: u8, cmd_id: u8, data: Option<&[u8]>, tx_id: u8) -> Vec<u8> {
    let data = data.unwrap_or(&[]);
    let take = data.len().min(80);
    let mut rep = vec![0u8; REPORT_LEN];
    rep[1] = tx_id;
    rep[5] = (data.len() & 0xFF) as u8; // Python 版记录原始长度（&0xFF），不是 min(80)
    rep[6] = cmd_class;
    rep[7] = cmd_id;
    rep[8..8 + take].copy_from_slice(&data[..take]);
    rep[88] = crc8(&rep);
    rep
}

/// HID++（罗技）帧：短报文 7 字节 / 长报文 20 字节，**首字节就是 report id**
/// （0x10 短 / 0x11 长）。参数 ≤3 字节发短报文，否则发长报文 —— 与 Solaar
/// `base.py` 的判据（载荷 >5 字节发长报文）等价，见 `hexwand/hardware/frame/hidpp.py`。
///
/// 参数最多 16 字节（长报文的参数区），超出部分被截断（与 Python 版一致：
/// 它按 `rep[4..]` 的长度逐个填，写不下的自然丢弃）。
#[pyfunction]
#[pyo3(signature = (device_index, feature_index, function, params = None, sw_id = 0x08))]
fn build_hidpp_frame(
    device_index: u8,
    feature_index: u8,
    function: u8,
    params: Option<&[u8]>,
    sw_id: u8,
) -> Vec<u8> {
    let params = params.unwrap_or(&[]);
    let mut rep = if params.len() <= 3 {
        let mut v = vec![0u8; 7];
        v[0] = 0x10; // HIDPP_REPORT_SHORT
        v
    } else {
        let mut v = vec![0u8; 20];
        v[0] = 0x11; // HIDPP_REPORT_LONG
        v
    };
    rep[1] = device_index;
    rep[2] = feature_index;
    // function 已左移 4 位，低 4 位留给 sw_id（写成 function << 4 会溢出丢掉功能号）
    rep[3] = (function & 0xF0) | (sw_id & 0x0F);
    let take = params.len().min(rep.len() - 4);
    rep[4..4 + take].copy_from_slice(&params[..take]);
    rep
}

/// CRC-CCITT（初值 0xFFFF，多项式 0x1021，高位在前/非反射）——罗技板载配置的校验。
#[pyfunction]
fn crc_ccitt(data: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    for &b in data {
        crc ^= (b as u16) << 8;
        for _ in 0..8 {
            crc = if crc & 0x8000 != 0 {
                (crc << 1) ^ 0x1021
            } else {
                crc << 1
            };
        }
    }
    crc
}

#[pymodule]
fn hexwand_rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("API_VERSION", API_VERSION)?;
    m.add_function(wrap_pyfunction!(crc8, m)?)?;
    m.add_function(wrap_pyfunction!(build_report, m)?)?;
    m.add_function(wrap_pyfunction!(build_hidpp_frame, m)?)?;
    m.add_function(wrap_pyfunction!(crc_ccitt, m)?)?;
    hid::register(m)?;
    remap::register(m)?;
    Ok(())
}
