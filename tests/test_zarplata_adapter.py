from urllib.parse import parse_qs, urlparse

import pytest

from backend.adapters.base.protocol import JobRef
from backend.adapters.hh.adapter import HHAdapter
from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter


async def _async(value):
    return value


class EmptyLocator:
    @property
    def first(self):
        return self

    def filter(self, **_kwargs):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class SearchPage:
    url = "https://zarplata.ru/"

    def __init__(self):
        self.visited = []

    def locator(self, *_args, **_kwargs):
        return EmptyLocator()

    def get_by_text(self, *_args, **_kwargs):
        return EmptyLocator()

    async def goto(self, url, **_kwargs):
        self.url = url
        self.visited.append(url)

    async def wait_for_timeout(self, *_args):
        return None


async def configured(queries):
    adapter = ZarplataAdapter()
    page = SearchPage()
    await adapter.open_search(page, {"queries": queries})
    return adapter, page


def test_manifest_domains_and_hh_isolation():
    adapter = ZarplataAdapter()

    assert adapter.manifest.site_id == "zarplata"
    assert adapter.manifest.display_name == "Zarplata.ru"
    assert adapter.manifest.supports_submission
    assert "krasnoyarsk.zarplata.ru" in adapter.allowed_domains
    assert "adsrv.zarplata.ru" not in adapter.allowed_domains
    assert all("zarplata.ru" not in host for host in HHAdapter.allowed_domains)


@pytest.mark.asyncio
async def test_login_state_does_not_treat_signup_resume_link_as_authenticated():
    class Page:
        def locator(self, selector):
            assert "account/signup" not in selector
            return type(
                "Locator",
                (),
                {
                    "first": property(lambda self: self),
                    "count": lambda _self: _async(0),
                    "is_visible": lambda _self: _async(False),
                },
            )()

    assert not (await ZarplataAdapter().get_login_state(Page())).authenticated


@pytest.mark.asyncio
async def test_login_state_accepts_applicant_menu_marker():
    class Page:
        def locator(self, _selector):
            return type(
                "Locator",
                (),
                {
                    "first": property(lambda self: self),
                    "count": lambda _self: _async(1),
                    "is_visible": lambda _self: _async(True),
                },
            )()

    state = await ZarplataAdapter().get_login_state(Page())
    assert state.authenticated
    assert state.message == "Вход выполнен"


class LoginLocator:
    def __init__(self, *, href=None, text="", visible=True):
        self.href, self.text, self.visible = href, text, visible
        self.first = self

    async def count(self):
        return 1

    def nth(self, _index):
        return self

    async def is_visible(self):
        return self.visible

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def inner_text(self):
        return self.text


class LoginPage:
    def __init__(self, mapping):
        self.mapping = mapping

    def locator(self, selector):
        return self.mapping.get(selector, LoginLocator(visible=False, href=None))


@pytest.mark.asyncio
async def test_login_state_public_guest_login_markers_is_false():
    page = LoginPage({
        "[data-qa='login']": LoginLocator(href="/account/login", text="Войти"),
        "[data-qa='mainmenu_profile-link']": LoginLocator(
            href="/account/login", text="Войти"
        ),
    })
    assert not (await ZarplataAdapter().get_login_state(page)).authenticated


@pytest.mark.asyncio
async def test_login_state_accepts_non_login_profile_button():
    class ProtectedPage(LoginPage):
        def __init__(self, mapping):
            super().__init__(mapping)
            self.url = "https://zarplata.ru/"
            self.visited = []

        async def goto(self, url, **_kwargs):
            self.visited.append(url)
            self.url = "https://krasnoyarsk.zarplata.ru/applicant/resumes"

        async def wait_for_timeout(self, *_args):
            return None

    page = ProtectedPage({
        "[data-qa='mainmenu_profile-link']": LoginLocator(
            href="/account/profile", text="Мой профиль"
        ),
    })
    assert (await ZarplataAdapter().get_login_state(page)).authenticated
    assert page.visited == ["https://zarplata.ru/applicant/resumes"]


@pytest.mark.asyncio
async def test_login_state_hidden_profile_and_visible_login_is_false():
    page = LoginPage({
        "[data-qa='login']": LoginLocator(href="/account/login", text="Войти"),
        "[data-qa='mainmenu_profile-link']": LoginLocator(
            href="/account/profile", text="Профиль", visible=False
        ),
    })
    assert not (await ZarplataAdapter().get_login_state(page)).authenticated


@pytest.mark.asyncio
async def test_login_state_ambiguous_profile_is_false():
    page = LoginPage({
        "[data-qa='mainmenu_profile-link']": LoginLocator(
            href="/account/login", text="Профиль"
        ),
    })
    assert not (await ZarplataAdapter().get_login_state(page)).authenticated


@pytest.mark.asyncio
async def test_login_state_no_markers_is_false():
    assert not (await ZarplataAdapter().get_login_state(LoginPage({}))).authenticated


class ProtectedLoginPage(LoginPage):
    def __init__(self, mapping, final_url=None, error=None):
        super().__init__(mapping)
        self.url = "https://zarplata.ru/"
        self.final_url = final_url
        self.error = error
        self.visited = []

    async def goto(self, url, **_kwargs):
        self.visited.append(url)
        if self.error:
            raise self.error
        self.url = self.final_url

    async def wait_for_timeout(self, *_args):
        return None


@pytest.mark.asyncio
async def test_login_state_guest_marker_redirects_to_login():
    page = ProtectedLoginPage(
        {"[data-qa='login']": LoginLocator(text="Войти")},
        "https://zarplata.ru/account/login",
    )
    assert not (await ZarplataAdapter().get_login_state(page)).authenticated
    assert page.visited


@pytest.mark.asyncio
async def test_login_state_ambiguous_profile_uses_protected_route():
    page = ProtectedLoginPage(
        {"[data-qa='mainmenu_profile-link']": LoginLocator(href="/account/profile")},
        "https://zarplata.ru/applicant/resumes",
    )
    assert (await ZarplataAdapter().get_login_state(page)).authenticated


@pytest.mark.asyncio
async def test_login_state_accepts_live_protected_profile_route():
    page = ProtectedLoginPage(
        {},
        "https://krasnoyarsk.zarplata.ru/applicant/profile/me",
    )
    state = await ZarplataAdapter().get_login_state(page)
    assert state.authenticated


@pytest.mark.asyncio
async def test_login_state_rejects_similar_unprotected_applicant_route():
    page = ProtectedLoginPage(
        {},
        "https://krasnoyarsk.zarplata.ru/applicant/profiled",
    )
    assert not (await ZarplataAdapter().get_login_state(page)).authenticated


@pytest.mark.asyncio
async def test_login_state_protected_route_rejects_external_and_navigation_errors():
    external = ProtectedLoginPage({}, "https://evil.example/applicant/resumes")
    failed = ProtectedLoginPage({}, error=RuntimeError("navigation failed"))
    assert not (await ZarplataAdapter().get_login_state(external)).authenticated
    assert not (await ZarplataAdapter().get_login_state(failed)).authenticated


@pytest.mark.asyncio
async def test_open_search_uses_zarplata_urls_and_casefold_deduplication():
    adapter, page = await configured(
        ["  Python/Backend ", "python backend", "QA & Automation"]
    )

    assert page.visited == ["https://zarplata.ru/"]
    assert adapter._search_queries == ["Python Backend", "QA Automation"]
    assert all(
        url.startswith("https://zarplata.ru/search/vacancy?")
        for url in adapter._fallback_search_urls
    )
    assert parse_qs(urlparse(adapter._fallback_search_urls[1]).query)["text"] == [
        "QA Automation"
    ]


class Link:
    def __init__(self, href, visible=True):
        self.href = href
        self.visible = visible

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def is_visible(self):
        return self.visible


class LinkLocator:
    def __init__(self, links):
        self.links = links
        self.first = self

    async def wait_for(self, **_kwargs):
        return None

    async def count(self):
        return len(self.links)

    def nth(self, index):
        return self.links[index]


@pytest.mark.asyncio
async def test_visible_refs_reject_ads_hidden_and_non_vacancy_links():
    links = LinkLocator(
        [
            Link("/vacancy/10"),
            Link("https://ekb.zarplata.ru/vacancy/20?from=search"),
            Link("https://adsrv.zarplata.ru/click?vacancy/30"),
            Link("/search/vacancy/map"),
            Link("/vacancy/40", visible=False),
            Link("/vacancy/10"),
        ]
    )
    page = type(
        "Page",
        (),
        {
            "url": "https://krasnoyarsk.zarplata.ru/search/vacancy",
            "locator": lambda _self, _selector: links,
        },
    )()

    refs = await ZarplataAdapter()._visible_job_refs(page, timeout=100)

    assert [(ref.external_id, urlparse(ref.url).hostname) for ref in refs] == [
        ("10", "krasnoyarsk.zarplata.ru"),
        ("20", "ekb.zarplata.ru"),
    ]


@pytest.mark.asyncio
async def test_personal_recommendations_allow_200_but_pages_default_to_100():
    links = LinkLocator([Link(f"/vacancy/{index}") for index in range(250)])
    page = type(
        "Page",
        (),
        {
            "url": "https://krasnoyarsk.zarplata.ru/",
            "locator": lambda _self, _selector: links,
        },
    )()
    adapter = ZarplataAdapter()

    personal = await adapter.collect_job_refs(page)
    ordinary = await adapter._visible_job_refs(page, timeout=100)

    assert len(personal) == 200
    assert len(ordinary) == 100


@pytest.mark.asyncio
async def test_personal_recommendations_traverse_four_pages_to_200_in_order():
    class Page(SearchPage):
        def __init__(self):
            super().__init__()
            self.url = "https://zarplata.ru/search/vacancy?text=python"
            self.pages = {
                n: LinkLocator([Link(f"/vacancy/{n * 60 + i}") for i in range(60)])
                for n in range(4)
            }

        def locator(self, *_args, **_kwargs):
            return self.pages[int(parse_qs(urlparse(self.url).query).get("page", [0])[0])]

    page = Page()
    adapter = ZarplataAdapter()
    adapter._recommendation_base_url = page.url
    refs = await adapter.collect_job_refs(page)
    assert [ref.external_id for ref in refs] == [str(i) for i in range(200)]
    assert [parse_qs(urlparse(url).query)["page"][0] for url in page.visited] == [
        "0", "1", "2", "3"
    ]


@pytest.mark.asyncio
async def test_personal_recommendations_dedupe_across_pages_and_stop_on_empty():
    class Page(SearchPage):
        def __init__(self):
            super().__init__()
            self.url = "https://zarplata.ru/search/vacancy?text=x"
            self.pages = {
                0: LinkLocator([Link("/vacancy/1"), Link("/vacancy/2")]),
                1: LinkLocator([Link("/vacancy/2"), Link("/vacancy/3")]),
                2: LinkLocator([]),
            }

        def locator(self, *_args, **_kwargs):
            return self.pages[int(parse_qs(urlparse(self.url).query).get("page", [0])[0])]

    page = Page()
    adapter = ZarplataAdapter()
    adapter._recommendation_base_url = page.url
    assert [r.external_id for r in await adapter.collect_job_refs(page)] == ["1", "2", "3"]
    assert page.visited[-1].endswith("page=2")


@pytest.mark.asyncio
async def test_personal_recommendations_stop_after_three_repeated_signatures():
    class Page(SearchPage):
        def __init__(self):
            super().__init__()
            self.url = "https://zarplata.ru/search/vacancy?text=x"
            self.links = LinkLocator([Link("/vacancy/9")])

        def locator(self, *_args, **_kwargs):
            return self.links

    page = Page()
    adapter = ZarplataAdapter()
    adapter._recommendation_base_url = page.url
    assert [r.external_id for r in await adapter.collect_job_refs(page)] == ["9"]
    assert len(page.visited) == 4  # page 0 plus three repeated signatures


@pytest.mark.asyncio
async def test_home_recommendation_snapshot_is_not_paginated():
    page = SearchPage()
    page.links = LinkLocator([Link("/vacancy/1")])
    page.locator = lambda *_args, **_kwargs: page.links
    adapter = ZarplataAdapter()
    adapter._recommendation_base_url = page.url
    assert [r.external_id for r in await adapter.collect_job_refs(page)] == ["1"]
    assert page.visited == []


@pytest.mark.asyncio
async def test_each_adjacent_query_is_capped_at_100_unique_refs():
    adapter, _page = await configured(["one", "two"])
    calls = []

    async def collect(_page, url, page_number):
        query = parse_qs(urlparse(url).query)["text"][0]
        calls.append((query, page_number))
        start = page_number * 60 + (0 if query == "one" else 1000)
        return [
            JobRef(
                external_id=str(start + index),
                url=f"https://zarplata.ru/vacancy/{start + index}",
            )
            for index in range(60)
        ]

    adapter._collect_search_page = collect
    batches = []
    while not adapter.search_exhausted:
        batch = await adapter.collect_more_job_refs(SearchPage())
        if batch:
            batches.append((adapter.current_search_query, batch))

    by_query = {"one": [], "two": []}
    for query, batch in batches:
        by_query[query].extend(ref.external_id for ref in batch)

    assert {query: len(ids) for query, ids in by_query.items()} == {
        "one": 100,
        "two": 100,
    }
    assert calls == [("one", 0), ("one", 1), ("two", 0), ("two", 1)]


@pytest.mark.asyncio
async def test_links_beyond_one_query_cap_can_appear_in_next_query():
    adapter, _page = await configured(["one", "two"])
    calls = {"one": 0, "two": 0}

    async def collect(_page, url, _page_number):
        query = parse_qs(urlparse(url).query)["text"][0]
        calls[query] += 1
        if query == "one" and calls[query] == 1:
            ids = range(90)
        elif query == "one":
            ids = range(90, 110)
        elif calls[query] == 1:
            ids = range(100, 111)
        else:
            return []
        return [
            JobRef(external_id=str(i), url=f"https://zarplata.ru/vacancy/{i}")
            for i in ids
        ]

    adapter._collect_search_page = collect
    returned = []
    while not adapter.search_exhausted:
        returned.extend(await adapter.collect_more_job_refs(SearchPage()))

    assert len(returned) == 111
    assert {ref.external_id for ref in returned} == {str(i) for i in range(111)}


@pytest.mark.asyncio
async def test_empty_result_advances_to_next_query_and_exhausts():
    adapter, _page = await configured(["empty", "result"])
    seen = []

    async def collect(_page, url, _number):
        query = parse_qs(urlparse(url).query)["text"][0]
        seen.append(query)
        if query == "empty":
            return []
        if seen.count("result") == 1:
            return [JobRef(external_id="2", url="https://zarplata.ru/vacancy/2")]
        return []

    adapter._collect_search_page = collect

    assert [
        ref.external_id for ref in await adapter.collect_more_job_refs(SearchPage())
    ] == ["2"]
    assert await adapter.collect_more_job_refs(SearchPage()) == []
    assert adapter.search_exhausted
    assert seen == ["empty", "result", "result"]


class TextLocator:
    def __init__(self, value, visible=True):
        self.value = value
        self.visible = visible
        self.first = self

    async def count(self):
        return int(self.value is not None)

    async def is_visible(self):
        return self.visible

    async def inner_text(self, timeout=None):
        return self.value


class MultiTextLocator:
    def __init__(self, values):
        self.values = values
        self.first = self.nth(0)

    async def count(self):
        return len(self.values)

    def nth(self, index):
        value, visible = self.values[index]
        return TextLocator(value, visible)


class DetailPage:
    url = "https://krasnoyarsk.zarplata.ru/vacancy/123"

    def __init__(self, values):
        self.values = values

    def locator(self, selector):
        value = self.values.get(selector)
        return value if isinstance(value, MultiTextLocator) else TextLocator(value)


@pytest.mark.asyncio
async def test_extract_job_uses_zarplata_company_and_structured_attributes():
    values = {
        locators.VACANCY_TITLE: "Backend-разработчик",
        locators.COMPANY: MultiTextLocator([(None, False), ("ООО Инжпроект-М", True)]),
        locators.DESCRIPTION: "Разработка сервисов на Python",
        locators.PAYMENT_FREQUENCY: "Выплаты: два раза в месяц",
        locators.WORK_EXPERIENCE: "Опыт работы: 1–3 года",
        locators.EMPLOYMENT: "Полная занятость",
        locators.HIRING_FORMAT: "Оформление: Трудовой договор",
        locators.WORK_SCHEDULE: "График: 5/2",
        locators.WORKING_HOURS: "Рабочие часы: 8",
        locators.WORK_FORMAT: "Формат работы: удалённо",
    }

    job = await ZarplataAdapter().extract_job(DetailPage(values))

    assert job.source == "zarplata"
    assert job.external_id == "123"
    assert job.company == "ООО Инжпроект-М"
    assert job.required_experience == "Опыт работы: 1–3 года"
    assert job.work_format == "Формат работы: удалённо"


@pytest.mark.parametrize(
    "description",
    [
        "Необходимо выполнить тестовое задание",
        "Готовность пройти тестовое испытание обязательна",
        "Задание после отклика обязательно",
    ],
)
def test_mandatory_test_assignment_patterns(description):
    assert ZarplataAdapter.has_test_assignment(description)


@pytest.mark.asyncio
async def test_open_job_rejects_untrusted_domain():
    with pytest.raises(ValueError, match="пределы разрешённых доменов"):
        await ZarplataAdapter().open_job(
            SearchPage(),
            JobRef(
                external_id="1", url="https://adsrv.zarplata.ru/vacancy/1"
            ),
        )
