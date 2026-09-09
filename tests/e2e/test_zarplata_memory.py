"""Shared profile answers reach real textarea controls on another adapter."""
import pytest

from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.browser.executor import BrowserExecutor
from backend.intelligence.application_answers import prepare_answers
from backend.schemas.domain import ApplicationPlan, JobPosting


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_zarplata_fills_confirmed_memory_and_rejects_changed_question(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("zarplata-memory", ("127.0.0.1",), headless=True)
    try:
        page = await executor.start()
        await page.set_content('<form><div data-qa="task-question">Когда можете начать?</div><textarea name="task_1_text"></textarea></form>')
        adapter = ZarplataAdapter()
        form = await adapter.open_application(page)
        # This synthetic form is already open, with no vacancy response button.
        from backend.adapters.base.protocol import ApplicationForm

        questions = await adapter._application_questions(page)
        form = ApplicationForm(questions=questions, fields=adapter._application_fields)

        class Gateway:
            async def structured(self, role, payload, schema):
                assert "Через две недели" in payload["sources"]["memory.1"]
                return schema.model_validate({"answers": [{
                    "field_id": form.fields[0].id, "category": "fact", "values": ["Через две недели"],
                    "evidence": [{"source": "memory.1", "quote": "Через две недели"}],
                    "confidence": 1, "reason": "Ответ пользователя из прошлой сессии HH",
                }]})

        plan = await prepare_answers(Gateway(), form, ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True),
            JobPosting(source="zarplata", url="https://zarplata.ru/vacancy/1", title="Разработчик", description="Работа"),
            {}, [], "", memory=[{"id": 1, "question": "Когда можете начать?", "answer": "Через две недели", "context": {}}])
        result = await adapter.fill_application(page, plan)
        assert result.success
        assert await page.locator("textarea").input_value() == "Через две недели"
        await page.set_content('<form><div data-qa="task-question">Готовы к переезду?</div><textarea name="task_1_text"></textarea></form>')
        assert not (await adapter.fill_application(page, plan)).success
        assert await page.locator("textarea").input_value() == ""
    finally:
        await executor.close()
