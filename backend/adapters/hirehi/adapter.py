from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from backend.adapters.base.errors import CaptchaRequired, JobDescriptionUnavailable
from backend.adapters.base.protocol import (
    AdapterManifest,
    ApplicationRoute,
    Blocker,
    EmployerContact,
    JobRef,
    LoginState,
)
from backend.adapters.base.resume_import import ResumeImportMixin
from backend.adapters.hh.salary import parse_salary
from backend.adapters.hirehi import discovery, locators
from backend.adapters.hirehi.resume import POLICY as resume_policy
from backend.adapters.hirehi.resume import extractor as resume_extractor
from backend.schemas.domain import JobPosting, Salary


class HireHiAdapter(ResumeImportMixin):
    GRADES = ("intern", "junior", "middle", "senior", "lead", "head")
    CATEGORY_PATHS = {
        "все вакансии": "/",
        "дизайн": "/vacancies/design",
        "разработка": "/vacancies/development",
        "devops": "/vacancies/devops",
        "менеджмент": "/vacancies/management",
        "тестирование": "/vacancies/qa",
        "аналитика": "/vacancies/analytics",
        "маркетинг": "/vacancies/marketing",
        "продажи": "/vacancies/sales",
        "финансы": "/vacancies/finance",
        "рекрутинг": "/vacancies/recruiting",
    }
    CATEGORIES = frozenset(CATEGORY_PATHS)
    SPECIALIZATIONS = discovery.SPECIALIZATION_SLUGS
    site_id = "hirehi"
    home_url = "https://hirehi.ru/"
    display_name = "HireHi"
    allowed_domains = ("hirehi.ru", "www.hirehi.ru")
    manifest = AdapterManifest(
        site_id=site_id,
        display_name=display_name,
        allowed_domains=allowed_domains,
        supports_submission=True,
        supports_resume_import=True,
        supports_public_resume_url=True,
        supports_account_resume_list=False,
    )
    resume_policy = resume_policy
    resume_extractor = resume_extractor

    @property
    def search_exhausted(self) -> bool:
        return getattr(self, "_exhausted", False)

    def search_checkpoint(self) -> dict:
        """The workflow saves this cursor together with its pending vacancy queue."""
        return {
            "algorithm": "hirehi_v1",
            "category": self._category,
            "listing_url": self._listing_url,
            "listing_page": self._listing_page,
            "seen": sorted(self._seen),
            "exhausted": self.search_exhausted,
        }

    def restore_search_checkpoint(self, checkpoint: dict) -> None:
        category = checkpoint.get("category")
        parsed = urlparse(checkpoint.get("listing_url", ""))
        if (
            checkpoint.get("algorithm") != "hirehi_v1"
            or category not in self.CATEGORY_PATHS
            or parsed.hostname not in self.allowed_domains
            or parsed.scheme != urlparse(self.home_url).scheme
            or parsed.username or parsed.password
            or parsed.path.rstrip("/") != self.CATEGORY_PATHS[category].rstrip("/")
        ):
            raise ValueError("Сохранённый поиск HireHi содержит недопустимый адрес или категорию")
        listing_page = int(checkpoint["listing_page"])
        if listing_page < 1:
            raise ValueError("Недопустимая страница поиска HireHi")
        self._category = category
        self._listing_url = checkpoint["listing_url"]
        self._listing_page = listing_page
        self._seen = set(checkpoint["seen"])
        self._exhausted = bool(checkpoint["exhausted"])

    async def start(self, context, settings: dict) -> None:
        return None

    # Adaptive HireHi search -------------------------------------------------
    # These methods form a separate source contract from the legacy v1
    # category cursor below.  Keeping the old methods intact is intentional:
    # existing sessions/checkpoints must remain restartable.
    @staticmethod
    def normalize_search_query(value: str) -> str:
        return discovery._normalize_query(value)

    def validate_search_source(self, spec: dict) -> dict:
        if not isinstance(spec, dict) or "url" not in spec:
            raise ValueError("Недопустимый источник поиска HireHi")
        if "page" in spec:
            discovery.validate_page_number(spec["page"])
        canonical = discovery.validate_url(self, spec["url"], spec)
        return {**spec, "url": canonical, "cluster": spec.get("cluster", "")}

    def build_source(self, spec: dict) -> dict | None:
        """Turn a planner's structured source into a validated visible URL."""
        raw = dict(spec or {})
        if raw.get("url"):
            return self.validate_search_source(raw)
        family = raw.get("family", raw.get("kind", "query"))
        cluster = raw.get("cluster", "")
        filters = raw.get("filters")
        if family == "query":
            source = self.query_source(raw.get("query", ""), cluster, filters=filters)
        elif family == "specialization":
            value = raw.get("category") or raw.get("query") or ""
            slug = self._source_slug(value)
            source = self.specialization_source(slug, filters=filters, cluster=cluster) if slug else None
        elif family in {"category", "coverage"}:
            value = raw.get("category") or raw.get("query") or ""
            slug = self._source_slug(value)
            source = self.category_source(slug, filters=filters, cluster=cluster) if slug else self.coverage_source(filters=filters, cluster=cluster)
        elif family in {"pro_recommendations", "recommendations"}:
            if filters:
                raise ValueError("Фильтры HireHi не поддерживаются источником PRO")
            source = self.pro_recommendations_source(True)
        else:
            # Related sources are created only from visible cards.  A planner
            # may not manufacture one.
            return None
        if source is None:
            return None
        return {**raw, **source, "family": family, "source_id": raw.get("source_id") or raw.get("key")}

    @staticmethod
    def _source_slug(value: str) -> str:
        value = re.sub(r"[^a-z0-9а-яё -]", "", str(value or "").casefold())
        value = re.sub(r"\s+", "-", value.strip())
        if value in {"all", "all-vacancies", "все", "все-вакансии"}:
            return ""
        return value

    @property
    def last_discovery_result(self) -> dict | None:
        """Stable read-only snapshot for the adaptive engine/checkpoint layer."""
        result = getattr(self, "_last_discovery_result", None)
        return dict(result) if result is not None else None

    def discovery_result(self) -> dict | None:
        return self.last_discovery_result

    @property
    def discovery_terminal(self) -> bool:
        return bool((getattr(self, "_last_discovery_result", None) or {}).get("terminal", False))

    @property
    def discovery_repeated(self) -> bool:
        return bool((getattr(self, "_last_discovery_result", None) or {}).get("repeated", False))

    @property
    def discovery_unavailable(self) -> bool:
        return bool((getattr(self, "_last_discovery_result", None) or {}).get("unavailable", False))

    @property
    def discovered_sources(self) -> list[dict]:
        return list((getattr(self, "_last_discovery_result", None) or {}).get("sources", []))

    async def open_source(self, page, spec: dict, cursor=0) -> None:
        page_number = discovery.validate_page_number(cursor)
        self._last_discovery_result = await discovery.read_page(self, page, spec, page_number)

    async def collect_card_refs(self, page) -> list[dict[str, object]]:
        result = getattr(self, "_last_discovery_result", None)
        if result is None:
            refs, cards = await discovery.card_refs(self, page)
            result = {
                "refs": refs, "cards": cards, "terminal": False,
                "repeated": False, "unavailable": False, "sources": [],
            }
            self._last_discovery_result = result
        else:
            refs, cards = result.get("refs", []), result.get("cards", {})
        return [{**cards.get(ref.external_id, {"external_id": ref.external_id, "url": ref.url}),
                 "external_id": ref.external_id, "url": ref.url} for ref in refs]

    @staticmethod
    def job_refs_from_cards(cards: list[dict[str, object]]) -> list[JobRef]:
        """Deterministically convert card dictionaries to the core JobRef type."""
        result, seen = [], set()
        for card in cards or []:
            external_id = str(card.get("external_id") or "").strip()
            url = str(card.get("url") or "").strip()
            if external_id and url and external_id not in seen:
                seen.add(external_id)
                result.append(JobRef(external_id=external_id, url=url))
        return result

    def query_source(self, query: str, cluster: str = "", filters: dict | None = None) -> dict:
        if isinstance(cluster, dict) and filters is None:
            filters, cluster = cluster, ""
        return discovery.query_spec(self, query, cluster, filters)

    def specialization_source(self, slug: str, filters: dict | None = None, cluster: str = "") -> dict:
        if isinstance(filters, str) and not cluster:
            cluster, filters = filters, None
        return discovery.specialization_spec(self, slug, filters, cluster)

    def category_source(self, category: str, filters: dict | None = None, cluster: str = "") -> dict:
        # Accept either a visible slug or one of the legacy localized names.
        if isinstance(filters, str) and not cluster:
            cluster, filters = filters, None
        raw = str(category).strip().lower()
        mapped = self.CATEGORY_PATHS.get(raw)
        slug = (mapped.rstrip("/").rsplit("/", 1)[-1] if mapped else raw.strip("/").rsplit("/", 1)[-1])
        if slug == "":
            return discovery.coverage_spec(self, filters, cluster)
        return discovery.category_spec(self, slug, filters, cluster)

    def coverage_source(self, category: str | None = None, filters: dict | None = None, cluster: str = "") -> dict:
        return self.category_source(category or "все вакансии", filters, cluster)

    def pro_recommendations_source(self, pro_enabled: bool = False) -> dict | None:
        return discovery.pro_spec(self) if pro_enabled else None

    async def ensure_pro_filter(self, page) -> bool:
        """Try the visible Match Me filter, closing a free-tier upsell safely."""
        self._pro_unavailable = False
        attempted = getattr(self, "_pro_unavailable_urls", set())
        if page.url in attempted:
            self._pro_unavailable = True
            return False
        try:
            item = page.locator(".filter-checkbox-item[data-filter-type='match'][data-filter-value='me']").first
            if not await item.count() or not await item.is_visible():
                item = page.get_by_text(re.compile(r"^подходят мне$", re.I)).first
            if not await item.count() or not await item.is_visible():
                self._pro_unavailable = True
                attempted.add(page.url)
                self._pro_unavailable_urls = attempted
                return False
            await item.click()
            close = page.locator("#proModalClose").first
            buy = page.locator("#proModalBuyBtn").first
            combined = page.locator("#proModalClose, #proModalBuyBtn")
            modal_visible = (
                (await close.count() and await close.is_visible())
                or (await buy.count() and await buy.is_visible())
                or (await combined.count() and await combined.first.is_visible())
            )
            if modal_visible:
                if await close.count() and await close.is_visible():
                    await close.click()
                self._pro_unavailable = True
                attempted.add(page.url)
                self._pro_unavailable_urls = attempted
                return False
            return True
        except Exception:
            # Tier/access failures are an unavailable source, not a source
            # failure that should trigger scheduler retries.
            self._pro_unavailable = True
            attempted.add(page.url)
            self._pro_unavailable_urls = attempted
            return False

    async def read_discovery_page(self, page, spec: dict, page_number: int) -> dict:
        page_number = discovery.validate_page_number(page_number)
        self.validate_search_source(spec)
        self._last_discovery_result = await discovery.read_page(self, page, spec, page_number)
        return self._last_discovery_result

    async def collect_visible_sources(self, page, context: str = "listing") -> list[dict]:
        return await discovery.visible_sources(self, page, context=context)

    async def collect_related_refs(self, page) -> list[JobRef]:
        return await discovery.related_refs(self, page)

    @staticmethod
    async def _has_visible_locator(page, selector: str) -> bool:
        """Return whether at least one element in ``selector`` is visible.

        Keeping this check visibility-aware is important for HireHi: challenge
        widgets and login forms can remain in a hidden template while the
        actual vacancy/profile is rendered.  The small fallback for page
        doubles also keeps the adapter usable with lightweight integrations
        that expose only ``first`` rather than ``nth``.
        """
        try:
            matches = page.locator(selector)
            count = await matches.count()
            for index in range(count):
                node = matches.nth(index) if hasattr(matches, "nth") else matches.first
                if await node.is_visible():
                    return True
        except Exception:
            return False
        return False

    async def get_login_state(self, page) -> LoginState:
        goto = getattr(page, "goto", None)
        if goto is None:
            return LoginState(authenticated=False, message="Войдите в HireHi вручную")
        authenticated = False
        try:
            await goto("https://hirehi.ru/profile", wait_until="commit", timeout=15_000)
            wait_for_timeout = getattr(page, "wait_for_timeout", None)
            # Server-rendered shell and client-side profile data can settle a
            # little after the initial ``commit``.  Poll for a bounded period,
            # but re-check the URL on every pass so a late redirect to /login
            # is still fail-closed.
            for attempt in range(11):
                parsed = urlparse(page.url)
                protected_route = parsed.hostname in self.allowed_domains and (
                    parsed.path == "/profile" or parsed.path.startswith("/profile/")
                )
                if not protected_route:
                    break

                login_visible = await self._has_visible_locator(
                    page, locators.AUTH_LOGIN_FIELDS
                ) or await self._has_visible_locator(
                    page, "input[type='password'], form[action*='login'], [data-testid='login-form']"
                )
                if login_visible:
                    break

                profile_visible = await self._has_visible_locator(
                    page, locators.AUTH_PROFILE_MARKERS
                ) or await self._has_visible_locator(
                    page, locators.AUTH_PROFILE_LEGACY_MARKERS
                )
                if profile_visible:
                    authenticated = True
                    break
                if wait_for_timeout is None or attempt == 10:
                    break
                await wait_for_timeout(500)
        except Exception:
            authenticated = False
        return LoginState(
            authenticated=authenticated,
            message="Вход выполнен" if authenticated else "Войдите в HireHi вручную",
        )

    async def open_search(self, page, filters: dict) -> None:
        category = str(filters.get("category", "все вакансии")).strip().lower()
        if category not in self.CATEGORIES:
            raise ValueError(f"Неподдерживаемая категория HireHi: {category}")
        self._category = category
        self._seen: set[str] = set()
        self._exhausted = False
        self._listing_url = ""
        self._listing_page = 1
        await page.goto(self.home_url, wait_until="domcontentloaded", timeout=15_000)
        await self._wait_for_search_dom(page)
        category_pattern = re.compile(r"^" + re.escape(category) + r"$", re.I)
        expected_path = self.CATEGORY_PATHS[category]
        choice = None
        for _ in range(20):
            # Some layouts expose the category sidebar directly, without the
            # category picker button. Prefer a visible link with the exact
            # category route to avoid hidden/mobile duplicate matches.
            links = page.locator(locators.LINKS)
            for index in range(await links.count()):
                link = links.nth(index)
                href = await link.get_attribute("href")
                if await link.is_visible() and urlparse(urljoin(page.url, href or "")).path.rstrip("/") == expected_path.rstrip("/") and (await link.inner_text()).strip().lower() == category:
                    choice = link
                    break
            if choice is None:
                opener = await self._first_visible(
                    page.get_by_role("button", name="Выбрать категорию вакансий")
                )
                if opener is None:
                    opener = await self._first_visible(
                        page.get_by_role("button", name=re.compile(r"^Категория\s", re.I))
                    )
                if opener is not None:
                    await opener.click()
                    await page.wait_for_timeout(100)
            dialog = await self._first_visible(page.get_by_role("dialog", name="Категория"))
            if dialog is not None:
                choice = await self._first_visible(
                    dialog.get_by_role("link", name=category_pattern)
                )
            if choice is None:
                for locator in (
                    page.get_by_role("link", name=category_pattern),
                    page.get_by_text(category_pattern),
                ):
                    choice = await self._first_visible(locator)
                    if choice is not None:
                        break
            if choice is not None:
                break
            await page.wait_for_timeout(250)
        if choice is None:
            raise ValueError(f"Категория не найдена в UI HireHi: {category}")
        await choice.click()
        await self._wait_for_results(page, category)
        await self._apply_grade_filters(page, filters.get("grades", []))
        for kind in filters.get("application_types", filters.get("types", [])):
            value = "direct_contact" if kind in {"direct", "direct_contact"} else "hirehi"
            item = page.locator(
                f"div.filter-checkbox-item[data-filter-type='{value}'][data-filter-value='{value}']"
            )
            if await item.count() and await item.first.is_visible():
                await item.first.click()
        await self._ensure_category_listing(page)
        self._listing_url = page.url
        self._listing_page = self._page_number(page.url)

    async def _apply_grade_filters(self, page, grades) -> None:
        """Select requested HireHi grade chips without toggling unrelated ones."""
        requested = [str(grade).strip().lower() for grade in (grades or [])]
        if not requested:
            return
        unknown = [grade for grade in requested if grade not in self.GRADES]
        if unknown:
            raise ValueError(f"Неподдерживаемый грейд HireHi: {unknown[0]}")

        groups = page.locator(".filter-group")
        for index in range(await groups.count()):
            group = groups.nth(index)
            if not await group.is_visible():
                continue
            title = group.locator(".filter-title").first
            if await title.count() and (await title.inner_text()).strip().lower() == "грейд":
                chips = group.locator(".filter-chip")
                for grade in requested:
                    option = None
                    for chip_index in range(await chips.count()):
                        chip = chips.nth(chip_index)
                        label = chip.locator(".chip-text").first
                        if await chip.is_visible() and await label.count() and (await label.inner_text()).strip().lower() == grade:
                            option = chip
                            break
                    if option is None:
                        raise RuntimeError(f"Грейд не найден в UI HireHi: {grade}")
                    if await self._is_selected(option) is not True:
                        await option.click()
                await page.wait_for_timeout(100)
                await self._wait_for_search_dom(page)
                return

        opener = page.get_by_role("button", name=re.compile(r"^грейд$", re.I)).first
        if not await opener.count():
            opener = page.get_by_text(re.compile(r"^грейд$", re.I)).first
        if not await opener.count() or not await opener.is_visible():
            raise RuntimeError("Не найден фильтр грейда HireHi")
        await opener.click()

        for grade in requested:
            option = page.get_by_role("button", name=re.compile(r"^" + re.escape(grade) + r"(?:\\s|$)", re.I)).first
            if not await option.count():
                option = page.get_by_text(re.compile(r"^" + re.escape(grade) + r"(?:\\s|$)", re.I)).first
            if not await option.count() or not await option.is_visible():
                raise RuntimeError(f"Грейд не найден в UI HireHi: {grade}")
            selected = await self._is_selected(option)
            if selected is not True:
                await option.click()
        await page.wait_for_timeout(100)
        await self._wait_for_search_dom(page)

    @staticmethod
    async def _is_selected(option):
        for attribute in ("aria-pressed", "aria-checked", "data-selected", "data-state"):
            value = await option.get_attribute(attribute)
            if value is not None:
                return value.lower() in {"true", "checked", "selected", "on"}
        classes = (await option.get_attribute("class") or "").lower().split()
        if any(marker in classes for marker in ("selected", "active", "checked")):
            return True
        return None

    @staticmethod
    async def _first_visible(locator):
        """Return the first visible match; HireHi renders hidden mobile duplicates."""
        for index in range(await locator.count()):
            item = locator.nth(index) if hasattr(locator, "nth") else locator.first
            if not hasattr(item, "is_visible") or await item.is_visible():
                return item
        return None

    async def _wait_for_search_dom(self, page) -> None:
        try:
            await page.get_by_role("textbox", name="Компания, должность или +навык").first.wait_for(
                state="visible", timeout=8_000
            )
            return
        except Exception:
            pass
        try:
            await page.locator(locators.LINKS).first.wait_for(
                state="attached", timeout=8_000
            )
        except Exception:
            return

    async def _wait_for_results(self, page, category: str) -> None:
        expected_path = self.CATEGORY_PATHS[category]
        expected_url = re.compile(
            rf"{re.escape(urljoin(self.home_url, expected_path))}(?:[?#].*)?$",
            re.I,
        )
        with suppress(Exception):
            await page.wait_for_url(expected_url, timeout=8_000)
        await self._ensure_category_listing(page)
        try:
            await page.locator(locators.LINKS).first.wait_for(
                state="attached", timeout=8_000
            )
        except Exception:
            await page.wait_for_timeout(250)

    def _is_listing_page(self, page) -> bool:
        """Reject vacancy detail pages when unwinding browser history."""
        parsed = urlparse(page.url)
        path = parsed.path.rstrip("/") or "/"
        expected = self.CATEGORY_PATHS.get(getattr(self, "_category", "все вакансии"), "/").rstrip("/") or "/"
        return parsed.hostname in self.allowed_domains and path == expected

    async def _ensure_category_listing(self, page) -> None:
        """A category click/filter may leave the UI on the unscoped home feed."""
        if self._is_listing_page(page):
            return
        parsed = urlparse(page.url)
        if parsed.hostname not in self.allowed_domains or (parsed.path.rstrip("/") or "/") not in self.CATEGORY_PATHS.values():
            raise RuntimeError("HireHi не открыла страницу выдачи")
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
        target = urlparse(urljoin(self.home_url, self.CATEGORY_PATHS[self._category]))
        await page.goto(
            urlunparse(target._replace(query=urlencode(query))),
            wait_until="domcontentloaded", timeout=15_000,
        )
        await self._wait_for_search_dom(page)
        if not self._is_listing_page(page):
            raise RuntimeError("HireHi не сохранила выбранную категорию")

    async def _refs(self, page) -> list[JobRef]:
        links = page.locator(locators.LINKS)
        refs = {}
        # The results page is an infinite list; do not truncate the DOM after
        # 200 anchors (navigation and footer links are filtered below).
        for i in range(await links.count()):
            href = await links.nth(i).get_attribute("href")
            url = urljoin(page.url, href or "")
            parsed = urlparse(url)
            match = re.fullmatch(r"/(?!vacancies(?:/|$)|vacancy(?:/|$))[^/?#]+/[^/?#]+-(\d+)/?", parsed.path)
            if (
                parsed.hostname in self.allowed_domains
                and match
                and f"/vacancies/{parsed.path.split('/')[1]}" in self.CATEGORY_PATHS.values()
                and match.group(1) not in self._seen
            ):
                refs[match.group(1)] = JobRef(external_id=match.group(1), url=url)
        # Do not lose a partial batch when reading an anchor fails mid-page.
        self._seen.update(refs)
        return list(refs.values())

    async def collect_job_refs(self, page) -> list[JobRef]:
        if not hasattr(self, "_seen"):
            self._seen = set()
        await self._wait_for_search_dom(page)
        if not self._is_listing_page(page):
            raise RuntimeError("Сбор вакансий HireHi вызван не на странице выдачи")
        if not getattr(self, "_listing_url", ""):
            self._listing_url = page.url
            self._listing_page = self._page_number(page.url)
        return await self._refs(page)

    @staticmethod
    def _page_number(url: str) -> int:
        raw_page = dict(parse_qsl(urlparse(url).query)).get("page", "1")
        try:
            return max(1, int(raw_page))
        except ValueError:
            return 1

    async def collect_more_job_refs(self, page) -> list[JobRef]:
        if self.search_exhausted:
            return []
        listing_url = getattr(self, "_listing_url", "")
        if not listing_url:
            raise RuntimeError("Не сохранена URL выдачи HireHi")
        next_page = getattr(self, "_listing_page", 1) + 1
        parsed = urlparse(listing_url)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
        query.append(("page", str(next_page)))
        next_url = urlunparse(parsed._replace(query=urlencode(query)))
        await page.goto(next_url, wait_until="domcontentloaded", timeout=15_000)
        if not self._is_listing_page(page):
            raise RuntimeError("HireHi не открыла страницу выдачи")
        await self._wait_for_search_dom(page)
        for _ in range(12):
            refs = await self._refs(page)
            if refs:
                self._listing_url = next_url
                self._listing_page = next_page
                return refs
            await page.wait_for_timeout(250)
        self._exhausted = True
        self._listing_url = next_url
        self._listing_page = next_page
        return []


    async def open_job(self, page, ref: JobRef) -> None:
        if urlparse(ref.url).hostname not in self.allowed_domains:
            raise ValueError("Переход за пределы HireHi запрещён")
        await page.goto(ref.url, wait_until="commit", timeout=15_000)
        heading = page.locator("h1").first
        try:
            await heading.wait_for(state="visible", timeout=8_000)
            for _ in range(8):
                if (await heading.inner_text()).strip():
                    return
                await page.wait_for_timeout(250)
            raise RuntimeError("Страница вакансии HireHi не готова: заголовок пуст")
        except Exception as exc:
            raise RuntimeError("Страница вакансии HireHi не готова: заголовок не найден") from exc

    async def _text(self, page, selector: str, label: str, required=True) -> str:
        selectors = [item.strip() for item in selector.split(",") if item.strip()]
        for candidate_selector in selectors:
            loc = page.locator(candidate_selector)
            for i in range(await loc.count()):
                item = loc.nth(i)
                if hasattr(item, "is_visible") and not await item.is_visible():
                    continue
                value = (await item.inner_text()).strip()
                if value:
                    return value
        if required:
            raise ValueError(f"Не удалось извлечь {label}")
        return ""

    async def _sidebar_values(self, page, selector: str, *, exclude_market=False) -> list[str]:
        """Read visible sidebar fields, excluding market-comparison values."""
        roots = page.locator(locators.VACANCY_SIDEBAR)
        root_count = await roots.count()
        has_sidebar_roots = root_count > 0
        if not root_count:
            roots, root_count = page, 1
        result: list[str] = []
        for root_index in range(root_count):
            root = roots.nth(root_index) if hasattr(roots, "nth") else roots
            if has_sidebar_roots and hasattr(root, "is_visible") and not await root.is_visible():
                continue
            fields = root.locator(selector)
            for index in range(await fields.count()):
                item = fields.nth(index) if hasattr(fields, "nth") else fields
                if hasattr(item, "is_visible") and not await item.is_visible():
                    continue
                try:
                    value = " ".join((await item.inner_text(timeout=5_000)).split())
                except TypeError:
                    value = " ".join((await item.inner_text()).split())
                except Exception:
                    continue
                if not value or (exclude_market and re.search(
                    r"market|рынк|средн|медиан|comparison|сравн", value, re.I
                )):
                    continue
                if value not in result:
                    result.append(value)
        return result

    async def _sidebar_labeled_values(self, page, labels: set[str]) -> list[str]:
        """Read the value paired with a visible semantic sidebar label.

        The live vacancy page uses ``.sidebar-item`` rows without test IDs.
        Pairing label and value within one row prevents market-comparison and
        related-vacancy cards from being mistaken for the current vacancy.
        """
        roots = page.locator(locators.VACANCY_SIDEBAR)
        result: list[str] = []
        for root_index in range(await roots.count()):
            root = roots.nth(root_index)
            rows = root.locator(".sidebar-item")
            for row_index in range(await rows.count()):
                row = rows.nth(row_index)
                if hasattr(row, "is_visible") and not await row.is_visible():
                    continue
                label_locator = row.locator(".sidebar-label")
                value_locator = row.locator(".sidebar-value")
                if not await label_locator.count() or not await value_locator.count():
                    continue
                label = " ".join((await label_locator.first.inner_text()).split()).casefold()
                if label not in labels:
                    continue
                value = " ".join((await value_locator.first.inner_text()).split())
                if value and value not in result:
                    result.append(value)
        return result

    async def _vacancy_skills(self, page) -> list[str]:
        sections = page.locator(locators.VACANCY_SKILLS_SECTION)
        result: list[str] = []
        for section_index in range(await sections.count()):
            section = sections.nth(section_index)
            if hasattr(section, "is_visible") and not await section.is_visible():
                continue
            skills = section.locator(locators.VACANCY_SKILLS)
            for index in range(await skills.count()):
                item = skills.nth(index)
                if hasattr(item, "is_visible") and not await item.is_visible():
                    continue
                value = " ".join((await item.inner_text()).split())
                if value and value not in result:
                    result.append(value)
        if result:
            return result
        # Compatibility hook for the older data-testid fixture.
        return await self._sidebar_values(page, "[data-testid='vacancy-skill']")

    @staticmethod
    def _split_sidebar_format(values: list[str]) -> tuple[str | None, list[str]]:
        formats = {
            "гибрид", "удалённо", "удаленно", "офис", "remote", "hybrid", "office",
            "on-site", "onsite",
        }
        found: list[str] = []
        locations: list[str] = []
        for value in values:
            parts = value.split(maxsplit=1)
            if parts and parts[0].casefold() in formats:
                found.append(parts[0])
                if len(parts) == 2 and parts[1].strip():
                    locations.append(parts[1].strip())
            else:
                found.append(value)
        return (", ".join(found) if found else None), locations

    @staticmethod
    def _parse_hirehi_salary(value: str | None) -> Salary | None:
        if not value:
            return None
        # HireHi's sidebar is plain visible text.  Keep this bounded parser
        # local so a market-comparison number cannot be inferred as pay.
        numbers = [int(part.replace(" ", "")) for part in re.findall(r"\d[\d\s]{2,}", value)]
        if not numbers:
            return parse_salary(value)
        currency = "RUB" if re.search(r"₽|руб|rub|rur", value, re.I) else None
        if currency is None:
            return parse_salary(value)
        lowered = value.casefold()
        if "до" in lowered and "от" not in lowered:
            minimum, maximum = None, numbers[0]
        elif "от" in lowered and len(numbers) == 1:
            minimum, maximum = numbers[0], None
        else:
            minimum, maximum = numbers[0], (numbers[1] if len(numbers) > 1 else numbers[0])
        if maximum is not None and minimum is not None and minimum > maximum:
            return None
        return Salary(minimum=minimum, maximum=maximum, currency=currency)

    async def extract_job(self, page) -> JobPosting:
        title = await self._text(page, locators.VACANCY_TITLE, "название")
        company = await self._text(page, locators.VACANCY_COMPANY, "компанию", False)
        try:
            description = await self._text(
                page, locators.VACANCY_DESCRIPTION, "описание", False
            )
            if not description:
                description = await self._main_vacancy_description(page)
            if not description:
                description = await self._text(page, "main", "описание")
            if not description or not description.strip():
                raise ValueError(JobDescriptionUnavailable.DEFAULT_MESSAGE)
        except CaptchaRequired:
            raise
        except Exception as exc:
            raise JobDescriptionUnavailable(title=title, company=company) from exc
        salary_values = await self._sidebar_labeled_values(page, {"зарплата", "salary"})
        if not salary_values:
            salary_values = await self._sidebar_values(page, locators.VACANCY_SALARY, exclude_market=True)
        sidebar_location = await self._sidebar_labeled_values(page, {"город", "страна", "локация", "location"})
        if not sidebar_location:
            sidebar_location = await self._sidebar_values(page, locators.VACANCY_LOCATION)
        sidebar_format = await self._sidebar_labeled_values(page, {"формат", "формат работы", "work format"})
        if not sidebar_format:
            sidebar_format = await self._sidebar_values(page, locators.VACANCY_FORMAT)
        sidebar_grade = await self._sidebar_labeled_values(page, {"грейд", "уровень", "grade"})
        if not sidebar_grade:
            sidebar_grade = await self._sidebar_values(page, locators.VACANCY_GRADE)
        work_format, format_locations = self._split_sidebar_format(sidebar_format)
        if format_locations:
            sidebar_location = sidebar_location + [
                value for value in format_locations if value not in sidebar_location
            ]
        sidebar_location = list(dict.fromkeys(value for value in sidebar_location if value))
        sidebar_skills = await self._vacancy_skills(page)
        return JobPosting(
            source=self.site_id,
            external_id=urlparse(page.url).path.rstrip("/").split("-")[-1],
            url=page.url,
            title=title,
            company=company or None,
            description=description,
            salary=self._parse_hirehi_salary(salary_values[0] if salary_values else None),
            location=", ".join(sidebar_location) if sidebar_location else None,
            work_format=work_format,
            grade=sidebar_grade[0] if sidebar_grade else None,
            required_skills=sidebar_skills,
            requires_cover_letter=True,
        )

    async def _main_vacancy_description(self, page) -> str:
        """Extract the vacancy body from current main, excluding surrounding feed blocks."""
        main = await self._first_visible(page.locator("main"))
        if main is None:
            return ""
        text = (await main.inner_text()).strip()
        if not text:
            return ""
        start_markers = re.compile(r"^(описание|задачи|требования|условия|навыки)\s*:?$", re.I)
        stop_markers = re.compile(
            r"^(похожие вакансии|статьи|про\s+зарплаты|зарплат(?:а|ный блок)?|предупреждение|инструменты|ai tools?)\s*:?.*$",
            re.I,
        )
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        start = next((i for i, line in enumerate(lines) if start_markers.match(line)), None)
        if start is None:
            return ""
        end = next((i for i in range(start + 1, len(lines)) if stop_markers.match(lines[i])), len(lines))
        return "\n".join(lines[start:end]).strip()

    async def classify_application_route(self, page) -> ApplicationRoute:
        text = (await page.locator("body").inner_text()).lower()
        direct_pattern = re.compile(r"отклик\s+(?:по|в)\s+(email|telegram|linkedin)", re.I)
        direct_link = page.get_by_role("link", name=direct_pattern).first
        if not await direct_link.count():
            direct_link = page.get_by_text(direct_pattern).first
        if await direct_link.count():
            label = (await direct_link.inner_text()).lower()
            if "осталось: 0" in label:
                return ApplicationRoute(
                    kind="direct_contact", contact=EmployerContact(exhausted=True)
                )
            contact, href = await self._reveal_direct_contact(page, direct_link)
            return ApplicationRoute(kind="direct_contact", contact=contact)
        contact = await self.collect_employer_contact(page)
        if contact.email or contact.telegram or contact.linkedin:
            return ApplicationRoute(kind="direct_contact", contact=contact)
        if "прямой контакт" in text:
            generic = page.get_by_role("link", name=re.compile(r"^отклик(?:нуться)?$", re.I)).first
            if not await generic.count():
                generic = page.get_by_role("button", name=re.compile(r"^отклик(?:нуться)?$", re.I)).first
            if await generic.count() and await generic.is_visible():
                contact, href = await self._reveal_direct_contact(page, generic)
                return ApplicationRoute(kind="direct_contact", contact=contact, target_url=href or None)
            return ApplicationRoute(kind="direct_contact", contact=contact)
        external = page.get_by_role(
            "link", name=re.compile(r"отклик(нуться)?|перейти к отклику", re.I)
        ).first
        if await external.count():
            href = await external.get_attribute("href")
            if href and urlparse(urljoin(page.url, href)).hostname not in self.allowed_domains:
                return ApplicationRoute(
                    kind="external_employer", target_url=urljoin(page.url, href)
                )
        return ApplicationRoute(kind="hirehi_chat", target_url=page.url)

    async def _reveal_direct_contact(self, page, control):
        before_url = page.url
        before_pages = list(getattr(getattr(page, "context", None), "pages", []))
        await control.click()
        destination = None
        href = ""
        for _ in range(10):
            pages = list(getattr(getattr(page, "context", None), "pages", []))
            destination = next((item for item in pages if item not in before_pages), None)
            href = (
                getattr(destination, "url", "")
                if destination
                else (page.url if page.url != before_url else "")
            )
            if href and href != "about:blank":
                break
            await page.wait_for_timeout(500)
        if href:
            contact = self._contact_from_text(href)
            if contact.email or contact.telegram or contact.linkedin:
                return contact, href
        for _ in range(10):
            contact = await self.collect_employer_contact(page)
            if contact.email or contact.telegram or contact.linkedin or contact.exhausted:
                return contact, href
            await page.wait_for_timeout(500)
        return contact, href

    async def collect_application_route(self, page) -> ApplicationRoute:
        """Discover an external apply destination without filling or submitting."""
        route = await self.classify_application_route(page)
        if route.kind != "hirehi_chat":
            return route
        destination = await self._discover_apply_destination(page)
        if destination and urlparse(destination).hostname not in self.allowed_domains:
            return ApplicationRoute(kind="external_employer", target_url=destination)
        return route

    async def _discover_apply_destination(self, page) -> str | None:
        apply = page.get_by_role(
            "link", name=re.compile(r"^отклик$|откликнуться", re.I)
        ).first
        if not await apply.count() or not await apply.is_visible():
            return None
        before_url = page.url
        context = getattr(page, "context", None)
        before_pages = list(getattr(context, "pages", [])) if context else []
        await apply.click()
        for _ in range(10):
            pages = list(getattr(context, "pages", [])) if context else []
            external = next(
                (candidate for candidate in pages if candidate not in before_pages
                 and getattr(candidate, "url", "") not in {"", "about:blank"}),
                None,
            )
            if external:
                return external.url
            if page.url != before_url:
                return page.url
            await page.wait_for_timeout(300)
        return None

    async def collect_employer_contact(self, page) -> EmployerContact:
        region = page.locator(
            "[role='dialog'], [data-testid='contact'], [class*='contact'], [class*='response']"
        )
        text = ""
        for index in range(await region.count()):
            candidate = region.nth(index) if hasattr(region, "nth") else region.first
            if await candidate.is_visible():
                text += " " + (await candidate.inner_text())
                if not hasattr(candidate, "locator"):
                    continue
                anchors = candidate.locator("a[href]")
                for anchor_index in range(await anchors.count()):
                    href = await anchors.nth(anchor_index).get_attribute("href")
                    if href and not any(x in href.lower() for x in ("hirehi.ru", "help", "support")):
                        text += " " + href
        if not text:
            text = await page.locator("body").inner_text()
        return self._contact_from_text(text)

    @staticmethod
    def _contact_from_text(text: str) -> EmployerContact:
        low = text.lower()
        telegram_candidates = re.finditer(
            r"(?<![\w.+-])(?:https?://)?t\.me/[\w-]+"
            r"|(?<![\w.+-])@[A-Za-z][\w_]{3,}",
            text,
        )
        support_markers = ("t.me/generalsupport", "t.me/jun_hi", "@generalsupport")
        telegram = next(
            (
                match[0]
                for match in telegram_candidates
                if not any(marker in match[0].lower() for marker in support_markers)
            ),
            None,
        )
        linkedin_match = re.search(r"https?://(?:www\.)?linkedin\.com/in/[\w-]+", text, re.I)
        return EmployerContact(
            email=(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text) or [None])[0],
            telegram=telegram,
            linkedin=linkedin_match[0] if linkedin_match else None,
            exhausted=any(
                x in low
                for x in (
                    "лимит контактов",
                    "контакты закончились",
                    "только pro",
                    "достигнут лимит",
                    "осталось: 0",
                )
            ),
        )

    async def detect_blockers(self, page) -> list[Blocker]:
        text = (await page.locator("body").inner_text()).lower()
        blockers = []
        # Vacancy descriptions may legitimately mention CAPTCHA/Cloudflare or
        # a JavaScript challenge.  Only a visible, structural widget is proof
        # that HireHi is currently blocking the browser session.
        if await self._has_visible_locator(page, locators.CAPTCHA_CHALLENGE_MARKERS):
            blockers.append(Blocker(kind="captcha", message="HireHi требует CAPTCHA"))
        if any(
            marker in text
            for marker in (
                "войдите, чтобы откликнуться",
                "авторизуйтесь, чтобы откликнуться",
                "нужно войти, чтобы откликнуться",
            )
        ):
            blockers.append(Blocker(kind="blocked", message="Требуется вход в HireHi"))
        if any(x in text for x in ("доступ ограничен", "заблокирован", "слишком много запросов")):
            blockers.append(Blocker(kind="blocked", message="HireHi ограничил доступ"))
        return blockers
