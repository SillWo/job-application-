"""Visible HH navigation primitives; ranking and learning belong to the orchestrator."""
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from backend.adapters.base.protocol import JobRef

from . import locators


def validate_url(adapter, url):
    parsed = urlparse(url)
    if (parsed.hostname not in adapter.allowed_domains or parsed.username or parsed.password
            or parsed.scheme not in {"https", "http"}
            or not re.fullmatch(r"/(?:search/vacancy|applicant/resumes|resume/[\w-]+|employer/\d+)?/?", parsed.path)):
        raise ValueError("Недопустимый адрес источника вакансий")
    return url


def query_spec(adapter, query, field="name", cluster=""):
    if field not in {"name", "description"}:
        raise ValueError("Unsupported search field")
    text = adapter.normalize_search_query(query)
    params = {"only_with_salary": "false"}
    if text:
        params.update(text=text, search_field=field)
    return {"url": f"{adapter.home_url.rstrip('/')}/search/vacancy?{urlencode(params)}",
            "kind": "query" if text else "coverage", "cluster": cluster or text}


async def visible_sources(adapter, page, *, context="listing"):
    links = page.locator(locators.DISCOVERY_LINKS)
    sources, seen = [], set()
    for index in range(await links.count()):
        link = links.nth(index)
        if not await link.is_visible():
            continue
        href = await link.get_attribute("href")
        if not href:
            continue
        url = urljoin(page.url, href)
        parsed = urlparse(url)
        try:
            validate_url(adapter, url)
        except ValueError:
            continue
        query = dict(parse_qsl(parsed.query))
        text = (await link.inner_text()).casefold()
        kind = None
        if parsed.path == "/applicant/resumes":
            kind = "resume_index"
        elif parsed.path.startswith("/resume/") and context == "resume_index":
            kind = "resume"
        elif parsed.path.startswith("/employer/") and context == "relevant":
            kind = "employer"
        elif parsed.path == "/search/vacancy":
            if "resume" in query or any(word in text for word in ("подходящ", "рекоменд", "для вас")) or context in {"resume", "resume_index"} and "ваканс" in text:
                kind = "recommendations"
            elif not query.get("text") and any(k in query for k in ("area", "professional_role", "industry")):
                # Split broad coverage only by actual visible UI filter links.
                kind = "coverage"
        if kind:
            query.pop("page", None)
            url = urlunparse(parsed._replace(query=urlencode(sorted(query.items())), fragment=""))
            if url not in seen:
                seen.add(url)
                sources.append({"url": url, "kind": kind, "cluster": kind})
    return sources


async def related_refs(adapter, page):
    result, seen = [], set()
    links = page.locator(locators.RELATED_VACANCIES)
    for index in range(await links.count()):
        link = links.nth(index)
        if not await link.is_visible():
            continue
        href = await link.get_attribute("href")
        parsed = urlparse(urljoin(page.url, href or ""))
        match = re.fullmatch(r"/vacancy/(\d+)/?", parsed.path)
        if parsed.hostname in adapter.allowed_domains and parsed.scheme in {"http", "https"} and match:
            key = match.group(1)
            if key not in seen:
                seen.add(key)
                result.append(JobRef(external_id=key, url=urlunparse(parsed)))
    return result


async def read_page(adapter, page, spec, page_number):
    url = validate_url(adapter, spec["url"])
    kind = spec["kind"]
    if urlparse(url).path == "/":
        await adapter.open_search(page, {})
        refs = await adapter.collect_job_refs(page)
        sources = await visible_sources(adapter, page)
        if urlparse(page.url).path == "/search/vacancy":
            sources.append({"url": adapter._search_page_url(page.url, 0), "kind": "recommendations", "cluster": "home"})
        return {"refs": refs, "terminal": True, "sources": sources}
    if urlparse(url).path == "/search/vacancy":
        refs = await adapter._collect_search_page(page, url, page_number)
        terminal = not refs or adapter._last_page_terminal
    else:
        await page.goto(url, wait_until="commit", timeout=60_000)
        await page.wait_for_timeout(1_000)
        if kind in {"resume_index", "resume"}:
            refs, terminal = [], True
        else:
            refs = await adapter._visible_job_refs(page, timeout=6_000)
            terminal = True  # Only the visible employer snapshot; linked listings continue below.
    sources = await visible_sources(adapter, page, context=kind)
    if kind == "employer":
        # Employer pages can expose a separate full vacancy listing.
        links = page.locator(locators.DISCOVERY_LINKS)
        for index in range(await links.count()):
            link = links.nth(index)
            href = await link.get_attribute("href")
            if href and await link.is_visible():
                linked = urljoin(page.url, href)
                parsed = urlparse(linked)
                if parsed.path == "/search/vacancy" and "employer_id" in dict(parse_qsl(parsed.query)):
                    validate_url(adapter, linked)
                    sources.append({"url": linked, "kind": "employer", "cluster": spec.get("cluster", "")})
    return {"refs": refs, "terminal": terminal, "sources": sources}
