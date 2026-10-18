"""Command line entry point: ``rigol-ds <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _host(args) -> str:
    if args.host:
        return args.host
    if os.environ.get("RIGOL_HOST"):
        return os.environ["RIGOL_HOST"]
    from .discovery import discover

    found = discover()
    if not found:
        sys.exit("no RIGOL scope found; pass --host or set RIGOL_HOST")
    print(f"# using {found[0]['model']} at {found[0]['host']}", file=sys.stderr)
    return found[0]["host"]


def cmd_discover(args):
    from .discovery import discover

    for d in discover(args.network):
        print(f"{d['host']:16s} {d['model']:10s} {d['serial']:16s} FW {d['firmware']}")


def cmd_info(args):
    from .scope import DS1000Z

    with DS1000Z(_host(args)) as s:
        print(json.dumps({"idn": s.idn(), "status": s.status()}, indent=2, ensure_ascii=False))


def cmd_bench(args):
    """Measure achievable rates on this setup."""
    from .scope import DS1000Z

    with DS1000Z(_host(args)) as s:
        print(s.idn())
        shown = s.displayed_channels()
        print(f"displayed channels: {shown}")
        if not shown:
            sys.exit("no channel displayed")

        def timed(label, fn, n):
            fn()
            t = time.perf_counter()
            for _ in range(n):
                fn()
            dt = (time.perf_counter() - t) / n
            print(f"{label:38s} {dt * 1000:8.2f} ms  {1 / dt:8.1f} /s")

        timed("query :TRIG:STAT?", s.trigger_status, 50)
        timed("status() (all settings)", s.status, 10)
        timed(f"read_waveform(CH{shown[0]}) 1200 pts", lambda: s.read_waveform(shown[0]), 100)
        timed(f"measure 3 items CH{shown[0]}", lambda: s.measure("VPP,FREQ,VRMS", shown[0]), 30)

        for chans in ([shown[0]], shown) if len(shown) > 1 else ([shown[0]],):
            for only_new in (False, True):
                n, t = 0, time.perf_counter()
                for _ in s.stream(chans, only_new=only_new):
                    n += 1
                    if time.perf_counter() - t > args.seconds:
                        break
                dt = time.perf_counter() - t
                kind = "new frames" if only_new else "reads"
                print(f"stream {str(chans):20s} {kind:10s}  {n / dt:8.1f} /s total, {n / dt / len(chans):6.1f} /s per ch")
        if len(shown) > 1:
            timed(f"read_waveforms({shown}, snapshot)", lambda: s.read_waveforms(shown, snapshot=True), 3)
        if args.raw:
            t = time.perf_counter()
            wf = s.read_raw(shown[0], stop=args.raw)
            dt = time.perf_counter() - t
            print(f"read_raw {len(wf):,} pts: {dt:.2f} s  ({len(wf) / dt / 1e6:.2f} Mpts/s)")
        print("errors:", s.errors() or "none")


def cmd_capture(args):
    from .lab import Lab

    lab = Lab(_host(args), args.out)
    chans = [int(c) for c in args.channels.split(",")] if args.channels else None
    cap = lab.capture(chans, "raw" if args.raw else "screen", True, args.points)
    print(json.dumps(cap.summary(), indent=2, ensure_ascii=False, default=str))
    print(lab.export_capture(cap.id, "npz" if args.raw else "csv"))
    if args.plot:
        p = lab.export_capture(cap.id, "csv").with_suffix(".png") if not args.raw else lab.data_dir / f"{cap.id}.png"
        p.write_bytes(lab.plot_capture(cap.id))
        print(p)


def cmd_screenshot(args):
    from .scope import DS1000Z

    with DS1000Z(_host(args)) as s:
        data = s.screenshot(args.format)
    out = args.output or time.strftime(f"rigol_%Y%m%d_%H%M%S.{args.format.lower().rstrip('248')}")
    with open(out, "wb") as f:
        f.write(data)
    print(out)


def cmd_serve(args):
    import uvicorn

    from .lab import Lab
    from .server import create_app

    app = create_app(Lab(args.host or os.environ.get("RIGOL_HOST"), args.out))
    uvicorn.run(app, host=args.bind, port=args.port, log_level="info")


def cmd_mcp(args):
    if args.host:
        os.environ["RIGOL_HOST"] = args.host
    if args.out:
        os.environ["RIGOL_DATA_DIR"] = args.out
    from .mcp_server import main

    main()


def main(argv=None):
    p = argparse.ArgumentParser(prog="rigol-ds", description="RIGOL DS1000Z LAN control")
    p.add_argument("--host", help="scope IP (default: $RIGOL_HOST or auto-discover)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="find scopes on local networks")
    s.add_argument("--network", help="also sweep e.g. 192.168.1.0/24")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("info", help="identity and all settings as JSON")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("bench", help="measure achievable rates")
    s.add_argument("--seconds", type=float, default=2.0)
    s.add_argument("--raw", type=int, default=0, help="also time a RAW read of N points")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("capture", help="capture displayed channels to CSV/NPZ")
    s.add_argument("--channels", help="e.g. 1,2")
    s.add_argument("--raw", action="store_true", help="read acquisition memory instead of screen")
    s.add_argument("--points", type=int, help="RAW: number of points (default all)")
    s.add_argument("--plot", action="store_true", help="also save a PNG plot")
    s.add_argument("--out", help="output directory (default ./rigol_data)")
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("screenshot", help="save the scope display")
    s.add_argument("-o", "--output")
    s.add_argument("--format", default="PNG", choices=["PNG", "BMP24", "BMP8", "JPEG", "TIFF"])
    s.set_defaults(fn=cmd_screenshot)

    s = sub.add_parser("serve", help="REST/WebSocket API + live viewer")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--out", help="data directory for captures/reports")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp", help="MCP server (stdio) for LLM agents")
    s.add_argument("--out", help="data directory for captures/reports")
    s.set_defaults(fn=cmd_mcp)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
