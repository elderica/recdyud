"""Channel name to center frequency (kHz) conversion for ISDB-T.

The table follows BonDriver_dyud (``TranslateChannelToFreq``):

* ``13`` .. ``62``   UHF terrestrial channels
* ``C13`` .. ``C63`` CATV channels (frequency pass-through)
* ``1`` .. ``12``    VHF channels (frequency pass-through)

A raw frequency can also be given as ``473143kHz`` or ``473.143MHz``.
"""

import re
from dataclasses import dataclass

UHF_RANGE = range(13, 63)
CATV_RANGE = range(13, 64)
VHF_RANGE = range(1, 13)

MIN_FREQUENCY_KHZ = 90_000
MAX_FREQUENCY_KHZ = 770_000


@dataclass(frozen=True)
class Channel:
    name: str
    frequency_khz: int

    def __str__(self) -> str:
        return f"{self.name} ({self.frequency_khz / 1000:.3f} MHz)"


class InvalidChannel(ValueError):
    pass


def uhf_frequency(ch: int) -> int:
    return 473_143 + 6_000 * (ch - 13)


def catv_frequency(ch: int) -> int:
    if 13 <= ch <= 21:  # VHF mid band
        return 111_143 + 6_000 * (ch - 13)
    if ch == 22:
        return 167_143
    if ch == 23:
        return 225_143
    if 24 <= ch <= 27:  # super high band
        return 233_143 + 6_000 * (ch - 24)
    if 28 <= ch <= 63:
        return 255_143 + 6_000 * (ch - 28)
    raise InvalidChannel(f"C{ch}")


def vhf_frequency(ch: int) -> int:
    if 1 <= ch <= 3:  # VHF low band
        return 93_143 + 6_000 * (ch - 1)
    if 4 <= ch <= 7:
        return 173_143 + 6_000 * (ch - 4)
    if 8 <= ch <= 12:
        return 195_143 + 6_000 * (ch - 8)
    raise InvalidChannel(str(ch))


_FREQ_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(khz|mhz)$", re.IGNORECASE)


def parse_channel(spec: str) -> Channel:
    s = spec.strip()
    if m := _FREQ_RE.match(s):
        value = float(m.group(1))
        khz = round(value * 1000) if m.group(2).lower() == "mhz" else round(value)
        if not MIN_FREQUENCY_KHZ <= khz <= MAX_FREQUENCY_KHZ:
            raise InvalidChannel(f"frequency out of range: {spec}")
        return Channel(f"{khz}kHz", khz)

    upper = s.upper()
    # Accept "GR27", "27ch" and similar spellings.
    upper = upper.removeprefix("GR").removesuffix("CH")
    if upper.startswith("C") and upper[1:].isdigit():
        ch = int(upper[1:])
        if ch in CATV_RANGE:
            return Channel(f"C{ch}", catv_frequency(ch))
    elif upper.isdigit():
        ch = int(upper)
        if ch in UHF_RANGE:
            return Channel(str(ch), uhf_frequency(ch))
        if ch in VHF_RANGE:
            return Channel(str(ch), vhf_frequency(ch))
    raise InvalidChannel(f"unsupported channel: {spec!r} (use 13-62, C13-C63, 1-12 or a frequency like 473143kHz)")


def expand_channel_ranges(spec: str) -> list[Channel]:
    """Parse a list such as ``"13-62"``, ``"20,21,27"`` or ``"C13-C63"``."""
    result: list[Channel] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            lo, hi = (p.strip() for p in part.split("-", 1))
            prefix = "C" if lo.upper().startswith("C") else ""
            lo_n = int(lo.upper().removeprefix("C"))
            hi_n = int(hi.upper().removeprefix("C"))
            if lo_n > hi_n:
                raise InvalidChannel(f"bad range: {part}")
            result.extend(parse_channel(f"{prefix}{n}") for n in range(lo_n, hi_n + 1))
        else:
            result.append(parse_channel(part))
    return result
