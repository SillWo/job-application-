from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.browser.executor import BrowserExecutor


class _Route:
    def __init__(self, request, *, fail_action: str | None = None):
        self.request = request
        self.fail_action = fail_action
        self.continued = 0
        self.aborted: list[dict] = []

    async def continue_(self):
        if self.fail_action == "continue_":
            raise RuntimeError("closed route")
        self.continued += 1

    async def abort(self, **kwargs):
        if self.fail_action == "abort":
            raise RuntimeError("closed route")
        self.aborted.append(kwargs)


class _Request:
    def __init__(self, url: str, frame, navigation: bool):
        self.url = url
        self._frame = frame
        self.navigation = navigation

    def is_navigation_request(self):
        return self.navigation

    @property
    def frame(self):
        return self._frame


class _ServiceWorkerRequest(_Request):
    @property
    def frame(self):
        raise RuntimeError("Service Worker requests do not have an associated frame")


@pytest.mark.asyncio
async def test_service_worker_route_continues_without_reading_frame():
    executor = BrowserExecutor("zarplata", ("zarplata.ru",))
    executor.page = SimpleNamespace(main_frame=object())
    route = _Route(_ServiceWorkerRequest("https://cdn.zarplata.ru/app.js", None, False))

    await executor._guard_route(route)

    assert route.continued == 1
    assert route.aborted == []


@pytest.mark.asyncio
async def test_disallowed_main_frame_navigation_aborts_once():
    executor = BrowserExecutor("hh", ("hh.ru",))
    main_frame = object()
    executor.page = SimpleNamespace(main_frame=main_frame)
    route = _Route(_Request("https://evil.example/", main_frame, True))

    await executor._guard_route(route)

    assert route.continued == 0
    assert route.aborted == [{"error_code": "blockedbyclient"}]


@pytest.mark.asyncio
async def test_main_frame_navigation_hop_limit_aborts_only_after_limit():
    executor = BrowserExecutor("hh", ("hh.ru",), navigation_hop_limit=1)
    main_frame = object()
    executor.page = SimpleNamespace(main_frame=main_frame)

    first = _Route(_Request("https://hh.ru/", main_frame, True))
    second = _Route(_Request("https://hh.ru/resume/abc", main_frame, True))
    await executor._guard_route(first)
    await executor._guard_route(second)

    assert first.continued == 1 and first.aborted == []
    assert second.continued == 0
    assert second.aborted == [{"error_code": "blockedbyclient"}]


@pytest.mark.asyncio
async def test_navigation_frame_errors_abort_and_closed_route_errors_are_safe():
    executor = BrowserExecutor("hh", ("hh.ru",))
    executor.page = SimpleNamespace(main_frame=object())
    frame_error = _Route(_ServiceWorkerRequest("https://hh.ru/", None, True))
    closed = _Route(
        _Request("https://evil.example/", executor.page.main_frame, True),
        fail_action="abort",
    )

    await executor._guard_route(frame_error)
    await executor._guard_route(closed)

    assert frame_error.continued == 0
    assert frame_error.aborted == [{"error_code": "blockedbyclient"}]
    assert closed.continued == 0
    assert closed.aborted == []
