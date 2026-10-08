import re
import time
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from playwright.async_api import Error as PlaywrightError

from backend.adapters.base.errors import CaptchaRequired, JobDescriptionUnavailable
from backend.adapters.base.protocol import (
    AdapterManifest,
    ApplicationForm,
    Blocker,
    FillResult,
    JobRef,
    LoginState,
    SubmissionResult,
)
from backend.adapters.base.resume_import import ResumeImportMixin
from backend.orchestrator.recovery import AuthenticationPending
from backend.schemas.domain import ApplicationField, ApplicationPlan, JobPosting

from . import locators
from .resume import POLICY as resume_policy
from .resume import extractor as resume_extractor


class ZarplataAdapter(ResumeImportMixin):
    home_url = "https://zarplata.ru/"
    _vacancy_readiness_timeout_ms = 8_000
    _vacancy_readiness_poll_interval_ms = 100
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
        supports_resume_import=True,
        supports_public_resume_url=True,
        supports_account_resume_list=False,
    )
    resume_policy = resume_policy
    resume_extractor = resume_extractor
    _submission_poll_interval_ms = 500
    _submission_timeout_ms = 30_000
    _letter_poll_interval_ms = 200
    _letter_readiness_timeout_ms = 3_000

    async def start(self, context, settings: dict) -> None:
        return None

    def query_source(self, query: str, field: str = "name", cluster: str = "") -> dict:
        if field not in {"name", "description"}:
            raise ValueError("Unsupported search field")
        text = self.normalize_search_query(str(query))
        params = {"only_with_salary": "false"}
        if text:
            params.update(text=text, search_field=field)
        return {
            "url": f"{self.home_url.rstrip('/')}/search/vacancy?{urlencode(params)}",
            "kind": "query" if text else "coverage",
            "cluster": cluster or text or "broad",
        }

    def validate_search_source(self, spec: dict) -> str:
        url = spec.get("url")
        parsed = urlparse(str(url or ""))
        path = unquote(parsed.path or "")
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        query_keys = {key for key, _ in query_items}
        query = dict(query_items)
        allowed_query_keys = {
            "text", "search_field", "only_with_salary", "salary", "page", "area",
            "region", "employer_id", "employment", "schedule", "professional_role",
            "hhtmFrom", "hhtmFromLabel", "suggestId", "resume",
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.allowed_domains
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not (
                path in {"/", "/search/vacancy", "/vacancies", "/recommendations"}
                or re.fullmatch(r"/employer/\d+/?", path)
            )
            or query_keys - allowed_query_keys
            or len(query_items) != len(query)
            or query.get("search_field", "name") not in {"name", "description"}
            or query.get("only_with_salary", "false") not in {"true", "false"}
            or ("page" in query and not re.fullmatch(r"\d{1,6}", query["page"]))
            or len(query.get("text", "")) > 120
            or ("resume" in query and not re.fullmatch(r"[a-fA-F0-9]{1,128}", query["resume"]))
        ):
            raise ValueError("Недопустимый адрес источника вакансий")
        if spec.get("kind") not in {"recommendations", "query", "coverage", "employer"}:
            raise ValueError("Недопустимый тип источника вакансий")
        canonical_items = [
            (key, value)
            for key, value in query_items
            if key not in {"hhtmFrom", "hhtmFromLabel", "suggestId"}
        ]
        return urlunparse(parsed._replace(query=urlencode(canonical_items), fragment=""))

    async def read_discovery_page(self, page, spec: dict, page_number: int) -> dict:
        base_url = self.validate_search_source(spec)
        parsed_base = urlparse(base_url)
        if parsed_base.path == "/":
            target = base_url
        else:
            target = self._search_page_url(base_url, page_number)
        await page.goto(target, wait_until="commit", timeout=60_000)
        await page.wait_for_timeout(1_000)
        self._validate_search_landing(page, spec, page_number)
        await self._ensure_no_captcha(page)

        if parsed_base.path == "/":
            refs = await self._visible_job_refs(page, timeout=6_000, limit=100)
            terminal = True
        elif parsed_base.path in {"/search/vacancy", "/vacancies", "/recommendations"}:
            refs = await self._visible_job_refs(page, timeout=6_000, limit=100)
            terminal = await self.discovery_listing_terminal(page)
            if not refs and not await self._visible_empty_results(page):
                raise RuntimeError("Выдача не загрузилась; отсутствие вакансий не подтверждено")
        else:
            refs = await self._visible_job_refs(page, timeout=6_000, limit=100)
            terminal = True

        context = "relevant" if spec.get("kind") == "employer" else "listing"
        sources = await self.collect_visible_sources(page, context=context)
        return {"refs": refs, "terminal": terminal, "sources": sources}

    def _validate_search_landing(self, page, spec: dict, page_number: int) -> None:
        current = urlparse(str(getattr(page, "url", "")))
        host = (current.hostname or "").lower().rstrip(".")
        path = unquote(current.path or "")
        if host not in self.allowed_domains:
            raise ValueError("Источник поиска перенаправил за пределы разрешённых доменов")
        if path.rstrip("/") in {"/login", "/account/login", "/account/signup"} or path.startswith("/auth/"):
            raise AuthenticationPending("Ожидание восстановления авторизации на Zarplata")
        expected_path = unquote(urlparse(spec["url"]).path or "")
        if path.rstrip("/") != expected_path.rstrip("/"):
            raise RuntimeError("Страница выдачи перенаправила на другой раздел")
        actual_query = dict(parse_qsl(current.query, keep_blank_values=True))
        expected_query = dict(parse_qsl(urlparse(spec["url"]).query, keep_blank_values=True))
        if page_number and actual_query.get("page") != str(page_number):
            raise RuntimeError("Страница выдачи не подтвердила номер страницы")
        for key in ("text", "search_field", "employer_id", "resume"):
            if actual_query.get(key, "") != expected_query.get(key, ""):
                raise RuntimeError("Страница выдачи не подтвердила параметры источника")

    async def _visible_empty_results(self, page) -> bool:
        empty = page.locator(locators.SEARCH_EMPTY).first
        return bool(await empty.count() and await empty.is_visible())

    async def discovery_listing_terminal(self, page) -> bool:
        pager = page.locator(locators.SEARCH_PAGER).first
        next_page = page.locator(locators.SEARCH_NEXT).first
        if await self._visible_empty_results(page):
            return True
        if await pager.count() and await pager.is_visible():
            return not await next_page.count() or not await next_page.is_visible()
        return False

    async def collect_visible_sources(self, page, context: str = "listing") -> list[dict]:
        links = page.locator(locators.DISCOVERY_LINKS)
        sources = []
        seen = set()
        for index in range(min(1_000, await links.count())):
            link = links.nth(index)
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            if not href:
                continue
            url = urljoin(page.url, href)
            parsed = urlparse(url)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            link_text = (await link.inner_text()).casefold()
            kind = None
            cluster = ""
            if parsed.path == "/search/vacancy":
                if any(marker in link_text for marker in ("для вас", "подходящ", "рекоменд")):
                    kind, cluster = "recommendations", "recommendations"
                elif query.get("text"):
                    kind, cluster = "query", query.get("text", "")
                elif any(key in query for key in ("area", "region", "professional_role", "salary")):
                    kind, cluster = "coverage", "visible-filter"
            elif context == "relevant" and re.fullmatch(r"/employer/\d+/?", unquote(parsed.path or "")):
                kind, cluster = "employer", "employer"
            if not kind:
                continue
            clean_items = sorted(
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key != "page"
            )
            clean_url = urlunparse(parsed._replace(query=urlencode(clean_items), fragment=""))
            source = {"url": clean_url, "kind": kind, "cluster": cluster}
            try:
                clean_url = self.validate_search_source(source)
            except ValueError:
                continue
            source["url"] = clean_url
            key = (clean_url, kind)
            if key not in seen:
                seen.add(key)
                sources.append(source)
                if len(sources) >= 100:
                    break
        return sources

    async def collect_related_refs(self, page) -> list[JobRef]:
        links = page.locator(locators.RELATED_VACANCIES)
        refs = []
        seen = set()
        for index in range(min(200, await links.count())):
            link = links.nth(index)
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            parsed = urlparse(urljoin(page.url, href or ""))
            match = re.fullmatch(r"/vacancy/(\d+)/?", unquote(parsed.path or ""))
            if (
                parsed.scheme == "https"
                and parsed.hostname in self.allowed_domains
                and not parsed.username
                and match
                and match.group(1) not in seen
            ):
                seen.add(match.group(1))
                refs.append(JobRef(external_id=match.group(1), url=urlunparse(parsed)))
        return refs

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
                timeout=60_000,
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
        self._validate_current_listing(page)
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
                    await page.goto(target, wait_until="commit", timeout=60_000)
                    await page.wait_for_timeout(1_500)
                    self._validate_current_listing(page)
                    await self._ensure_no_captcha(page)
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

        while self._search_query_index < len(search_urls):
            fallback_url = search_urls[self._search_query_index]
            self.current_search_query = self._search_queries[self._search_query_index]
            page_number = self._search_page_number
            page_refs = await self._collect_search_page(page, fallback_url, page_number)
            # Advance only after a successfully read page.
            self._search_page_number += 1
            self._search_navigation_count += 1
            if not page_refs:
                # _collect_search_page already retried this page three times;
                # only now is an empty listing considered confirmed exhaustion.
                self._search_query_index += 1
                self._search_page_number = 0
                self._last_search_page_signature = None
                self._repeated_search_pages = 0
                continue
            if self._repeated_search_pages >= 3:
                raise RuntimeError("Выдача повторяет страницу; требуется повторная загрузка")

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

    def _validate_current_listing(self, page) -> None:
        current = urlparse(str(getattr(page, "url", "")))
        host = (current.hostname or "").lower().rstrip(".")
        path = unquote(current.path or "")
        if host not in self.allowed_domains:
            raise ValueError("Страница выдачи находится вне разрешённых доменов Zarplata")
        if path.rstrip("/") in {"/login", "/account/login", "/account/signup"} or path.startswith("/auth/"):
            raise AuthenticationPending("Ожидание восстановления авторизации на Zarplata")
        if path.rstrip("/") not in {"", "/search/vacancy", "/vacancies", "/recommendations"}:
            raise RuntimeError("Страница рекомендаций перенаправила на другой раздел")

    async def _refs(self, page, limit: int = 100) -> list[JobRef]:
        return await self._visible_job_refs(page, timeout=6_000, limit=limit)

    async def open_job(self, page, ref: JobRef) -> None:
        if urlparse(ref.url).hostname not in self.allowed_domains:
            raise ValueError("Переход за пределы разрешённых доменов остановлен")
        self._expected_job_id = str(ref.external_id)
        await page.goto(ref.url, wait_until="commit", timeout=60_000)
        await page.wait_for_timeout(1_000)

    async def extract_job(self, page) -> JobPosting:
        title, company, description = await self._wait_for_vacancy_content(page)

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

    async def _wait_for_vacancy_content(self, page) -> tuple[str, str, str]:
        selectors = {
            "title": locators.VACANCY_TITLE,
            "company": locators.COMPANY,
            "description": locators.DESCRIPTION,
        }
        deadline = time.monotonic() + self._vacancy_readiness_timeout_ms / 1000
        wait = getattr(page, "wait_for_timeout", None)
        values = {}
        diagnostics = {}
        while True:
            await self._validate_vacancy_page(page)
            await self._ensure_no_captcha(page)
            current_values, current_diagnostics = await self._read_required_fields(page, selectors, deadline)
            values.update({field: value for field, value in current_values.items() if value})
            diagnostics.update(current_diagnostics)
            if all(values.get(name) for name in selectors):
                return values["title"], values["company"], values["description"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not callable(wait):
                for field in selectors:
                    if not values.get(field):
                        diagnostics.setdefault(field, {})["outcome"] = "timeout"
                if not values.get("title") or not values.get("company"):
                    raise ValueError("Не удалось извлечь обязательные поля вакансии")
                raise JobDescriptionUnavailable(
                    title=values.get("title"),
                    company=values.get("company"),
                    diagnostics=self._safe_readiness_diagnostics(page, diagnostics),
                )
            await wait(min(self._vacancy_readiness_poll_interval_ms, max(1, int(remaining * 1000))))

    async def _ensure_no_captcha(self, page) -> None:
        blockers = await self.detect_blockers(page)
        if any(blocker.kind == "captcha" for blocker in blockers):
            raise CaptchaRequired("Обнаружена CAPTCHA; требуется пользователь")

    async def _read_required_fields(self, page, selectors: dict[str, str], deadline: float):
        values = {}
        diagnostics = {}
        for field, selector in selectors.items():
            locator = page.locator(selector)
            try:
                count = min(100, max(0, int(await locator.count())))
            except Exception as exc:
                diagnostics[field] = {"count": None, "visibility": [], "states": [self._read_error_state(exc)]}
                continue
            if not count:
                diagnostics[field] = {"count": 0, "visibility": [], "states": ["absent"]}
                continue
            states = []
            for index in range(min(count, 20)):
                candidate = locator.nth(index) if hasattr(locator, "nth") else locator.first
                try:
                    visible = await candidate.is_visible()
                except Exception as exc:
                    states.append(("read-error", self._read_error_state(exc)))
                    continue
                if not visible:
                    states.append(("hidden", "hidden"))
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    states.append(("visible", "timeout"))
                    break
                try:
                    timeout_ms = min(
                        self._vacancy_readiness_poll_interval_ms,
                        max(1, int(remaining * 1000)),
                    )
                    value = (await candidate.inner_text(timeout=timeout_ms)).strip()
                except CaptchaRequired:
                    raise
                except Exception as exc:
                    states.append(("visible", self._read_error_state(exc)))
                    continue
                if value:
                    values[field] = value
                    states.append(("visible", "ready"))
                    break
                states.append(("visible", "blank"))
            diagnostics[field] = {
                "count": count,
                "visibility": [visibility for visibility, _ in states[:5]],
                "states": [state for _, state in states[:5]],
                "outcome": "ready" if field in values else "waiting",
            }
        return values, diagnostics

    @staticmethod
    def _read_error_state(exc: Exception) -> str:
        detail = str(exc).casefold()
        if "detach" in detail or "not attached" in detail:
            return "detached"
        if "timeout" in detail:
            return "timeout"
        return "read-error"

    async def _validate_vacancy_page(self, page) -> None:
        current = urlparse(str(getattr(page, "url", "")))
        host = (current.hostname or "").lower().rstrip(".")
        path = unquote(current.path or "")
        if current.scheme == "about" and current.path == "blank" and not hasattr(self, "_expected_job_id"):
            return
        if host not in self.allowed_domains:
            raise AuthenticationPending("Zarplata перенаправила с вакансии; требуется проверить вход")
        if path.rstrip("/") in {"/login", "/account/login", "/account/signup"} or path.startswith("/auth/"):
            raise AuthenticationPending("Ожидание восстановления авторизации на Zarplata")
        match = re.fullmatch(r"/vacancy/([^/]+)/?", path)
        expected = getattr(self, "_expected_job_id", None)
        if not match or (expected is not None and match.group(1) != str(expected)):
            raise JobDescriptionUnavailable(
                diagnostics=self._safe_readiness_diagnostics(page, {
                    "identity": {"count": 0, "visibility": [], "states": ["wrong-vacancy"]}
                })
            )

    async def _validate_application_page(self, page) -> None:
        """Allow only the expected vacancy or its observed response form route."""
        current = urlparse(str(getattr(page, "url", "")))
        host = (current.hostname or "").lower().rstrip(".")
        path = unquote(current.path or "")
        try:
            port = current.port
        except ValueError:
            port = -1
        if (
            current.scheme != "https"
            or host not in self.allowed_domains
            or current.username
            or current.password
            or port not in (None, 443)
        ):
            raise AuthenticationPending(
                "Zarplata перенаправила со страницы отклика; требуется проверить вход"
            )
        if path.rstrip("/") in {"/login", "/account/login", "/account/signup"} or path.startswith("/auth/"):
            raise AuthenticationPending("Ожидание восстановления авторизации на Zarplata")

        expected = getattr(self, "_expected_job_id", None)
        if expected is not None and not re.fullmatch(r"[0-9]+", str(expected)):
            raise JobDescriptionUnavailable(
                diagnostics=self._safe_readiness_diagnostics(page, {
                    "identity": {"count": 0, "visibility": [], "states": ["invalid-expected-vacancy"]}
                })
            )
        vacancy = re.fullmatch(r"/vacancy/([0-9]+)/?", path)
        if vacancy and (expected is None or vacancy.group(1) == str(expected)):
            return

        if path == "/applicant/vacancy_response":
            vacancy_ids = [
                value
                for key, value in parse_qsl(current.query, keep_blank_values=True)
                if key == "vacancyId"
            ]
            if (
                len(vacancy_ids) == 1
                and re.fullmatch(r"[0-9]+", vacancy_ids[0])
                and expected is not None
                and vacancy_ids[0] == str(expected)
            ):
                return

        raise JobDescriptionUnavailable(
            diagnostics=self._safe_readiness_diagnostics(page, {
                "identity": {"count": 0, "visibility": [], "states": ["wrong-application-route"]}
            })
        )

    @staticmethod
    def _safe_readiness_diagnostics(page, fields: dict) -> dict:
        current = urlparse(str(getattr(page, "url", "")))
        path = unquote(current.path or "")
        match = re.fullmatch(r"/vacancy/([^/]+)/?", path)
        safe_path = f"/vacancy/{match.group(1)}" if match else "/<non-vacancy>"
        return {"url_path": safe_path, "external_id": match.group(1) if match else None, "fields": fields}

    async def open_application(self, page) -> ApplicationForm:
        self._reset_submission_progress()
        await self._validate_vacancy_page(page)
        await self._ensure_no_captcha(page)
        response = page.locator(locators.RESPONSE_BUTTON).first
        if not await response.count():
            return await self.read_application(page)
        await response.click()
        self._application_attempt_clicked = True
        await page.wait_for_timeout(800)
        self._record_cv_confirmation(await self.verify_cv_submission(page))
        return await self.read_application(page)

    def _reset_submission_progress(self) -> None:
        self._application_attempt_clicked = False
        self._zarplata_cv_submission_confirmed = False
        self._zarplata_cover_letter_pending = False
        self._zarplata_cover_letter_confirmed = False
        self._zarplata_cover_letter_diagnostic = None
        self._zarplata_cover_letter_expected = False
        self._zarplata_cover_letter_attempted = False
        self._zarplata_cover_letter_filled = False
        self._zarplata_cover_letter_submit_clicked = False
        self._zarplata_cover_letter_flow = None

    def get_submission_progress(self) -> dict:
        return {
            "cv_confirmed": bool(
                getattr(self, "_zarplata_cv_submission_confirmed", False)
            ),
            "cover_letter_pending": bool(
                getattr(self, "_zarplata_cover_letter_pending", False)
            ),
            "cover_letter_confirmed": bool(
                getattr(self, "_zarplata_cover_letter_confirmed", False)
            ),
            "cover_letter_attempted": bool(
                getattr(self, "_zarplata_cover_letter_attempted", False)
            ),
            "cover_letter_diagnostic": getattr(
                self, "_zarplata_cover_letter_diagnostic", None
            ),
        }

    def prepare_submission_progress(self) -> dict:
        """Mark an exact, expected letter as an attempt before submit is called."""
        if (
            getattr(self, "_zarplata_cover_letter_expected", False)
            and getattr(self, "_zarplata_cover_letter_filled", False)
            and not getattr(self, "_zarplata_cover_letter_confirmed", False)
        ):
            self._zarplata_cover_letter_attempted = True
        return self.get_submission_progress()

    def _record_cv_confirmation(self, result: SubmissionResult) -> SubmissionResult:
        if result.status == "submitted":
            self._zarplata_cv_submission_confirmed = True
        return result

    async def read_application(self, page) -> ApplicationForm:
        await self._validate_application_page(page)
        await self._ensure_no_captcha(page)
        self._record_cv_confirmation(await self.verify_cv_submission(page))
        questions = await self._application_questions(page)
        input_state = await self._letter_input_state(page)
        has_letter_ui = bool(
            input_state["count"]
            or await page.locator(locators.COVER_LETTER_TOGGLE).count()
            or await page.locator(locators.COVER_LETTER_SUBMIT).count()
        )
        return ApplicationForm(
            requires_cover_letter=has_letter_ui,
            questions=questions,
            fields=getattr(self, "_application_fields", []),
        )

    async def _letter_input_state(self, page) -> dict:
        field = page.locator(locators.COVER_LETTER_INPUT).first
        count = min(100, max(0, int(await field.count())))
        visible = bool(count and await field.is_visible())
        enabled = bool(visible and await field.is_enabled())
        if not count:
            category = "absent"
        elif not visible:
            category = "hidden"
        elif not enabled:
            category = "disabled"
        else:
            category = "ready"
        diagnostic = {
            "category": category,
            "count": count,
            "visible": visible if count else None,
            "enabled": enabled if visible else None,
            "exception_category": None,
        }
        self._zarplata_cover_letter_diagnostic = diagnostic
        return diagnostic

    async def prepare_application(
        self, page, plan: ApplicationPlan
    ) -> ApplicationForm:
        await self._validate_application_page(page)
        await self._ensure_no_captcha(page)
        if not plan.cover_letter:
            return await self.read_application(page)

        self._record_cv_confirmation(await self.verify_cv_submission(page))
        self._zarplata_cover_letter_expected = True
        self._zarplata_cover_letter_pending = not getattr(
            self, "_zarplata_cover_letter_confirmed", False
        )
        state = await self._letter_input_state(page)
        if not state["count"] and getattr(
            self, "_zarplata_cv_submission_confirmed", False
        ):
            attach = page.locator(locators.ATTACH_COVER_LETTER).first
            if await attach.count() and await attach.is_visible():
                await self._ensure_no_captcha(page)
                await attach.click()
        elif not state["count"]:
            toggle = page.locator(locators.COVER_LETTER_TOGGLE).first
            if await toggle.count() and await toggle.is_visible():
                await self._ensure_no_captcha(page)
                await toggle.click()

        deadline = time.monotonic() + self._letter_readiness_timeout_ms / 1000
        while True:
            await self._validate_application_page(page)
            await self._ensure_no_captcha(page)
            state = await self._letter_input_state(page)
            separate_submit_ready = await self._letter_submit_ready(page)
            cv_confirmed = getattr(
                self, "_zarplata_cv_submission_confirmed", False
            )
            if state["category"] == "ready" and (
                not cv_confirmed or separate_submit_ready
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await page.wait_for_timeout(
                min(self._letter_poll_interval_ms, max(1, int(remaining * 1000)))
            )
        questions = await self._application_questions(page)
        if (
            state["category"] == "ready"
            and cv_confirmed
            and not separate_submit_ready
        ):
            state = {
                **state,
                "category": "letter_submit_unavailable",
                "submit_count": min(
                    100,
                    max(0, int(await page.locator(locators.COVER_LETTER_SUBMIT).count())),
                ),
            }
            self._zarplata_cover_letter_diagnostic = state
        has_letter_ui = state["category"] == "ready" or bool(
            await page.locator(locators.COVER_LETTER_SUBMIT).count()
        )
        return ApplicationForm(
            requires_cover_letter=has_letter_ui,
            questions=questions,
            fields=getattr(self, "_application_fields", []),
        )

    async def _letter_submit_ready(self, page) -> bool:
        submit = page.locator(locators.COVER_LETTER_SUBMIT).first
        return bool(
            await submit.count()
            and await submit.is_visible()
            and await submit.is_enabled()
        )

    async def fill_application(self, page, plan: ApplicationPlan) -> FillResult:
        await self._validate_application_page(page)
        self._zarplata_cover_letter_expected = bool(plan.cover_letter)
        if plan.cover_letter:
            self._zarplata_cover_letter_pending = not getattr(
                self, "_zarplata_cover_letter_confirmed", False
            )
        await self._ensure_no_captcha(page)
        if not plan.submission_allowed:
            return FillResult(success=False, unknown_questions=["Отправка не разрешена планом"])
        if plan.cover_letter:
            await self.prepare_application(page, plan)
            state = await self._letter_input_state(page)
            if state["category"] != "ready":
                self._zarplata_cover_letter_pending = bool(
                    getattr(self, "_zarplata_cv_submission_confirmed", False)
                )
                return FillResult(
                    success=False,
                    unknown_questions=["Сопроводительное письмо"],
                )
            cv_confirmed = getattr(
                self, "_zarplata_cv_submission_confirmed", False
            )
            if cv_confirmed and not await self._letter_submit_ready(page):
                self._zarplata_cover_letter_pending = True
                self._zarplata_cover_letter_diagnostic = {
                    **state,
                    "category": "letter_submit_unavailable",
                    "submit_count": min(
                        100,
                        max(
                            0,
                            int(await page.locator(locators.COVER_LETTER_SUBMIT).count()),
                        ),
                    ),
                }
                return FillResult(
                    success=False,
                    unknown_questions=["Сопроводительное письмо"],
                )
            letter_input = page.locator(locators.COVER_LETTER_INPUT).first
            await self._ensure_no_captcha(page)
            await letter_input.fill(plan.cover_letter)
            if await letter_input.input_value() != plan.cover_letter:
                self._zarplata_cover_letter_diagnostic = {
                    **state,
                    "category": "value_mismatch",
                }
                return FillResult(
                    success=False,
                    unknown_questions=["Сопроводительное письмо"],
                )
            self._zarplata_cover_letter_filled = True
            self._zarplata_cover_letter_pending = True
            self._zarplata_cover_letter_diagnostic = {
                **state,
                "category": "filled_exactly",
            }
            separate_submit = page.locator(locators.COVER_LETTER_SUBMIT).first
            self._zarplata_cover_letter_flow = (
                "separate"
                if self._zarplata_cv_submission_confirmed
                and await separate_submit.count()
                and await separate_submit.is_visible()
                else "ordinary"
            )
        unanswered = await self._application_questions(page)
        answered = []
        controls = page.locator(locators.APPLICATION_CONTROL)
        for field in getattr(self, "_application_fields", []):
            answer = plan.form_answers.get(field.id)
            if not answer or answer.field != field or len(answer.values) != 1 or field.kind == "unsupported":
                continue
            control = controls.nth(int(field.id.removeprefix("zarplata-control-")))
            await control.fill(answer.values[0])
            if await control.input_value() == answer.values[0]:
                answered.append(field.id)
        pending_fields = [field.label for field in getattr(self, "_application_fields", []) if field.id not in answered]
        return FillResult(success=not pending_fields and (bool(answered) or not unanswered)
                          and (not plan.cover_letter or self._zarplata_cover_letter_filled),
                          unknown_questions=pending_fields or ([] if answered else unanswered),
                          answered_fields=answered)

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
        self._application_fields = []
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

            if prompt and "сопровод" not in prompt.lower():
                self._application_fields.append(ApplicationField(
                    id=f"zarplata-control-{index}", label=prompt,
                    kind="number" if control_type == "number" else "text" if control_type in {"", "text", "email", "tel", "url", "textarea"} else "unsupported",
                ))
                if prompt not in questions:
                    questions.append(prompt)
        return questions

    async def can_retry_application(self, page) -> bool:
        """The loaded vacancy explicitly offers a new application, with no prior response."""
        # This check runs while the workflow is on the vacancy page. Do not
        # re-check authentication here: the fallback protected-route probe in
        # ``get_login_state`` navigates away and destroys the vacancy context.
        try:
            current = urlparse(str(getattr(page, "url", "")))
            hostname = (current.hostname or "").lower().rstrip(".")
            path = unquote(current.path or "")
        except Exception:
            return False
        if (
            current.scheme not in {"http", "https"}
            or hostname not in self.allowed_domains
            or not re.fullmatch(r"/vacancy/\d+/?", path)
        ):
            return False
        if await page.locator(locators.ALREADY_APPLIED).count():
            return False
        response = page.locator(locators.RESPONSE_BUTTON).first
        return bool(await response.count() and await response.is_visible())

    async def verify_cv_submission(self, page) -> SubmissionResult:
        """Verify the current page's visible CV response state without clicking."""
        await self._validate_application_page(page)
        await self._ensure_no_captcha(page)
        if await page.locator(locators.ALREADY_APPLIED).count():
            self._zarplata_cv_submission_confirmed = True
            return SubmissionResult(
                status="submitted", message="Zarplata подтвердил отправку резюме"
            )
        if await page.locator(locators.SUBMISSION_CONFIRMED).count():
            self._zarplata_cv_submission_confirmed = True
            return SubmissionResult(
                status="submitted", message="Zarplata подтвердил отправку резюме"
            )
        text = ((await page.locator("body").inner_text()) or "").lower()
        if any(marker in text for marker in locators.SUBMISSION_TEXT_MARKERS):
            self._zarplata_cv_submission_confirmed = True
            return SubmissionResult(
                status="submitted", message="Zarplata подтвердил отправку резюме"
            )
        return SubmissionResult(
            status="unknown", message="Zarplata не подтвердил отправку резюме"
        )

    async def reconcile_submission_progress(
        self, page, *, letter_expected: bool
    ) -> dict:
        """Read-only recovery state; never infer a letter from a CV topic."""
        cv_result = await self.verify_cv_submission(page)
        cv_confirmed = cv_result.status == "submitted"
        pending = bool(cv_confirmed and letter_expected)
        recovery_safe = False
        if pending:
            attach = page.locator(locators.ATTACH_COVER_LETTER).first
            attach_visible = bool(await attach.count() and await attach.is_visible())
            field = page.locator(locators.COVER_LETTER_INPUT).first
            field_ready = bool(
                await field.count()
                and await field.is_visible()
                and await field.is_enabled()
            )
            submit = page.locator(locators.COVER_LETTER_SUBMIT).first
            submit_ready = bool(
                await submit.count()
                and await submit.is_visible()
                and await submit.is_enabled()
            )
            recovery_safe = attach_visible or (field_ready and submit_ready)
        self._zarplata_cv_submission_confirmed = cv_confirmed
        self._zarplata_cover_letter_expected = bool(letter_expected)
        self._zarplata_cover_letter_pending = pending
        # An adapter instance can be reused. Only the workflow may restore a
        # durable, vacancy-scoped letter confirmation.
        self._zarplata_cover_letter_confirmed = False
        return {
            "cv_confirmed": cv_confirmed,
            "cover_letter_pending": pending,
            "cover_letter_confirmed": False,
            "letter_recovery_safe": recovery_safe,
        }

    async def resume_application(
        self,
        page,
        plan: ApplicationPlan,
        *,
        cv_confirmed: bool,
        cover_letter_pending: bool,
    ) -> ApplicationForm:
        """Resume a pending letter only after re-verifying CV; never click response."""
        await self._validate_application_page(page)
        await self._ensure_no_captcha(page)
        if not cv_confirmed:
            return ApplicationForm(questions=["Отправка резюме не подтверждена"])
        result = await self.verify_cv_submission(page)
        if result.status != "submitted":
            return ApplicationForm(
                questions=["Не удалось подтвердить ранее отправленное резюме"]
            )
        self._application_attempt_clicked = False
        self._zarplata_cv_submission_confirmed = True
        self._zarplata_cover_letter_expected = bool(plan.cover_letter)
        self._zarplata_cover_letter_pending = bool(
            plan.cover_letter and cover_letter_pending
        )
        self._zarplata_cover_letter_confirmed = False
        self._zarplata_cover_letter_filled = False
        self._zarplata_cover_letter_submit_clicked = False
        self._zarplata_cover_letter_flow = "separate" if plan.cover_letter else None
        return await self.prepare_application(page, plan)

    async def submit_application(self, page) -> SubmissionResult:
        await self._validate_application_page(page)
        await self._ensure_no_captcha(page)
        if getattr(self, "_zarplata_cover_letter_expected", False):
            if not getattr(self, "_zarplata_cover_letter_filled", False):
                self._zarplata_cover_letter_pending = bool(
                    self._zarplata_cv_submission_confirmed
                )
                return SubmissionResult(
                    status="needs_input",
                    message="Сопроводительное письмо не заполнено и не отправлено",
                )
            self.prepare_submission_progress()
            if getattr(self, "_zarplata_cover_letter_flow", None) == "separate":
                if not self._zarplata_cv_submission_confirmed:
                    cv_result = await self.verify_cv_submission(page)
                    if cv_result.status != "submitted":
                        return SubmissionResult(
                            status="unknown",
                            message="Не удалось подтвердить отправку резюме",
                        )
                if getattr(self, "_zarplata_cover_letter_submit_clicked", False):
                    return await self._verify_letter_submission(page)
                if not await self._click_letter_submit_when_ready(page):
                    self._zarplata_cover_letter_pending = True
                    return SubmissionResult(
                        status="needs_input",
                        message="Кнопка отправки сопроводительного письма недоступна",
                    )
                return await self._verify_letter_submission(page)

            if getattr(self, "_zarplata_cover_letter_submit_clicked", False):
                cv_result = await self.verify_submission(page, just_submitted=True)
                if cv_result.status == "submitted":
                    self._zarplata_cover_letter_confirmed = True
                    self._zarplata_cover_letter_pending = False
                return cv_result
            if await self._click_submission_when_ready(page, letter_submit=True):
                cv_result = await self.verify_submission(page, just_submitted=True)
                if cv_result.status == "submitted":
                    self._zarplata_cover_letter_confirmed = True
                    self._zarplata_cover_letter_pending = False
                    self._zarplata_cover_letter_diagnostic = {
                        **(self._zarplata_cover_letter_diagnostic or {}),
                        "category": "confirmed",
                    }
                return cv_result
            return SubmissionResult(
                status="needs_input",
                message="Форма отклика с сопроводительным письмом недоступна",
            )

        if await self._click_submission_when_ready(page):
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

    async def _click_letter_submit_when_ready(self, page) -> bool:
        submit = page.locator(locators.COVER_LETTER_SUBMIT).first
        attempts = max(0, self._submission_timeout_ms // self._submission_poll_interval_ms)
        for attempt in range(attempts + 1):
            await self._validate_application_page(page)
            await self._ensure_no_captcha(page)
            if await submit.count() and await submit.is_visible() and await submit.is_enabled():
                self._zarplata_cover_letter_submit_clicked = True
                await submit.click()
                return True
            if attempt < attempts:
                await page.wait_for_timeout(self._submission_poll_interval_ms)
        return False

    async def _verify_letter_submission(self, page) -> SubmissionResult:
        attempts = max(0, self._submission_timeout_ms // self._submission_poll_interval_ms)
        for attempt in range(attempts + 1):
            await self._validate_application_page(page)
            await self._ensure_no_captcha(page)
            cv_result = await self.verify_cv_submission(page)
            if cv_result.status != "submitted":
                self._zarplata_cover_letter_pending = True
                return SubmissionResult(
                    status="unknown",
                    message="Текущее подтверждение отправки резюме не найдено",
                )
            field = page.locator(locators.COVER_LETTER_INPUT).first
            submit = page.locator(locators.COVER_LETTER_SUBMIT).first
            if not await field.count() and not await submit.count():
                self._zarplata_cover_letter_confirmed = True
                self._zarplata_cover_letter_pending = False
                self._zarplata_cover_letter_diagnostic = {
                    **(self._zarplata_cover_letter_diagnostic or {}),
                    "category": "confirmed",
                }
                return SubmissionResult(
                    status="submitted",
                    message="Zarplata подтвердил отправку сопроводительного письма",
                )
            if attempt < attempts:
                await page.wait_for_timeout(self._submission_poll_interval_ms)
        self._zarplata_cover_letter_pending = True
        return SubmissionResult(
            status="unknown",
            message="Zarplata не подтвердил отправку сопроводительного письма",
        )

    async def _click_submission_when_ready(
        self, page, *, letter_submit: bool = False
    ) -> bool:
        """Click an attached, visible, enabled submit control at most once."""
        submit = page.locator(locators.RESPONSE_SUBMIT)
        if not await submit.count():
            return False
        attempts = self._submission_timeout_ms // self._submission_poll_interval_ms
        for attempt in range(attempts + 1):
            await self._validate_application_page(page)
            await self._ensure_no_captcha(page)
            is_visible = getattr(submit, "is_visible", None)
            if is_visible is not None and not await is_visible():
                ready = False
            else:
                is_enabled = getattr(submit, "is_enabled", None)
                ready = is_enabled is None or await is_enabled()
            if ready:
                if letter_submit:
                    self._zarplata_cover_letter_submit_clicked = True
                await submit.click()
                return True
            if attempt < attempts:
                await page.wait_for_timeout(self._submission_poll_interval_ms)
        return False

    async def verify_submission(self, page, just_submitted: bool = False) -> SubmissionResult:
        # Zarplata may update the response state asynchronously well after the
        # click. Poll the visible state without a second click: repeating the
        # submit action would risk a duplicate application.
        await self._validate_application_page(page)
        await self._ensure_no_captcha(page)
        attempts = self._submission_timeout_ms // self._submission_poll_interval_ms
        for attempt in range(attempts + 1):
            await self._validate_application_page(page)
            await self._ensure_no_captcha(page)
            if await page.locator(locators.ALREADY_APPLIED).count():
                if just_submitted:
                    self._zarplata_cv_submission_confirmed = True
                    return SubmissionResult(
                        status="submitted", message="zarplata.ru подтвердил отправку отклика"
                    )
                return SubmissionResult(
                    status="already_applied",
                    message="zarplata.ru показывает ранее отправленный отклик",
                )
            if await page.locator(locators.SUBMISSION_CONFIRMED).count():
                self._zarplata_cv_submission_confirmed = True
                return SubmissionResult(
                    status="submitted", message="zarplata.ru подтвердил отправку отклика"
                )
            text = ((await page.locator("body").inner_text()) or "").lower()
            if any(marker in text for marker in locators.SUBMISSION_TEXT_MARKERS):
                self._zarplata_cv_submission_confirmed = True
                return SubmissionResult(
                    status="submitted", message="zarplata.ru подтвердил отправку отклика"
                )
            if attempt < attempts:
                await page.wait_for_timeout(self._submission_poll_interval_ms)
        return SubmissionResult(
            status="unknown", message="zarplata.ru не показал однозначное подтверждение отправки"
        )

    async def detect_blockers(self, page) -> list[Blocker]:
        body = page.locator("body")
        inner_text = getattr(body, "inner_text", None)
        try:
            text = ((await inner_text()) or "").lower() if inner_text else ""
        except Exception:
            text = ""
        return (
            [Blocker(kind="captcha", message="Обнаружена CAPTCHA; требуется пользователь")]
            if any(marker.lower() in text for marker in locators.CAPTCHA_MARKERS)
            else []
        )


