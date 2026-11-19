import os
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from src.core.config import Settings
from src.domain.schemas import DealContextInput, LLMProposalCopy, ProposalFinancials

HEADERS = ("Позиция", "Кол.", "Цена без НДС", "Скидка", "НДС", "Итого, ₽")
WIDTHS = (170, 46, 73, 68, 65, 82)


def money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def create_default_template(path: Path, brand: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    section = document.sections[0]
    section.left_margin = section.right_margin = Inches(0.75)
    section.top_margin = section.bottom_margin = Inches(0.65)
    document.styles["Normal"].font.name = "Arial"
    document.styles["Normal"].font.size = Pt(11)
    section.header.paragraphs[0].text = brand
    section.footer.paragraphs[0].text = "Коммерческое предложение"
    with NamedTemporaryFile(dir=path.parent, suffix=".docx", delete=False) as target:
        temporary = Path(target.name)
        try:
            document.save(target)
            target.close()
            if not path.exists():
                os.replace(temporary, path)
        finally:
            target.close()
            temporary.unlink(missing_ok=True)


def pdf_font_path(configured: Path | None) -> Path:
    candidates = [
        configured, Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]
    for path in candidates:
        if path is not None and path.is_file():
            return path
    raise FileNotFoundError("Укажите PDF_FONT с поддержкой кириллицы")


class DocumentRenderer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        create_default_template(settings.template, settings.brand)
        font = pdf_font_path(settings.pdf_font)
        self.font_name = f"Proposal-{font.resolve()}"
        if self.font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(self.font_name, str(font)))

    def render(
        self, cycle_id: str, context: DealContextInput, copy: LLMProposalCopy,
        financials: list[ProposalFinancials], *, needs_approval: bool,
    ) -> tuple[Path, Path]:
        folder = self.settings.artifacts / cycle_id
        folder.mkdir(parents=True, exist_ok=True)
        docx_path, pdf_path = folder / "proposal.docx", folder / "proposal.pdf"
        document = Document(str(self.settings.template))
        for name in ("Title", "Heading 1"):
            style = document.styles[name]
            style.font.name = "Arial"
            style.font.color.rgb = RGBColor.from_string("000000")
            properties = style.element.find(qn("w:pPr"))
            if properties is not None:
                border = properties.find(qn("w:pBdr"))
                if border is not None:
                    properties.remove(border)
        normal = ParagraphStyle("Body", fontName=self.font_name, fontSize=10, leading=14)
        heading = ParagraphStyle("Heading", parent=normal, fontSize=14, leading=18, keepWithNext=True)
        title = ParagraphStyle("Title", parent=heading, fontSize=21, leading=25)
        cell = ParagraphStyle("Cell", parent=normal, fontSize=8.5, leading=11)
        amount = ParagraphStyle("Amount", parent=cell, alignment=TA_RIGHT)
        header = ParagraphStyle("Header", parent=cell, textColor=colors.white)
        story: list[Flowable] = []

        def add(text: str, level: int = 0) -> None:
            if level:
                document.add_heading(text, level - 1)
            else:
                document.add_paragraph(text)
            style = {0: normal, 1: title, 2: heading}[level]
            story.extend([Paragraph(escape(text).replace("\n", "<br/>"), style), Spacer(1, 8)])

        add("Коммерческое предложение", 1)
        add(f"{context.company_name} · Сделка № {context.deal_id}")
        if context.company_details:
            add(context.company_details)
        if needs_approval:
            add("Проект для внутреннего согласования")
        add(copy.greeting)
        add(copy.value_proposition)
        add(copy.objection_handling)
        for variant in financials:
            add(variant.label, 2)
            rows = [HEADERS]
            rows.extend(
                (line.name, str(line.quantity), money(line.unit_price), money(line.discount),
                 money(line.vat), money(line.total))
                for line in variant.lines
            )
            table = document.add_table(rows=0, cols=len(HEADERS))
            table.style = "Table Grid"

            table.autofit = False
            for column, width in zip(table.columns, WIDTHS, strict=True):
                column.width = Pt(width)
            for index, row in enumerate(rows):
                table_row = table.add_row()
                if index == 0:
                    table_row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
                for position, (target, value) in enumerate(zip(table_row.cells, row, strict=True)):
                    target.width = Pt(WIDTHS[position])
                    paragraph = target.paragraphs[0]
                    paragraph.paragraph_format.space_after = Pt(3)
                    paragraph.paragraph_format.space_before = Pt(3)
                    run = paragraph.add_run(value)
                    run.font.size = Pt(9)
                    if index and position:
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    if index == 0 or index % 2 == 0:
                        shade = OxmlElement("w:shd")
                        shade.set(qn("w:fill"), "193756" if index == 0 else "F1F5F9")
                        target._tc.get_or_add_tcPr().append(shade)
                    if index == 0:
                        run.font.color.rgb = RGBColor.from_string("FFFFFF")
            pdf_rows = [
                [Paragraph(escape(value), header if i == 0 else (cell if j == 0 else amount))
                 for j, value in enumerate(row)]
                for i, row in enumerate(rows)
            ]
            pdf_table = LongTable(pdf_rows, colWidths=WIDTHS, repeatRows=1)
            pdf_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#193756")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F9")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D9D9D9")),
            ]))
            story.extend([pdf_table, Spacer(1, 8)])
            add(f"Без НДС {money(variant.net)} ₽ · НДС {money(variant.vat)} ₽ · Итого {money(variant.total)} ₽")
        add("Условия", 2)
        add(f"Оплата. {copy.payment_terms}")
        add(f"Внедрение. {copy.implementation_terms}")
        document.save(str(docx_path))
        pdf = SimpleDocTemplate(
            str(pdf_path), pagesize=letter, leftMargin=48, rightMargin=48,
            topMargin=55, bottomMargin=45, title="Коммерческое предложение",
            author=self.settings.brand,
        )

        def decorate(canvas: object, _: object) -> None:
            from typing import cast

            from reportlab.pdfgen.canvas import Canvas

            page = cast(Canvas, canvas)
            page.setFont(self.font_name, 9)
            page.drawString(54, 756, self.settings.brand)
            page.drawRightString(558, 28, str(page.getPageNumber()))

        pdf.build(story, onFirstPage=decorate, onLaterPages=decorate)
        return docx_path, pdf_path
