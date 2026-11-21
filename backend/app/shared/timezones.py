"""IANA time zones: the one place that says whether a zone name is valid.

Consumer drones write EXIF time without a zone, as the local time of the
drone's clock. Turning it into an instant needs the zone that clock was in:
continental Chile uses America/Santiago, Magallanes America/Punta_Arenas (no
daylight saving since 2017) and Rapa Nui Pacific/Easter. A wrong or invented
zone would shift every capture silently, so names are checked against the
IANA database and restricted to "Region/City" names (and UTC): the system
database also lists entries such as "localtime", which load but are not zones
anyone should configure.
"""

from __future__ import annotations

import zoneinfo
from functools import lru_cache
from typing import Final

MAX_TIMEZONE_NAME_LENGTH: Final = 64

# Only "Region/City" names, plus UTC. Excluded although loadable: entries of
# the system database that are not zones (localtime, posixrules), the posix/
# and right/ variants, and Etc/GMT+3 style names, whose sign is the opposite
# of what they seem (Etc/GMT+3 is UTC-3).
_ALSO_VALID: Final = frozenset({"UTC"})
_EXCLUDED_PREFIXES: Final = ("posix/", "right/", "Etc/")


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def is_valid_timezone(name: str) -> bool:
    """Return True when ``name`` is an IANA time zone that can be loaded."""
    if not name or len(name) > MAX_TIMEZONE_NAME_LENGTH or name != name.strip():
        return False
    if name not in _known_timezones():
        return False
    region_city = "/" in name and not name.startswith(_EXCLUDED_PREFIXES)
    if not (region_city or name in _ALSO_VALID):
        return False
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True
