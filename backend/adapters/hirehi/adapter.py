from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from backend.adapters.base.protocol import (
    AdapterManifest,
    ApplicationRoute,
    Blocker,
    EmployerContact,
    JobRef,
    LoginState,
)
from backend.adapters.hirehi import locators
from backend.schemas.domain import JobPosting


class HireHiAdapter:
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
    site_id = "hirehi"
    home_url = "https://hirehi.ru/"
    display_name = "HireHi"
    allowed_domains = ("hirehi.ru", "www.hirehi.ru")
    manifest = AdapterManifest(
        site_id=site_id,
        display_name=display_name,
        allowed_domains=allowed_domains,
        supports_submission=True,
    )

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

    async def get_login_state(self, page) -> LoginState:
        goto = getattr(page, "goto", None)
        if goto is None:
            return LoginState(authenticated=False, message="Войдите в HireHi вручную")
        try:
            await goto("https://hirehi.ru/profile", wait_until="commit", timeout=15_000)
            wait_for_timeout = getattr(page, "wait_for_timeout", None)
            if wait_for_timeout is not None:
                await wait_for_timeout(500)
            parsed = urlparse(page.url)
            protected_route = parsed.hostname in self.allowed_domains and (
                parsed.path == "/profile" or parsed.path.startswith("/profile/")
            )
            login_fields = page.locator(
                "input[type='password'], form[action*='login'], [data-testid='login-form']"
            )
            login_visible = False
            for index in range(await login_fields.count()):
                field = (
                    login_fields.nth(index)
                    if hasattr(login_fields, "nth")
                    else login_fields.first
                )
                if await field.is_visible():
                    login_visible = True
                    break
            profile_markers = page.locator(
                "main h1, main h2, [data-testid='profile-name'], [data-testid='profile-page']"
            )
            profile_visible = False
            for index in range(await profile_markers.count()):
                marker = (
                    profile_markers.nth(index)
                    if hasattr(profile_markers, "nth")
                    else profile_markers.first
                )
                if await marker.is_visible():
                    profile_visible = True
                    break
            authenticated = protected_route and profile_visible and not login_visible
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
        loc = page.locator(selector)
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

    async def extract_job(self, page) -> JobPosting:
        title = await self._text(page, "h1", "название")
        company = await self._text(
            page, "a[href*='/companies/'], [class*='company']", "компанию", False
        )
        description = await self._text(
            page,
            "[data-testid='vacancy-description'], [class*='vacancy-description'], article",
            "описание",
            False,
        )
        if not description:
            description = await self._main_vacancy_description(page)
        if not description:
            description = await self._text(page, "main", "описание")
        return JobPosting(
            source=self.site_id,
            external_id=urlparse(page.url).path.rstrip("/").split("-")[-1],
            url=page.url,
            title=title,
            company=company or None,
            description=description,
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
        telegram_match = re.search(r"(?:https?://)?t\.me/[\w-]+|@[A-Za-z][\w_]{3,}", text)
        telegram = telegram_match[0] if telegram_match else None
        if telegram and any(
            marker in telegram.lower()
            for marker in ("t.me/generalsupport", "t.me/jun_hi", "@generalsupport")
        ):
            telegram = None
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
        if any(x in text for x in ("captcha", "я не робот", "подтвердите, что вы не робот")):
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
