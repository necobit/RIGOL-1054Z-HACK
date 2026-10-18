"""Find DS1000Z scopes on the local network without VISA or mDNS libraries.

1. A VXI-11 portmapper GETPORT request is broadcast from every local IPv4
   interface (UDP 111); LXI instruments answer it.  The socket is bound to the
   interface address because macOS otherwise sends 255.255.255.255 out of the
   default route only.  This finds scopes on link-local 169.254.x.x networks
   (direct USB-LAN connection without DHCP), where a /16 is too big to sweep.
2. The ARP cache is scanned for RIGOL's MAC prefix (00:19:AF).
3. Optionally an IPv4 network (e.g. ``192.168.1.0/24``) is swept for TCP 5555.

Every candidate is confirmed with ``*IDN?``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from .transport import DEFAULT_PORT

RIGOL_OUI = "00:19:af"
_ARP_RE = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)")


def _norm_mac(mac: str) -> str:
    return ":".join(p.zfill(2) for p in mac.lower().split(":"))


def arp_candidates() -> list[str]:
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ip for ip, mac in _ARP_RE.findall(out) if _norm_mac(mac).startswith(RIGOL_OUI)]


_IFCONFIG_RE = re.compile(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+|\d+\.\d+\.\d+\.\d+)")


def local_networks() -> list[tuple[str, str]]:
    """(address, broadcast) of every non-loopback IPv4 interface."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    nets = []
    for addr, mask in _IFCONFIG_RE.findall(out):
        if addr.startswith("127."):
            continue
        if mask.startswith("0x"):
            mask = str(ipaddress.IPv4Address(int(mask, 16)))
        net = ipaddress.ip_network(f"{addr}/{mask}", strict=False)
        if net.prefixlen < 32:
            nets.append((addr, str(net.broadcast_address)))
    return nets


def vxi11_broadcast(wait: float = 0.6) -> list[str]:
    """Hosts answering a broadcast portmapper GETPORT for the VXI-11 core."""
    # RPC CALL: xid, CALL, rpcvers 2, portmapper 100000 v2 GETPORT, AUTH_NULL x2,
    # args: program 0x0607AF (VXI-11 core), version 1, TCP, port 0.
    pkt = struct.pack(">14I", 0x52494730, 0, 2, 100000, 2, 3, 0, 0, 0, 0, 0x0607AF, 1, 6, 0)
    socks = []
    for addr, bcast in local_networks():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((addr, 0))
            s.sendto(pkt, (bcast, 111))
            s.setblocking(False)
            socks.append(s)
        except OSError:
            continue
    found: list[str] = []
    deadline = time.monotonic() + wait
    while socks and time.monotonic() < deadline:
        for s in socks:
            try:
                data, (ip, _) = s.recvfrom(512)
            except (BlockingIOError, OSError):
                continue
            if data[:4] == pkt[:4] and ip not in found:
                found.append(ip)
        time.sleep(0.01)
    for s in socks:
        s.close()
    return found


def probe(host: str, port: int = DEFAULT_PORT, timeout: float = 0.5) -> str | None:
    """Return the *IDN? string if a SCPI instrument answers on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(max(timeout, 1.0))
            s.sendall(b"*IDN?\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            idn = buf.decode("ascii", "replace").strip()
            return idn or None
    except OSError:
        return None


def discover(network: str | None = None, port: int = DEFAULT_PORT, timeout: float = 0.4,
             workers: int = 64) -> list[dict]:
    hosts = list(dict.fromkeys(vxi11_broadcast() + arp_candidates()))
    if network:
        net = ipaddress.ip_network(network, strict=False)
        if net.num_addresses > 4096:
            raise ValueError("network too large to sweep (max /20)")
        hosts += [str(h) for h in net.hosts() if str(h) not in hosts]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        idns = list(ex.map(lambda h: probe(h, port, timeout), hosts))
    found = []
    for host, idn in zip(hosts, idns):
        if idn and "RIGOL" in idn.upper():
            maker, model, serial, fw = (idn.split(",") + ["", "", "", ""])[:4]
            found.append({"host": host, "port": port, "model": model, "serial": serial,
                          "firmware": fw, "idn": idn})
    return found
