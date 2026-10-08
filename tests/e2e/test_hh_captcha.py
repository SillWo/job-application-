"""Visible HH captcha handling against synthetic Chromium pages only."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.adapters.base.errors import CaptchaRequired
from backend.adapters.hh import locators
from backend.adapters.hh.adapter import HHAdapter
from backend.browser.executor import BrowserExecutor
from backend.orchestrator.recovery import RecoveryAdapter


@pytest.mark.e2e
async def test_modal_iframe_pauses_submission_then_reconciles_without_second_click(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("captcha-modal-test", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.set_content("""
            <form>
              <button data-qa="vacancy-response-submit-popup" type="button"
                onclick="document.querySelector('[role=dialog]').hidden = false;
                         document.querySelector('[data-qa=vacancy-response-link-view-topic]').hidden = false;
                         this.hidden = true;
                         document.querySelector('#click-count').textContent =
                           Number(document.querySelector('#click-count').textContent) + 1">
                Откликнуться
              </button>
            </form>
            <span id="click-count">0</span>
            <a data-qa="vacancy-response-link-view-topic" hidden>Мои отклики</a>
            <section role="dialog" hidden>
              <iframe title="SmartCaptcha challenge" srcdoc=""></iframe>
              <button id="clear" type="button" onclick="this.parentElement.hidden = true">Проверка пройдена</button>
            </section>
        """)
        adapter = HHAdapter()
        adapter.allowed_domains = ("",)
        recovery = RecoveryAdapter(adapter)

        with pytest.raises(CaptchaRequired):
            await recovery.submit_application(page)
        assert not page.is_closed()
        assert await page.locator(locators.ALREADY_APPLIED).count() == 1
        assert not getattr(adapter, "_hh_cv_submission_confirmed", False)

        await page.locator("#clear").click()
        reconciled = await recovery.verify_submission(page, just_submitted=True)
        assert reconciled.status == "submitted"
        assert adapter._hh_cv_submission_confirmed
        assert await page.locator(locators.RESPONSE_SUBMIT).count() == 1
        assert await page.locator("#click-count").inner_text() == "1"
    finally:
        await executor.close()


@pytest.mark.e2e
async def test_hidden_widget_and_vacancy_text_do_not_pause_but_visible_challenge_does(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("captcha-detection-test", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.set_content("""
          <h1 data-qa="vacancy-title">Frontend engineer</h1>
          <div data-qa="vacancy-description">Опыт интеграции с captcha сервисом будет плюсом.</div>
          <div class="smartcaptcha solved" data-state="solved">Captcha</div>
        """)
        adapter = HHAdapter()
        assert await adapter.detect_blockers(page) == []

        await page.set_content("""
          <h1 data-qa="vacancy-title">Frontend engineer</h1>
          <div data-qa="vacancy-description">Опыт интеграции с captcha сервисом будет плюсом.</div>
          <div class="smartcaptcha incomplete">Challenge</div>
        """)
        blockers = await adapter.detect_blockers(page)
        assert [blocker.kind for blocker in blockers] == ["captcha"]

        await page.set_content("""
          <h1 data-qa="vacancy-title">Frontend engineer</h1>
          <div data-qa="vacancy-description">Ordinary job content.</div>
          <div role="dialog">
            <p>Подтвердите, что вы не робот</p>
            <img src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" alt="">
            <input type="text" aria-label="Ответ">
          </div>
        """)
        blockers = await adapter.detect_blockers(page)
        assert [blocker.kind for blocker in blockers] == ["captcha"]

        await page.set_content("<main><h1>Captcha</h1><p>Подтвердите, что вы не робот</p></main>")
        blockers = await adapter.detect_blockers(page)
        assert [blocker.kind for blocker in blockers] == ["captcha"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if "mode=challenge" in self.path:
                    body = "<main><p>Подтвердите, что вы не робот</p></main>"
                elif "mode=listing" in self.path:
                    body = ("<a data-qa='serp-item__title' href='/vacancy/1'>Security engineer</a>"
                            "<div>Vacancy requires a security check and CAPTCHA experience.</div>")
                else:
                    body = ("<h1 data-qa='vacancy-title'>Security engineer</h1>"
                            "<div data-qa='vacancy-description'>The role includes a security check and "
                            "captcha integration. A security check may be required.</div>")
                content = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy/1?from=captcha")
            assert await adapter.detect_blockers(page) == []
            await page.goto(f"http://127.0.0.1:{server.server_port}/search/vacancy?mode=listing")
            assert await adapter.detect_blockers(page) == []
            await page.goto(f"http://127.0.0.1:{server.server_port}/search/vacancy?mode=challenge")
            blockers = await adapter.detect_blockers(page)
            assert [blocker.kind for blocker in blockers] == ["captcha"]
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
    finally:
        await executor.close()

