import pytest

from backend.adapters.base.protocol import JobRef
from backend.adapters.hirehi import discovery, locators
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


class _InvisibleNode(_Node):
    async def is_visible(self):
        return False


class _BlockerPage(_Page):
    def __init__(self, body: str, challenge_visible: bool):
        super().__init__(body)
        self.challenge_visible = challenge_visible

    def locator(self, selector):
        if selector == locators.CAPTCHA_CHALLENGE_MARKERS:
            return _Node("challenge") if self.challenge_visible else _InvisibleNode()
        return super().locator(selector)


@pytest.mark.asyncio
async def test_vacancy_text_mentions_captcha_without_blocking():
    page = _BlockerPage(
        "Работать с Cloudflare, JavaScript challenge и CAPTCHA",
        challenge_visible=False,
    )
    assert await HireHiAdapter().detect_blockers(page) == []


@pytest.mark.asyncio
async def test_visible_structural_captcha_challenge_blocks():
    blockers = await HireHiAdapter().detect_blockers(
        _BlockerPage("Вакансия доступна", challenge_visible=True)
    )
    assert [blocker.kind for blocker in blockers] == ["captcha"]


@pytest.mark.asyncio
async def test_hidden_structural_captcha_challenge_does_not_block():
    blockers = await HireHiAdapter().detect_blockers(
        _BlockerPage("Вакансия доступна", challenge_visible=False)
    )
    assert not any(blocker.kind == "captcha" for blocker in blockers)


@pytest.mark.asyncio
async def test_profile_auth_waits_for_delayed_marker_and_ignores_login_label():
    class DelayedProfilePage:
        url = "https://hirehi.ru/"

        def __init__(self):
            self.profile_polls = 0

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_timeout(self, _value):
            return None

        def locator(self, selector):
            if selector == locators.AUTH_PROFILE_MARKERS:
                self.profile_polls += 1
                return _Node("Профиль") if self.profile_polls >= 3 else _InvisibleNode()
            return _NodeEmpty()

    state = await HireHiAdapter().get_login_state(DelayedProfilePage())
    assert state.authenticated is True


@pytest.mark.asyncio
async def test_visible_login_form_and_public_profile_are_not_authenticated():
    class LoginPage:
        url = "https://hirehi.ru/profile"

        async def goto(self, url, **kwargs):
            self.url = "https://hirehi.ru/profile"

        async def wait_for_timeout(self, _value):
            return None

        def locator(self, selector):
            if selector in {
                locators.AUTH_LOGIN_FIELDS,
                "input[type='password'], form[action*='login'], [data-testid='login-form']",
            }:
                return _Node("login form")
            return _NodeEmpty()

    class PublicPage(LoginPage):
        async def goto(self, url, **kwargs):
            self.url = "https://hirehi.ru/"

    assert (await HireHiAdapter().get_login_state(LoginPage())).authenticated is False
    assert (await HireHiAdapter().get_login_state(PublicPage())).authenticated is False


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


class _AdaptiveCollection:
    def __init__(self, items=()):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0] if self.items else _NodeEmpty()


class _AdaptiveCard:
    def __init__(self, ident, *, title="", company="", grade="", work_format="", location="", salary="", aria=""):
        self.href = f"/management/{ident}-92172"
        self.fields = {
            "title": title, "company": company, "grade": grade,
            "work_format": work_format, "location": location, "salary": salary,
        }
        self.aria = aria

    async def is_visible(self):
        return True

    async def count(self):
        return 1

    async def get_attribute(self, name):
        return self.href if name == "href" else self.aria if name == "aria-label" else None

    async def inner_text(self):
        return self.fields["title"]

    @property
    def first(self):
        return self

    def locator(self, selector):
        if selector == locators.CARD_LINKS:
            return _AdaptiveCollection([self])
        for key, marker in (
            ("title", locators.CARD_TITLE), ("company", locators.CARD_COMPANY),
            ("salary", locators.CARD_SALARY), ("grade", locators.CARD_GRADE),
            ("location", locators.CARD_LOCATION), ("work_format", locators.CARD_FORMAT),
        ):
            if selector == marker and self.fields[key]:
                return _AdaptiveCollection([_AdaptiveText(self.fields[key])])
        return _AdaptiveCollection()


class _AdaptiveText:
    def __init__(self, text):
        self.text = text
        self.first = self

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self.text


class _AdaptivePage:
    def __init__(self, cards, *, next_page=True, pro_modal=False, pro_item=True):
        self.url = "https://hirehi.ru/"
        self.cards = cards
        self.next_page = next_page
        self.pro_modal = pro_modal
        self.pro_item = pro_item
        self.events = []
        self.goto_urls = []

    async def goto(self, url, **kwargs):
        self.events.append("goto")
        self.goto_urls.append(url)
        self.url = url

    async def wait_for_timeout(self, _value):
        return None

    def locator(self, selector):
        if selector == locators.CARDS:
            return _AdaptiveCollection(self.cards)
        if selector == locators.LINKS:
            return _AdaptiveCollection(self.cards)
        if selector == locators.PAGINATION:
            return _AdaptiveCollection([_AdaptiveLink("/management?page=2")]) if self.next_page else _AdaptiveCollection()
        if selector == ".filter-checkbox-item[data-filter-type='match'][data-filter-value='me']":
            return _AdaptiveProItem(self) if self.pro_item else _NodeEmpty()
        if selector == "#proModalClose, #proModalBuyBtn":
            return _AdaptiveCollection([_AdaptiveModal(self)]) if self.pro_modal else _AdaptiveCollection()
        if selector == "#proModalClose":
            return _AdaptiveModal(self) if self.pro_modal else _NodeEmpty()
        if selector == "#proModalBuyBtn":
            return _AdaptiveModal(self) if self.pro_modal else _NodeEmpty()
        return _AdaptiveCollection()

    def get_by_text(self, *_args, **_kwargs):
        return _NodeEmpty()


class _AdaptiveLink(_AdaptiveText):
    def __init__(self, href):
        super().__init__("")
        self.href = href

    async def get_attribute(self, name):
        return self.href if name == "href" else None


class _AdaptiveProItem(_AdaptiveText):
    def __init__(self, page):
        super().__init__("подходят мне")
        self.page = page

    async def click(self):
        self.page.events.append("pro_click")


class _AdaptiveModal(_AdaptiveText):
    def __init__(self, page):
        super().__init__("")
        self.page = page

    async def is_visible(self):
        return self.page.pro_modal

    async def click(self):
        self.page.events.append("modal_close")
        self.page.pro_modal = False


def test_adaptive_url_allowlist_and_source_builders():
    adapter = HireHiAdapter()
    assert adapter.pro_recommendations_source(False) is None
    assert adapter.query_source(" product manager +sql -gambling ")["url"] == (
        "https://hirehi.ru/?search=product+manager+%2Bsql+-gambling"
    )
    assert adapter.category_source("менеджмент")["url"] == "https://hirehi.ru/vacancies/management"
    assert adapter.specialization_source("product-manager")["url"] == "https://hirehi.ru/vacancies/product-manager"
    assert adapter.validate_search_source({"url": "https://hirehi.ru/vacancies/management?grade=middle&page=2"})["url"] == (
        "https://hirehi.ru/vacancies/management?grade=middle&page=2"
    )
    for url in (
        "http://hirehi.ru/vacancies/management", "https://user:pass@hirehi.ru/vacancies/management",
        "https://hirehi.ru:443/vacancies/management", "https://hirehi.ru/vacancies/management#x",
        "https://hirehi.ru/vacancies/management?unknown=x", "https://hirehi.ru/management/job-92172",
    ):
        with pytest.raises(ValueError):
            adapter.validate_search_source({"url": url})
    with pytest.raises(ValueError):
        adapter.validate_search_source({"url": "https://hirehi.ru/vacancies/management", "page": -1})


@pytest.mark.asyncio
async def test_extract_job_without_sidebar_uses_main_and_page_is_not_visibility_checked():
    class Collection:
        def __init__(self, items=()):
            self.items = list(items)

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

        @property
        def first(self):
            return self.items[0]

    class Main:
        async def is_visible(self):
            return True

        async def inner_text(self):
            return "РћРїРёСЃР°РЅРёРµ\nBuild reliable systems"

    class Page:
        url = "https://hirehi.ru/management/engineer-98765"

        def locator(self, selector):
            if selector == "h1":
                return Collection([_Node("Engineer")])
            if selector == "main":
                return Collection([Main()])
            # No sidebar or dedicated description fields are present.
            return Collection()

        async def is_visible(self, selector):
            raise AssertionError("Page.is_visible requires a selector and must not be called as a Locator")

    posting = await HireHiAdapter().extract_job(Page())

    assert posting.title == "Engineer"
    assert posting.description == "РћРїРёСЃР°РЅРёРµ\nBuild reliable systems"
    assert posting.external_id == "98765"


@pytest.mark.asyncio
async def test_adaptive_card_metadata_pagination_and_repeated_marker():
    adapter = HireHiAdapter()
    card = _AdaptiveCard("job", title="Product Manager", company="Acme", grade="senior", work_format="remote", location="Россия", salary="200 000 ₽")
    page = _AdaptivePage([card], next_page=True)
    result = await adapter.read_discovery_page(page, adapter.category_source("management"), 0)
    assert result["cards"]["92172"]["company"] == "Acme"
    assert result["cards"]["92172"]["salary_text"] == "200 000 ₽"
    assert "page=2" not in page.goto_urls[0]
    result2 = await adapter.read_discovery_page(page, adapter.category_source("management"), 1)
    assert "page=2" in page.goto_urls[1]
    assert result2["repeated"] is True
    assert adapter.last_discovery_result["repeated"] is True
    assert adapter.job_refs_from_cards([result["cards"]["92172"]])[0].external_id == "92172"
    empty = _AdaptivePage([], next_page=False)
    terminal = await adapter.read_discovery_page(empty, adapter.category_source("management"), 0)
    assert terminal["terminal"] is True and terminal["refs"] == []
    with pytest.raises(ValueError):
        await adapter.read_discovery_page(empty, adapter.category_source("management"), -1)
    assert empty.goto_urls == ["https://hirehi.ru/vacancies/management"]


@pytest.mark.asyncio
async def test_pro_navigation_is_after_goto_and_free_tier_closes_without_purchase():
    adapter = HireHiAdapter()
    page = _AdaptivePage([], pro_modal=True)
    result = await adapter.read_discovery_page(page, adapter.pro_recommendations_source(True), 0)
    assert result["unavailable"] is True and result["terminal"] is True and not result["refs"]
    assert page.events == ["goto", "pro_click", "modal_close"]
    assert "buy" not in page.events
    again = await adapter.read_discovery_page(page, adapter.pro_recommendations_source(True), 0)
    assert again["unavailable"] is True and page.events == ["goto", "pro_click", "modal_close", "goto"]


@pytest.mark.asyncio
async def test_pro_success_collects_cards_after_filter_click():
    adapter = HireHiAdapter()
    page = _AdaptivePage([_AdaptiveCard("good", title="Product Manager")], next_page=False)
    result = await adapter.read_discovery_page(page, adapter.pro_recommendations_source(True), 0)
    assert result["unavailable"] is False and [ref.external_id for ref in result["refs"]] == ["92172"]
    assert page.events[:2] == ["goto", "pro_click"]


class _RelatedSection:
    def __init__(self, links):
        self.links = links

    async def is_visible(self):
        return True

    def locator(self, selector):
        return _AdaptiveCollection(self.links)


class _RelatedPage:
    url = "https://hirehi.ru/management/source-92172"

    def __init__(self):
        self.category = _AdaptiveLink("/vacancies/product-manager")
        self.similar = _RelatedSection([_AdaptiveLink("/management/other-123"), _AdaptiveLink("/blog/article-1")])

    def locator(self, selector):
        if selector == locators.LINKS:
            return _AdaptiveCollection([self.category])
        if selector == locators.RELATED_VACANCIES:
            return _AdaptiveCollection([self.similar])
        return _AdaptiveCollection()


@pytest.mark.asyncio
async def test_visible_sources_and_related_refs_are_scoped_to_hirehi_ui_sections():
    page = _RelatedPage()
    adapter = HireHiAdapter()
    sources = await adapter.collect_visible_sources(page)
    related = await adapter.collect_related_refs(page)
    assert any(item["url"] == "https://hirehi.ru/vacancies/product-manager" for item in sources)
    assert {item.external_id for item in related} == {"123"}
    assert not any("blog" in item["url"] for item in sources)


@pytest.mark.asyncio
async def test_protected_profile_markers_override_misleading_login_button_label():
    class ProfilePage:
        url = "https://hirehi.ru/management/source-92172"

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_timeout(self, _value):
            return None

        def locator(self, selector):
            if selector.startswith("#profileBlockDesktop"):
                return _Node("Профиль")
            return _NodeEmpty()

    state = await HireHiAdapter().get_login_state(ProfilePage())
    assert state.authenticated is True


def test_observed_management_detail_route_is_a_valid_related_vacancy():
    parsed = discovery._vacancy_url(
        HireHiAdapter(), "/management/product-owner-92172", "https://hirehi.ru/"
    )
    assert parsed == ("https://hirehi.ru/management/product-owner-92172", "92172")


def test_builders_canonicalize_live_filter_values_and_build_source_passes_filters():
    adapter = HireHiAdapter()
    filters = {
        "level": ["senior", "intern", "senior"],
        "format": ["гибрид", "удалённо"],
        "english": "english",
        "direct_contact": ["telegram", "direct_contact"],
        "salary_from": 100000,
        "salary_to": 250000,
    }
    source = adapter.query_source("product manager", filters=filters)
    assert "level=intern&level=senior" in source["url"]
    assert "format=%D0%B3%D0%B8%D0%B1%D1%80%D0%B8%D0%B4&format=%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D0%BE" in source["url"]
    assert "english=english" in source["url"] and "salary=range%3A100000%3A250000" in source["url"]
    assert adapter.specialization_source("product-manager", filters=filters)["url"].startswith("https://hirehi.ru/vacancies/product-manager?")
    assert adapter.coverage_source(filters=filters)["url"].startswith("https://hirehi.ru/?")
    built = adapter.build_source({"family": "query", "query": "product manager", "filters": filters})
    assert built["url"] == source["url"]


@pytest.mark.parametrize("filters", [
    {"page": 2}, {"unknown": ["x"]}, {"level": ["principal"]},
    {"format": ["remote"]}, {"english": "maybe"},
    {"direct_contact": ["phone"]}, {"salary_from": -1},
    {"salary_to": True}, {"salary_to": 1_000_000_001},
])
def test_planner_filters_reject_unknown_or_invalid_values_before_navigation(filters):
    with pytest.raises(ValueError):
        HireHiAdapter().query_source("product manager", filters=filters)


def test_detail_reference_rejects_credentials_port_fragment_and_query():
    adapter = HireHiAdapter()
    for href in (
        "https://user:pass@hirehi.ru/management/job-92172",
        "https://hirehi.ru:443/management/job-92172",
        "https://hirehi.ru/management/job-92172?x=1",
        "https://hirehi.ru/management/job-92172#section",
    ):
        assert discovery._vacancy_url(adapter, href, "https://hirehi.ru/") is None


def test_salary_builder_uses_hirehi_range_query_and_region_is_strict():
    adapter = HireHiAdapter()
    assert adapter.query_source("manager", filters={"salary_from": 300000, "salary_to": 600000})["url"] == (
        "https://hirehi.ru/?salary=range%3A300000%3A600000&search=manager"
    )
    assert adapter.query_source("manager", filters={"salary_from": 300000})["url"] == (
        "https://hirehi.ru/?salary=range%3A300000%3A&search=manager"
    )
    assert "region=CIS&region=Russia" in adapter.query_source(
        "manager", filters={"region": ["Russia", "CIS", "Russia"]}
    )["url"]
    for filters in (
        {"salary": "range:1:2"}, {"salary_from": 2, "salary_to": 1},
        {"salary_from": None, "salary_to": None},
        {"region": ["Mars"]}, {"country": ["Russia"]},
    ):
        with pytest.raises(ValueError):
            adapter.query_source("manager", filters=filters)


def test_visible_salary_query_requires_canonical_range_grammar():
    adapter = HireHiAdapter()
    valid = adapter.validate_search_source({"url": "https://hirehi.ru/?salary=range:300000:600000"})
    assert valid["url"] == "https://hirehi.ru/?salary=range%3A300000%3A600000"
    for url in (
        "https://hirehi.ru/?salary=300000-600000",
        "https://hirehi.ru/?salary=range::",
        "https://hirehi.ru/?salary=range:600000:300000",
        "https://hirehi.ru/?salary=range:-1:2",
        "https://hirehi.ru/?salary_from=300000",
    ):
        with pytest.raises(ValueError):
            adapter.validate_search_source({"url": url})
