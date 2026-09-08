import pytest

from backend.adapters.base.protocol import JobRef
from backend.adapters.hirehi.adapter import HireHiAdapter


class _Node:
    def __init__(self, text="", href=None):
        self.text, self.href, self.clicked = text, href, False
        self.fill_calls = []
        self.press_calls = []

    async def count(self):
        return 1

    async def wait_for(self, **kwargs):
        return None

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def click(self):
        self.clicked = True

    async def fill(self, value):
        self.fill_calls.append(value)
        self.text = value

    async def press(self, value):
        self.press_calls.append(value)

    @property
    def first(self):
        return self


class _NodeEmpty(_Node):
    async def count(self):
        return 0


class _Links:
    def __init__(self, hrefs):
        self.items = [_Node(href=href) for href in hrefs]

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _RefsPage:
    url = "https://hirehi.ru/"

    def __init__(self, hrefs):
        self.hrefs = hrefs

    def locator(self, selector):
        return _Links(self.hrefs)


class _Page:
    url = "https://hirehi.ru/vacancies/test-1"

    def __init__(self, text, direct="", external=None):
        self.text, self.direct, self.external = text, direct, external

    def locator(self, selector):
        if selector == "body":
            return _Node(self.text)
        if "profile" in selector or "account" in selector or "data-testid='profile'" in selector:
            return _NodeEmpty()
        if "dialog" in selector or "contact" in selector or "response" in selector:
            return _Node(self.direct)
        return _NodeEmpty()

    def get_by_role(self, role, name=None):
        if role == "textbox":
            return _Node("search")
        if (
            role == "link"
            and self.direct
            and name
            and hasattr(name, "pattern")
            and any(kind in name.pattern for kind in ("email", "telegram", "linkedin"))
        ):
            return _Node(self.direct)
        if (
            role == "link"
            and self.external
            and name
            and not (hasattr(name, "pattern") and "email" in name.pattern)
        ):
            return _Node("Откликнуться", self.external)
        return _NodeEmpty()

    async def wait_for_timeout(self, value):
        return None

    def get_by_text(self, name):
        return _Node(self.direct) if self.direct else _NodeEmpty()


class _SearchPage(_Page):
    def __init__(self):
        super().__init__("public listing")
        self.goto_urls = []
        self.waited_urls = []
        self.opener = _Node("Выбрать категорию вакансий")
        self.textbox = _Node()
        self.category_links = {
            category: _Node(category, path)
            for category, path in HireHiAdapter.CATEGORY_PATHS.items()
        }
        self.dialog = _CategoryDialog(self.category_links)

    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.url = url

    async def wait_for_url(self, *args, **kwargs):
        self.waited_urls.append(args[0])
        return None

    def get_by_role(self, role, name=None):
        if role == "button" and name == "Выбрать категорию вакансий":
            return self.opener
        if role == "dialog" and name == "Категория":
            return self.dialog
        if role == "textbox":
            return self.textbox
        return super().get_by_role(role, name)


class _CategoryDialog:
    def __init__(self, links):
        self.links = links

    def get_by_role(self, role, name=None):
        if role != "link": return _NodeEmpty()
        for label, node in self.links.items():
            if name.search(label): return node
        return _NodeEmpty()

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def wait_for(self, **kwargs):
        return None



class _GradeNode:
    def __init__(self, text, selected=False):
        self.text, self.selected, self.clicks = text, selected, 0

    async def count(self): return 1
    async def is_visible(self): return True
    async def inner_text(self): return self.text
    async def get_attribute(self, name):
        if name == "class": return "filter-chip active" if self.selected else "filter-chip"
        return None
    async def click(self): self.selected, self.clicks = True, self.clicks + 1
    @property
    def first(self): return self
    def locator(self, selector): return self


class _GradePage:
    def __init__(self, grades=None):
        self.nodes = {grade: _GradeNode(grade) for grade in (grades or HireHiAdapter.GRADES)}
        self.waits = []
    def locator(self, selector):
        if selector == ".filter-group": return _GradeCollection([_GradeGroup(self.nodes)])
        return _GradeCollection([])
    async def wait_for_timeout(self, value): self.waits.append(value)
    def get_by_role(self, *args, **kwargs): return _GradeCollection([])
    def get_by_text(self, *args, **kwargs): return _GradeCollection([])


class _GradeGroup:
    def __init__(self, nodes): self.nodes = nodes
    async def is_visible(self): return True
    def locator(self, selector):
        if selector == ".filter-title": return _GradeCollection([_GradeNode("грейд")])
        return _GradeCollection(list(self.nodes.values()))


class _GradeCollection:
    def __init__(self, items): self.items = items
    async def count(self): return len(self.items)
    def nth(self, index): return self.items[index]
    @property
    def first(self): return self.items[0] if self.items else _NodeEmpty()


@pytest.mark.asyncio
@pytest.mark.parametrize("grades", [("intern",), ("intern", "junior", "middle"), ("senior",), ("lead", "head")])
async def test_apply_grade_filters_selects_requested_chips(grades):
    page = _GradePage()
    await HireHiAdapter()._apply_grade_filters(page, grades)
    assert [g for g, node in page.nodes.items() if node.selected] == list(grades)
    assert all(node.clicks == (1 if g in grades else 0) for g, node in page.nodes.items())


@pytest.mark.asyncio
async def test_apply_grade_filters_rejects_unknown_grade():
    with pytest.raises(ValueError, match="грейд"):
        await HireHiAdapter()._apply_grade_filters(_GradePage(), ["principal"])


@pytest.mark.asyncio
async def test_apply_grade_filters_requires_control():
    page = _GradePage()
    page.locator = lambda selector: _GradeCollection([])
    with pytest.raises(RuntimeError, match="фильтр грейда"):
        await HireHiAdapter()._apply_grade_filters(page, ["intern"])


@pytest.mark.asyncio
async def test_manifest_and_login_state():
    adapter = HireHiAdapter()
    assert adapter.manifest.site_id == "hirehi"
    assert (await adapter.get_login_state(_Page("public listing"))).authenticated is False


@pytest.mark.asyncio
async def test_direct_contact_limit_does_not_click():
    page = _Page("Осталось: 0", direct="Осталось: 0")
    route = await HireHiAdapter().classify_application_route(page)
    assert route.kind == "direct_contact" and route.contact.exhausted


@pytest.mark.asyncio
async def test_telegram_direct_contact_and_exhausted_label():
    adapter = HireHiAdapter()
    route = await adapter.classify_application_route(_Page("", direct="Отклик в Telegram Осталось: 0"))
    assert route.kind == "direct_contact"
    assert route.contact.exhausted is True


@pytest.mark.asyncio
async def test_direct_telegram_click_detects_new_tab_destination():
    class Destination:
        url = "https://t.me/annapestall"
    class Context:
        pages = []
    class Page(_Page):
        def __init__(self):
            super().__init__("", direct="Отклик в Telegram Осталось: 4")
            self.context = Context()
        def get_by_role(self, role, name=None):
            node = super().get_by_role(role, name)
            if role == "link" and self.direct:
                async def click(): self.context.pages.append(Destination())
                node.click = click
            return node
    route = await HireHiAdapter().classify_application_route(Page())
    assert route.kind == "direct_contact"
    assert route.contact.telegram == "https://t.me/annapestall"


@pytest.mark.asyncio
async def test_collect_application_route_returns_hirehi_vacancy_url_for_chat():
    page = _Page("", external=None)
    page.url = "https://hirehi.ru/management/product-owner-123"
    route = await HireHiAdapter().collect_application_route(page)
    assert route.kind == "hirehi_chat"
    assert route.target_url == page.url


@pytest.mark.asyncio
async def test_collect_application_route_discovers_external_popup_without_submit():
    class Destination:
        url = "https://employer.example/job/42"
    class Context:
        pages = []
    class Apply(_Node):
        async def click(self):
            self.clicked = True
            page.context.pages.append(Destination())
    class Page(_Page):
        def __init__(self):
            super().__init__("")
            self.context = Context()
            self.apply = Apply("Отклик", "#")
        def get_by_role(self, role, name=None):
            if (
                role == "link" and name and hasattr(name, "pattern")
                and "отклик" in name.pattern
                and not any(kind in name.pattern for kind in ("email", "telegram", "linkedin"))
            ):
                return self.apply
            return super().get_by_role(role, name)
    page = Page()
    route = await HireHiAdapter().collect_application_route(page)
    assert route.kind == "external_employer"
    assert route.target_url == "https://employer.example/job/42"
    assert page.apply.clicked is True


@pytest.mark.asyncio
async def test_reveal_direct_contact_clicks_control_once_and_reads_popup():
    class Control(_Node):
        def __init__(self): super().__init__("Отклик"); self.click_count = 0
        async def click(self): self.click_count += 1
    class Destination: url = "https://t.me/annapestall"
    class Context: pages = []
    page = _Page(""); page.context = Context(); control = Control()
    async def click(): control.click_count += 1; page.context.pages.append(Destination())
    control.click = click
    contact, href = await HireHiAdapter()._reveal_direct_contact(page, control)
    assert control.click_count == 1 and contact.telegram == "https://t.me/annapestall" and href.endswith("annapestall")


@pytest.mark.asyncio
async def test_reveal_direct_contact_polls_late_dom_after_single_click():
    class Control(_Node):
        def __init__(self): super().__init__("Отклик"); self.click_count = 0
        async def click(self): self.click_count += 1
    page = _Page(""); control = Control(); calls = 0
    original = page.locator
    def locator(selector):
        nonlocal calls
        if "response" in selector:
            calls += 1
            return _Node("https://t.me/late" if calls > 1 else "")
        return original(selector)
    page.locator = locator
    contact, _ = await HireHiAdapter()._reveal_direct_contact(page, control)
    assert control.click_count == 1 and contact.telegram == "https://t.me/late"


@pytest.mark.asyncio
async def test_reveal_direct_contact_waits_for_popup_url_after_about_blank():
    class Control(_Node):
        def __init__(self): super().__init__("Отклик"); self.click_count = 0
        async def click(self): self.click_count += 1
    class Destination:
        def __init__(self): self.url = "about:blank"; self.reads = 0
    destination = Destination()
    class Context: pages = []
    page = _Page(""); page.context = Context()
    async def wait(_):
        destination.reads += 1
        if destination.reads >= 2: destination.url = "https://t.me/zhakosha_bay"
    page.wait_for_timeout = wait
    control = Control()
    async def click(): control.click_count += 1; page.context.pages.append(destination)
    control.click = click
    contact, _ = await HireHiAdapter()._reveal_direct_contact(page, control)
    assert control.click_count == 1 and contact.telegram.endswith("zhakosha_bay")


@pytest.mark.asyncio
async def test_reveal_direct_contact_waits_for_late_popup_creation():
    class Control(_Node):
        def __init__(self): super().__init__("Отклик"); self.click_count = 0
        async def click(self): self.click_count += 1
    class Destination: url = "https://t.me/zhakosha_bay"
    class Context: pages = []
    page = _Page(""); page.context = Context(); waits = 0
    async def wait(_):
        nonlocal waits
        waits += 1
        if waits == 2: page.context.pages.append(Destination())
    page.wait_for_timeout = wait
    control = Control()
    contact, _ = await HireHiAdapter()._reveal_direct_contact(page, control)
    assert control.click_count == 1 and contact.telegram.endswith("zhakosha_bay")


@pytest.mark.asyncio
async def test_telegram_direct_contact_clicks_and_extracts_revealed_contact():
    page = _Page("", direct="Отклик в Telegram @product_recruiter Осталось: 4")
    route = await HireHiAdapter().classify_application_route(page)
    assert route.kind == "direct_contact"
    assert route.contact.telegram == "@product_recruiter"
    assert route.contact.exhausted is False


@pytest.mark.asyncio
async def test_contact_extraction_from_revealed_region():
    route = await HireHiAdapter().classify_application_route(
        _Page("", "email test@example.com https://t.me/alice")
    )
    assert route.contact.email == "test@example.com"


@pytest.mark.asyncio
async def test_contact_extraction_skips_hidden_region_and_reads_visible_href():
    class Anchors:
        def __init__(self, hrefs):
            self.items = [_Node(href=href) for href in hrefs]

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Region:
        def __init__(self, visible, hrefs):
            self.visible, self.hrefs = visible, hrefs

        async def is_visible(self):
            return self.visible

        async def inner_text(self):
            return ""

        def locator(self, selector):
            return Anchors(self.hrefs)

    class Regions:
        def __init__(self):
            self.items = [
                Region(False, ["https://t.me/wrong"]),
                Region(True, ["https://t.me/product_recruiter"]),
            ]

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        def locator(self, selector):
            return _Node("") if selector == "body" else Regions()

    contact = await HireHiAdapter().collect_employer_contact(Page())
    assert contact.telegram == "https://t.me/product_recruiter"


@pytest.mark.asyncio
async def test_external_route_keeps_target_url():
    route = await HireHiAdapter().classify_application_route(
        _Page("", external="https://employer.example/apply")
    )
    assert route.kind == "external_employer" and route.target_url.endswith("/apply")


@pytest.mark.asyncio
async def test_public_page_is_not_login_blocker():
    assert await HireHiAdapter().detect_blockers(_Page("Войти на сайт")) == []


@pytest.mark.asyncio
async def test_search_exhausted_property_defaults_false():
    assert HireHiAdapter().search_exhausted is False


@pytest.mark.asyncio
async def test_open_search_ignores_keyword_queries_and_uses_category_filter():
    page = _SearchPage()
    await HireHiAdapter().open_search(page, {"queries": ["Product Owner & Lead"]})
    assert page.goto_urls == ["https://hirehi.ru/"]
    assert page.opener.clicked
    assert page.category_links["все вакансии"].clicked
    assert page.textbox.fill_calls == []
    assert page.textbox.press_calls == []


@pytest.mark.asyncio
async def test_category_filter_opener_and_scoped_management_link_are_clicked():
    page = _SearchPage()
    await HireHiAdapter().open_search(page, {"category": "менеджмент"})
    assert page.category_links["менеджмент"].clicked
    assert page.url == "https://hirehi.ru/vacancies/management"


@pytest.mark.asyncio
async def test_open_search_chooses_visible_category_duplicate():
    class DuplicateCollection:
        def __init__(self, items): self.items = items
        async def count(self): return len(self.items)
        def nth(self, index): return self.items[index]
        @property
        def first(self): return self.items[0]

    class VisibleDialog(_CategoryDialog):
        def get_by_role(self, role, name=None):
            node = super().get_by_role(role, name)
            if role == "link":
                hidden = _Node(node.text)
                async def hidden_visibility(): return False
                hidden.is_visible = hidden_visibility
                return DuplicateCollection([hidden, node])
            return node

    class Page(_SearchPage):
        def __init__(self):
            super().__init__()
            self.dialog = VisibleDialog(self.category_links)
            hidden = _Node("hidden")
            async def hidden_visibility(): return False
            hidden.is_visible = hidden_visibility
            self.opener = DuplicateCollection([hidden, self.opener])
        def get_by_role(self, role, name=None):
            if role == "button" and name == "Выбрать категорию вакансий": return self.opener
            if role == "dialog" and name == "Категория": return DuplicateCollection([self.dialog])
            return super().get_by_role(role, name)

    page = Page()
    await HireHiAdapter().open_search(page, {"category": "менеджмент"})
    assert page.opener.items[1].clicked
    assert page.category_links["менеджмент"].clicked


@pytest.mark.asyncio
async def test_open_search_uses_visible_direct_category_when_opener_is_absent():
    class DirectCategoryPage(_SearchPage):
        def __init__(self):
            super().__init__()
            self.direct = _Node("менеджмент", "https://hirehi.ru/vacancies/management")

            async def click():
                self.direct.clicked = True
                self.url = self.direct.href

            self.direct.click = click

        def get_by_role(self, role, name=None):
            if role == "button" and name == "Выбрать категорию вакансий":
                return _NodeEmpty()
            if role == "dialog":
                return _NodeEmpty()
            if role == "link" and name and name.search("менеджмент"):
                return self.direct
            return super().get_by_role(role, name)

    page = DirectCategoryPage()
    await HireHiAdapter().open_search(page, {"category": "менеджмент"})
    assert page.direct.clicked is True
    assert page.url == "https://hirehi.ru/vacancies/management"


@pytest.mark.asyncio
async def test_open_search_supports_current_sidebar_without_dialog():
    class SidebarPage(_SearchPage):
        def get_by_role(self, role, name=None):
            if role == "dialog": return _NodeEmpty()
            return super().get_by_role(role, name)
        def get_by_text(self, name):
            hidden = _Node("менеджмент")
            async def hidden_visibility(): return False
            hidden.is_visible = hidden_visibility
            return type("Options", (), {
                "count": lambda self: _count(),
                "nth": lambda self, i: [hidden, SidebarPage.visible][i],
            })()
        visible = _Node("менеджмент", "/vacancies/management")

    async def _count(): return 2
    page = SidebarPage()
    await HireHiAdapter().open_search(page, {"category": "менеджмент"})
    assert page.visible.clicked


@pytest.mark.asyncio
async def test_extract_job_main_fallback_keeps_only_vacancy_sections():
    class DuplicateCollection:
        def __init__(self, items): self.items = items
        async def count(self): return len(self.items)
        def nth(self, index): return self.items[index]
        @property
        def first(self): return self.items[0]

    class Main:
        async def is_visible(self): return True
        async def inner_text(self):
            return "AI tools\nПрофиль\nОписание\nСоздаём продукт\nЗадачи\nВести команду\nТребования\nОпыт 3 года\nУсловия\nУдалённо\nПро\u00a0зарплаты\nАнонимные данные по зарплатам.\nПосмотреть зарплаты\nПохожие вакансии\nДругая роль"
    class Page:
        url = "https://hirehi.ru/management/product-owner-123"
        def locator(self, selector):
            if selector == "main": return DuplicateCollection([Main()])
            if selector == "h1": return DuplicateCollection([_Node("Product Owner")])
            return _NodeEmpty()
    posting = await HireHiAdapter().extract_job(Page())
    assert posting.description == (
        "Описание\nСоздаём продукт\nЗадачи\nВести команду\n"
        "Требования\nОпыт 3 года\nУсловия\nУдалённо"
    )


@pytest.mark.asyncio
async def test_open_job_waits_for_heading_after_commit():
    class JobPage:
        url = "https://hirehi.ru/management/product-owner-123"
        def __init__(self): self.waited = False
        async def goto(self, url, **kwargs): self.navigated = url
        def locator(self, selector): return self
        @property
        def first(self): return self
        async def wait_for(self, **kwargs): self.waited = True
        async def inner_text(self): return "Product Owner"
        async def wait_for_timeout(self, value): return None
    page = JobPage()
    await HireHiAdapter().open_job(page, JobRef(external_id="123", url=page.url))
    assert page.waited is True


@pytest.mark.asyncio
async def test_open_job_rejects_heading_that_stays_empty():
    class EmptyPage:
        url = "https://hirehi.ru/management/product-owner-123"
        async def goto(self, url, **kwargs): pass
        def locator(self, selector): return self
        @property
        def first(self): return self
        async def wait_for(self, **kwargs): pass
        async def inner_text(self): return ""
        async def wait_for_timeout(self, value): pass
    with pytest.raises(RuntimeError):
        await HireHiAdapter().open_job(EmptyPage(), JobRef(external_id="123", url=EmptyPage.url))


@pytest.mark.asyncio
async def test_collect_refs_supports_real_vacancies_urls_and_deduplicates():
    page = _RefsPage([
        "/management/product-owner-in-web3-79097",
        "/management/menedzher-produkta-v-hr-tech-79531",
        "/analytics/product-analyst-79572",
        "/vacancies/product-manager",
        "/companies/acme",
        "/management/product-owner-in-web3-79097",
    ])
    refs = await HireHiAdapter().collect_job_refs(page)
    assert [(ref.external_id, ref.url) for ref in refs] == [
        ("79097", "https://hirehi.ru/management/product-owner-in-web3-79097"),
        ("79531", "https://hirehi.ru/management/menedzher-produkta-v-hr-tech-79531"),
        ("79572", "https://hirehi.ru/analytics/product-analyst-79572"),
    ]


class _DirectPagingPage:
    """Deterministic page URLs with repeated listing filters."""

    base = "https://hirehi.ru/vacancies/management?grade=middle&grade=senior&application_type=direct"

    def __init__(self):
        self.url = self.base
        self.goto_urls = []
        self.history = []
        self.go_back_calls = 0
        self.page = 1
    def _hrefs(self):
        if "/job-" in self.url:
            return [f"/management/similar-{i}" for i in range(900, 906)]
        if self.page > 4:
            return []
        start = (self.page - 1) * 51 + 1
        return [f"/management/job-{i}" for i in range(start, start + 51)]

    async def goto(self, url, **kwargs):
        if url != self.url:
            self.history.append(self.url)
        self.goto_urls.append(url)
        self.url = url
        if "page=" in url:
            self.page = int(url.split("page=", 1)[1].split("&", 1)[0])

    async def go_back(self, **kwargs):
        self.go_back_calls += 1
        if self.history:
            self.url = self.history.pop()

    async def wait_for_timeout(self, _value):
        return None

    async def wait_for_url(self, *_args, **_kwargs):
        return None

    def locator(self, selector):
        if selector == "a[href]":
            return _Links(self._hrefs())
        if selector == "h1":
            return _Node("Vacancy") if "/job-" in self.url else _NodeEmpty()
        return _NodeEmpty()

    def get_by_role(self, role, name=None):
        return _NodeEmpty()

@pytest.mark.asyncio
async def test_direct_pagination_preserves_filtered_listing_query_across_four_pages():
    adapter = HireHiAdapter()
    adapter._category = "менеджмент"
    adapter._exhausted = False
    page = _DirectPagingPage()
    refs = await adapter.collect_job_refs(page)
    assert len(refs) == 51

    for expected_page in (2, 3, 4):
        await adapter.open_job(page, refs[0])
        refs = await adapter.collect_more_job_refs(page)
        assert len(refs) == 51
        assert page.url == (
            f"{_DirectPagingPage.base}&page={expected_page}"
        )

    assert len(adapter._seen) == 204
    await adapter.open_job(page, refs[0])
    assert await adapter.collect_more_job_refs(page) == []
    assert adapter.search_exhausted is True
    assert page.url == f"{_DirectPagingPage.base}&page=5"


@pytest.mark.asyncio
async def test_filters_cannot_replace_category_with_home_listing(monkeypatch):
    page = _SearchPage()
    adapter = HireHiAdapter()

    async def grades(page, requested):
        assert requested == ["intern", "junior", "middle"]
        page.url = "https://hirehi.ru/?level=intern&level=junior&level=middle&page=9"

    monkeypatch.setattr(adapter, "_apply_grade_filters", grades)
    await adapter.open_search(page, {"category": "менеджмент", "grades": ["intern", "junior", "middle"]})
    assert page.url == "https://hirehi.ru/vacancies/management?level=intern&level=junior&level=middle"
    assert adapter._listing_url == page.url
    assert adapter._is_listing_page(page)


@pytest.mark.asyncio
async def test_pagination_retries_failed_page_without_skipping_it():
    adapter = HireHiAdapter()
    adapter._category = "менеджмент"
    page = _DirectPagingPage()
    await adapter.collect_job_refs(page)
    original_goto = page.goto

    async def fail(*args, **kwargs):
        raise TimeoutError("fixture navigation failure")

    page.goto = fail
    with pytest.raises(TimeoutError):
        await adapter.collect_more_job_refs(page)
    page.goto = original_goto
    refs = await adapter.collect_more_job_refs(page)
    assert refs[0].external_id == "52"
    assert page.url.endswith("page=2")


@pytest.mark.asyncio
async def test_restart_resumes_pagination_and_keeps_seen_ids():
    adapter = HireHiAdapter()
    adapter._category = "менеджмент"
    page = _DirectPagingPage()
    await adapter.collect_job_refs(page)
    await adapter.collect_more_job_refs(page)
    restored = HireHiAdapter()
    restored.restore_search_checkpoint(adapter.search_checkpoint())
    refs = await restored.collect_more_job_refs(page)
    assert refs[0].external_id == "103"
    assert len(restored._seen) == 153
    assert page.url == f"{page.base}&page=3"


@pytest.mark.asyncio
async def test_repeated_vacancies_with_rotating_blog_links_end_search():
    class RepeatingPage(_DirectPagingPage):
        def _hrefs(self):
            return ["/management/job-1", f"/blog/article-{self.page}"]

    page = RepeatingPage()
    adapter = HireHiAdapter()
    adapter._category = "менеджмент"
    assert [ref.external_id for ref in await adapter.collect_job_refs(page)] == ["1"]
    assert await adapter.collect_more_job_refs(page) == []
    assert adapter.search_exhausted
    assert await adapter.collect_more_job_refs(page) == []
    assert len(page.goto_urls) == 1


@pytest.mark.asyncio
async def test_failed_anchor_read_does_not_lose_partial_batch():
    adapter = HireHiAdapter()
    page = _RefsPage(["/management/job-1", "/management/job-2"])
    links = _Links(page.hrefs)

    async def fail(_name):
        raise TimeoutError("fixture anchor disappeared")

    links.items[1].get_attribute = fail
    original_locator = page.locator
    page.locator = lambda _: links
    with pytest.raises(TimeoutError):
        await adapter.collect_job_refs(page)
    page.locator = original_locator
    assert [ref.external_id for ref in await adapter.collect_job_refs(page)] == ["1", "2"]


@pytest.mark.parametrize("url", [
    "https://outside.example/vacancies/management",
    "https://hirehi.ru/management/job-1",
    "https://hirehi.ru/vacancies/design",
    "https://user:password@hirehi.ru/vacancies/management",
    "http://hirehi.ru/vacancies/management",
])
def test_checkpoint_rejects_urls_outside_selected_listing(url):
    with pytest.raises(ValueError):
        HireHiAdapter().restore_search_checkpoint({
            "algorithm": "hirehi_v1", "category": "менеджмент",
            "listing_url": url, "listing_page": 2, "seen": [], "exhausted": False,
        })


@pytest.mark.asyncio
async def test_open_search_tracks_page_reached_by_ui(monkeypatch):
    adapter = HireHiAdapter()
    page = _SearchPage()

    async def grades(page, requested):
        page.url = "https://hirehi.ru/vacancies/management?level=middle&page=3"

    monkeypatch.setattr(adapter, "_apply_grade_filters", grades)
    await adapter.open_search(page, {"category": "менеджмент"})
    assert adapter.search_checkpoint()["listing_page"] == 3
