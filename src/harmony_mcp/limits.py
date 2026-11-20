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
# Volume-up presses per hub: at most VOLUME_UP_LIMIT in any VOLUME_UP_WINDOW seconds, counted across calls.
# A hub sends blind IR and can't read the receiver's level, so each press is one step louder whatever the
# level already is; the receiver's own server caps a level it is *set* to (max_volume), but not presses. A
# sliding window (WindowLimit), not a bucket, because the count itself is the safety limit: a bucket of 20
# refilling over five minutes would allow 40 in a five-minute stretch. Settings can lower it, never raise it.
VOLUME_UP_LIMIT = 20
VOLUME_UP_WINDOW = 300.0
# Command names (after catalog.norm) that turn the volume up. Volume down and mute are never limited.
VOLUME_UP_NAMES = frozenset({"volumeup", "volup", "volumeplus", "volplus"})
# Per call.
MAX_REPEAT = 10
MAX_HOLD_MS = 3000
MIN_DELAY_MS, MAX_DELAY_MS = 100, 2000
MAX_CALL_SECONDS = 15.0  # repeat * (hold + delay) for one send_command
# Sleep timer: the longest one `harmony_set_sleep_timer` accepts. Twelve hours covers "turn it off when I'm
# asleep" and keeps a forgotten timer from firing days later; the timer is only in memory anyway.
MAX_SLEEP_MINUTES = 12 * 60
# Tuning a channel (harmony_tune_channel): the pause between the digit presses. Like repeat_gap it keeps an
# IR receiver from merging two quick presses into one; a TV's own channel entry also times out if the
# digits come too slowly, so it is not longer than that.
DIGIT_GAP_MS = 400
# A channel number: at most this many digits, with at most one "-" or "." between two of them
# (702, 5.1, 5-1, 1234.56). Longer is not a channel, it's a mistake worth refusing before pressing.
MAX_CHANNEL_DIGITS = 6
# Presses in one send_sequence: the digits, one separator and the select button.
MAX_SEQUENCE = MAX_CHANNEL_DIGITS + 2
# The channel argument also takes a favorite's name, which is longer than a number.
MAX_CHANNEL_ARG = 40

# Activity history (history.py): events kept in memory and in the file. A household makes a few changes
# a day, so this is months. The file is rewritten down to the cap only once it is SLACK lines over it.
HISTORY_MAX_EVENTS = 1000
HISTORY_FILE_SLACK = 250
