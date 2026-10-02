"""Periodic reports: editions recognised by their name, their charts read from where the labels sit,
their other figures listed by the AI (kept only when printed on the page), the figures lined up over
time across editions, the pages, adding many editions at once, and Ask NeuroDB's lookups and charts."""

import datetime
import io
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import charts, tools
from neurodb.knowledge import indexing, pdf_layout, periodic
from neurodb.knowledge.models import Document, ReportFigure, ReportSeries
from tests.assistant.test_assistant import ENABLED, _ask, _events, _outputs, call, reply, say

pytestmark = pytest.mark.django_db

NAME = "ESCALATION OF HOSTILITIES - LEBANON 2026 - UNICEF SNAPSHOT - {date}-NUM-{n}"
HEADER = [
    ("UNICEF LEBANON", 100, 810, False),
    ("Escalation of hostilities 2026 Snapshot", 100, 795, False),
]


# ------------------------------------------------------------------------------- a drawn report
def drawn_pdf(*pages: list[tuple[str, float, float, bool]]) -> bytes:
    """A PDF with each text drawn where given, 45 degrees up when ``rotated`` (as chart dates are)."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for items in pages:
        ops = []
        for text, x, y, rotated in items:
            matrix = "0.53 0.53 -0.53 0.53" if rotated else "1 0 0 1"
            safe = text.replace("(", "[").replace(")", "]")
            ops.append(f"BT /F1 9 Tf {matrix} {x} {y} Tm ({safe}) Tj ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
        content = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 630 842] /Contents {content} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n{obj}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def snapshot(number: int, issued: str, values: list[int], killed: str = "4,321") -> bytes:
    """An edition like the UNICEF snapshot: headline figures and an indicator on page 1, a chart of
    IDPs outside shelters (internal use) on page 2, its labels drawn twice as in the real report."""
    head = [*HEADER, (f"# {number} - Issued {issued}", 100, 780, False)]
    page1 = [
        *head,
        ("People Killed", 100, 700, False),
        (f"{killed} in total", 100, 680, False),
        ("250 children", 100, 660, False),
        ("# children vaccinated", 100, 600, False),
        ("82%", 300, 600, False),
        ("T:10,000", 360, 600, False),
    ]
    dates = [("14 April", 80), ("17 Apr...", 120), ("26 July", 160), ("08...", 200), ("26 August", 240)]
    chart = [("Trends of IDPs outside Shelters - IOM - Internal use", 100, 740, False)]
    chart += [("IDPs outside shelters", 150, 700, False), ("144K", 50, 650, False), ("13K", 50, 520, False)]
    for (label, x), value in zip(dates, values, strict=True):
        chart.append((label, x, 490, True))
        for _ in range(2):  # drawn twice at the same place
            chart.append((f"{value}", x + 5, 520 + value / 10000, False))
    chart += [("IDPs outside shelters", 250, 515, False)] * 2 + [("Date", 160, 465, False)]
    return drawn_pdf(page1, [*head, *chart])


def edition(number: int, issued: str, iso: str, values: list[int], **kw) -> Document:
    data = snapshot(number, issued, values, **kw)
    doc = Document(title=NAME.format(date=iso, n=number), periodic=True)
    doc.file.save("edition.pdf", ContentFile(data), save=False)
    periodic.assign(doc)
    doc.save()
    return doc


class FakeAI:
    """The AI listing figures: what a model would read from the page, plus one number not printed."""

    def __init__(self, figures):
        self.figures, self.requests = figures, []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.requests.append(params)
        return SimpleNamespace(output_text=json.dumps({"figures": self.figures}))


def figure(**kw):
    base = {
        "page": 1, "group": "Casualties", "metric": "People killed", "breakdown": "", "value": "4,321",
        "is_percent": False, "target": "", "unit": "people", "as_of": "", "period": "", "source": "",
        "internal_use": False, "quote": "",
    }  # fmt: skip
    return {**base, **kw}


AI_FIGURES = [
    figure(as_of="2026-09-26", source="Ministry of Public Health"),
    figure(breakdown="children", value="250"),
    figure(group="Health", metric="# children vaccinated", value="82%", is_percent=True, target="T:10,000"),
    figure(group="Health", metric="# children dewormed", value="99,999"),  # not on the page: left out
]


@pytest.fixture
def ai(monkeypatch):
    def install(figures=AI_FIGURES):
        fake_ai = FakeAI(figures)
        monkeypatch.setattr("neurodb.assistant.agent.client", lambda: fake_ai)
        return fake_ai

    return install


@pytest.fixture
def quiet(monkeypatch, media):
    monkeypatch.setattr("neurodb.graph.refresh.request", lambda *a, **k: None)
    monkeypatch.setattr("neurodb.knowledge.indexing.summarise", lambda d: False)


# ------------------------------------------------------------------------------------ recognise
@pytest.mark.parametrize(
    ("name", "key", "number", "issued"),
    [
        (
            "088e823b-ESCALATION_OF_HOSTILITIES_-_LEBANON_2026_-_UNICEF_SNAPSHOT_-_02_October-2026-NUM-37.pdf",
            "escalation-of-hostilities-lebanon-2026-unicef-snapshot",
            37,
            datetime.date(2026, 10, 2),
        ),
        (
            "625d24ec-ESCALATION_OF_HOSTILITIES_-_LEBANON_2026_-_UNICEF_SNAPSHOT_-_5_May_2026_-_NUM_23.pdf",
            "escalation-of-hostilities-lebanon-2026-unicef-snapshot",
            23,
            datetime.date(2026, 5, 5),
        ),
        (
            "ESCALATION_OF_HOSTILITIES_-_LEBANON_2026_-_UNICEF_SNAPSHOT_-_4-June-2026-NUM-27",
            "escalation-of-hostilities-lebanon-2026-unicef-snapshot",
            27,
            datetime.date(2026, 6, 4),
        ),
        ("Lebanon Flash Update No. 12 - 2026-03-04", "lebanon-flash-update", 12, datetime.date(2026, 3, 4)),
        ("Sitrep #5 (October 3, 2026)", "sitrep", 5, datetime.date(2026, 10, 3)),
    ],
)
def test_an_edition_is_known_by_its_name(name, key, number, issued):
    found = periodic.identify(name)
    assert (found.key, found.edition, found.issued_on) == (key, number, issued)


def test_a_name_without_number_or_date_is_not_an_edition_unless_its_text_says_so():
    assert periodic.identify("Education WG minutes") is None
    found = periodic.identify("Snapshot", "UNICEF LEBANON\n# 22 - Issued 15 May 2026\nPeople")
    assert (found.edition, found.issued_on) == (22, datetime.date(2026, 5, 15))
    assert periodic.identify("ESCALATION OF HOSTILITIES - UNICEF SNAPSHOT - NUM-3").name == (
        "Escalation of hostilities - UNICEF snapshot"
    )


# ---------------------------------------------------------------------------------- the charts
def test_a_chart_is_read_from_where_its_labels_sit():
    reader = pdf_layout.open_pdf(snapshot(37, "02 October 2026", [1000100, 1000200, 400300, 400400, 400500]))
    (chart,) = pdf_layout.read_charts(reader, datetime.date(2026, 10, 2))
    assert (chart.page, chart.group, chart.metric) == (2, "IDPs outside Shelters", "IDPs outside shelters")
    assert chart.source == "IOM" and chart.internal and chart.dropped == 0
    assert [(d.isoformat(), int(v)) for d, v, _ in chart.points] == [
        ("2026-04-14", 1000100),  # the ticks (144K, 13K) are left out, the doubled labels count once
        ("2026-04-17", 1000200),  # "17 Apr..." cut short
        ("2026-07-26", 400300),
        ("2026-08-08", 400400),  # "08..." takes the month that keeps the dates in order
        ("2026-08-26", 400500),
    ]


def test_a_cut_date_is_only_given_a_month_when_one_fits():
    issued = datetime.date(2026, 10, 2)
    labels = [(17, 4, None), (8, None, None), (26, 8, None)]  # 8 May, June, July or August?
    assert pdf_layout._resolve(labels, issued, {})[1] is None
    assert pdf_layout._resolve(labels, issued, {8: {8}})[1] == datetime.date(
        2026, 8, 8
    )  # "08 August" elsewhere


def test_cut_dates_at_the_start_of_an_axis_take_the_nearest_month():
    issued = datetime.date(2026, 6, 4)  # edition 27: "10…", "14…", "17…" … then "5 May"
    labels = [(10, None, None), (14, None, None), (19, None, None), (27, None, None), (5, 5, None)]
    assert [d.isoformat() for d in pdf_layout._resolve(labels, issued, {14: {4}})] == [
        "2026-04-10", "2026-04-14", "2026-04-19", "2026-04-27", "2026-05-05",
    ]  # fmt: skip


def test_the_ticks_are_a_column_and_a_value_may_sit_left_of_its_date():
    item = pdf_layout.Item
    numbers = [item("1.1M", 58.5, 665, False), item("1M", 58.5, 637, False), item("1000000", 82, 657, False)]
    edge = pdf_layout._ticks_edge(numbers, first_date_x=90.4)
    assert 58.5 < edge < 82  # edition 27: its first value is drawn left of its date label


def test_pages_of_pictures_are_named(monkeypatch):
    def page(text, images):
        return SimpleNamespace(extract_text=lambda: text, images=[object()] * images)

    head = "UNICEF LEBANON\nEscalation of hostilities 2026 Snapshot\n"
    reader = SimpleNamespace(
        pages=[
            page(head + "People killed 2,696 in total 186 children " * 3, 1),
            page(head + "TRENDS RELATED TO DISPLACED PERSONS", 4),  # the charts are pictures
            page(head + "Convoys completed 24, 16 by UNICEF, 8 joint with WFP. Source: calendar", 2),
        ]
    )
    assert pdf_layout.picture_pages(reader, []) == [2]


def test_dates_without_a_year_fall_in_the_year_up_to_the_issue():
    reader = pdf_layout.open_pdf(snapshot(3, "10 January 2027", [1, 2, 3, 4, 5]))
    (chart,) = pdf_layout.read_charts(reader, datetime.date(2027, 1, 10))
    assert chart.points[0][0] == datetime.date(2026, 4, 14) and chart.points[-1][0] == datetime.date(
        2026, 8, 26
    )


# --------------------------------------------------------------------------------- the figures
def test_an_editions_figures_are_kept_when_printed_on_their_page(quiet, ai):
    model = ai()
    doc = edition(37, "02 October 2026", "02 October-2026", [100, 200, 300, 400, 500])
    indexing.process(doc)
    doc.refresh_from_db()
    assert doc.status == "ready" and doc.figures_status == "read"
    assert (doc.series.name, doc.edition, doc.issued_on) == (
        "Escalation of hostilities - Lebanon 2026 - UNICEF snapshot",
        37,
        datetime.date(2026, 10, 2),
    )
    assert (
        doc.document_date == doc.issued_on
        and "1 figure(s) listed by the AI were left out" in doc.figures_note
    )
    killed = ReportFigure.objects.get(document=doc, metric="People killed", breakdown="")
    assert killed.value == 4321 and killed.as_of == datetime.date(2026, 9, 26) and killed.method == "text"
    vaccinated = ReportFigure.objects.get(document=doc, metric="# children vaccinated")
    assert vaccinated.is_percent and vaccinated.value == 82 and vaccinated.target == 10000
    assert vaccinated.as_of == doc.issued_on  # no date of its own: the issue date
    assert not ReportFigure.objects.filter(metric="# children dewormed").exists()
    chart = ReportFigure.objects.filter(document=doc, method="chart")
    assert chart.count() == 5 and all(f.internal and f.source == "IOM" for f in chart)
    prompt = json.dumps(model.requests[0]["input"])
    assert "Charts already read" in prompt and "IDPs outside shelters" in prompt


def test_without_the_ai_the_charts_are_still_kept(quiet, settings):
    settings.AI_ASSISTANT_ENABLED = False
    doc = edition(37, "02 October 2026", "02 October-2026", [100, 200, 300, 400, 500])
    indexing.process(doc)
    doc.refresh_from_db()
    assert doc.figures_status == "read" and "not configured" in doc.figures_note
    assert doc.figures.count() == 5


def test_editions_line_up_over_time_and_the_newest_counts_for_a_date(quiet, ai):
    model = ai()
    older = edition(36, "26 September 2026", "26 September-2026", [100, 200, 300, 400, 500])
    indexing.process(older)
    model.figures = [figure(value="4,455"), figure(breakdown="children", value="255")]
    newer = edition(37, "02 October 2026", "02 October-2026", [100, 210, 300, 400, 450], killed="4,455")
    indexing.process(newer)
    assert newer.series_id == older.series_id
    known = json.dumps(model.requests[-1]["input"])
    assert "Known measures" in known and "Casualties | People killed | children" in known
    series = ReportSeries.objects.get()
    killed = periodic.measures(series, "killed")[0]
    line = periodic.timeline(series, [killed["key"]])[killed["key"]]
    assert [(p["as_of"], p["value"], p["edition"]) for p in line] == [
        (datetime.date(2026, 9, 26), Decimal("4321"), 36),
        (datetime.date(2026, 10, 2), Decimal("4455"), 37),
    ]
    assert periodic.changes(line)["overall"]["change"] == 134
    outside = periodic.measures(series, "outside")[0]
    points = periodic.timeline(series, [outside["key"]])[outside["key"]]
    assert [int(p["value"]) for p in points] == [100, 210, 300, 400, 450]  # every date from #37, the newest
    assert points[-1]["edition"] == 37


# ------------------------------------------------------------------------- adding and the pages
def test_many_editions_are_added_at_once_and_read_oldest_first(client, editor, media, started):
    client.force_login(editor)
    files = [
        SimpleUploadedFile(NAME.format(date="02_October-2026", n=37).replace(" ", "_") + ".pdf", b"%PDF-1.4"),
        SimpleUploadedFile(NAME.format(date="15_May-2026", n=22).replace(" ", "_") + ".pdf", b"%PDF-1.4"),
    ]
    response = client.post(reverse("knowledge:add"), {"files": files, "periodic": "on", "source": "UNICEF"})
    docs = list(Document.objects.order_by("edition"))
    assert [d.edition for d in docs] == [22, 37] and all(d.periodic and d.source == "UNICEF" for d in docs)
    assert docs[0].series_id == docs[1].series_id and docs[0].issued_on == datetime.date(2026, 5, 15)
    assert started == [("index_knowledge", "--pending")]
    assert response.status_code == 302 and response.url == docs[0].series.get_absolute_url()
    # another edition of a known report is recognised without the box ticked
    one = SimpleUploadedFile(NAME.format(date="26_September-2026", n=36).replace(" ", "_") + ".pdf", b"x")
    client.post(reverse("knowledge:add"), {"files": [one]})
    assert Document.objects.get(edition=36).periodic


def test_pending_documents_are_read_oldest_edition_first(db, monkeypatch, media):
    order = []
    monkeypatch.setattr(
        "neurodb.knowledge.management.commands.index_knowledge.process",
        lambda d: (order.append(d.edition), Document.objects.filter(pk=d.pk).update(status="ready")),
    )
    series = ReportSeries.objects.create(key="s", name="S")
    for n, day in ((37, 30), (22, 1), (30, 15)):
        Document.objects.create(
            title=f"S {n}", series=series, edition=n, issued_on=datetime.date(2026, 6, day)
        )
    Document.objects.create(title="Note", text="x")
    call_command("index_knowledge", "--pending")
    assert order == [22, 30, 37, None]


def test_the_report_pages_show_editions_measures_and_trend(client_viewer, quiet, ai):
    ai()
    doc = edition(37, "02 October 2026", "02 October-2026", [100, 200, 300, 400, 500])
    indexing.process(doc)
    response = client_viewer.get(reverse("knowledge:series_index"))
    assert response.status_code == 200 and doc.series.name.encode() in response.content
    response = client_viewer.get(doc.series.get_absolute_url())
    assert response.status_code == 200
    trend = response.context["chart_data"]["trend"]
    assert trend["series"] and trend["default"].startswith("idps outside shelters")
    assert b"internal use" in response.content and b"People killed" in response.content
    response = client_viewer.get(doc.get_absolute_url())
    assert b"#37" in response.content and b"figures kept" in response.content


# --------------------------------------------------------------------------------- Ask NeuroDB
def test_the_assistant_lists_reports_and_follows_a_measure(quiet, ai):
    ai()
    indexing.process(edition(36, "26 September 2026", "26 September-2026", [100, 200, 300, 400, 500]))
    listed = tools.run("periodic_reports", {"measure": "killed"})
    assert listed["report"].startswith("Escalation") and listed["measures_found"] == 2
    assert [e["edition"] for e in listed["editions"]] == [36]
    out = tools.run("report_figures", {"measures": ["outside shelters"]})
    (m,) = out["measures"]
    assert m["internal_use"] and [v["value"] for v in m["values"]] == [100, 200, 300, 400, 500]
    assert m["overall"]["change"] == 400 and m["overall"]["change_percent"] == 400.0
    at = tools.run("report_figures", {"measures": ["killed"], "edition": 36})
    assert at["edition"] == 36 and {v["value"] for m in at["measures"] for v in m["values"]} == {4321, 250}
    with pytest.raises(tools.ToolInputError):
        tools.run("report_figures", {"measures": ["nothing like it"]})
    with pytest.raises(tools.ToolInputError):
        tools.run("report_figures", {"report": "unknown report", "measures": ["killed"]})


def test_a_chart_shows_only_figures_looked_up():
    seen = charts.numbers_in({"values": [{"value": 4321.0}, {"value": "250"}], "pct": "82%"})
    spec = charts.build({"kind": "pie", "title": "Killed", "labels": ["Adults", "Children"], "series": [
        {"name": "Killed", "values": [4321, 250]}]}, seen)  # fmt: skip
    assert spec["chart"] == "pie" and spec["data"] == [["Adults", 4321.0], ["Children", 250.0]]
    with pytest.raises(tools.ToolInputError, match="not returned by a lookup"):
        charts.build(
            {"kind": "column", "title": "x", "labels": ["a"], "series": [{"name": "s", "values": [5000]}]},
            seen,
        )
    with pytest.raises(tools.ToolInputError, match="one series"):
        charts.build(
            {"kind": "pie", "title": "x", "labels": ["a"], "series": [{"values": [250]}, {"values": [250]}]},
            seen,
        )
    line = charts.build(
        {
            "kind": "line",
            "title": "t",
            "labels": ["1", "2"],
            "series": [{"name": "k", "values": [250, None]}],
        },
        seen,
    )
    assert line["chart"] == "lines" and line["data"]["series"] == {"k": [250.0, None]}


@override_settings(**ENABLED)
def test_the_answer_carries_the_chart_drawn_from_its_lookups(client_viewer, quiet, ai, fake):
    ai()
    indexing.process(edition(36, "26 September 2026", "26 September-2026", [100, 200, 300, 400, 500]))
    model = fake(
        reply(call("report_figures", {"measures": ["outside shelters"]})),
        reply(
            call(
                "make_chart",
                {
                    "kind": "line",
                    "title": "IDPs outside shelters",
                    "labels": ["14 Apr", "29 Sep"],
                    "series": [{"name": "IDPs", "values": [100, 999]}],
                },
                "call_2",
            ),  # fmt: skip
        ),
        reply(
            call(
                "make_chart",
                {
                    "kind": "line",
                    "title": "IDPs outside shelters",
                    "labels": ["14 Apr", "29 Sep"],
                    "series": [{"name": "IDPs", "values": [100, 500]}],
                },
                "call_3",
            ),  # fmt: skip
        ),
        reply(say("They fell from 100 to 500 (internal use).")),
    )
    events = _events(_ask(client_viewer, "Chart the IDPs outside shelters"))
    refused = _outputs(model.requests[2])["call_2"]
    assert "not returned by a lookup" in refused["error"]
    drawn = [e for e in events if e["type"] == "chart"]
    assert len(drawn) == 1 and drawn[0]["spec"]["data"]["series"] == {"IDPs": [100.0, 500.0]}
    assert events[-1]["type"] == "done"
