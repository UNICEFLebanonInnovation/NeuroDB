"""The desk review as a Word file (``desk-review-YYYY-MM-DD.docx``), assembled from the document review's
figures without an AI call: the scope, the counts, the recurring themes with their cited evidence, the
recurring challenges, the repeated findings, the most urgent statements, the open action points by owner,
the coverage and the method. Every claim is cited "(Document title, p. n)".

It reads ``review_data`` as the page does, so the file and the page agree for the same batch, Verified
only switch and minimum of documents. The file is a minimal Office Open XML package written with
``zipfile`` (:class:`Docx`): the content types, the package relationships, the document, its
relationships and the styles (Title, Heading 1-3, a list paragraph and a grid table style).
"""

from __future__ import annotations

import datetime
import io
import zipfile
from collections import defaultdict
from xml.sax.saxutils import escape

from django.utils import timezone

from . import review_data
from .models import Document, DocumentStatement, ReviewBatch, Verdict

THEMES = 15
EVIDENCE_PER_THEME = 3
CHALLENGES = 10
REPEATED = 15
STATEMENTS = 10
ACTIONS_PER_OWNER = 10

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PACKAGE = "http://schemas.openxmlformats.org/package/2006"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
CONTENT_TYPES = (
    f'{XML_HEAD}<Types xmlns="{PACKAGE}/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{WORD_TYPE}.document.main+xml"/>'
    f'<Override PartName="/word/styles.xml" ContentType="{WORD_TYPE}.styles+xml"/>'
    "</Types>"
)
PACKAGE_RELS = (
    f'{XML_HEAD}<Relationships xmlns="{PACKAGE}/relationships">'
    f'<Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
DOCUMENT_RELS = (
    f'{XML_HEAD}<Relationships xmlns="{PACKAGE}/relationships">'
    f'<Relationship Id="rId1" Type="{OFFICE}/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _heading_style(level: int, size: int) -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        f'<w:pPr><w:keepNext/><w:spacing w:before="{360 - 60 * level}" w:after="120"/>'
        f'<w:outlineLvl w:val="{level - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:color w:val="1F3864"/><w:sz w:val="{size}"/></w:rPr></w:style>'
    )


STYLES = (
    f'{XML_HEAD}<w:styles xmlns:w="{W}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:lang w:val="en-GB"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
    "</w:style>"
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="240"/></w:pPr>'
    '<w:rPr><w:b/><w:color w:val="1CABE2"/><w:sz w:val="48"/></w:rPr></w:style>'
    + _heading_style(1, 32)
    + _heading_style(2, 26)
    + _heading_style(3, 23)
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="60"/>'
    '<w:ind w:left="360" w:hanging="240"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:ind w:left="567"/></w:pPr><w:rPr><w:i/><w:color w:val="595959"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Small"><w:name w:val="Small"/><w:basedOn w:val="Normal"/>'
    '<w:rPr><w:color w:val="595959"/><w:sz w:val="18"/></w:rPr></w:style>'
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
    '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
    '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/>'
    "</w:tblCellMar></w:tblPr></w:style>"
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
    '<w:basedOn w:val="TableNormal"/>'
    '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr><w:sz w:val="20"/></w:rPr>'
    '<w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
    '<w:left w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
    '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
    '<w:right w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
    '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
    '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/></w:tblBorders></w:tblPr></w:style>'
    "</w:styles>"
)


def _text(value: object) -> str:
    """Text Word accepts: XML-escaped, without the control characters XML forbids."""
    text = "".join(c for c in str(value) if c in "\t\n" or ord(c) >= 32)
    return escape(text)


class Docx:
    """A minimal Word document: styled paragraphs, bullets and grid tables."""

    def __init__(self):
        self.body: list[str] = []

    def paragraph(self, text: str = "", style: str = "", bold: bool = False) -> None:
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
        lines = str(text).split("\n")
        runs = "<w:br/>".join(f'<w:t xml:space="preserve">{_text(line)}</w:t>' for line in lines)
        self.body.append(f"<w:p>{props}<w:r>{run_props}{runs}</w:r></w:p>")

    def heading(self, text: str, level: int = 1) -> None:
        self.paragraph(text, f"Heading{level}")

    def bullet(self, text: str) -> None:
        self.paragraph(f"• {text}", "ListParagraph")

    def table(self, header: list[str], rows: list[list[object]]) -> None:
        def cell(value: object, head: bool = False) -> str:
            run_props = "<w:rPr><w:b/></w:rPr>" if head else ""
            return (
                '<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{_text(value)}</w:t></w:r></w:p></w:tc>'
            )

        grid = "".join("<w:gridCol/>" for _ in header)
        head = "<w:tr><w:trPr><w:tblHeader/></w:trPr>" + "".join(cell(h, True) for h in header) + "</w:tr>"
        body = "".join("<w:tr>" + "".join(cell(v) for v in row) + "</w:tr>" for row in rows)
        self.body.append(
            '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{head}{body}</w:tbl>"
        )
        self.paragraph()

    def save(self) -> bytes:
        document = (
            f'{XML_HEAD}<w:document xmlns:w="{W}"><w:body>{"".join(self.body)}'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" '
            'w:footer="567" w:gutter="0"/></w:sectPr></w:body></w:document>'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("_rels/.rels", PACKAGE_RELS)
            archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/styles.xml", STYLES)
        return buffer.getvalue()


def filename(today: datetime.date | None = None) -> str:
    return f"desk-review-{(today or timezone.localdate()).isoformat()}.docx"


def _statement_cite(statement: DocumentStatement) -> str:
    cited = [f for f in statement.cites.all() if f.verdict != Verdict.REJECTED]
    if cited:
        return " ".join(dict.fromkeys(review_data.cite(f) for f in cited[:2]))
    return f"({statement.document.title})"


def build(batch: int | None, verified: bool, min_documents: int = 2) -> bytes:
    """The desk review of ``batch`` (every batch when None) as a .docx file's bytes."""
    doc = Docx()
    today = timezone.localdate()
    chosen = ReviewBatch.objects.filter(pk=batch).first() if batch else None
    documents = list(
        review_data.in_review_documents(batch)
        .filter(review_status__in=[Document.ReviewStatus.DONE, Document.ReviewStatus.PARTLY])
        .select_related("review_batch")
        .order_by("review_batch__name", "title")
    )
    counted = review_data.findings(batch, verified)
    figures = review_data.dashboard(batch, verified)
    tiles = figures["tiles"]
    found = review_data.synthesis(batch, verified, min_documents)
    challenges = review_data.synthesis(batch, verified, min_documents, challenges=True)

    doc.paragraph("Desk review", "Title")
    doc.paragraph(
        f"Assembled by NeuroDB from the document review on {today:%d %B %Y}, without AI: every figure is "
        "counted from the findings, and every claim is cited (Document title, page).",
        "Small",
    )

    # ---- scope
    doc.heading("Scope", 1)
    batch_names = [chosen.name] if chosen else sorted({d.review_batch.name for d in documents})
    years = sorted({y for d in documents if (y := review_data.as_of(d).year)})
    period = f"{years[0]}–{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "not dated")
    doc.bullet(f"Batches: {', '.join(batch_names) or 'none'}")
    doc.bullet(f"Documents analysed: {len(documents)}")
    doc.bullet(f"Period the documents cover: {period}")
    doc.bullet(
        "Findings used: only those a reviewer accepted (Verified only)"
        if verified
        else "Findings used: every finding not rejected by a reviewer (accepted or not reviewed yet)"
    )
    doc.bullet(f"A theme is recurring when at least {found.min_documents} documents raise it.")
    if documents:
        doc.heading("Documents", 2)
        doc.table(
            ["Document", "Batch", "Status", "Analysed on"],
            [
                [
                    d.title,
                    d.review_batch.name,
                    d.get_review_status_display(),
                    timezone.localtime(d.reviewed_at).strftime("%d %b %Y") if d.reviewed_at else "",
                ]
                for d in documents
            ],
        )

    # ---- summary counts
    doc.heading("Summary", 1)
    doc.table(
        ["Figure", "Count"],
        [
            ["Documents analysed", tiles["documents"]],
            ["Findings", tiles["findings"]],
            ["Key statements", tiles["statements"]],
            [f"High-urgency statements (urgency {review_data.HIGH_URGENCY} or more)", tiles["urgent"]],
            ["Open action points", tiles["open_actions"]],
            ["Recurring themes", len(found.themes)],
        ],
    )
    by_category = ", ".join(f"{label} {n}" for label, n, _key in figures["charts"]["category"]) or "none"
    doc.paragraph(f"Findings by category: {by_category}.")
    if figures["average_evidence"] is not None:
        doc.paragraph(f"Average evidence score: {figures['average_evidence']} out of 100.")

    # ---- recurring themes
    doc.heading("Recurring themes", 1)
    if not found.themes:
        doc.paragraph(f"No theme is raised by {found.min_documents} documents or more.")
    for theme in found.themes[:THEMES]:
        doc.heading(theme.topic.path, 2)
        years_text = f", {theme.year_span}" if theme.year_span else ""
        doc.paragraph(
            f"Raised by {theme.n_documents} documents ({theme.n_findings} findings{years_text}); "
            f"average evidence {theme.average_evidence}. "
            f"{review_data.TRENDS[theme.trend]}."
        )
        for finding in review_data.theme_findings(theme.topic.pk, batch, verified, limit=EVIDENCE_PER_THEME):
            doc.bullet(f"{finding.text} {review_data.cite(finding)}")
            if finding.quote:
                doc.paragraph(f"“{finding.quote}”", "Quote")
    if len(found.themes) > THEMES:
        doc.paragraph(f"{len(found.themes) - THEMES} more themes are listed on the Synthesis tab.", "Small")

    # ---- recurring challenges
    doc.heading("Recurring challenges", 1)
    if not challenges.themes:
        doc.paragraph(f"No challenge is raised by {found.min_documents} documents or more.")
    for theme in challenges.themes[:CHALLENGES]:
        top = review_data.theme_findings(theme.topic.pk, batch, verified, challenges=True, limit=1)
        example = f" For example: {top[0].text} {review_data.cite(top[0])}" if top else ""
        doc.bullet(f"{theme.topic.path}: raised as a challenge by {theme.n_documents} documents.{example}")

    # ---- repeated findings
    doc.heading("Findings repeated across documents", 1)
    groups = review_data.repeated(batch, verified, limit=REPEATED)
    if not groups:
        doc.paragraph("No finding is repeated in near-identical words across documents.")
    for group in groups:
        lead = group.lead
        places = " ".join(dict.fromkeys(review_data.cite(f) for f in group.findings))
        doc.bullet(f"{lead.text} Raised in {group.documents} documents: {places}")

    # ---- most urgent statements
    doc.heading("Most urgent statements", 1)
    urgent = list(
        review_data.statements(batch, verified)
        .select_related("document")
        .prefetch_related("cites__document")
        .order_by("-urgency", "pk")[:STATEMENTS]
    )
    if not urgent:
        doc.paragraph("No key statement.")
    for statement in urgent:
        doc.bullet(f"[{statement.urgency}] {statement.text} {_statement_cite(statement)}")

    # ---- open action points by owner
    doc.heading("Open action points by owner", 1)
    points = list(
        review_data.action_points(batch, verified)
        .filter(status="open")
        .select_related("document")
        .prefetch_related("cites__document")
        .order_by("owner_text", "deadline_date", "pk")
    )
    if not points:
        doc.paragraph("No open action point.")
    by_owner: dict[str, list] = defaultdict(list)
    for point in points:
        by_owner[point.owner_text].append(point)
    for owner, owned in sorted(by_owner.items(), key=lambda item: (-len(item[1]), item[0])):
        doc.heading(f"{owner} ({len(owned)})", 2)
        for point in owned[:ACTIONS_PER_OWNER]:
            cited = [f for f in point.cites.all() if f.verdict != Verdict.REJECTED]
            where = review_data.cite(cited[0]) if cited else f"({point.document.title})"
            deadline = f" By {point.deadline_text}." if point.deadline_text else ""
            doc.bullet(f"{point.action}{deadline} Priority: {point.get_priority_display()}. {where}")
        if len(owned) > ACTIONS_PER_OWNER:
            doc.paragraph(f"{len(owned) - ACTIONS_PER_OWNER} more on the Actions tab.", "Small")

    # ---- coverage
    doc.heading("Coverage", 1)
    single = found.coverage()
    if not found.themes:
        doc.paragraph("No recurring theme to check.")
    elif not single:
        doc.paragraph(
            "Every recurring theme is raised in more than one batch (more than one kind of document)."
        )
    else:
        doc.paragraph(
            f"{len(single)} of {len(found.themes)} recurring themes rest on one batch only, one kind of "
            "document with no independent corroboration yet:"
        )
        for theme in single:
            doc.bullet(
                f"{theme.topic.path}: {next(iter(theme.batches.values()))} ({theme.n_documents} documents)"
            )

    # ---- method
    doc.heading("Method", 1)
    doc.paragraph(
        "The documents were read by the AI into findings (challenges, recommendations, observations and "
        "action points), each with the words of the document that support it, the place, the date and a "
        "topic. NeuroDB then looked for each quote in the text to give its page, matched the place to "
        "Lebanon's governorates and districts and read the date, without AI."
    )
    doc.paragraph(
        "Evidence score (0–100), computed by NeuroDB, never by the AI: the quote found in the text 45, "
        "reported by the document rather than interpreted 25, dated 10, placed 10, given a topic other than "
        "Other 10."
    )
    reviewed = counted.exclude(verdict=Verdict.UNREVIEWED).count()
    every = review_data.all_findings(batch)
    rejected = every.filter(verdict=Verdict.REJECTED).count()
    doc.paragraph(
        f"Review: {every.count()} findings in all; {reviewed} of the {tiles['findings']} used were reviewed "
        f"by a person, and {rejected} rejected findings are left out of this report."
    )
    doc.paragraph(
        "Verified only was on: only accepted findings and statements are used."
        if verified
        else "Verified only was off: findings not reviewed yet are used along with accepted ones."
    )
    doc.paragraph(
        "Themes are topics ranked by the number of distinct documents raising them, not by how often they "
        "are mentioned. Over time, from the years the findings carry (else their document's year): "
        "persistent (every year up to the latest), recurring (again after a gap), emerging (the latest year "
        "only), no longer raised (not in the latest year). Repeated findings are findings of different "
        f"documents whose words (small words apart) are at least {round(review_data.SIMILAR * 100)}% alike."
    )
    return doc.save()
