"""A TCP fake of the DS1000Z SCPI socket reproducing the quirks observed on a
real DS1104Z (FW 00.04.04.SP3):

* replies of one program message are joined with ';' and end with one LF;
* a failing measurement answers ``measure error!`` plus an extra LF;
* binary data as ``#9<len><bytes>`` LF, optionally after text replies;
* RAW reads of more than 1,000,000 points return an empty block.
"""

from __future__ import annotations

import socket
import threading

import numpy as np

SCREEN = 1200


class FakeScope:
    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.sets: list[str] = []  # every set command received
        self.queries: list[str] = []
        self.running = True
        self.empty_reads = 0  # next N screen reads return no data (post-RUN behaviour)
        self.mdep = 24_000
        self.disp = {1: True, 2: True, 3: False, 4: False}
        self.wav = {"SOUR": "CHAN1", "MODE": "NORM", "FORM": "BYTE", "STAR": 1, "STOP": SCREEN}
        self.chan = {c: {"SCAL": 1.0, "OFFS": 0.0, "COUP": "DC", "PROB": 10.0, "BWL": "OFF", "INV": "0",
                         "UNIT": "VOLT"} for c in range(1, 5)}
        n = np.arange(self.mdep)
        # CH1: 1 kHz-like square wave, CH2: sine; codes centred on 127.
        self.mem = {1: np.where((n // 100) % 2 == 0, 177, 77).astype(np.uint8),
                    2: (127 + 50 * np.sin(2 * np.pi * n / 300)).astype(np.uint8)}
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    def close(self):
        self._stop = True
        self.srv.close()

    # ------------------------------------------------------------------ server
    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        buf = b""
        with conn:
            while True:
                try:
                    d = conn.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    out = self._message(line.decode().strip())
                    if out:
                        conn.sendall(out)

    def _message(self, msg: str) -> bytes:
        texts: list[str] = []
        block: bytes | None = None
        for cmd in [c.strip() for c in msg.split(";") if c.strip()]:
            if "?" in cmd:
                self.queries.append(cmd)
                r = self._query(cmd)
                if isinstance(r, bytes):
                    block = r
                else:
                    texts.append(r)
            else:
                self.sets.append(cmd)
                self._set(cmd)
        if block is None and not texts:
            return b""
        text = ";".join(t + ("\n" if t == "measure error!" else "") for t in texts)
        if block is not None:
            prefix = (text + ";") if texts else ""
            return prefix.encode() + b"#9%09d" % len(block) + block + b"\n"
        return text.encode() + b"\n"

    # ---------------------------------------------------------------- commands
    def _set(self, cmd: str):
        head, _, arg = cmd.partition(" ")
        h = head.upper().lstrip(":")
        if h == "RUN":
            self.running = True
        elif h in ("STOP", "SING"):
            self.running = False
        elif h.startswith("WAV:"):
            key = h.split(":")[1][:4]
            self.wav[key] = int(arg) if key in ("STAR", "STOP") else arg.upper()
        elif h.startswith("CHAN"):
            c = int(h[4])
            key = h.split(":")[1][:4]
            if key == "DISP":
                self.disp[c] = arg.upper() in ("ON", "1")
            else:
                self.chan[c][key] = arg

    def _pre(self) -> str:
        if self.wav["MODE"] == "RAW":
            return f"0,2,{self.mdep},1,1.000000e-06,-1.200000e-02,0,4.000000e-02,0,127"
        return "0,0,1200,1,1.000000e-05,-6.000000e-03,0,4.000000e-02,0,127"

    def _query(self, cmd: str) -> str | bytes:
        c = cmd.upper().lstrip(":")
        if c == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1104Z,FAKE0001,00.04.04.SP3"
        if c == "*OPC?":
            return "1"
        if c == "SYST:ERR?":
            return '0,"No error"'
        if c == "TRIG:STAT?":
            return "AUTO" if self.running else "STOP"
        if c == "ACQ:MDEP?":
            return str(self.mdep)
        if c == "ACQ:SRAT?":
            return "1.000000e+06"
        if c.startswith("CHAN") and c.endswith("DISP?"):
            return "1" if self.disp[int(c[4])] else "0"
        if c.startswith("CHAN"):
            v = self.chan[int(c[4])][c.split(":")[1].rstrip("?")[:4]]
            return str(v)
        if c.startswith("WAV:PRE?"):
            return self._pre()
        if c.startswith("WAV:DATA?"):
            ch = int(self.wav["SOUR"][-1])
            if self.wav["MODE"] == "RAW":
                if self.running:
                    return b""
                a, b = self.wav["STAR"], self.wav["STOP"]
                if b - a + 1 > 1_000_000:
                    return b""
                return self.mem[ch][a - 1 : b].tobytes()
            if self.empty_reads:
                self.empty_reads -= 1
                return b""
            return self.mem[ch][:SCREEN].tobytes()
        if c.startswith("WAV:"):
            key = c.split(":")[1][:4]
            return str(self.wav[key])
        if c.startswith("MEAS:ITEM?"):
            item, _, src = c.split(" ", 1)[1].partition(",")
            ch = int(src[-1]) if src else 1
            if not self.disp.get(ch):
                return "9.9E37"
            return {"VPP": "4.000000e+00", "FREQ": "1.000000e+03", "VRMS": "2.000000e+00"}.get(item, "measure error!")
        if c.startswith("DISP:DATA?"):
            return b"\x89PNG\r\n\x1a\nfake"
        # Global settings used by status()
        return {"TIM:SCAL?": "1.000000e-03", "TIM:OFFS?": "0.000000e+00", "TIM:MODE?": "MAIN",
                "ACQ:TYPE?": "NORM", "ACQ:AVER?": "2", "TRIG:MODE?": "EDGE", "TRIG:SWE?": "AUTO",
                "TRIG:COUP?": "DC", "TRIG:HOLD?": "1.600000e-08", "TRIG:EDG:SOUR?": "CHAN1",
                "TRIG:EDG:SLOP?": "POS", "TRIG:EDG:LEV?": "0.000000e+00"}.get(c, "0")
