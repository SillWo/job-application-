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


def test_write_session_pdf_does_not_add_footer_only_boundary_page(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    letter = "Hello,\n" + "\n".join(
        f"{index}. I led product research, tested assumptions, and delivered a working pilot."
        for index in range(1, 22)
    ) + "\nKind regards,\nTest Candidate\nEmail: candidate@example.test\nTelegram: https://t.me/test_candidate"

    path = write_session_pdf(
        900001,
        [
            {
                "title": "Product manager",
                "company": "Example company",
                "score": 85,
                "hirehi_url": "https://hirehi.ru/management/synthetic-test",
                "route_kind": "external_employer",
                "target_url": "https://employer.example/jobs/test",
                "contact": "candidate@example.test",
                "short_description": "A synthetic vacancy for report pagination verification.",
                "cover_letter": letter,
            }
        ],
    )

    reader = PdfReader(path)
    assert len(reader.pages) == 1
    text = _pdf_text(path)
    assert "candidate@example.test" in text
    assert "https://hirehi.ru/management/synthetic-test" in text
    assert "https://employer.example/jobs/test" in text
    assert "A synthetic vacancy for report pagination verification." in text
    assert "Hello," in text and "Telegram: https://t.me/test_candidate" in text
    assert text.count("Страница 1") == 1


def test_write_session_pdf_keeps_near_boundary_cover_letter_on_one_page(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    letter_lines = [
        f"Synthetic application detail {index}: I plan, coordinate, and deliver product work across teams."
        for index in range(1, 31)
    ]

    path = write_session_pdf(
        900003,
        [
            {
                "title": "Synthetic product role",
                "company": "Example employer",
                "score": 82,
                "hirehi_url": "https://hirehi.ru/role/synthetic-boundary",
                "route_kind": "external_employer",
                "target_url": "https://employer.example/jobs/synthetic-boundary",
                "short_description": "A synthetic role description for pagination verification.",
                "cover_letter": "\n".join(letter_lines),
            }
        ],
    )

    reader = PdfReader(path)
    assert len(reader.pages) == 1
    text = _pdf_text(path)
    assert all(
        line in text for line in (letter_lines[0], letter_lines[14], letter_lines[-1])
    )
    assert all(f"Synthetic application detail {index}:" in text for index in range(1, 31))
    assert text.count("Страница 1") == 1


def test_write_session_pdf_splits_long_cover_letter_without_loss(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    letter_lines = [
        f"Synthetic cover-letter line {index}: product discovery and delivery experience."
        for index in range(1, 180)
    ]

    path = write_session_pdf(
        900002,
        [
            {
                "title": "Long letter role",
                "company": "Synthetic employer",
                "hirehi_url": "https://hirehi.ru/role/long-letter",
                "route_kind": "direct_contact",
                "contact": "recruiter@example.test",
                "target_url": "https://employer.example/jobs/long-letter",
                "short_description": "Synthetic description.",
                "cover_letter": "\n".join(letter_lines),
            }
        ],
    )

    reader = PdfReader(path)
    assert len(reader.pages) > 1
    page_text = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(page_text)
    assert all(line in text for line in (letter_lines[0], letter_lines[89], letter_lines[-1]))
    assert all(
        f"Synthetic cover-letter line {index}:" in text
        for index in range(1, len(letter_lines) + 1)
    )
    assert all(content.strip() for content in page_text)
    assert "recruiter@example.test" in text
    assert "https://employer.example/jobs/long-letter" in text


def test_write_session_pdf_explains_empty_selection(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    path = write_session_pdf(43, [])

    text = _pdf_text(path)
    assert len(PdfReader(path).pages) == 1
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
