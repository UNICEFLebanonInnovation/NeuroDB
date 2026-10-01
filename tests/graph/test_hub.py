"""The knowledge hub: one place where everything NeuroDB holds is named and linked across sources, the
assistant lookups over it, and the library and CPD documents read into the knowledge base."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.assistant.tools import ToolInputError
from neurodb.core.models import SyncRun
from neurodb.cpd.models import CountryProgramme, CPDocument
from neurodb.graph import build, builders, query
from neurodb.graph.models import Edge, Entity
from neurodb.knowledge.management.commands.index_documents import run as index_documents
from neurodb.knowledge.models import Document
from neurodb.library.models import Map, Resource
from tests.graph.conftest import NUMBER


def _e(kind, key) -> Entity:
    return Entity.objects.get(kind=kind, key=str(key))


def _linked(source, relation, target) -> bool:
    return Edge.objects.filter(source=_e(*source), relation=relation, target=_e(*target)).exists()


# ------------------------------------------------------------------------------------- build
def test_the_hub_names_and_links_every_source(world):
    run = build.run(triggered_by="test", documents=False)
    assert run.status == SyncRun.Status.SUCCEEDED, run.details
    w = world
    pd, amel = ("programme_document", w.pd.pk), ("partner", w.amel.pk)
    assert _linked(pd, "implemented_by", amel)
    assert _linked(pd, "in_section", ("section", w.education.pk))
    assert _linked(pd, "funded_by", ("donor", "european union"))
    assert _linked(pd, "has_grant", ("grant", "SC220001"))
    assert _linked(("donor", "european union"), "funds_through", ("grant", "SC220001"))
    # the PD's village is walked up to its district and governorate; the two gazetteers merge by name
    assert _linked(pd, "takes_place_in", ("district", "halba"))
    assert _linked(pd, "takes_place_in", ("governorate", "akkar"))
    assert _linked(("district", "halba"), "part_of", ("governorate", "akkar"))
    assert "LB1" in _e("governorate", "akkar").aliases
    assert _linked(amel, "reports_in", ("database", w.database.pk))
    assert _linked(("master_indicator", w.master.pk), "part_of", ("database", w.database.pk))
    assert _linked(pd, "contributes_to", ("cpd_output", w.output.pk))
    assert _linked(("cpd_indicator", w.indicator.pk), "linked_to", pd)
    assert _linked(("cpd_indicator", w.indicator.pk), "measures", ("cpd_output", w.output.pk))
    assert _linked(("cpd_outcome", w.output.outcome_id), "in_section", ("section", w.education.pk))
    assert _linked(("document", w.note.pk), "mentions", ("partner", w.care.pk))
    library = Entity.objects.get(kind="document", name="Out-of-school study")
    assert library.key.startswith("library:") and _linked(
        ("document", library.key), "in_section", ("section", w.education.pk)
    )
    assert _linked(("makani_centre", 7), "run_by", amel)  # the partner's short name
    assert _linked(("makani_centre", 7), "located_in", ("governorate", "akkar"))
    assert _linked(("review_finding", "pd_ending:LEB/PCA2026005/PD2026012"), "about", pd)
    assert _linked(("review_finding", "ap_overdue:CARE"), "about", ("partner", w.care.pk))
    assert Entity.objects.filter(kind="map", name="Schools map").exists()


def test_every_lookup_the_hub_gives_is_a_valid_tool_call(world):
    build.run(triggered_by="test", documents=False)
    lookups = list(Entity.objects.exclude(lookup={}).values_list("lookup", flat=True))
    assert len({lk["tool"] for lk in lookups}) >= 8
    for lookup in lookups:
        tools.validate(lookup["tool"], lookup["args"])  # raises on an unknown tool or bad arguments


def test_a_rebuild_drops_what_is_gone_and_a_failing_source_only_marks_the_run_partial(world, monkeypatch):
    build.run(triggered_by="test", documents=False)
    Map.objects.all().delete()
    monkeypatch.setattr(
        builders, "SOURCES", [*builders.SOURCES, ("broken", lambda c, n: 1 / 0)]
    )  # fmt: skip
    run = build.run(triggered_by="test", documents=False)
    assert run.status == SyncRun.Status.PARTIAL and "broken" in run.details["failed_sources"]
    assert not Entity.objects.filter(kind="map").exists()
    assert Entity.objects.filter(kind="partner").count() == 2


# ------------------------------------------------------------------------------------- query
def test_find_matches_numbers_short_names_and_words(world):
    build.run(triggered_by="test", documents=False)
    assert [e.key for e in query.find("LEB/PCA2026005/PD2026012")] == [str(world.pd.pk)]
    assert query.find("AMEL", kinds=["partner"])[0].key == str(world.amel.pk)
    assert query.find("2500212345")[0].kind == "partner"  # vendor number
    assert any(e.kind == "cpd_output" for e in query.find("access learning"))
    assert query.find("") == [] and query.find("nothing like this anywhere") == []


def test_reach_follows_two_links_and_says_through_what(world):
    build.run(triggered_by="test", documents=False)
    donors = query.reach(_e("governorate", "akkar"), "donor")
    assert [d["name"] for d in donors] == ["European Union"]
    assert donors[0]["through"] == [f"{NUMBER} Education support in the north"]
    partners = {p["name"]: p["connection"] for p in query.reach(_e("governorate", "akkar"), "partner")}
    assert partners["Amel Association International"].startswith("through")
    groups = {(g["link"], g["kind"]) for g in query.neighbours(_e("partner", world.amel.pk))}
    assert ("implements", "Programme document") in groups and ("runs", "Makani centre") in groups


# ------------------------------------------------------------------------------- assistant
def test_the_assistant_finds_profiles_and_connects_across_sources(world):
    build.run(triggered_by="test", documents=False)
    found = tools.run("find_anything", tools.validate("find_anything", {"text": "AMEL"}))
    partner = next(e for e in found["entities"] if e["kind"] == "partner")
    assert partner["lookup"] == {"tool": "partner_details", "args": {"partner_id": world.amel.pk}}
    profile = tools.run("entity_profile", {"kind": "partner", "key": partner["key"]})
    assert {"implements", "runs", "reports in"} <= {g["link"] for g in profile["linked"]}
    reached = tools.run("connected", {"kind": "partner", "key": partner["key"], "to_kind": "cpd_output"})
    assert [r["name"] for r in reached["found"]] == ["Output 1.1: Access to learning"]
    with pytest.raises(ToolInputError):
        tools.run("entity_profile", {"kind": "partner", "key": "999"})
    with pytest.raises(ToolInputError):
        tools.run("find_anything", {"text": "x", "kind": "planet"})


def test_the_new_lookups_answer_from_live_data(world):
    cp = tools.run("country_programme", {})
    assert cp["cycle"]["name"] == "Lebanon CP 2026-2028"
    output = cp["outcomes"][0]["outputs"][0]
    assert output["code"] == "1.1" and NUMBER in output["programme_documents"]
    assert tools.run("cpd_indicator", {"indicator_id": world.indicator.pk})["target"] == 30000
    review = tools.run("daily_review", {})
    assert {f["title"] for f in review["findings"]} == {"A programme document ends soon", "Overdue"}


def test_makani_wellbeing_gives_centre_totals_only(world):
    out = tools.run("makani_wellbeing", {})
    assert out["centres"] == [
        {
            "centre": "Center A",
            "partner": "AMEL",
            "governorate": "Akkar",
            "round": "",
            "children": 120,
            "children_with_open_flag": 4,
        }
    ]
    assert "registration" not in str(out)
    with pytest.raises(ToolInputError):
        tools.run("makani_wellbeing", {"month": "September"})


def test_the_lookups_say_when_a_source_has_no_data_yet(db):
    assert "note" in tools.run("country_programme", {})
    assert "note" in tools.run("youth_figures", {})
    assert "note" in tools.run("education_figures", {"programme": "makani"})
    assert "note" in tools.run("makani_wellbeing", {})
    assert "note" in tools.run("daily_review", {})
    with pytest.raises(ToolInputError):
        tools.run("education_figures", {"programme": "unknown"})


# -------------------------------------------------------------------- library and CPD documents
def test_library_publications_are_read_refreshed_and_removed(db, no_ai):
    item = Resource.objects.create(
        title="Learning study",
        publication_year="2025",
        section="Education",
        resource_file=b"Dropout in Akkar schools fell after transport support.",
        resource_file_name="study.txt",
    )
    bare = Resource.objects.create(
        title="Water brief", publication_year="2024", section="WASH", description="Wells"
    )
    assert index_documents() == {"read": 2, "failed": 0, "removed": 0}
    doc = Document.objects.get(origin="library", origin_id=item.pk)
    assert doc.status == Document.Status.READY and "transport support" in doc.text and doc.year == 2025
    assert "Wells" in Document.objects.get(origin="library", origin_id=bare.pk).text  # title and summary
    assert index_documents()["read"] == 0  # nothing changed
    bare.description = "Pipes"  # same length: the summary's hash still tells
    bare.save()
    assert index_documents()["read"] == 1
    item.published = False
    item.save()
    assert index_documents()["removed"] == 1 and not Document.objects.filter(origin_id=item.pk).exists()


def test_cpd_documents_are_read_and_only_admins_manage_them(
    client, db, roles, viewer, admin_user, media, no_ai
):
    cycle = CountryProgramme.objects.create(name="CP", start_year=2026, end_year=2028)
    source = CPDocument.objects.create(
        programme=cycle, title="Results framework", file=SimpleUploadedFile("rf.txt", b"Outcome 1: learning")
    )
    assert index_documents(origin="cpd")["read"] == 1
    doc = Document.objects.get(origin="cpd", origin_id=source.pk)
    assert "learning" in doc.text and doc.origin_url == reverse("cpd:document", args=[source.pk])
    client.force_login(admin_user)
    assert client.post(reverse("knowledge:delete", args=[doc.pk])).status_code == 403  # remove at the source
    source.delete()
    assert index_documents(origin="cpd")["removed"] == 1


def test_saving_a_publication_starts_reading_it_after_the_save(
    db, monkeypatch, settings, django_capture_on_commit_callbacks
):
    started = []
    monkeypatch.setattr("neurodb.integrations.background.start_command", lambda *a: started.append(a))
    with django_capture_on_commit_callbacks(execute=True):
        item = Resource.objects.create(title="Brief", publication_year="2025", section="Education")
    assert started == [("index_documents", "--origin", "library", "--id", str(item.pk))]
    settings.KNOWLEDGE_INDEX_ON_SAVE = False
    with django_capture_on_commit_callbacks(execute=True):
        item.save()
    assert len(started) == 1
