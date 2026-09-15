from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from backend.config import settings

from .pointer import BezierPointer
from .tools import ALLOWED_ACTIONS


class BrowserExecutor:
    def __init__(
        self, site_id: str, allowed_domains: tuple[str, ...],
        headless: bool | None = None, *, navigation_hop_limit: int | None = None,
    ) -> None:
        self.site_id = site_id
        self.allowed_domains = allowed_domains
        self.headless = settings.browser_headless if headless is None else headless
        self.pointer = BezierPointer()
        self.navigation_hop_limit = navigation_hop_limit
        self._navigation_hops = 0
        self._playwright = self.context = self.page = None

    async def start(self):
        profile = Path(f"data/browser-profiles/{self.site_id}").resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            str(profile), headless=self.headless,
            user_agent=settings.browser_user_agent,
            viewport={"width": 1280, "height": 720},
        )
        # Every main-frame navigation, including redirects initiated by a
        # direct adapter page.goto(), is checked here.  Subresources are left
        # alone because sites commonly serve assets from a CDN.
        route = getattr(self.context, "route", None)
        if route is not None:
            await route("**/*", self._guard_route)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page._jao_validate_navigation = self.validate_navigation_url
        return self.page

    def _allowed_hostname(self, hostname: str | None) -> bool:
        host = (hostname or "").casefold().rstrip(".")
        allowed = {str(value).casefold().rstrip(".") for value in self.allowed_domains}
        if host in allowed:
            return True
        # HH serves resume and vacancy pages from one regional subdomain.  A
        # single label under hh.ru is allowed; lookalikes such as hh.ru.evil
        # remain outside this boundary.
        return self.site_id in {"hh", "zarplata"} and host.endswith(
            f".{self.site_id}.ru"
        ) and host.count(".") == 2

    def _is_loopback_fixture(self, hostname: str | None) -> bool:
        host = (hostname or "").casefold().rstrip(".")
        allowed = {str(value).casefold().rstrip(".") for value in self.allowed_domains}
        return host in {"localhost", "127.0.0.1", "::1"} and host in allowed

    def validate_navigation_url(self, url: str) -> str:
        parsed = urlparse(str(url or ""))
        # Plain HTTP is available only to explicitly allowlisted local fixture
        # servers used by tests.  Production site navigation remains HTTPS
        # only, even when a site adapter happens to allow a broad host.
        if parsed.scheme == "http":
            if not self._is_loopback_fixture(parsed.hostname) or parsed.username or parsed.password:
                raise ValueError("Переход на неразрешённый адрес остановлен")
            if parsed.port is None or not 1 <= parsed.port <= 65535:
                raise ValueError("Переход на неразрешённый адрес остановлен")
            return url
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Переход на неразрешённый адрес остановлен")
        if not self._allowed_hostname(parsed.hostname):
            raise ValueError("Переход на неразрешённый домен остановлен")
        return url

    async def _guard_route(self, route) -> None:
        request = route.request
        try:
            is_navigation = getattr(request, "is_navigation_request", None)
            navigation_request = bool(is_navigation and is_navigation())
        except Exception:
            # A request whose navigation state cannot be read is not safe to
            # continue blindly.  Route errors (for example, a closed page)
            # are swallowed by _safe_route_call below, but the request is
            # still handled exactly once.
            await self._safe_route_call(route, "abort", error_code="blockedbyclient")
            return

        # Playwright's Service Worker requests have no associated frame.  Do
        # not even read request.frame for subresources/non-navigation calls;
        # they are allowed to continue and commonly include site assets.
        if not navigation_request:
            await self._safe_route_call(route, "continue_")
            return

        try:
            frame = request.frame
        except Exception:
            # Main-frame navigations must fail closed when their frame cannot
            # be identified.  In particular, never turn a frame lookup error
            # into an unvalidated navigation.
            await self._safe_route_call(route, "abort", error_code="blockedbyclient")
            return

        try:
            main_frame = self.page is None or frame == self.page.main_frame
        except Exception:
            await self._safe_route_call(route, "abort", error_code="blockedbyclient")
            return
        if main_frame:
            self._navigation_hops += 1
            if (
                self.navigation_hop_limit is not None
                and self._navigation_hops > self.navigation_hop_limit
            ):
                await self._safe_route_call(route, "abort", error_code="blockedbyclient")
                return
            try:
                url = request.url
                self.validate_navigation_url(url)
            except Exception:
                await self._safe_route_call(route, "abort", error_code="blockedbyclient")
                return
        await self._safe_route_call(route, "continue_")

    @staticmethod
    async def _safe_route_call(route, method: str, **kwargs) -> None:
        """Handle a route once while tolerating closed/already-handled pages."""
        try:
            action = getattr(route, method)
            result = action(**kwargs)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            # Playwright can reject an action when the page/context closes or
            # another internal owner has already handled the route.  There is
            # no useful recovery at this boundary; importantly, validation
            # errors above are handled before this narrow suppression point.
            return

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()

    async def execute(self, action: str, **kwargs):
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Запрещённое действие Browser Agent: {action}")
        if action == "navigate":
            self.validate_navigation_url(kwargs["url"])
            return await self.page.goto(
                kwargs["url"],
                wait_until=kwargs.get("wait_until", "commit"),
                timeout=min(int(kwargs.get("timeout_ms", 60_000)), 60_000),
            )
        locator = self.page.get_by_role(kwargs["role"], name=kwargs.get("name")) if "role" in kwargs else None
        if action == "click":
            await self.pointer.move_to_locator(self.page, locator)
            return await locator.click()
        if action == "fill":
            return await locator.fill(kwargs["value"])
        if action == "go_back":
            return await self.page.go_back()
        if action == "scroll":
            return await self.page.mouse.wheel(0, kwargs.get("delta", 600))
        if action == "wait":
            return await self.page.wait_for_timeout(min(kwargs.get("ms", 500), 5000))
        if action == "extract_text":
            return await locator.inner_text() if locator else await self.page.locator("body").inner_text()
        raise ValueError(f"Действие {action} доступно только через site adapter")
