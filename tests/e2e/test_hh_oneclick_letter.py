"""Synthetic Chromium coverage for HH's post-response cover-letter dialog."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from backend.adapters.hh.adapter import HHAdapter
from backend.browser.executor import BrowserExecutor
from backend.schemas.domain import ApplicationPlan

LETTER = "Здравствуйте!\nСвязаться: +7 900 123-45-67\nПортфолио: https://example.test/me"


@pytest.mark.e2e
async def test_oneclick_attaches_exact_letter_after_delayed_success_action(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses: list[dict[str, list[str]]] = []
    letters: list[dict[str, list[str]]] = []
    server, thread = _server(responses, letters, delayed_ms=1_200)
    executor = BrowserExecutor("hh-oneclick-letter", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy")
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter=LETTER)

        await adapter.open_application(page)
        form = await adapter.prepare_application(page, plan)
        assert form.requires_cover_letter
        filled = await adapter.fill_application(page, plan)
        assert filled.success
        assert await page.locator("[data-qa='vacancy-response-popup-form-letter-input']").input_value() == LETTER

        result = await adapter.submit_application(page)

        assert result.status == "submitted"
        assert responses == [{"resume": ["selected"]}]
        assert letters == [{"text": [LETTER]}]
        assert await page.locator("[data-qa='vacancy-response-link-top']").count() == 0
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.e2e
async def test_preexisting_response_does_not_attach_a_letter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses: list[dict[str, list[str]]] = []
    letters: list[dict[str, list[str]]] = []
    server, thread = _server(responses, letters, preexisting=True)
    executor = BrowserExecutor("hh-preexisting-letter", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy")
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter=LETTER)

        await adapter.open_application(page)
        form = await adapter.prepare_application(page, plan)
        assert not form.requires_cover_letter
        filled = await adapter.fill_application(page, plan)
        assert not filled.success
        assert "Сопроводительное письмо" in filled.unknown_questions
        result = await adapter.submit_application(page)

        assert result.status == "already_applied"
        assert responses == []
        assert letters == []
        assert await page.locator("[role='dialog']").count() == 0
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.e2e
async def test_restart_confirms_existing_cv_and_resumes_only_letter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses: list[dict[str, list[str]]] = []
    letters: list[dict[str, list[str]]] = []
    server, thread = _server(responses, letters, preexisting=True)
    executor = BrowserExecutor("hh-letter-restart", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy")
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter=LETTER)

        result = await adapter.verify_cv_submission(page)
        form = await adapter.resume_application(
            page, plan, cv_confirmed=True, cover_letter_pending=True,
        )
        assert result.status == "submitted"
        assert form.requires_cover_letter
        assert (await adapter.fill_application(page, plan)).success
        submitted = await adapter.submit_application(page)

        assert submitted.status == "submitted"
        assert responses == []
        assert letters == [{"text": [LETTER]}]
        assert adapter.get_submission_progress() == {
            "cv_confirmed": True,
            "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.e2e
async def test_open_letter_dialog_is_not_false_success_and_never_reclicked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses: list[dict[str, list[str]]] = []
    letters: list[dict[str, list[str]]] = []
    server, thread = _server(responses, letters, keep_dialog=True)
    executor = BrowserExecutor("hh-letter-remains-open", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy")
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        adapter._hh_cover_letter_verify_timeout_ms = 1_000
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter=LETTER)

        await adapter.open_application(page)
        await adapter.prepare_application(page, plan)
        assert (await adapter.fill_application(page, plan)).success
        first = await adapter.submit_application(page)
        second = await adapter.submit_application(page)

        assert first.status == second.status == "unknown"
        assert responses == [{"resume": ["selected"]}]
        assert letters == [{"text": [LETTER]}]
        assert await page.locator("[data-qa='letter-click-count']").inner_text() == "1"
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.e2e
async def test_busy_letter_submission_waits_beyond_five_seconds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses: list[dict[str, list[str]]] = []
    letters: list[dict[str, list[str]]] = []
    server, thread = _server(responses, letters, letter_delay_ms=5_500)
    executor = BrowserExecutor("hh-letter-delayed-submit", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy")
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter=LETTER)

        await adapter.open_application(page)
        await adapter.prepare_application(page, plan)
        assert (await adapter.fill_application(page, plan)).success
        result = await adapter.submit_application(page)

        assert result.status == "submitted"
        assert responses == [{"resume": ["selected"]}]
        assert letters == [{"text": [LETTER]}]
        assert await page.locator("[role='dialog']").count() == 0
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.e2e
async def test_busy_letter_timeout_is_unknown_without_second_click(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses: list[dict[str, list[str]]] = []
    letters: list[dict[str, list[str]]] = []
    server, thread = _server(responses, letters, never_complete=True)
    executor = BrowserExecutor("hh-letter-pending-submit", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/vacancy")
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        adapter._hh_cover_letter_verify_timeout_ms = 1_000
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter=LETTER)

        await adapter.open_application(page)
        await adapter.prepare_application(page, plan)
        assert (await adapter.fill_application(page, plan)).success
        first = await adapter.submit_application(page)
        second = await adapter.submit_application(page)

        assert first.status == second.status == "unknown"
        assert responses == [{"resume": ["selected"]}]
        assert letters == [{"text": [LETTER]}]
        assert await page.locator("[data-qa='letter-click-count']").inner_text() == "1"
        assert not await page.locator("[data-qa='vacancy-response-letter-submit']").is_enabled()
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _server(responses, letters, *, delayed_ms=0, preexisting=False, keep_dialog=False,
            letter_delay_ms=0, never_complete=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, body):
            content = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            existing_markup = (
                '<a data-qa="vacancy-response-link-view-topic">Вы откликнулись</a>'
                '<button data-qa="responded-success-attach-cover-letter">Приложить сопроводительное письмо</button>'
                if preexisting else '<button data-qa="vacancy-response-link-top">Откликнуться</button>'
            )
            html = f'''<!doctype html><html><body>{existing_markup}<div id="root"></div>
                <script>
                const root = document.querySelector('#root');
                const delay = {delayed_ms};
                const keepDialog = {str(keep_dialog).lower()};
                const neverComplete = {str(never_complete).lower()};
                const attachExisting = {str(preexisting).lower()};
                function openLetter() {{
                  root.innerHTML = `<div role="dialog" aria-label="Сопроводительное письмо">
                    <textarea data-qa="vacancy-response-popup-form-letter-input" name="text"></textarea>
                    <button data-qa="vacancy-response-letter-submit">Отправить</button>
                    <span data-qa="letter-click-count">0</span></div>`;
                  const button = root.querySelector('[data-qa="vacancy-response-letter-submit"]');
                  let clicks = 0;
                  button.addEventListener('click', async (event) => {{
                    event.preventDefault(); clicks += 1; button.disabled = true;
                    button.setAttribute('aria-busy', 'true');
                    root.querySelector('[role="dialog"]').setAttribute('aria-busy', 'true');
                    root.querySelector('[data-qa="letter-click-count"]').textContent = String(clicks);
                    const body = new URLSearchParams({{text: root.querySelector('textarea').value}});
                    await fetch('/letter', {{method: 'POST', body}});
                    if (neverComplete) await new Promise(() => {{}});
                    if (!keepDialog) root.innerHTML = '<div data-qa="vacancy-response-popup-success">Сопроводительное письмо отправлено</div>';
                  }});
                }}
                function showAttach() {{
                  const button = document.createElement('button');
                  button.dataset.qa = 'responded-success-attach-cover-letter';
                  button.textContent = 'Приложить сопроводительное письмо';
                  button.onclick = openLetter;
                  document.body.append(button);
                }}
                if (attachExisting) document.querySelector('[data-qa="responded-success-attach-cover-letter"]').onclick = openLetter;
                const response = document.querySelector('[data-qa="vacancy-response-link-top"]');
                if (response) response.onclick = async () => {{
                  response.remove();
                  await fetch('/respond', {{method: 'POST', body: new URLSearchParams({{resume: 'selected'}})}});
                  document.body.insertAdjacentHTML('beforeend', '<a data-qa="vacancy-response-link-view-topic">Вы откликнулись</a>');
                  setTimeout(showAttach, delay);
                }};
                </script></body></html>'''
            self.respond(html)

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            if self.path == "/respond":
                responses.append(form)
            elif self.path == "/letter":
                letters.append(form)
                if letter_delay_ms:
                    time.sleep(letter_delay_ms / 1_000)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
