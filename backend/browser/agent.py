from pydantic import BaseModel

from .tools import ALLOWED_ACTIONS


class BrowserAction(BaseModel):
    action: str
    role: str | None = None
    name: str | None = None
    value: str | None = None

    def checked(self) -> "BrowserAction":
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError("Action is outside the browser allowlist")
        return self

