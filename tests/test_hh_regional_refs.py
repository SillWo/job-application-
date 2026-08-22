from __future__ import annotations

import pytest

from backend.adapters.hh.adapter import HHAdapter


class Link:
    def __init__(self, href: str, visible: bool = True):
        self.href = href
        self.visible = visible

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name: str):
        return self.href if name == "href" else None


class Links:
    def __init__(self, links: list[Link]):
        self.links = links
        self.first = self

    async def wait_for(self, **kwargs):
        return None

    async def count(self):
        return len(self.links)

    def nth(self, index: int):
        return self.links[index]


class Page:
    url = "https://krasnoyarsk.hh.ru/search/vacancy?text=python"

    def __init__(self, links: list[Link]):
        self.links = Links(links)

    def locator(self, selector: str):
        return self.links


@pytest.mark.asyncio
async def test_visible_job_refs_accept_regional_relative_and_tracking_links():
    page = Page(
        [
            Link("/vacancy/1824?from=vacancy_search_list&hhtmFrom=vacancy_search_list"),
            Link("https://krasnoyarsk.hh.ru/vacancy/1825/?from=search"),
            Link("//krasnoyarsk.hh.ru/vacancy/1826?from=employer"),
            Link("/vacancy/1824?from=duplicate"),
            Link("/vacancy/9999", visible=False),
        ]
    )

    refs = await HHAdapter()._visible_job_refs(page, timeout=100)

    assert [(ref.external_id, ref.url) for ref in refs] == [
        ("1824", "https://krasnoyarsk.hh.ru/vacancy/1824?from=vacancy_search_list&hhtmFrom=vacancy_search_list"),
        ("1825", "https://krasnoyarsk.hh.ru/vacancy/1825/?from=search"),
        ("1826", "https://krasnoyarsk.hh.ru/vacancy/1826?from=employer"),
    ]


@pytest.mark.asyncio
async def test_visible_job_refs_rejects_unallowed_vacancy_host():
    page = Page([Link("https://evil.example/vacancy/1824?from=search")])

    assert await HHAdapter()._visible_job_refs(page, timeout=100) == []
