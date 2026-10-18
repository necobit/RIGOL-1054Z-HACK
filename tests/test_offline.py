"""Offline tests against tests/fake_scope.py (no instrument needed)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fake_scope import FakeScope  # noqa: E402

from rigol_ds1000z import DS1000Z, ChannelOff, analyze  # noqa: E402
from rigol_ds1000z.discovery import _ARP_RE, _norm_mac  # noqa: E402
from rigol_ds1000z.transport import ScpiTransport  # noqa: E402
from rigol_ds1000z.waveform import Preamble  # noqa: E402


@pytest.fixture
def fake():
    f = FakeScope()
    yield f
    f.close()


@pytest.fixture
def scope(fake):
    s = DS1000Z(fake.host, fake.port, timeout=2).connect()
    yield s
    s.close()


# ----------------------------------------------------------------- transport
def test_measure_error_extra_newline_does_not_shift_replies(fake):
    t = ScpiTransport(fake.host, fake.port, 2)
    assert t.query(":MEAS:ITEM? RTIM,CHAN1") == "measure error!"
    # The next reply must not be the stray empty line.
    assert t.query("*IDN?").startswith("RIGOL")
    assert t.query_many([":MEAS:ITEM? VPP,CHAN1", ":MEAS:ITEM? RTIM,CHAN1", ":MEAS:ITEM? VRMS,CHAN1"]) == [
        "4.000000e+00", "measure error!", "2.000000e+00"]
    assert t.query("*OPC?") == "1"


def test_block_with_text_prefix(fake):
    t = ScpiTransport(fake.host, fake.port, 2)
    text, data = t.query_block(":WAV:SOUR?;:WAV:PRE?;:WAV:DATA?")
    sour, pre = text.split(";", 1)
    assert sour == "CHAN1"
    assert Preamble.parse(pre).points == 1200
    assert len(data) == 1200
    assert t.query("*OPC?") == "1"  # stream still in sync after the trailing LF


def test_query_any_detects_binary(fake):
    t = ScpiTransport(fake.host, fake.port, 2)
    assert isinstance(t.query_any(":DISP:DATA? ON,OFF,PNG"), bytes)
    assert t.query_any("*IDN?").startswith("RIGOL")


# ------------------------------------------------------------------- scope
def test_repeated_reads_send_no_set_commands(scope, fake):
    scope.read_waveform(1)
    n = len(fake.sets)
    for _ in range(5):
        wf = scope.read_waveform(1)
    assert len(fake.sets) == n, fake.sets[n:]
    assert len(wf) == 1200
    assert wf.volts.max() == pytest.approx((177 - 127) * 0.04)


def test_channel_switch_costs_one_set(scope, fake):
    scope.read_waveform(1)
    n = len(fake.sets)
    scope.read_waveform(2)
    assert fake.sets[n:] == [":WAV:SOUR CHAN2"]


def test_source_changed_by_other_client_is_detected(scope, fake):
    scope.read_waveform(1)
    fake.wav["SOUR"] = "CHAN2"  # e.g. another program selected CH2
    wf = scope.read_waveform(1)
    assert wf.channel == "CHAN1"
    assert fake.wav["SOUR"] == "CHAN1"


def test_hidden_channel_raises(scope):
    with pytest.raises(ChannelOff):
        scope.read_waveform(3)


def test_empty_screen_after_run_is_retried(scope, fake):
    scope.read_waveform(1)
    fake.empty_reads = 3
    assert len(scope.read_waveform(1)) == 1200


def test_raw_read_chunks_and_restores_norm(scope, fake):
    wf = scope.read_raw(1, chunk=10_000)
    assert len(wf) == fake.mdep
    assert np.array_equal(wf.raw, fake.mem[1])
    # STAR/STOP ordering must never make start > stop (firmware lock-up risk).
    star, stop = 1, 1200
    for cmd in fake.sets:
        if cmd.startswith(":WAV:STAR"):
            star = int(cmd.split()[1])
        elif cmd.startswith(":WAV:STOP"):
            stop = int(cmd.split()[1])
        assert star <= stop, (cmd, star, stop)
    assert fake.wav["MODE"] == "NORM" and (fake.wav["STAR"], fake.wav["STOP"]) == (1, 1200)
    assert fake.running  # resumed


def test_raw_partial_range_time_axis(scope):
    wf = scope.read_raw(1, start=101, stop=300)
    assert len(wf) == 200
    assert wf.times[0] == pytest.approx(-1.2e-2 + 100 * 1e-6)


def test_raw_rejects_out_of_range(scope):
    with pytest.raises(ValueError):
        scope.read_raw(1, start=0)
    with pytest.raises(ValueError):
        scope.read_raw(1, stop=10**9)


def test_measure_parsing(scope):
    r = scope.measure("VPP,FREQ,RTIM", 1)
    assert r == {"VPP": 4.0, "FREQ": 1000.0, "RTIM": None}
    assert scope.measure(["VPP"], 3) == {"VPP": None}  # 9.9E37 -> None
    with pytest.raises(ValueError):
        scope.measure("NOPE")


def test_status_one_shot(scope):
    st = scope.status()
    assert st["memory_depth"] == 24_000
    assert st["channels"][1]["display"] is True
    assert st["channels"][3]["display"] is False


def test_stream_only_new_dedups(scope):
    got = []
    for wf in scope.stream([1, 2], only_new=True):
        got.append(wf.channel)
        if len(got) >= 2:
            break
    assert sorted(got) == ["CHAN1", "CHAN2"]


def test_set_channel_validates(scope, fake):
    with pytest.raises(ValueError):
        scope.set_channel(1, coupling="XX")
    with pytest.raises(ValueError):
        scope.set_channel(1, probe=3)
    scope.set_channel(1, probe=1, scale=0.5)
    scope.query("*OPC?")  # writes are fire-and-forget; sync before checking
    assert fake.sets[-2:] == [":CHAN1:PROB 1", ":CHAN1:SCAL 5.000000e-01"]


# ---------------------------------------------------------------- analysis
def test_analyze_square_wave():
    dt = 1e-5
    t = np.arange(1200) * dt
    v = np.where(np.sin(2 * np.pi * 1000 * t) >= 0, 3.0, 0.0) + np.random.default_rng(0).normal(0, 0.05, t.size)
    a = analyze(v, dt)
    assert a["freq"] == pytest.approx(1000, rel=0.01)
    assert a["duty"] == pytest.approx(0.5, abs=0.02)
    assert a["vpp"] == pytest.approx(3.0, abs=0.4)


def test_analyze_dc_has_no_frequency():
    a = analyze(np.full(100, 1.0), 1e-6)
    assert a["freq"] is None and a["vpp"] == 0


def test_arp_parsing():
    line = "? (169.254.51.130) at 0:19:af:35:82:32 on en13 [ethernet]"
    ip, mac = _ARP_RE.findall(line)[0]
    assert ip == "169.254.51.130" and _norm_mac(mac).startswith("00:19:af")


# ------------------------------------------------------------------ server
def test_rest_api(fake, tmp_path):
    from fastapi.testclient import TestClient

    from rigol_ds1000z.lab import Lab
    from rigol_ds1000z.server import create_app

    lab = Lab(fake.host, tmp_path)
    lab._scope = DS1000Z(fake.host, fake.port, timeout=2).connect()
    with TestClient(create_app(lab)) as c:
        assert c.get("/api/info").json()["model"] == "DS1104Z"
        w = c.get("/api/waveform/1").json()["1"]
        assert w["points"] == 1200 and len(w["volts"]) == 1200
        assert c.get("/api/waveform/3").status_code == 409
        assert c.get("/api/measure", params={"items": "VPP"}).json() == {"VPP": 4.0}
        assert c.get("/api/measure", params={"items": "BAD"}).status_code == 422
        assert c.post("/api/scpi", json={"command": "*IDN?"}).json()["reply"].startswith("RIGOL")
        assert c.get("/api/waveforms", params={"format": "csv"}).text.startswith("time_s,ch1_V,ch2_V")
        with c.websocket_connect("/ws/stream?channels=1") as ws:
            data = ws.receive_bytes()
            assert len(data) > 1200


def test_lab_capture_and_report(fake, tmp_path):
    pytest.importorskip("matplotlib")
    from rigol_ds1000z.lab import Lab

    lab = Lab(fake.host, tmp_path)
    lab._scope = DS1000Z(fake.host, fake.port, timeout=2).connect()
    cap = lab.capture([1, 2])
    assert set(cap.waveforms) == {1, 2}
    rep = lab.report_create("Test report")
    lab.report_add_text(rep.id, "## Summary\n- one\n- **two**")
    lab.report_add_capture(rep.id, cap.id)
    out = lab.report_finish(rep.id)
    html = Path(out["html"]).read_text()
    assert "<h3>Summary</h3>" in html and "data:image/png;base64" in html
    assert Path(lab.export_capture(cap.id)).read_text().startswith("time_s,ch1_V,ch2_V")
