"""Log redaction: keep LAN addresses and MACs out of logs you might paste into an issue.

Logs mention hubs by name wherever we control the message, but aioharmony logs
its IP address on nearly every line, and tracebacks carry URLs. So instead of
editing messages one by one, the stderr handler's *formatter* redacts the
final text, tracebacks included:

    192.168.1.60          -> x.x.x.60      (last octet kept, to tell hubs apart)
    AA:BB:CC:DD:EE:FF     -> xx:xx:xx:xx:EE:FF
    AABBCCDDEEFF          -> xxxxxxxxEEFF  (bare uppercase hex, as in MQTT topics)

Loopback and unspecified addresses (127.x, 0.0.0.0) are left alone: they say
nothing about your network and matter when debugging the simulator.

Set HARMONY_LOG_UNREDACTED=1 (or pass --no-redact) to see everything, e.g.
when you are the only reader.
"""

from __future__ import annotations

import logging
import re

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_MAC = re.compile(r"(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{2}[:-]){4})([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})(?![0-9A-Fa-f:])")
_BARE_MAC = re.compile(r"(?<![0-9A-Za-z])([0-9A-F]{8})([0-9A-F]{4})(?![0-9A-Za-z])")


def _ip(m: re.Match[str]) -> str:
    first = m.group(1)
    if first in ("127", "0"):
        return m.group(0)
    return f"x.x.x.{m.group(4)}"


def redact(text: str) -> str:
    text = _IPV4.sub(_ip, text)
    text = _MAC.sub(lambda m: "xx:xx:xx:xx:" + m.group(2), text)
    return _BARE_MAC.sub(lambda m: "xxxxxxxx" + m.group(2), text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int, *, redacted: bool = True) -> None:
    """Log to stderr (stdout belongs to the MCP stdio transport), redacted unless asked not to."""
    import sys

    handler = logging.StreamHandler(sys.stderr)
    fmt = "%(levelname)s %(name)s: %(message)s"
    handler.setFormatter(RedactingFormatter(fmt) if redacted else logging.Formatter(fmt))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
