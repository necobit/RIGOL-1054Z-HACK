"""REST + WebSocket API.  Run with ``rigol-ds serve``; docs at /docs.

A single background thread owns waveform streaming and fans frames out to
all WebSocket clients, so N viewers cost the instrument no more than one.
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from contextlib import asynccontextmanager
from importlib import resources
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .lab import Lab
from .scope import SCREENSHOT_FORMATS, ChannelOff
from .transport import ScpiError, ScpiTimeout


class ChannelSettings(BaseModel):
    display: bool | None = None
    scale: float | None = None
    offset: float | None = None
    coupling: Literal["AC", "DC", "GND"] | None = None
    probe: float | None = None
    bwlimit: bool | None = None
    invert: bool | None = None


class TimebaseSettings(BaseModel):
    scale: float | None = None
    offset: float | None = None
    mode: Literal["MAIN", "XY", "ROLL"] | None = None


class TriggerSettings(BaseModel):
    source: int | None = None
    level: float | None = None
    slope: Literal["POS", "NEG", "RFAL"] | None = None
    sweep: Literal["AUTO", "NORM", "SING"] | None = None
    coupling: Literal["AC", "DC", "LFR", "HFR"] | None = None
    holdoff: float | None = None


class AcquireSettings(BaseModel):
    memory_depth: str | None = None
    type: Literal["NORM", "AVER", "PEAK", "HRES"] | None = None
    averages: int | None = None


class ScpiCommand(BaseModel):
    command: str


def frame_bytes(wf, seq: int) -> bytes:
    """Binary WebSocket frame: uint32 LE header length, JSON header, uint8 samples.
    volts = (raw - yoffset) * yinc;  t = x0 + i * dx."""
    p = wf.preamble
    head = json.dumps({
        "ch": int(wf.channel[-1]), "seq": seq, "ts": wf.timestamp, "n": len(wf),
        "x0": p.xorigin - p.xreference * p.xincrement, "dx": p.xincrement,
        "yinc": p.yincrement, "yoffset": p.yorigin + p.yreference,
    }).encode()
    return struct.pack("<I", len(head)) + head + wf.raw.tobytes()


class Broadcaster:
    """Streams the union of all subscribers' channels in one thread."""

    def __init__(self, lab: Lab):
        self.lab = lab
        self.subs: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue, set[int]]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next = 0
        self.fps = 0.0
        self.error: str | None = None

    def subscribe(self, channels: set[int]) -> tuple[int, asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        with self._lock:
            self._next += 1
            sid = self._next
            self.subs[sid] = (asyncio.get_running_loop(), q, set(channels))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="rigol-stream")
                self._thread.start()
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self.subs.pop(sid, None)

    def _wanted(self) -> list[int]:
        with self._lock:
            return sorted(set().union(*(s[2] for s in self.subs.values()))) if self.subs else []

    @staticmethod
    def _offer(q: asyncio.Queue, item) -> None:
        if q.full():  # slow client: drop the oldest frame, keep latency low
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(item)

    def _run(self) -> None:
        seq = 0
        t_win, n_win = time.monotonic(), 0
        while True:
            wanted = self._wanted()
            if not wanted:
                return
            try:
                s = self.lab.scope
                for wf in s.stream(wanted, only_new=True, should_stop=lambda w=wanted: self._wanted() != w):
                    seq += 1
                    n_win += 1
                    now = time.monotonic()
                    if now - t_win >= 1.0:
                        self.fps, t_win, n_win = n_win / (now - t_win), now, 0
                    data = frame_bytes(wf, seq)
                    ch = int(wf.channel[-1])
                    with self._lock:
                        targets = [(loop, q) for loop, q, chans in self.subs.values() if ch in chans]
                    for loop, q in targets:
                        loop.call_soon_threadsafe(self._offer, q, data)
                self.error = None
            except Exception as e:  # keep streaming through transient errors
                self.error = repr(e)
                time.sleep(0.5)


def create_app(lab: Lab | None = None) -> FastAPI:
    lab = lab or Lab()
    bc = Broadcaster(lab)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if lab._scope is not None:
            lab._scope.close()

    app = FastAPI(title="RIGOL DS1000Z LAN API", version="0.1.0", lifespan=lifespan,
                  description="Fast raw-socket control of RIGOL DS1000Z oscilloscopes. "
                              "WebSocket /ws/stream streams screen waveforms.")
    app.state.lab = lab
    app.state.broadcaster = bc

    @app.exception_handler(ChannelOff)
    async def _off(request: Request, exc: ChannelOff):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ScpiTimeout)
    async def _timeout(request: Request, exc: ScpiTimeout):
        return JSONResponse({"detail": str(exc)}, status_code=504)

    @app.exception_handler(ScpiError)
    async def _scpi(request: Request, exc: ScpiError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ValueError)
    async def _value(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def _key(request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    def scope():
        return lab.scope

    async def th(fn, *a, **k):
        return await run_in_threadpool(fn, *a, **k)

    def _chans(channels: str | None) -> list[int] | None:
        return [int(c) for c in channels.split(",") if c.strip()] if channels else None

    # ----------------------------------------------------------- general
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return resources.files("rigol_ds1000z").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/api/info")
    async def info():
        s = await th(scope)
        return {"host": s.host, **(await th(s.idn))}

    @app.get("/api/status")
    async def status():
        s = await th(scope)
        return await th(s.status)

    @app.get("/api/errors")
    async def errors():
        s = await th(scope)
        return await th(s.errors)

    @app.post("/api/connect")
    async def connect(host: str | None = None):
        return await th(lab.connect, host)

    @app.get("/api/discover")
    async def discover_(network: str | None = None):
        from .discovery import discover

        return await th(discover, network)

    @app.post("/api/control/{action}")
    async def control(action: Literal["run", "stop", "single", "force", "autoscale", "clear"]):
        s = await th(scope)
        fn = {"run": s.run, "stop": s.stop, "single": s.single, "force": s.force_trigger,
              "autoscale": s.autoscale, "clear": s.clear}[action]
        await th(fn)
        return {"done": action, "trigger_status": await th(s.trigger_status)}

    # ---------------------------------------------------------- settings
    @app.patch("/api/channels/{ch}")
    async def set_channel(ch: int, body: ChannelSettings):
        s = await th(scope)
        await th(s.set_channel, ch, **body.model_dump())
        return (await th(s.status))["channels"][ch]

    @app.patch("/api/timebase")
    async def set_timebase(body: TimebaseSettings):
        s = await th(scope)
        await th(s.set_timebase, **body.model_dump())
        st = await th(s.status)
        return {k: st[k] for k in ("timebase_scale", "timebase_offset", "timebase_mode", "sample_rate")}

    @app.patch("/api/trigger")
    async def set_trigger(body: TriggerSettings):
        s = await th(scope)
        await th(s.set_trigger, **body.model_dump())
        st = await th(s.status)
        return {k: v for k, v in st.items() if k.startswith("trigger")}

    @app.patch("/api/acquire")
    async def set_acquire(body: AcquireSettings):
        s = await th(scope)
        await th(s.set_acquire, **body.model_dump())
        st = await th(s.status)
        return {k: st[k] for k in ("memory_depth", "sample_rate", "acquire_type", "averages")}

    # --------------------------------------------------------- waveforms
    def _wave_response(wfs: dict, fmt: str):
        if fmt == "csv":
            chans = list(wfs)
            first = wfs[chans[0]]
            n = min(len(w) for w in wfs.values())
            cols = [first.times[:n]] + [wfs[c].volts[:n] for c in chans]
            lines = ["time_s," + ",".join(f"ch{c}_V" for c in chans)]
            lines += [",".join(f"{x:.9g}" for x in row) for row in np.column_stack(cols)]
            return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv")
        if fmt == "bin":
            body = b"".join(frame_bytes(w, i) for i, w in enumerate(wfs.values()))
            return Response(body, media_type="application/octet-stream")
        return {str(c): {**w.to_dict(include_volts=True), "analysis": w.analyze()} for c, w in wfs.items()}

    @app.get("/api/waveform/{ch}")
    async def waveform(ch: int, mode: Literal["NORM", "MAX"] = "NORM",
                       format: Literal["json", "csv", "bin"] = "json"):
        """One channel's screen data (1200 points).  ~3 ms on the selected
        channel; +50 ms when switching channel."""
        s = await th(scope)
        wf = await th(s.read_waveform, ch, mode)
        return _wave_response({ch: wf}, format)

    @app.get("/api/waveforms")
    async def waveforms(channels: str | None = Query(None, description="e.g. 1,2,3,4; default: displayed"),
                        snapshot: bool = Query(False, description="STOP, read time-aligned, RUN"),
                        format: Literal["json", "csv", "bin"] = "json"):
        s = await th(scope)
        wfs = await th(s.read_waveforms, _chans(channels), snapshot)
        return _wave_response(wfs, format)

    @app.get("/api/raw/{ch}")
    async def raw(ch: int, start: int = 1, stop: int | None = None,
                  format: Literal["bin", "npy", "csv"] = "bin"):
        """Acquisition memory (stops the scope while reading, ~1.5 Mpts/s).
        bin: one binary frame (see /ws/stream), npy: float32 volts."""
        s = await th(scope)
        wf = await th(s.read_raw, ch, start, stop)
        if format == "npy":
            import io

            buf = io.BytesIO()
            np.save(buf, wf.volts)
            return Response(buf.getvalue(), media_type="application/octet-stream",
                            headers={"X-Dx": str(wf.preamble.xincrement),
                                     "X-X0": str(wf.preamble.xorigin)})
        return _wave_response({ch: wf}, format)

    @app.get("/api/analyze")
    async def analyze(channels: str | None = None, snapshot: bool = False):
        """Host-side measurements on screen data (no 5-item limit)."""
        s = await th(scope)
        wfs = await th(s.read_waveforms, _chans(channels), snapshot)
        return {str(c): w.analyze() for c, w in wfs.items()}

    @app.get("/api/measure")
    async def measure(items: str = Query(..., description="e.g. VPP,FREQ,VRMS"), ch: str = "1"):
        """Scope-side measurements; fast while <= 5 distinct item/source pairs are in use."""
        s = await th(scope)
        return await th(s.measure, items, ch)

    @app.get("/api/screenshot")
    async def screenshot(format: Literal["PNG", "BMP24", "BMP8", "JPEG", "TIFF"] = "PNG"):
        s = await th(scope)
        return Response(await th(s.screenshot, format), media_type=SCREENSHOT_FORMATS[format])

    @app.post("/api/scpi")
    async def scpi(body: ScpiCommand):
        s = await th(scope)
        r = await th(s.scpi, body.command)
        if isinstance(r, bytes):
            return Response(r, media_type="application/octet-stream")
        return {"reply": r}

    @app.get("/api/stream/stats")
    async def stream_stats():
        return {"subscribers": len(bc.subs), "frames_per_s": round(bc.fps, 2), "error": bc.error}

    # --------------------------------------------------------- websocket
    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket, channels: str = "1,2,3,4", format: Literal["bin", "json"] = "bin"):
        """Pushes every new screen frame of the requested (displayed) channels.

        bin: [uint32 LE header length][JSON header][uint8 samples]
             volts = (sample - yoffset) * yinc, t = x0 + i * dx
        json: {"ch", "seq", "ts", "x0", "dx", "volts": [...]}"""
        await ws.accept()
        sid, q = bc.subscribe(set(_chans(channels) or [1, 2, 3, 4]))

        async def watch_close():
            try:
                while True:
                    if (await ws.receive())["type"] == "websocket.disconnect":
                        return
            except Exception:
                return

        closed = asyncio.create_task(watch_close())
        try:
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    getter.cancel()
                    break
                data = getter.result()
                if format == "json":
                    hl = struct.unpack_from("<I", data)[0]
                    head = json.loads(data[4:4 + hl])
                    raw = np.frombuffer(data[4 + hl:], dtype=np.uint8)
                    head["volts"] = np.round((raw.astype(np.float32) - head.pop("yoffset")) * head.pop("yinc"),
                                             5).tolist()
                    await ws.send_json(head)
                else:
                    await ws.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            closed.cancel()
            bc.unsubscribe(sid)

    return app
