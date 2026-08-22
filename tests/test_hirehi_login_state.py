from __future__ import annotations

import pytest

from backend.adapters.hirehi.adapter import HireHiAdapter


class Node:
    def __init__(self, visible: bool, text: str = ""):
        self.visible = visible
        self.text = text
        self.first = self

    async def count(self):
        return 1

    async def is_visible(self):
        return self.visible

    async def inner_text(self):
        return self.text


class ProtectedProfilePage:
    url = "https://hirehi.ru/management/vacancy-79859"

    def __init__(self):
        self.goto_urls: list[str] = []

    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.url = url

    async def wait_for_timeout(self, value):
        return None

    def locator(self, selector):
        if selector == "main h1, main h2, [data-testid='profile-name'], [data-testid='profile-page']":
            return Node(True, "Профиль")
        return Node(False)


class LoginRedirectPage(ProtectedProfilePage):
    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.url = "https://hirehi.ru/login?next=/profile"

    def locator(self, selector):
        if selector == "input[type='password'], form[action*='login'], [data-testid='login-form']":
            return Node(True)
        return Node(False)


@pytest.mark.asyncio
async def test_login_state_uses_visible_protected_profile_after_hidden_menu():
    state = await HireHiAdapter().get_login_state(ProtectedProfilePage())

    assert state.authenticated is True


@pytest.mark.asyncio
async def test_login_state_rejects_redirect_to_login_form():
    state = await HireHiAdapter().get_login_state(LoginRedirectPage())

    assert state.authenticated is False
