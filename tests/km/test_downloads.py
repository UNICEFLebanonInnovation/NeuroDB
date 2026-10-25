"""The KM Database's downloads (SPEC_km §3.7, the user's decision "on screen only, not in downloads"): Excel and
CSV of the filter on both tabs, their columns and the About sheet; organisations named, individual authors
counted and never named; names of people withheld in every text; the search box's words written only when
they name no author; formula escaping; the Excel limit; the queries. Fake generated workbooks only."""

from __future__ import annotations

import csv
import io
import re
import zipfile

import openpyxl
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.km import authors, exports
from neurodb.km import columns as cols
from tests.km import factory

pytestmark = pytest.mark.django_db

NAMES = (b"Doe", b"Roe", b"Poe", b"Moe", b"Jane", b"Fakename", b"Fakefirst")
EDITS = {
    ("findings", 3, "Evidence Origin"): "Secondary: from Moe (2015)",
    ("findings", 3, "Evidence Confidence Level"): "Level B, as Moe, 2016 graded it; fake spilled words " * 2,
    ("findings", 4, "Limitations"): "Write to jane.doe@fake.example.org; Dr Fakefirst Fakelast agreed.",
    # a text a spreadsheet would run ("=" would be written as a formula by the fake workbook itself)
    ("findings", 5, "Key Finding"): '@HYPERLINK("http://fake.example.org","fake")',
}


def _get(client, name="km:index", **params):
    response = client.get(reverse(name), params)
    assert response.status_code == 200, response.content[:300]
    body = b"".join(response.streaming_content) if response.streaming else response.content
    return response, body


def _xlsx_text(body: bytes) -> bytes:
    """Every part of an Excel file, unpacked (the file itself is compressed)."""
    with zipfile.ZipFile(io.BytesIO(body)) as book:
        return b"".join(book.read(name) for name in book.namelist())


def _sheets(body: bytes) -> dict[str, list[tuple]]:
    book = openpyxl.load_workbook(io.BytesIO(body), read_only=True)
    return {ws.title: list(ws.iter_rows(values_only=True)) for ws in book.worksheets}


def _row(header: tuple, values: tuple) -> dict:
    """A row of a sheet read back (the reader leaves out the empty cells at its end)."""
    return dict(zip(header, (*values, *(None,) * (len(header) - len(values))), strict=True))


def _csv(body: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))


@pytest.fixture
def loaded(db):
    return factory.load(factory.workbook(documents=3, findings=6, edits=EDITS))


def test_the_excel_of_the_findings(client_viewer, loaded):
    response, body = _get(client_viewer, export="xlsx", pillar="P1")
    assert re.fullmatch(
        r'attachment; filename="km-database-findings_\d{4}-\d{2}-\d{2}\.xlsx"',
        response["Content-Disposition"],
    )
    sheets = _sheets(body)
    assert list(sheets) == ["Findings", "About"]
    header, *rows = sheets["Findings"]
    assert header == exports.FINDING_COLUMNS
    assert "publishing_organisations" in header and "item_title" in header
    assert not any("Publishing Entity" in str(h) or "entity" in str(h) for h in header)
    assert len(rows) == 3  # P1: findings 1, 3 and 5
    row = _row(header, rows[0])
    assert row["finding_id"] == factory.finding_id(1, 1) and row["document_id"] == "TST-0001"
    assert row["publishing_organisations"] == "Fakeorg Institute + 2 authors"
    assert row["publication_year"] == 2022  # a number
    assert row["early_adolescence"] == "Yes" and row["prenatal"] == "No"
    assert row["study_period"] == "2023-02-01"
    about = dict(sheets["About"][1:])
    assert sheets["About"][0][0] == "KM Database"
    assert about["Prepared on"] == "1 Oct 2026" and "Loaded on" in about
    assert about["Filter"] == "Pillar: P1" and about["Rows"] == 3
    assert (
        about["Publishing organisations"]
        == "Organisations only. Individual authors are counted, never named."
    )
    assert about["Names of people"] == "Names of people inside the texts are withheld."


def test_the_csv_of_the_findings_and_both_files_of_the_documents(client_viewer, loaded):
    response, body = _get(client_viewer, export="csv")
    assert response["Content-Type"].startswith("text/csv")
    rows = _csv(body)
    assert list(rows[0]) == list(exports.FINDING_COLUMNS) and len(rows) == 6
    shown = {r["finding_id"]: r["publishing_organisations"] for r in rows}
    assert shown[factory.finding_id(1, 1)] == "Fakeorg Institute + 2 authors"
    assert shown[factory.finding_id(2, 1)] == "Fakeorg University"
    response, body = _get(client_viewer, "km:documents", export="xlsx")
    assert re.search(r'filename="km-database-documents_[\d-]+\.xlsx"', response["Content-Disposition"])
    header, *rows = _sheets(body)["Documents"]
    assert header == exports.DOCUMENT_COLUMNS and len(rows) == 3
    first = _row(header, rows[0])
    assert first["publishing_organisations"] == "Fakeorg Institute + 2 authors"
    assert first["number_of_findings"] == 2 and first["publication_year"] == 2021
    _, body = _get(client_viewer, "km:documents", export="csv", q="Fake document title 3")
    assert {r["document_id"] for r in _csv(body)} >= {"TST-0003"}


def test_no_author_name_is_in_any_download(client_viewer, loaded):
    bodies = [
        _xlsx_text(_get(client_viewer, export="xlsx")[1]),
        _get(client_viewer, export="csv")[1],
        _xlsx_text(_get(client_viewer, "km:documents", export="xlsx")[1]),
        _get(client_viewer, "km:documents", export="csv")[1],
    ]
    for body in bodies:
        for name in NAMES:
            assert name not in body, name
    csv_rows = {r["finding_id"]: r for r in _csv(bodies[1])}
    row = csv_rows[factory.finding_id(1, 1)]
    assert row["methods_and_sample"].startswith(
        "[name withheld] et al. (2021) asked fake.example.org/x"
    )  # a link
    assert "+961 3 123 456" in row["methods_and_sample"]  # a number is kept
    assert "“…” – [name withheld], 34" in row["methods_and_sample"]
    assert row["finding_analysis"] == (
        "Fake analysis, as [name withheld] (2018) found (Fakeorg Institute, 2020; [name withheld], 2019)."
    )
    assert row["limitations"] == "Fake limitations ([name withheld], 2017, p. 4)."
    assert "<5 years (February 2023)" in row["key_finding"]
    second = csv_rows[factory.finding_id(2, 1)]
    assert second["evidence_origin"] == "Secondary: from [name withheld] (2015)"
    assert second["evidence_confidence_level"].startswith("Level B, as [name withheld], 2016 graded it")
    assert "Evidence Confidence Level" in second["values_to_check"]
    third = csv_rows[factory.finding_id(3, 1)]
    assert third["limitations"] == "Write to [email withheld]; [name withheld] agreed."


@pytest.mark.parametrize(("q", "fmt"), [("Doe", "xlsx"), ("Jane Doe", "csv"), ("roe", "xlsx")])
def test_the_search_words_are_never_written_when_they_name_an_author(client_viewer, loaded, q, fmt):
    _, body = _get(client_viewer, export=fmt, q=q)
    text = _xlsx_text(body) if fmt == "xlsx" else body
    for name in NAMES:
        assert name not in text and name.lower() not in text.replace(b"[name withheld]", b""), name
    if fmt == "xlsx":
        about = dict(_sheets(body)["About"][1:])
        assert about["Filter"] == "Search: (your words are not written here: one of them names an author)"


def test_an_ordinary_search_is_written_with_the_filter(client_viewer, loaded):
    _, body = _get(client_viewer, export="xlsx", q="zqkmtoken", age="early_adolescence", confidence="none")
    about = dict(_sheets(body)["About"][1:])
    assert about["Filter"] == "Confidence: Not given; Age groups: Ages 10-14; Search: zqkmtoken"
    _, body = _get(
        client_viewer, export="xlsx", document="TST-0002", sex="female", origin="P", quality="ready"
    )
    about = dict(_sheets(body)["About"][1:])
    assert about["Filter"] == (
        "Document ID: TST-0002; Girls and boys: Girls and women; Evidence: Collected by the document itself; "
        "Data quality: Ready"
    )
    assert dict(_sheets(_get(client_viewer, export="xlsx")[1])["About"][1:])["Filter"] == "None: every row"


def test_a_text_that_starts_like_a_formula_stays_a_text(client_viewer, loaded):
    _, body = _get(client_viewer, export="csv")
    rows = {r["finding_id"]: r for r in _csv(body)}
    assert rows[factory.finding_id(1, 2)]["key_finding"].startswith("'@HYPERLINK(")
    _, body = _get(client_viewer, export="xlsx")
    assert b"<f>" not in _xlsx_text(body)  # no formula in the Excel file
    header, *values = _sheets(body)["Findings"]
    cell = next(v[header.index("key_finding")] for v in values if v[0] == factory.finding_id(1, 2))
    assert cell.startswith("@HYPERLINK(")  # kept as text


def test_the_excel_file_has_a_limit(client_viewer, loaded, monkeypatch):
    monkeypatch.setattr(exports, "XLSX_MAX", 5)
    response = client_viewer.get(reverse("km:index"), {"export": "xlsx"})
    assert response.status_code == 400
    assert response.content.decode() == ("The filter holds 6 findings, more than 5: choose a pillar first.")
    assert client_viewer.get(reverse("km:index"), {"export": "csv"}).status_code == 200  # no limit


def test_a_correction_changes_the_downloads_at_once(client_viewer, loaded):
    from neurodb.km.models import KMOrganisation

    KMOrganisation.objects.create(name="Fakeorg University", kind="person")
    rows = {r["finding_id"]: r for r in _csv(_get(client_viewer, export="csv")[1])}
    assert rows[factory.finding_id(2, 1)]["publishing_organisations"] == "Authors: 1"


def test_a_download_reads_a_fixed_number_of_queries(client_viewer, loaded):
    def count(**params) -> int:
        with CaptureQueriesContext(connection) as captured:
            _get(client_viewer, **params)
        return len(captured.captured_queries)

    _get(client_viewer, export="csv")  # the session and the menu
    authors.forget()
    cold = count(export="csv")
    warm = count(export="csv")
    assert cold == warm + 2  # on a miss: the corrections, then both sheets' authors
    assert count(export="xlsx") == warm


def test_the_columns_name_no_flag_by_its_header():
    assert [c for c in exports.FINDING_COLUMNS if c in {f.field for f in cols.FLAGS}] == [
        f.field for f in cols.FLAGS
    ]
    assert all(re.fullmatch(r"[a-z_]+", c) for c in (*exports.FINDING_COLUMNS, *exports.DOCUMENT_COLUMNS))


INSIDE = {
    # an author named inside an organisation part (the shape of a real entry, with fake names)
    ("findings", 2, "Publishing Entity / Author"): (
        "Fakeorg Institute; author Jane Fakename. Published in 'The Fake Monitor' with Fakeorg International "
        "under the FAKE project"
    ),
    # a document's own author introduced by a role word
    ("documents", 3, "Publishing Entity / Authors"): "Fakeorg Country Office; consultant John Fakeroe",
    ("findings", 3, "Methods and Sample"): "Done by Fakeorg Country Office and consultant John Fakeroe.",
    # citations written "Name YEAR", without a comma
    ("findings", 5, "Methods and Sample"): (
        "Trials in Lebanon (Fakecited 2010, Fakeother 2020); a fake (FAKEORG, Fakeone and Faketwo 2020) "
        "design; (Fakesolo 2002 methodology)."
    ),
}
INSIDE_NAMES = (b"Fakename", b"Fakeroe", b"Fakecited", b"Fakeother", b"Fakeone", b"Faketwo", b"Fakesolo")


def test_no_name_inside_an_organisation_part_or_a_citation_without_a_comma_is_downloaded(client_viewer):
    factory.load(factory.workbook(documents=3, findings=6, edits=INSIDE))
    _, body = _get(client_viewer, export="csv")
    for name in INSIDE_NAMES:
        assert name not in body, name
    rows = {r["finding_id"]: r for r in _csv(body)}
    assert rows[factory.finding_id(1, 1)]["publishing_organisations"] == (
        "Fakeorg Institute; Fakeorg International under the FAKE project + 1 author"
    )
    assert rows[factory.finding_id(2, 1)]["methods_and_sample"] == (
        "Done by Fakeorg Country Office and consultant [name withheld]."
    )
    assert rows[factory.finding_id(1, 2)]["methods_and_sample"] == (
        "Trials in Lebanon ([name withheld] 2010, [name withheld] 2020); a fake (FAKEORG, [name withheld] and "
        "[name withheld] 2020) design; ([name withheld] 2002 methodology)."
    )
    _, body = _get(client_viewer, "km:documents", export="csv")
    assert b"Fakeroe" not in body
    assert {r["document_id"]: r for r in _csv(body)}["TST-0002"]["publishing_organisations"] == (
        "Fakeorg Country Office + 1 author"
    )


TYPE = "Desk review by Dr Fakefirst Fakelast (Moe, 2016)"


def test_the_about_sheet_never_writes_a_document_type_that_names_a_person(client_viewer):
    factory.load(
        factory.workbook(documents=2, findings=4, edits={("documents", 2, "Document / Study Type"): TYPE})
    )
    _, body = _get(client_viewer, export="xlsx", type=TYPE)
    text = _xlsx_text(body)
    assert b"Moe" not in text and b"Fakelast" not in text and b"Fakefirst" not in text
    about = dict(_sheets(body)["About"][1:])
    assert about["Filter"] == "Document type: (not written here: one of them names a person)"
    _, body = _get(client_viewer, export="xlsx", type="Fake study")
    assert dict(_sheets(body)["About"][1:])["Filter"] == "Document type: Fake study"
