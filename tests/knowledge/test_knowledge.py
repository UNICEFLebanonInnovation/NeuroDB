"""Knowledge base: reading files, passages, links to NeuroDB records, the AI summary, search, the
assistant tools and the pages."""

import datetime
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.assistant import tools
from neurodb.geo.models import DistrictLocation, GovernorateLocation
from neurodb.knowledge import indexing, linking, search, text
from neurodb.knowledge.models import Chunk, Document, Link
from neurodb.partnerships.models import PCA, PartnerOrganization


# ------------------------------------------------------------------------------- file builders
def pdf(*pages: str) -> bytes:
    """A small valid PDF with one line of text per page."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for body in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({body}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n{obj}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def docx(*paragraphs: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
        )
    return buffer.getvalue()


def pptx(*slides: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for n, body in enumerate(slides, start=1):
            archive.writestr(
                f"ppt/slides/slide{n}.xml",
                f'<p:sld xmlns:a="{ns}" xmlns:p="x"><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:sld>',
            )
    return buffer.getvalue()


# ------------------------------------------------------------------------------------- text
def test_files_of_every_kind_are_read_with_their_pages():
    out = text.read("Review.PDF", pdf("Dropout fell in Akkar", "Teachers were trained"))
    assert out.pages == 2 and out.text.split(text.PAGE_BREAK)[1].strip() == "Teachers were trained"
    assert text.read("note.docx", docx("First line", "Second line")).text == "First line\nSecond line"
    assert text.read("deck.pptx", pptx("Slide one", "Slide two")).pages == 2
    wb = Workbook()
    wb.active.append(["Partner", "Children"])
    wb.active.append(["AMEL", 1200])
    buffer = io.BytesIO()
    wb.save(buffer)
    assert "AMEL | 1200" in text.read("figures.xlsx", buffer.getvalue()).text
    assert text.read("notes.txt", "Café\x00 notes".encode("cp1252")).text == "Café notes"


def test_unreadable_files_say_why():
    with pytest.raises(text.TextError, match="scan"):
        text.read("scan.pdf", pdf("", ""))
    with pytest.raises(text.TextError, match="cannot be read"):
        text.read("photo.jpg", b"x")
    with pytest.raises(text.TextError, match="could not be opened"):
        text.read("broken.docx", b"not a zip")
    with pytest.raises(text.TextError, match="could not be read"):
        text.read("broken.pdf", b"%PDF-1.4 garbage")


def test_passages_overlap_and_keep_their_page():
    body = " ".join(f"Sentence {n} about schools." for n in range(400))
    parts = indexing.passages(f"Short first page{text.PAGE_BREAK}{body}")
    assert parts[0] == (1, "Short first page")
    assert all(page == 2 for page, _ in parts[1:]) and len(parts) > 5
    assert all(len(piece) <= indexing.CHUNK_CHARS for _, piece in parts)
    first, second = parts[1][1], parts[2][1]
    assert second[:40] in first  # the next passage starts inside the previous one
    assert indexing.passages("one line")[0] == (None, "one line")


# ---------------------------------------------------------------------------------- linking
@pytest.fixture
def records(db):
    partner = PartnerOrganization.objects.create(
        etl_id="1",
        name="Amel Association International",
        short_name="AMEL",
        vendor_number="2500212345",
        partner_type="CSO",
    )
    care = PartnerOrganization.objects.create(etl_id="2", name="CARE International", short_name="CARE")
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA2026005/PD2026012-1",
        title="Education support",
        status="active",
    )
    education = Section.objects.create(name="Education", code="EDU")
    akkar = GovernorateLocation.objects.create(code="LB1", name="Akkar", ai_id=1)
    north = GovernorateLocation.objects.create(code="LB2", name="North", ai_id=2)
    zahle = DistrictLocation.objects.create(code="LB41", gov_code="LB4", name="Zahle")
    return SimpleNamespace(
        partner=partner, care=care, pd=pd, education=education, akkar=akkar, north=north, zahle=zahle
    )


def test_detect_finds_names_references_sections_and_places(records):
    found = {
        (f.kind, f.object_id): f.mentions
        for f in linking.detect(
            "AMEL and Amel Association International (vendor 2500212345) run LEB/PCA2026005/PD2026012 in "
            "Akkar and Zahle under Education. Children take care of siblings. We went north."
        )
    }
    assert found[("partner", records.partner.pk)] == 3
    assert found[("programme_document", records.pd.pk)] == 1  # the amendment suffix left out
    assert ("section", records.education.pk) in found
    assert ("governorate", records.akkar.pk) in found and ("district", records.zahle.pk) in found
    assert ("partner", records.care.pk) not in found  # "care" is a word; only "CARE" is the partner
    assert ("governorate", records.north.pk) not in found  # "north" alone is a direction
    assert ("governorate", records.north.pk) in {
        (f.kind, f.object_id) for f in linking.detect("Schools in North Lebanon reopened.")
    }


def test_names_from_the_ai_resolve_to_records_only_when_unambiguous(records):
    found = {
        (f.kind, f.object_id)
        for f in linking.resolve(["Amel Association", "Unknown NGO"], ["akkar"], ["LEB/PCA2026005"])
    }
    assert found == {
        ("partner", records.partner.pk),
        ("governorate", records.akkar.pk),
        ("programme_document", records.pd.pk),
    }


# --------------------------------------------------------------------------------- indexing
class FakeResponses:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.payload, Exception):
            raise self.payload
        return SimpleNamespace(output_text=json.dumps(self.payload))


SUMMARY = {
    "summary": "Minutes of the education working group.",
    "key_points": ["Dropout fell"],
    "document_date": "2026-03",
    "organisations": ["CARE International", "Amel Association International"],
    "places": ["Zahle"],
    "references": [],
}


@pytest.fixture
def ai(monkeypatch):
    fake = SimpleNamespace(responses=FakeResponses(SUMMARY))
    monkeypatch.setattr("neurodb.assistant.agent.client", lambda: fake)
    return fake


@pytest.fixture
def no_ai(monkeypatch):
    from neurodb.assistant.agent import AssistantUnavailable

    def unavailable():
        raise AssistantUnavailable("off")

    monkeypatch.setattr("neurodb.assistant.agent.client", unavailable)


def _note(title, body, **kwargs):
    return Document.objects.create(title=title, text=body, **kwargs)


def test_a_pasted_text_is_indexed_linked_and_summarised(records, ai):
    doc = indexing.process(_note("Education WG minutes", "AMEL reported that dropout fell in Akkar schools."))
    assert doc.status == "ready" and doc.indexed_at and doc.error == ""
    assert doc.chunks.count() == 1 and Chunk.objects.filter(search_vector__isnull=False).count() == 1
    links = {(lk.kind, lk.object_id): lk.origin for lk in doc.links.all()}
    assert links[("partner", records.partner.pk)] == "detected"  # also named by the AI: the text wins
    assert links[("partner", records.care.pk)] == "ai" and links[("district", records.zahle.pk)] == "ai"
    assert doc.summary == SUMMARY["summary"] and doc.document_date == datetime.date(2026, 3, 1)
    call = ai.responses.calls[0]
    assert call["store"] is False and "<document>" in call["input"][0]["content"][1]["text"]


def test_reading_again_keeps_links_added_by_hand(records, no_ai):
    doc = indexing.process(_note("Note", "Nothing named here."))
    Link.objects.create(
        document=doc, kind="partner", object_id=records.care.pk, label="CARE", origin="manual"
    )
    indexing.process(doc)
    assert list(doc.links.values_list("origin", flat=True)) == ["manual"]


def test_without_ai_or_when_the_summary_fails_the_document_is_still_searchable(records, no_ai, monkeypatch):
    doc = indexing.process(_note("Note", "Schools reopened."))
    assert doc.status == "ready" and doc.summary == ""
    monkeypatch.setattr(
        "neurodb.assistant.agent.client",
        lambda: SimpleNamespace(responses=FakeResponses(RuntimeError("down"))),
    )
    doc = indexing.process(doc)
    assert doc.status == "ready" and "summary failed" in doc.error


def test_a_file_that_cannot_be_read_fails_with_the_reason(db, media, no_ai):
    doc = Document.objects.create(title="Scan", file=SimpleUploadedFile("scan.pdf", pdf("")))
    doc = indexing.process(doc)
    assert doc.status == "failed" and "scan" in doc.error


@pytest.fixture
def media(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


# ----------------------------------------------------------------------------------- search
@pytest.fixture
def corpus(records, no_ai):
    a = indexing.process(
        _note(
            "Education WG minutes",
            "AMEL reported that dropout of Syrian children fell in Akkar schools.",
            year=2026,
        )
    )
    b = indexing.process(
        _note("WASH assessment", "Water trucking costs rose in Zahle; schools lack latrines.", year=2025)
    )
    c = indexing.process(_note("ملاحظات", "زيارة مدرسة في عكار"))
    return SimpleNamespace(a=a, b=b, c=c)


def test_search_matches_words_stems_and_falls_back_to_any_word(corpus):
    hits = search.search("dropout school")
    assert hits[0].document.pk == corpus.a.pk  # both words ("schools" found by "school") come first
    assert [h.document.pk for h in hits[1:]] == [corpus.b.pk]  # then passages with one of them
    hits = search.search("dropout latrines")  # no passage has both: either word
    assert {h.document.pk for h in hits} == {corpus.a.pk, corpus.b.pk}
    assert [h.document.pk for h in search.search("عكار")] == [corpus.c.pk]
    assert search.search("the and") == []


def test_search_filters_by_link_section_and_year(records, corpus):
    assert [
        h.document.pk for h in search.search("schools", search.Filters(partner_id=records.partner.pk))
    ] == [corpus.a.pk]
    assert [h.document.pk for h in search.search("schools", search.Filters(year=2025))] == [corpus.b.pk]
    assert [d.pk for d in search.linked("district", records.zahle.pk)] == [corpus.b.pk]


def test_snippets_are_escaped(records, no_ai):
    indexing.process(_note("x", "Payload <script>alert(1)</script> about dropout"))
    snippet = search.search("dropout")[0].snippet()
    assert "<script>" not in snippet and "&lt;script&gt;" in snippet and "<mark>dropout</mark>" in snippet


# ------------------------------------------------------------------------------- assistant
def test_the_assistant_searches_and_reads_the_knowledge_base(records, corpus):
    names = [d["name"] for d in tools.definitions()]
    assert "search_knowledge" in names and "read_knowledge" in names
    out = tools.run("search_knowledge", {"query": "dropout", "partner": "AMEL"})
    assert out["passages"][0]["document_id"] == corpus.a.pk and "instructions" in out["note"]
    assert out["documents"][0]["url"] == reverse("knowledge:detail", args=[corpus.a.pk])
    listed = tools.run("search_knowledge", {"programme_document": "LEB/PCA2026005"})
    assert listed["passages"] == [] and [d["document_id"] for d in listed["documents"]] == []
    read = tools.run("read_knowledge", {"document_id": corpus.b.pk})
    assert read["parts"] == 1 and "Zahle" in read["text"]
    with pytest.raises(tools.ToolInputError):
        tools.run("read_knowledge", {"document_id": corpus.b.pk, "part": 2})
    assert tools._knowledge_documents("partner", records.partner.pk)[0]["title"] == "Education WG minutes"


# ------------------------------------------------------------------------------------ pages
@pytest.fixture
def editor(db, roles, records):
    user = User.objects.create_user(username="editor", email="e@example.org", password="editor-pass-123456")
    user.groups.add(Group.objects.get(name=SECTION_EDITOR))
    return user


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr("neurodb.integrations.background.start_command", lambda *a: calls.append(a))
    return calls


def test_editors_add_a_file_or_a_text_and_it_is_read_in_the_background(client, editor, media, started):
    client.force_login(editor)
    url = reverse("knowledge:add")
    assert client.get(url).status_code == 200
    response = client.post(url, {"title": "Review", "file": SimpleUploadedFile("review.pdf", pdf("Dropout"))})
    doc = Document.objects.get()
    assert response.status_code == 302 and doc.added_by == editor and doc.file.name.startswith("knowledge/")
    assert started == [("index_knowledge", "--document", str(doc.pk))]
    response = client.post(url, {"title": "Note", "text": "Pasted note"})
    assert Document.objects.filter(title="Note", text="Pasted note").exists()
    response = client.post(url, {"title": "Both", "text": "x", "file": SimpleUploadedFile("a.txt", b"y")})
    assert response.status_code == 200 and b"one of the two" in response.content
    response = client.post(url, {"title": "Exe", "file": SimpleUploadedFile("a.exe", b"y")})
    assert response.status_code == 200 and not Document.objects.filter(title="Exe").exists()


def test_viewers_read_and_search_but_do_not_add(client_viewer, corpus):
    assert client_viewer.get(reverse("knowledge:add")).status_code == 403
    response = client_viewer.get(reverse("knowledge:index"))
    assert response.status_code == 200 and b"Education WG minutes" in response.content
    response = client_viewer.get(reverse("knowledge:index"), {"q": "latrines"})
    assert b"WASH assessment" in response.content and b"Education WG minutes" not in response.content
    response = client_viewer.get(reverse("knowledge:detail", args=[corpus.a.pk]))
    assert response.status_code == 200 and b"Amel Association International" in response.content
    assert client_viewer.post(reverse("knowledge:delete", args=[corpus.a.pk])).status_code == 403


def test_the_person_who_added_it_or_an_admin_reads_again_or_removes_it(
    client, editor, admin_user, media, started
):
    doc = Document.objects.create(
        title="Mine", text="x", added_by=editor, file=SimpleUploadedFile("m.txt", b"x")
    )
    other = Document.objects.create(title="Theirs", text="x", added_by=admin_user)
    client.force_login(editor)
    assert client.post(reverse("knowledge:delete", args=[other.pk])).status_code == 403
    assert client.post(reverse("knowledge:reindex", args=[doc.pk])).status_code == 302 and started
    path = media / doc.file.name
    assert path.exists()
    client.post(reverse("knowledge:delete", args=[doc.pk]))
    assert not Document.objects.filter(pk=doc.pk).exists() and not path.exists()
    client.force_login(admin_user)
    assert client.post(reverse("knowledge:delete", args=[other.pk])).status_code == 302


def test_files_download_and_documents_show_on_partner_and_programme_pages(
    client_viewer, records, corpus, media
):
    doc = Document.objects.create(title="Report", file=SimpleUploadedFile("r.txt", b"body"))
    response = client_viewer.get(reverse("knowledge:file", args=[doc.pk]))
    assert response.status_code == 200 and b"".join(response.streaming_content) == b"body"
    for name, pk in (
        ("reports:partner_profile", records.partner.pk),
        ("reports:programme_detail", records.pd.pk),
    ):
        response = client_viewer.get(reverse(name, args=[pk]))
        assert response.status_code == 200
    assert (
        b"In the knowledge base"
        in client_viewer.get(reverse("reports:partner_profile", args=[records.partner.pk])).content
    )


def test_the_ask_page_offers_adding_to_editors_only(client, editor, viewer, settings):
    settings.AI_ASSISTANT_ENABLED = True
    client.force_login(editor)
    assert reverse("knowledge:add").encode() in client.get(reverse("assistant:ask")).content
    client.force_login(viewer)
    assert reverse("knowledge:add").encode() not in client.get(reverse("assistant:ask")).content


def test_admin_adds_a_link_by_hand(client, admin_user, records, corpus):
    client.force_login(admin_user)
    doc = corpus.b
    url = reverse("admin:knowledge_document_change", args=[doc.pk])
    assert client.get(url).status_code == 200
    existing = list(doc.links.all())
    data = {
        "title": doc.title,
        "source": "",
        "section": "",
        "year": "2025",
        "summary": "",
        "key_points": "[]",
        "document_date": "",
        "links-TOTAL_FORMS": str(len(existing) + 1),
        "links-INITIAL_FORMS": str(len(existing)),
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "1000",
    }
    for n, lk in enumerate(existing):
        data.update(
            {
                f"links-{n}-id": lk.pk,
                f"links-{n}-document": doc.pk,
                f"links-{n}-kind": lk.kind,
                f"links-{n}-object_id": lk.object_id,
            }
        )
    n = len(existing)
    data.update(
        {
            f"links-{n}-kind": "programme_document",
            f"links-{n}-object_id": records.pd.pk,
            f"links-{n}-document": doc.pk,
        }
    )
    response = client.post(url, data)
    assert response.status_code == 302, response.content[-2000:]
    link = doc.links.get(kind="programme_document")
    assert link.origin == "manual" and link.label.startswith("LEB/PCA2026005")
