"""Limit/offset pagination."""

from __future__ import annotations

import pytest

from app.shared.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, PageParams


def test_defaults_to_the_first_page() -> None:
    params = PageParams()
    assert params.page == 1
    assert params.page_size == DEFAULT_PAGE_SIZE
    assert params.offset == 0


def test_the_page_size_is_capped() -> None:
    assert PageParams(page_size=10_000).page_size == MAX_PAGE_SIZE


@pytest.mark.parametrize(("page", "page_size"), [(0, 0), (-3, -10)])
def test_non_positive_values_are_normalised(page: int, page_size: int) -> None:
    params = PageParams(page=page, page_size=page_size)
    assert params.page == 1
    assert params.page_size == 1


def test_the_offset_follows_the_page() -> None:
    assert PageParams(page=3, page_size=25).offset == 50


@pytest.mark.parametrize(("total", "pages"), [(0, 0), (1, 1), (20, 1), (21, 2)])
def test_page_count(total: int, pages: int) -> None:
    page: Page[int] = Page(items=[], total=total, page=1, page_size=20)
    assert page.pages == pages
