import pytest

from backend.adapters.base.errors import CaptchaRequired
from backend.orchestrator.recovery import RecoveryAdapter


@pytest.mark.asyncio
async def test_recovery_checks_captcha_before_application_mutation():
    class Adapter:
        called = False

        async def detect_blockers(self, _page):
            from backend.adapters.base.protocol import Blocker

            return [Blocker(kind="captcha", message="challenge")]

        async def fill_application(self, _page, _plan):
            self.called = True

    adapter = Adapter()
    with pytest.raises(CaptchaRequired, match="challenge"):
        await RecoveryAdapter(adapter).fill_application(object(), object())
    assert not adapter.called

