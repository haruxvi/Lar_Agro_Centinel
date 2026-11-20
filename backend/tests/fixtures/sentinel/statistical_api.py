"""A double of the Statistical API that applies its rules, not a canned answer.

A canned response answers whatever was asked. That is how a bug went through
every test: the request ended at 23:59:59, the real API drops an incomplete
last interval by default, and the canned fixture answered for that day anyway
(found against the real API on 2026-11-20; see ADR-004). This double reads
the request and answers what the real API would, for the documented rules
that change the result:

- the range is split into ``aggregationInterval.of`` intervals (only P1D is
  used, so only P1D is implemented; anything else fails the test loudly);
- intervals start at ``timeRange.from``;
- an interval that would end after ``timeRange.to`` is incomplete and follows
  ``lastIntervalBehavior``: SKIP (the default) drops it, SHORTEN cuts it at
  ``to``, EXTEND keeps it whole;
- only days with an acquisition appear in ``data``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

PU_HEADER = "x-processingunits-spent"

# The September scenario every selection test shares: cloudy fraction over the
# predio per acquisition date. 09-20 is the newest and too cloudy; 09-15 is the
# one to choose.
SEPTEMBER_CLOUD: Mapping[date, float] = {
    date(2026, 9, 10): 0.0,
    date(2026, 9, 15): 0.12,
    date(2026, 9, 20): 0.85,
}

_BEHAVIOURS = ("SKIP", "SHORTEN", "EXTEND")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass
class StatisticalApiDouble:
    """Answers Statistical API requests from a cloud fraction per date.

    ``pu`` is the PU header of each successive call; the last value repeats.
    """

    cloud_by_date: Mapping[date, float] = field(default_factory=lambda: SEPTEMBER_CLOUD)
    pu: Sequence[float] = (0.0,)
    sample_count: int = 300
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as the real API would."""
        body = json.loads(request.content)
        self.requests.append(body)
        aggregation = body["aggregation"]
        interval = aggregation["aggregationInterval"]
        if interval.get("of") != "P1D":
            raise AssertionError(f"this double implements P1D only, got {interval}")
        behaviour = interval.get("lastIntervalBehavior", "SKIP")
        if behaviour not in _BEHAVIOURS:
            raise AssertionError(f"unknown lastIntervalBehavior {behaviour!r}")

        start = _instant(aggregation["timeRange"]["from"])
        end = _instant(aggregation["timeRange"]["to"])
        data: list[dict[str, Any]] = []
        day_start = start
        while day_start < end:
            day_end = day_start + timedelta(days=1)
            if day_end > end:
                if behaviour == "SKIP":
                    break
                if behaviour == "SHORTEN":
                    day_end = end
            fraction = self.cloud_by_date.get(day_start.date())
            if fraction is not None:
                data.append(self._interval(day_start, day_end, fraction))
            day_start = day_start + timedelta(days=1)

        index = min(len(self.requests), len(self.pu)) - 1
        headers = {PU_HEADER: str(self.pu[index])}
        return httpx.Response(200, json={"data": data, "status": "OK"}, headers=headers)

    def _interval(
        self, start: datetime, end: datetime, fraction: float
    ) -> dict[str, Any]:
        stats = {
            "min": 0.0,
            "max": 1.0 if fraction > 0 else 0.0,
            "mean": fraction,
            "sampleCount": self.sample_count,
            "noDataCount": 0,
        }
        return {
            "interval": {"from": _iso(start), "to": _iso(end)},
            "outputs": {"cloud": {"bands": {"B0": {"stats": stats}}}},
        }
