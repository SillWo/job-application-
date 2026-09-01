from types import SimpleNamespace

import pytest

from backend.adapters.registry import AdapterRegistry
from backend.api import router


def test_registry_exposes_zarplata_as_fresh_instances():
    registry = AdapterRegistry()
    manifests = registry.manifests()
    assert any(item["site_id"] == "zarplata" for item in manifests)
    first, second = registry.get("zarplata"), registry.get("zarplata")
    assert first is not second
    assert first.site_id == second.site_id == "zarplata"


class _DB:
    def __init__(self, item): self.item = item
    def get(self, model, item_id): return self.item


@pytest.mark.asyncio
async def test_open_browser_uses_zarplata_target_and_message(monkeypatch):
    item = SimpleNamespace(id=1, adapter_id="zarplata")
    adapter = SimpleNamespace(site_id="zarplata", allowed_domains=("zarplata.ru",), display_name="Zarplata.ru")
    calls = []

    class Executor:
        def __init__(self, *args, **kwargs): pass
        async def start(self): self.page = object()
        async def execute(self, action, **kwargs): calls.append((action, kwargs["url"]))

    monkeypatch.setattr(router, "get_browser", lambda _: None)
    monkeypatch.setattr(router.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(router, "acquire_browser_lease", lambda *args: True)
    monkeypatch.setattr(router, "set_browser", lambda *args: None)
    monkeypatch.setattr(router, "BrowserExecutor", Executor)
    result = await router.open_session_browser(1, _DB(item))
    assert calls == [("navigate", "https://zarplata.ru/")]
    assert "Zarplata.ru" in result["message"]


@pytest.mark.asyncio
async def test_login_status_is_read_only_and_sanitizes_url(monkeypatch):
    item = SimpleNamespace(id=1, adapter_id="zarplata")
    page = SimpleNamespace(
        url="https://krasnoyarsk.zarplata.ru/applicant/resumes?token=secret#section"
    )
    executor = SimpleNamespace(page=page)

    class Adapter:
        display_name = "Zarplata.ru"
        allowed_domains = ("zarplata.ru", "krasnoyarsk.zarplata.ru")

        async def get_login_state(self, checked_page):
            assert checked_page is page
            return SimpleNamespace(authenticated=True, message="Вход выполнен")

    monkeypatch.setattr(router, "get_browser", lambda _session_id: executor)
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter_id: Adapter())
    monkeypatch.setattr(
        router.workflow_manager,
        "launch",
        lambda _session_id: pytest.fail("read-only login status launched workflow"),
    )

    result = await router.session_browser_login_status(1, _DB(item))

    assert result == {
        "authenticated": True,
        "message": "Вход выполнен",
        "url": "https://krasnoyarsk.zarplata.ru/applicant/resumes",
    }


@pytest.mark.asyncio
async def test_login_status_hides_external_page_url(monkeypatch):
    item = SimpleNamespace(id=1, adapter_id="zarplata")
    executor = SimpleNamespace(page=SimpleNamespace(url="https://evil.example/path?x=1"))
    adapter = SimpleNamespace(
        display_name="Zarplata.ru",
        allowed_domains=("zarplata.ru",),
        get_login_state=lambda _page: _async_login(False),
    )
    monkeypatch.setattr(router, "get_browser", lambda _session_id: executor)
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter_id: adapter)

    result = await router.session_browser_login_status(1, _DB(item))

    assert result["authenticated"] is False
    assert result["url"] is None


@pytest.mark.asyncio
async def test_login_status_requires_open_browser(monkeypatch):
    item = SimpleNamespace(id=1, adapter_id="zarplata")
    adapter = SimpleNamespace(display_name="Zarplata.ru")
    monkeypatch.setattr(router, "get_browser", lambda _session_id: None)
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter_id: adapter)

    with pytest.raises(router.HTTPException) as exc_info:
        await router.session_browser_login_status(1, _DB(item))

    assert exc_info.value.status_code == 400


async def _async_login(authenticated):
    return SimpleNamespace(
        authenticated=authenticated,
        message="Вход выполнен" if authenticated else "Войдите вручную",
    )
