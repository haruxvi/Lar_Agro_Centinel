"""Limit/offset pagination shared by every listing endpoint."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100


@dataclass(frozen=True)
class PageParams:
    """A requested page. The size is clamped, never trusted as sent."""

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        """Normalise the request into a safe window."""
        # A client asking for 10_000 rows gets the maximum, not a table scan.
        object.__setattr__(self, "page", max(1, self.page))
        object.__setattr__(self, "page_size", min(max(1, self.page_size), MAX_PAGE_SIZE))

    @property
    def offset(self) -> int:
        """Return the number of rows to skip."""
        return (self.page - 1) * self.page_size


@dataclass(frozen=True)
class Page[T]:
    """One page of results plus what a client needs to request the next."""

    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        """Return the number of pages available."""
        return math.ceil(self.total / self.page_size) if self.total else 0


def paginate[T](session: Session, statement: Select[Any], params: PageParams) -> Page[T]:
    """Run ``statement`` for one page and count the full result set."""
    total = session.execute(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ).scalar_one()
    rows = session.execute(
        statement.limit(params.page_size).offset(params.offset)
    ).scalars()
    return Page(
        items=list(rows),
        total=int(total),
        page=params.page,
        page_size=params.page_size,
    )
