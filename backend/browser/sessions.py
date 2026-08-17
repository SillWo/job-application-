from .executor import BrowserExecutor

open_browsers: dict[int, BrowserExecutor] = {}


def get_browser(session_id: int) -> BrowserExecutor | None:
    executor = open_browsers.get(session_id)
    if executor and executor.page:
        return executor
    return None


def set_browser(session_id: int, executor: BrowserExecutor) -> None:
    open_browsers[session_id] = executor
