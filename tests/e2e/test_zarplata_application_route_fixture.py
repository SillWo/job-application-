"""Chromium contract for Zarplata's full-page response route."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.adapters.zarplata import locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.browser.executor import BrowserExecutor
from backend.schemas.domain import ApplicationPlan, FormAnswer

LETTER = "Synthetic full-page route letter"
TASK_PROMPTS = ["Опишите ваш опыт", "Почему вы выбрали эту вакансию?"]
TASK_ANSWERS = ["Синтетический ответ об опыте", "Синтетический ответ о вакансии"]


def _server(cv_submissions, letter_submissions, response_views):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/applicant/vacancy_response?"):
                response_views.append(self.path)
                content = """<!doctype html><html><body>
                  <form id="response-form">
                    <div data-qa="task-question">Опишите ваш опыт</div>
                    <textarea type="textarea" name="task_386366758_text"></textarea>
                    <div data-qa="task-question">Почему вы выбрали эту вакансию?</div>
                    <textarea type="textarea" name="task_386366759_text"></textarea>
                    <button type="button" data-qa="vacancy-response-letter-toggle">
                      Сопроводительное письмоДобавить
                    </button>
                    <button type="button" data-qa="vacancy-response-submit-popup">Откликнуться</button>
                  </form>
                  <script>
                    const form = document.querySelector('#response-form');
                    const toggle = form.querySelector('[data-qa="vacancy-response-letter-toggle"]');
                    const submit = form.querySelector('[data-qa="vacancy-response-submit-popup"]');
                    toggle.addEventListener('click', () => {
                      const input = document.createElement('textarea');
                      input.setAttribute('data-qa', 'vacancy-response-popup-form-letter-input');
                      form.insertBefore(input, submit);
                      toggle.hidden = true;
                    });
                    submit.addEventListener('click', async () => {
                      const text = form.querySelector('[data-qa="vacancy-response-popup-form-letter-input"]').value;
                      await fetch('/cv', {method: 'POST', body: 'resume=selected'});
                      await fetch('/letter', {method: 'POST', body: new URLSearchParams({text})});
                      const success = document.createElement('div');
                      success.setAttribute('data-qa', 'vacancy-response-success');
                      success.textContent = 'Отклик отправлен';
                      document.body.append(success);
                    });
                  </script>
                </body></html>"""
            else:
                content = """<!doctype html><html><body>
                  <button data-qa="vacancy-response-link-top">Откликнуться</button>
                  <script>
                    document.querySelector('[data-qa="vacancy-response-link-top"]')
                      .addEventListener('click', () => {
                        window.location.href = '/applicant/vacancy_response?vacancyId=1';
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
async def test_full_page_response_redirect_fills_and_submits_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cv_submissions = []
    letter_submissions = []
    response_views = []
    server, thread = _server(cv_submissions, letter_submissions, response_views)
    executor = BrowserExecutor(
        "zarplata-full-page-form-fixture", ("zarplata.ru",), headless=True
    )
    try:
        page = await executor.start()
        await _bridge_https_to_local(page, server.server_port)
        await page.goto("https://zarplata.ru/vacancy/1")
        adapter = ZarplataAdapter()
        adapter._expected_job_id = "1"
        adapter._submission_timeout_ms = 2_000
        adapter._letter_readiness_timeout_ms = 2_000
        form = await adapter.open_application(page)
        assert form.requires_cover_letter
        assert [field.kind for field in form.fields] == ["text", "text"]
        assert [field.label for field in form.fields] == TASK_PROMPTS
        assert page.url == "https://zarplata.ru/applicant/vacancy_response?vacancyId=1"

        plan = ApplicationPlan(
            vacancy_id=1,
            resume_file="",
            submission_allowed=True,
            cover_letter=LETTER,
            form_answers={
                field.id: FormAnswer(field=field, values=[answer], source="fixture")
                for field, answer in zip(form.fields, TASK_ANSWERS, strict=True)
            },
        )

        filled = await adapter.fill_application(page, plan)
        assert filled.success
        assert [
            await page.locator(f'textarea[name="{name}"]').input_value()
            for name in ("task_386366758_text", "task_386366759_text")
        ] == TASK_ANSWERS
        assert await page.locator(locators.COVER_LETTER_INPUT).input_value() == LETTER

        result = await adapter.submit_application(page)
        assert result.status == "submitted"
        assert (await adapter.verify_submission(page, just_submitted=True)).status == "submitted"
        assert response_views == ["/applicant/vacancy_response?vacancyId=1"]
        assert cv_submissions == [{"resume": ["selected"]}]
        assert letter_submissions == [{"text": [LETTER]}]
        assert adapter.get_submission_progress()["cover_letter_confirmed"] is True
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
