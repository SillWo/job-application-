"""HireHi adaptive-search navigation primitives.

This module intentionally knows only about URLs and information rendered in
the browser.  It does not call HireHi endpoints or make decisions about
relevance; the orchestrator owns those concerns.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from backend.adapters.base.protocol import JobRef

from . import locators

# Slugs confirmed in the visible HireHi category/specialization navigation.
SPECIALIZATION_SLUGS = frozenset(
    ["product-manager", "project-manager", "backend", "frontend", "fullstack", "python", "java", "go", "mobile", "ios", "android", "ml-ai", "nodejs", "data-engineer", "kotlin", "rust", "1c", "dotnet", "cpp", "php", "ci-cd", "cloud", "iac", "infrastructure", "kubernetes", "observability", "security", "sre-platform", "business-analyst", "data-analyst", "product-analyst", "system-analyst", "aso-orm", "community", "content-creative", "crm-lifecycle", "general-marketing", "media-buyer", "performance-marketing", "seo", "smm", "game-design", "graphic-design", "illustration", "motion-design", "product-design", "ux-ui", "web-design", "qa-automation", "manual-qa", "sales-management", "account-management", "business-development", "customer-success", "recruiters", "talent-acquisition", "hr", "sourcing", "financial-analysis", "finance-management", "accounting", "fpna-controlling"]
)
CATEGORY_SLUGS = frozenset(
    path.rsplit("/", 1)[-1]
    for path in (
        "/vacancies/design", "/vacancies/development", "/vacancies/devops",
        "/vacancies/management", "/vacancies/qa", "/vacancies/analytics",
        "/vacancies/marketing", "/vacancies/sales", "/vacancies/finance",
        "/vacancies/recruiting",
    )
)
LISTING_SLUGS = CATEGORY_SLUGS | SPECIALIZATION_SLUGS

# These are URL parameter names emitted by the observed filter controls.  A
# broad allow-list is safer than copying arbitrary query parameters from an
# anchor, while retaining compatibility with old HireHi fixtures (`level`).
QUERY_PARAMS = frozenset({
    "search", "page", "level", "grade", "application_type",
    "application_type_subtype", "work_format", "format", "remote",
    "location", "region", "industry", "language", "english",
    "salary",
    "employment_type", "pro", "match_me", "match", "direct_contact",
})
MAX_PAGE = 10_000
FILTER_LEVELS = frozenset({"intern", "junior", "middle", "senior", "lead", "head"})
FILTER_FORMATS = frozenset({"удалённо", "офис", "гибрид", "удалённо по РФ"})
FILTER_ENGLISH = frozenset({"english", "no_english"})
FILTER_CONTACT = frozenset({"direct_contact", "linkedin", "email", "telegram", "exclude"})
FILTER_REGIONS = frozenset({"Europe & UK", "CIS", "Russia"})
FILTER_SALARY_MAX = 1_000_000_000
_DETAIL_RE = re.compile(r"^/(?P<category>[a-z0-9-]+)/[^/?#]+-(?P<id>\d+)/?$", re.I)


def _canonical_query(parsed):
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    unknown = [key for key, _ in pairs if key not in QUERY_PARAMS]
    if unknown:
        raise ValueError(f"Недопустимый параметр поиска HireHi: {unknown[0]}")
    # Stable ordering makes source fingerprints deterministic.  Keep repeated
    # grade/filter values; they are meaningful to the visible UI.
    return urlencode(sorted(pairs), doseq=True)


def validate_url(adapter, url: str, spec: dict | None = None) -> str:
    """Validate and canonicalize a browser-visible HireHi listing URL."""
    if not isinstance(url, str) or not url:
        raise ValueError("Недопустимый URL источника HireHi")
    if spec and "page" in spec:
        validate_page_number(spec["page"])
    if spec and "filters" in spec:
        normalize_filters(spec["filters"])
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Недопустимый порт HireHi") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https" or host not in adapter.allowed_domains
        or parsed.username or parsed.password or port is not None
        or parsed.fragment
    ):
        raise ValueError("Недопустимый URL источника HireHi")
    path = parsed.path or "/"
    normalized_path = path.rstrip("/") or "/"
    allowed = normalized_path == "/" or (
        normalized_path.startswith("/vacancies/")
        and normalized_path.count("/") == 2
        and normalized_path.rsplit("/", 1)[-1].lower() in LISTING_SLUGS
    )
    # Similar-vacancy cards are detail URLs observed in the current page.  We
    # permit only an explicitly marked visible URL, never a model-generated
    # arbitrary detail route.
    if not allowed and spec and spec.get("kind") == "similar" and spec.get("visible"):
        match = _DETAIL_RE.fullmatch(path)
        allowed = bool(match and match.group("category").lower() in LISTING_SLUGS)
    if not allowed:
        raise ValueError("Недопустимый путь источника вакансий HireHi")
    query = _canonical_query(parsed)
    _validate_visible_query(parsed)
    return urlunparse(("https", host, normalized_path, "", query, ""))


def validate_page_number(value: object) -> int:
    """Validate the scheduler cursor before any browser navigation occurs."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_PAGE:
        raise ValueError("Недопустимый курсор страницы HireHi")
    return value


def normalize_filters(filters: dict | None) -> list[tuple[str, str]]:
    """Validate planner filter JSON and return deterministic query pairs."""
    if filters is None:
        return []
    if not isinstance(filters, dict):
        raise ValueError("Фильтры HireHi должны быть JSON-словарём")
    pairs: list[tuple[str, str]] = []
    salary_from, salary_to = filters.get("salary_from"), filters.get("salary_to")
    if "salary_from" in filters or "salary_to" in filters:
        if salary_from is None and salary_to is None:
            raise ValueError("Задайте хотя бы одну границу зарплаты HireHi")
        for key, value in (("salary_from", salary_from), ("salary_to", salary_to)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > FILTER_SALARY_MAX:
                raise ValueError(f"Недопустимое значение фильтра HireHi: {key}")
        if salary_from is not None and salary_to is not None and salary_from > salary_to:
            raise ValueError("Нижняя граница зарплаты HireHi выше верхней")
        pairs.append(("salary", f"range:{salary_from if salary_from is not None else ''}:{salary_to if salary_to is not None else ''}"))
    for key, value in filters.items():
        if key in {"salary_from", "salary_to"}:
            continue
        if key == "page" or key not in {"level", "format", "english", "direct_contact", "region"}:
            raise ValueError(f"Недопустимый фильтр HireHi: {key}")
        if key in {"level", "format", "direct_contact", "region"}:
            if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, str) for item in value):
                raise ValueError(f"Фильтр HireHi {key} должен быть списком")
            allowed = {"level": FILTER_LEVELS, "format": FILTER_FORMATS, "direct_contact": FILTER_CONTACT, "region": FILTER_REGIONS}[key]
            if any(item not in allowed for item in value):
                raise ValueError(f"Недопустимое значение фильтра HireHi: {key}")
            pairs.extend((key, item) for item in sorted(set(value)))
        elif key == "english":
            if not isinstance(value, str) or value not in FILTER_ENGLISH:
                raise ValueError("Недопустимое значение фильтра HireHi: english")
            pairs.append((key, value))
        else:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > FILTER_SALARY_MAX:
                raise ValueError(f"Недопустимое значение фильтра HireHi: {key}")
            pairs.append((key, str(value)))
    return sorted(pairs)


def _validate_visible_query(parsed) -> None:
    """Apply value checks to known filter params while allowing UI-only geo keys."""
    grouped: dict[str, list[str]] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        grouped.setdefault(key, []).append(value)
    for key, values in grouped.items():
        if key in {"level", "format", "direct_contact"}:
            allowed = {"level": FILTER_LEVELS, "format": FILTER_FORMATS, "direct_contact": FILTER_CONTACT}[key]
            if any(value not in allowed for value in values):
                raise ValueError(f"Недопустимое значение фильтра HireHi: {key}")
        elif key == "english" and any(value not in FILTER_ENGLISH for value in values):
            raise ValueError("Недопустимое значение фильтра HireHi: english")
        elif key == "region":
            if any(value not in FILTER_REGIONS for value in values):
                raise ValueError("Недопустимое значение фильтра HireHi: region")
        elif key == "salary":
            if len(values) != 1 or not re.fullmatch(r"range:(\d*):(\d*)", values[0]):
                raise ValueError("Недопустимое значение фильтра HireHi: salary")
            match = re.fullmatch(r"range:(\d*):(\d*)", values[0])
            lower, upper = match.groups()
            if not lower and not upper or (lower and int(lower) > FILTER_SALARY_MAX) or (upper and int(upper) > FILTER_SALARY_MAX) or (lower and upper and int(lower) > int(upper)):
                raise ValueError("Недопустимое значение фильтра HireHi: salary")


def _normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).replace("_", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:200]


def query_spec(adapter, query: str, cluster: str = "", filters: dict | None = None) -> dict:
    query = _normalize_query(query)
    params = ([ ("search", query) ] if query else []) + normalize_filters(filters)
    url = urlunparse(urlparse(adapter.home_url)._replace(query=urlencode(params, doseq=True), fragment=""))
    return {"url": validate_url(adapter, url), "kind": "query" if query else "coverage", "cluster": cluster or query}


def _listing_spec(adapter, slug: str, kind: str, cluster: str = "", filters: dict | None = None) -> dict:
    slug = str(slug).strip().lower().strip("/")
    if slug not in LISTING_SLUGS:
        raise ValueError(f"Неизвестная специализация HireHi: {slug}")
    params = []
    params.extend(normalize_filters(filters))
    url = f"{adapter.home_url.rstrip('/')}/vacancies/{slug}"
    if params:
        url += "?" + urlencode(sorted(params), doseq=True)
    return {"url": validate_url(adapter, url), "kind": kind, "cluster": cluster or slug}


def specialization_spec(adapter, slug: str, filters: dict | None = None, cluster: str = "") -> dict:
    return _listing_spec(adapter, slug, "specialization", cluster, filters)


def category_spec(adapter, slug: str, filters: dict | None = None, cluster: str = "") -> dict:
    return _listing_spec(adapter, slug, "category", cluster, filters)


def coverage_spec(adapter, filters: dict | None = None, cluster: str = "") -> dict:
    query = urlencode(normalize_filters(filters), doseq=True)
    url = urlunparse(urlparse(adapter.home_url)._replace(query=query, fragment=""))
    return {"url": validate_url(adapter, url), "kind": "coverage", "cluster": cluster or "home"}


def pro_spec(adapter) -> dict:
    return {"url": validate_url(adapter, adapter.home_url), "kind": "recommendations", "cluster": "pro", "pro_enabled": True}


async def _is_visible(item) -> bool:
    try:
        result = item.is_visible() if hasattr(item, "is_visible") else True
        return bool(await result) if hasattr(result, "__await__") else bool(result)
    except Exception:
        return False


async def _text(item, selector: str | None = None) -> str:
    try:
        target = item.locator(selector).first if selector else item
        if selector and not await _is_visible(target):
            return ""
        value = await target.inner_text()
        return str(value or "").strip()
    except Exception:
        return ""


async def _attribute(item, name: str) -> str:
    try:
        return (await item.get_attribute(name)) or ""
    except Exception:
        return ""


def _vacancy_url(adapter, href: str, base: str) -> tuple[str, str] | None:
    url = urljoin(base, href or "")
    parsed = urlparse(url)
    match = _DETAIL_RE.fullmatch(parsed.path)
    try:
        explicit_port = parsed.port is not None
    except ValueError:
        return None
    if (
        parsed.hostname not in adapter.allowed_domains or parsed.scheme != "https" or not match
        or match.group("category").lower() not in LISTING_SLUGS
        or parsed.username or parsed.password or explicit_port or parsed.fragment or parsed.query
    ):
        return None
    return urlunparse(parsed._replace(fragment="")), match.group("id")


async def _anchor_for(node):
    try:
        links = node.locator(locators.CARD_LINKS)
        for index in range(await links.count()):
            link = links.nth(index)
            if await _is_visible(link):
                return link
    except Exception:
        return None
    return node


async def _card(adapter, node, base: str) -> tuple[JobRef, dict] | None:
    link = await _anchor_for(node)
    href = await _attribute(link, "href")
    resolved = _vacancy_url(adapter, href, base)
    if not resolved:
        return None
    url, external_id = resolved
    title = await _text(node, locators.CARD_TITLE)
    if not title:
        title = await _text(link)
    company = await _text(node, locators.CARD_COMPANY) or None
    salary = await _text(node, locators.CARD_SALARY) or None
    grade = await _text(node, locators.CARD_GRADE) or None
    location = await _text(node, locators.CARD_LOCATION) or None
    work_format = await _text(node, locators.CARD_FORMAT) or None
    # Accessible descriptions are the stable fallback on the live card UI.
    description = await _attribute(node, "aria-label") or await _attribute(link, "aria-label")
    if description:
        chunks = [x.strip() for x in re.split(r"[|·•\n]+", description) if x.strip()]
        if chunks:
            def clean(value: str) -> str:
                return re.sub(r"^(?:компания|company)\s*[:：-]?\s*", "", value, flags=re.I).strip()

            if not company and len(chunks) > 1:
                company = clean(chunks[0])
            for chunk in chunks:
                lower = chunk.casefold()
                if not company and re.match(r"^(?:компания|company)\b", chunk, re.I):
                    company = clean(chunk)
                if not salary and re.search(r"(?:₽|руб\.?|€|\$|зарплат)", chunk, re.I):
                    salary = chunk
                if not grade and lower in {"intern", "junior", "middle", "senior", "lead", "head"}:
                    grade = chunk
                if not work_format and re.search(r"remote|удал[её]н|офис|office|гибрид|hybrid", lower):
                    work_format = chunk
            if not title:
                candidates = [
                    chunk for chunk in chunks
                    if chunk != company
                    and not re.match(r"^(?:компания|company)\b", chunk, re.I)
                    and not re.search(r"(?:₽|руб\.?|€|\$|зарплат|remote|удал[её]н|офис|office|гибрид|hybrid)", chunk, re.I)
                    and lower_date(chunk) is False
                ]
                if candidates:
                    title = candidates[0]
            if not location and len(chunks) > 1:
                # Location is generally the final geographic segment.  Only
                # use this fallback when the UI did not expose a dedicated
                # location element; an unknown location must remain None.
                candidate = chunks[-1]
                if candidate != title and not re.search(r"(?:₽|руб\.?|€|\$)", candidate, re.I):
                    location = candidate
    metadata = {
        "external_id": external_id, "url": url, "title": title or None,
        "company": company, "grade": grade, "work_format": work_format,
        "location": location, "salary_text": salary,
    }
    return JobRef(external_id=external_id, url=url), metadata


def lower_date(value: str) -> bool:
    """Recognize date fragments without binding to a particular locale."""
    return bool(re.search(
        r"\b\d{1,2}[./ -]\d{1,2}(?:[./ -]\d{2,4})?\b"
        r"|\b\d+\s+(?:дн|день|дня|месяц|месяца|мес)\b"
        r"|\b(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\b",
        value, re.I,
    ))


async def card_refs(adapter, page) -> tuple[list[JobRef], dict[str, dict]]:
    refs, cards, seen = [], {}, set()
    try:
        nodes = page.locator(locators.CARDS)
        count = await nodes.count()
    except Exception:
        nodes, count = None, 0
    if count:
        for index in range(count):
            node = nodes.nth(index)
            if not await _is_visible(node):
                continue
            item = await _card(adapter, node, page.url)
            if item and item[0].external_id not in seen:
                seen.add(item[0].external_id)
                refs.append(item[0]); cards[item[0].external_id] = item[1]
    if not refs:
        links = page.locator(locators.LINKS)
        for index in range(await links.count()):
            link = links.nth(index)
            if not await _is_visible(link):
                continue
            item = await _card(adapter, link, page.url)
            if item and item[0].external_id not in seen:
                seen.add(item[0].external_id)
                refs.append(item[0]); cards[item[0].external_id] = item[1]
    return refs, cards


async def _has_next(page, page_number: int) -> bool:
    try:
        links = page.locator(locators.PAGINATION)
        for index in range(await links.count()):
            link = links.nth(index)
            if not await _is_visible(link):
                continue
            href = await _attribute(link, "href")
            candidate = dict(parse_qsl(urlparse(urljoin(page.url, href)).query)).get("page")
            if candidate and int(candidate) > page_number:
                return True
            text = (await _text(link)).casefold()
            if any(token in text for token in ("след", "далее", "next", "›", "→")):
                return True
    except Exception:
        return False
    return False


async def visible_sources(adapter, page, *, context: str = "listing") -> list[dict]:
    sources, seen = [], set()
    links = page.locator(locators.DISCOVERY_LINKS)
    for index in range(await links.count()):
        link = links.nth(index)
        if not await _is_visible(link):
            continue
        href = await _attribute(link, "href")
        parsed = urlparse(urljoin(page.url, href))
        path = (parsed.path.rstrip("/") or "/")
        if not path.startswith("/vacancies/") or path.count("/") != 2:
            continue
        slug = path.rsplit("/", 1)[-1].lower()
        if slug not in LISTING_SLUGS:
            continue
        kind = "category" if slug in CATEGORY_SLUGS else "specialization"
        try:
            canonical = validate_url(adapter, urljoin(page.url, href))
        except ValueError:
            continue
        if canonical not in seen:
            seen.add(canonical)
            sources.append({"url": canonical, "kind": kind, "cluster": slug})
    # A detail page has no separate "similar" listing URL: its visible cards
    # are detail links.  Keep those links as explicitly observed source specs
    # so the adaptive engine can spend a bounded exploration turn on them.
    try:
        containers = page.locator(locators.RELATED_VACANCIES)
        container_count = await containers.count()
    except Exception:
        container_count = 0
    for container_index in range(container_count):
        container = containers.nth(container_index)
        if not await _is_visible(container):
            continue
        links = container.locator(locators.CARD_LINKS)
        for link_index in range(await links.count()):
            link = links.nth(link_index)
            if not await _is_visible(link):
                continue
            href = await _attribute(link, "href")
            resolved = _vacancy_url(adapter, href, page.url)
            if not resolved:
                continue
            detail_url, _ = resolved
            if detail_url in seen:
                continue
            seen.add(detail_url)
            sources.append({
                "url": detail_url, "kind": "similar", "cluster": "similar", "visible": True,
            })
    return sources


async def related_refs(adapter, page) -> list[JobRef]:
    result, seen = [], set()
    try:
        containers = page.locator(locators.RELATED_VACANCIES)
        count = await containers.count()
    except Exception:
        count = 0
    for index in range(count):
        container = containers.nth(index)
        if not await _is_visible(container):
            continue
        links = container.locator(locators.CARD_LINKS)
        for link_index in range(await links.count()):
            link = links.nth(link_index)
            if not await _is_visible(link):
                continue
            resolved = _vacancy_url(adapter, await _attribute(link, "href"), page.url)
            if resolved and resolved[1] not in seen:
                seen.add(resolved[1]); result.append(JobRef(external_id=resolved[1], url=resolved[0]))
    return result


async def read_page(adapter, page, spec: dict, page_number: int) -> dict:
    page_number = validate_page_number(page_number)
    if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
        raise ValueError("Недопустимый источник поиска HireHi")
    canonical = validate_url(adapter, spec["url"], spec)
    spec = {**spec, "url": canonical}
    parsed = urlparse(canonical)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "page"]
    if page_number > 0:
        query.append(("page", str(page_number + 1)))
    target = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    await page.goto(target, wait_until="domcontentloaded", timeout=60_000)
    wait = getattr(page, "wait_for_timeout", None)
    if wait:
        await wait(500)
    # Match Me is a UI filter, not a query parameter.  It must be toggled only
    # after the safe listing navigation, otherwise goto discards the filter.
    if spec.get("kind") == "recommendations" and spec.get("pro_enabled"):
        available = await adapter.ensure_pro_filter(page)
        if not available:
            return {
                "refs": [], "cards": {}, "terminal": True, "sources": [],
                "unavailable": True, "repeated": False,
            }
        if wait:
            await wait(500)
    refs, cards = await card_refs(adapter, page)
    signature = tuple(ref.external_id for ref in refs)
    repeated = bool(signature and signature == getattr(adapter, "_last_search_page_signature", None))
    adapter._last_search_page_signature = signature or None
    # A stale DOM snapshot is a recoverable navigation problem.  It must be
    # surfaced to the scheduler and cannot masquerade as a legitimate last
    # page merely because the stale snapshot has no next-link.
    terminal = False if repeated else (not refs or not await _has_next(page, page_number + 1))
    return {"refs": refs, "cards": cards, "terminal": terminal, "sources": await visible_sources(adapter, page, context=spec.get("kind", "listing")), "repeated": repeated, "unavailable": False}
