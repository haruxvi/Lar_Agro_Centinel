"""docs/audit-events.md and the event catalogues of the code say the same thing.

The catalogues are discovered from ``app.modules.audit.events``: every
module-level mapping of event type to ``EventSpec`` (PREDIO_EVENTS,
ANALYSIS_EVENTS, and whatever the next phase adds). Nothing here lists them by
name, so a new catalogue is checked the day it exists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from app.modules.audit import events
from app.modules.audit.events import EventSpec

CATALOGUE_DOC = Path(__file__).resolve().parents[4] / "docs" / "audit-events.md"
_DOC_ROW = re.compile(r"^\| `([A-Z0-9_]+)` \| (\w+) \| (\w+) \|", re.MULTILINE)


def _catalogues() -> dict[str, Mapping[str, EventSpec]]:
    return {
        name: value
        for name, value in vars(events).items()
        if isinstance(value, Mapping)
        and value
        and all(isinstance(spec, EventSpec) for spec in value.values())
    }


def _documented() -> list[tuple[str, str, str]]:
    return _DOC_ROW.findall(CATALOGUE_DOC.read_text(encoding="utf-8"))


def test_the_catalogues_are_found() -> None:
    # Guards the discovery itself: if it silently found nothing, every other
    # test here would pass against an empty set.
    assert {"PREDIO_EVENTS", "ANALYSIS_EVENTS"} <= set(_catalogues())


def test_no_event_type_is_catalogued_twice() -> None:
    seen: dict[str, str] = {}
    for name, catalogue in _catalogues().items():
        for event_type in catalogue:
            assert event_type not in seen, (
                f"{event_type} in {seen.get(event_type)} and {name}"
            )
            seen[event_type] = name


def test_no_event_type_is_documented_twice() -> None:
    documented = [row[0] for row in _documented()]
    assert len(documented) == len(set(documented))


def test_the_document_matches_every_catalogue_with_category_and_severity() -> None:
    documented = {
        event_type: (category, severity)
        for event_type, category, severity in _documented()
    }
    expected = {
        event_type: (spec.category.value, spec.severity.value)
        for catalogue in _catalogues().values()
        for event_type, spec in catalogue.items()
    }
    assert documented == expected
