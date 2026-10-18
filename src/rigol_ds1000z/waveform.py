"""Waveform containers, preamble parsing and host-side analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

FORMATS = {0: "BYTE", 1: "WORD", 2: "ASC"}
MODES = {0: "NORM", 1: "MAX", 2: "RAW"}


@dataclass(frozen=True)
class Preamble:
    """Parsed ``:WAV:PRE?`` reply."""

    format: str
    mode: str
    points: int
    count: int
    xincrement: float
    xorigin: float
    xreference: float
    yincrement: float
    yorigin: float
    yreference: float

    @classmethod
    def parse(cls, text: str) -> "Preamble":
        f = text.strip().split(",")
        if len(f) != 10:
            raise ValueError(f"bad preamble: {text!r}")
        return cls(
            format=FORMATS.get(int(f[0]), f[0]),
            mode=MODES.get(int(f[1]), f[1]),
            points=int(f[2]),
            count=int(f[3]),
            xincrement=float(f[4]),
            xorigin=float(f[5]),
            xreference=float(f[6]),
            yincrement=float(f[7]),
            yorigin=float(f[8]),
            yreference=float(f[9]),
        )

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Waveform:
    """One channel's samples.  ``raw`` keeps the 8-bit ADC codes; ``volts`` is
    computed lazily so high-rate streaming never pays for the float conversion."""

    channel: str
    raw: np.ndarray  # uint8
    preamble: Preamble
    timestamp: float  # time.time() when the transfer completed
    _volts: np.ndarray | None = field(default=None, repr=False)

    @property
    def volts(self) -> np.ndarray:
        if self._volts is None:
            p = self.preamble
            self._volts = (self.raw.astype(np.float32) - np.float32(p.yorigin + p.yreference)) * np.float32(
                p.yincrement
            )
        return self._volts

    @property
    def times(self) -> np.ndarray:
        p = self.preamble
        return p.xorigin + (np.arange(self.raw.size) - p.xreference) * p.xincrement

    @property
    def sample_interval(self) -> float:
        return self.preamble.xincrement

    def __len__(self) -> int:
        return int(self.raw.size)

    def analyze(self) -> dict:
        return analyze(self.volts, self.preamble.xincrement)

    def to_dict(self, include_volts: bool = True, include_raw: bool = False) -> dict:
        d = {
            "channel": self.channel,
            "points": len(self),
            "timestamp": self.timestamp,
            "x0": self.preamble.xorigin - self.preamble.xreference * self.preamble.xincrement,
            "dx": self.preamble.xincrement,
            "preamble": self.preamble.to_dict(),
        }
        if include_volts:
            d["volts"] = np.round(self.volts, 6).tolist()
        if include_raw:
            d["raw"] = self.raw.tolist()
        return d


def _clean(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def analyze(v: np.ndarray, dt: float) -> dict:
    """Host-side measurements.  Costs microseconds, unlike scope measurements
    (~400 ms each once more than five items are in use)."""
    if v.size == 0:
        return {}
    v = v.astype(np.float64, copy=False)
    vmax = float(v.max())
    vmin = float(v.min())
    vpp = vmax - vmin
    out = {
        "vmax": vmax,
        "vmin": vmin,
        "vpp": vpp,
        "vavg": float(v.mean()),
        "vrms": float(np.sqrt(np.mean(v * v))),
        "vac_rms": float(v.std()),
        "freq": None,
        "period": None,
        "duty": None,
        "rise_edges": 0,
    }
    if vpp <= 0:
        return out
    # Schmitt-trigger the signal around mid level (10% hysteresis) so that ADC
    # noise does not produce false edges.
    mid = (vmax + vmin) / 2
    hi, lo = mid + 0.1 * vpp, mid - 0.1 * vpp
    state = np.full(v.shape, -1, dtype=np.int8)
    state[v > hi] = 1
    state[v < lo] = 0
    idx = np.flatnonzero(state >= 0)
    if idx.size < 2:
        return out
    # Forward fill undecided samples with the last decided state (samples
    # before the first decided one take its state).
    pos = np.searchsorted(idx, np.arange(v.size), side="right") - 1
    filled = state[idx][np.clip(pos, 0, None)]
    edges = np.flatnonzero(np.diff(filled) == 1) + 1
    out["rise_edges"] = int(edges.size)
    if edges.size >= 2:
        period = (edges[-1] - edges[0]) / (edges.size - 1) * dt
        out["period"] = _clean(period)
        out["freq"] = _clean(1.0 / period) if period > 0 else None
        out["duty"] = _clean(float(np.mean(filled[edges[0] : edges[-1]])))
    return out
