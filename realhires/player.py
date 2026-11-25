"""The playback engine of the 2.0 player (Python version).

FFmpeg decodes every song to 32-bit float stereo at the output's sample rate
(``ffmpeg -nostdin -v error -ss <start> -i <file> -map 0:a:0 -vn [-af <filters>]
-f f32le -ac 2 -ar <rate> pipe:1``), a decoder thread keeps a short buffer
(about a third of a second) filled, and the sound device pulls frames from it
through miniaudio (https://github.com/irmen/pyminiaudio), which plays them.

* The device opens at the song's sample rate when it supports that rate
  ("Resampling: None"); otherwise at the device's own rate, and FFmpeg
  resamples with soxr (or its default resampler when the build has no soxr).
* Gapless playback: about 10 seconds before a song ends the engine starts
  FFmpeg for the next song (``set_next()``), and at the end of the song it
  feeds the next one into the same buffer, so not a single sample of silence
  lies between them.  When the next song needs another output rate, the device
  is reopened at that rate once the song has been heard to the end.
* The equalizer runs in FFmpeg's filter chain (``eq.ffmpeg_filter()``, in
  double precision); changing it restarts FFmpeg at the current position.
* The volume (``gain = volume ** 2``, 0 when muted) is applied to the samples
  as they are handed to the device, after the equalizer.
* The position counts the frames handed to the device since the song started
  (not the frames decoded), so the seek bar and the lyrics follow what is heard.
* Bit-perfect playback (``set_bit_perfect()``, 2.1): FFmpeg decodes without
  ``-ar`` and without filters (only ``pan`` copies a mono song to both
  channels), the gain is exactly 1, and the device is opened at the song's rate
  in the format ``bitperfect.plan()`` picks from what it offers, through WASAPI
  exclusive mode (Windows), the ALSA hardware device (Linux) or with its
  nominal rate switched to the song's (macOS).  The floats are turned into the
  device's integers exactly (``device_samples()``).  When the device cannot be
  had, the song plays through the normal path, and
  ``output_info()["bit_perfect"]`` says why.  Since 2.3 the equalizer is not
  bypassed: when it is on, FFmpeg runs it in double precision at the song's
  rate, and the samples it changed get TPDF dither of ±1 LSB of the device's
  integer format in ``_pull()`` (``_tpdf()``) before they are rounded and
  clamped; with the equalizer off the output stays bit-exact.

Nothing here touches Tk: the desktop app polls ``poll()`` for events
("advanced", "finished", "error") and ``position()`` for the seek bar.  The
output is pluggable: ``MiniaudioOutput`` plays through a sound device (with
``REALHIRES_AUDIO_BACKEND=null`` through miniaudio's NULL backend, which
consumes audio in real time without sound hardware), and ``ManualOutput``
lets a test pull the frames by hand.
"""

from __future__ import annotations

import atexit
import collections
import math
import os
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import bitperfect as bp
from . import eq as eq_mod
from .probe import _no_window, tool

BACKEND_ENV = "REALHIRES_AUDIO_BACKEND"   # "null": miniaudio's NULL backend (tests, no sound hardware)
BUFFER_SECONDS = 0.35                     # decoded audio kept ahead of the device
PREFETCH_SECONDS = 10.0                   # the next song's FFmpeg starts this long before the end
CHUNK_FRAMES = 2048                       # frames read from FFmpeg at a time
PERIOD_MS = 40                            # the device asks for this much audio per callback
EQ_RESTART_DELAY_S = 0.08                 # EQ changes within this time restart FFmpeg once
CHANNELS = 2
FRAME_BYTES = 4 * CHANNELS                # float32 stereo
SAMPLE_FORMAT_LABEL = "32-bit float"


class PlayerError(Exception):
    """Something the player cannot do (the message is a full sentence for the user)."""


# ------------------------------------------------------------- FFmpeg facts ---
_FACTS: Dict[str, object] = {}
_FACTS_LOCK = threading.Lock()


def has_soxr() -> bool:
    """Whether this FFmpeg can resample with soxr (checked once with a tiny conversion)."""
    with _FACTS_LOCK:
        if "soxr" not in _FACTS:
            cmd = [tool("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                   "-t", "0.01", "-af", "aresample=48000:resampler=soxr", "-f", "null", "-"]
            try:
                ok = subprocess.run(cmd, capture_output=True, timeout=20, **_no_window()).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            _FACTS["soxr"] = ok
        return bool(_FACTS["soxr"])


def ffmpeg_version() -> str:
    """'FFmpeg 6.1.1' from `ffmpeg -version`, or 'FFmpeg' when it cannot be read."""
    with _FACTS_LOCK:
        if "version" not in _FACTS:
            text = "FFmpeg"
            try:
                out = subprocess.run([tool("ffmpeg"), "-hide_banner", "-version"], capture_output=True, text=True,
                                     timeout=20, **_no_window()).stdout
                words = (out.splitlines() or [""])[0].split()
                if len(words) >= 3 and words[0].lower() == "ffmpeg" and words[1] == "version":
                    text = f"FFmpeg {words[2]}"
            except (OSError, subprocess.TimeoutExpired):
                pass
            _FACTS["version"] = text
        return str(_FACTS["version"])


def resampler_name() -> str:
    return "soxr" if has_soxr() else "FFmpeg's default resampler"


# ------------------------------------------------------------------- songs ---
@dataclass
class SongInfo:
    """What the engine needs to know about a song (from the library, or ffprobe)."""
    path: str
    rate: int
    channels: int = 2
    duration: float = 0.0
    bits: int = 0              # bits per sample of the file (0: unknown or lossy)
    is_float: bool = False
    is_lossy: bool = False

    def song(self) -> bp.Song:
        """What the bit-perfect decision needs."""
        return bp.Song(self.rate, self.channels, self.bits, self.is_float, self.is_lossy)


def song_info(path: str) -> SongInfo:
    """Read the song's rate, channels and length with ffprobe.  Raises PlayerError."""
    from .probe import probe

    if not os.path.isfile(path):
        raise PlayerError(f"The file {os.path.basename(path)} does not exist any more.")
    try:
        info = probe(path)
    except Exception as exc:  # noqa: BLE001 - ffprobe failed: not an audio file we can read
        raise PlayerError(f"The file {os.path.basename(path)} could not be read: {_first_line(str(exc))}") from exc
    if not info.sample_rate:
        raise PlayerError(f"The file {os.path.basename(path)} has no audio that can be played.")
    return SongInfo(path, int(info.sample_rate), int(info.channels or 2), float(info.duration or 0.0),
                    int(info.claimed_bits or 0), bool(info.is_float), bool(info.is_lossy))


def _first_line(text: str) -> str:
    text = text.replace("ffprobe failed: ", "").strip()
    line = text.splitlines()[0] if text else "unknown error"
    return line.rstrip(".") + "."


def filter_chain(info: SongInfo, out_rate: int, eq_state: Optional[dict]) -> Optional[str]:
    """The -af chain: mono duplicated to both channels, the resampler when the output
    runs at another rate, and the equalizer (at the output rate, in double precision)."""
    parts = []
    if info.channels == 1:
        parts.append("pan=stereo|c0=c0|c1=c0")   # -ac 2 alone would lower a mono song by 3 dB
    if info.rate != out_rate:
        parts.append(f"aresample={out_rate}" + (":resampler=soxr:precision=28" if has_soxr() else ""))
    eq_chain = eq_mod.ffmpeg_filter(eq_state, out_rate) if eq_state else None
    if eq_chain:
        parts.append("aformat=sample_fmts=dbl")
        parts.append(eq_chain)
    return ",".join(parts) or None


def eq_changes(eq_state: Optional[dict], rate: int) -> bool:
    """Whether the equalizer changes the samples at this rate (its FFmpeg chain is not empty)."""
    return bool(eq_state) and eq_mod.ffmpeg_filter(eq_state, rate) is not None  # type: ignore[arg-type]


def decode_command(path: str, start: float, out_rate: int, chain: Optional[str],
                   slow_seek: bool = False, native: bool = False) -> List[str]:
    """`slow_seek` decodes from the beginning and drops the audio before `start`, for
    files FFmpeg cannot seek in (short FLAC files without a seek table give nothing).
    `native` (bit-perfect at the song's own rate): no `-ar`, the samples as they are."""
    cmd = [tool("ffmpeg"), "-nostdin", "-v", "error"]
    if start > 0 and not slow_seek:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", path]
    if start > 0 and slow_seek:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-map", "0:a:0", "-vn"]
    if chain:
        cmd += ["-af", chain]
    cmd += ["-f", "f32le", "-ac", str(CHANNELS)]
    return cmd + (["pipe:1"] if native else ["-ar", str(out_rate), "pipe:1"])


def device_samples(block: np.ndarray, fmt: str, channels: int = CHANNELS) -> np.ndarray:
    """Float32 stereo frames as the device gets them: float32 for F32, else the integers
    round(x * 2^(bits - 1)), clamped (int16 for I16, int32 for I24 and I32), with silent
    extra channels when the device has more than two (as `bitperfect.int_sample()`)."""
    if channels > block.shape[1]:
        wide = np.zeros((len(block), channels), np.float32)
        wide[:, :block.shape[1]] = block
        block = wide
    if fmt == bp.F32:
        return np.ascontiguousarray(block, np.float32)
    top = float(1 << (bp.CONTAINER_BITS[fmt] - 1))
    v = np.clip(block.astype(np.float64) * top, -top, top - 1.0)
    v = np.rint(np.nan_to_num(v, nan=0.0))           # rint rounds halves to even, as round()
    return v.astype(np.int16 if fmt == bp.I16 else np.int32)


def device_bytes(samples: np.ndarray, fmt: str) -> bytes:
    """`device_samples()` as the little-endian bytes of the format (I24: 3 bytes each)."""
    if fmt == bp.I24:
        return samples.astype("<i4").reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    if fmt == bp.I16:
        return samples.astype("<i2").tobytes()
    return samples.astype("<i4" if fmt == bp.I32 else "<f4").tobytes()


class _Decoder:
    """One FFmpeg process decoding one song from `start`."""

    def __init__(self, info: SongInfo, start: float, out_rate: int, chain: Optional[str],
                 slow_seek: bool = False, native: bool = False, eq: bool = False):
        self.info, self.start, self.rate, self.chain = info, start, out_rate, chain
        self.slow_seek, self.native = slow_seek, native
        self.eq = eq  # the chain holds the equalizer: its samples are dithered for an integer device
        self.frames = 0
        self._rest = b""
        self.proc = subprocess.Popen(decode_command(info.path, start, out_rate, chain, slow_seek, native),
                                     stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window())
        self._err: List[bytes] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                if len(self._err) < 20:
                    self._err.append(line)
        except (OSError, ValueError):
            pass

    def read(self) -> Optional[np.ndarray]:
        """The next block of float32 stereo frames, or None at the end."""
        try:
            data = self.proc.stdout.read(CHUNK_FRAMES * FRAME_BYTES)  # type: ignore[union-attr]
        except (OSError, ValueError):
            data = b""
        if not data:
            return None
        data = self._rest + data
        usable = len(data) - len(data) % FRAME_BYTES
        self._rest = data[usable:]
        if not usable:
            return np.zeros((0, CHANNELS), np.float32)
        block = np.frombuffer(data[:usable], "<f4").reshape(-1, CHANNELS)
        self.frames += len(block)
        return block

    @property
    def decoded_seconds(self) -> float:
        return self.start + self.frames / max(self.rate, 1)

    def error(self) -> str:
        """FFmpeg's complaint when it ended badly, '' when it ended well."""
        try:
            rc = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return ""
        if rc == 0:
            return ""
        text = b"".join(self._err).decode("utf-8", "replace").strip()
        return text.splitlines()[-1].strip() if text else f"FFmpeg stopped with exit code {rc}"

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except OSError:
            pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()  # type: ignore[union-attr]
            except (OSError, AttributeError):
                pass
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


# ------------------------------------------------------------------ outputs ---
@dataclass
class DeviceInfo:
    name: str
    id: object = None          # the backend's device id (miniaudio cdata), None for the default
    default: bool = False
    rates: List[int] = field(default_factory=list)  # sample rates it runs at natively; [] or 0 = any


@dataclass
class Exclusive:
    """How bit-perfect playback reaches a device: `access` (bitperfect.WASAPI, ALSA,
    COREAUDIO or DIRECT), `label` (the ALSA device "hw:0", else the device's name), what
    it offers (`modes`, [] = anything) and `error` (why it cannot be had for this app
    alone, a full sentence; the song then plays through the normal path)."""
    access: str
    label: str = ""
    modes: List[bp.Mode] = field(default_factory=list)
    error: str = ""
    target: object = None      # what open() needs to reach it (backend specific)
    note: str = ""             # added to the reasons when the song is not played as it is


class OpenError(PlayerError):
    """The device could not be opened; `busy`: another program holds it."""

    def __init__(self, message: str, busy: bool = False, detail: str = ""):
        super().__init__(message)
        self.busy, self.detail = busy, detail

    def reason(self) -> str:
        return bp.BUSY if self.busy else bp.failure_reason(self.detail)


class Output:
    """Where the frames go.  `open()` starts pulling: the device calls
    `callback(n)` whenever it needs n frames (float32, shape (m, 2), m <= n).
    `fmt`/`channels`: what the device is opened with (bit-perfect playback; the
    output turns the floats into it with `device_samples()`); `exclusive`: the
    `Exclusive` that `exclusive()` returned, for bit-perfect access, or None."""

    name = ""
    backend = ""
    realtime = True            # False: every frame handed over counts as heard at once (tests)

    def devices(self) -> List[DeviceInfo]:
        return []

    def device_for(self, name: str) -> DeviceInfo:
        devs = self.devices()
        for d in devs:
            if name and d.name == name:
                return d
        for d in devs:
            if d.default:
                return d
        return devs[0] if devs else DeviceInfo("System default", None, True, [])

    def native_rate(self, dev: DeviceInfo) -> int:
        """The rate to run a device at when it cannot run at the song's: its only rate,
        else 48 kHz or 44.1 kHz when it has them, else its highest."""
        good = sorted({r for r in dev.rates if r})
        if len(good) == 1:
            return good[0]
        for r in (48000, 44100):
            if r in good or not good:
                return r
        return good[-1]

    def pick_rate(self, device: str, song_rate: int) -> int:
        """The song's rate when the device runs at it, else the device's own rate."""
        dev = self.device_for(device)
        if not dev.rates or 0 in dev.rates or song_rate in dev.rates:
            return song_rate
        return self.native_rate(dev)

    def exclusive(self, device: str) -> Exclusive:
        """How bit-perfect playback reaches `device` and what it offers there."""
        return Exclusive(bp.DIRECT, self.device_for(device).name if device else (self.name or "System default"))

    def open(self, device: str, rate: int, callback: Callable[[int], np.ndarray],
             ready: Optional[Callable[[int, float], None]] = None, fmt: str = bp.F32, channels: int = CHANNELS,
             exclusive: Optional[Exclusive] = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _miniaudio():
    try:
        import miniaudio  # noqa: F401 - the optional sound output (pip install miniaudio)
    except ImportError as exc:
        raise PlayerError("Sound output needs the miniaudio package, which is not installed "
                          "(pip install miniaudio).") from exc
    return miniaudio


# miniaudio's result codes that the bit-perfect fallback explains
_MA_BUSY, _MA_FORMAT_NOT_SUPPORTED, _MA_SHARE_MODE_NOT_SUPPORTED = -19, -200, -202


def _ma_formats(ma) -> Dict[str, object]:
    return {bp.I16: ma.SampleFormat.SIGNED16, bp.I24: ma.SampleFormat.SIGNED24, bp.I32: ma.SampleFormat.SIGNED32,
            bp.F32: ma.SampleFormat.FLOAT32}


def _ma_code(exc: Exception) -> int:
    for a in getattr(exc, "args", ())[1:]:
        if isinstance(a, int):
            return a
    return 0


def _ma_open_error(exc: Exception, what: str) -> OpenError:
    code = _ma_code(exc)
    detail = {_MA_SHARE_MODE_NOT_SUPPORTED: "the system does not allow exclusive use of it",
              _MA_FORMAT_NOT_SUPPORTED: "it does not take that format"}.get(code, f"{exc}")
    return OpenError(f"{what} ({exc}).", busy=code == _MA_BUSY, detail=detail)


def _miniaudio_device(ma, fmt: str, channels: int, rate: int, device_id, backends, exclusive: bool,
                      alsa_name: str = ""):
    """A miniaudio playback device built like miniaudio.PlaybackDevice, but with the
    settings bit-perfect playback needs: exclusive share mode, and an ALSA device name
    used as it is ("hw:0,0", which miniaudio opens without dmix or plug).  Raises
    MiniaudioError."""
    ffi, lib = ma.ffi, ma.lib

    class Device(ma.PlaybackDevice):
        def __init__(self):  # noqa: D401 - pyminiaudio 1.73's PlaybackDevice.__init__, plus share mode
            ma.AbstractDevice.__init__(self)
            self.format = _ma_formats(ma)[fmt]
            self.sample_width = ma.width_from_format(self.format)
            self.nchannels, self.sample_rate, self.buffersize_msec = channels, rate, PERIOD_MS
            self._ffi_handle = ffi.new_handle(self)
            cfg = self._devconfig = lib.ma_device_config_init(lib.ma_device_type_playback)
            cfg.sampleRate = rate
            cfg.playback.channels = channels
            cfg.playback.format = self.format.value
            self._id = device_id
            if alsa_name:
                self._id = ffi.new("ma_device_id *")
                name = alsa_name.encode("ascii")[:200] + b"\0"
                ffi.memmove(self._id, name, len(name))  # the union's ALSA member: char[256] at offset 0
            cfg.playback.pDeviceID = self._id or ffi.NULL
            if exclusive:
                cfg.playback.shareMode = lib.ma_share_mode_exclusive
            cfg.noClip = 1
            cfg.wasapi.noAutoConvertSRC = 1
            cfg.alsa.noAutoFormat = cfg.alsa.noAutoChannels = cfg.alsa.noAutoResample = 1
            cfg.periodSizeInMilliseconds = PERIOD_MS
            cfg.pUserData = self._ffi_handle
            cfg.dataCallback = lib._internal_data_callback
            cfg.stopCallback = lib._internal_stop_callback
            cfg.periods = 0
            self.callback_generator = None
            self._context = self._make_context(backends or [], ma.ThreadPriority.HIGHEST, "Real-Hi-Res")
            result = lib.ma_device_init(self._context, ffi.addressof(cfg), self._device)
            if result != lib.MA_SUCCESS:
                raise ma.MiniaudioError("failed to init device", result)
            self.backend = ffi.string(lib.ma_get_backend_name(self._device.pContext.backend)).decode()

    return Device()


_OPEN_OUTPUTS: "weakref.WeakSet[MiniaudioOutput]" = weakref.WeakSet()


@atexit.register
def _close_open_outputs() -> None:
    """Close every device still open when Python exits.  miniaudio's own thread calls
    into Python for each period, and a call after the interpreter has shut down
    crashes the process (SIGSEGV in PyGILState_Ensure, seen on macOS when a window
    was destroyed without the app's Quit).  atexit runs while Python still works."""
    for out in list(_OPEN_OUTPUTS):
        try:
            out.close()
        except Exception:  # noqa: BLE001 - exiting: nothing more to do
            pass


class MiniaudioOutput(Output):
    """A sound device through miniaudio.  `backends` = None: the system's real sound
    systems in miniaudio's order of preference (never the silent NULL backend, unless
    REALHIRES_AUDIO_BACKEND=null asks for it).  A device still open when Python
    exits is closed by `_close_open_outputs()`."""

    def __init__(self, backends=None):
        self._backends = backends
        self._device = None
        self._dev_cache: Optional[Tuple[float, List[DeviceInfo]]] = None
        self._ex_cache: Optional[Tuple[str, Exclusive]] = None
        self._ex_open = False
        self.name = ""
        self.backend = ""

    def backends(self):
        ma = _miniaudio()
        if self._backends is not None:
            return self._backends
        if os.environ.get(BACKEND_ENV, "").strip().lower() == "null":
            return [ma.Backend.NULL]
        enabled = ma.get_enabled_backends()
        return [b for b in ma.Backend if b in enabled and b not in (ma.Backend.NULL, ma.Backend.CUSTOM)]

    def devices(self) -> List[DeviceInfo]:
        """The playback devices (cached for 5 s; the list is asked for on every song)."""
        now = time.monotonic()
        if self._dev_cache is not None and now - self._dev_cache[0] < 5.0:
            return self._dev_cache[1]
        out: List[DeviceInfo] = []
        try:
            ma = _miniaudio()
            ffi, lib = ma.ffi, ma.lib
            ctx = ma.Devices(backends=self.backends())
            infos, count = ffi.new("ma_device_info**"), ffi.new("ma_uint32*")
            if lib.ma_context_get_devices(ctx._context, infos, count, ffi.NULL, ffi.NULL) == lib.MA_SUCCESS:
                for i in range(count[0]):
                    info = infos[0][i]
                    lib.ma_context_get_device_info(ctx._context, lib.ma_device_type_playback,
                                                   ffi.addressof(info.id), ffi.addressof(info))
                    rates = sorted({int(info.nativeDataFormats[k].sampleRate)
                                    for k in range(info.nativeDataFormatCount)})
                    out.append(DeviceInfo(ffi.string(info.name).decode("utf-8", "replace"),
                                          ffi.new("ma_device_id *", info.id), bool(info.isDefault), rates))
            self.backend = ctx.backend
            del ctx
        except Exception:  # noqa: BLE001 - no sound system at all: no devices
            out = []
        self._dev_cache = (now, out)
        return out

    # ---- bit-perfect access ---------------------------------------------
    def exclusive(self, device: str) -> Exclusive:
        """What bit-perfect playback gets on this system: WASAPI exclusive mode, the ALSA
        hardware device, or the device switched to the song's rate (CoreAudio).  The
        answer for the device open now is reused (probing would find it busy)."""
        if self._ex_open and self._ex_cache is not None and self._ex_cache[0] == device:
            return self._ex_cache[1]
        try:
            ma = _miniaudio()
            backends = self.backends()
            if backends == [ma.Backend.NULL]:
                ex = Exclusive(bp.DIRECT, "NULL Playback Device", [], target=("null", None))
            elif sys.platform.startswith("win") and ma.Backend.WASAPI in backends:
                ex = self._wasapi(ma, device)
            elif sys.platform == "darwin":
                ex = _coreaudio_exclusive(device)
            elif sys.platform.startswith("linux") and ma.Backend.ALSA in ma.get_enabled_backends():
                ex = _alsa_exclusive(device)
            else:
                ex = Exclusive(bp.DIRECT, device or "System default",
                               error=bp.failure_reason("this system has no way to do that"))
        except PlayerError as exc:
            ex = Exclusive(bp.DIRECT, device or "System default", error=bp.failure_reason(str(exc)))
        except Exception as exc:  # noqa: BLE001 - a backend that cannot be asked: play the normal way
            ex = Exclusive(bp.DIRECT, device or "System default", error=bp.failure_reason(str(exc)))
        self._ex_cache = (device, ex)
        return ex

    def _wasapi(self, ma, device: str) -> Exclusive:
        """WASAPI exclusive mode: miniaudio 0.11 opens an exclusive stream in the format
        Windows' Sound settings set for the device (its "Default Format"), which is the one
        format it lists with the exclusive-mode flag."""
        ffi, lib = ma.ffi, ma.lib
        dev = self.device_for(device)
        ctx = ma.Devices(backends=[ma.Backend.WASAPI])
        info = ffi.new("ma_device_info *")
        res = lib.ma_context_get_device_info(ctx._context, lib.ma_device_type_playback, dev.id or ffi.NULL, info)
        del ctx
        if res != lib.MA_SUCCESS:
            raise PlayerError(f"Windows could not describe the device (miniaudio error {res})")
        names = {int(lib.ma_format_s16): bp.I16, int(lib.ma_format_s24): bp.I24, int(lib.ma_format_s32): bp.I32,
                 int(lib.ma_format_f32): bp.F32}
        modes = []
        for k in range(info.nativeDataFormatCount):
            nd = info.nativeDataFormats[k]
            if nd.flags & 2 and int(nd.format) in names and nd.sampleRate:  # MA_DATA_FORMAT_FLAG_EXCLUSIVE_MODE
                modes.append(bp.Mode(names[int(nd.format)], int(nd.sampleRate), int(nd.sampleRate), int(nd.channels)))
        name = dev.name if device or dev.default else "System default"
        if not modes:
            return Exclusive(bp.WASAPI, name, error=bp.failure_reason("Windows offers no exclusive-mode format for it"))
        return Exclusive(bp.WASAPI, name, modes, target=("wasapi", dev.id if device else None),
                         note=bp.WASAPI_FORMAT_NOTE)

    # ---- playing -----------------------------------------------------------
    def open(self, device, rate, callback, ready=None, fmt=bp.F32, channels=CHANNELS, exclusive=None) -> None:
        ma = _miniaudio()
        self.close()
        dev = self.device_for(device)
        target = exclusive.target if exclusive is not None else None
        try:
            if target is None:
                d = ma.PlaybackDevice(output_format=_ma_formats(ma)[fmt], nchannels=channels, sample_rate=rate,
                                      buffersize_msec=PERIOD_MS, device_id=dev.id if device else None,
                                      backends=self.backends(), app_name="Real-Hi-Res")
            elif target[0] == "wasapi":
                d = _miniaudio_device(ma, fmt, channels, rate, target[1], [ma.Backend.WASAPI], True)
            elif target[0] == "alsa":
                d = _miniaudio_device(ma, fmt, channels, rate, None, [ma.Backend.ALSA], True, alsa_name=target[1])
            elif target[0] == "coreaudio":
                target[1].set_rate(rate)  # raises OpenError
                d = _miniaudio_device(ma, fmt, channels, rate, dev.id if device else None, [ma.Backend.COREAUDIO],
                                     False)
            else:  # the NULL backend: nothing in between
                d = _miniaudio_device(ma, fmt, channels, rate, None, self.backends(), False)
        except OpenError:
            raise
        except Exception as exc:  # noqa: BLE001 - MiniaudioError: no device, busy device...
            raise _ma_open_error(exc, "No sound output device could be opened") from exc
        width = channels

        def frames():
            n = yield b""
            while True:
                block = callback(n)
                if len(block) < n:
                    pad = np.zeros((n, CHANNELS), block.dtype)
                    pad[:len(block)] = block
                    block = pad
                n = yield device_bytes(device_samples(block, fmt, width), fmt)

        gen = frames()
        next(gen)
        try:
            d.start(gen)
        except Exception as exc:  # noqa: BLE001
            d.close()
            raise _ma_open_error(exc, "The sound output device could not be started") from exc
        self._device = d
        _OPEN_OUTPUTS.add(self)
        self._ex_open = exclusive is not None
        if exclusive is not None:
            self.name = exclusive.label
        else:
            self.name = dev.name if device else (dev.name if dev.default else "System default")
        self.backend = d.backend

    def close(self) -> None:
        d, self._device = self._device, None
        self._ex_open = False
        _OPEN_OUTPUTS.discard(self)
        if d is not None:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------- bit-perfect: ALSA and macOS ---
_ALSA_FORMATS = ((bp.I16, 2), (bp.I24, 32), (bp.I32, 10), (bp.F32, 14))  # S16_LE, S24_3LE, S32_LE, FLOAT_LE
_EBUSY = 16


def _alsa_exclusive(device: str) -> Exclusive:
    """The ALSA hardware device of the chosen output's card (or card 0), and the
    formats and rates it takes in stereo (asked of libasound directly, because
    miniaudio describes "hw:" devices only through dmix)."""
    import ctypes
    import ctypes.util

    try:
        with open("/proc/asound/cards", encoding="utf-8", errors="replace") as fh:
            cards = bp.alsa_cards(fh.read())
    except OSError:
        cards = []
    hw = bp.alsa_device(device, cards)
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("asound") or "libasound.so.2")
    except OSError:
        return Exclusive(bp.ALSA, hw, error=bp.failure_reason("the ALSA library is missing"))
    lib.snd_strerror.restype = ctypes.c_char_p
    lib.snd_pcm_hw_params_sizeof.restype = ctypes.c_size_t
    pcm = ctypes.c_void_p()
    res = lib.snd_pcm_open(ctypes.byref(pcm), hw.encode("ascii"), 0, 1)  # PLAYBACK, NONBLOCK
    if res < 0:
        if -res == _EBUSY:
            return Exclusive(bp.ALSA, hw, error=bp.BUSY)
        return Exclusive(bp.ALSA, hw, error=bp.failure_reason(
            f"ALSA: {(lib.snd_strerror(res) or b'error').decode('utf-8', 'replace')}"))
    modes: List[bp.Mode] = []
    try:
        params = ctypes.create_string_buffer(int(lib.snd_pcm_hw_params_sizeof()) + 64)
        lo, hi = ctypes.c_uint(), ctypes.c_uint()
        for fmt, code in _ALSA_FORMATS:
            if lib.snd_pcm_hw_params_any(pcm, params) < 0 or lib.snd_pcm_hw_params_test_format(pcm, params, code):
                continue
            lib.snd_pcm_hw_params_set_format(pcm, params, code)
            channels = 2
            if lib.snd_pcm_hw_params_test_channels(pcm, params, ctypes.c_uint(2)):
                lib.snd_pcm_hw_params_get_channels_min(params, ctypes.byref(lo))
                lib.snd_pcm_hw_params_get_channels_max(params, ctypes.byref(hi))
                if hi.value < 2:
                    continue
                channels = max(lo.value, 2)
            lib.snd_pcm_hw_params_set_channels(pcm, params, ctypes.c_uint(channels))
            for r in bp.STANDARD_RATES:
                if lib.snd_pcm_hw_params_test_rate(pcm, params, ctypes.c_uint(r), 0) == 0:
                    modes.append(bp.Mode(fmt, r, r, channels))
    finally:
        lib.snd_pcm_close(pcm)
    if not modes:
        return Exclusive(bp.ALSA, hw, error=bp.failure_reason("it takes no stereo format this player writes"))
    return Exclusive(bp.ALSA, hw, modes, target=("alsa", hw))


class _CoreAudioDevice:
    """A macOS output device through CoreAudio (ctypes): its available nominal sample
    rates, and setting the nominal rate (what miniaudio leaves alone unless asked)."""

    def __init__(self, name: str):
        import ctypes
        import ctypes.util

        self.ct = ctypes
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio") or
                              "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or
                              "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self.ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self.ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.device = self._find(name)
        if not self.device:
            raise PlayerError(f"CoreAudio has no output device {name!r}" if name else "CoreAudio has no output device")

    def _addr(self, selector: str, scope: str = "glob"):
        ct = self.ct

        class Address(ct.Structure):
            _fields_ = [("mSelector", ct.c_uint32), ("mScope", ct.c_uint32), ("mElement", ct.c_uint32)]

        def four(code: str) -> int:
            return int.from_bytes(code.encode("ascii"), "big")
        return Address(four(selector), four(scope), 0)  # kAudioObjectPropertyElementMain

    def _get(self, obj: int, selector: str, ctype, count: int = 1, scope: str = "glob"):
        ct = self.ct
        addr = self._addr(selector, scope)
        if count < 0:  # as many as there are
            size = ct.c_uint32(0)
            if self.ca.AudioObjectGetPropertyDataSize(ct.c_uint32(obj), ct.byref(addr), 0, None, ct.byref(size)):
                return []
            count = size.value // ct.sizeof(ctype)
        buf = (ctype * max(count, 1))()
        size = ct.c_uint32(ct.sizeof(ctype) * count)
        if self.ca.AudioObjectGetPropertyData(ct.c_uint32(obj), ct.byref(addr), 0, None, ct.byref(size), buf):
            return []
        return list(buf)[:size.value // ct.sizeof(ctype)]

    def _name(self, obj: int) -> str:
        ct = self.ct
        ref = self._get(obj, "lnam", ct.c_void_p)  # kAudioObjectPropertyName (a CFStringRef)
        if not ref or not ref[0]:
            return ""
        buf = ct.create_string_buffer(1024)
        ok = self.cf.CFStringGetCString(ref[0], buf, 1024, 0x08000100)  # kCFStringEncodingUTF8
        self.cf.CFRelease(ref[0])
        return buf.value.decode("utf-8", "replace") if ok else ""

    def _find(self, name: str) -> int:
        ct = self.ct
        if name:
            for obj in self._get(1, "dev#", ct.c_uint32, -1):  # kAudioObjectSystemObject, kAudioHardwarePropertyDevices
                if self._name(obj) == name and self._get(obj, "stm#", ct.c_uint32, -1, "outp"):
                    return int(obj)
        dev = self._get(1, "dOut", ct.c_uint32)  # kAudioHardwarePropertyDefaultOutputDevice
        return int(dev[0]) if dev else 0

    def rates(self) -> List[Tuple[int, int]]:
        ct = self.ct

        class Range(ct.Structure):
            _fields_ = [("mMinimum", ct.c_double), ("mMaximum", ct.c_double)]
        ranges = self._get(self.device, "nsr#", Range, -1)  # kAudioDevicePropertyAvailableNominalSampleRates
        return [(int(round(r.mMinimum)), int(round(r.mMaximum))) for r in ranges if r.mMaximum >= r.mMinimum > 0]

    def rate(self) -> float:
        got = self._get(self.device, "nsrt", self.ct.c_double)  # kAudioDevicePropertyNominalSampleRate
        return float(got[0]) if got else 0.0

    def set_rate(self, rate: int) -> None:
        """Switch the device to `rate` and wait (up to 2 s) until it runs at it."""
        ct = self.ct
        if abs(self.rate() - rate) < 1:
            return
        value = ct.c_double(float(rate))
        addr = self._addr("nsrt")
        status = self.ca.AudioObjectSetPropertyData(ct.c_uint32(self.device), ct.byref(addr), 0, None,
                                                    ct.sizeof(value), ct.byref(value))
        if status:
            raise OpenError(f"CoreAudio could not switch the device to {bp.fmt_khz(rate)} (error {status}).",
                            detail=f"CoreAudio could not switch it to {bp.fmt_khz(rate)}")
        end = time.monotonic() + 2.0
        while time.monotonic() < end and abs(self.rate() - rate) >= 1:
            time.sleep(0.02)
        if abs(self.rate() - rate) >= 1:
            raise OpenError(f"The device did not switch to {bp.fmt_khz(rate)}.",
                            detail=f"it did not switch to {bp.fmt_khz(rate)}")


def _coreaudio_exclusive(device: str) -> Exclusive:
    """macOS: the device is switched to the song's rate (its nominal sample rate), so
    CoreAudio does not resample; the samples go to it as 32-bit floats."""
    dev = _CoreAudioDevice(device)
    modes = [bp.Mode(bp.F32, lo, hi, 0) for lo, hi in dev.rates()]
    label = device or dev._name(dev.device) or "System default"
    if not modes:
        return Exclusive(bp.COREAUDIO, label, error=bp.failure_reason("CoreAudio lists no sample rate for it"))
    return Exclusive(bp.COREAUDIO, label, modes, target=("coreaudio", dev))


class ManualOutput(Output):
    """A pretend device for tests: nothing plays until `pull(n)` asks for frames.
    `rates` are the rates it accepts (empty: any), like a device's native formats;
    `formats` the sample formats it takes for bit-perfect playback (empty: any);
    `busy`: another program holds it, so bit-perfect playback cannot have it.  `pull()`
    returns what the device gets: float32 frames, or the integers of `fmt`."""

    realtime = False

    def __init__(self, rates: Optional[List[int]] = None, name: str = "Test output",
                 formats: Optional[List[str]] = None, busy: bool = False):
        self.name = name
        self.backend = "manual"
        self.rates = list(rates or [])
        self.formats = list(formats or [])
        self.busy = busy
        self.rate = 0
        self.fmt = bp.F32
        self.channels = CHANNELS
        self.opened: List[int] = []
        self.opened_formats: List[str] = []
        self.exclusive_opened: List[bool] = []
        self._callback: Optional[Callable[[int], np.ndarray]] = None
        self._ready: Optional[Callable[[int, float], None]] = None

    def devices(self) -> List[DeviceInfo]:
        return [DeviceInfo(self.name, None, True, list(self.rates))]

    def native_rate(self, dev: DeviceInfo) -> int:
        return self.rates[0] if self.rates else 48000

    def exclusive(self, device: str) -> Exclusive:
        if not self.rates and not self.formats:
            modes: List[bp.Mode] = []
        else:
            rates = self.rates or list(bp.STANDARD_RATES)
            modes = [bp.Mode(f, r, r, 2) for f in (self.formats or list(bp.FORMATS)) for r in rates]
        return Exclusive(bp.DIRECT, self.name, modes, target=("manual", None))

    def open(self, device, rate, callback, ready=None, fmt=bp.F32, channels=CHANNELS, exclusive=None) -> None:
        if exclusive is not None and self.busy:
            raise OpenError("The test output is in use.", busy=True)
        self.rate, self.fmt, self.channels = rate, fmt, channels
        self.opened.append(rate)
        self.opened_formats.append(fmt)
        self.exclusive_opened.append(exclusive is not None)
        self._callback, self._ready = callback, ready

    def close(self) -> None:
        self._callback = None

    def pull(self, n: int, timeout: float = 10.0) -> np.ndarray:
        """Up to n frames, as a device would get them (waits until the device has been
        opened and the decoder has them)."""
        end = time.monotonic() + timeout
        while self._callback is None and time.monotonic() < end:
            time.sleep(0.005)
        if self._callback is None:
            return np.zeros((0, CHANNELS), np.float32)
        if self._ready is not None:
            self._ready(n, timeout)
        cb = self._callback
        block = cb(n) if cb is not None else np.zeros((0, CHANNELS), np.float32)
        if self.fmt == bp.F32 and self.channels == CHANNELS:
            return block
        return device_samples(block, self.fmt, self.channels)


def default_output() -> Output:
    return MiniaudioOutput()


def list_devices(output: Optional[Output] = None) -> List[DeviceInfo]:
    """The playback devices of the system (for Settings → Playback → Output device)."""
    return (output or default_output()).devices()


# ------------------------------------------------------------------ engine ---
@dataclass
class _Segment:
    """One song in the stream.  `start` is the global frame at which its first frame
    was handed to the device (None until then); `base` the song time it starts at."""
    path: str
    info: SongInfo
    base: float
    rate: int
    start: Optional[int] = None
    out: Optional["_OutPlan"] = None


@dataclass
class _OutPlan:
    """How the device is opened for a song.  `ex` is set when bit-perfect playback has
    the device for itself; `plan` (the bit-perfect decision) whenever the setting is on."""
    rate: int
    fmt: str = bp.F32
    channels: int = CHANNELS
    ex: Optional[Exclusive] = None
    plan: Optional[bp.Plan] = None
    song: Optional[bp.Song] = None
    access: str = bp.DIRECT
    label: str = ""

    def key(self) -> tuple:
        """Songs with the same key play one after the other on the open device."""
        return (self.rate, self.fmt, self.channels, self.ex is not None)

    @property
    def native(self) -> bool:
        """Bit-perfect at the song's own rate: FFmpeg decodes without -ar."""
        return self.plan is not None and self.song is not None and self.rate == self.song.rate


@dataclass
class _Prefetch:
    path: str
    info: Optional[SongInfo] = None
    out: Optional[_OutPlan] = None
    chain: Optional[str] = None
    decoder: Optional[_Decoder] = None
    error: str = ""
    done: threading.Event = field(default_factory=threading.Event)


class Player:
    """Plays one song at a time, gaplessly into the next one.  Thread-safe; meant to be
    driven from the GUI thread, which calls `poll()` a few times a second."""

    def __init__(self, output: Optional[Output] = None, device: str = "", gapless: bool = True):
        self.output = output or default_output()
        self.device = device
        self.gapless = gapless
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._out_lock = threading.Lock()      # opening/closing the output (never with _lock held)
        self._gen = 0
        self._chunks: collections.deque = collections.deque()  # [gen, segment, frames, offset, eq]
        self._buffered = 0
        self._segs: collections.deque = collections.deque()    # heard now, then the ones queued behind it
        self._eos = False                      # the decoder has nothing more for this generation
        self._draining = 0                     # the generation whose decoder waits for the device
        self._paused = True
        self._active = False                   # a song is loaded (playing or paused)
        self._played = 0                       # frames handed to the device since it was opened
        self._blk_time = 0.0
        self._blk_start = 0
        self._blk_len = 0
        self._out_rate = 0
        self._out_device: Optional[str] = None
        self._volume, self._muted = 0.8, False
        self._gain = 0.64
        self._bit_perfect = False
        self._out_plan: Optional[_OutPlan] = None   # how the open device was opened
        self._eq = eq_mod.default_state()
        self._eq_timer: Optional[threading.Timer] = None
        self._rng = np.random.default_rng()       # the TPDF dither (2.3)
        self._next_path: Optional[str] = None
        self._next_after: Optional[str] = None
        self._prefetch: Optional[_Prefetch] = None
        self._events: collections.deque = collections.deque()
        self._error = ""

    # ------------------------------------------------------------ controls ---
    def play(self, path: str, position: float = 0.0, info: Optional[SongInfo] = None,
             paused: bool = False) -> None:
        """Start `path` at `position` seconds (the queue's next song comes from `set_next()`)."""
        path = os.path.abspath(path)
        with self._lock:
            gen = self._new_generation()
            self._paused = paused
            self._active = True
            self._error = ""
            placeholder = info or SongInfo(path, 0, 2, 0.0)
            self._segs.append(_Segment(path, placeholder, max(0.0, position), 0))
        threading.Thread(target=self._run, args=(gen, path, max(0.0, position), info), daemon=True,
                         name="realhires-decoder").start()

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if self._active:
                self._paused = False

    def toggle(self) -> None:
        with self._lock:
            if self._paused:
                self.resume()
            else:
                self.pause()

    def seek(self, seconds: float) -> None:
        """Jump within the current song (FFmpeg restarts there and the buffer is dropped)."""
        with self._lock:
            seg = self._current_seg()
            if seg is None:
                return
            dur = seg.info.duration
            seconds = max(0.0, min(seconds, dur - 0.05) if dur else seconds)
            path, info, paused = seg.path, seg.info if seg.info.rate else None, self._paused
        self.play(path, seconds, info, paused=paused)

    def stop(self) -> None:
        """Stop and close the device (the song is forgotten)."""
        with self._lock:
            self._new_generation()
            self._active = False
            self._paused = True
        self._close_output()

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._drop_prefetch()

    def set_next(self, path: Optional[str], after: Optional[str] = None) -> None:
        """The song to continue with when `after` (default: the song playing now) ends;
        None: stop there.  It is used once: call it again whenever a song starts
        ("advanced"), as the queue moves on."""
        with self._lock:
            if after is None:
                seg = self._segs[-1] if self._segs else None
                after = seg.path if seg is not None else None
            self._next_after = os.path.abspath(after) if after else None
            self._next_path = os.path.abspath(path) if path else None

    def _next_for(self, path: str) -> Optional[str]:
        """The song set to follow `path` (called with _lock held)."""
        if self._next_path and (self._next_after is None or self._next_after == path):
            return self._next_path
        return None

    def set_volume(self, volume: float, muted: bool = False) -> None:
        """gain = volume squared, 0 when muted; exactly 1 while bit-perfect playback is on
        (the volume is kept for when it is turned off)."""
        with self._lock:
            self._volume = min(max(float(volume), 0.0), 1.0)
            self._muted = bool(muted)
            self._update_gain()

    def _update_gain(self) -> None:
        self._gain = 0.0 if self._muted else 1.0 if self._bit_perfect else self._volume ** 2

    def set_bit_perfect(self, on: bool) -> None:
        """Bit-perfect playback on or off: from the next song, and at once for the song
        playing, which restarts at its position (the device is opened again)."""
        with self._lock:
            if bool(on) == self._bit_perfect:
                return
            self._bit_perfect = bool(on)
            self._update_gain()
            playing = self._active
            pos = self.position() if playing else 0.0
        if playing:
            self._close_output()
            with self._lock:
                seg = self._current_seg()
                if seg is None or not self._active:
                    return
                path, info, paused = seg.path, seg.info if seg.info.rate else None, self._paused
            self.play(path, pos, info, paused=paused)

    @property
    def bit_perfect(self) -> bool:
        with self._lock:
            return self._bit_perfect

    def _eq_used(self) -> Optional[dict]:
        """The equalizer FFmpeg runs (2.3: also while bit-perfect playback is on; it was
        bypassed then before)."""
        return self._eq

    def set_gapless(self, on: bool) -> None:
        with self._lock:
            self.gapless = bool(on)

    def set_device(self, name: str) -> None:
        """Play through another device from now on (the song continues there)."""
        with self._lock:
            if name == self.device:
                return
            self.device = name
            playing = self._active
        if playing:
            self._close_output()
            self.seek(self.position())

    def set_eq(self, state: dict) -> None:
        """A new equalizer: heard at once (FFmpeg restarts at the current position)."""
        state = eq_mod.normalize(state)
        with self._lock:
            seg = self._current_seg()
            old = filter_chain(seg.info, seg.rate, self._eq_used()) if seg is not None and seg.info.rate else None
            self._eq = state
            new = filter_chain(seg.info, seg.rate, self._eq_used()) if seg is not None and seg.info.rate else None
            if seg is None or not self._active or old == new:
                return
            if self._eq_timer is not None:
                self._eq_timer.cancel()
            self._eq_timer = threading.Timer(EQ_RESTART_DELAY_S, self._apply_eq)
            self._eq_timer.daemon = True
            self._eq_timer.start()

    def _apply_eq(self) -> None:
        with self._lock:
            self._eq_timer = None
            if not self._active:
                return
        self.seek(self.position())

    # --------------------------------------------------------------- state ---
    @property
    def current(self) -> Optional[str]:
        with self._lock:
            seg = self._current_seg()
            return seg.path if seg is not None and self._active else None

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._active and not self._paused

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def duration(self) -> float:
        with self._lock:
            seg = self._current_seg()
            return seg.info.duration if seg is not None else 0.0

    @property
    def gain(self) -> float:
        with self._lock:
            return self._gain

    @property
    def eq_state(self) -> dict:
        with self._lock:
            return self._eq

    def position(self) -> float:
        """Seconds into the current song, as heard."""
        with self._lock:
            self._advance()
            seg = self._current_seg()
            if seg is None:
                return 0.0
            heard = self._heard()
            if seg.start is None or heard <= seg.start or not seg.rate:
                return seg.base
            pos = seg.base + (heard - seg.start) / seg.rate
            return min(pos, seg.info.duration) if seg.info.duration else pos

    def poll(self) -> List[tuple]:
        """Events since the last call: ("advanced", path) when the next song's first
        sample is heard, ("finished", path) when the last song has been heard to the
        end, ("error", path, message) when a song cannot be played."""
        with self._lock:
            self._advance()
            out = list(self._events)
            self._events.clear()
            return out

    def output_info(self) -> dict:
        """For the Playback info window: the device, what it gets ("rate", "channels",
        "format" as a label), the song's rate, the resampler, the gain, and the
        "Bit-perfect" line (`bitperfect.status()`)."""
        with self._lock:
            seg = self._current_seg()
            song_rate = seg.info.rate if seg is not None else 0
            out_rate = self._out_rate if self._out_device is not None else 0
            out = seg.out if seg is not None and seg.out is not None else self._out_plan
            fmt, channels, label = bp.F32, CHANNELS, SAMPLE_FORMAT_LABEL
            if out is not None and out_rate:
                bits = bp.song_bits(out.song) if out.song is not None else 0
                fmt, channels, label = out.fmt, out.channels, bp.format_label(out.fmt, bits)
            status = bp.OFF
            if self._bit_perfect:
                status = bp.ON_IDLE
                if out is not None and out.plan is not None and out_rate and self._active:
                    eq_on = seg is not None and eq_changes(self._eq, seg.rate or out_rate)
                    status = bp.status(out.song, out.plan, out.access, out.label, eq_on=eq_on)
            return {"device": self.output.name or "System default", "backend": self.output.backend,
                    "rate": out_rate, "channels": channels, "format": label, "sample_format": fmt,
                    "song_rate": song_rate,
                    "resampler": resampler_name() if out_rate and song_rate and out_rate != song_rate else "",
                    "gain": self._gain, "muted": self._muted, "gapless": self.gapless,
                    "bit_perfect_on": self._bit_perfect, "bit_perfect": status, "error": self._error}

    # ------------------------------------------------------------ internals ---
    def _new_generation(self) -> int:
        """Drop everything queued for the device (called with _lock held)."""
        self._gen += 1
        self._chunks.clear()
        self._buffered = 0
        self._segs.clear()
        self._events.clear()   # what the songs before did no longer matters
        self._eos = False
        self._cond.notify_all()
        return self._gen

    def _current_seg(self) -> Optional[_Segment]:
        return self._segs[0] if self._segs else None

    def _heard(self) -> float:
        """Frames heard so far (global), interpolated between device callbacks."""
        if not self.output.realtime or not self._out_rate:
            return float(self._played)
        elapsed = max(0.0, time.monotonic() - self._blk_time)
        return self._blk_start + min(float(self._blk_len), elapsed * self._out_rate)

    def _advance(self) -> None:
        """The next song has started to be heard: it becomes the current one."""
        heard = self._heard()
        while len(self._segs) > 1 and self._segs[1].start is not None and heard >= self._segs[1].start:
            self._segs.popleft()
            self._events.append(("advanced", self._segs[0].path))

    def _tpdf(self, frames: int, top: float) -> np.ndarray:
        """TPDF dither of ±1 LSB for an integer device whose full scale is `top` (2.3): the
        sum of two uniform numbers in [-0.5, 0.5) LSB, as a float added to the samples."""
        r = self._rng.random((2, frames, CHANNELS))
        return (r[0] + r[1] - 1.0) / top

    def _pull(self, n: int) -> np.ndarray:
        """The device callback: n frames (fewer when the decoder is behind).  For an
        integer device, the frames the equalizer changed get TPDF dither, and then the
        block is float64, so that `device_samples()` rounds the dithered values exactly."""
        out = np.zeros((n, CHANNELS), np.float32)
        try:
            now = time.monotonic()
            with self._lock:
                filled = 0
                plan = self._out_plan
                top = float(1 << (bp.CONTAINER_BITS[plan.fmt] - 1)) if plan is not None and \
                    plan.fmt in bp.CONTAINER_BITS else 0.0
                if not self._paused and self._active:
                    while filled < n and self._chunks:
                        item = self._chunks[0]
                        gen, seg, block, off, eq_on = item
                        if gen != self._gen:
                            self._chunks.popleft()
                            continue
                        if seg.start is None:
                            seg.start = self._played + filled
                        take = min(n - filled, len(block) - off)
                        if eq_on and top:
                            if out.dtype != np.float64:
                                out = out.astype(np.float64)
                            out[filled:filled + take] = block[off:off + take] + self._tpdf(take, top)
                        else:
                            out[filled:filled + take] = block[off:off + take]
                        filled += take
                        if off + take >= len(block):
                            self._chunks.popleft()
                        else:
                            item[3] = off + take
                    self._buffered -= filled
                self._blk_time, self._blk_start, self._blk_len = now, self._played, filled
                self._played += filled
                gain = self._gain
                self._cond.notify_all()
            if filled < n:
                out = out[:filled] if not self.output.realtime else out
            if gain != 1.0:
                out *= out.dtype.type(gain)
        except Exception:  # noqa: BLE001 - the audio thread must never die
            return np.zeros((n, CHANNELS), np.float32)
        return out

    def _ready(self, n: int, timeout: float) -> None:
        """ManualOutput: wait until n frames are buffered (or nothing more is coming)."""
        end = time.monotonic() + timeout
        with self._lock:
            while self._buffered < n and not self._eos and self._draining != self._gen and self._active:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 0.05))

    def _alive(self, gen: int) -> bool:
        return gen == self._gen

    def _close_output(self) -> None:
        with self._out_lock:
            self.output.close()
            with self._lock:
                self._out_device = None
                self._out_rate = 0
                self._out_plan = None

    def _plan_output(self, device: str, info: SongInfo) -> _OutPlan:
        """How the device is opened for `info`: the normal way (the song's rate when the
        device runs at it, float32), or, with bit-perfect playback, as `bitperfect.plan()`
        decides from what the device offers for this app alone."""
        with self._lock:
            on = self._bit_perfect
        if not on:
            return _OutPlan(self.output.pick_rate(device, info.rate))
        song = info.song()
        ex = self.output.exclusive(device)
        if ex.error:
            return self._shared_plan(device, song, ex.error, ex)
        p = bp.plan(song, ex.modes)
        if ex.note and p.reasons:
            p.reasons.append(ex.note)
        return _OutPlan(p.rate, p.format, p.channels, ex, p, song, ex.access, ex.label)

    def _shared_plan(self, device: str, song: bp.Song, why: str, ex: Optional[Exclusive] = None) -> _OutPlan:
        """Bit-perfect playback could not have the device: the normal path (no EQ, gain 1)."""
        rate = self.output.pick_rate(device, song.rate)
        p = bp.plan(song, [bp.Mode(bp.F32, rate, rate, CHANNELS)], failure=why)
        return _OutPlan(rate, bp.F32, CHANNELS, None, p, song, ex.access if ex else bp.DIRECT, ex.label if ex else "")

    def _open(self, device: str, out: _OutPlan) -> _OutPlan:
        """Open the device as `out` says (with _out_lock held); when bit-perfect access
        fails, the normal path instead.  Returns what was opened.  Raises PlayerError."""
        try:
            self.output.open(device, out.rate, self._pull, self._ready, fmt=out.fmt, channels=out.channels,
                             exclusive=out.ex)
            return out
        except OpenError as exc:
            if out.ex is None or out.song is None:
                raise
            shared = self._shared_plan(device, out.song, exc.reason(), out.ex)
            self.output.open(device, shared.rate, self._pull, self._ready)
            return shared

    def _ensure_output(self, gen: int, out: _OutPlan) -> Optional[_OutPlan]:
        """Open the device for `out` unless it already runs that way.  Returns the plan
        the device runs with (None if this generation is no longer wanted).  Raises
        PlayerError."""
        with self._out_lock:
            with self._lock:
                if not self._alive(gen):
                    return None
                cur = self._out_plan
                same = self._out_device == self.device and cur is not None and cur.key() == out.key()
                device = self.device
            if same:
                return out
            self.output.close()
            with self._lock:
                self._out_device, self._out_rate, self._out_plan = None, 0, None
            used = self._open(device, out)
            with self._lock:
                self._out_device, self._out_rate, self._out_plan = device, used.rate, used
                self._played, self._blk_start, self._blk_len = 0, 0, 0
                self._blk_time = time.monotonic()
                for seg in self._segs:
                    seg.start = None
            return used

    def _post_error(self, gen: int, path: str, message: str) -> None:
        with self._lock:
            if self._alive(gen):
                self._error = message
                self._events.append(("error", path, message))
                self._active = False
                self._paused = True
                self._segs.clear()
                self._segs.append(_Segment(path, SongInfo(path, 0), 0.0, 0))

    def _push(self, gen: int, seg: _Segment, block: np.ndarray, eq: bool = False) -> bool:
        """Queue decoded frames, waiting while the buffer is full.  False: stop decoding.
        `eq`: the equalizer changed them (they are dithered for an integer device)."""
        limit = int(BUFFER_SECONDS * max(seg.rate, 8000))
        with self._lock:
            while self._alive(gen) and self._buffered >= limit:
                self._cond.wait(0.05)
            if not self._alive(gen):
                return False
            if len(block):
                self._chunks.append([gen, seg, block, 0, eq])
                self._buffered += len(block)
                self._cond.notify_all()
            return True

    def _wait_heard(self, gen: int) -> bool:
        """Wait until everything queued has been heard.  False if the generation changed."""
        with self._lock:
            self._draining = gen
            self._cond.notify_all()
        try:
            while True:
                with self._lock:
                    if not self._alive(gen):
                        return False
                    if not self._chunks and self._heard() >= self._played:
                        return True
                    self._cond.wait(0.02)
        finally:
            with self._lock:
                if self._draining == gen:
                    self._draining = 0

    def _drop_prefetch(self) -> None:
        pf, self._prefetch = self._prefetch, None
        if pf is not None and pf.decoder is not None:
            pf.decoder.close()

    def _start_prefetch(self, gen: int, path: str) -> None:
        """Probe the next song and start its FFmpeg, so it begins without a gap."""
        pf = _Prefetch(path)
        with self._lock:
            self._drop_prefetch()
            self._prefetch = pf
            eq_state, device = self._eq_used(), self.device

        def work():
            try:
                pf.info = song_info(path)
                pf.out = self._plan_output(device, pf.info)
                pf.chain = filter_chain(pf.info, pf.out.rate, eq_state)
                with self._lock:
                    wanted = self._alive(gen) and self._prefetch is pf
                if wanted:
                    pf.decoder = _Decoder(pf.info, 0.0, pf.out.rate, pf.chain, native=pf.out.native,
                                          eq=eq_changes(eq_state, pf.out.rate))
            except PlayerError as exc:
                pf.error = str(exc)
            except OSError as exc:
                pf.error = f"FFmpeg could not be started: {exc}."
            finally:
                pf.done.set()

        threading.Thread(target=work, daemon=True, name="realhires-prefetch").start()

    def _take_next(self, gen: int, path: str) -> Optional[_Prefetch]:
        """At the end of `path`: the next song, ready to decode (None: stop here)."""
        with self._lock:
            nxt = self._next_for(path)
            if nxt:
                self._next_path = self._next_after = None  # used once
            pf = self._prefetch
            eq_state, device = self._eq_used(), self.device
        if not nxt:
            return None
        if pf is None or pf.path != nxt:
            self._start_prefetch(gen, nxt)
            with self._lock:
                pf = self._prefetch
        if pf is None:
            return None
        pf.done.wait(30)
        with self._lock:
            if not self._alive(gen) or self._prefetch is not pf:
                return None
            self._prefetch = None
        if pf.error or pf.info is None:
            if pf.decoder is not None:
                pf.decoder.close()
            return None
        out = self._plan_output(device, pf.info)
        chain = filter_chain(pf.info, out.rate, eq_state)
        if (pf.decoder is None or chain != pf.chain or pf.out is None or out.key() != pf.out.key()
                or out.native != pf.out.native):
            if pf.decoder is not None:
                pf.decoder.close()
            pf.out, pf.chain = out, chain
            pf.decoder = _Decoder(pf.info, 0.0, out.rate, chain, native=out.native, eq=eq_changes(eq_state, out.rate))
        return pf

    def _run(self, gen: int, path: str, start: float, info: Optional[SongInfo]) -> None:
        """The decoder thread of one generation: this song, then the next ones while
        the queue continues gaplessly."""
        decoder: Optional[_Decoder] = None
        try:
            try:
                info = info if info is not None and info.rate else song_info(path)
                with self._lock:
                    device, eq_state = self.device, self._eq_used()
                out = self._ensure_output(gen, self._plan_output(device, info))
                if out is None:
                    return
                rate = out.rate
            except PlayerError as exc:
                self._post_error(gen, path, str(exc))
                return
            with self._lock:
                if not self._alive(gen):
                    return
                seg = self._segs[0]
                seg.info, seg.rate, seg.out = info, rate, out
                chain = filter_chain(info, rate, eq_state)
            try:
                decoder = _Decoder(info, start, rate, chain, native=out.native, eq=eq_changes(eq_state, rate))
            except OSError as exc:
                self._post_error(gen, path, f"FFmpeg could not be started: {exc}.")
                return
            while True:
                prefetched = False
                while True:
                    block = decoder.read()
                    if block is None:
                        break
                    if not self._push(gen, seg, block, decoder.eq):
                        return
                    if not prefetched and self.gapless:
                        left = (info.duration - decoder.decoded_seconds) if info.duration else 0.0
                        if left <= PREFETCH_SECONDS:
                            with self._lock:
                                nxt = self._next_for(seg.path)
                            if nxt:
                                self._start_prefetch(gen, nxt)
                            prefetched = True
                err = decoder.error()
                got = decoder.frames
                retry = (decoder.info, decoder.start, decoder.rate, decoder.chain)
                retry = retry if not got and decoder.start > 0 and not decoder.slow_seek else None
                native, eq_on = decoder.native, decoder.eq
                decoder.close()
                decoder = None
                if retry:  # FFmpeg could not seek in this file: decode from the start instead
                    try:
                        decoder = _Decoder(*retry, slow_seek=True, native=native, eq=eq_on)
                    except OSError as exc:
                        self._post_error(gen, seg.path, f"FFmpeg could not be started: {exc}.")
                        return
                    continue
                if err and not got:
                    msg = f"{os.path.basename(seg.path)} could not be played: {err.rstrip('.')}."
                    if not self._wait_heard(gen):
                        return
                    with self._lock:  # a queued song failed: the queue has moved on to it
                        if self._alive(gen) and self._segs and self._segs[0] is not seg:
                            self._events.append(("advanced", seg.path))
                    self._post_error(gen, seg.path, msg)
                    return
                pf = self._take_next(gen, seg.path) if self.gapless else None
                if pf is None:
                    with self._lock:
                        if not self._alive(gen):
                            return
                        self._eos = True
                        self._cond.notify_all()
                    if not self._wait_heard(gen):
                        return
                    with self._lock:
                        if self._alive(gen):
                            self._events.append(("finished", seg.path))
                            self._active = False
                            self._paused = True
                    return
                nout: _OutPlan = pf.out  # type: ignore[assignment]
                nseg = _Segment(pf.path, pf.info, 0.0, nout.rate, out=nout)  # type: ignore[arg-type]
                if nout.key() != out.key():
                    # the next song needs another rate or format: let this one be heard to the end, then reopen
                    if not self._wait_heard(gen):
                        pf.decoder.close()  # type: ignore[union-attr]
                        return
                    with self._lock:
                        if not self._alive(gen):
                            pf.decoder.close()  # type: ignore[union-attr]
                            return
                        self._segs.append(nseg)
                    try:
                        with self._out_lock:
                            self.output.close()
                            used = self._open(self.device, nout)
                            with self._lock:
                                self._out_device, self._out_rate, self._out_plan = self.device, used.rate, used
                                self._played, self._blk_start, self._blk_len = 0, 0, 0
                                self._blk_time = time.monotonic()
                                nseg.out, nseg.rate = used, used.rate
                    except PlayerError as exc:
                        pf.decoder.close()  # type: ignore[union-attr]
                        with self._lock:
                            if self._alive(gen):
                                self._segs.popleft()
                        self._post_error(gen, pf.path, str(exc))
                        return
                    if used.rate != nout.rate or used.native != nout.native:  # fell back to the normal path
                        pf.decoder.close()  # type: ignore[union-attr]
                        try:
                            pf.decoder = _Decoder(pf.info, 0.0, used.rate,  # type: ignore[arg-type]
                                                  filter_chain(pf.info, used.rate, self._eq_used()),  # type: ignore
                                                  native=used.native, eq=eq_changes(self._eq_used(), used.rate))
                        except OSError as exc:
                            self._post_error(gen, pf.path, f"FFmpeg could not be started: {exc}.")
                            return
                    out, rate = used, used.rate
                else:
                    with self._lock:
                        if not self._alive(gen):
                            pf.decoder.close()  # type: ignore[union-attr]
                            return
                        self._segs.append(nseg)
                seg, info, decoder = nseg, pf.info, pf.decoder  # type: ignore[assignment]
        finally:
            if decoder is not None:
                decoder.close()


# ----------------------------------------------------- texts for the window ---
def fmt_khz(hz: int) -> str:
    """96000 -> '96 kHz', 44100 -> '44.1 kHz'."""
    return f"{hz / 1000:g} kHz" if hz else "—"


def fmt_gain(gain: float, muted: bool = False) -> str:
    """The volume as the Playback info shows it: '−6.0 dB' or 'Muted'."""
    if muted or gain <= 0:
        return "Muted"
    db = 20.0 * math.log10(gain)
    return ("−" if db < -0.05 else "") + f"{abs(db):.1f} dB"


def fmt_db(db: float) -> str:
    return ("−" if db < -0.005 else "+" if db > 0.005 else "") + f"{abs(db):.1f} dB"


def channels_label(n: int) -> str:
    return {1: "Mono", 2: "Stereo"}.get(n, f"{n} channels" if n else "—")


def bit_depth_label(bits: int, is_float: bool, is_lossy: bool) -> str:
    if is_lossy or not bits:
        return "—"
    return f"{bits}-bit float" if is_float else f"{bits}-bit"


def bit_rate_label(bps: int) -> str:
    return f"{round(bps / 1000):,} kbps" if bps else "—"


def eq_summary(state: dict) -> str:
    """'Off', 'Graphic: Rock', 'Parametric: 3 bands', ' (pre-amp −4.5 dB)' when not zero."""
    s = eq_mod.normalize(state)
    if not s["enabled"]:
        return "Off"
    parts = []
    if s["graphic"]["enabled"]:
        parts.append(f"Graphic: {s['graphic']['preset']}")
    if s["parametric"]["enabled"]:
        n = sum(1 for b in s["parametric"]["bands"] if b["on"])
        parts.append(f"Parametric: {n} band{'s' if n != 1 else ''}")
    if not parts:
        return "Off"
    text = ", ".join(parts[:1] + [p[0].lower() + p[1:] for p in parts[1:]])
    pre = eq_mod.preamp_db(s)
    return text + (f" (pre-amp {fmt_db(pre)})" if abs(pre) >= 0.05 else "")


def resampling_label(song_rate: int, out_rate: int, resampler: str) -> str:
    if not song_rate or not out_rate:
        return "—"
    if song_rate == out_rate:
        return "None: the device runs at the song's rate"
    return f"From {fmt_khz(song_rate)} to {fmt_khz(out_rate)} ({resampler})"


def output_label(rate: int, channels: int = CHANNELS, fmt_label: str = SAMPLE_FORMAT_LABEL) -> str:
    """'96 kHz, 32-bit float, stereo'; `fmt_label` is `output_info()["format"]` (with bit-perfect
    playback what the device really gets, '24-bit integer in 32-bit')."""
    return f"{fmt_khz(rate)}, {fmt_label}, {channels_label(channels).lower()}" if rate else "Not playing"


__all__ = ["Player", "PlayerError", "SongInfo", "song_info", "Output", "MiniaudioOutput", "ManualOutput",
           "DeviceInfo", "list_devices", "default_output", "has_soxr", "ffmpeg_version", "resampler_name",
           "filter_chain", "decode_command", "eq_summary", "fmt_gain", "fmt_khz", "fmt_db", "channels_label",
           "bit_depth_label", "bit_rate_label", "resampling_label", "output_label", "Exclusive", "OpenError",
           "device_samples", "device_bytes", "eq_changes"]
