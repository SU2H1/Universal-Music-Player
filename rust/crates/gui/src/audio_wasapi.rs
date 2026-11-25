//! Bit-perfect playback on Windows (2.1): a small WASAPI exclusive-mode output
//! beside cpal, whose WASAPI backend plays in shared mode only.
//!
//! `probe()` asks the device which stereo formats and rates it takes in
//! exclusive mode (`IAudioClient::IsFormatSupported`), and `open()` starts an
//! event-driven exclusive stream in one of them on a thread of its own: every
//! period it takes the frames from the engine with the gain at exactly 1 and
//! writes them as the device's integers (`audio::render_ints`: the file's own
//! integers, or with the equalizer on its 64-bit output with TPDF dither, 2.3)
//! or as floats (`audio::render`).  Another program
//! holding the device, or Windows not allowing exclusive use of it
//! ("Allow applications to take exclusive control of this device"), is
//! reported so that the engine plays the song through the normal path.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread::JoinHandle;

use realhires::bitperfect::{self, Format, Mode};
use windows::core::HRESULT;
use windows::Win32::Devices::Properties;
use windows::Win32::Foundation::{CloseHandle, HANDLE, PROPERTYKEY, RPC_E_CHANGED_MODE, WAIT_OBJECT_0};
use windows::Win32::Media::Audio::{
    eConsole, eRender, IAudioClient, IAudioRenderClient, IMMDevice, IMMDeviceEnumerator, MMDeviceEnumerator,
    AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED, AUDCLNT_E_DEVICE_IN_USE, AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED,
    AUDCLNT_SHAREMODE_EXCLUSIVE, AUDCLNT_STREAMFLAGS_EVENTCALLBACK, DEVICE_STATE_ACTIVE, WAVEFORMATEX,
    WAVEFORMATEXTENSIBLE, WAVEFORMATEXTENSIBLE_0,
};
use windows::Win32::Media::KernelStreaming::{KSDATAFORMAT_SUBTYPE_PCM, WAVE_FORMAT_EXTENSIBLE};
use windows::Win32::Media::Multimedia::KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
use windows::Win32::System::Com::StructuredStorage::PropVariantClear;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED, STGM_READ,
};
use windows::Win32::System::Threading::{CreateEventW, WaitForSingleObject};
use windows::Win32::System::Variant::VT_LPWSTR;

use crate::audio::{render, render_ints, Event, RenderState, Shared};

/// Why the device could not be had for this app alone.
#[derive(Debug)]
pub struct WasapiError {
    pub busy: bool,
    /// the device's name, for the Playback info
    pub device: String,
    pub message: String,
}

impl WasapiError {
    /// The reason the Playback info gives.
    pub fn reason(&self) -> String {
        if self.busy {
            bitperfect::BUSY.into()
        } else {
            bitperfect::failure_reason(&self.message)
        }
    }

    fn from_win(e: &windows::core::Error, device: &str) -> WasapiError {
        let code = e.code();
        let message = if code == AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED {
            // English, as the core's sentence around it (shown with `tr_text()`)
            realhires::n_!("Windows does not allow programs to take exclusive control of it").to_string()
        } else {
            format!("WASAPI: {}", e.message().trim())
        };
        WasapiError { busy: code == AUDCLNT_E_DEVICE_IN_USE, device: device.into(), message }
    }
}

/// COM for the thread that calls it, undone when dropped.
struct Com(bool);

impl Com {
    fn init() -> Com {
        let hr = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
        Com(hr.is_ok() && hr != RPC_E_CHANGED_MODE)
    }
}

impl Drop for Com {
    fn drop(&mut self) {
        if self.0 {
            unsafe { CoUninitialize() };
        }
    }
}

unsafe fn friendly_name(device: &IMMDevice) -> Option<String> {
    let store = device.OpenPropertyStore(STGM_READ).ok()?;
    let mut value = store.GetValue(&Properties::DEVPKEY_Device_FriendlyName as *const _ as *const PROPERTYKEY).ok()?;
    let inner = &value.Anonymous.Anonymous;
    let mut out = None;
    if inner.vt == VT_LPWSTR {
        let p = *(&inner.Anonymous as *const _ as *const *const u16);
        if !p.is_null() {
            let mut len = 0usize;
            while len < 32768 && *p.add(len) != 0 {
                len += 1;
            }
            out = Some(String::from_utf16_lossy(std::slice::from_raw_parts(p, len)));
        }
    }
    let _ = PropVariantClear(&mut value);
    out
}

/// The output device named `name` (as cpal and the Settings page name it), else the default one.
unsafe fn find_device(name: &str) -> windows::core::Result<(IMMDevice, String)> {
    let enumerator: IMMDeviceEnumerator = CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)?;
    if !name.is_empty() {
        let all = enumerator.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)?;
        for i in 0..all.GetCount()? {
            let device = all.Item(i)?;
            if friendly_name(&device).as_deref() == Some(name) {
                return Ok((device, name.to_string()));
            }
        }
    }
    let device = enumerator.GetDefaultAudioEndpoint(eRender, eConsole)?;
    let label = friendly_name(&device).unwrap_or_else(|| "System default".into());
    Ok((device, label))
}

/// The exclusive-mode formats tried for each engine format: (container bits,
/// valid bits, float).  An I32 stream may be a 32-bit word with 24 valid bits,
/// which takes the engine's 32-bit integers as they are (left-justified).
fn variants(format: Format) -> &'static [(u16, u16, bool)] {
    match format {
        Format::I16 => &[(16, 16, false)],
        Format::I24 => &[(24, 24, false)],
        Format::I32 => &[(32, 32, false), (32, 24, false)],
        Format::F32 => &[(32, 32, true)],
    }
}

fn wave_format(rate: u32, channels: u16, (bits, valid, float): (u16, u16, bool)) -> WAVEFORMATEXTENSIBLE {
    let block = channels * bits / 8;
    WAVEFORMATEXTENSIBLE {
        Format: WAVEFORMATEX {
            wFormatTag: WAVE_FORMAT_EXTENSIBLE as u16,
            nChannels: channels,
            nSamplesPerSec: rate,
            nAvgBytesPerSec: rate * block as u32,
            nBlockAlign: block,
            wBitsPerSample: bits,
            cbSize: (std::mem::size_of::<WAVEFORMATEXTENSIBLE>() - std::mem::size_of::<WAVEFORMATEX>()) as u16,
        },
        Samples: WAVEFORMATEXTENSIBLE_0 { wValidBitsPerSample: valid },
        // front left and right for stereo; other counts go to the outputs in order
        dwChannelMask: if channels == 2 { 0x3 } else { 0 },
        SubFormat: if float { KSDATAFORMAT_SUBTYPE_IEEE_FLOAT } else { KSDATAFORMAT_SUBTYPE_PCM },
    }
}

unsafe fn supported(client: &IAudioClient, wf: &WAVEFORMATEXTENSIBLE) -> HRESULT {
    client.IsFormatSupported(AUDCLNT_SHAREMODE_EXCLUSIVE, wf as *const _ as *const WAVEFORMATEX, None)
}

/// The device's name and the stereo formats and rates it takes in exclusive mode.
pub fn probe(name: &str) -> Result<(String, Vec<Mode>), WasapiError> {
    let name = name.to_string();
    // COM in a thread of its own, so the window's thread keeps its apartment
    std::thread::spawn(move || {
        let _com = Com::init();
        unsafe {
            let (device, label) = find_device(&name).map_err(|e| WasapiError::from_win(&e, &name))?;
            let client: IAudioClient = device.Activate(CLSCTX_ALL, None).map_err(|e| WasapiError::from_win(&e, &label))?;
            let mut modes = vec![];
            for format in Format::ALL {
                for &rate in bitperfect::STANDARD_RATES.iter() {
                    for &v in variants(format) {
                        let hr = supported(&client, &wave_format(rate, 2, v));
                        if hr == AUDCLNT_E_DEVICE_IN_USE {
                            return Err(WasapiError { busy: true, device: label, message: String::new() });
                        }
                        if hr == AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED {
                            return Err(WasapiError::from_win(&hr.into(), &label));
                        }
                        if hr.0 == 0 {
                            modes.push(Mode::new(format, rate, rate, 2));
                            break;
                        }
                    }
                }
            }
            if modes.is_empty() {
                return Err(WasapiError {
                    busy: false,
                    device: label,
                    message: realhires::n_!("it takes no stereo format in exclusive mode").into(),
                });
            }
            Ok((label, modes))
        }
    })
    .join()
    .unwrap_or_else(|_| Err(WasapiError { busy: false, device: String::new(), message: "WASAPI failed".into() }))
}

/// An exclusive-mode stream; dropping it stops it and gives the device back.
pub struct Exclusive {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Drop for Exclusive {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

struct Stream {
    client: IAudioClient,
    render: IAudioRenderClient,
    event: HANDLE,
    frames: u32,
    variant: (u16, u16, bool),
}

/// Initialize an event-driven exclusive stream (with the period aligned to the
/// device's buffer when it asks for that).
unsafe fn start_stream(device: &IMMDevice, label: &str, rate: u32, format: Format, channels: u16) -> Result<Stream, WasapiError> {
    let err = |e: &windows::core::Error| WasapiError::from_win(e, label);
    let mut last: Option<windows::core::Error> = None;
    for &v in variants(format) {
        let wf = wave_format(rate, channels, v);
        let mut client: IAudioClient = device.Activate(CLSCTX_ALL, None).map_err(|e| err(&e))?;
        let mut period = 0i64;
        client.GetDevicePeriod(Some(&mut period), None).map_err(|e| err(&e))?;
        let mut result =
            client.Initialize(AUDCLNT_SHAREMODE_EXCLUSIVE, AUDCLNT_STREAMFLAGS_EVENTCALLBACK, period, period, &wf.Format, None);
        if let Err(e) = &result {
            if e.code() == AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED {
                // the period must be a whole number of the device's frames: ask again with that
                let frames = client.GetBufferSize().map_err(|e| err(&e))?;
                period = ((10_000_000f64 * frames as f64 / rate as f64) + 0.5) as i64;
                client = device.Activate(CLSCTX_ALL, None).map_err(|e| err(&e))?;
                result = client.Initialize(
                    AUDCLNT_SHAREMODE_EXCLUSIVE,
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                    period,
                    period,
                    &wf.Format,
                    None,
                );
            }
        }
        match result {
            Ok(()) => {
                let event = CreateEventW(None, false, false, None).map_err(|e| err(&e))?;
                client.SetEventHandle(event).map_err(|e| err(&e))?;
                let frames = client.GetBufferSize().map_err(|e| err(&e))?;
                let render: IAudioRenderClient = client.GetService().map_err(|e| err(&e))?;
                return Ok(Stream { client, render, event, frames, variant: v });
            }
            Err(e) => {
                if e.code() == AUDCLNT_E_DEVICE_IN_USE || e.code() == AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED {
                    return Err(err(&e));
                }
                last = Some(e);
            }
        }
    }
    Err(match last {
        Some(e) => err(&e),
        None => WasapiError { busy: false, device: label.into(), message: "no format to try".into() },
    })
}

/// Write `samples` (interleaved floats) into the device's buffer (a float format).
unsafe fn write_floats(dst: *mut u8, samples: &[f32]) {
    let out = std::slice::from_raw_parts_mut(dst, samples.len() * 4);
    for (o, x) in out.chunks_exact_mut(4).zip(samples) {
        o.copy_from_slice(&x.to_le_bytes());
    }
}

/// Write `samples` (interleaved integers of `format`, from `render_ints()`) into the
/// device's buffer: 16-bit, 24-bit in 3 bytes, or 32-bit words (whose 24 valid bits are
/// the top ones, as the engine's 32-bit integers are left-justified).
unsafe fn write_ints(dst: *mut u8, samples: &[i32], format: Format, variant: (u16, u16, bool)) {
    let bytes = variant.0 as usize / 8;
    let out = std::slice::from_raw_parts_mut(dst, samples.len() * bytes);
    for (o, x) in out.chunks_exact_mut(bytes).zip(samples) {
        match format {
            Format::I16 => o.copy_from_slice(&(*x as i16).to_le_bytes()),
            Format::I24 => o.copy_from_slice(&x.to_le_bytes()[..3]),
            _ => o.copy_from_slice(&x.to_le_bytes()),
        }
    }
}

/// Start an exclusive stream at `rate` in `format` with `channels` channels
/// (more than two: the others stay silent).  Err when the device cannot be had.
pub fn open(name: &str, rate: u32, format: Format, channels: u32, shared: Arc<Shared>) -> Result<Exclusive, WasapiError> {
    let stop = Arc::new(AtomicBool::new(false));
    let (tx, rx) = mpsc::channel::<Result<(), WasapiError>>();
    let name = name.to_string();
    let flag = stop.clone();
    let channels = channels.max(2) as u16;
    let thread = std::thread::Builder::new()
        .name("player-wasapi-exclusive".into())
        .spawn(move || {
            let _com = Com::init();
            unsafe {
                let started = find_device(&name)
                    .map_err(|e| WasapiError::from_win(&e, &name))
                    .and_then(|(device, label)| start_stream(&device, &label, rate, format, channels));
                let stream = match started {
                    Ok(s) => s,
                    Err(e) => {
                        let _ = tx.send(Err(e));
                        return;
                    }
                };
                let mut st = RenderState::new(&shared, rate);
                let mut tmp = vec![0f32; stream.frames as usize * channels as usize];
                let mut ints = vec![0i32; stream.frames as usize * channels as usize];
                // the first buffer is filled before the stream starts, as WASAPI asks
                let fill = |stream: &Stream,
                            st: &mut RenderState,
                            tmp: &mut Vec<f32>,
                            ints: &mut Vec<i32>|
                 -> windows::core::Result<()> {
                    let dst = stream.render.GetBuffer(stream.frames)?;
                    if format == Format::F32 {
                        render(&shared, st, tmp, channels as usize);
                        write_floats(dst, tmp);
                    } else {
                        render_ints(&shared, st, ints, channels as usize, format);
                        write_ints(dst, ints, format, stream.variant);
                    }
                    stream.render.ReleaseBuffer(stream.frames, 0)
                };
                let ok = fill(&stream, &mut st, &mut tmp, &mut ints).and_then(|_| stream.client.Start());
                if let Err(e) = ok {
                    let _ = tx.send(Err(WasapiError::from_win(&e, &name)));
                    let _ = CloseHandle(stream.event);
                    return;
                }
                let _ = tx.send(Ok(()));
                while !flag.load(Ordering::Relaxed) {
                    if WaitForSingleObject(stream.event, 500) != WAIT_OBJECT_0 {
                        continue;
                    }
                    if let Err(e) = fill(&stream, &mut st, &mut tmp, &mut ints) {
                        shared.events.lock().unwrap().push(Event::OutputError(realhires::tr!(
                            "The audio output failed: {e}.",
                            e = e.message().trim().trim_end_matches('.')
                        )));
                        break;
                    }
                }
                let _ = stream.client.Stop();
                let _ = CloseHandle(stream.event);
            }
        })
        .map_err(|e| WasapiError { busy: false, device: String::new(), message: e.to_string() })?;
    match rx.recv() {
        Ok(Ok(())) => Ok(Exclusive { stop, thread: Some(thread) }),
        Ok(Err(e)) => {
            let _ = thread.join();
            Err(e)
        }
        Err(_) => {
            let _ = thread.join();
            Err(WasapiError { busy: false, device: String::new(), message: "the WASAPI thread ended".into() })
        }
    }
}
