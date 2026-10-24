"""This server's rate limits and per-call caps: the numbers, per hub.

A model in a loop ("volume up... still quiet... volume up...") can send a lot
of IR in a few seconds, and an activity start/stop loop can wear out a TV's
power relay. The schema caps one call (repeat <= 10, hold <= 3 s); token
buckets cap the *rate* across calls, per hub. Each press (or activity change)
takes one token, and a call that needs more than are there is refused whole,
with how long to wait, rather than half-sent: half a "volume up x5" is worse
than none.

The mechanism (TokenBucket, refusal) is shared with the siblings in
ratelimit.py; these numbers are about a remote hub.
"""

from __future__ import annotations

# Presses per hub: a burst of 20, then 4 per second sustained.
PRESS_CAPACITY, PRESS_RATE = 20.0, 4.0
# Activity changes (start or power off) per hub: 4 in a burst, then one per 15 s.
ACTIVITY_CAPACITY, ACTIVITY_RATE = 4.0, 1 / 15
# Per call.
MAX_REPEAT = 10
MAX_HOLD_MS = 3000
MIN_DELAY_MS, MAX_DELAY_MS = 100, 2000
MAX_CALL_SECONDS = 15.0  # repeat * (hold + delay) for one send_command
