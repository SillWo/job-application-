"""Visible compensation DOM -> adapter -> model payload, without external sites."""

import pytest

from backend.adapters.hh.adapter import HHAdapter
from backend.browser.executor import BrowserExecutor
from backend.intelligence.evaluator import evaluate
from backend.schemas.domain import MatchAssessment, ResumeAnalysis


@pytest.mark.e2e
@pytest.mark.parametrize("salary_qa", ["vacancy-salary", "vacancy-compensation"])
async def test_visible_salary_reaches_model_and_blocks_low_paid_job(tmp_path, monkeypatch, salary_qa):
    monkeypatch.chdir(tmp_path)
    executor = BrowserExecutor("salary-test", ("127.0.0.1",), headless=True)

    class Gateway:
        async def structured(self, role, payload, schema):
            assert payload["job"]["salary"] == {
                "minimum": None, "maximum": 36000, "currency": "RUB", "gross": False,
            }
            assert "до 36 000 ₽ за месяц, на руки" in payload["job"]["description"]
            assessment = MatchAssessment(score=2, confidence=1, evidence=[])
            return ResumeAnalysis(
                tasks=assessment, skills=[], experience_depth=assessment,
                role_match=assessment, industry=assessment, special_requirements=assessment,
            )

    try:
        page = await executor.start()
        await page.set_content(
            '<h1 data-qa="vacancy-title">Стажёр</h1>'
            '<div data-qa="vacancy-company-name">Компания</div>'
            '<div data-qa="vacancy-description">Координация проектов</div>'
            '<div data-qa="vacancy-view-raw-address">Москва, улица Вавилова, 19</div>'
            f'<div data-qa="{salary_qa}" style="display:none">от 100 000 ₽</div>'
            f'<div data-qa="{salary_qa}"><span>до 36&nbsp;000 ₽</span>'
            '<span> за месяц, на руки</span></div>'
        )
        job = await HHAdapter().extract_job(page)
        assert job.location == "Москва, улица Вавилова, 19"
        result = await evaluate(job, {}, [], Gateway(), preference_policy={
            "desired_salary": {"minimum_monthly_amount": 60000, "currency": "RUB"},
        })
        assert result.decision == "skip"
        assert "salary_below_preference" in result.hard_rule_violations
    finally:
        await executor.close()
