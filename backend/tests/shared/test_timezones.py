"""Which time-zone names a device clock (or the default) may be set to."""

from __future__ import annotations

import pytest

from app.shared.timezones import MAX_TIMEZONE_NAME_LENGTH, is_valid_timezone


@pytest.mark.parametrize(
    "name",
    [
        "America/Santiago",  # continental Chile
        "America/Punta_Arenas",  # Magallanes, no daylight saving since 2017
        "Pacific/Easter",  # Rapa Nui
        "America/Argentina/Buenos_Aires",  # three levels: region/country/city
        "UTC",
        "Europe/Madrid",
    ],
)
def test_iana_region_city_names_are_valid(name: str) -> None:
    assert is_valid_timezone(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        " America/Santiago",  # surrounding spaces are not trimmed silently
        "America/Santiago ",
        "america/santiago",  # names are case-sensitive
        "Mars/Olympus_Mons",
        "localtime",  # loads from the system database, but is not a zone
        "posixrules",
        "posix/America/Santiago",
        "right/America/Santiago",
        "Etc/GMT+3",  # means UTC-3: a trap, refused
        "../../etc/passwd",
        "/usr/share/zoneinfo/UTC",
        "America/" + "x" * MAX_TIMEZONE_NAME_LENGTH,
        "-03:00",  # an offset is not a zone: it ignores daylight saving
    ],
)
def test_anything_else_is_refused(name: str) -> None:
    assert not is_valid_timezone(name)
