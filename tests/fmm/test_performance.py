"""Hardening (stage 8b): Monitoring insights at production size. A world of 5,000 visits (15,000 finding
rows) and 100,000 checklist answer records, with its gazetteer, partners, programme documents, sites and
FM action points, is built in bulk; then:

- a full refresh takes under 60 seconds, and its peak traced memory (``tracemalloc``) stays under
  200 MB, because it runs inside the Datamart sync's process (A4 "Memory budget");
- a scores-only refresh takes under 15 seconds;
- each tab of the page answers in under 300 ms of server time, measured on a cold cache (the first view
  after a refresh or a filter change, the slowest one) as the best of three views, with the figures
  computed from the 5,000 visits and the tables analysed as autovacuum keeps them in production.

The pages the earlier stages asked to watch are timed too (the brief card, a visit page, a programme
document page, the CSV and the rules preview), against looser limits that would catch a page gone
quadratic. The timings are printed (``-s``) so that a run's figures can be read.

The test takes a few minutes; it is part of the suite on purpose (§I stage 8b)."""

from __future__ import annotations

import datetime
import gc
import random
import time
import tracemalloc

import pytest
from django.core.cache import cache
from django.db import connection
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.fmm import refresh, versions
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import QuestionAnswer, Visit
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 5)
VISITS = 5_000
ROWS_PER_VISIT = 3
QUESTIONS = 100_000
TREE = {"lft": 1, "rght": 2, "level": 0, "tree_id": 1}
LIMITS = {"full": 60.0, "scores": 15.0, "memory_mb": 200.0, "tab_ms": 300.0}
# looser limits for the pages the spec does not bound (a page gone quadratic would break them)
PAGE_LIMITS_MS = {
    "insights card": 3_000,
    "visit page": 1_500,
    "PD page": 2_000,
    "CSV": 5_000,
    "rules preview": 30_000,  # an administrator's button: two rescores of the year in memory
}
TABS = ("insights", "quality", "analysis", "visits", "map")
RATINGS = ("On Track", "On Track", "On Track", "Off Track", "Not Monitored", "", "Constrained")
STATUSES = ("completed",) * 6 + ("submitted", "data_collection", "assigned", "cancelled")
WORDS = (
    "classes attendance registers children teachers materials delayed supplies water centre session "
    "shortage outreach referral caregivers psychosocial support kits distributed volunteers training"
).split()
QUESTION_TEXTS = (
    (12, "Have the activities been implemented as planned and reported by the implementing partner?"),
    (13, "Q2 – Activities monitored"),
    (14, "Q3 – Key observations and findings"),
    (15, "PSEA: Were any protection from sexual exploitation and abuse concerns observed?"),
    *((16 + k, f"Checklist question {k + 1}: are the records of the site kept?") for k in range(16)),
)


def _narrative(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randrange(5, 60))).capitalize() + "."


def _world(rng: random.Random) -> dict[str, int]:
    """The production-size world, inserted in bulk. Returns what was inserted."""
    levels = {n: LocationType.objects.create(name=f"Admin level {n}", admin_level=n) for n in (0, 1, 2, 3)}
    country = Location.objects.create(name="Lebanon", p_code="LB", type=levels[0], **TREE)
    governorates = Location.objects.bulk_create(
        Location(name=f"Governorate {g}", p_code=f"LB{g}", type=levels[1], parent=country, **TREE)
        for g in range(8)
    )
    districts = Location.objects.bulk_create(
        Location(
            name=f"District {d}",
            p_code=f"LB{d // 4}{d:02d}",
            type=levels[2],
            parent=governorates[d % 8],
            latitude=33.5 + d * 0.03,
            longitude=35.5 + d * 0.02,
            **TREE,
        )
        for d in range(26)
    )
    cadasters = Location.objects.bulk_create(
        Location(
            name=f"Cadaster {c}",
            p_code=f"LBC{c:04d}",
            type=levels[3],
            parent=districts[c % 26],
            latitude=33.3 + (c % 50) * 0.02,
            longitude=35.3 + (c // 50) * 0.1,
            **TREE,
        )
        for c in range(300)
    )
    sites = dm.MonitoringSite.objects.bulk_create(
        dm.MonitoringSite(
            datamart_id=90_000 + s,
            name=f"Site {s}",
            p_code=f"LBS{s:04d}",
            latitude=c.latitude + 0.001,
            longitude=c.longitude + 0.001,
            parent=c,
        )
        for s, c in enumerate(cadasters[:60])
    )
    partners = PartnerOrganization.objects.bulk_create(
        PartnerOrganization(
            etl_id=str(p),
            name=f"Partner organisation {p}",
            short_name=f"P{p}",
            partner_type="CSO",
            vendor_number=f"25{p:08d}",
        )
        for p in range(60)
    )
    pcas = PCA.objects.bulk_create(
        PCA(
            etl_id=str(1000 + k),
            partner=partners[k % 60],
            number=f"LEB/PCA2025{k:03d}/PD2026{k:03d}",
            title=f"Programme {k}",
            section_names=[("Education", "Child Protection", "WASH", "Health")[k % 4]],
            offices_set=[("Beirut", "Zahle", "Tripoli", "Tyre")[k % 4]],
            status="active",
            start=datetime.date(2026, 1, 1),
            end=datetime.date(2026, 12, 31),
        )
        for k in range(240)
    )
    through = PCA.locations.through
    through.objects.bulk_create(
        through(pca_id=pca.pk, location_id=cadasters[(k * 7 + j) % 300].pk)
        for k, pca in enumerate(pcas[::2])
        for j in range(3)
    )
    rows = []
    for v in range(VISITS):
        activity = 100_000 + v
        partner = partners[v % 60]
        pca = pcas[(v % 60) + 60 * rng.randrange(4)]
        cadaster = cadasters[rng.randrange(300)]
        site = sites[v % 60] if v % 3 == 0 else None
        end = datetime.date(2026, 1, 1) + datetime.timedelta(days=rng.randrange(0, 300))
        status = rng.choice(STATUSES)
        team = [{"name": f"Monitor {v % 97}", "email": f"monitor{v % 97}@example.org"}]
        for r, (entity, entity_type) in enumerate(
            (
                (pca.number.replace("LEB/", "LEBA/"), "PD/SSFA"),
                ("2.2 INCREASED ACCESS", "CP Output"),
                (partner.name, "Partner"),
            )
        ):
            rating = rng.choice(RATINGS) if status in ("completed", "submitted") else ""
            narrative = _narrative(rng)
            n = activity * 10 + r
            rows.append(
                dm.MonitoringFinding(
                    datamart_id=n,
                    partner=partner,
                    vendor_number=partner.vendor_number,
                    entity=entity,
                    entity_type=entity_type,
                    monitoring_activity=f"FM-2026-{v:05d}",
                    monitoring_activity_id=activity,
                    reference_number=f"FM-2026-{v:05d}",
                    status=status,
                    overall_finding_rating=rating,
                    narrative_finding=narrative,
                    start_date=end - datetime.timedelta(days=1),
                    end_date=end,
                    location=cadaster,
                    monitoring_site=site,
                    site=site.name if site else "",
                    is_programmatic_visit=v % 2 == 0,
                    visit_lead=f"Monitor {v % 97}",
                    data={
                        "id": n,
                        "entity": entity,
                        "entity_type": entity_type,
                        "monitoring_activity": f"FM-2026-{v:05d}",
                        "monitoring_activity_id": activity,
                        "status": status,
                        "overall_finding_rating": rating,
                        "narrative_finding": narrative,
                        "monitoring_activity_end_date": end.isoformat(),
                        "location": {"id": cadaster.pk, "name": cadaster.name, "p_code": cadaster.p_code},
                        "visit_lead": f"Monitor {v % 97}",
                        "team_members": team,
                        "field_office": ("Beirut", "Zahle", "Tripoli", "Tyre")[v % 4],
                        "country_name": "Lebanon",
                    },
                )
            )
    dm.MonitoringFinding.objects.bulk_create(rows, batch_size=2_000)
    findings = len(rows)
    del rows

    answers = ("On track", "Constrained", "Off track", "1", "2", "3", "Yes", "No", "", "n/a")
    batch, written = [], 0
    for q in range(QUESTIONS):
        v = q // 20
        question_id, text = QUESTION_TEXTS[q % 20]
        answer = rng.choice(answers) if question_id in (12, 15) else rng.choice(("", "n/a", _narrative(rng)))
        if question_id == 15:
            answer = rng.choice(("No", "No", "No", "Yes", ""))
        record = {
            "id": 500_000 + q,
            "monitoring_activity_id": 100_000 + v,
            "monitoring_activity": f"FM-2026-{v:05d}",
            "question_id": question_id,
            "question_text": text,
            "is_hact": question_id == 12,
            "order": q % 20,
            "entity": rng.choice(
                ("", f"LEBA/PCA2025{v % 240:03d}/PD2026{v % 240:03d}", "2.2 INCREASED ACCESS")
            ),
            "entity_type": "",
            "answer": answer,
            "summary": _narrative(rng) if q % 5 == 0 else "",
            "method": "Interview",
        }
        batch.append(dm.DatamartDocument(dataset="fm_questions", record_key=str(record["id"]), data=record))
        if len(batch) == 5_000:
            dm.DatamartDocument.objects.bulk_create(batch)
            written += len(batch)
            batch = []
    dm.DatamartDocument.objects.bulk_create(batch)
    written += len(batch)
    dm.DatamartDocument.objects.bulk_create(
        [
            dm.DatamartDocument(
                dataset="fm_options",
                record_key=str(70_000 + k),
                data={"id": 70_000 + k, "question_id": 12, "value": str(k + 1), "label": label},
            )
            for k, label in enumerate(("On track", "Constrained", "Off track"))
        ]
        + [
            dm.DatamartDocument(
                dataset="fm_programme_activities",
                record_key=str(80_000 + v),
                data={
                    "id": 80_000 + v,
                    "monitoring_activity_id": 100_000 + v,
                    "programme_activity": f"Activity {v % 40}",
                    "cp_output": "2.2 INCREASED ACCESS TO EDUCATION",
                    "intervention_number": pcas[v % 240].number,
                },
            )
            for v in range(VISITS)
        ],
        batch_size=2_000,
    )
    dm.ActionPoint.objects.bulk_create(
        [
            dm.ActionPoint(
                datamart_id=800_000 + a,
                related_module="fm",
                related_module_id=100_000 + rng.randrange(VISITS),
                status=rng.choice(("open", "open", "completed")),
                high_priority=a % 7 == 0,
                due_date=datetime.date(2026, 1, 1) + datetime.timedelta(days=rng.randrange(0, 365)),
            )
            for a in range(2_500)
        ],
        batch_size=2_000,
    )
    return {"findings": findings, "questions": written}


def _ms(fn) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


# committed, as in production: rows a test's own open transaction inserted are read more slowly (their
# visibility is checked row by row) and are invisible to autovacuum. The database is restored afterwards
# (serialized_rollback), and no background process is started meanwhile.
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_monitoring_insights_at_production_size(admin_user, client, monkeypatch):
    from neurodb.integrations import background

    monkeypatch.setattr(background, "start_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)
    inserted = _world(random.Random(1722))
    assert inserted == {"findings": VISITS * ROWS_PER_VISIT, "questions": QUESTIONS}

    # the full refresh: time, then memory (the same work again, the programme document links undone)
    start = time.perf_counter()
    full = refresh.run(triggered_by="test", today=TODAY)
    full_s = time.perf_counter() - start
    assert full.status == SyncRun.Status.SUCCEEDED, full.error
    assert Visit.objects.count() == VISITS and QuestionAnswer.objects.count() == QUESTIONS
    assert Visit.objects.filter(quality_score__isnull=False).count() > VISITS // 2

    dm.MonitoringFinding.objects.update(intervention=None, pd_match="")
    tracemalloc.start()
    try:
        again = refresh.run(triggered_by="test", today=TODAY)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert again.status == SyncRun.Status.SUCCEEDED, again.error
    peak_mb = peak / 2**20

    start = time.perf_counter()
    scores = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    scores_s = time.perf_counter() - start
    assert scores.status == SyncRun.Status.SUCCEEDED, scores.error

    # the planner's statistics, as autovacuum keeps them in production (it may not have reached the rows
    # just inserted, and without them the tabs' queries are planned blind)
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")

    # each tab as an HTMX partial on a cold cache (the first view after a refresh or a filter change,
    # the slowest one): the best of three cold views, after one view that loads the templates
    client.force_login(admin_user)
    base = {"year": "2026", "section": ""}
    tabs = {}
    # as in a web worker, not in a test process that has grown with every test before this one: the
    # objects already there are set aside from the garbage collector's full passes while timing
    gc.collect()
    gc.freeze()
    try:
        for tab in TABS:
            params = {**base, "tab": tab}
            assert client.get(reverse("fmm:dashboard"), params, HTTP_HX_REQUEST="true").status_code == 200
            tabs[tab] = min(_cold_ms(client, reverse("fmm:dashboard"), params, hx=True) for _ in range(3))
    finally:
        gc.unfreeze()

    # the other pages, also cold
    visit = Visit.objects.order_by("-urgency").first()
    pd = PCA.objects.first()
    pages = {}
    for name, url, params in (
        ("insights card", reverse("fmm:insights"), base),
        ("visit page", reverse("fmm:visit", args=[visit.key]), {}),
        ("PD page", reverse("reports:programme_detail", args=[pd.pk]), {}),
        ("CSV", reverse("fmm:visits"), {**base, "export": "csv"}),
    ):
        pages[name] = _cold_ms(client, url, params)
    pages["rules preview"] = _ms(lambda: versions.preview({"R2": {"threshold": 90}}, {}))

    report = (
        f"full refresh {full_s:.1f} s · peak traced memory {peak_mb:.0f} MB · scores-only {scores_s:.1f} s · "
        + " · ".join(f"{tab} {ms:.0f} ms" for tab, ms in tabs.items())
        + " · "
        + " · ".join(f"{name} {ms:.0f} ms" for name, ms in pages.items())
    )
    print("\n" + report)
    assert full_s < LIMITS["full"], report
    assert peak_mb < LIMITS["memory_mb"], report
    assert scores_s < LIMITS["scores"], report
    for tab, ms in tabs.items():
        assert ms < LIMITS["tab_ms"], f"{tab}: {report}"
    for name, limit in PAGE_LIMITS_MS.items():
        assert pages[name] < limit, f"{name}: {report}"


def _cold_ms(client, url: str, params: dict, hx: bool = False) -> float:
    """The server time of one request with nothing cached (every block computed)."""
    cache.clear()
    extra = {"HTTP_HX_REQUEST": "true"} if hx else {}
    start = time.perf_counter()
    response = client.get(url, params, **extra)
    elapsed = (time.perf_counter() - start) * 1000
    assert response.status_code == 200, url
    if getattr(response, "streaming", False):
        b"".join(response.streaming_content)
    return elapsed


# ------------------------------------------------------------------------------------------ the fast paths
# The faster ways the pages are now built (stage 8b) give what the plain ways gave, on the small world.
def test_the_drill_links_of_a_table_equal_their_own_drill_url(built):
    from django.http import QueryDict

    from neurodb.fmm import views
    from neurodb.fmm.scope import Scope

    scope = Scope.from_params(QueryDict("year=2026&section=Education&q=a b&location=30"))
    rows = [{"drill": value} for value in ("31", "Zahle & Bekaa", "Saïda / 2", "a+b", "")] + [{"drill": None}]
    for key in ("location", "office", "section"):
        made = views._with_urls(rows, scope, key)
        for row, out in zip(rows, made, strict=True):
            wanted = views.drill_url(scope, **{key: str(row["drill"])}) if row["drill"] else ""
            assert out["url"] == wanted, (key, row)


def test_each_kind_of_entity_alone_equals_its_part_of_every_kind(built):
    from django.http import QueryDict

    from neurodb.fmm import metrics
    from neurodb.fmm.scope import Scope

    for query in ("year=2026&section=", "year=2026&section=&entity_type=pd", "year=2026&section=Education"):
        scope = Scope.from_params(QueryDict(query))
        every = metrics.entity_rows(scope)
        for kind in ("pd", "cp_output", "partner", "other"):
            one = metrics.entities_performance(scope, kind)
            assert one["rows"] == every["entities"].get(kind, []), (query, kind)
        rows = list(scope.entities().values_list("kind", flat=True))
        assert metrics.entity_kinds(scope) == {k: rows.count(k) for k in set(rows)}, query


def test_the_long_place_lists_come_when_show_all_is_opened(built, client_viewer, monkeypatch):
    from neurodb.fmm import views

    monkeypatch.setattr(views, "PLACES_TOP", 2)
    page = reverse("fmm:dashboard")
    for tab, rest in (("quality", "fmm-places-rest"), ("analysis", "fmm-frequency-rest")):
        html = client_viewer.get(page, {"tab": tab, "section": ""}, HTTP_HX_REQUEST="true").content.decode()
        tail = html.split(f'id="{rest}"', 1)[1].split("</details>", 1)[0]
        assert "<tr>" not in tail and "places=all" in html and 'hx-trigger="toggle once"' in html, tab
        full = client_viewer.get(page, {"tab": tab, "section": "", "places": "all"}).content.decode()
        tail = full.split(f'id="{rest}"', 1)[1].split("</details>", 1)[0]
        assert tail.count("<tr>") == 4, tab  # the 6 places of the world but the first 2


def test_folding_a_text_is_unchanged_by_the_quick_way_for_plain_letters():
    import string
    import unicodedata

    from neurodb.watch.people import fold

    def slow(text):
        decomposed = unicodedata.normalize("NFKD", str(text or ""))
        return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()

    rng = random.Random(7)
    letters = string.printable + "éÉßİıﬁ½²Ⅻ"
    for _ in range(5_000):
        text = "".join(rng.choice(letters) for _ in range(rng.randrange(30)))
        assert fold(text) == slow(text), text
    assert fold(None) == "" and fold(12) == "12"


def test_a_role_is_read_in_one_query(db, roles, django_assert_max_num_queries):
    from neurodb.accounts.models import User
    from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER, role_of

    for name, role in (("v", VIEWER), ("e", SECTION_EDITOR), ("a", ADMIN)):
        user = User.objects.create_user(username=name, password="x-pass-123456")
        user.groups.add(roles[role])
        with django_assert_max_num_queries(1):
            assert role_of(user) == role
    both = User.objects.create_user(username="both", password="x-pass-123456")
    both.groups.add(roles[SECTION_EDITOR], roles[ADMIN])
    assert role_of(both) == ADMIN


def test_the_entity_table_comes_when_it_is_reached_or_asked_for(built, client_viewer):
    page = reverse("fmm:dashboard")
    html = client_viewer.get(
        page, {"tab": "analysis", "section": ""}, HTTP_HX_REQUEST="true"
    ).content.decode()
    block = html.split('id="fmm-entities"', 1)[1].split("</section>", 1)[0]
    assert 'hx-trigger="revealed"' in block and "entity_kind=pd" in block and "<tbody>" not in block
    assert "PD/SSFA 9" in " ".join(block.split())  # the chips still count every kind
    asked = client_viewer.get(page, {"tab": "analysis", "section": "", "entity_kind": "pd"}).content.decode()
    block = asked.split('id="fmm-entities"', 1)[1].split("</section>", 1)[0]
    assert 'hx-trigger="revealed"' not in block and block.count("<tr>") >= 2
