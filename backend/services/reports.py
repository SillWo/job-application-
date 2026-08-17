from __future__ import annotations

import csv
import html
import json
import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import ROOT
from backend.persistence.models import (
    Application,
    BrowserEvent,
    Evaluation,
    JobSession,
    Report,
    Vacancy,
)

REPORTS_ROOT = (ROOT / "data" / "reports").resolve()


def _font_candidates(bold: bool = False) -> list[Path]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return [
        windows / ("arialbd.ttf" if bold else "arial.ttf"),
        windows / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]


def _register_fonts() -> tuple[str, str]:
    names = ("JAO-Regular", "JAO-Bold")
    for name, candidates in zip(names, (_font_candidates(), _font_candidates(True)), strict=True):
        if name not in pdfmetrics.getRegisteredFontNames():
            path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if not path:
                raise RuntimeError("Не найден Unicode TTF-шрифт для PDF-отчёта")
            pdfmetrics.registerFont(TTFont(name, str(path)))
    return names


def _clean(value: object, fallback: str = "Не указано") -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def build_summary(db: Session, job_session: JobSession) -> dict:
    vacancies = list(
        db.scalars(
            select(Vacancy)
            .where(Vacancy.session_id == job_session.id)
            .order_by(Vacancy.id)
        )
    )
    evaluation_by_vacancy = {
        row.vacancy_id: row
        for row in db.scalars(
            select(Evaluation).where(Evaluation.vacancy_id.in_([v.id for v in vacancies]))
        )
    } if vacancies else {}
    vacancy_ids = [vacancy.id for vacancy in vacancies]
    applications = list(
        db.scalars(select(Application).where(Application.vacancy_id.in_(vacancy_ids)))
    ) if vacancy_ids else []
    application_status = {
        application.vacancy_id: application.status.upper() for application in applications
    }
    vacancy_rows = []
    for vacancy in vacancies:
        evaluation = evaluation_by_vacancy.get(vacancy.id)
        data = evaluation.data if evaluation else None
        vacancy_rows.append(
            {
                "id": vacancy.id,
                "title": vacancy.title,
                "company": vacancy.company,
                "url": vacancy.url,
                "state": vacancy.state,
                "score": data.get("score") if data else None,
                "reason": data.get("reason") if data else None,
                "score_breakdown": data.get("score_breakdown", []) if data else [],
                "evaluation_available": evaluation is not None,
            }
        )
    counters = dict(job_session.counters or {})
    matched_ids = {
        vacancy_id
        for vacancy_id, evaluation in evaluation_by_vacancy.items()
        if str((evaluation.data or {}).get("decision", "")).lower() == "apply"
    }
    states = {vacancy.id: (vacancy.state or "").upper() for vacancy in vacancies}
    submitted_ids = {
        vacancy_id
        for vacancy_id, state in states.items()
        if state == "SUBMITTED" or application_status.get(vacancy_id) == "SUBMITTED"
    }
    already_applied_ids = {
        vacancy_id
        for vacancy_id, state in states.items()
        if state == "ALREADY_APPLIED" or application_status.get(vacancy_id) == "ALREADY_APPLIED"
    }
    review_ids = {
        vacancy_id for vacancy_id, state in states.items() if state == "NEEDS_REVIEW"
    }
    error_ids = {
        vacancy_id
        for vacancy_id, state in states.items()
        if state in {"UNKNOWN", "FAILED", "ERROR", "BROWSER_ERROR"}
        or application_status.get(vacancy_id) in {"UNKNOWN", "FAILED", "ERROR"}
    }
    unscoped_errors = 0
    for event in db.scalars(
        select(BrowserEvent).where(
            BrowserEvent.session_id == job_session.id,
            BrowserEvent.event_type.in_(("browser_error", "error")),
        )
    ):
        vacancy_id = (event.data or {}).get("vacancy_id")
        if vacancy_id in states:
            error_ids.add(vacancy_id)
        else:
            unscoped_errors += 1
    return {
        "session_id": job_session.id,
        "started_at": job_session.started_at.isoformat() if job_session.started_at else None,
        "finished_at": job_session.finished_at.isoformat() if job_session.finished_at else None,
        "stop_reason": job_session.stop_reason,
        "adapter": job_session.adapter_id,
        "mode": job_session.mode,
        "status": job_session.status,
        "counters": counters,
        "aggregates": {
            "total": len(vacancy_rows),
            "evaluated": sum(row["evaluation_available"] for row in vacancy_rows),
            "matched": len(matched_ids),
            "submitted": len(submitted_ids),
            "already_applied": len(already_applied_ids),
            "review": len(review_ids),
            "errors": len(error_ids) + unscoped_errors,
        },
        "vacancies": vacancy_rows,
    }


def _pdf_styles() -> dict[str, ParagraphStyle]:
    regular, bold = _register_fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("TitleRU", parent=base["Title"], fontName=bold, fontSize=22, leading=27, textColor=colors.HexColor("#0a0a0a"), spaceAfter=8),
        "h2": ParagraphStyle("H2RU", parent=base["Heading2"], fontName=bold, fontSize=14, leading=18, textColor=colors.HexColor("#0a0a0a"), spaceBefore=8, spaceAfter=6),
        "h3": ParagraphStyle("H3RU", parent=base["Heading3"], fontName=bold, fontSize=11, leading=15, textColor=colors.HexColor("#0a0a0a"), spaceBefore=5, spaceAfter=3),
        "body": ParagraphStyle("BodyRU", parent=base["BodyText"], fontName=regular, fontSize=9, leading=13, textColor=colors.HexColor("#171717"), spaceAfter=4),
        "small": ParagraphStyle("SmallRU", parent=base["BodyText"], fontName=regular, fontSize=7.5, leading=10.5, textColor=colors.HexColor("#737373"), spaceAfter=2),
        "metric": ParagraphStyle("MetricRU", parent=base["BodyText"], fontName=bold, fontSize=16, leading=20, alignment=TA_CENTER, textColor=colors.HexColor("#0a0a0a")),
        "metric_label": ParagraphStyle("MetricLabelRU", parent=base["BodyText"], fontName=regular, fontSize=7, leading=9, alignment=TA_CENTER, textColor=colors.HexColor("#737373")),
        "footer": ParagraphStyle("FooterRU", parent=base["BodyText"], fontName=regular, fontSize=7, leading=9, textColor=colors.HexColor("#737373")),
    }


def _p(text: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(html.escape(_clean(text)), style)


def _format_datetime(value: object) -> str:
    return _clean(value).replace("T", " ")


def _draw_page(canvas, document) -> None:
    regular, _ = _register_fonts()
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#e5e5e5"))
    canvas.line(18 * mm, 17 * mm, A4[0] - 18 * mm, 17 * mm)
    canvas.setFont(regular, 7)
    canvas.setFillColor(colors.HexColor("#737373"))
    canvas.drawString(18 * mm, 12 * mm, "Job Application Orchestrator - локальный отчёт")
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Страница {document.page}")
    canvas.restoreState()


def write_pdf(summary: dict, path: Path) -> None:
    styles = _pdf_styles()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=27 * mm,
        title=f"Отчёт сессии {summary['session_id']}",
        author="Job Application Orchestrator",
    )
    story = [
        _p(f"Отчёт по сессии #{summary['session_id']}", styles["title"]),
        Paragraph(
            f"Период: {html.escape(_format_datetime(summary.get('started_at')))} - {html.escape(_format_datetime(summary.get('finished_at')))}<br/>"
            f"Режим: {html.escape(_clean(summary.get('mode')))} · Сайт: {html.escape(_clean(summary.get('adapter')))} · Статус: {html.escape(_clean(summary.get('status')))}<br/>"
            f"Причина завершения: {html.escape(_clean(summary.get('stop_reason')))}",
            styles["body"],
        ),
        Spacer(1, 5 * mm),
    ]
    aggregates = summary["aggregates"]
    metrics = [
        ("Всего", aggregates["total"]),
        ("Оценено", aggregates["evaluated"]),
        ("Подходит", aggregates["matched"]),
        ("Отправлено", aggregates["submitted"]),
        ("Уже откликались", aggregates["already_applied"]),
        ("Ошибки", aggregates["errors"]),
    ]
    metric_table = Table(
        [[_p(value, styles["metric"]) for _, value in metrics], [_p(label, styles["metric_label"]) for label, _ in metrics]],
        colWidths=[document.width / len(metrics)] * len(metrics),
    )
    metric_table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e5e5")), ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e5e5")), ("BACKGROUND", (0, 0), (-1, -1), colors.white), ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 1), (-1, 1), 8)]))
    story += [metric_table, Spacer(1, 7 * mm), _p("Вакансии", styles["h2"])]

    for index, vacancy in enumerate(summary["vacancies"], start=1):
        story.append(CondPageBreak(55 * mm))
        heading = f"{index}. {_clean(vacancy.get('title'))}"
        score = f"{vacancy['score']}/100" if vacancy.get("score") is not None else "Оценка отсутствует"
        header = Table(
            [[_p(heading, styles["h2"]), _p(score, styles["h3"])]],
            colWidths=[document.width - 35 * mm, 35 * mm],
        )
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story += [header]
        url = html.escape(_clean(vacancy.get("url")))
        story.append(
            Paragraph(
                f"Компания: {html.escape(_clean(vacancy.get('company')))} · Статус: {html.escape(_clean(vacancy.get('state')))}<br/>"
                f"URL: <link href=\"{url}\" color=\"#0a0a0a\"><u>{url}</u></link>",
                styles["small"],
            )
        )
        if not vacancy.get("evaluation_available"):
            story.append(_p("Оценка ИИ отсутствует для этой вакансии.", styles["body"]))
        else:
            story += [_p("Итоговое объяснение", styles["h3"]), _p(vacancy.get("reason"), styles["body"])]
            breakdown = vacancy.get("score_breakdown") or []
            if not breakdown:
                story.append(_p("Детализация оценки отсутствует.", styles["body"]))
            for component in breakdown:
                title = f"{_clean(component.get('title'))}: {component.get('points', 0)}/{component.get('max_points', 0)}"
                block = [
                    _p(title, styles["h3"]),
                    _p(component.get("explanation"), styles["body"]),
                ]
                evidence = component.get("evidence") or []
                if evidence:
                    block.append(_p("Доказательства:", styles["small"]))
                    block.extend(_p(f"• {item}", styles["small"]) for item in evidence)
                else:
                    block.append(_p("Доказательства не указаны.", styles["small"]))
                story.extend(block)
        story += [Spacer(1, 3 * mm), HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e5e5e5")), Spacer(1, 3 * mm)]
        if index < len(summary["vacancies"]) and index % 4 == 0:
            story.append(PageBreak())
    document.build(story, onFirstPage=_draw_page, onLaterPages=_draw_page)


def _write_legacy_files(summary: dict, folder: Path) -> tuple[Path, Path, Path]:
    json_path, html_path, csv_path = folder / "report.json", folder / "report.html", folder / "vacancies.csv"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text("<!doctype html><meta charset='utf-8'><title>Отчёт сессии</title><p>Используйте PDF-версию отчёта.</p>", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "title", "company", "score", "url", "state"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary["vacancies"])
    return json_path, html_path, csv_path


def generate_report(db: Session, job_session: JobSession, report: Report | None = None) -> Report:
    summary = build_summary(db, job_session)
    folder = REPORTS_ROOT / f"session-{job_session.id}"
    folder.mkdir(parents=True, exist_ok=True)
    json_path, html_path, csv_path = _write_legacy_files(summary, folder)
    pdf_path = folder / "report.pdf"
    write_pdf(summary, pdf_path)
    if report is None:
        report = db.scalar(select(Report).where(Report.session_id == job_session.id))
    if report is None:
        report = Report(session_id=job_session.id)
        db.add(report)
    report.summary = summary
    report.html_path = str(html_path)
    report.json_path = str(json_path)
    report.csv_path = str(csv_path)
    report.pdf_path = str(pdf_path)
    db.commit()
    db.refresh(report)
    return report


def ensure_report_pdf(db: Session, report: Report) -> Path:
    path = Path(report.pdf_path).resolve() if report.pdf_path else None
    if path and REPORTS_ROOT not in path.parents:
        raise FileNotFoundError("Небезопасный путь PDF-отчёта")
    if path and path.is_file():
        return path
    job_session = db.get(JobSession, report.session_id)
    if not job_session:
        raise FileNotFoundError("Сессия отчёта не найдена")
    regenerated = generate_report(db, job_session, report)
    path = Path(regenerated.pdf_path).resolve()
    if REPORTS_ROOT not in path.parents or not path.is_file():
        raise FileNotFoundError("PDF-отчёт не создан")
    return path
