import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from playwright.async_api import Error as PlaywrightError

from backend.adapters.base.protocol import (
    AdapterManifest,
    ApplicationForm,
    Blocker,
    FillResult,
    JobRef,
    LoginState,
    SubmissionResult,
)
from backend.schemas.domain import ApplicationPlan, JobPosting

from . import discovery, forms, locators
from .salary import parse_salary


class HHAdapter:
    home_url = "https://hh.ru/"
    site_id = "hh"
    display_name = "HH.ru"
    # HH redirects authenticated users to their regional subdomain and emits
    # vacancy links on that same host.
    allowed_domains = (
        "hh.ru",
        "www.hh.ru",
        "krasnoyarsk.hh.ru",
    )
    manifest = AdapterManifest(
        site_id=site_id,
        display_name=display_name,
        allowed_domains=allowed_domains,
        supports_submission=True,
    )
    async def start(self, context, settings: dict) -> None:
        return None

    def query_source(self, query, field="name", cluster=""):
        return discovery.query_spec(self, query, field, cluster)

    def validate_search_source(self, spec):
        discovery.validate_url(self, spec["url"])

    async def read_discovery_page(self, page, spec, page_number):
        # The orchestrator maintains an independent signature for every source.
        self._last_search_page_signature = None
        self._repeated_search_pages = 0
        return await discovery.read_page(self, page, spec, page_number)

    async def collect_visible_sources(self, page, context="listing"):
        return await discovery.visible_sources(self, page, context=context)

    async def collect_related_refs(self, page):
        return await discovery.related_refs(self, page)

    async def discovery_listing_terminal(self, page):
        pager = page.locator(locators.SEARCH_PAGER).first
        next_page = page.locator(locators.SEARCH_NEXT).first
        if await pager.count() and await pager.is_visible():
            return not await next_page.count() or not await next_page.is_visible()
        return False

    @property
    def search_exhausted(self) -> bool:
        """Whether the cursor has confirmed the end of the fallback listing."""
        return getattr(self, "_search_exhausted", False)

    @search_exhausted.setter
    def search_exhausted(self, value: bool) -> None:
        self._search_exhausted = value

    def search_checkpoint(self) -> dict:
        """Export only the listing cursor, never browser/account state."""
        names = (
            "_search_page_number", "_search_query_index", "_search_exhausted",
            "_repeated_search_pages", "_search_navigation_count",
            "current_result_page", "current_search_query",
        )
        return {
            **{name: getattr(self, name, None) for name in names},
            "_fallback_search_urls": list(getattr(self, "_fallback_search_urls", [])),
            "_search_queries": list(getattr(self, "_search_queries", [])),
            "_search_seen_ids": sorted(getattr(self, "_search_seen_ids", set())),
            "_last_search_page_signature": getattr(self, "_last_search_page_signature", None),
        }

    def restore_search_checkpoint(self, checkpoint: dict) -> None:
        urls = checkpoint.get("_fallback_search_urls")
        if urls is not None:
            if any(urlparse(url).hostname not in self.allowed_domains or urlparse(url).path != "/search/vacancy" for url in urls):
                raise ValueError("Сохранённый поиск содержит недопустимый адрес")
            self._fallback_search_urls = list(urls)
            self._search_queries = list(checkpoint.get("_search_queries", []))
        for name in (
            "_search_page_number", "_search_query_index", "_search_exhausted",
            "_repeated_search_pages", "_search_navigation_count",
            "current_result_page", "current_search_query",
        ):
            if checkpoint.get(name) is not None:
                setattr(self, name, checkpoint[name])
        self._search_seen_ids = set(checkpoint.get("_search_seen_ids", []))
        signature = checkpoint.get("_last_search_page_signature")
        self._last_search_page_signature = tuple(signature) if signature else None

    @staticmethod
    def normalize_search_query(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).replace("_", " ")
        normalized = re.sub(r"[^\w\s+\-]", " ", normalized, flags=re.UNICODE)
        return re.sub(r"\s+", " ", normalized).strip()[:120]

    @staticmethod
    def has_test_assignment(description: str) -> bool:
        text = unicodedata.normalize("NFKC", description).lower()
        mandatory_patterns = (
            r"(?:обязательн\w*|необходимо|нужно|предстоит)\s*.{0,50}тестов\w*\s+задан",
            r"готовност\w*\s*.{0,30}(?:выполнить|пройти)\s+тестов",
            r"(?:выполнить|пройти)\s+обязательн\w*\s+тестов",
            r"тестов\w*\s+(?:задание|испытание)\s*.{0,30}обязательн",
            r"задание\s+после\s+отклика\s+обязательн",
        )
        return any(re.search(pattern, text) for pattern in mandatory_patterns)

    async def get_login_state(self, page) -> LoginState:
        # HH exposes stable UI hooks through ``data-qa`` rather than
        # Playwright's default ``data-testid`` attribute. Using
        # get_by_test_id() here therefore reported authenticated users as
        # logged out.
        authenticated_markers = page.locator(
            "[data-qa='mainmenu_applicantProfile'], "
            "[data-qa='mainmenu_myResumes'], "
            "a[href*='/applicant/resumes']"
        )
        logged_in = await authenticated_markers.count() > 0
        return LoginState(
            authenticated=logged_in,
            message="Вход выполнен" if logged_in else "Войдите вручную в открытом Chromium",
        )

    async def open_search(self, page, filters: dict) -> None:
        raw_queries = filters.get("queries", [])
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        queries = []
        query_keys = set()
        for value in raw_queries:
            query = self.normalize_search_query(str(value))
            query_key = query.casefold()
            if query and query_key not in query_keys:
                queries.append(query)
                query_keys.add(query_key)
        self._search_queries = queries
        self._fallback_search_urls = []
        for query in queries:
            params = {
                "text": query,
                "search_field": "name",
                "only_with_salary": str(
                    not filters.get("include_unspecified_salary", True)
                ).lower(),
            }
            if filters.get("salary_min"):
                params["salary"] = filters["salary_min"]
            self._fallback_search_urls.append(
                f"https://hh.ru/search/vacancy?{urlencode(params)}"
            )
        self._fallback_search_url = (
            self._fallback_search_urls[0] if self._fallback_search_urls else None
        )
        self._search_query_index = 0
        self.current_search_query: str | None = None
        self._search_page_number = 0
        self._search_seen_ids: set[str] = set()
        self._search_exhausted = False
        self._last_search_page_signature: tuple[str, ...] | None = None
        self._repeated_search_pages = 0
        self.current_result_page: int | None = None
        self._search_navigation_count = 0

        # The authenticated home page contains HH's personalized "Для вас"
        # recommendations. Prefer that ranking over a brittle text query.
        await page.goto(
            "https://hh.ru/",
            wait_until="commit",
            timeout=60_000,
        )
        await page.wait_for_timeout(1_500)
        for label in ("Для вас", "Вакансии для вас", "Подходящие вакансии"):
            candidate = page.get_by_text(label, exact=True).first
            if await candidate.count() and await candidate.is_visible():
                await candidate.click()
                await page.wait_for_timeout(1_500)
                break
        more_recommendations = (
            page.locator("a, button").filter(has_text="Посмотреть").filter(has_text="ваканс").first
        )
        if await more_recommendations.count() and await more_recommendations.is_visible():
            await more_recommendations.click()
            await page.wait_for_timeout(1_500)
        # Traverse the actual recommendation listing as well as planned queries.
        # A first-page snapshot alone is not evidence that recommendations ended.
        reached = urlparse(page.url)
        if reached.hostname in self.allowed_domains and reached.path == "/search/vacancy":
            self._fallback_search_urls.insert(0, self._search_page_url(page.url, 0))
            self._search_queries.insert(0, "Рекомендации")
        if not self._fallback_search_urls:
            self._fallback_search_urls = ["https://hh.ru/search/vacancy"]
            self._search_queries = [""]

    async def collect_job_refs(self, page) -> list[JobRef]:
        # Personalized recommendations are a larger initial snapshot than a
        # single text-search page. Keep the latter capped by the helper's
        # default limit of 100.
        recommended_refs = await self._visible_job_refs(page, timeout=6_000, limit=200)
        self.last_discovery_batch = {"source": "recommendations", "ids": [r.external_id for r in recommended_refs]}
        self._search_page_number = 0
        self._search_seen_ids = {ref.external_id for ref in recommended_refs}
        self._search_exhausted = False
        self._last_search_page_signature = None
        self._repeated_search_pages = 0
        self.current_result_page = None
        self._search_query_index = 0
        self.current_search_query = None
        return recommended_refs

    async def collect_more_job_refs(self, page) -> list[JobRef]:
        """Fetch the next search-result batch after recommendations are drained.

        HH's personalized page and the text-search result pages are separate
        listings. Keeping a cursor here prevents the workflow from treating a
        duplicate-heavy personalized snapshot as the end of an unlimited
        session. Only confirmed empty pages advance to the next query;
        stale listings raise an error for the workflow to recover.
        """
        if self._search_exhausted:
            self.last_discovery_batch = {"source": "exhausted", "ids": []}
            return []
        search_urls = getattr(self, "_fallback_search_urls", [])
        if not search_urls:
            self._search_exhausted = True
            return []

        if self._search_query_index >= len(search_urls):
            self._search_exhausted = True
            return []
        fallback_url = search_urls[self._search_query_index]
        self.current_search_query = self._search_queries[self._search_query_index]
        page_number = self._search_page_number
        page_refs = await self._collect_search_page(page, fallback_url, page_number)
        self.last_discovery_batch = {"source": self.current_search_query or "broad", "page": page_number,
                                     "ids": [r.external_id for r in page_refs]}
        # Advance only after a successfully read page. Return one page per call
        # so the workflow can persist progress even through duplicate-only pages.
        self._search_page_number += 1
        self._search_navigation_count += 1
        if self._repeated_search_pages >= 3 and page_refs:
            raise RuntimeError("Выдача повторяет страницу; требуется повторная загрузка")
        new_refs = [ref for ref in page_refs if ref.external_id not in self._search_seen_ids]
        self._search_seen_ids.update(ref.external_id for ref in new_refs)
        if not page_refs or getattr(self, "_last_page_terminal", False):
            self._search_query_index += 1
            self._search_page_number = 0
            self._last_search_page_signature = None
            self._repeated_search_pages = 0
            self._search_exhausted = self._search_query_index >= len(search_urls)
        return new_refs

    async def _collect_search_page(self, page, fallback_url: str, page_number: int) -> list[JobRef]:
        """Navigate to a known result page and reject stale DOM snapshots."""
        url = self._search_page_url(fallback_url, page_number)
        page_refs: list[JobRef] = []
        for attempt in range(3):
            await page.goto(url, wait_until="commit", timeout=60_000)
            await page.wait_for_timeout(1_500 + attempt * 500)
            page_refs = await self._visible_job_refs(page, timeout=8_000)
            if page_refs:
                break
        if not page_refs:
            empty = page.locator(locators.SEARCH_EMPTY).first
            if not await empty.count() or not await empty.is_visible():
                raise RuntimeError("Выдача не загрузилась; отсутствие вакансий не подтверждено")
        signature = tuple(ref.external_id for ref in page_refs)
        if page_number and signature and signature == self._last_search_page_signature:
            # A commit navigation can leave the previous result list attached
            # briefly. Retry once before recording a no-progress page.
            await page.goto(url, wait_until="commit", timeout=60_000)
            await page.wait_for_timeout(1_500)
            page_refs = await self._visible_job_refs(page, timeout=8_000)
            signature = tuple(ref.external_id for ref in page_refs)
            self._repeated_search_pages += int(
                bool(signature and signature == self._last_search_page_signature)
            )
        elif page_refs:
            self._repeated_search_pages = 0
        if not page_refs:
            self._repeated_search_pages += 1
        pager = page.locator(locators.SEARCH_PAGER).first
        next_page = page.locator(locators.SEARCH_NEXT).first
        self._last_page_terminal = bool(
            await pager.count() and await pager.is_visible()
            and (not await next_page.count() or not await next_page.is_visible())
        )
        if self._last_page_terminal:
            self._repeated_search_pages = 0
        self.current_result_page = page_number
        self._last_search_page_signature = signature or None
        return page_refs

    @staticmethod
    def _search_page_url(base_url: str, page_number: int) -> str:
        parsed = urlparse(base_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page_number)
        return urlunparse(parsed._replace(query=urlencode(query)))

    async def _visible_job_refs(self, page, timeout: int, limit: int = 100) -> list[JobRef]:
        links = page.locator(locators.VACANCY_LINK)
        try:
            await links.first.wait_for(state="attached", timeout=timeout)
        except Exception:
            return []
        refs: list[JobRef] = []
        for i in range(min(await links.count(), limit)):
            link = links.nth(i)
            is_visible = getattr(link, "is_visible", None)
            if is_visible is not None and not await is_visible():
                continue
            href = await link.get_attribute("href")
            url = urljoin(page.url, href) if href else None
            parsed = urlparse(url) if url else None
            path = unquote(parsed.path) if parsed else ""
            vacancy_match = re.fullmatch(r"/vacancy/(\d+)/?", path)
            if parsed and parsed.hostname in self.allowed_domains and vacancy_match:
                external_id = vacancy_match.group(1)
                if all(ref.external_id != external_id for ref in refs):
                    refs.append(JobRef(external_id=external_id, url=url))
        return refs

    async def open_job(self, page, ref: JobRef) -> None:
        if urlparse(ref.url).hostname not in self.allowed_domains:
            raise ValueError("Переход за пределы разрешённых доменов остановлен")
        await page.goto(ref.url, wait_until="commit", timeout=60_000)
        await page.wait_for_timeout(1_000)

    async def extract_job(self, page) -> JobPosting:
        async def required_text(selector: str, label: str) -> str:
            locator = page.locator(selector)
            for index in range(await locator.count()):
                candidate = locator.nth(index) if hasattr(locator, "nth") else locator.first
                is_visible = getattr(candidate, "is_visible", None)
                if is_visible is not None and not await is_visible():
                    continue
                try:
                    value = (await candidate.inner_text(timeout=8_000)).strip()
                except Exception:
                    continue
                if value:
                    return value
            raise ValueError(f"Не удалось извлечь {label} вакансии")

        title = await required_text(locators.VACANCY_TITLE, "название")
        company = await required_text(locators.COMPANY, "компанию")
        description = await required_text(locators.DESCRIPTION, "описание")
        async def optional_text(selector: str, *, require_readable: bool = False) -> str | None:
            locator = page.locator(selector)
            visible_found = False
            for index in range(await locator.count()):
                candidate = locator.nth(index) if hasattr(locator, "nth") else locator.first
                if not await candidate.is_visible():
                    continue
                visible_found = True
                try:
                    value = " ".join((await candidate.inner_text(timeout=3_000)).split())
                except Exception:
                    continue
                if value:
                    return value
            if require_readable and visible_found:
                raise ValueError("Не удалось прочитать видимый блок зарплаты вакансии")
            return None

        salary_text = await optional_text(locators.SALARY, require_readable=True)
        if salary_text:
            # Keep exact compensation terms available to the model, grounding
            # checks and persisted snapshots, even if normalization is impossible.
            description = f"Зарплата: {salary_text}\n\n{description}"
        payment_frequency = await optional_text(locators.PAYMENT_FREQUENCY)
        required_experience = await optional_text(locators.WORK_EXPERIENCE)
        employment_type = await optional_text(locators.EMPLOYMENT)
        hiring_format = await optional_text(locators.HIRING_FORMAT)
        work_schedule = await optional_text(locators.WORK_SCHEDULE)
        working_hours = await optional_text(locators.WORKING_HOURS)
        work_format = await optional_text(locators.WORK_FORMAT)
        location = await optional_text(locators.LOCATION)
        external_id = page.url.rstrip("/").split("/")[-1].split("?")[0]
        return JobPosting(
            source="hh",
            external_id=external_id,
            url=page.url,
            title=title,
            company=company,
            description=description,
            salary=parse_salary(salary_text),
            has_test_assignment=self.has_test_assignment(description),
            payment_frequency=payment_frequency,
            required_experience=required_experience,
            employment_type=employment_type,
            hiring_format=hiring_format,
            work_schedule=work_schedule,
            working_hours=working_hours,
            work_format=work_format,
            location=location,
        )

    async def open_application(self, page) -> ApplicationForm:
        # Keep the result classification tied to this attempt.  HH's
        # one-click response closes the form immediately and then exposes the
        # same topic link used for responses that existed before this attempt.
        self._application_attempt_clicked = False
        response = page.locator(locators.RESPONSE_BUTTON).first
        if not await response.count():
            return ApplicationForm()
        await response.click()
        self._application_attempt_clicked = True
        await page.wait_for_timeout(800)
        return await self.read_application(page)

    async def read_application(self, page) -> ApplicationForm:
        self._validate_application_domain(page)
        for attempt in range(2):
            try:
                return await forms.read_form(page)
            except PlaywrightError as exc:
                if attempt or "Execution context was destroyed" not in str(exc):
                    raise
                await page.wait_for_timeout(750)
        return ApplicationForm()

    def _validate_application_domain(self, page) -> None:
        if (urlparse(page.url).hostname or "") not in self.allowed_domains:
            raise ValueError("Форма отклика находится вне разрешённых доменов HH.ru")

    async def prepare_application(self, page, plan: ApplicationPlan) -> ApplicationForm:
        form = await self.read_application(page)
        if form.confirmation == "foreign_country":
            if not plan.submission_allowed or not plan.allow_foreign_application:
                return form.model_copy(update={"questions": ["Подтвердите отклик в другой стране"]})
            # This notice acknowledges the vacancy's country; it does not assert relocation.
            button = page.get_by_role("button", name=locators.FOREIGN_CONTINUE, exact=True)
            if await button.count() == 1 and await button.is_visible():
                await button.click()
                await page.wait_for_timeout(800)
                return await self.read_application(page)
        return form

    async def fill_application(self, page, plan: ApplicationPlan) -> FillResult:
        self._validate_application_domain(page)
        if not plan.submission_allowed:
            return FillResult(success=False, unknown_questions=["Отправка не разрешена планом"])
        letter_input = page.locator(locators.COVER_LETTER_INPUT)
        if plan.cover_letter and not await letter_input.count():
            toggle = page.locator(locators.COVER_LETTER_TOGGLE)
            if await toggle.count():
                await toggle.click()
                await page.wait_for_timeout(300)
        if plan.cover_letter and await letter_input.count():
            await letter_input.fill(plan.cover_letter)
        return await forms.fill_fields(page, plan)

    async def can_retry_application(self, page) -> bool:
        """The loaded vacancy explicitly offers a new application, with no prior response."""
        if not (await self.get_login_state(page)).authenticated:
            return False
        if await page.locator(locators.ALREADY_APPLIED).count():
            return False
        response = page.locator(locators.RESPONSE_BUTTON).first
        return bool(await response.count() and await response.is_visible())

    async def submit_application(self, page) -> SubmissionResult:
        self._validate_application_domain(page)
        submit = page.locator(locators.RESPONSE_SUBMIT).first
        # HH can render the topic link while the response popup is still
        # active. The active submit control is authoritative in that state.
        if await submit.count() and await submit.is_visible():
            await submit.click()
            return await self.verify_submission(page, just_submitted=True)

        # With no active form, the topic link means the response existed
        # before this attempt.
        if await page.locator(locators.ALREADY_APPLIED).count():
            if getattr(self, "_application_attempt_clicked", False):
                return await self.verify_submission(page, just_submitted=True)
            return SubmissionResult(
                status="already_applied",
                message="hh.ru показывает ранее отправленный отклик",
            )
        return await self.verify_submission(page)

    async def verify_submission(self, page, just_submitted: bool = False) -> SubmissionResult:
        # HH updates the response form asynchronously. A fixed 1.2 second
        # delay was too short in session 9 and classified six response flows
        # as errors. Poll the visible state without a second click: repeating
        # the submit action would risk a duplicate application.
        for attempt in range(10):
            self._validate_application_domain(page)
            notice = page.get_by_text(locators.FOREIGN_NOTICE, exact=False)
            if await notice.count() and await notice.first.is_visible():
                return SubmissionResult(status="needs_input", message="HH.ru запросил подтверждение страны")
            submit = page.locator(locators.RESPONSE_SUBMIT).first
            active_form = bool(await submit.count() and await submit.is_visible())
            if active_form:
                if attempt < 9:
                    await page.wait_for_timeout(500)
                    continue
                return SubmissionResult(status="needs_input", message="HH.ru ожидает заполнения формы")
            if await page.locator(locators.ALREADY_APPLIED).count():
                if just_submitted:
                    return SubmissionResult(
                        status="submitted", message="hh.ru подтвердил отправку отклика"
                    )
                return SubmissionResult(
                    status="already_applied",
                    message="hh.ru показывает ранее отправленный отклик",
                )
            if await page.locator(locators.SUBMISSION_CONFIRMED).count():
                return SubmissionResult(
                    status="submitted", message="hh.ru подтвердил отправку отклика"
                )
            text = (await page.locator("body").inner_text()).lower()
            if any(marker in text for marker in locators.SUBMISSION_TEXT_MARKERS):
                return SubmissionResult(
                    status="submitted", message="hh.ru подтвердил отправку отклика"
                )
            if attempt < 9:
                await page.wait_for_timeout(500)
        form = await self.read_application(page)
        if form.fields or form.confirmation:
            return SubmissionResult(status="needs_input", message="HH.ru запросил дополнительную информацию")
        return SubmissionResult(
            status="unknown", message="hh.ru не показал однозначное подтверждение отправки"
        )

    async def detect_blockers(self, page) -> list[Blocker]:
        text = (await page.locator("body").inner_text()).lower()
        return (
            [Blocker(kind="captcha", message="Обнаружена CAPTCHA; требуется пользователь")]
            if any(marker.lower() in text for marker in locators.CAPTCHA_MARKERS)
            else []
        )
