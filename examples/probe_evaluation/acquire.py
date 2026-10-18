"""Step 1: acquire the CH1 test signal at various scales for the probe evaluation.
Saves captures + scope measurements to <work>/probe_eval.pkl and restores the
front-panel settings at the end.

    python examples/probe_evaluation/acquire.py [--host IP] [--work DIR]
"""

import argparse
import pickle
import time
from pathlib import Path

from rigol_ds1000z.lab import Lab

ap = argparse.ArgumentParser()
ap.add_argument("--host", help="scope IP (default: $RIGOL_HOST or auto-discover)")
ap.add_argument("--work", default="rigol_data/probe_eval")
args = ap.parse_args()
work = Path(args.work)
work.mkdir(parents=True, exist_ok=True)
lab = Lab(args.host, work)
s = lab.scope
orig = s.status()
print("orig saved")
results = []


def settle(sec=0.6):
    s.query("*OPC?")
    time.sleep(sec)


def shot(group, label, items, settle_s=0.6, scope_src=1):
    settle(settle_s)
    st = s.status()
    meas = s.measure(items, scope_src)
    meas = s.measure(items, scope_src)  # second call: values from the now cached items
    cap = lab.capture([1], note=label)
    errs = s.errors()
    a = cap.waveforms[1].analyze()
    print(f"[{group}] {label:28s} V/div={st['channels'][1]['scale']:g} off={st['channels'][1]['offset']:g} "
          f"t/div={st['timebase_scale']:g} srate={st['sample_rate']:.3g} vpp={a['vpp']:.3f} f={a['freq']} "
          f"meas={meas} err={errs}")
    results.append({"group": group, "label": label, "status": st, "measure": meas, "capture": cap, "errors": errs})


try:
    s.set_trigger(source=1, level=1.5, slope="POS", sweep="AUTO")
    s.set_acquire(type="NORM")

    # 1. Vertical scale sweep at 200 us/div
    s.set_timebase(scale=200e-6, offset=0)
    for vdiv, off in ((5.0, 0.0), (2.0, -1.5), (1.0, -1.5), (0.5, -1.5)):
        s.set_channel(1, scale=vdiv, offset=off)
        shot("vscale", f"{vdiv:g} V/div", "VTOP,VBAS,VAMP,FREQ,PDUT")

    # 2. Timebase sweep, rising edge at screen centre, 0.5 V/div
    s.set_channel(1, scale=0.5, offset=-1.5)
    for tdiv in (1e-3, 200e-6, 20e-6, 2e-6, 200e-9, 50e-9):
        s.set_timebase(scale=tdiv, offset=0)
        shot("tscale", f"rise {tdiv * 1e6:g} us/div", "RTIM,OVER,PRES,VAMP,FREQ")

    # 2b. Falling edge
    s.set_trigger(slope="NEG")
    for tdiv in (2e-6, 200e-9, 50e-9):
        s.set_timebase(scale=tdiv, offset=0)
        shot("fall", f"fall {tdiv * 1e6:g} us/div", "FTIM,OVER,PRES,VAMP,FREQ")

    # 3. Classic compensation view: ~2 periods, 0.5 V/div, averaged
    s.set_trigger(slope="POS", level=1.5)
    s.set_timebase(scale=100e-6, offset=0)
    s.set_acquire(type="AVER", averages=16)
    shot("comp", "comp view 100us/div AVG16", "OVER,PRES,VTOP,VBAS,VAMP", settle_s=3)

    # 3b. Top plateau zoom: 0.1 V/div centred on the top level, edge near the left
    top = results[-1]["measure"]["VTOP"] or 2.94
    base = results[-1]["measure"]["VBAS"] or -0.05
    s.set_channel(1, scale=0.1, offset=-top)
    s.set_timebase(scale=50e-6, offset=250e-6)
    s.set_trigger(slope="POS", level=round(top - 0.25, 3))
    shot("plateau", "top plateau 0.1V/div AVG16", "VTOP,VMAX,OVER,VAMP,FREQ", settle_s=4)

    # 3c. Bottom plateau zoom
    s.set_channel(1, scale=0.1, offset=-base)
    s.set_trigger(slope="NEG", level=round(base + 0.25, 3))
    shot("plateau", "bottom plateau 0.1V/div AVG16", "VBAS,VMIN,PRES,VAMP,FREQ", settle_s=4)

    # Screenshot of the classic view for the report
    s.set_acquire(type="NORM")
    s.set_channel(1, scale=0.5, offset=-1.5)
    s.set_timebase(scale=200e-6, offset=0)
    s.set_trigger(slope="POS", level=1.5)
    settle(1.0)
    shot_png = s.screenshot("PNG")
finally:
    # Restore the original front-panel state
    c = orig["channels"][1]
    s.set_acquire(type=orig["acquire_type"], averages=orig["averages"])
    s.set_channel(1, scale=c["scale"], offset=c["offset"])
    s.set_timebase(scale=orig["timebase_scale"], offset=orig["timebase_offset"])
    s.set_trigger(source=1, level=orig["trigger_level"], slope=orig["trigger_slope"], sweep=orig["trigger_sweep"])
    s.run()
    now = s.status()
    diff = {k: (orig[k], now[k]) for k in orig if k not in ("channels", "trigger_status") and orig[k] != now[k]}
    cdiff = {k: (c[k], now["channels"][1][k]) for k in c if c[k] != now["channels"][1][k]}
    print("restored; remaining diffs:", diff, cdiff, s.errors())

with open(work / "probe_eval.pkl", "wb") as f:
    pickle.dump({"idn": s.idn(), "orig": orig, "results": results, "screenshot": shot_png}, f)
print("saved", work / "probe_eval.pkl")
