"""High level driver for the RIGOL DS1000Z series (DS1054Z / DS1074Z / DS1104Z ...).

Speed strategy (all numbers measured over a 100 Mbit USB-LAN adapter):

* Set commands cost ~50 ms each inside the instrument, so the ``:WAV:*`` state
  is cached and only changed when needed.  Reading the same channel again costs
  no set command at all; switching channel costs one (~50 ms).
* Queries are bundled with ``;`` so status, preamble checks and up to dozens of
  values come back in a single ~1-3 ms round trip.
* A screen waveform (1200 points, BYTE) is ``:WAV:SOUR?;:WAV:PRE?;:WAV:DATA?`` in
  one round trip (~3-4 ms).  The scope itself only refreshes the screen buffer
  ~10-20 times per second, so the streaming helpers can drop duplicate frames.
* Scope measurements are fast (~1-3 ms) only while at most five
  (item, source) pairs are in use; every additional pair costs ~400 ms.  Host
  side analysis (``Waveform.analyze``) has no such limit.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Sequence

import numpy as np

from .transport import DEFAULT_PORT, MEASURE_ERROR, ScpiError, ScpiTransport
from .waveform import Preamble, Waveform

CHANNELS = (1, 2, 3, 4)
SCREEN_POINTS = 1200
# The programming guide limits one BYTE read to 250k points, but 1M point
# chunks work on FW 00.04.04.SP3 and are ~2x faster (fewer 50 ms set commands).
# Anything above 1,000,000 returns an empty block.
RAW_CHUNK = 1_000_000
INVALID = 9.9e37

MEASURE_ITEMS = (
    "VMAX", "VMIN", "VPP", "VTOP", "VBAS", "VAMP", "VAVG", "VRMS", "OVER", "PRES",
    "MAR", "MPAR", "PER", "FREQ", "RTIM", "FTIM", "PWID", "NWID", "PDUT", "NDUT",
    "TVMAX", "TVMIN", "PSLEWRATE", "NSLEWRATE", "VUPPER", "VMID", "VLOWER",
    "VARIANCE", "PVRMS", "PPULSES", "NPULSES", "PEDGES", "NEDGES",
    # Two-source items: pass "CHAN1,CHAN2" as the source.
    "RDEL", "FDEL", "RPH", "FPH",
)
COUPLINGS = ("AC", "DC", "GND")
PROBE_RATIOS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
ACQ_TYPES = ("NORM", "AVER", "PEAK", "HRES")
TRIG_SWEEPS = ("AUTO", "NORM", "SING")
TRIG_SLOPES = ("POS", "NEG", "RFAL")
TRIG_COUPLINGS = ("AC", "DC", "LFR", "HFR")
WAVE_MODES = ("NORM", "MAX", "RAW")
SCREENSHOT_FORMATS = {"PNG": "image/png", "BMP24": "image/bmp", "BMP8": "image/bmp",
                      "JPEG": "image/jpeg", "TIFF": "image/tiff"}


class ChannelOff(ScpiError):
    """Requested channel is not displayed, so it has no waveform."""


def source_name(ch: int | str) -> str:
    if isinstance(ch, int) or str(ch).isdigit():
        n = int(ch)
        if n not in CHANNELS:
            raise ValueError(f"channel must be 1-4, got {ch}")
        return f"CHAN{n}"
    s = str(ch).upper()
    if s.startswith("CH") and s[-1].isdigit() and int(s[-1]) in CHANNELS:
        return f"CHAN{s[-1]}"
    if s in ("MATH",) or (s.startswith("D") and s[1:].isdigit() and 0 <= int(s[1:]) <= 15):
        return s
    raise ValueError(f"unknown source {ch!r}")


def _num(text: str) -> float | None:
    """Parse a numeric reply; invalid/overflow (9.9E37) and errors map to None."""
    t = text.strip()
    if not t or t.startswith(MEASURE_ERROR[:7]):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if not math.isfinite(v) or abs(v) >= INVALID:
        return None
    return v


def _bool(text: str) -> bool:
    return text.strip() in ("1", "ON")


def _fmt(v: float) -> str:
    return f"{v:.6e}"


@dataclass
class _WaveState:
    source: str | None = None
    mode: str | None = None
    form: str | None = None
    start: int | None = None
    stop: int | None = None


class DS1000Z:
    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0):
        self.t = ScpiTransport(host, port, timeout)
        self.t.on_reconnect = self.invalidate
        self._wav = _WaveState()
        self._disp: tuple[float, list[int]] | None = None  # (monotonic time, displayed channels)
        self._idn: str | None = None

    # ------------------------------------------------------------ connection
    @property
    def host(self) -> str:
        return self.t.host

    @property
    def lock(self):
        return self.t.lock

    def connect(self) -> "DS1000Z":
        self.t.connect()
        self.invalidate()
        return self

    def close(self) -> None:
        self.t.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def invalidate(self) -> None:
        """Forget cached instrument state (after reconnect or raw SCPI)."""
        self._wav = _WaveState()
        self._disp = None

    # ------------------------------------------------------------ raw access
    def write(self, *commands: str) -> None:
        self.t.write(*commands)

    def query(self, command: str, timeout: float | None = None) -> str:
        return self.t.query(command, timeout)

    def scpi(self, command: str, timeout: float | None = None) -> str | bytes | None:
        """Pass-through: queries return text (or bytes for block data)."""
        cmd = command.strip()
        with self.lock:
            if "?" not in cmd:
                if ":WAV" in cmd.upper():
                    self.invalidate()
                self.t.write(cmd)
                return None
            return self.t.query_any(cmd, timeout or 20)

    def idn(self) -> dict:
        if self._idn is None:
            self._idn = self.t.query("*IDN?")
        maker, model, serial, fw = (self._idn.split(",") + ["", "", "", ""])[:4]
        return {"manufacturer": maker, "model": model, "serial": serial, "firmware": fw}

    def error(self) -> str:
        return self.t.query(":SYST:ERR?")

    def errors(self, limit: int = 20) -> list[str]:
        out = []
        for _ in range(limit):
            e = self.error()
            if e.startswith("0,"):
                break
            out.append(e)
        return out

    # -------------------------------------------------------------- control
    def run(self, wait: bool = True) -> None:
        """:TRIG:STAT? keeps answering STOP for a while after :RUN, so by
        default wait for the transition to make later state checks reliable."""
        with self.lock:
            self.t.write(":RUN")
            if wait:
                deadline = time.monotonic() + 1.0
                while self.trigger_status() == "STOP" and time.monotonic() < deadline:
                    time.sleep(0.005)

    def stop(self, wait: bool = True) -> None:
        with self.lock:
            self.t.write(":STOP")
            if wait:
                self.wait_trigger_status(["STOP"], timeout=1.0)

    def single(self) -> None:
        self.t.write(":SING")

    def force_trigger(self) -> None:
        self.t.write(":TFOR")

    def clear(self) -> None:
        self.t.write(":CLE")

    def autoscale(self) -> None:
        with self.lock:
            self.t.write(":AUT")
            self.t.query("*OPC?", timeout=15)
            self.invalidate()

    def trigger_status(self) -> str:
        return self.t.query(":TRIG:STAT?")

    def wait_trigger_status(self, states: Iterable[str], timeout: float = 5.0, poll: float = 0.005) -> str:
        want = {s.upper() for s in states}
        deadline = time.monotonic() + timeout
        while True:
            st = self.trigger_status()
            if st in want or time.monotonic() > deadline:
                return st
            time.sleep(poll)

    # --------------------------------------------------------------- status
    _GLOBAL_Q = (
        ("trigger_status", ":TRIG:STAT?", str),
        ("timebase_scale", ":TIM:SCAL?", float),
        ("timebase_offset", ":TIM:OFFS?", float),
        ("timebase_mode", ":TIM:MODE?", str),
        ("memory_depth", ":ACQ:MDEP?", str),
        ("sample_rate", ":ACQ:SRAT?", float),
        ("acquire_type", ":ACQ:TYPE?", str),
        ("averages", ":ACQ:AVER?", int),
        ("trigger_mode", ":TRIG:MODE?", str),
        ("trigger_sweep", ":TRIG:SWE?", str),
        ("trigger_coupling", ":TRIG:COUP?", str),
        ("trigger_holdoff", ":TRIG:HOLD?", float),
        ("trigger_source", ":TRIG:EDG:SOUR?", str),
        ("trigger_slope", ":TRIG:EDG:SLOP?", str),
        ("trigger_level", ":TRIG:EDG:LEV?", float),
    )
    _CHAN_Q = (
        ("display", "DISP?", _bool),
        ("scale", "SCAL?", float),
        ("offset", "OFFS?", float),
        ("coupling", "COUP?", str),
        ("probe", "PROB?", float),
        ("bwlimit", "BWL?", str),
        ("invert", "INV?", _bool),
        ("units", "UNIT?", str),
    )

    def status(self) -> dict:
        """Full front-panel state in two round trips (~5 ms total)."""
        g = self.t.query_many([q for _, q, _ in self._GLOBAL_Q])
        out: dict = {k: conv(v) for (k, _, conv), v in zip(self._GLOBAL_Q, g)}
        try:
            out["memory_depth"] = int(float(out["memory_depth"]))
        except ValueError:
            pass  # "AUTO"
        cq = [f":CHAN{c}:{q}" for c in CHANNELS for _, q, _ in self._CHAN_Q]
        cv = self.t.query_many(cq)
        n = len(self._CHAN_Q)
        out["channels"] = {
            c: {k: conv(cv[i * n + j]) for j, (k, _, conv) in enumerate(self._CHAN_Q)}
            for i, c in enumerate(CHANNELS)
        }
        return out

    def displayed_channels(self) -> list[int]:
        r = self.t.query_many([f":CHAN{c}:DISP?" for c in CHANNELS])
        shown = [c for c, v in zip(CHANNELS, r) if _bool(v)]
        self._disp = (time.monotonic(), shown)
        return shown

    # -------------------------------------------------------------- setters
    def set_channel(
        self,
        ch: int,
        display: bool | None = None,
        scale: float | None = None,
        offset: float | None = None,
        coupling: str | None = None,
        probe: float | None = None,
        bwlimit: str | bool | None = None,
        invert: bool | None = None,
    ) -> None:
        src = source_name(ch)
        cmds = []
        if probe is not None:  # before scale: probe ratio rescales V/div
            if float(probe) not in PROBE_RATIOS:
                raise ValueError(f"probe must be one of {PROBE_RATIOS}")
            cmds.append(f":{src}:PROB {probe:g}")
        if display is not None:
            cmds.append(f":{src}:DISP {'ON' if display else 'OFF'}")
            self._disp = None
        if coupling is not None:
            c = coupling.upper()
            if c not in COUPLINGS:
                raise ValueError(f"coupling must be one of {COUPLINGS}")
            cmds.append(f":{src}:COUP {c}")
        if bwlimit is not None:
            b = "20M" if bwlimit is True or str(bwlimit).upper() in ("20M", "ON") else "OFF"
            cmds.append(f":{src}:BWL {b}")
        if invert is not None:
            cmds.append(f":{src}:INV {'ON' if invert else 'OFF'}")
        if scale is not None:
            if not scale > 0:
                raise ValueError("scale must be > 0")
            cmds.append(f":{src}:SCAL {_fmt(scale)}")
        if offset is not None:
            cmds.append(f":{src}:OFFS {_fmt(offset)}")
        self.t.write(*cmds)

    def set_timebase(self, scale: float | None = None, offset: float | None = None, mode: str | None = None) -> None:
        cmds = []
        if mode is not None:
            m = mode.upper()
            if m not in ("MAIN", "XY", "ROLL"):
                raise ValueError("mode must be MAIN, XY or ROLL")
            cmds.append(f":TIM:MODE {m}")
        if scale is not None:
            if not scale > 0:
                raise ValueError("scale must be > 0")
            cmds.append(f":TIM:SCAL {_fmt(scale)}")
        if offset is not None:
            cmds.append(f":TIM:OFFS {_fmt(offset)}")
        self.t.write(*cmds)

    def set_trigger(
        self,
        source: int | str | None = None,
        level: float | None = None,
        slope: str | None = None,
        sweep: str | None = None,
        coupling: str | None = None,
        holdoff: float | None = None,
    ) -> None:
        """Edge trigger settings."""
        cmds = [":TRIG:MODE EDGE"] if any(v is not None for v in (source, level, slope)) else []
        if source is not None:
            s = str(source).upper()
            cmds.append(f":TRIG:EDG:SOUR {s if s in ('AC', 'EXT') else source_name(source)}")
        if slope is not None:
            s = slope.upper()
            if s not in TRIG_SLOPES:
                raise ValueError(f"slope must be one of {TRIG_SLOPES}")
            cmds.append(f":TRIG:EDG:SLOP {s}")
        if level is not None:
            cmds.append(f":TRIG:EDG:LEV {_fmt(level)}")
        if sweep is not None:
            s = sweep.upper()
            if s not in TRIG_SWEEPS:
                raise ValueError(f"sweep must be one of {TRIG_SWEEPS}")
            cmds.append(f":TRIG:SWE {s}")
        if coupling is not None:
            c = coupling.upper()
            if c not in TRIG_COUPLINGS:
                raise ValueError(f"coupling must be one of {TRIG_COUPLINGS}")
            cmds.append(f":TRIG:COUP {c}")
        if holdoff is not None:
            cmds.append(f":TRIG:HOLD {_fmt(holdoff)}")
        self.t.write(*cmds)

    def set_acquire(self, memory_depth: int | str | None = None, type: str | None = None,
                    averages: int | None = None) -> None:
        cmds = []
        if type is not None:
            t = type.upper()
            if t not in ACQ_TYPES:
                raise ValueError(f"type must be one of {ACQ_TYPES}")
            cmds.append(f":ACQ:TYPE {t}")
        if averages is not None:
            if averages < 2 or averages > 1024 or averages & (averages - 1):
                raise ValueError("averages must be a power of two in 2..1024")
            cmds.append(f":ACQ:AVER {averages}")
        if memory_depth is not None:
            md = str(memory_depth).upper()
            if md != "AUTO" and not md.isdigit():
                raise ValueError("memory_depth must be AUTO or an integer")
            # MDEP can only be changed while running.
            cmds.append(":RUN")
            cmds.append(f":ACQ:MDEP {md}")
        self.t.write(*cmds)

    # ---------------------------------------------------------- waveforms
    def _set_wave(self, source: str | None = None, mode: str | None = None, form: str | None = None) -> None:
        """Send only the :WAV settings that differ from the cached state."""
        w = self._wav
        cmds = []
        if mode is not None and w.mode != mode:
            if w.mode == "RAW" or w.mode is None:
                # Leaving RAW with STAR/STOP > 1200 is out of range for NORM;
                # out-of-range :WAV:STAR is known to lock FW 00.04.04 up.
                self._set_range(1, SCREEN_POINTS)
            cmds.append(f":WAV:MODE {mode}")
            w.mode = mode
        if form is not None and w.form != form:
            cmds.append(f":WAV:FORM {form}")
            w.form = form
        if source is not None and w.source != source:
            cmds.append(f":WAV:SOUR {source}")
            w.source = source
        self.t.write(*cmds)

    def _set_range(self, start: int, stop: int) -> None:
        """Set :WAV:STAR/:WAV:STOP keeping start <= stop at every step."""
        w = self._wav
        if w.start is None or w.stop is None:
            w.start, w.stop = (int(x) for x in self.t.query_many([":WAV:STAR?", ":WAV:STOP?"]))
        if start == w.start and stop == w.stop:
            return
        if start > w.stop:
            order = ((":WAV:STOP", stop), (":WAV:STAR", start))
        else:
            order = ((":WAV:STAR", start), (":WAV:STOP", stop))
        cmds = [f"{c} {v}" for c, v in order if getattr(w, "start" if c.endswith("STAR") else "stop") != v]
        self.t.write(*cmds)
        w.start, w.stop = start, stop

    def _displayed(self, max_age: float) -> list[int]:
        if self._disp is None or time.monotonic() - self._disp[0] > max_age:
            self.displayed_channels()
        assert self._disp is not None
        return self._disp[1]

    def _fetch_screen(self, src: str, mode: str, display_max_age: float = 0.5) -> Waveform:
        """Screen data in one round trip: ``:WAV:SOUR?;:WAV:PRE?;:WAV:DATA?``.

        :WAV:SOUR is instrument-global, so the reply's source is checked and
        the read repeated if another client switched it in between."""
        if src.startswith("CHAN") and int(src[-1]) not in self._displayed(display_max_age):
            raise ChannelOff(f"{src} is not displayed")
        mismatches = 0
        empty_deadline = None
        while True:
            self._set_wave(src, mode, "BYTE")
            if self._wav.start != 1 or self._wav.stop != SCREEN_POINTS:
                self._set_range(1, SCREEN_POINTS)
            text, data = self.t.query_block(":WAV:SOUR?;:WAV:PRE?;:WAV:DATA?")
            sour, pre_text = text.split(";", 1)
            pre = Preamble.parse(pre_text)
            if not (sour == src and pre.mode == mode and pre.format == "BYTE"):
                self.invalidate()  # someone else changed :WAV state behind our back
                mismatches += 1
                if mismatches >= 3:
                    raise ScpiError(f"could not select {src}/{mode}; another client keeps changing :WAV settings")
                continue
            if not data:
                # For ~100 ms after :RUN the screen buffer is empty.
                now = time.monotonic()
                empty_deadline = empty_deadline or now + 1.0
                if now < empty_deadline:
                    time.sleep(0.01)
                    continue
            return Waveform(src, np.frombuffer(data, dtype=np.uint8), pre, time.time())

    def read_waveform(self, ch: int | str = 1, mode: str = "NORM") -> Waveform:
        """Screen waveform (NORM: 1200 points; MAX: screen when running,
        memory when stopped).  ~3 ms for the selected channel, ~50 ms more
        when switching channel."""
        mode = mode.upper()
        if mode not in ("NORM", "MAX"):
            raise ValueError("use read_raw() for RAW mode")
        with self.lock:
            return self._fetch_screen(source_name(ch), mode)

    def read_waveforms(self, channels: Sequence[int] | None = None, snapshot: bool = False,
                       mode: str = "NORM") -> dict[int, Waveform]:
        """Read several channels.  Each channel switch costs ~50 ms.

        While running, channels come from different acquisitions; pass
        ``snapshot=True`` to STOP, read a time-aligned set and RUN again."""
        with self.lock:
            chans = list(channels) if channels else self.displayed_channels()
            resume = False
            if snapshot and self.trigger_status() != "STOP":
                self.stop()
                self.wait_trigger_status(["STOP"])
                resume = True
            try:
                # Start with the channel already selected to save one switch.
                cur = self._wav.source
                chans.sort(key=lambda c: source_name(c) != cur)
                return {int(source_name(c)[-1]): self._fetch_screen(source_name(c), mode.upper())
                        for c in chans}
            finally:
                if resume:
                    self.run()

    def _settled_raw_preamble(self, expected: int | None, timeout: float = 1.0) -> Preamble | None:
        """Right after :STOP (or a configuration change) the RAW preamble
        briefly reports 0 points.  Wait until it matches the memory depth, or,
        with MDEP AUTO, until two consecutive reads agree."""
        deadline = time.monotonic() + timeout
        prev = None
        while True:
            pre = Preamble.parse(self.t.query(":WAV:PRE?"))
            ok = pre.mode == "RAW" and pre.points > 0
            if ok and (pre.points == expected if expected else pre == prev):
                return pre
            if time.monotonic() > deadline:
                return None
            prev = pre
            time.sleep(0.05)

    def _stop_with_full_memory(self, src: str, was_running: bool) -> Preamble:
        """Stop and return the RAW preamble of a complete acquisition.

        Stopping a 24 Mpt acquisition before it finished (e.g. right after
        :RUN) leaves only 1200 or 0 points in memory; in that case run for
        a couple of acquisition times and stop again."""
        mdep = self.t.query(":ACQ:MDEP?")
        expected = int(float(mdep)) if mdep[:1].isdigit() else None
        for attempt in range(4):
            if was_running or attempt:
                self.stop()
                self.wait_trigger_status(["STOP"])
            self._set_wave(src, "RAW", "BYTE")
            pre = self._settled_raw_preamble(expected)
            if pre is not None:
                return pre
            if not was_running:
                break  # memory of a stopped scope is what it is; don't re-acquire
            srate = float(self.t.query(":ACQ:SRAT?")) or 1e6
            self.run()
            time.sleep(0.2 + 3 * (expected or 1_200_000) / srate)
        pre = Preamble.parse(self.t.query(":WAV:PRE?"))
        if pre.points <= 0:
            raise ScpiError("no acquisition in memory; run or single-trigger first")
        return pre

    def read_raw(
        self,
        ch: int | str = 1,
        start: int = 1,
        stop: int | None = None,
        chunk: int = RAW_CHUNK,
        auto_stop: bool = True,
        resume: bool = True,
        progress: Callable[[int, int], None] | None = None,
    ) -> Waveform:
        """Read acquisition memory (up to the memory depth, e.g. 24 Mpts).

        Requires the scope to be stopped; with ``auto_stop`` it is stopped and,
        with ``resume``, restarted afterwards.  ~2.3 MB/s with 1M chunks."""
        src = source_name(ch)
        with self.lock:
            was_running = self.trigger_status() != "STOP"
            if was_running and not auto_stop:
                raise ScpiError("scope is running; stop it first or pass auto_stop=True")
            if src.startswith("CHAN") and not _bool(self.t.query(f":{src}:DISP?")):
                raise ChannelOff(f"{src} is not displayed")
            try:
                pre = self._stop_with_full_memory(src, was_running)
                total = pre.points
                stop = total if stop is None else int(stop)
                start = int(start)
                if not 1 <= start <= stop <= total:
                    raise ValueError(f"need 1 <= start <= stop <= {total}")
                chunk = max(1, min(int(chunk), RAW_CHUNK))
                n = stop - start + 1
                out = np.empty(n, dtype=np.uint8)
                pos = start
                while pos <= stop:
                    end = min(pos + chunk - 1, stop)
                    self._set_range(pos, end)
                    _, data = self.t.query_block(":WAV:DATA?", timeout=10 + chunk / 1e5)
                    out[pos - start : pos - start + len(data)] = np.frombuffer(data, dtype=np.uint8)
                    if len(data) != end - pos + 1:
                        raise ScpiError(f"short read: {len(data)} of {end - pos + 1} points")
                    pos = end + 1
                    if progress:
                        progress(pos - start, n)
                ts = time.time()
                p = Preamble(**{**pre.to_dict(), "points": n,
                                "xorigin": pre.xorigin + (start - 1) * pre.xincrement})
                return Waveform(src, out, p, ts)
            finally:
                # Leave the scope in a state where NORM reads are in range.
                self._set_range(1, SCREEN_POINTS)
                self._set_wave(mode="NORM")
                if was_running and resume:
                    self.run()

    def stream(
        self,
        channels: Sequence[int] | None = None,
        only_new: bool = True,
        max_rate: float | None = None,
        mode: str = "NORM",
        recheck_interval: float = 0.5,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[Waveform]:
        """Yield screen waveforms as fast as possible.

        * One channel: no set commands at all; ~3 ms per read.  ``only_new``
          drops frames identical to the previous one (the scope refreshes its
          screen buffer only ~10-20x per second).
        * Several channels: round robin, ~55 ms per channel (``:WAV:SOUR``).

        The display state is re-checked every ``recheck_interval`` seconds so
        that a channel turned off on the front panel is skipped instead of read.
        """
        mode = mode.upper()
        wanted = list(channels) if channels else None
        last: dict[str, bytes] = {}
        min_dt = 1.0 / max_rate if max_rate else 0.0
        t_last = 0.0
        while not (should_stop and should_stop()):
            with self.lock:
                shown = self._displayed(recheck_interval)
            active = [c for c in (wanted or shown) if c in shown]
            if not active:
                time.sleep(0.05)
                continue
            # Read the currently selected channel first to avoid a switch.
            cur = self._wav.source
            order = sorted(active, key=lambda c: f"CHAN{c}" != cur)
            for c in order:
                if min_dt:
                    wait = t_last + min_dt - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                with self.lock:
                    try:
                        wf = self._fetch_screen(f"CHAN{c}", mode, recheck_interval)
                    except ChannelOff:
                        continue
                t_last = time.monotonic()
                b = wf.raw.tobytes()
                if not b or (only_new and last.get(wf.channel) == b):
                    continue
                last[wf.channel] = b
                yield wf
            if len(order) == 1 and only_new:
                # Nothing to switch between; avoid hammering with identical reads.
                time.sleep(0.001)

    # ---------------------------------------------------------- measurements
    def measure(self, items: Sequence[str] | str, source: int | str = 1) -> dict[str, float | None]:
        """Scope-side measurements in one round trip.

        Keep the number of distinct (item, source) pairs <= 5: the instrument
        caches five, and each extra pair costs ~400 ms per query."""
        if isinstance(items, str):
            items = [i for i in items.replace(" ", "").split(",") if i]
        src = ",".join(source_name(s) for s in str(source).split(","))
        norm = []
        for it in items:
            u = it.upper()
            if u not in MEASURE_ITEMS:
                raise ValueError(f"unknown measurement {it!r}; choose from {MEASURE_ITEMS}")
            norm.append(u)
        timeout = 2.0 + 0.6 * len(norm)
        vals = self.t.query_many([f":MEAS:ITEM? {it},{src}" for it in norm], timeout=timeout)
        return {it: _num(v) for it, v in zip(norm, vals)}

    def measure_many(self, pairs: Sequence[tuple[str, int | str]]) -> list[float | None]:
        """Arbitrary (item, source) pairs in one round trip."""
        qs = []
        for it, src in pairs:
            u = it.upper()
            if u not in MEASURE_ITEMS:
                raise ValueError(f"unknown measurement {it!r}")
            qs.append(f":MEAS:ITEM? {u},{source_name(src)}")
        vals = self.t.query_many(qs, timeout=2.0 + 0.6 * len(qs))
        return [_num(v) for v in vals]

    def measure_stat(self, item: str, source: int | str = 1) -> dict[str, float | None]:
        """Statistics (current/average/min/max/deviation) of one measurement."""
        u = item.upper()
        if u not in MEASURE_ITEMS:
            raise ValueError(f"unknown measurement {item!r}")
        src = source_name(source)
        kinds = ("CURR", "AVER", "MIN", "MAX", "DEV")
        vals = self.t.query_many([f":MEAS:STAT:ITEM? {k},{u},{src}" for k in kinds], timeout=5)
        return {k.lower(): _num(v) for k, v in zip(kinds, vals)}

    def frequency_counter(self) -> float | None:
        return _num(self.t.query(":MEAS:COUN:VAL?"))

    # ------------------------------------------------------------ screenshot
    def screenshot(self, fmt: str = "PNG") -> bytes:
        f = fmt.upper()
        if f not in SCREENSHOT_FORMATS:
            raise ValueError(f"format must be one of {tuple(SCREENSHOT_FORMATS)}")
        _, data = self.t.query_block(f":DISP:DATA? ON,OFF,{f}", timeout=20)
        return data
