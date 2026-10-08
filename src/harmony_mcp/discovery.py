"""Finding hubs on the LAN with SSDP (UPnP discovery).

You asked for zeroconf; Harmony hubs don't advertise over mDNS as far as I
can find. Home Assistant's harmony integration discovers them with SSDP,
matching deviceType ``urn:myharmony-com:device:harmony:1`` from manufacturer
Logitech, then confirms each one with aioharmony's provisioning request. This
does the same: one M-SEARCH, collect replies for a few seconds, take the host
from each reply's LOCATION URL.

ASSUMPTION H-SSDP: that the hub *answers* an M-SEARCH for that type. HA could
be learning it from the hub's periodic NOTIFY instead; if `discover` finds
nothing on your LAN, try `--st ssdp:all --raw` and see what answers.

Stdlib only (asyncio UDP). SSDP is multicast to 239.255.255.250:1900, which
doesn't cross VLANs or most Wi-Fi guest networks, and on WSL2 needs mirrored
networking; `check --host` always works instead.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

SSDP_ADDR = ("239.255.255.250", 1900)
HARMONY_ST = "urn:myharmony-com:device:harmony:1"  # ASSUMPTION H-SSDP


@dataclass(frozen=True)
class SsdpHit:
    host: str  # from LOCATION, else the address the reply came from
    location: str | None
    st: str | None
    usn: str | None
    server: str | None
    raw: str  # the whole reply, for --raw


def m_search(st: str, mx: int = 2) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()


def parse_reply(data: bytes, sender: str) -> SsdpHit | None:
    """An SSDP reply (HTTP-over-UDP), or None for anything else."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode with "replace" doesn't raise
        return None
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    location = headers.get("location")
    host = (urlparse(location).hostname if location else None) or sender
    return SsdpHit(host, location, headers.get("st"), headers.get("usn"), headers.get("server"), text)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.hits: list[SsdpHit] = []

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        hit = parse_reply(data, str(addr[0]))
        if hit is not None:
            self.hits.append(hit)


async def discover(timeout: float = 3.0, st: str = HARMONY_ST, target: tuple[str, int] = SSDP_ADDR) -> list[SsdpHit]:
    """M-SEARCH for `st` and return one hit per host. `target` is overridable for tests."""
    loop = asyncio.get_running_loop()
    collector = _Collector()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: collector, local_addr=("0.0.0.0", 0), family=socket.AF_INET
    )
    try:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        packet = m_search(st)
        for _ in range(2):  # UDP is lossy; hubs answer each, we dedupe below
            transport.sendto(packet, target)
            await asyncio.sleep(0.1)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    wanted = None if st == "ssdp:all" else st.lower()
    seen: dict[str, SsdpHit] = {}
    for hit in collector.hits:
        if wanted is not None and wanted not in (hit.st or "").lower() and wanted not in (hit.usn or "").lower():
            continue
        seen.setdefault(hit.host, hit)
    return list(seen.values())
