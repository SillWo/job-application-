from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer

ROUTE_LABELS = {
    "direct_contact": "Прямой контакт",
    "external_employer": "Сайт работодателя",
    "hirehi_chat": "Вакансия HireHi",
    "unknown": "Маршрут не определён",
}


def _safe(value: object) -> str:
    return escape(str(value or "")).replace("\n", "<br/>")


def write_session_pdf(session_id: int, rows: list[dict]) -> str:
    root = Path("output/pdf")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"hirehi-session-{session_id}.pdf"
    font = "Helvetica"
    bold_font = "Helvetica-Bold"
    regular_path = Path("C:/Windows/Fonts/arial.ttf")
    bold_path = Path("C:/Windows/Fonts/arialbd.ttf")
    if regular_path.exists():
        pdfmetrics.registerFont(TTFont("HireHi", str(regular_path)))
        font = "HireHi"
        if bold_path.exists():
            pdfmetrics.registerFont(TTFont("HireHiBold", str(bold_path)))
            bold_font = "HireHiBold"
            pdfmetrics.registerFontFamily("HireHi", normal=font, bold=bold_font)

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "HireHiTitle",
        parent=styles["Title"],
        fontName=bold_font,
        fontSize=18,
        leading=22,
        spaceAfter=12,
    )
    heading = ParagraphStyle(
        "HireHiHeading",
        parent=styles["Heading2"],
        fontName=bold_font,
        fontSize=13,
        leading=16,
        spaceBefore=10,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "HireHiBody",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=9.5,
        leading=13,
        spaceAfter=5,
        splitLongWords=True,
    )
    label = ParagraphStyle("HireHiLabel", parent=body, fontName=bold_font, spaceAfter=2)

    story = [Paragraph(f"Отчёт HireHi - сессия {session_id}", title)]
    story.append(Paragraph(f"Выбрано вакансий: {len(rows)}", body))
    if not rows:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Подходящих вакансий не найдено.", body))
    for index, row in enumerate(rows, start=1):
        if index > 1:
            story.append(PageBreak())
        header = [
            Paragraph(f"{index}. {_safe(row.get('title') or 'Вакансия')}", heading),
            Paragraph(f"Компания: {_safe(row.get('company') or 'Не указана')}", body),
        ]
        if row.get("score") is not None:
            header.append(Paragraph(f"Оценка релевантности: {_safe(row['score'])}/100", body))
        story.append(KeepTogether(header))
        fields = (
            ("Ссылка на HireHi", "hirehi_url"),
            ("Тип отклика", "route_kind"),
            ("Контакт работодателя", "contact"),
            ("Ссылка на сайт работодателя", "target_url"),
            ("Краткое описание", "short_description"),
            ("Рекомендуемое сопроводительное письмо", "cover_letter"),
        )
        for field_label, key in fields:
            value = row.get(key)
            if key == "route_kind":
                value = ROUTE_LABELS.get(str(value), value)
            if not value and key == "contact":
                value = (
                    "Контакт не получен"
                    if row.get("route_kind") == "direct_contact"
                    else "Не требуется для этого типа вакансии"
                )
            if not value and key == "target_url":
                value = (
                    "Ссылка не получена"
                    if row.get("route_kind") == "external_employer"
                    else "Не требуется для этого типа вакансии"
                )
            story.append(Paragraph(_safe(field_label), label))
            story.append(Paragraph(_safe(value or "Не указано"), body))
            story.append(Spacer(1, 2))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.drawRightString(
            A4[0] - 20 * mm,
            10 * mm,
            f"Страница {document.page}",
        )
        canvas.restoreState()

    SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Отчёт HireHi - сессия {session_id}",
        author="Job Application Orchestrator",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return str(path)
