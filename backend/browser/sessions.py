from .executor import BrowserExecutor

open_browsers: dict[int, BrowserExecutor] = {}
# A persistent profile is shared by all sessions for a site.  Keep the lease
# separate from ``open_browsers`` so callers can still retrieve each session's
# own executor while preventing concurrent contexts for the same profile.
browser_leases: dict[str, int] = {}


def get_browser(session_id: int) -> BrowserExecutor | None:
    executor = open_browsers.get(session_id)
    if executor and executor.page:
        return executor
    if executor:
        open_browsers.pop(session_id, None)
        release_browser_lease(session_id, executor.site_id)
    return None


def set_browser(session_id: int, executor: BrowserExecutor) -> None:
    open_browsers[session_id] = executor


def acquire_browser_lease(session_id: int, site_id: str) -> bool:
    """Reserve a site's persistent profile for *session_id*.

    Re-acquiring a lease owned by the same session is idempotent; another
    session must wait until the owner releases it.
    """
    owner = browser_leases.get(site_id)
    if owner is not None and owner != session_id:
        return False
    browser_leases[site_id] = session_id
    return True


def release_browser_lease(session_id: int, site_id: str) -> None:
    if browser_leases.get(site_id) == session_id:
        browser_leases.pop(site_id, None)


async def close_browser(session_id: int) -> None:
    """Close and forget a session browser, releasing its profile lease."""
    executor = open_browsers.pop(session_id, None)
    if not executor:
        return
    try:
        await executor.close()
    finally:
        release_browser_lease(session_id, executor.site_id)
