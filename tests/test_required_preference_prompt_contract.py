from __future__ import annotations

from backend.intelligence.gateway import _system_prompt_for_role


def _policy_payload() -> dict:
    return {
        "preference_policy": {
            "green_flags": [{
                "id": "required-role",
                "text": "Продуктовый менеджмент или продуктовая аналитика в IT-продукте",
                "category": "desired_task",
                "required": True,
                "source_quote": "Ищу продуктовый менеджмент или продуктовую аналитику в IT-продукте",
            }],
            "red_flags": [],
        },
    }


def test_resume_analyst_policy_prompt_treats_required_preference_as_whole_condition():
    prompt = _system_prompt_for_role("resume_analyst", _policy_payload()).casefold()

    assert "required=true" in prompt
    assert "всю обязательную комбинацию как условие допуска" in prompt
    assert "через «и»" in prompt and "каждое из них" in prompt
    assert "через «или»" in prompt and "одного явно подходящего варианта" in prompt
    assert "точными цитатами из обязанностей" in prompt
    assert "прошлый опыт кандидата сами по себе не подтверждают" in prompt
    assert "частичном, косвенном или неясном совпадении" in prompt
    assert "бизнес-инициативы и небольшое упоминание it не подтверждают" in prompt
    assert "исследование рынка для руководства само по себе не означает работу с пользователями продукта" in prompt
    assert "аналитика продуктовых метрик, экспериментов и поведения пользователей" in prompt
    assert "не повышай score за частичное совпадение" in prompt


def test_required_preference_instruction_is_dynamic_and_does_not_change_writer_prompt():
    analyst_without_policy = _system_prompt_for_role("resume_analyst", {}).casefold()
    writer_with_policy = _system_prompt_for_role("writer", _policy_payload()).casefold()

    assert "всю обязательную комбинацию как условие допуска" not in analyst_without_policy
    assert "всю обязательную комбинацию как условие допуска" not in writer_with_policy
    assert "required=true" not in writer_with_policy
