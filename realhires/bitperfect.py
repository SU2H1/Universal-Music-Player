"""Bit-perfect playback (2.1): the decisions and the texts, shared by both players.

With the setting ``bit_perfect`` on, the player sends each song's samples to the
sound device as they are in the file: at the song's own sample rate, without the
volume or resampling, and with the device to itself where the system allows it
(WASAPI exclusive mode on Windows, the ALSA hardware device on Linux; on macOS
the device is switched to the song's rate).  Since 2.3 the equalizer is applied
when it is on (in 64-bit float, with TPDF dither of ±1 LSB when the device takes
integers); everything else stays, and `status(..., eq_on=True)` says so.

This module holds no sound code: the engines (``realhires/player.py`` and
``rust/crates/gui/src/audio.rs``) ask the device what it offers (`Mode`s), let
`plan()` choose the output rate and sample format for a song, and show what
`status()` and `output_label()` say in the Playback info.  The Rust core module
``bitperfect.rs`` gives the same answers (``rust/tools/gen_player_vectors.py``
writes Python's into ``python_bitperfect.json``).

* Output format: 16-bit songs I16, I32, I24, F32; 24-bit songs I32, I24, F32;
  32-bit integer songs I32, F32, I24; float and lossy songs F32, I32, I24, I16
  (the first one the device has at the chosen rate).  The engines decode to
  32-bit float, which holds integers of up to 24 bits exactly, and turn floats
  into integers as round(x * 2^(bits - 1)), clamped (`int_sample()`), so the
  device gets the file's own integers.
* The rate is the song's when the device offers it; otherwise the next higher
  rate (a whole multiple of the song's when there is one), else the highest
  lower rate (one the song's rate is a multiple of, when there is one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# sample formats the engines can write
I16, I24, I32, F32 = "i16", "i24", "i32", "f32"
FORMATS = (I16, I24, I32, F32)
CONTAINER_BITS = {I16: 16, I24: 24, I32: 32}

# how the device was reached
WASAPI = "wasapi"        # Windows: WASAPI exclusive mode
ALSA = "alsa"            # Linux: the ALSA hardware device (hw:N)
COREAUDIO = "coreaudio"  # macOS: the device's nominal sample rate set to the song's
DIRECT = "direct"        # the test and null outputs (no system mixer in between)

OFF = "Off"
ON_IDLE = "On"
BUSY = "The device is in use by another program, so the sound is mixed by the system."
LOSSY_NOTE = "The song is decoded from a lossy format, so it is played as decoded."
# 2.3: the equalizer works with bit-perfect playback (it was bypassed before)
EQ_ON = "The equalizer is on, so the samples are changed."
EQ_NOTE = ("Bit-perfect playback is on: with the equalizer on, the sound is not bit-perfect. "
           "The device still gets the song's own sample rate.")
# the Python app on Windows: miniaudio opens a WASAPI exclusive stream only in the device's Default Format
WASAPI_FORMAT_NOTE = ("Windows gives the device to this app alone only in the format chosen in its Sound "
                      "settings (the device's Properties, Advanced, Default Format).")

# the rates a device with a range of rates is tried at, as miniaudio and cpal list them
STANDARD_RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 176400, 192000,
                  352800, 384000, 705600, 768000)


@dataclass(frozen=True)
class Song:
    """What the decision needs to know about a song."""
    rate: int
    channels: int = 2
    bits: int = 0          # bits per sample of the file (0: unknown)
    is_float: bool = False
    is_lossy: bool = False


@dataclass(frozen=True)
class Mode:
    """One thing the device offers: a sample format at rates `min_rate`..`max_rate`
    (equal for a single rate) with `channels` channels (0: any number)."""
    format: str
    min_rate: int
    max_rate: int
    channels: int = 2


@dataclass
class Plan:
    """What the device is opened with for a song, and why it is not bit-perfect."""
    rate: int
    format: str
    channels: int = 2                                   # the device's channels (2 = stereo)
    reasons: List[str] = field(default_factory=list)    # full sentences; any makes it "No"

    @property
    def resampled(self) -> bool:
        return any(r.startswith("The device cannot play ") for r in self.reasons)


# ------------------------------------------------------------------- texts ---
def fmt_khz(hz: int) -> str:
    """96000 -> '96 kHz', 44100 -> '44.1 kHz' (as the Playback info writes rates)."""
    return f"{hz / 1000:g} kHz" if hz else "—"


def _ascii_lower(text: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


def song_bits(song: Song) -> int:
    """The integer bits the decision works with: 0 for float and lossy songs, 24 when unknown."""
    if song.is_float or song.is_lossy:
        return 0
    return song.bits if song.bits > 0 else 24


def format_label(fmt: str, bits: int = 0) -> str:
    """'32-bit float', '16-bit integer', '24-bit integer in 32-bit' (`bits`: the song's
    integer bits, 0 for float and lossy songs)."""
    if fmt == F32:
        return "32-bit float"
    size = CONTAINER_BITS.get(fmt, 32)
    if 0 < bits < size:
        return f"{bits}-bit integer in {size}-bit"
    return f"{size}-bit integer"


def channels_label(n: int) -> str:
    return {1: "mono", 2: "stereo"}.get(n, f"{n} channels")


def output_label(rate: int, fmt: str, bits: int = 0, channels: int = 2) -> str:
    """The Playback info's Output line: '96 kHz, 24-bit integer in 32-bit, stereo'."""
    if not rate:
        return "Not playing"
    return f"{fmt_khz(rate)}, {format_label(fmt, bits)}, {channels_label(channels)}"


def access_label(access: str, rate: int, device: str = "") -> str:
    if access == WASAPI:
        return "exclusive mode (WASAPI)"
    if access == ALSA:
        return f"ALSA hardware device {device}".rstrip()
    if access == COREAUDIO:
        return f"the device runs at {fmt_khz(rate)} (CoreAudio)"
    return device or "the output device"


def failure_reason(detail: str = "") -> str:
    """Why the device could not be had for this app alone (it then plays mixed)."""
    detail = detail.strip().rstrip(".")
    if not detail:
        return "The device could not be opened for this app alone, so the sound is mixed by the system."
    return f"The device could not be opened for this app alone ({detail}), so the sound is mixed by the system."


def rate_reason(song_rate: int, out_rate: int) -> str:
    return f"The device cannot play {fmt_khz(song_rate)}, so the song is resampled to {fmt_khz(out_rate)}."


def still_runs(rate: int, access: str, device: str = "") -> str:
    """What bit-perfect playback still does while the equalizer changes the samples: the
    device at the plan's rate, reached as `access` says."""
    khz = fmt_khz(rate)
    if access == WASAPI:
        return f"The device still runs at {khz} in exclusive mode."
    if access == ALSA:
        return f"The ALSA hardware device {device} still runs at {khz}." if device.strip() else \
            f"The ALSA hardware device still runs at {khz}."
    if access == COREAUDIO:
        return f"The device still runs at {khz} (CoreAudio)."
    return f"The device still runs at {khz}."


def status(song: Optional[Song], plan: Optional[Plan], access: str = DIRECT, device: str = "",
           enabled: bool = True, eq_on: bool = False) -> str:
    """The Playback info's "Bit-perfect" line: 'Off', 'Yes: 24-bit, 96 kHz, exclusive mode
    (WASAPI)', 'Yes (decoded lossy audio): …', or 'No: ' and every reason that applies.
    `eq_on`: the equalizer changes the song's samples (2.3); when that is the only reason,
    the line also says what the device still does ('… The device still runs at 96 kHz in
    exclusive mode.')."""
    if not enabled:
        return OFF
    if song is None or plan is None:
        return ON_IDLE
    if plan.reasons or eq_on:
        reasons = list(plan.reasons)
        if eq_on:
            reasons.append(EQ_ON)
            if not plan.reasons:
                reasons.append(still_runs(plan.rate, access, device))
        reasons += [LOSSY_NOTE] if song.is_lossy else []
        return "No: " + " ".join(reasons)
    bits = song_bits(song)
    if bits:
        what = f"{song.bits}-bit" if song.bits > 0 else format_label(plan.format)
    else:
        what = "32-bit float"
    head = "Yes (decoded lossy audio)" if song.is_lossy else "Yes"
    return f"{head}: {what}, {fmt_khz(plan.rate)}, {access_label(access, plan.rate, device)}"


# --------------------------------------------------------------- decisions ---
def preferred_formats(song: Song) -> List[str]:
    """The formats a song is best played in, best first, then the others."""
    bits = song_bits(song)
    if not bits:
        pref = [F32, I32, I24, I16]
    elif bits <= 16:
        pref = [I16, I32, I24, F32]
    elif bits <= 24:
        pref = [I32, I24, F32]
    else:
        pref = [I32, F32, I24]
    return pref + [f for f in (I32, I24, F32, I16) if f not in pref]


def usable(modes: Sequence[Mode]) -> List[Mode]:
    """The modes the player can use: known formats, at least two channels (or any)."""
    return [m for m in modes if m.format in FORMATS and m.channels != 1 and m.max_rate >= m.min_rate > 0]


def rates_offered(modes: Sequence[Mode]) -> List[int]:
    """Every rate the modes offer (a range counts with its standard rates and its ends)."""
    out = set()
    for m in usable(modes):
        if m.min_rate == m.max_rate:
            out.add(m.min_rate)
            continue
        out.update(r for r in STANDARD_RATES if m.min_rate <= r <= m.max_rate)
        out.update((m.min_rate, m.max_rate))
    return sorted(out)


def pick_rate(song_rate: int, rates: Sequence[int]) -> int:
    """The song's rate when offered, else the next higher one (a whole multiple of the
    song's when there is one), else the highest lower one (preferring rates the song's
    rate is a multiple of)."""
    if not rates or song_rate in rates or song_rate <= 0:
        return song_rate if song_rate > 0 or not rates else max(rates)
    higher = [r for r in rates if r > song_rate]
    if higher:
        multiples = [r for r in higher if r % song_rate == 0]
        return min(multiples or higher)
    lower = [r for r in rates if r < song_rate]
    family = [r for r in lower if song_rate % r == 0]
    return max(family or lower)


def plan(song: Song, modes: Sequence[Mode], failure: str = "") -> Plan:
    """The rate, format and channels for `song` on a device that offers `modes` ([] =
    anything), with the reasons it is not bit-perfect.  `failure`: why the device could
    not be had alone (the song then plays through the system's mixer, as `modes` say)."""
    good = usable(modes)
    prefs = preferred_formats(song)
    if not good:
        rate, fmt, channels = song.rate, prefs[0], 2
    else:
        rate = pick_rate(song.rate, rates_offered(good))
        here = [m for m in good if m.min_rate <= rate <= m.max_rate]
        offered = {m.format for m in here}
        fmt = next((f for f in prefs if f in offered), prefs[0])
        counts = [m.channels for m in here if m.format == fmt]
        channels = 2 if not counts or any(c in (0, 2) for c in counts) else min(counts)
    reasons: List[str] = []
    if song.rate > 0 and rate != song.rate:
        reasons.append(rate_reason(song.rate, rate))
    if failure:
        reasons.append(failure)
    bits = song_bits(song)
    if bits:
        if fmt == I16 and bits > 16:
            reasons.append(f"The device has no {bits}-bit format, so the samples are converted.")
        elif bits > 24:
            reasons.append(f"The {bits}-bit samples are played with 24-bit precision.")
    else:
        if fmt != F32:
            reasons.append("The device has no 32-bit float format, so the samples are converted.")
        elif song.is_float and song.bits > 32:
            reasons.append(f"The {song.bits}-bit float samples are played as 32-bit float.")
    if song.channels > 2:
        reasons.append(f"This {song.channels}-channel song is mixed down to stereo.")
    return Plan(rate, fmt, channels, reasons)


def dithered_int_sample(x: float, fmt: str, d: float) -> int:
    """A float sample changed by the equalizer as the device's integer: round(x * 2^(bits - 1)
    + d), clamped, where `d` is the TPDF dither in LSB (the sum of two uniform numbers in
    [-0.5, 0.5), so within ±1 LSB).  The engines add it only to samples the equalizer
    changed, so with the equalizer off the output stays bit-exact (`int_sample()`)."""
    top = 1 << (CONTAINER_BITS[fmt] - 1)
    v = float(x) * top
    if v != v:  # NaN
        return 0
    return int(round(max(-float(top), min(float(top - 1), v + d))))


def int_sample(x: float, fmt: str) -> int:
    """A float sample as the device's integer: round(x * 2^(bits - 1)), clamped (a float
    of a file's integer of up to 24 bits gives that integer back, shifted left)."""
    top = 1 << (CONTAINER_BITS[fmt] - 1)
    v = float(x) * top
    if v != v:  # NaN
        return 0
    return int(round(max(-float(top), min(float(top - 1), v))))  # round() rounds halves to even


# -------------------------------------------------------------------- ALSA ---
def alsa_cards(text: str) -> List[Tuple[int, str, str]]:
    """The sound cards in /proc/asound/cards: (index, id, name), for example
    ' 0 [PCH            ]: HDA-Intel - HDA Intel PCH' gives (0, 'PCH', 'HDA Intel PCH')."""
    out = []
    for line in text.splitlines():
        s = line.lstrip(" \t")
        digits = ""
        while s and "0" <= s[0] <= "9":
            digits, s = digits + s[0], s[1:]
        s = s.lstrip(" \t")
        if not digits or not s.startswith("["):
            continue
        end = s.find("]")
        if end < 0:
            continue
        card_id, rest = s[1:end].strip(), s[end + 1:]
        if not rest.startswith(":"):
            continue
        rest = rest[1:].strip()
        sep = rest.find(" - ")
        name = rest[sep + 3:].strip() if sep >= 0 else rest
        out.append((int(digits), card_id, name))
    return out


def alsa_device(device: str, cards: Sequence[Tuple[int, str, str]]) -> str:
    """The ALSA hardware device ('hw:1', 'hw:0,3') for the chosen output device: its card
    and device when the name is an ALSA one ('hw:CARD=PCH,DEV=3', 'plughw:1,0',
    'front:CARD=Audio,DEV=0'), else the card whose name the device's name contains (the
    longest such), else card 0."""
    d = device.strip()
    card: Optional[int] = None
    dev = 0
    if ":" in d and " " not in d.split(":", 1)[0]:
        for i, part in enumerate(d.split(":", 1)[1].split(",")):
            key, eq, value = part.partition("=")
            key, value = key.strip(), value.strip()
            if not eq:
                key, value = ("CARD" if i == 0 else "DEV" if i == 1 else ""), key
            key = key.upper()
            if key == "CARD":
                if value.isdigit() and value.isascii():
                    card = int(value)
                else:
                    card = next((n for n, cid, _ in cards if _ascii_lower(cid) == _ascii_lower(value)), card)
            elif key == "DEV" and value.isdigit() and value.isascii():
                dev = int(value)
    if card is None:
        low = _ascii_lower(d)
        best = -1
        for n, _, name in cards:
            nm = _ascii_lower(name.strip())
            if len(nm) >= 3 and nm in low and len(nm) > best:
                card, best = n, len(nm)
    if card is None:
        card = 0
    return f"hw:{card}" if dev == 0 else f"hw:{card},{dev}"


__all__ = ["I16", "I24", "I32", "F32", "FORMATS", "WASAPI", "ALSA", "COREAUDIO", "DIRECT", "OFF", "ON_IDLE",
           "BUSY", "LOSSY_NOTE", "EQ_ON", "EQ_NOTE", "WASAPI_FORMAT_NOTE", "STANDARD_RATES", "Song", "Mode", "Plan", "fmt_khz",
           "format_label", "output_label", "access_label", "failure_reason", "rate_reason", "status", "still_runs", "dithered_int_sample",
           "preferred_formats", "usable", "rates_offered", "pick_rate", "plan", "int_sample", "song_bits",
           "alsa_cards", "alsa_device", "channels_label"]
