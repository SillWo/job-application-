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

from . import locators


class ZarplataAdapter:
    site_id = "zarplata"
    display_name = "Zarplata.ru"
    # HH redirects authenticated users to their regional subdomain and emits
    # vacancy links on that same host.
    allowed_domains = (
        "zarplata.ru",
        "www.zarplata.ru",
        "krasnoyarsk.zarplata.ru",
        "ekb.zarplata.ru",
        "krs.zarplata.ru",
        "nsk.zarplata.ru",
        "omsk.zarplata.ru",
        "chelyabinsk.zarplata.ru",
    )
    manifest = AdapterManifest(
        site_id=site_id,
        display_name=display_name,
        allowed_domains=allowed_domains,
        supports_submission=True,
    )
    async def start(self, context, settings: dict) -> None:
        return None

    @property
    def search_exhausted(self) -> bool:
        """Whether the cursor has confirmed the end of the fallback listing."""
        return getattr(self, "_search_exhausted", False)

    @search_exhausted.setter
    def search_exhausted(self, value: bool) -> None:
        self._search_exhausted = value

    @staticmethod
    def normalize_search_query(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).replace("_", " ")
        normalized = re.sub(r"[^\w\s+\-]", " ", normalized, flags=re.UNICODE)
        return re.sub(r"\s+", " ", normalized).strip()[:120]

    # Backwards-compatible names used by older integrations.
    _query = normalize_search_query

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
        """Classify the visible Zarplata account controls conservatively."""

        async def visible(locator) -> bool:
            try:
                is_visible = getattr(locator, "is_visible", None)
                return bool(is_visible and await is_visible())
            except Exception:
                return False

        async def count(locator) -> int:
            try:
                return int(await locator.count())
            except Exception:
                return 0

        # These legacy hooks are unambiguous when visible. Keep each selector
        # separate: test doubles and the live DOM need not support comma lists.
        for selector in (
            "[data-qa='mainmenu_applicantProfile']",
            "[data-qa='mainmenu_myResumes']",
            "a[href*='/applicant/resumes']",
        ):
            try:
                marker = page.locator(selector).first
                if await count(marker) and await visible(marker):
                    logged_in = True
                    break
            except Exception:
                continue
        else:
            logged_in = False

        if logged_in:
            return LoginState(authenticated=True, message="Вход выполнен")

        # Profile and login controls are not authoritative: public Zarplata
        # pages can render both, and regional redirects can change the host.
        # Resolve the state through the same protected UI route the workflow
        # can safely revisit before opening search. Do not inspect cookies,
        # storage, or site APIs.
        try:
            await page.goto(
                "https://zarplata.ru/applicant/resumes",
                wait_until="commit",
                timeout=15_000,
            )
            await page.wait_for_timeout(500)
            reached = urlparse(str(getattr(page, "url", "")))
            hostname = (reached.hostname or "").lower().rstrip(".")
            path = unquote(reached.path or "/").rstrip("/") or "/"
            if hostname not in ZarplataAdapter.allowed_domains:
                authenticated = False
            elif any(
                path == prefix or path.startswith(f"{prefix}/")
                for prefix in ("/applicant/resumes", "/applicant/profile")
            ):
                authenticated = True
            elif (
                path == "/account/login"
                or path == "/account/signup"
                or path.startswith("/auth/")
            ):
                authenticated = False
            else:
                authenticated = False
        except Exception:
            authenticated = False
        return LoginState(
            authenticated=authenticated,
            message="Вход выполнен" if authenticated else "Войдите вручную в открытом Chromium",
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
                f"https://zarplata.ru/search/vacancy?{urlencode(params)}"
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
        self._query_accepted_count = 0

        # The authenticated home page contains Zarplata\x27s personalized "Для вас"
        # recommendations. Prefer that ranking over a brittle text query.
        await page.goto(
            "https://zarplata.ru/",
            wait_until="commit",
            timeout=15_000,
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
        # The recommendation link can lead to a regional host or a different
        # listing route.  Keep the URL actually reached; never manufacture a
        # pagination URL from the home page.
        self._recommendation_base_url = page.url

    async def collect_job_refs(self, page) -> list[JobRef]:
        # Recommendation cards are paginated in the public DOM (usually about
        # 20 per page). Traverse the reached listing, preserving page order
        # and globally deduplicating ids. A home snapshot is still useful, but
        # must not be followed by meaningless ``/?page=N`` navigations.
        base_url = getattr(self, "_recommendation_base_url", None) or page.url
        parsed_base = urlparse(base_url)
        listing_path = parsed_base.path.rstrip("/") in {
            "/search/vacancy",
            "/vacancies",
            "/recommendations",
        }
        recommended_refs: list[JobRef] = []
        seen: set[str] = set()
        previous_signature: tuple[str, ...] | None = None
        repeated_signature_count = 0
        max_pages = 25
        for page_number in range(max_pages):
            # Page 0 is the DOM already rendered after open_search.  For a
            # known listing route, explicitly request each page so test and
            # real navigation both observe the same deterministic sequence.
            if page_number > 0 or listing_path and page_number == 0:
                target = self._search_page_url(base_url, page_number)
                try:
                    await page.goto(target, wait_until="commit", timeout=15_000)
                    await page.wait_for_timeout(1_500)
                except Exception:
                    # A stale/failed navigation must not spin indefinitely;
                    # retain already collected refs and finish this phase.
                    break
            page_refs = await self._visible_job_refs(
                page, timeout=6_000, limit=100 if listing_path else 200
            )
            signature = tuple(ref.external_id for ref in page_refs)
            if not signature:
                break
            if signature == previous_signature:
                repeated_signature_count += 1
                if repeated_signature_count >= 3:
                    break
            else:
                repeated_signature_count = 0
            previous_signature = signature
            for ref in page_refs:
                if ref.external_id not in seen:
                    seen.add(ref.external_id)
                    recommended_refs.append(ref)
                    if len(recommended_refs) >= 200:
                        break
            if len(recommended_refs) >= 200:
                break
            # A non-pageable home result is a single snapshot by design.
            if not listing_path:
                break
        self._search_page_number = 0
        self._search_seen_ids = {ref.external_id for ref in recommended_refs}
        self._search_exhausted = False
        self._last_search_page_signature = None
        self._repeated_search_pages = 0
        self.current_result_page = None
        self._search_query_index = 0
        self.current_search_query = None
        self._query_accepted_count = 0
        return recommended_refs

    async def collect_more_job_refs(self, page) -> list[JobRef]:
        """Fetch the next search-result batch after recommendations are drained.

        Zarplata\x27s personalized page and the text-search result pages are separate
        listings. Keeping a cursor here prevents the workflow from treating a
        duplicate-heavy personalized snapshot as the end of an unlimited
        session. Empty/repeated pages are consumed internally, while the
        finite page bound protects against a broken/stale listing.
        """
        if self._search_exhausted:
            return []
        search_urls = getattr(self, "_fallback_search_urls", [])
        if not search_urls:
            self._search_exhausted = True
            return []

        while self._search_navigation_count < 100 and self._search_query_index < len(search_urls):
            fallback_url = search_urls[self._search_query_index]
            self.current_search_query = self._search_queries[self._search_query_index]
            page_number = self._search_page_number
            self._search_page_number += 1
            self._search_navigation_count += 1
            page_refs = await self._collect_search_page(page, fallback_url, page_number)
            if not page_refs:
                # _collect_search_page already retried this page three times;
                # only now is an empty listing considered confirmed exhaustion.
                self._search_query_index += 1
                self._search_page_number = 0
                self._last_search_page_signature = None
                self._repeated_search_pages = 0
                continue
            if self._repeated_search_pages >= 3:
                self._search_query_index += 1
                self._search_page_number = 0
                self._last_search_page_signature = None
                self._repeated_search_pages = 0
                continue

            new_refs = []
            for ref in page_refs:
                if ref.external_id not in self._search_seen_ids:
                    new_refs.append(ref)
            remaining = 100 - self._query_accepted_count
            if remaining <= 0:
                new_refs = []
            elif len(new_refs) > remaining:
                new_refs = new_refs[:remaining]
            # Only returned vacancies are globally processed.  Links beyond
            # this query's 100-item cap may legitimately appear in a later
            # adjacent query and must not be suppressed before the workflow
            # has seen them.
            self._search_seen_ids.update(ref.external_id for ref in new_refs)
            self._query_accepted_count += len(new_refs)
            if self._query_accepted_count >= 100:
                self._search_query_index += 1
                self._search_page_number = 0
                self._last_search_page_signature = None
                self._repeated_search_pages = 0
                self._query_accepted_count = 0
            if new_refs:
                return new_refs
        self._search_exhausted = True
        return []

    async def _collect_search_page(self, page, fallback_url: str, page_number: int) -> list[JobRef]:
        """Navigate to a known result page and reject stale DOM snapshots."""
        url = self._search_page_url(fallback_url, page_number)
        page_refs: list[JobRef] = []
        for attempt in range(3):
            await page.goto(url, wait_until="commit", timeout=15_000)
            await page.wait_for_timeout(1_500 + attempt * 500)
            page_refs = await self._visible_job_refs(page, timeout=8_000)
            if page_refs:
                break
        signature = tuple(ref.external_id for ref in page_refs)
        if page_number and signature and signature == self._last_search_page_signature:
            # A commit navigation can leave the previous result list attached
            # briefly. Retry once before recording a no-progress page.
            await page.goto(url, wait_until="commit", timeout=15_000)
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

    async def _refs(self, page, limit: int = 100) -> list[JobRef]:
        return await self._visible_job_refs(page, timeout=6_000, limit=limit)

    async def open_job(self, page, ref: JobRef) -> None:
        if urlparse(ref.url).hostname not in self.allowed_domains:
            raise ValueError("Переход за пределы разрешённых доменов остановлен")
        await page.goto(ref.url, wait_until="commit", timeout=15_000)
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
        async def optional_text(selector: str) -> str | None:
            locator = page.locator(selector).first
            if not await locator.count():
                return None
            try:
                value = " ".join((await locator.inner_text(timeout=3_000)).split())
            except Exception:
                return None
            return value or None

        payment_frequency = await optional_text(locators.PAYMENT_FREQUENCY)
        required_experience = await optional_text(locators.WORK_EXPERIENCE)
        employment_type = await optional_text(locators.EMPLOYMENT)
        hiring_format = await optional_text(locators.HIRING_FORMAT)
        work_schedule = await optional_text(locators.WORK_SCHEDULE)
        working_hours = await optional_text(locators.WORKING_HOURS)
        work_format = await optional_text(locators.WORK_FORMAT)
        external_id = page.url.rstrip("/").split("/")[-1].split("?")[0]
        return JobPosting(
            source="zarplata",
            external_id=external_id,
            url=page.url,
            title=title,
            company=company,
            description=description,
            has_test_assignment=self.has_test_assignment(description),
            payment_frequency=payment_frequency,
            required_experience=required_experience,
            employment_type=employment_type,
            hiring_format=hiring_format,
            work_schedule=work_schedule,
            working_hours=working_hours,
            work_format=work_format,
        )

    async def open_application(self, page) -> ApplicationForm:
        # Keep the result classification tied to this attempt.  Zarplata\x27s
        # one-click response closes the form immediately and then exposes the
        # same topic link used for responses that existed before this attempt.
        self._application_attempt_clicked = False
        response = page.locator(locators.RESPONSE_BUTTON).first
        if not await response.count():
            return ApplicationForm()
        await response.click()
        self._application_attempt_clicked = True
        await page.wait_for_timeout(800)
        questions = await self._application_questions(page)
        return ApplicationForm(
            requires_cover_letter=bool(
                await page.locator(locators.COVER_LETTER_TOGGLE).count()
                or await page.locator(locators.COVER_LETTER_INPUT).count()
            ),
            questions=questions,
        )

    async def fill_application(self, page, plan: ApplicationPlan) -> FillResult:
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
        unanswered = await self._application_questions(page)
        return FillResult(success=not unanswered, unknown_questions=unanswered)

    async def _application_questions(self, page) -> list[str]:
        """Return visible employer questions that the agent cannot answer safely.

        Zarplata\x27s standalone response page renders test questions as bare textareas,
        not as labels matching the popup selector. Detect controls first and
        attach Zarplata\x27s nearby task prompts by order; metadata is a conservative
        fallback for other form variants.
        """
        for attempt in range(2):
            try:
                task_prompts = [
                    text.strip()
                    for text in await page.locator(locators.TASK_QUESTION).all_text_contents()
                    if text.strip()
                ]
                label_prompts = [
                    text.strip()
                    for text in await page.locator(
                        locators.APPLICATION_QUESTION
                    ).all_text_contents()
                    if text.strip() and "сопровод" not in text.lower()
                ]
                return await self._questions_from_controls(
                    page, task_prompts, label_prompts
                )
            except PlaywrightError as exc:
                if attempt or "Execution context was destroyed" not in str(exc):
                    raise
                await page.wait_for_timeout(750)
        return []

    async def _questions_from_controls(
        self, page, task_prompts: list[str], label_prompts: list[str]
    ) -> list[str]:
        questions: list[str] = []
        task_index = 0
        controls = page.locator(locators.APPLICATION_CONTROL)
        for index in range(await controls.count()):
            control = controls.nth(index)
            if not await control.is_visible():
                continue
            control_type = ((await control.get_attribute("type")) or "").lower()
            if control_type in {
                "hidden",
                "submit",
                "button",
                "reset",
                "file",
                "image",
                "checkbox",
                "radio",
            }:
                continue
            data_qa = ((await control.get_attribute("data-qa")) or "").lower()
            name = ((await control.get_attribute("name")) or "").strip()
            lowered_name = name.lower()
            if (
                data_qa == "vacancy-response-popup-form-letter-input"
                or "letter" in data_qa
                or "cover" in data_qa
                or "сопровод" in data_qa
                or any(
                    marker in lowered_name
                    for marker in ("cover_letter", "coverletter", "csrf", "xsrf", "captcha")
                )
            ):
                continue

            is_task_control = lowered_name.startswith("task_")
            if is_task_control and task_index < len(task_prompts):
                prompt = task_prompts[task_index]
                task_index += 1
            else:
                aria = ((await control.get_attribute("aria-label")) or "").strip()
                placeholder = ((await control.get_attribute("placeholder")) or "").strip()
                prompt = aria or placeholder
                if prompt.lower() in {"", "писать тут", "ответ", "ваш ответ"}:
                    prompt = label_prompts[len(questions)] if len(questions) < len(label_prompts) else ""
                if not prompt and is_task_control:
                    prompt = f"Обязательный вопрос работодателя ({name})"

            if prompt and "сопровод" not in prompt.lower() and prompt not in questions:
                questions.append(prompt)
        return questions

    async def submit_application(self, page) -> SubmissionResult:
        submit = page.locator(locators.RESPONSE_SUBMIT)
        # HH can render the topic link while the response popup is still
        # active. The active submit control is authoritative in that state.
        if await submit.count():
            await submit.click()
            return await self.verify_submission(page, just_submitted=True)

        # With no active form, the topic link means the response existed
        # before this attempt.
        if await page.locator(locators.ALREADY_APPLIED).count():
            if getattr(self, "_application_attempt_clicked", False):
                return await self.verify_submission(page, just_submitted=True)
            return SubmissionResult(
                status="already_applied",
                message="zarplata.ru показывает ранее отправленный отклик",
            )
        # A one-click response may close the form without rendering the topic
        # link immediately.  Poll the independent success markers before
        # classifying the attempt as unknown.
        return await self.verify_submission(
            page, just_submitted=getattr(self, "_application_attempt_clicked", False)
        )

    async def verify_submission(self, page, just_submitted: bool = False) -> SubmissionResult:
        # HH updates the response form asynchronously. A fixed 1.2 second
        # delay was too short in session 9 and classified six response flows
        # as errors. Poll the visible state without a second click: repeating
        # the submit action would risk a duplicate application.
        for attempt in range(10):
            if await page.locator(locators.ALREADY_APPLIED).count():
                if just_submitted:
                    return SubmissionResult(
                        status="submitted", message="zarplata.ru подтвердил отправку отклика"
                    )
                return SubmissionResult(
                    status="already_applied",
                    message="zarplata.ru показывает ранее отправленный отклик",
                )
            if await page.locator(locators.SUBMISSION_CONFIRMED).count():
                return SubmissionResult(
                    status="submitted", message="zarplata.ru подтвердил отправку отклика"
                )
            text = (await page.locator("body").inner_text()).lower()
            if any(marker in text for marker in locators.SUBMISSION_TEXT_MARKERS):
                return SubmissionResult(
                    status="submitted", message="zarplata.ru подтвердил отправку отклика"
                )
            if attempt < 9:
                await page.wait_for_timeout(500)
        return SubmissionResult(
            status="unknown", message="zarplata.ru не показал однозначное подтверждение отправки"
        )

    async def detect_blockers(self, page) -> list[Blocker]:
        text = (await page.locator("body").inner_text()).lower()
        return (
            [Blocker(kind="captcha", message="Обнаружена CAPTCHA; требуется пользователь")]
            if any(marker.lower() in text for marker in locators.CAPTCHA_MARKERS)
            else []
        )


