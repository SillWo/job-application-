import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

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


class HHAdapter:
    site_id = "hh"
    display_name = "HH.ru"
    # HH redirects authenticated users to their regional subdomain and emits
    # vacancy links on that same host.
    allowed_domains = ("hh.ru", "www.hh.ru", "krasnoyarsk.hh.ru")
    manifest = AdapterManifest(
        site_id=site_id,
        display_name=display_name,
        allowed_domains=allowed_domains,
        supports_submission=True,
        safe_live_modes=("analysis_only", "review_before_submit", "autopilot"),
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
        query = self.normalize_search_query(filters.get("query", ""))
        params = {
            "text": query,
            "search_field": "name",
            "only_with_salary": str(not filters.get("include_unspecified_salary", True)).lower(),
        }
        if filters.get("salary_min"):
            params["salary"] = filters["salary_min"]
        self._fallback_search_url = f"https://hh.ru/search/vacancy?{urlencode(params)}"
        self._search_page_number = 0
        self._search_seen_ids: set[str] = set()
        self._search_exhausted = False
        self._last_search_page_signature: tuple[str, ...] | None = None
        self._repeated_search_pages = 0
        self.current_result_page: int | None = None

        # The authenticated home page contains HH's personalized "Для вас"
        # recommendations. Prefer that ranking over a brittle text query.
        await page.goto(
            "https://hh.ru/",
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

    async def collect_job_refs(self, page) -> list[JobRef]:
        recommended_refs = await self._visible_job_refs(page, timeout=6_000)
        self._search_page_number = 0
        self._search_seen_ids = {ref.external_id for ref in recommended_refs}
        self._search_exhausted = False
        self._last_search_page_signature = None
        self._repeated_search_pages = 0
        self.current_result_page = None
        return recommended_refs

    async def collect_more_job_refs(self, page) -> list[JobRef]:
        """Fetch the next search-result batch after recommendations are drained.

        HH's personalized page and the text-search result pages are separate
        listings. Keeping a cursor here prevents the workflow from treating a
        duplicate-heavy personalized snapshot as the end of an unlimited
        session. Empty/repeated pages are consumed internally, while the
        finite page bound protects against a broken/stale listing.
        """
        if self._search_exhausted:
            return []
        fallback_url = getattr(self, "_fallback_search_url", None)
        if not fallback_url:
            self._search_exhausted = True
            return []

        while self._search_page_number < 100:
            page_number = self._search_page_number
            self._search_page_number += 1
            page_refs = await self._collect_search_page(page, fallback_url, page_number)
            if not page_refs:
                # _collect_search_page already retried this page three times;
                # only now is an empty listing considered confirmed exhaustion.
                self._search_exhausted = True
                return []
            if self._repeated_search_pages >= 3:
                self._search_exhausted = True
                return []

            new_refs = []
            for ref in page_refs:
                if ref.external_id not in self._search_seen_ids:
                    self._search_seen_ids.add(ref.external_id)
                    new_refs.append(ref)
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

    async def _visible_job_refs(self, page, timeout: int) -> list[JobRef]:
        links = page.locator(locators.VACANCY_LINK)
        try:
            await links.first.wait_for(state="attached", timeout=timeout)
        except Exception:
            return []
        ranked_refs: list[tuple[int, int, JobRef]] = []
        for i in range(min(await links.count(), 100)):
            link = links.nth(i)
            href = await link.get_attribute("href")
            url = urljoin(page.url, href) if href else None
            parsed = urlparse(url) if url else None
            vacancy_match = re.fullmatch(r"/vacancy/(\d+)/?", parsed.path) if parsed else None
            if parsed and parsed.hostname in self.allowed_domains and vacancy_match:
                external_id = vacancy_match.group(1)
                if all(ref.external_id != external_id for _, _, ref in ranked_refs):
                    try:
                        label = (await link.inner_text()).lower()
                    except Exception:
                        label = ""
                    priority = (
                        0
                        if "product" in label or "продукт" in label
                        else 1
                        if "project" in label or "проект" in label
                        else 2
                    )
                    ranked_refs.append(
                        (priority, i, JobRef(external_id=external_id, url=url))
                    )
        return [ref for _, _, ref in sorted(ranked_refs)]

    async def open_job(self, page, ref: JobRef) -> None:
        if urlparse(ref.url).hostname not in self.allowed_domains:
            raise ValueError("Переход за пределы разрешённых доменов остановлен")
        await page.goto(ref.url, wait_until="commit", timeout=15_000)
        await page.wait_for_timeout(1_000)

    async def extract_job(self, page) -> JobPosting:
        title = (await page.locator(locators.VACANCY_TITLE).inner_text(timeout=8_000)).strip()
        company = (await page.locator(locators.COMPANY).inner_text(timeout=8_000)).strip()
        description = (await page.locator(locators.DESCRIPTION).inner_text(timeout=8_000)).strip()
        external_id = page.url.rstrip("/").split("/")[-1].split("?")[0]
        return JobPosting(
            source="hh",
            external_id=external_id,
            url=page.url,
            title=title,
            company=company,
            description=description,
            has_test_assignment=self.has_test_assignment(description),
        )

    async def detect_page_type(self, page) -> str:
        if await page.locator(locators.VACANCY_TITLE).count():
            return "vacancy"
        if "/search/vacancy" in page.url:
            return "search"
        return "unknown"

    async def open_application(self, page) -> ApplicationForm:
        response = page.locator(locators.RESPONSE_BUTTON).first
        if not await response.count():
            return ApplicationForm()
        await response.click()
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

        HH's standalone response page renders test questions as bare textareas,
        not as labels matching the popup selector. Detect controls first and
        attach HH's nearby task prompts by order; metadata is a conservative
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
            return SubmissionResult(
                status="already_applied",
                message="HH.ru показывает ранее отправленный отклик",
            )
        return await self.verify_submission(page)

    async def verify_submission(self, page, just_submitted: bool = False) -> SubmissionResult:
        # HH updates the response form asynchronously. A fixed 1.2 second
        # delay was too short in session 9 and classified six response flows
        # as errors. Poll the visible state without a second click: repeating
        # the submit action would risk a duplicate application.
        for attempt in range(10):
            if await page.locator(locators.ALREADY_APPLIED).count():
                if just_submitted:
                    return SubmissionResult(
                        status="submitted", message="HH.ru подтвердил отправку отклика"
                    )
                return SubmissionResult(
                    status="already_applied",
                    message="HH.ru показывает ранее отправленный отклик",
                )
            if await page.locator(locators.SUBMISSION_CONFIRMED).count():
                return SubmissionResult(
                    status="submitted", message="HH.ru подтвердил отправку отклика"
                )
            text = (await page.locator("body").inner_text()).lower()
            if any(marker in text for marker in locators.SUBMISSION_TEXT_MARKERS):
                return SubmissionResult(
                    status="submitted", message="HH.ru подтвердил отправку отклика"
                )
            if attempt < 9:
                await page.wait_for_timeout(500)
        return SubmissionResult(
            status="unknown", message="HH.ru не показал однозначное подтверждение отправки"
        )

    async def detect_blockers(self, page) -> list[Blocker]:
        text = (await page.locator("body").inner_text()).lower()
        return (
            [Blocker(kind="captcha", message="Обнаружена CAPTCHA; требуется пользователь")]
            if any(marker.lower() in text for marker in locators.CAPTCHA_MARKERS)
            else []
        )
