"""Real Chromium with synthetic HH forms; no real applications or profile data."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from backend.adapters.hh.adapter import HHAdapter
from backend.browser.executor import BrowserExecutor
from backend.orchestrator.hh_application import complete_application
from backend.schemas.domain import ApplicationPlan, FormAnswer, JobPosting


@pytest.mark.e2e
async def test_fields_keep_their_question_and_ignore_unrelated_controls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("form-test", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.set_content('''
            <input aria-label="Поиск вакансий"><label><input type="checkbox">Какая зарплата?</label>
            <form>
              <div style="display:none"><div data-qa="task-question">Невидимый</div><input name="task_0_text"></div>
              <div><div data-qa="task-question">Город проживания</div><textarea name="task_1_text" placeholder="Писать тут"></textarea></div>
              <div><div data-qa="task-question">Опыт с Python</div>
                <label><input type="radio" name="task_2" value="y">Да</label>
                <label><input type="radio" name="task_2" value="n">Нет</label></div>
              <div><div data-qa="task-question">Технологии</div>
                <label><input type="checkbox" name="task_3_checkbox_0" value="p">Python</label>
                <label><input type="checkbox" name="task_3_checkbox_1" value="s">SQL</label></div>
              <label for="lang">Язык</label><select name="language" id="lang"><option value="">Выбрать</option><option value="ru">Русский</option></select>
              <label for="other">Дополнительно</label><input id="other" aria-required="false">
              <textarea data-qa="vacancy-response-popup-form-letter-input" name="letter"></textarea>
              <button data-qa="vacancy-response-submit-popup">Откликнуться</button>
            </form><select aria-label="Язык сайта"><option>English</option></select>
        ''')
        adapter = HHAdapter()
        adapter.allowed_domains = ("",)
        form = await adapter.read_application(page)
        assert [field.label for field in form.fields] == ["Город проживания", "Опыт с Python", "Технологии", "Язык", "Дополнительно"]
        assert [field.kind for field in form.fields] == ["text", "radio", "checkbox", "select", "text"]
        assert form.fields[1].options == ["Да", "Нет"]
        assert form.fields[2].options == ["Python", "SQL"]
        assert form.fields[3].options == ["Русский"]
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, cover_letter="Письмо")
        for field, values in zip(form.fields, [["Красноярск"], ["Да"], ["Python", "SQL"], ["Русский"]], strict=False):
            plan.form_answers[field.id] = FormAnswer(field=field, values=values)
        result = await adapter.fill_application(page, plan)
        assert result.success
        assert len(result.answered_fields) == 4
        assert await page.get_by_label("Поиск вакансий").input_value() == ""
        assert await page.locator("[name='letter']").input_value() == "Письмо"
        assert await page.locator("[name='task_1_text']").input_value() == "Красноярск"
        # A previously accepted answer cannot be reused for a changed label/options.
        await page.set_content('<form><label>Другое согласие<input name="task_1_text"></label><button data-qa="vacancy-response-submit-popup">Send</button></form>')
        result = await adapter.fill_application(page, plan)
        assert not result.success
        assert await page.locator("input").input_value() == ""
    finally:
        await executor.close()


@pytest.mark.e2e
async def test_foreign_notice_questionnaire_and_followup_are_completed_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    submitted = []
    questionnaire = '''<form method="post" action="/answer">
        <div><div data-qa="task-question">Желаемая зарплата</div><input type="number" name="task_1_text"></div>
        <button data-qa="vacancy-response-submit-popup">Откликнуться</button></form>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body):
            content = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == "/start":
                self.respond('<div role="dialog">Вы откликаетесь на вакансию в другой стране'
                             '<form action="/questions"><button>Все равно откликнуться</button></form></div>')
            else:
                self.respond(questionnaire)

        def do_POST(self):
            submitted.append(parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            if len(submitted) == 1:
                self.respond('<form method="post" action="/answer"><div><div data-qa="task-question">Город проживания</div>'
                             '<textarea name="task_2_text"></textarea></div><button data-qa="vacancy-response-submit-popup">Откликнуться</button></form>')
            else:
                self.respond('<div data-qa="vacancy-response-success">Отклик отправлен</div>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    executor = BrowserExecutor("form-flow", ("127.0.0.1",), headless=True)
    saved = []

    class Gateway:
        async def structured(self, role, payload, schema):
            if role == "application_salary_rules":
                return schema.model_validate(dict(has_salary_rules=True, rules=[dict(
                    amount=180000, currency="RUB", gross=False, period="month", condition="", quote="180000 RUB на руки")]))
            field = payload["fields"][0]
            salary = "зарплата" in field["label"].lower()
            return schema.model_validate(dict(answers=[dict(
                field_id=field["id"], category="salary" if salary else "fact",
                values=["180000" if salary else "Красноярск"], confidence=1, reason="Из данных пользователя",
                evidence=[dict(source="salary" if salary else "profile.residence", quote="180000 RUB" if salary else "Красноярск")],
            )]))

    try:
        page = await executor.start()
        await page.goto(f"http://127.0.0.1:{server.server_port}/start")
        plan = ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True, allow_foreign_application=True)
        adapter = HHAdapter()
        adapter.allowed_domains = ("127.0.0.1",)
        outcome = await complete_application(
            adapter, page, plan,
            JobPosting(source="hh", url="https://hh.ru/vacancy/1", title="Разработчик", description="Удалённая работа"),
            {"residence": "Красноярск"}, [{"desired_salary": "180000 RUB на руки"}], "", Gateway(),
            lambda current: saved.append(current.model_dump()) or True,
        )
        assert outcome.submission.status == "submitted"
        assert submitted == [{"task_1_text": ["180000"]}, {"task_2_text": ["Красноярск"]}]
        assert any(plan["form_answers"] for plan in saved)
        # Persisted answers bind to separate steps; no duplicate send is needed.
        assert len(saved[-1]["form_answers"]) == 2
    finally:
        await executor.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.e2e
async def test_unknown_required_checkbox_stops_and_same_form_is_not_resubmitted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("unknown-form", ("127.0.0.1",), headless=True)

    class Gateway:
        async def structured(self, role, payload, schema):
            return schema.model_validate({"answers": []})

    try:
        page = await executor.start()
        await page.set_content('<form><fieldset><legend>Согласие на переезд</legend>'
                               '<label><input type="checkbox" name="task_1">Да</label></fieldset>'
                               '<button data-qa="vacancy-response-submit-popup">Send</button></form>')
        adapter = HHAdapter()
        adapter.allowed_domains = ("",)
        outcome = await complete_application(adapter, page,
            ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True),
            JobPosting(source="hh", url="https://hh.ru/vacancy/1", title="Роль", description="Описание"),
            {}, [{}], "", Gateway(), lambda _: True)
        assert outcome.pending
        assert not await page.locator("input").is_checked()
    finally:
        await executor.close()
