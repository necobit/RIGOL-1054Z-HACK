"""MCP server exposing the oscilloscope to LLM agents (Claude Code, Claude
Desktop, ...).  Run with ``rigol-ds mcp`` (stdio transport).

The tool descriptions carry the instrument's timing characteristics so the
model can pick fast strategies on its own (e.g. snapshot captures instead of
repeated single-channel reads, host-side analysis instead of >5 scope
measurement items)."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Image, MCPServer
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer, Image  # type: ignore

from .lab import Lab
from .scope import MEASURE_ITEMS

INSTRUCTIONS = """\
Controls a RIGOL DS1000Z 4-channel oscilloscope (DS1054Z/DS1104Z) over LAN.

Typical flow for "measure X and write a report":
1. scope_status (see what is connected/displayed) -> adjust with set_channel /
   set_timebase / set_trigger, or scope_control(action="autoscale").
2. capture (snapshot of displayed channels; returns host-side analysis:
   Vpp, Vmax, Vmin, Vavg, Vrms, AC rms, frequency, period, duty).
   Use plot_capture to look at it.  Use monitor for behaviour over time.
3. report_create -> report_add_text / report_add_capture / report_add_monitor /
   report_add_screenshot -> report_finish (writes HTML + Markdown, returns paths).

Timing facts (measured): any setting change costs ~50 ms in the scope; a
1200-point screen capture of one channel ~3 ms, each further channel ~50 ms;
a raw memory read ~1.5 Mpts/s (24 Mpts ~17 s); screenshot ~0.6 s.  Scope
measurements (measure tool) are fast only for <= 5 distinct (item, channel)
pairs, otherwise ~0.4 s each - prefer capture's host-side analysis.
Screen data is 8-bit (1200 points across 12 divisions); for precise
amplitudes set V/div so the signal fills the screen.
"""

mcp = MCPServer("rigol-ds1000z", instructions=INSTRUCTIONS)
lab = Lab()


async def _run(fn, *args, **kwargs):
    """Instrument I/O is blocking; keep it off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


@mcp.tool()
async def scope_connect(host: str | None = None) -> str:
    """Connect to the oscilloscope.  Without host, uses $RIGOL_HOST or
    auto-discovers on all local networks.  Other tools connect automatically,
    so this is only needed to switch instruments."""
    return _json(await _run(lab.connect, host))


@mcp.tool()
async def scope_discover(network: str | None = None) -> str:
    """List RIGOL instruments reachable on local networks (VXI-11 broadcast +
    ARP).  Optionally sweep an IPv4 network such as '192.168.1.0/24'."""
    from .discovery import discover

    return _json(await _run(discover, network))


@mcp.tool()
async def scope_status() -> str:
    """All front-panel settings: run state, timebase, sample rate, memory
    depth, trigger, and per-channel display/scale/offset/coupling/probe."""
    s = await _run(lambda: lab.scope)
    st = await _run(s.status)
    st["instrument"] = await _run(s.idn)
    st["host"] = s.host
    return _json(st)


@mcp.tool()
async def scope_control(action: Literal["run", "stop", "single", "force_trigger", "autoscale", "clear"]) -> str:
    """Run/stop acquisition, arm a single trigger, force a trigger, autoscale
    (takes a few seconds and changes V/div and timebase), or clear the screen."""
    s = await _run(lambda: lab.scope)
    fn = {"run": s.run, "stop": s.stop, "single": s.single, "force_trigger": s.force_trigger,
          "autoscale": s.autoscale, "clear": s.clear}[action]
    await _run(fn)
    return _json({"done": action, "trigger_status": await _run(s.trigger_status), "errors": await _run(s.errors)})


@mcp.tool()
async def set_channel(channel: int, display: bool | None = None, scale_v_per_div: float | None = None,
                      offset_v: float | None = None, coupling: Literal["AC", "DC", "GND"] | None = None,
                      probe_ratio: float | None = None, bw_limit_20mhz: bool | None = None,
                      invert: bool | None = None) -> str:
    """Change one channel's vertical settings.  Only given parameters are sent."""
    s = await _run(lambda: lab.scope)
    await _run(s.set_channel, channel, display, scale_v_per_div, offset_v, coupling, probe_ratio,
               bw_limit_20mhz, invert)
    st = await _run(s.status)
    return _json({"channel": st["channels"][channel], "errors": await _run(s.errors)})


@mcp.tool()
async def set_timebase(scale_s_per_div: float | None = None, offset_s: float | None = None,
                       mode: Literal["MAIN", "XY", "ROLL"] | None = None) -> str:
    """Horizontal settings.  The screen spans 12 divisions, so e.g. 5 periods
    of a 1 kHz signal need about 0.5 ms/div."""
    s = await _run(lambda: lab.scope)
    await _run(s.set_timebase, scale_s_per_div, offset_s, mode)
    st = await _run(s.status)
    return _json({k: st[k] for k in ("timebase_scale", "timebase_offset", "timebase_mode", "sample_rate",
                                     "memory_depth")} | {"errors": await _run(s.errors)})


@mcp.tool()
async def set_trigger(source_channel: int | None = None, level_v: float | None = None,
                      slope: Literal["POS", "NEG", "RFAL"] | None = None,
                      sweep: Literal["AUTO", "NORM", "SING"] | None = None,
                      coupling: Literal["AC", "DC", "LFR", "HFR"] | None = None,
                      holdoff_s: float | None = None) -> str:
    """Edge trigger settings.  sweep AUTO free-runs without a trigger, NORM
    waits for one, SING captures once."""
    s = await _run(lambda: lab.scope)
    await _run(s.set_trigger, source_channel, level_v, slope, sweep, coupling, holdoff_s)
    st = await _run(s.status)
    return _json({k: v for k, v in st.items() if k.startswith("trigger")} | {"errors": await _run(s.errors)})


@mcp.tool()
async def set_acquisition(memory_depth: str | None = None,
                          acquire_type: Literal["NORM", "AVER", "PEAK", "HRES"] | None = None,
                          averages: int | None = None) -> str:
    """memory_depth: 'AUTO' or points (1 ch: 12000, 120000, 1200000, 12000000,
    24000000; halves/quarters with 2/3-4 channels).  AUTO gives the fastest
    screen refresh.  averages: power of two 2..1024 for acquire_type AVER."""
    s = await _run(lambda: lab.scope)
    await _run(s.set_acquire, memory_depth, acquire_type, averages)
    st = await _run(s.status)
    return _json({k: st[k] for k in ("memory_depth", "sample_rate", "acquire_type", "averages")}
                 | {"errors": await _run(s.errors)})


@mcp.tool()
async def capture(channels: list[int] | None = None, mode: Literal["screen", "raw"] = "screen",
                  raw_points: int | None = None, note: str = "", with_plot: bool = True):
    """Capture waveforms and analyse them on the host.

    mode 'screen': 1200 points/channel, time-aligned snapshot (scope is
    stopped briefly), ~0.3 s for 4 channels.  mode 'raw': acquisition memory
    for full resolution (raw_points limits the length; ~1.5 Mpts/s).
    Returns a capture id for plot_capture / export_capture / report_add_capture,
    the analysis per channel and (with_plot) a plot image."""
    cap = await _run(lab.capture, channels, mode, True, raw_points, note)
    out = [_json(cap.summary())]
    if with_plot:
        out.append(Image(data=await _run(lab.plot_capture, cap.id), format="png"))
    return out


@mcp.tool()
async def plot_capture(capture_id: str, separate_axes: bool | None = None):
    """Plot a previous capture (PNG image)."""
    return Image(data=await _run(lab.plot_capture, capture_id, separate_axes), format="png")


@mcp.tool()
async def export_capture(capture_id: str, format: Literal["csv", "npz"] = "csv") -> str:
    """Save a capture to disk (CSV: time + volts per channel; NPZ: raw 8-bit
    codes + preamble, compact for RAW captures).  Returns the file path."""
    return str(await _run(lab.export_capture, capture_id, format))


@mcp.tool(description="Scope-side measurements, computed by the instrument on full memory. "
                       "Fast (~3 ms) for up to 5 distinct item/channel pairs in use; each extra pair "
                       "costs ~0.4 s. Two-source items (RDEL, FDEL, RPH, FPH) are not supported here; "
                       "use scpi. Items: " + ", ".join(MEASURE_ITEMS[:-4]))
async def measure(items: list[str], channel: int = 1) -> str:
    s = await _run(lambda: lab.scope)
    return _json(await _run(s.measure, items, channel))


@mcp.tool()
async def monitor(duration_s: float = 10.0, channels: list[int] | None = None,
                  scope_items: list[str] | None = None, scope_channel: int = 1, with_plot: bool = True):
    """Watch signals for a while: every new screen frame is analysed on the
    host (1 channel ~10-20 frames/s, 4 channels ~5 frames/s each).  Returns
    mean/min/max/std of Vpp, Vrms, frequency, duty ... per channel, a monitor
    id for report_add_monitor / export_monitor and a Vpp-vs-time plot.
    scope_items additionally polls up to 5 scope measurements at ~10 Hz."""
    mon = await _run(lab.monitor, channels, duration_s, scope_items or (), scope_channel)
    out = [_json(mon.summary())]
    if with_plot and mon.rows:
        out.append(Image(data=await _run(lab.plot_monitor, mon.id, "vpp"), format="png"))
    return out


@mcp.tool()
async def export_monitor(monitor_id: str) -> str:
    """Save a monitor's per-frame analysis to CSV.  Returns the path."""
    return str(await _run(lab.export_monitor, monitor_id))


@mcp.tool()
async def screenshot():
    """Screenshot of the oscilloscope display (PNG, ~0.6 s)."""
    s = await _run(lambda: lab.scope)
    return Image(data=await _run(s.screenshot, "PNG"), format="png")


@mcp.tool()
async def report_create(title: str) -> str:
    """Start a measurement report; returns its id."""
    return lab.report_create(title).id


@mcp.tool()
async def report_add_text(report_id: str, markdown: str) -> str:
    """Append text (Markdown: headings, bullet lists, paragraphs, **bold**)."""
    lab.report_add_text(report_id, markdown)
    return "ok"


@mcp.tool()
async def report_add_capture(report_id: str, capture_id: str, caption: str = "") -> str:
    """Append a capture: waveform plot, analysis table and scope settings table."""
    await _run(lab.report_add_capture, report_id, capture_id, caption)
    return "ok"


@mcp.tool()
async def report_add_monitor(report_id: str, monitor_id: str, items: list[str] | None = None) -> str:
    """Append monitor statistics and trend plots (items default: vpp, freq)."""
    await _run(lab.report_add_monitor, report_id, monitor_id, items or ("vpp", "freq"))
    return "ok"


@mcp.tool()
async def report_add_screenshot(report_id: str, caption: str = "Oscilloscope screen") -> str:
    """Append a screenshot of the scope display."""
    s = await _run(lambda: lab.scope)
    lab.report_add_image(report_id, await _run(s.screenshot, "PNG"), caption)
    return "ok"


@mcp.tool()
async def report_add_table(report_id: str, header: list[str], rows: list[list[str]], caption: str = "") -> str:
    """Append a custom table (e.g. a comparison you computed)."""
    lab.report_add_table(report_id, header, rows, caption)
    return "ok"


@mcp.tool()
async def report_finish(report_id: str, out_dir: str | None = None) -> str:
    """Write the report as self-contained HTML plus Markdown with PNG figures.
    Returns the file paths."""
    return _json(await _run(lab.report_finish, report_id, out_dir))


@mcp.tool()
async def scpi(command: str) -> str:
    """Send a raw SCPI command (escape hatch for anything not covered above).
    Commands ending in '?' return the reply; binary replies are summarised.
    Never send :WAV:STAR/:WAV:STOP outside the valid range: FW 00.04.04 can
    lock up until power-cycled."""
    s = await _run(lambda: lab.scope)
    r = await _run(s.scpi, command)
    if isinstance(r, bytes):
        return f"<binary block, {len(r)} bytes>"
    return "ok" if r is None else r


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
