"""Exceptions shared between adapters and orchestration wrappers."""


class CaptchaRequired(RuntimeError):
    """The browser is blocked on a CAPTCHA that needs user interaction."""


class JobDescriptionUnavailable(ValueError):
    """A vacancy description could not be read from the site."""

    DEFAULT_MESSAGE = "Описание вакансии отсутствует или не удалось его прочитать"

    def __init__(
        self,
        message: str = DEFAULT_MESSAGE,
        *,
        title: str | None = None,
        company: str | None = None,
        diagnostics: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.title = title
        self.company = company
        # Callers may attach bounded, explicitly sanitized DOM diagnostics.
        # Keep the payload structured so it can be recorded without exposing
        # page text, query strings, or browser state.
        self.diagnostics = diagnostics or {}

