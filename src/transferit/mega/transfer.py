"""
Duration parsing and expiry-range constants used by the upload flow.

Everything that actually talks to the MEGA API now lives on
:class:`MegaAPI` in ``_api.py``; this module only holds the pure helpers.
"""

from __future__ import annotations

import re

# Preset expiry values surfaced by the web form (glb-expire-radio).  Days.
EXPIRY_PRESETS_DAYS: tuple[int, ...] = (0, 7, 30, 90, 180, 365)

# Empirical server behaviour of the `e` field on xm (see REVERSE_ENGINEERING.md):
#   - value is **duration in seconds** from transfer creation (not unix ts)
#   - e == 0 is rejected (-11/EACCESS); omit `e` entirely to disable expiry
#   - bt7 accepts up to ~2^32 s with sporadic -11 on exact powers of two
#
# Client-side clamp: 1 second .. 10 years.
MIN_EXPIRY_SECONDS: int = 1
MAX_EXPIRY_SECONDS: int = 3650 * 86400  # 10 years


_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 86400 * 7,
    "y": 86400 * 365,
}
_DURATION_TOKEN_RE = re.compile(r"(?:(\d+)\s*([smhdwy]))", re.IGNORECASE)


def parse_duration(text: str) -> int:
    """
    Parse a human duration string into seconds.

    Accepts:
      * single unit  — ``"30s"``, ``"5m"``, ``"2h"``, ``"7d"``, ``"1w"``, ``"10y"``
      * compound     — ``"1y6m"``, ``"2h30m"``, ``"1d 12h"``
      * bare integer — ``"3600"`` (seconds)

    Units are case-insensitive; ``m`` is always minutes.
    """
    if text is None:
        raise ValueError("empty duration")
    s = str(text).strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty duration")
    if s.lstrip("-").isdigit():
        return int(s)

    total = 0
    consumed = 0
    for m in _DURATION_TOKEN_RE.finditer(s):
        total += int(m.group(1)) * _DURATION_UNITS[m.group(2)]
        consumed += m.end() - m.start()
    if consumed != len(s):
        raise ValueError(
            f"can't parse duration {text!r}; use e.g. 30s, 5m, 2h, 7d, 1w, 1y"
        )
    return total


def humanise_duration(seconds: int) -> str:
    """Best-effort inverse of :func:`parse_duration`."""
    if seconds <= 0:
        return "0s"
    parts = []
    for unit, unit_s in (
        ("y", 86400 * 365),
        ("w", 86400 * 7),
        ("d", 86400),
        ("h", 3600),
        ("m", 60),
        ("s", 1),
    ):
        if seconds >= unit_s:
            n, seconds = divmod(seconds, unit_s)
            parts.append(f"{n}{unit}")
    return "".join(parts) or "0s"


def cast_expiry_seconds(seconds: int | None) -> int | None:
    """
    Clamp-check a seconds value; return ``None`` for 0 so callers omit ``e``.

    Raises :class:`ValueError` for out-of-range durations.
    """
    if seconds is None or seconds == 0:
        return None
    if not (MIN_EXPIRY_SECONDS <= seconds <= MAX_EXPIRY_SECONDS):
        raise ValueError(
            f"expiry {seconds}s out of range "
            f"[{MIN_EXPIRY_SECONDS}s .. {MAX_EXPIRY_SECONDS}s "
            f"({MAX_EXPIRY_SECONDS // 86400} days)]"
        )
    return seconds
