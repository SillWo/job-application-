"""Synthetic Chromium contract for Zarplata's CV and inline-letter stages."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.browser.executor import BrowserExecutor
from backend.schemas.domain import ApplicationPlan

LETTER = "Synthetic fixture cover letter"


def _server(cv_submissions, letter_submissions):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = """<!doctype html><html><body>
              <button data-qa="vacancy-response-link-top">Respond</button>
              <a data-qa="vacancy-response-link-view-topic" hidden>Topic</a>
              <form id="cv-form" hidden>
                <button data-qa="vacancy-response-submit-popup">Send response</button>
              </form>
              <script>
                const response = document.querySelector('[data-qa="vacancy-response-link-top"]');
                response.addEventListener('click', async () => {
                  await fetch('/cv', {method: 'POST', body: 'resume=selected'});
                  response.hidden = true;
                  document.querySelector('[data-qa="vacancy-response-link-view-topic"]').hidden = false;
                  const form = document.createElement('form');
                  form.id = 'letter-form';
                  form.innerHTML = '<textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>' +
                    '<button type="button" data-qa="vacancy-response-letter-submit">Send letter</button>';
                  form.querySelector('button').addEventListener('click', async () => {
                    const text = form.querySelector('textarea').value;
                    await fetch('/letter', {method: 'POST', body: new URLSearchParams({text})});
                    form.remove();
                  });
                  document.body.append(form);
                });
              </script>
            </body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = parse_qs(self.rfile.read(length).decode())
            if self.path == "/cv":
                cv_submissions.append(payload)
            elif self.path == "/letter":
                letter_submissions.append(payload)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def _bridge_https_to_local(page, port):
    async def bridge(route):
        request = route.request
        observed = urlparse(request.url)
        local_url = f"http://127.0.0.1:{port}{observed.path}"
        if observed.query:
            local_url += f"?{observed.query}"
        response = await route.fetch(
            url=local_url,
            method=request.method,
            post_data=request.post_data,
        )
        await route.fulfill(response=response)

    await page.route("https://zarplata.ru/**", bridge)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_local_chromium_sends_cv_and_exact_inline_letter_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cv_submissions = []
    letter_submissions = []
    server, thread = _server(cv_submissions, letter_submissions)
    executor = BrowserExecutor("zarplata-inline-letter-fixture", ("zarplata.ru",), headless=True)
    try:
        page = await executor.start()
        await _bridge_https_to_local(page, server.server_port)
        await page.goto("https://zarplata.ru/vacancy/1")
        adapter = ZarplataAdapter()
        adapter._submission_timeout_ms = 2_000
        plan = ApplicationPlan(
            vacancy_id=1,
            resume_file="",
            submission_allowed=True,
            cover_letter=LETTER,
        )

        form = await adapter.open_application(page)
        assert form.requires_cover_letter
        filled = await adapter.fill_application(page, plan)
        assert filled.success
        assert await page.locator(locators.COVER_LETTER_INPUT).input_value() == LETTER
        result = await adapter.submit_application(page)

        assert result.status == "submitted"
        assert cv_submissions == [{"resume": ["selected"]}]
        assert letter_submissions == [{"text": [LETTER]}]
        assert adapter.get_submission_progress()["cover_letter_confirmed"] is True
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
