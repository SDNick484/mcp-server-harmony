"""Channel numbers and favorites: what the model may ask harmony_tune_channel for.

A channel is typed on a remote as digit buttons, so a channel number here is only
digits with at most one separator: ``702``, ``5.1`` or ``5-1``. Everything else
is a favorite's name from config.json. Pure functions, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .limits import MAX_CHANNEL_DIGITS
from .lookup import norm

SEPARATORS = "-."
# ASCII only: \d would also take digits from other scripts, which no remote has a button for.
_NUMBER = re.compile(r"[0-9]+(?:[.-][0-9]+)?")
_NUMBERISH = re.compile(r"[0-9.-]+")


def looks_like_number(text: str) -> bool:
    """True for anything made only of digits and separators, valid or not ("5.1.2" included).

    Decides between "a number to press" and "a favorite's name to look up", so a typo'd number gets a
    message about numbers instead of "unknown channel".
    """
    return bool(_NUMBERISH.fullmatch(text.strip()))


def number_problem(text: str) -> str | None:
    """Why `text` is not a channel number, or None when it is one."""
    text = text.strip()
    if not _NUMBER.fullmatch(text):
        return f"{text!r} is not a channel number: use digits with at most one '-' or '.' between them, like 702 or 5.1"
    if sum(ch.isdigit() for ch in text) > MAX_CHANNEL_DIGITS:
        return f"{text!r} has more than {MAX_CHANNEL_DIGITS} digits"
    return None


def favorite(channels: Mapping[str, str], wanted: str) -> tuple[str, str] | None:
    """(name, number) of the favorite `wanted` names, matched the way other names are (case, spaces, dashes)."""
    key = norm(wanted)
    if not key:
        return None
    return next(((n, num) for n, num in channels.items() if norm(n) == key), None)
