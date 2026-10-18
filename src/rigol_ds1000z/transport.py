"""Raw-socket SCPI transport for RIGOL DS1000Z (TCP port 5555).

Behaviour of the instrument measured on a DS1104Z / FW 00.04.04.SP3, which
shapes the design of this module:

* Queries are cheap (~1-3 ms round trip).  Set commands are not: every set
  command keeps the instrument busy for ~50 ms, whether sent alone or batched.
* Several queries joined with ``;`` (or sent back to back) are answered with a
  single ``;``-separated line, so N values cost one round trip.
* A failing ``:MEAS:ITEM?`` answers ``measure error!`` followed by an *extra*
  ``\\n``.  Unless that newline is swallowed every later reply is shifted.
* Binary data arrives as an IEEE 488.2 definite-length block
  (``#9000001200<bytes>\\n``).
* Sending an unterminated command, or ``:WAV:STAR``/``:WAV:STOP`` out of range,
  can lock the firmware up until a power cycle, so every command is LF
  terminated here and range checks live in the higher layer.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Iterable, Sequence

DEFAULT_PORT = 5555
MEASURE_ERROR = "measure error!"


class ScpiError(Exception):
    """Base error for instrument communication."""


class ScpiTimeout(ScpiError):
    """The instrument did not answer in time."""


class ScpiTransport:
    """Thread-safe, persistent raw socket connection.

    All public methods take ``self.lock`` so one instance can be shared between
    threads (e.g. an API server and a streaming loop).  ``lock`` is re-entrant,
    so callers may also hold it around several calls to make them atomic.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.lock = threading.RLock()
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self.reconnects = 0
        # Called after a reconnect so the owner can drop cached instrument state.
        self.on_reconnect = None

    # ------------------------------------------------------------------ socket
    def connect(self) -> None:
        with self.lock:
            self.close()
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self._sock = sock
            self._buf.clear()
            self._drain()

    def close(self) -> None:
        with self.lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None
            self._buf.clear()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        return self._sock

    def _drain(self, wait: float = 0.15) -> None:
        """Discard stale bytes left over from an interrupted transaction."""
        sock = self._sock
        if sock is None:
            return
        sock.settimeout(wait)
        try:
            while sock.recv(65536):
                pass
        except (socket.timeout, OSError):
            pass
        sock.settimeout(self.timeout)
        self._buf.clear()

    def _recover(self) -> None:
        """Reconnect after a timeout/desync; the reply stream can't be trusted."""
        self.reconnects += 1
        try:
            self.connect()
        except OSError:
            self.close()
        if self.on_reconnect:
            self.on_reconnect()

    # --------------------------------------------------------------- low level
    def _send(self, data: bytes) -> None:
        self._ensure().sendall(data)

    def _fill(self, deadline: float, bufsize: int = 1 << 20) -> None:
        sock = self._ensure()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScpiTimeout(f"timeout waiting for {self.host}")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(bufsize)
        except socket.timeout as e:
            raise ScpiTimeout(f"timeout waiting for {self.host}") from e
        if not chunk:
            raise ScpiError("connection closed by instrument")
        self._buf += chunk

    def _read_line(self, deadline: float) -> str:
        buf = self._buf
        start = 0
        while True:
            i = buf.find(b"\n", start)
            if i >= 0:
                break
            start = len(buf)
            self._fill(deadline, 65536)
        line = bytes(buf[:i]).decode("ascii", "replace")
        del buf[: i + 1]
        return line

    def _read_reply(self, deadline: float) -> str:
        """Read one reply line, re-joining lines split by ``measure error!``."""
        line = part = self._read_line(deadline)
        while part.endswith(MEASURE_ERROR):
            # "measure error!" carries its own "\n"; the real terminator follows
            # ("" for a lone error, ";next;values" when more replies follow).
            part = self._read_line(deadline)
            line += part
        return line

    def _read_block(self, deadline: float) -> tuple[str, bytes]:
        """Read ``[text;]#N<len><data>\\n``; returns (text prefix, data)."""
        buf = self._buf
        while True:
            i = buf.find(b"#")
            if i >= 0 and len(buf) >= i + 2:
                break
            self._fill(deadline, 65536)
        prefix = bytes(buf[:i]).decode("ascii", "replace").rstrip(";")
        ndig_c = buf[i + 1]
        if not 0x31 <= ndig_c <= 0x39:
            raise ScpiError(f"malformed block header: {bytes(buf[i:i + 12])!r}")
        ndig = ndig_c - 0x30
        while len(buf) < i + 2 + ndig:
            self._fill(deadline, 65536)
        length = int(buf[i + 2 : i + 2 + ndig])
        start = i + 2 + ndig
        end = start + length
        if len(buf) < end + 1:
            # Read straight into a preallocated buffer for large transfers.
            out = bytearray(end + 1)
            out[: len(buf)] = buf
            view = memoryview(out)
            pos = len(buf)
            sock = self._ensure()
            while pos < end + 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScpiTimeout(f"timeout reading {length} byte block")
                sock.settimeout(remaining)
                try:
                    n = sock.recv_into(view[pos:], min(end + 1 - pos, 4 << 20))
                except socket.timeout as e:
                    raise ScpiTimeout(f"timeout reading {length} byte block") from e
                if n == 0:
                    raise ScpiError("connection closed by instrument")
                pos += n
            data = bytes(out[start:end])
            self._buf = bytearray(out[end:])
        else:
            data = bytes(buf[start:end])
            del buf[:end]
        # Swallow the terminating LF.
        if self._buf[:1] == b"\n":
            del self._buf[:1]
        return prefix, data

    def _transact(self, fn, timeout: float | None):
        deadline = time.monotonic() + (timeout or self.timeout)
        with self.lock:
            try:
                return fn(deadline)
            except (ScpiError, OSError):
                self._recover()
                raise

    # -------------------------------------------------------------- public API
    def write(self, *commands: str) -> None:
        """Send one or more set commands (each costs the instrument ~50 ms)."""
        if not commands:
            return
        payload = "".join(c.strip() + "\n" for c in commands).encode("ascii")
        with self.lock:
            try:
                self._send(payload)
            except OSError:
                self._recover()
                raise

    def query(self, command: str, timeout: float | None = None) -> str:
        def run(deadline):
            self._send((command.strip() + "\n").encode("ascii"))
            return self._read_reply(deadline)

        return self._transact(run, timeout)

    def query_many(self, commands: Sequence[str], timeout: float | None = None) -> list[str]:
        """Run several queries in one round trip.  Replies must not contain ';'."""
        if not commands:
            return []
        reply = self.query(";".join(c.strip() for c in commands), timeout)
        parts = reply.split(";")
        if len(parts) != len(commands):
            with self.lock:
                self._recover()
            raise ScpiError(f"expected {len(commands)} replies, got {len(parts)}: {reply!r}")
        return parts

    def query_block(self, command: str, timeout: float | None = None) -> tuple[str, bytes]:
        """Query returning a binary block, optionally preceded by text replies
        (e.g. ``":WAV:PRE?;:WAV:DATA?"``).  Returns (text, data)."""

        def run(deadline):
            self._send((command.strip() + "\n").encode("ascii"))
            return self._read_block(deadline)

        return self._transact(run, timeout)

    def query_any(self, command: str, timeout: float | None = None) -> str | bytes:
        """Query whose reply may be text or a binary block (decided by the
        first byte).  Used for raw SCPI pass-through."""

        def run(deadline):
            self._send((command.strip() + "\n").encode("ascii"))
            while not self._buf:
                self._fill(deadline, 65536)
            if self._buf[:1] == b"#":
                return self._read_block(deadline)[1]
            return self._read_reply(deadline)

        return self._transact(run, timeout)

    def __enter__(self):
        self._ensure()
        return self

    def __exit__(self, *exc):
        self.close()


def join_queries(commands: Iterable[str]) -> str:
    return ";".join(c.strip() for c in commands)
