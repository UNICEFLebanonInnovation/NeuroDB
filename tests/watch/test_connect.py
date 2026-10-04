"""NeuroDB Watch connecting what it follows through the knowledge hub: what each open item connects to,
the situations where open items meet on one partner or grant, and the What's new changes around them
added to their stories."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from neurodb.datamart.models import Grant
from neurodb.graph.models import Change, Edge, Entity
from neurodb.knowledge.models import Document
from neurodb.knowledge.models import Link as KnowledgeLink
from neurodb.partnerships.models import PCA
from neurodb.watch import connect, redact
from neurodb.watch.models import RELATED_MAX, WatchItem
from tests.graph.conftest import NUMBER, build_hub

pytestmark = pytest.mark.django_db

PD, PARTNER, GRANT, FINDING, DOCUMENT, DONOR = (
    "programme_document",
    "partner",
    "grant",
    "review_finding",
    "document",
    "donor",
)
PD_NAME = f"{NUMBER} Education support in the north"
REPORT, PD_END = "due:report:LEB/PCA2026005/PD2026012-1:7", "due:pd_end:LEB/PCA2026005/PD2026013-1"


def watch_item(key, entity_kind="", entity_key="", severity="warning", due=None, **fields) -> WatchItem:
    today = timezone.localdate()
    values = {
        "detector": "test_check",
        "kind": WatchItem.Kind.DEADLINE,
        "title": f"Open point {key}",
        "evidence": {"source": "test", "records": [{"label": "row", "date": "", "value": "", "url": ""}]},
        "first_seen_on": today,
        "last_seen_on": today,
        "changed_on": today,
        **fields,
    }
    return WatchItem.objects.create(
        key=key,
        severity=severity,
        due_date=due,
        entity_kind=entity_kind,
        entity_key=str(entity_key),
        **values,
    )


def second_pd(world, grants=(), partner=None, etl_id="12", number="LEB/PCA2026005/PD2026013-1") -> PCA:
    partner = partner or world.amel
    return PCA.objects.create(
        etl_id=etl_id,
        partner=partner,
        partner_name=partner.name,
        number=number,
        title="Protection in the south",
        status="active",
        section_names=["Education"],
        grants=list(grants),
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )


def fresh(key) -> WatchItem:
    return WatchItem.objects.get(key=key)


def refs(item, kind=None) -> list[tuple[str, str]]:
    return [(e["kind"], str(e["key"])) for e in item.related if kind is None or e["kind"] == kind]


def what_is_new(key) -> list[dict]:
    return [line for line in fresh(key).story if line.get("changes")]


def read_on(document, days_ago: int) -> Document:
    when = timezone.now() - datetime.timedelta(days=days_ago)
    Document.objects.filter(pk=document.pk).update(indexed_at=when, created_at=when)
    return document


# ---------------------------------------------------------------------------- situations
def test_two_items_on_two_pds_of_one_partner_meet_on_the_partner(world):
    pd2 = second_pd(world)
    build_hub()
    watch_item(REPORT, PD, world.pd.pk, due=datetime.date(2026, 10, 15))
    watch_item(PD_END, PD, pd2.pk, severity="critical")
    entities, edges = Entity.objects.count(), Edge.objects.count()

    situations = connect.link()

    amel = f"partner:{world.amel.pk}"
    assert [s.key for s in situations] == [amel]
    situation = situations[0]
    assert situation.item_keys == [PD_END, REPORT]  # the worst first
    assert situation.name == "Amel Association International" and situation.kind == PARTNER
    assert situation.headline == "2 open points meet on Amel Association International"
    assert situation.related[0]["key"] == str(world.amel.pk)
    assert {(PD, str(world.pd.pk)), (PD, str(pd2.pk))} <= {(e["kind"], e["key"]) for e in situation.related}
    report, ending = fresh(REPORT), fresh(PD_END)
    assert report.situation_key == ending.situation_key == amel
    # the PD it is about, its partner, the other open item, then the grant and the donor
    assert report.related[0] == {
        "kind": PD,
        "key": str(world.pd.pk),
        "name": PD_NAME,
        "url": report.related[0]["url"],
    }
    assert refs(report) == [
        (PD, str(world.pd.pk)),
        (PARTNER, str(world.amel.pk)),
        ("watch_item", PD_END),
        (GRANT, "SC220001"),
        (DONOR, "european union"),
    ]
    assert refs(ending, "watch_item") == [("watch_item", REPORT)]
    # the hub is only read
    assert (Entity.objects.count(), Edge.objects.count()) == (entities, edges)
    # what the AI may read of them
    assert redact.for_model(report)["about"] == {"kind": PD, "name": PD_NAME}
    told = redact.situation_for_model(situation)
    assert told["key"] == amel and told["items"] == [PD_END, REPORT]
    assert {"kind": PD, "name": PD_NAME} in told["connected"]


def test_a_daily_review_finding_meets_others_through_what_it_is_about(hub):
    # the review's finding about the PD, and the watch's own item on the same PD
    finding = "review:pd_ending:LEB/PCA2026005/PD2026012"
    watch_item(finding, FINDING, "pd_ending:LEB/PCA2026005/PD2026012", kind=WatchItem.Kind.CONCERN)
    watch_item(REPORT, PD, hub.pd.pk)

    situations = connect.link()

    assert [(s.key, s.item_keys) for s in situations] == [(f"partner:{hub.amel.pk}", [REPORT, finding])]
    assert refs(fresh(finding))[:3] == [
        (FINDING, "pd_ending:LEB/PCA2026005/PD2026012"),
        (PD, str(hub.pd.pk)),
        (PARTNER, str(hub.amel.pk)),
    ]
    assert ("watch_item", REPORT) in refs(fresh(finding))


def test_one_item_with_a_recent_document_about_its_partner_is_a_situation(hub):
    # the finding is about CARE, and the field visit minutes read today mention CARE
    finding = "review:ap_overdue:CARE"
    watch_item(finding, FINDING, "ap_overdue:CARE", severity="critical", kind=WatchItem.Kind.CONCERN)

    situations = connect.link()

    assert [s.key for s in situations] == [f"partner:{hub.care.pk}"]
    assert situations[0].headline == "An open point on CARE International, with a recent document"
    assert [d["key"] for d in situations[0].documents] == [str(hub.note.pk)]
    item = fresh(finding)
    assert refs(item) == [
        (FINDING, "ap_overdue:CARE"),
        (PARTNER, str(hub.care.pk)),
        (DOCUMENT, str(hub.note.pk)),
    ]
    assert item.related[2]["date"] == timezone.localdate().isoformat()


def test_a_recent_document_about_the_partner_connects_to_the_items_meeting_on_it(world):
    pd2 = second_pd(world)
    minutes = Document.objects.create(title="Minutes of 2 Oct", text="Amel was discussed.", status="ready")
    KnowledgeLink.objects.create(
        document=read_on(minutes, 2), kind="partner", object_id=world.amel.pk, label="AMEL", origin="detected"
    )
    build_hub()
    watch_item(REPORT, PD, world.pd.pk)
    watch_item(PD_END, PD, pd2.pk)

    situations = connect.link()

    assert [[d["key"] for d in s.documents] for s in situations] == [[str(minutes.pk)]]
    assert refs(fresh(REPORT), DOCUMENT) == refs(fresh(PD_END), DOCUMENT) == [(DOCUMENT, str(minutes.pk))]
    # alone, the report due still meets the minutes on its partner
    WatchItem.objects.filter(key=PD_END).update(state=WatchItem.State.GONE)
    assert [(s.key, s.item_keys) for s in connect.link()] == [(f"partner:{world.amel.pk}", [REPORT])]
    assert [s.headline for s in connect.situations_of(WatchItem.objects.all())] == [
        "An open point on Amel Association International, with a recent document"
    ]


def test_a_document_read_2_days_ago_is_connected_and_one_read_200_days_ago_is_not(world):
    recent = Document.objects.create(title="Minutes of 2 Oct", text="The PD was discussed.", status="ready")
    old = Document.objects.create(title="An old visit report", text="The PD was discussed.", status="ready")
    for document in (read_on(recent, 2), read_on(old, 200)):
        KnowledgeLink.objects.create(
            document=document,
            kind="programme_document",
            object_id=world.pd.pk,
            label=NUMBER,
            origin="detected",
        )
    build_hub()
    watch_item(REPORT, PD, world.pd.pk)

    situations = connect.link()

    assert refs(fresh(REPORT), DOCUMENT) == [(DOCUMENT, str(recent.pk))]
    expected = timezone.localdate(timezone.now() - datetime.timedelta(days=2)).isoformat()
    assert [e["date"] for e in fresh(REPORT).related if e["kind"] == DOCUMENT] == [expected]
    # one open item with a recent document mentioning its PD: a situation on its partner
    assert [(s.key, [d["key"] for d in s.documents]) for s in situations] == [
        (f"partner:{world.amel.pk}", [str(recent.pk)])
    ]


def test_an_item_whose_hub_thing_is_missing_stands_alone(hub):
    watch_item(REPORT, PD, hub.pd.pk)
    lost = watch_item(
        "due:pd_end:GONE",
        PD,
        999999,
        related=[{"kind": PARTNER, "key": str(hub.amel.pk), "name": "AMEL", "url": ""}],
        situation_key=f"partner:{hub.amel.pk}",
    )
    watch_item("rollover:2027", scope=WatchItem.Scope.ADMINS)  # the administrators' own: never connected
    watch_item("system:jobs", PD, hub.pd.pk, kind=WatchItem.Kind.SYSTEM, scope=WatchItem.Scope.ADMINS)

    situations = connect.link()

    lost.refresh_from_db()
    assert (lost.related, lost.situation_key) == ([], "")
    assert fresh("system:jobs").related == [] and fresh("rollover:2027").related == []
    # the PD's own item, alone with no news, is connected but in no situation
    assert situations == [] and fresh(REPORT).situation_key == ""
    assert refs(fresh(REPORT))[:2] == [(PD, str(hub.pd.pk)), (PARTNER, str(hub.amel.pk))]


def test_items_meeting_on_a_grant_form_a_grant_situation_and_grants_expiring_soonest_come_first(world):
    today = timezone.localdate()
    Grant.objects.create(
        datamart_id=2, name="SC230002", donor="Germany", expiry=today + datetime.timedelta(days=20)
    )
    Grant.objects.create(
        datamart_id=3, name="SC240003", donor="Japan", expiry=today + datetime.timedelta(days=300)
    )
    PCA.objects.filter(pk=world.pd.pk).update(grants=["SC220001", "SC240003", "SC230002"])
    pd_care = second_pd(world, grants=["SC230002"], partner=world.care)
    build_hub()
    watch_item("due:grant:SC230002", GRANT, "SC230002", severity="critical")
    watch_item(REPORT, PD, world.pd.pk)
    watch_item(PD_END, PD, pd_care.pk)

    situations = connect.link()

    assert [(s.key, s.label) for s in situations] == [("grant:SC230002", "grant SC230002")]
    assert situations[0].headline == "3 open points meet on grant SC230002"
    assert situations[0].item_keys == ["due:grant:SC230002", PD_END, REPORT]  # the worst, then by key
    assert refs(fresh(REPORT), GRANT) == [(GRANT, "SC230002"), (GRANT, "SC240003"), (GRANT, "SC220001")]
    grant = fresh("due:grant:SC230002")
    assert refs(grant)[0] == (GRANT, "SC230002") and (DONOR, "germany") in refs(grant)
    assert grant.related[0]["date"] == (today + datetime.timedelta(days=20)).isoformat()


def test_an_item_keeps_at_most_8_connections_the_other_open_items_first(hub):
    watch_item(f"due:report:{NUMBER}:0", PD, hub.pd.pk, due=datetime.date(2026, 10, 10))
    with CaptureQueriesContext(connection) as one:
        connect.link()
    for n in range(1, 10):
        watch_item(f"due:report:{NUMBER}:{n}", PD, hub.pd.pk, due=datetime.date(2026, 10, 10 + n))

    with CaptureQueriesContext(connection) as ten:
        situations = connect.link()

    assert len(ten) == len(one) <= 15  # the hub is read in the same few queries, however many items

    item = fresh(f"due:report:{NUMBER}:0")
    assert len(item.related) == RELATED_MAX
    assert refs(item)[:2] == [(PD, str(hub.pd.pk)), (PARTNER, str(hub.amel.pk))]
    assert [key for _, key in refs(item, "watch_item")] == [f"due:report:{NUMBER}:{n}" for n in range(1, 7)]
    assert [len(s.items) for s in situations] == [10] and len(situations[0].related) <= RELATED_MAX


def test_linking_again_gives_the_same_result_and_writes_nothing(world):
    pd2 = second_pd(world)
    build_hub()
    watch_item(REPORT, PD, world.pd.pk)
    watch_item(PD_END, PD, pd2.pk)
    watch_item("review:ap_overdue:CARE", FINDING, "ap_overdue:CARE", kind=WatchItem.Kind.CONCERN)
    first = connect.link()
    stored = {i.key: (i.related, i.situation_key) for i in WatchItem.objects.all()}

    with CaptureQueriesContext(connection) as queries:
        again = connect.link()

    writes = [q["sql"] for q in queries if q["sql"].split()[0].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert writes == []
    assert {i.key: (i.related, i.situation_key) for i in WatchItem.objects.all()} == stored
    assert [(s.key, s.item_keys, s.related) for s in again] == [
        (s.key, s.item_keys, s.related) for s in first
    ]


def test_an_item_that_closed_leaves_its_situation(world):
    pd2 = second_pd(world)
    build_hub()
    watch_item(REPORT, PD, world.pd.pk)
    watch_item(PD_END, PD, pd2.pk)
    connect.link()
    WatchItem.objects.filter(key=PD_END).update(state=WatchItem.State.CLOSED)

    assert connect.link() == []

    assert fresh(PD_END).situation_key == "" and fresh(PD_END).related  # what it connected to is kept
    assert fresh(REPORT).situation_key == "" and ("watch_item", PD_END) not in refs(fresh(REPORT))


def test_an_empty_hub_raises_nothing(db):
    watch_item(REPORT, PD, 1)
    watch_item("due:grant:SC220001", GRANT, "SC220001")
    watch_item("no:hub:thing")

    assert connect.link() == []
    assert connect.changes_since(0) == 0 and connect.changes_since(7) == 7
    assert all(i.related == [] and i.situation_key == "" for i in WatchItem.objects.all())
    assert connect.situations_of(WatchItem.objects.all()) == []


# ---------------------------------------------------------------------------- stored situations
def test_the_stored_situations_are_found_again_without_the_hub_and_narrowed_to_an_audience(world):
    pd2 = second_pd(world)
    build_hub()
    watch_item(REPORT, PD, world.pd.pk)
    watch_item(PD_END, PD, pd2.pk, severity="critical")
    linked = connect.link()

    with CaptureQueriesContext(connection) as queries:
        stored = connect.situations_of(WatchItem.objects.all())

    # the items and the recent changes; the hub's things and links are not read
    assert len(queries) == 2 and not [
        q for q in queries if "graph_entity" in q["sql"] or "graph_edge" in q["sql"]
    ]
    assert [(s.key, s.name, s.item_keys) for s in stored] == [(s.key, s.name, s.item_keys) for s in linked]
    assert stored[0].related[0]["kind"] == PARTNER
    # one person who sees one of the two: no situation left for them
    assert linked[0].restricted([REPORT]) is None
    assert connect.situations_of([fresh(REPORT)]) == []
    assert linked[0].restricted([REPORT, PD_END]).item_keys == [PD_END, REPORT]


# ---------------------------------------------------------------------------- What's new in the stories
def test_a_pd_status_change_is_added_to_the_story_once_over_two_passes(hub):
    watch_item(REPORT, PD, hub.pd.pk)
    PCA.objects.filter(pk=hub.pd.pk).update(status="suspended")
    build_hub()
    change = Change.objects.get(kind=PD, key=str(hub.pd.pk), op="changed")
    assert change.notable

    situations = connect.link()
    stats: dict = {}
    watermark = connect.changes_since(0, stats=stats)

    assert watermark == max(Change.objects.values_list("pk", flat=True))
    lines = what_is_new(REPORT)
    assert [line["text"] for line in lines] == [f"What's new: {PD_NAME}: status active → suspended"]
    assert lines[0]["changes"] == [change.pk] and lines[0]["on"] == timezone.localdate().isoformat()
    assert stats["added"] == 1 and stats["items"] == 1 and stats["read"] >= 1
    # the change makes the item alone a situation on its partner
    assert [(s.key, s.change_lines) for s in situations] == [
        (f"partner:{hub.amel.pk}", [f"{PD_NAME}: status active → suspended"])
    ]
    assert situations[0].headline == "An open point on Amel Association International, with a recent change"
    # the next pass reads after the watermark; a pass repeated from the old one adds nothing either
    assert connect.changes_since(watermark) == watermark
    assert connect.changes_since(0) == watermark
    assert len(what_is_new(REPORT)) == 1
    assert [s.change_lines for s in connect.situations_of(WatchItem.objects.all())] == [
        situations[0].change_lines
    ]


def test_a_daily_review_finding_change_is_left_to_the_review(hub):
    finding = "review:ap_overdue:CARE"
    watch_item(finding, FINDING, "ap_overdue:CARE", kind=WatchItem.Kind.CONCERN)
    connect.link()
    change = Change.objects.create(
        detected_at=timezone.now(),
        kind=FINDING,
        key="ap_overdue:CARE",
        name="Overdue action points",
        op=Change.Op.CHANGED,
        fields={"severity": ["warning", "critical"]},
        notable=True,
    )

    assert connect.changes_since(0) == change.pk
    assert what_is_new(finding) == []


def test_only_notable_recent_changes_around_open_items_are_told(hub):
    watch_item(REPORT, PD, hub.pd.pk)
    watch_item("closed:item", PD, hub.pd.pk, state=WatchItem.State.CLOSED)
    connect.link()
    now = timezone.now()

    def change(key, notable=True, days_ago=0, **values):
        return Change.objects.create(
            detected_at=now - datetime.timedelta(days=days_ago),
            kind=values.pop("kind", PARTNER),
            key=key,
            name=values.pop("name", "Amel Association International"),
            op=values.pop("op", Change.Op.CHANGED),
            notable=notable,
            **values,
        )

    told = change(str(hub.amel.pk), fields={"risk_rating": ["Low", "High"]})
    change(str(hub.amel.pk), notable=False, fields={"type": ["a", "b"]})  # minor
    change(str(hub.amel.pk), days_ago=30, fields={"risk_rating": ["Medium", "Low"]})  # too old
    change(str(hub.care.pk), name="CARE International", fields={"risk_rating": ["Low", "High"]})  # elsewhere
    linked = change(  # a new link to the PD, told from the donor's side
        "germany",
        kind=DONOR,
        name="Germany",
        op=Change.Op.LINKED,
        link={"relation": "funds", "kind": PD, "key": str(hub.pd.pk), "name": PD_NAME, "url": ""},
    )
    many = [change(str(hub.pd.pk), kind=PD, name=PD_NAME, fields={"status": ["a", str(n)]}) for n in range(5)]

    connect.changes_since(0)

    lines = what_is_new(REPORT)
    assert [line["changes"] for line in lines] == [
        [told.pk],
        [linked.pk],
        *[[c.pk] for c in many[:3]],
        [c.pk for c in many[3:]],
    ]
    assert lines[0]["text"] == "What's new: Amel Association International: risk rating Low → High"
    assert lines[-1]["text"] == "What's new: and 2 more changes around it"
    assert what_is_new("closed:item") == []
