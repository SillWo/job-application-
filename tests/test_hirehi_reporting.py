from types import SimpleNamespace

from pypdf import PdfReader

from backend.orchestrator.workflow import (
    _application_count,
    _application_limit_reason,
    _vacancy_scope,
)
from backend.services.hirehi_reporting import write_session_pdf


def _pdf_text(path: str) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def test_hirehi_application_limit_counts_reported_rows() -> None:
    session = SimpleNamespace(counters={"reported": 5, "submitted": 0})

    assert _application_count(session, "hirehi") == 5
    assert _application_count(session, "hh") == 0
    assert _application_limit_reason("hirehi") == "Достигнут лимит выбранных вакансий"


def test_hirehi_duplicate_scope_is_session_local_but_hh_remains_global() -> None:
    assert len(_vacancy_scope("hirehi", "hirehi", "42", 7)) == 3
    assert len(_vacancy_scope("hh", "hh", "42", 7)) == 2


def test_write_session_pdf_contains_all_report_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    path = write_session_pdf(
        42,
        [
            {
                "title": "Менеджер <продукта>",
                "company": "Компания & команда",
                "score": 87,
                "hirehi_url": "https://hirehi.ru/management/product-42",
                "route_kind": "external_employer",
                "target_url": "https://employer.example/jobs/42",
                "contact": "",
                "short_description": "Развитие B2B-продукта и продуктовых метрик.",
                "cover_letter": "Здравствуйте! Мой опыт соответствует задачам вакансии.",
            }
        ],
    )

    text = _pdf_text(path)
    assert "Менеджер <продукта>" in text
    assert "Компания & команда" in text
    assert "Оценка релевантности: 87/100" in text
    assert "Сайт работодателя" in text
    assert "https://employer.example/jobs/42" in text
    assert "Рекомендуемое сопроводительное письмо" in text
    assert "Страница 1" in text


def test_write_session_pdf_explains_empty_selection(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    path = write_session_pdf(43, [])

    text = _pdf_text(path)
    assert "Выбрано вакансий: 0" in text
    assert "Подходящих вакансий не найдено." in text


def test_write_session_pdf_distinguishes_missing_required_route_data(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    path = write_session_pdf(
        44,
        [
            {"title": "Direct", "route_kind": "direct_contact"},
            {"title": "External", "route_kind": "external_employer"},
        ],
    )

    text = _pdf_text(path)
    assert "Контакт не получен" in text
    assert "Ссылка не получена" in text
