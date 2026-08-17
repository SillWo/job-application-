from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from backend.config import settings

from .pointer import BezierPointer
from .tools import ALLOWED_ACTIONS


class BrowserExecutor:
    def __init__(self, site_id: str, allowed_domains: tuple[str, ...], headless: bool | None = None) -> None:
        self.site_id = site_id
        self.allowed_domains = allowed_domains
        self.headless = settings.browser_headless if headless is None else headless
        self.pointer = BezierPointer()
        self._playwright = self.context = self.page = None

    async def start(self):
        profile = Path(f"data/browser-profiles/{self.site_id}").resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(str(profile), headless=self.headless, viewport={"width": 1280, "height": 720})
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self.page

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()

    async def execute(self, action: str, **kwargs):
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Запрещённое действие Browser Agent: {action}")
        if action == "navigate":
            hostname = urlparse(kwargs["url"]).hostname
            if hostname not in self.allowed_domains:
                raise ValueError("Переход на неразрешённый домен остановлен")
            return await self.page.goto(
                kwargs["url"],
                wait_until=kwargs.get("wait_until", "commit"),
                timeout=min(int(kwargs.get("timeout_ms", 15_000)), 30_000),
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
