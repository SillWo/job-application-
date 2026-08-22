import pytest

from backend.browser.sessions import (
    acquire_browser_lease,
    browser_leases,
    close_browser,
    get_browser,
    open_browsers,
    release_browser_lease,
    set_browser,
)


class FakeExecutor:
    def __init__(self, site_id: str, page=True):
        self.site_id = site_id
        self.page = page
        self.closed = False

    async def close(self):
        self.closed = True
        self.page = None


@pytest.fixture(autouse=True)
def reset_browser_state():
    open_browsers.clear()
    browser_leases.clear()
    yield
    open_browsers.clear()
    browser_leases.clear()


def test_same_site_blocked_but_cross_site_allowed():
    assert acquire_browser_lease(1, "hh")
    assert not acquire_browser_lease(2, "hh")
    assert acquire_browser_lease(2, "hirehi")


@pytest.mark.asyncio
async def test_close_releases_lease_and_removes_exact_session():
    first = FakeExecutor("hh")
    second = FakeExecutor("hh")
    set_browser(1, first)
    set_browser(2, second)
    acquire_browser_lease(1, "hh")
    assert get_browser(1) is first
    assert get_browser(2) is second
    assert get_browser(99) is None
    await close_browser(1)
    assert first.closed
    assert get_browser(1) is None
    assert acquire_browser_lease(2, "hh")


def test_release_only_owner_can_release():
    assert acquire_browser_lease(1, "hh")
    release_browser_lease(2, "hh")
    assert not acquire_browser_lease(2, "hh")
    release_browser_lease(1, "hh")
    assert acquire_browser_lease(2, "hh")
