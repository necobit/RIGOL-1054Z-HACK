"""Task-level helpers shared by the MCP server and the REST API: connection
management, numbered captures, timed monitoring, plots and reports."""

from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np

from .discovery import discover
from .scope import DS1000Z
from .waveform import Waveform

CH_COLORS = {1: "#d4a600", 2: "#00a3c4", 3: "#c2185b", 4: "#1f4fd1"}
ANALYSIS_KEYS = ("vpp", "vmax", "vmin", "vavg", "vrms", "vac_rms", "freq", "period", "duty")

_PREFIX = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"))


def eng(v: float | None, unit: str = "", digits: int = 4) -> str:
    """12345 -> '12.35 k' style formatting."""
    if v is None or not np.isfinite(v):
        return "—"
    if v == 0:
        return f"0 {unit}".strip()
    for scale, p in _PREFIX:
        if abs(v) >= scale:
            return f"{v / scale:.{digits}g} {p}{unit}".strip()
    return f"{v:.{digits}g} {unit}".strip()


def _time_axis(t: np.ndarray) -> tuple[np.ndarray, str]:
    span = float(np.max(np.abs(t))) if t.size else 1.0
    for scale, p in ((1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if span >= scale:
            return t / scale, f"{p}s"
    return t / 1e-9, "ns"


@dataclass
class Capture:
    id: str
    timestamp: float
    mode: str
    waveforms: dict[int, Waveform]
    note: str = ""
    settings: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "time": datetime.fromtimestamp(self.timestamp).isoformat(timespec="milliseconds"),
            "mode": self.mode,
            "note": self.note,
            "channels": {
                ch: {"points": len(w), "sample_interval_s": w.preamble.xincrement,
                     "duration_s": len(w) * w.preamble.xincrement,
                     **{k: w.analyze().get(k) for k in ANALYSIS_KEYS}}
                for ch, w in self.waveforms.items()
            },
        }


@dataclass
class MonitorResult:
    id: str
    started: float
    duration: float
    channels: list[int]
    # rows: (t_since_start, channel, {analysis})
    rows: list[tuple[float, int, dict]]
    scope_rows: list[tuple[float, dict]] = field(default_factory=list)

    def stats(self) -> dict:
        out: dict = {}
        for ch in self.channels:
            rs = [r for t, c, r in self.rows if c == ch]
            d = {"frames": len(rs)}
            for k in ANALYSIS_KEYS:
                vals = np.array([r[k] for r in rs if r.get(k) is not None], dtype=float)
                if vals.size:
                    d[k] = {"mean": float(vals.mean()), "min": float(vals.min()),
                            "max": float(vals.max()), "std": float(vals.std())}
            out[ch] = d
        if self.scope_rows:
            keys = sorted({k for _, r in self.scope_rows for k in r})
            sd = {}
            for k in keys:
                vals = np.array([r[k] for _, r in self.scope_rows if r.get(k) is not None], dtype=float)
                if vals.size:
                    sd[k] = {"mean": float(vals.mean()), "min": float(vals.min()),
                             "max": float(vals.max()), "std": float(vals.std()), "n": int(vals.size)}
            out["scope_measurements"] = sd
        return out

    def summary(self) -> dict:
        return {"id": self.id, "time": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
                "duration_s": self.duration, "channels": self.channels,
                "frames_per_s": round(len(self.rows) / max(self.duration, 1e-9), 2),
                "stats": self.stats()}


@dataclass
class Report:
    id: str
    title: str
    created: float
    blocks: list[dict] = field(default_factory=list)  # {"kind": "md"|"image"|"table", ...}


class Lab:
    def __init__(self, host: str | None = None, data_dir: str | Path | None = None):
        self._host = host or os.environ.get("RIGOL_HOST")
        self.data_dir = Path(data_dir or os.environ.get("RIGOL_DATA_DIR", "rigol_data")).resolve()
        self._scope: DS1000Z | None = None
        self._lock = threading.RLock()
        self.captures: dict[str, Capture] = {}
        self.monitors: dict[str, MonitorResult] = {}
        self.reports: dict[str, Report] = {}
        self._n = 0

    # ---------------------------------------------------------- connection
    @property
    def scope(self) -> DS1000Z:
        with self._lock:
            if self._scope is None:
                self.connect()
            assert self._scope is not None
            return self._scope

    def connect(self, host: str | None = None) -> dict:
        """Connect to ``host`` (or $RIGOL_HOST).  If that address does not
        answer - e.g. the scope moved to another network - fall back to
        auto-discovery instead of failing."""
        with self._lock:
            if self._scope is not None:
                self._scope.close()
                self._scope = None
            host = host or self._host
            scope = None
            if host:
                try:
                    scope = DS1000Z(host, timeout=3).connect()
                    scope.idn()
                except OSError:
                    scope = None
            if scope is None:
                found = discover()
                if not found:
                    raise RuntimeError(f"no RIGOL scope found (tried {host or 'nothing'} and auto-discovery); "
                                       "set RIGOL_HOST or pass host")
                host = found[0]["host"]
                scope = DS1000Z(host).connect()
            scope.t.timeout = 5.0
            self._scope = scope
            self._host = host
            return {"host": host, **self._scope.idn()}

    def _next_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    # ------------------------------------------------------------- capture
    def capture(self, channels: Sequence[int] | None = None, mode: str = "screen",
                snapshot: bool = True, raw_points: int | None = None, note: str = "") -> Capture:
        """mode: 'screen' (1200 pts/ch) or 'raw' (acquisition memory)."""
        s = self.scope
        with s.lock:
            chans = list(channels) if channels else s.displayed_channels()
            settings = s.status()
            if mode == "raw":
                wfs = {}
                was_running = s.trigger_status() != "STOP"
                try:
                    for c in chans:
                        wfs[c] = s.read_raw(c, stop=raw_points, resume=False)
                finally:
                    if was_running:
                        s.run()
            else:
                wfs = s.read_waveforms(chans, snapshot=snapshot)
        cap = Capture(self._next_id("cap"), time.time(), mode, wfs, note, settings)
        self.captures[cap.id] = cap
        return cap

    def get_capture(self, cid: str) -> Capture:
        if cid not in self.captures:
            raise KeyError(f"unknown capture {cid!r}; known: {list(self.captures)}")
        return self.captures[cid]

    def export_capture(self, cid: str, fmt: str = "csv") -> Path:
        cap = self.get_capture(cid)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(cap.timestamp).strftime("%Y%m%d_%H%M%S")
        base = self.data_dir / f"{stamp}_{cap.id}"
        if fmt == "npz":
            path = base.with_suffix(".npz")
            np.savez_compressed(path, **{f"ch{c}_raw": w.raw for c, w in cap.waveforms.items()},
                                meta=json.dumps({c: w.preamble.to_dict() for c, w in cap.waveforms.items()}))
            return path
        path = base.with_suffix(".csv")
        chans = list(cap.waveforms)
        n = max(len(w) for w in cap.waveforms.values())
        first = cap.waveforms[chans[0]]
        cols = [first.times[:n]] + [cap.waveforms[c].volts for c in chans]
        if any(len(c) != n for c in cols):
            raise ValueError("channels have different lengths; export as npz")
        np.savetxt(path, np.column_stack(cols), delimiter=",", fmt="%.9g",
                   header="time_s," + ",".join(f"ch{c}_V" for c in chans), comments="")
        return path

    # ------------------------------------------------------------- monitor
    def monitor(self, channels: Sequence[int] | None = None, duration: float = 10.0,
                scope_items: Sequence[str] = (), scope_source: int = 1) -> MonitorResult:
        """Stream for ``duration`` seconds, analysing every new frame on the
        host.  Optionally also poll scope measurements (<= 5 items)."""
        s = self.scope
        chans = list(channels) if channels else s.displayed_channels()
        rows: list[tuple[float, int, dict]] = []
        scope_rows: list[tuple[float, dict]] = []
        t0 = time.monotonic()
        started = time.time()
        stop_at = t0 + duration
        next_meas = t0
        for wf in s.stream(chans, only_new=True, should_stop=lambda: time.monotonic() >= stop_at):
            now = time.monotonic()
            rows.append((now - t0, int(wf.channel[-1]), wf.analyze()))
            if scope_items and now >= next_meas:
                scope_rows.append((now - t0, s.measure(list(scope_items), scope_source)))
                next_meas = now + 0.1
        mon = MonitorResult(self._next_id("mon"), started, time.monotonic() - t0, chans, rows, scope_rows)
        self.monitors[mon.id] = mon
        return mon

    def export_monitor(self, mid: str) -> Path:
        mon = self.monitors[mid]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / f"{datetime.fromtimestamp(mon.started).strftime('%Y%m%d_%H%M%S')}_{mon.id}.csv"
        with path.open("w") as f:
            f.write("t_s,channel," + ",".join(ANALYSIS_KEYS) + "\n")
            for t, c, r in mon.rows:
                f.write(f"{t:.4f},{c}," + ",".join("" if r.get(k) is None else f"{r[k]:.9g}" for k in ANALYSIS_KEYS) + "\n")
        return path

    # --------------------------------------------------------------- plots
    @staticmethod
    def _fig():
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt

    def plot_capture(self, cid: str, separate: bool | None = None, title: str | None = None) -> bytes:
        cap = self.get_capture(cid)
        plt = self._fig()
        chans = list(cap.waveforms)
        separate = len(chans) > 1 if separate is None else separate
        rows = len(chans) if separate else 1
        fig, axes = plt.subplots(rows, 1, figsize=(10, 2.2 + 1.8 * rows), sharex=True, squeeze=False)
        for i, c in enumerate(chans):
            w = cap.waveforms[c]
            ax = axes[i if separate else 0][0]
            step = max(1, len(w) // 20000)  # decimate huge RAW captures for plotting
            if step > 1:
                v = w.volts[: len(w) // step * step].reshape(-1, step)
                t, unit = _time_axis(w.times[: len(w) // step * step : step])
                ax.fill_between(t, v.min(1), v.max(1), color=CH_COLORS.get(c, "k"), lw=0, alpha=0.9, label=f"CH{c}")
            else:
                t, unit = _time_axis(w.times)
                ax.plot(t, w.volts, color=CH_COLORS.get(c, "k"), lw=1.0, label=f"CH{c}")
            a = w.analyze()
            if separate or len(chans) == 1:
                ax.set_ylabel(f"CH{c} [V]")
                ax.set_title(f"CH{c}  Vpp {eng(a['vpp'], 'V')}  Vrms {eng(a['vrms'], 'V')}  "
                             f"f {eng(a['freq'], 'Hz')}", fontsize=9, loc="left")
            ax.grid(True, alpha=0.3)
            ax.set_xlabel(f"time [{unit}]")
        if not separate and len(chans) > 1:
            axes[0][0].legend(loc="upper right", fontsize=8)
            axes[0][0].set_ylabel("V")
        fig.suptitle(title or f"{cap.id}  {datetime.fromtimestamp(cap.timestamp):%Y-%m-%d %H:%M:%S}"
                     + (f"  — {cap.note}" if cap.note else ""), fontsize=11)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        return buf.getvalue()

    def plot_monitor(self, mid: str, key: str = "vpp") -> bytes:
        mon = self.monitors[mid]
        plt = self._fig()
        fig, ax = plt.subplots(figsize=(10, 3.6))
        for c in mon.channels:
            pts = [(t, r.get(key)) for t, ch, r in mon.rows if ch == c and r.get(key) is not None]
            if pts:
                t, v = zip(*pts)
                ax.plot(t, v, ".-", ms=2.5, lw=0.8, color=CH_COLORS.get(c, "k"), label=f"CH{c}")
        ax.set_xlabel("time [s]")
        ax.set_ylabel(key)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        ax.set_title(f"{mon.id}: {key} over {mon.duration:.1f} s", fontsize=11)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        return buf.getvalue()

    # -------------------------------------------------------------- report
    def report_create(self, title: str) -> Report:
        r = Report(self._next_id("rep"), title, time.time())
        self.reports[r.id] = r
        return r

    def _report(self, rid: str) -> Report:
        if rid not in self.reports:
            raise KeyError(f"unknown report {rid!r}; known: {list(self.reports)}")
        return self.reports[rid]

    def report_add_text(self, rid: str, markdown: str) -> None:
        self._report(rid).blocks.append({"kind": "md", "text": markdown})

    def report_add_image(self, rid: str, png: bytes, caption: str = "") -> None:
        self._report(rid).blocks.append({"kind": "image", "png": png, "caption": caption})

    def report_add_table(self, rid: str, header: list[str], rows: list[list], caption: str = "") -> None:
        self._report(rid).blocks.append({"kind": "table", "header": header, "rows": rows, "caption": caption})

    def report_add_capture(self, rid: str, cid: str, caption: str = "") -> None:
        cap = self.get_capture(cid)
        self.report_add_image(rid, self.plot_capture(cid), caption or f"Capture {cid}")
        rows = []
        for c, w in cap.waveforms.items():
            a = w.analyze()
            rows.append([f"CH{c}", eng(a["vpp"], "V"), eng(a["vmax"], "V"), eng(a["vmin"], "V"),
                         eng(a["vavg"], "V"), eng(a["vrms"], "V"), eng(a["freq"], "Hz"),
                         "—" if a["duty"] is None else f"{a['duty'] * 100:.1f} %", f"{len(w):,}"])
        self.report_add_table(rid, ["ch", "Vpp", "Vmax", "Vmin", "Vavg", "Vrms", "freq", "duty", "points"], rows,
                              f"Host-side analysis of {cid}")
        st = cap.settings
        if st:
            ch_rows = [[f"CH{c}", eng(v["scale"], "V/div"), eng(v["offset"], "V"), v["coupling"], f"{v['probe']:g}x",
                        v["bwlimit"]] for c, v in st["channels"].items() if v["display"]]
            self.report_add_table(rid, ["ch", "scale", "offset", "coupling", "probe", "BW limit"], ch_rows,
                                  f"Timebase {eng(st['timebase_scale'], 's/div')}, sample rate "
                                  f"{eng(st['sample_rate'], 'Sa/s')}, memory depth {st['memory_depth']}, "
                                  f"trigger {st['trigger_source']} {st['trigger_slope']} @ "
                                  f"{eng(st['trigger_level'], 'V')} ({st['trigger_sweep']})")

    def report_add_monitor(self, rid: str, mid: str, keys: Sequence[str] = ("vpp", "freq")) -> None:
        mon = self.monitors[mid]
        for k in keys:
            self.report_add_image(rid, self.plot_monitor(mid, k), f"{mid}: {k} vs time")
        units = {"freq": "Hz", "period": "s", "duty": ""}
        rows = []
        for c, d in mon.stats().items():
            if c == "scope_measurements":
                continue
            for k in ANALYSIS_KEYS:
                if k in d:
                    u = units.get(k, "V")
                    f = (lambda x: f"{x * 100:.2f} %") if k == "duty" else (lambda x, u=u: eng(x, u))
                    rows.append([f"CH{c}", k, f(d[k]["mean"]), f(d[k]["min"]), f(d[k]["max"]), f(d[k]["std"]),
                                 d["frames"]])
        self.report_add_table(rid, ["ch", "item", "mean", "min", "max", "std", "frames"], rows,
                              f"{mid}: statistics over {mon.duration:.1f} s")

    def report_finish(self, rid: str, out_dir: str | Path | None = None) -> dict:
        r = self._report(rid)
        out = Path(out_dir or self.data_dir / "reports")
        slug = re.sub(r"[^\w-]+", "_", r.title).strip("_")[:40] or "report"
        d = out / f"{datetime.fromtimestamp(r.created):%Y%m%d_%H%M%S}_{slug}"
        d.mkdir(parents=True, exist_ok=True)
        md = [f"# {r.title}", "", f"_{datetime.fromtimestamp(r.created):%Y-%m-%d %H:%M}_", ""]
        body = []
        n_img = 0
        for b in r.blocks:
            if b["kind"] == "md":
                md += [b["text"], ""]
                body.append(_md_to_html(b["text"]))
            elif b["kind"] == "image":
                n_img += 1
                name = f"fig{n_img}.png"
                (d / name).write_bytes(b["png"])
                md += [f"![{b['caption']}]({name})", "", f"*{b['caption']}*" if b["caption"] else "", ""]
                b64 = base64.b64encode(b["png"]).decode()
                body.append(f'<figure><img src="data:image/png;base64,{b64}" alt="{html.escape(b["caption"])}">'
                            f"<figcaption>{html.escape(b['caption'])}</figcaption></figure>")
            elif b["kind"] == "table":
                md += ["| " + " | ".join(map(str, b["header"])) + " |",
                       "|" + "---|" * len(b["header"])]
                md += ["| " + " | ".join(map(str, row)) + " |" for row in b["rows"]]
                md += ["", f"*{b['caption']}*" if b["caption"] else "", ""]
                th = "".join(f"<th>{html.escape(str(h))}</th>" for h in b["header"])
                trs = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>"
                              for row in b["rows"])
                cap = f"<caption>{html.escape(b['caption'])}</caption>" if b["caption"] else ""
                body.append(f"<table>{cap}<thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
        (d / "report.md").write_text("\n".join(md), encoding="utf-8")
        page = _HTML.format(title=html.escape(r.title),
                            date=f"{datetime.fromtimestamp(r.created):%Y-%m-%d %H:%M}",
                            body="\n".join(body))
        (d / "report.html").write_text(page, encoding="utf-8")
        return {"dir": str(d), "markdown": str(d / "report.md"), "html": str(d / "report.html")}


def _md_to_html(text: str) -> str:
    """Tiny Markdown subset (headings, bullets, paragraphs, **bold**, `code`)."""
    out, para, items = [], [], []

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", s)

    def flush():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            flush()
            lvl = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif re.match(r"^[-*]\s+", s):
            if para:
                flush()
            items.append(re.sub(r"^[-*]\s+", "", s))
        elif not s:
            flush()
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "\n".join(out)


_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#666; --line:#e3e3e3; --head:#f5f5f7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16171a; --fg:#e8e8ea; --muted:#9a9aa0; --line:#33343a; --head:#202126; }}
  img {{ background:#fff; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.6 -apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;
  max-width:980px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ margin:0 0 4px; font-size:26px; }} .date {{ color:var(--muted); margin-bottom:24px; }}
h2 {{ margin-top:32px; border-bottom:1px solid var(--line); padding-bottom:4px; }}
figure {{ margin:16px 0; }} img {{ max-width:100%; border:1px solid var(--line); border-radius:6px; }}
figcaption, caption {{ color:var(--muted); font-size:13px; padding:4px 0; caption-side:bottom; text-align:left; }}
table {{ border-collapse:collapse; margin:12px 0; font-variant-numeric:tabular-nums; display:block; overflow-x:auto; }}
th, td {{ border:1px solid var(--line); padding:4px 10px; text-align:right; white-space:nowrap; }}
th {{ background:var(--head); }} td:first-child, th:first-child {{ text-align:left; }}
code {{ background:var(--head); padding:1px 4px; border-radius:4px; }}
</style></head><body>
<h1>{title}</h1><div class="date">{date}</div>
{body}
</body></html>
"""
