"""Bound external operations; let the workflow own durable retries and state."""
from __future__ import annotations

import asyncio

from backend.services.search_metrics import discovery, measure


class CaptchaRequired(RuntimeError):
    pass


class RecoverableFailure(RuntimeError):
    pass


class AuthenticationPending(RecoverableFailure):
    pass


class RecoveryAdapter:
    """Preserve adapter cursors while bounding every external operation."""

    timeout = 120

    def __init__(self, adapter):
        self.adapter = adapter

    def __getattr__(self, name):
        value = getattr(self.adapter, name)
        if not asyncio.iscoroutinefunction(value):
            return value

        async def invoke(*args, **kwargs):
            try:
                with measure(f"browser.{name}"):
                    result = await asyncio.wait_for(value(*args, **kwargs), timeout=self.timeout)
            except Exception:
                # A CAPTCHA may replace any page, including a response dialog.
                if args and name != "detect_blockers":
                    await self._captcha(args[0])
                raise
            if args and (name.startswith("collect_") or name in {
                "open_application", "fill_application", "submit_application", "verify_submission",
            }):
                await self._captcha(args[0])
            if name in {"collect_job_refs", "collect_more_job_refs"}:
                discovery(self.adapter, result)
            return result

        return invoke

    async def _captcha(self, page):
        try:
            blockers = await asyncio.wait_for(self.adapter.detect_blockers(page), timeout=10)
        except Exception:
            return
        captcha = next((b for b in blockers if b.kind == "captcha"), None)
        if captcha:
            raise CaptchaRequired(captcha.message)
