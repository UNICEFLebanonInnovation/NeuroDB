"""The figures of the record-counting blocks are the same however they are computed: every metrics block,
key figure tile, chart series, table and drill list of the Monitoring insights page, the map and the AI
brief's facts, for some 40 varied filters (periods, visit filters, record filters, drill-downs) on a
randomised world of visits holding one to four records each, equal the figures the plain per-record
implementation gave (commit 8eee057, before the speed-ups), value for value.

The figures are frozen in ``data/records_figures_golden.json``: each filter's key figures in clear, and
a fingerprint (sha256 of the canonical JSON: a Decimal keeps its digits, a float its exact value) of
every block. Each filter's blocks are worked out twice: as the page reads them (one cache shared by the
tabs, in the order a reader opens them), from the figures the refresh kept per visit (``tallies``)
wherever the filter keeps whole visits; and each block alone on a cold cache, from the records and their
checks themselves (the figures kept per visit marked out of date, as any change to the records marks
them). Both must equal the frozen figures. Every object a figure names (place, partner, programme
document, site) is created with a fixed id, so the figures read the same ids whatever ran before.

To see what differs, dump the figures of the frozen commit (``FMM_FIGURES_DUMP=/tmp/ref.json.gz``, run
there) and run this test with ``FMM_FIGURES_REFERENCE=/tmp/ref.json.gz``: each difference is then named
down to the value. To freeze the figures again (only when a figure is meant to change), run it with
``FMM_WRITE_GOLDEN=1``.
"""

from __future__ import annotations

import datetime
import decimal
import gzip
import hashlib
import json
import os
import random
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.http import QueryDict
from django.test import RequestFactory, override_settings
from django.utils.functional import Promise

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.fmm import metrics
from neurodb.fmm import scope as scope_module
from neurodb.fmm.scope import Scope
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PCA, PartnerOrganization

from .conftest import FakeChecks

pytestmark = pytest.mark.django_db

GOLDEN = Path(__file__).parent / "data" / "records_figures_golden.json"
TODAY = datetime.date(2026, 10, 5)
SEED = 20261010
ID = 7_300_000  # the fixed ids of the world's places, partners, programme documents and sites start here
TREE = {"lft": 1, "rght": 2, "level": 0, "tree_id": 1}
VISITS = 150
GOVERNORATES = ("Bekaa", "North", "Mount Lebanon", "South", "Akkar")
SECTIONS = ("Education", "Child Protection", "WASH", "Health")
OFFICES = ("Beirut", "Zahle", "Tripoli", "Tyre")
MODALITIES = ("UNICEF Staff", "TPM - iAPS", "Third party")
STATUSES = ("completed",) * 6 + (
    "submitted",
    "report_finalization",
    "review",
    "data_collection",
    "assigned",
    "checklist",
    "cancelled",
    "draft",
)
RATINGS = ("On Track", "On Track", "On Track", "Constrained", "Off Track", "Not Monitored", "", "Partially")
OUTPUTS = ("2.2 INCREASED ACCESS TO EDUCATION", "1.4 NUTRITION OF YOUNG CHILDREN", "3.1 SAFE WATER")
WORDS = (
    "classes attendance registers children teachers materials supplies water centre session outreach "
    "caregivers psychosocial support distributed volunteers training follow-up action partner agreed"
).split()
# sentences some notes end with, which the fake AI checks flag (VERDICTS)
TRIGGERS = (
    (0.20, "Sessions were delayed by two weeks."),
    (0.15, "Activities were suspended."),
    (0.25, "A shortage of kits was reported."),
    (0.15, "A referral was made."),
    (0.25, "Registers were missing."),
)
Q1_TEXT = "Have the activities been implemented as planned and reported by the implementing partner?"
QUESTIONS = (
    (12, Q1_TEXT),
    (13, "Q2 – Activities monitored"),
    (14, "Q3 – Key observations and findings"),
    (15, "PSEA: Were any protection from sexual exploitation and abuse concerns observed?"),
    (16, "Are attendance registers kept up to date?"),
    (17, "Is the space safe for children?"),
)
VERDICTS = {  # the fake AI checks' answers, told by what a record sends (passed otherwise)
    ("delayed by two weeks", "R8"): (False, "The delays are not followed by an action point."),
    ("were suspended", "R32"): (False, "The suspension has no matching action point."),
    ("shortage of kits", "R6"): (False, "The narrative does not address the visit objective."),
    ("Constrained", "R5"): (False, "Q1 says Constrained while Q2 lists no problem."),
    ("referral was made", "R7"): (False, "Q3 names no responsible party."),
    ("Registers were missing", "R3"): (False, "Q2 lists no activity the monitor verified."),
}
AI_CHECKS_MADE = 750  # the rest stay pending (a provisional score)


def _narrative(rng: random.Random) -> str:
    if rng.random() < 0.08:
        return ""
    text = " ".join(rng.choice(WORDS) for _ in range(rng.randrange(3, 50))).capitalize() + "."
    return " ".join([text, *(sentence for share, sentence in TRIGGERS if rng.random() < share)])


# ------------------------------------------------------------------------------------------ the world
def _world(rng: random.Random) -> dict[str, Any]:
    """Visits of one to four records of every kind, rating and status, over 2025-2027, with places,
    sites, sections, offices, modalities, checklist answers, action points and HACT figures."""
    levels = {
        n: LocationType.objects.create(id=ID + n, name=f"Admin level {n}", admin_level=n)
        for n in (0, 1, 2, 3)
    }
    country = Location.objects.create(id=ID + 9, name="Lebanon", p_code="LB", type=levels[0], **TREE)
    governorates = [
        Location.objects.create(
            id=ID + 10 + g,
            name=name,
            p_code=f"LB{g + 1}",
            type=levels[1],
            parent=country,
            latitude=33.6 + g * 0.2 if g == 0 else None,
            longitude=35.6 + g * 0.1 if g == 0 else None,
            **TREE,
        )
        for g, name in enumerate(GOVERNORATES)
    ]
    districts = [
        Location.objects.create(
            id=ID + 100 + d,
            name=f"District {d}",
            p_code=f"LB{d % 5 + 1}{d:02d}",
            type=levels[2],
            parent=governorates[d % 5],
            latitude=33.4 + d * 0.05 if d % 3 else None,
            longitude=35.4 + d * 0.04 if d % 3 else None,
            **TREE,
        )
        for d in range(12)
    ]
    cadasters = [
        Location.objects.create(
            id=ID + 1000 + c,
            name=f"Cadaster {c}" if c % 9 else f"Saïda quarter {c}",
            p_code=f"LBC{c:04d}",
            type=levels[3],
            parent=districts[c % 12],
            latitude=33.3 + (c % 10) * 0.03 if c % 7 else None,
            longitude=35.3 + (c // 10) * 0.05 if c % 7 else None,
            **TREE,
        )
        for c in range(60)
    ]
    sites = [
        dm.MonitoringSite.objects.create(
            id=ID + s,
            datamart_id=95_000 + s,
            name=f"Site {s}",
            p_code=f"LBS{s:04d}",
            latitude=(cadasters[s * 5].latitude or 33.5) + 0.001,
            longitude=(cadasters[s * 5].longitude or 35.5) + 0.001,
            parent=cadasters[s * 5],
        )
        for s in range(10)
    ]
    partners = [
        PartnerOrganization.objects.create(
            id=ID + p,
            etl_id=str(ID + p),
            name=f"Partner organisation {p}",
            short_name=f"P{p}" if p % 4 else "",
            partner_type=("CSO", "Civil Society Organization", "Government", "UN Agency", "")[p % 5],
            vendor_number=f"26{p:08d}",
        )
        for p in range(12)
    ]
    pcas = []
    for k in range(30):
        ssfa = k % 7 == 3
        pcas.append(
            PCA.objects.create(
                id=ID + k,
                etl_id=str(ID + k),
                partner=partners[k % 12],
                number=f"LEB/SSFA2026{k:03d}" if ssfa else f"LEB/PCA2025{k:03d}/PD2026{k:03d}",
                document_type="SSFA" if ssfa else "PD",
                title=f"Programme {k}",
                section_names=[SECTIONS[(k + j) % 4] for j in range(k % 3)],
                offices_set=[OFFICES[(k + j) % 4] for j in range(k % 3)],
                status="active" if k % 5 else "ended",
                start=datetime.date(2025, 7, 1),
                end=datetime.date(2026, 12, 31),
            )
        )
        if k % 3:
            pcas[-1].locations.set([cadasters[(k * 7 + j) % 60] for j in range(1 + k % 3)])

    findings, questions, n_question = [], [], 0
    activities = []
    for v in range(VISITS):
        activity = 300_000 + v
        partner = partners[rng.randrange(12)]
        own = [pca for pca in pcas if pca.partner_id == partner.pk] or pcas
        status = rng.choice(STATUSES)
        end = datetime.date(2025, 7, 1) + datetime.timedelta(days=rng.randrange(0, 560))
        start = None if rng.random() < 0.15 else end - datetime.timedelta(days=rng.randrange(0, 3))
        if rng.random() < 0.02:
            start = end = None  # no date at all: left out of every period
        place = rng.random()
        cadaster = cadasters[rng.randrange(60)]
        location = cadaster if place < 0.7 else districts[rng.randrange(12)] if place < 0.85 else None
        site = sites[rng.randrange(10)] if rng.random() < 0.25 else None
        reference_only = rng.random() < 0.04
        reference = f"FM/2026/R{v}" if reference_only else f"FM-2026-{v:05d}"
        office = rng.choice((*OFFICES, None))
        modality = rng.choice((*MODALITIES, ""))
        programmatic = rng.random() < 0.5
        lead = f"Monitor {v % 17}"
        team = [{"name": lead, "email": f"monitor{v % 17}@example.org"}]
        kinds = rng.sample(("pd", "pd", "cp_output", "partner", "other"), rng.choice((1, 1, 2, 2, 3, 3, 4)))
        entities = []
        for kind in kinds:
            if kind == "pd":
                pca = rng.choice(own)
                number = pca.number.replace("LEB/", "LEBA/") if rng.random() < 0.3 else pca.number
                entities.append((number, "SSFA" if "SSFA" in number else "PD/SSFA"))
            elif kind == "cp_output":
                entities.append((rng.choice(OUTPUTS), "CP Output"))
            elif kind == "partner":
                entities.append((partner.name, "Partner"))
            else:
                entities.append((rng.choice(("Community centre visit", "School cluster", "")), "Other"))
        for r, (entity, entity_type) in enumerate(entities):
            n = activity * 10 + r
            rated = status in ("completed", "submitted", "report_finalization", "review")
            rating = rng.choice(RATINGS) if rated else rng.choice(("", "", "Not Monitored"))
            narrative = _narrative(rng)
            data = {
                "id": n,
                "entity": entity,
                "entity_type": entity_type,
                "monitoring_activity": reference,
                "status": status,
                "overall_finding_rating": rating,
                "narrative_finding": narrative,
                "monitoring_activity_start_date": start.isoformat() if start else None,
                "monitoring_activity_end_date": end.isoformat() if end else None,
                "location": {"id": location.pk, "name": location.name, "p_code": location.p_code}
                if location
                else {"name": rng.choice(("Saida old town", "Unknown hamlet", ""))},
                "site": site.name if site else "",
                "is_programmatic_visit": programmatic,
                "visit_lead": lead,
                "team_members": team,
                "country_name": "Lebanon",
            }
            if not reference_only:
                data["monitoring_activity_id"] = activity
            if office:
                data["field_office"] = office
            if modality:
                data["monitoring_modality"] = modality
            findings.append(
                dm.MonitoringFinding(
                    datamart_id=n,
                    partner=partner,
                    vendor_number=partner.vendor_number,
                    entity=entity,
                    entity_type=entity_type,
                    monitoring_activity=reference,
                    monitoring_activity_id=None if reference_only else activity,
                    reference_number=reference,
                    status=status,
                    overall_finding_rating=rating,
                    narrative_finding=narrative,
                    start_date=start,
                    end_date=end,
                    location=location,
                    location_name="" if location else data["location"]["name"],
                    monitoring_site=site,
                    site=site.name if site else "",
                    is_programmatic_visit=programmatic,
                    visit_lead=lead,
                    data=data,
                )
            )
        activities.append((activity, reference, reference_only, entities))
        # its checklist answers: Q1 per record or for the visit, Q2, Q3, PSEA and two others
        for question_id, text in QUESTIONS:
            if rng.random() < 0.25:
                continue  # not asked on this visit
            targets = (
                [e for e in entities if e[0]] if question_id == 12 and rng.random() < 0.6 else [("", "")]
            )
            for entity, entity_type in targets:
                if question_id == 12:
                    answer = rng.choice(("On track", "Constrained", "Off track", "1", "2", "3", "", "n/a"))
                elif question_id == 15:
                    answer = rng.choice(("No", "No", "No", "Yes", ""))
                else:
                    answer = rng.choice(("", "n/a", _narrative(rng)))
                n_question += 1
                record = {
                    "id": 900_000 + n_question,
                    "monitoring_activity": reference,
                    "monitoring_activity_end_date": end.isoformat() if end else None,
                    "question_id": question_id,
                    "question_text": text,
                    "is_hact": question_id == 12,
                    "order": question_id - 11,
                    "entity": entity,
                    "entity_type": entity_type,
                    "answer": answer,
                    "summary": _narrative(rng) if rng.random() < 0.2 else "",
                    "method": rng.choice(("Interview", "Observation", "Document review")),
                    "country_name": "Lebanon",
                }
                if not reference_only:
                    record["monitoring_activity_id"] = activity
                questions.append(
                    dm.DatamartDocument(dataset="fm_questions", record_key=str(record["id"]), data=record)
                )
    dm.MonitoringFinding.objects.bulk_create(findings)
    dm.DatamartDocument.objects.bulk_create(questions)
    options = [
        {"id": 97_000 + k, "question_id": question_id, "value": value, "label": label}
        for k, (question_id, value, label) in enumerate(
            (
                (12, "1", "On track"),
                (12, "2", "Constrained"),
                (12, "3", "Off track"),
                (15, "1", "Yes"),
                (15, "2", "No"),
            )
        )
    ]
    dm.DatamartDocument.objects.bulk_create(
        [dm.DatamartDocument(dataset="fm_options", record_key=str(o["id"]), data=o) for o in options]
        + [
            dm.DatamartDocument(
                dataset="fm_programme_activities",
                record_key=str(98_000 + k),
                data={
                    "id": 98_000 + k,
                    "monitoring_activity_id": activity,
                    "monitoring_activity": reference,
                    "programme_activity": f"Activity {k % 9}",
                    "cp_output": OUTPUTS[k % 3],
                    "intervention_number": pcas[k % 30].number,
                },
            )
            for k, (activity, reference, reference_only, _entities) in enumerate(activities)
            if not reference_only and k % 2
        ]
        + [
            dm.DatamartDocument(dataset="offices", record_key=str(k), data={"id": k, "name": name})
            for k, name in enumerate(OFFICES)
        ]
        + [
            dm.DatamartDocument(dataset="sections", record_key=str(k), data={"id": k, "name": name})
            for k, name in enumerate(SECTIONS)
        ]
    )
    dm.ActionPoint.objects.bulk_create(
        dm.ActionPoint(
            datamart_id=880_000 + a,
            related_module="fm",
            related_module_id=None if a % 9 == 0 else 300_000 + rng.randrange(VISITS),
            module_reference_number=f"FM-2026-{rng.randrange(VISITS):05d}" if a % 9 == 0 else "",
            reference_number=f"LEB/2026/AP/{a}",
            category=rng.choice(("Programmatic", "Financial", "")),
            status=rng.choice(("open", "open", "completed", "cancelled")),
            high_priority=a % 4 == 0,
            due_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=rng.randrange(0, 240)),
        )
        for a in range(70)
    )
    for p, partner in enumerate(partners[:7]):
        dm.PartnerHACTYear.objects.create(
            datamart_id=ID + p,
            partner=partner,
            year=2026,
            pv_required=p % 4,
            pv_planned=p % 3,
            pv_completed=p % 2,
        )
    for k, pca in enumerate(pcas[:12]):
        dm.PlannedVisits.objects.create(
            datamart_id=ID + k,
            intervention=pca,
            partner=pca.partner,
            year=2026,
            q1=k % 2,
            q2=1,
            q3=k % 3,
            q4=0,
        )
    from neurodb.fmm.models import FieldOfficeStaff

    FieldOfficeStaff.objects.update_or_create(
        office="Zahle", defaults={"emails": "\n".join(f"monitor{n}@example.org" for n in range(0, 17, 2))}
    )
    return {
        "partners": [p.pk for p in partners],
        "pcas": [p.pk for p in pcas],
        "cadasters": [c.pk for c in cadasters],
    }


def _after_refresh(rng: random.Random) -> None:
    """NeuroDB's own action points and reviews, on the visits the refresh built."""
    from neurodb.fmm.models import LocalActionPoint, Visit, VisitReview

    keys = list(Visit.objects.order_by("key").values_list("key", flat=True))
    for k in range(25):
        LocalActionPoint.objects.create(
            title=f"Follow up {k}",
            visit_key=rng.choice(keys),
            status=rng.choice(("open", "open", "done", "dropped")),
            source=rng.choice(("manual", "auto")),
            due_date=TODAY + datetime.timedelta(days=rng.randrange(-60, 60)) if k % 5 else None,
        )
    for _review in range(20):
        VisitReview.objects.create(
            visit_key=rng.choice(keys), status=rng.choice(("reviewed", "follow_up", "data_issue"))
        )


# ------------------------------------------------------------------------------------------ the scopes
def _scopes(world: dict[str, Any]) -> list[str]:
    partners, pcas, cadasters = world["partners"], world["pcas"], world["cadasters"]
    issue = metrics.top_issues(Scope.from_params(QueryDict("year=2026&section="), None, TODAY), 3)
    return [
        "year=2026&section=",
        "section=",  # this year
        "preset=all_time&section=",
        "preset=last_90&section=",
        "preset=custom&from=2026-03-01&to=2026-06-30&section=",
        "year=2025&section=",
        "year=2026&section=Education",
        "year=2026&section=none&section=WASH",
        "year=2026&section=&governorate=bekaa",
        "year=2026&section=&governorate=none",
        "year=2026&section=&office=Zahle&office=none",
        f"year=2026&section=&partner={partners[1]}&partner={partners[4]}",
        f"year=2026&section=&pd={pcas[1]}",
        "year=2026&section=&entity_type=partner",
        "year=2026&section=&entity_type=other&entity_type=cp_output",
        "year=2026&section=&rating=off_track&rating=not_monitored",
        "year=2026&section=&status=reported",
        "year=2026&section=&status=planned&status=in_progress",
        f"year=2026&section=&modality={MODALITIES[1]}&programmatic=1",
        "year=2026&section=&quality=low&quality=pending",
        "year=2026&section=&urgency_level=high&urgency_level=low",
        "year=2026&section=&q=partner organisation 3",
        "year=2026&section=&month=2026-05",
        "year=2026&section=&visit_status=completed&visit_rating=off_track",
        "year=2026&section=&visit_rating=not_monitored",
        "year=2026&section=&hact_q1=not_monitored",
        "year=2026&section=&hact_q1=none&bucket=80-100",
        "year=2026&section=&bucket=none",
        "year=2026&section=&flag=R6",
        "year=2026&section=&flags=3+",
        "year=2026&section=&flags=1",
        "year=2026&section=&flags=2&urgency=none",
        "year=2026&section=&urgency=red",
        f"year=2026&section=&location={cadasters[3]}",
        *(f"year=2026&section=&issue={row['drill']}" for row in issue if row["drill"]),
        "year=2026&section=&rule=R1&rule_state=pass",
        "preset=all_time&section=&rule=R5&rule_state=pending",
        "year=2026&section=&review=none",
        "year=2026&section=&review=follow_up",
        "year=2026&section=Health&rating=on_track&quality=high",
    ]


# ------------------------------------------------------------------------------------------ the figures
def _plain(value: Any) -> Any:
    """``value`` as plain JSON values that tell apart what a page could show apart: a Decimal keeps its
    digits ("90.0" is not "90"), a float its exact value, a date its day, a set its members."""
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        return {"$f": repr(value)}
    if isinstance(value, decimal.Decimal):
        return {"$d": str(value)}
    if isinstance(value, datetime.datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"$date": value.isoformat()}
    if isinstance(value, Promise):
        return str(value)
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: _plain(v) for k, v in value.items()}
        return {"$map": sorted(([_plain(k), _plain(v)] for k, v in value.items()), key=json.dumps)}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, set | frozenset):
        return {"$set": sorted((_plain(v) for v in value), key=json.dumps)}
    raise TypeError(f"no plain value for {type(value).__name__}")


def _context(query: str):
    from neurodb.fmm.models import RuleSetting, ScoreSetting

    scope = Scope.from_params(QueryDict(query), None, TODAY)
    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    return scope, metrics.stamp(), metrics.thresholds(setting), list(RuleSetting.objects.all()), setting


ANALYSIS_ALL = {
    "entity_kind": "all",
    "entity_all": "1",
    "sections": "all",
    "places": "all",
    "rule_trends": "1",
}


def _page(query: str) -> dict[str, Any]:
    """The scope's figures as the page reads them: the tabs in a reader's order on one cache (each block
    kept for the next), the brief card and its facts last."""
    cache.clear()
    # as in one request: the blocks read are kept for the rest of it (metrics.request_started)
    begin, end = getattr(metrics, "request_started", None), getattr(metrics, "request_finished", None)
    if begin is not None:
        begin()
    try:
        return _plain(_tabs(query))
    finally:
        if end is not None:
            end()


def _tabs(query: str) -> dict[str, Any]:
    """The tabs' blocks, the brief card and its facts of ``query``, in a reader's order."""
    from neurodb.fmm import views
    from neurodb.fmm.ai import facts
    from neurodb.fmm.ai import insights as ai_insights

    scope, when, limits, rules, setting = _context(query)
    get = RequestFactory().get
    out: dict[str, Any] = {}
    kpis = metrics.kpis(scope, when, limits)
    out["kpis"] = kpis
    out["tiles"] = views._kpi_tiles(scope, kpis)
    out["notes"] = metrics.notes(scope, when)
    out["window"] = metrics.data_window(scope, when)
    out["briefing"] = views._briefing(scope, when, limits)
    out["critical"] = views._critical_items(scope, when, limits, kpis)
    out["quality_tab"] = views._quality_tab(scope, when, limits, rules)
    out["quality_tab_all"] = views._quality_tab(scope, when, limits, rules, places_all=True)
    out["analysis_tab"] = _shown(views._analysis_tab(get("/"), scope, when, limits, rules, setting))
    out["analysis_tab_all"] = _shown(
        views._analysis_tab(get("/", ANALYSIS_ALL), scope, when, limits, rules, setting)
    )
    out["map_tab"] = views._map_tab(get("/"), scope, when)
    out["map_tab_active"] = views._map_tab(get("/", {"pd_scope": "active"}), scope, when)
    out["brief"] = ai_insights.code_written(scope, None)
    built = facts.build(scope, None, narratives=False)
    out["facts"] = {"payload": built.payload, "input_hash": built.input_hash, "sent": built.sent}
    return out


def _shown(tab: dict[str, Any]) -> dict[str, Any]:
    """The Analysis tab as its template reads it: each section's lines are those it writes out (``top``,
    then ``rest`` after "Show all", and ``rest_count``), not the list they are cut from (the whole list is
    the ``sections`` block's)."""
    rows = [{k: v for k, v in row.items() if k != "lines"} for row in tab["section_rows"]]
    return {**tab, "section_rows": rows}


def _drill(scope: Scope) -> dict[str, Any]:
    from django.db.models import Count

    from neurodb.fmm import views

    counted = scope.records().aggregate(n=Count("pk"), visits=Count("visit_id", distinct=True))
    ids = list(views._records_qs(scope)[: views.DRILL_ROWS])
    by_pk = dict(scope.records().filter(pk__in=ids).values_list("pk", "datamart_id"))
    return {**counted, "records": [by_pk[pk] for pk in ids]}


def _card_keys(scope: Scope, limits) -> list[str]:
    from neurodb.fmm.ai import facts
    from neurodb.fmm.models import Visit

    order = facts.card_order(scope, facts.VISIT_CARDS, limits)
    keys = dict(Visit.objects.filter(pk__in=order).values_list("pk", "key"))
    return [keys[pk] for pk in order]


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    """The one pass's figures (the records listed under each section are the sections block's)."""
    return {
        **data,
        "sections": {
            name: {k: v for k, v in section.items() if k not in ("lines", "more")}
            for name, section in data["sections"].items()
        },
    }


def _by_rule_and_month(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rule months in a fixed order (the database gives its groups in any order; every block that
    reads them reads them by rule and month)."""
    return sorted(rows, key=lambda row: (row["rule"], row["month"]))


def _blocks() -> dict[str, Any]:
    """Every block alone: ``name -> f(scope, when, limits, rules, setting)``."""
    from neurodb.fmm import geo
    from neurodb.fmm.ai import facts

    blocks: dict[str, Any] = {
        "kpis": lambda s, w, lim, r, st: metrics.kpis(s, w, lim),
        "notes": lambda s, w, lim, r, st: metrics.notes(s, w),
        "briefing": lambda s, w, lim, r, st: metrics.briefing(s, w, lim),
        "critical_items": lambda s, w, lim, r, st: metrics.critical_items(s, w, lim),
        "summary": lambda s, w, lim, r, st: _summary(metrics.summary(s, w, lim)),
        "monthly_quality": lambda s, w, lim, r, st: metrics.monthly_quality(s, w, lim),
        "monthly_volume": lambda s, w, lim, r, st: metrics.monthly_volume(s, w, lim),
        "hact_q1_by_month": lambda s, w, lim, r, st: metrics.hact_q1_by_month(s, w, lim),
        "rating_by_month": lambda s, w, lim, r, st: metrics.rating_by_month(s, w, lim),
        "score_buckets": lambda s, w, lim, r, st: metrics.score_buckets(s, w, lim),
        "top_issues": lambda s, w, lim, r, st: metrics.top_issues(s, 200, w, r),
        "locations": lambda s, w, lim, r, st: metrics.locations(s, w, lim),
        "rule_months": lambda s, w, lim, r, st: _by_rule_and_month(metrics.rule_months(s, w)),
        "rule_stats": lambda s, w, lim, r, st: metrics.rule_stats(s, w),
        "rule_stats_after_months": lambda s, w, lim, r, st: (
            _by_rule_and_month(metrics.rule_months(s, w)),
            metrics.rule_stats(s, w),
        ),
        "rule_analysis": lambda s, w, lim, r, st: metrics.rule_analysis(s, r, w),
        "issues_summary": lambda s, w, lim, r, st: metrics.issues_summary(s, w, lim),
        "flag_distribution": lambda s, w, lim, r, st: metrics.flag_distribution(s, w, lim),
        "coverage": lambda s, w, lim, r, st: metrics.coverage(s, w, lim),
        "governorate_gaps": lambda s, w, lim, r, st: metrics.governorate_gaps(s, w, lim),
        "offices": lambda s, w, lim, r, st: metrics.offices(s, w, lim),
        "office_rule_badges": lambda s, w, lim, r, st: metrics.office_rule_badges(s, r, w, lim),
        "sections": lambda s, w, lim, r, st: metrics.sections(s, w, lim),
        "breakdown_partner": lambda s, w, lim, r, st: metrics.breakdown(s, "partner", w, lim),
        "breakdown_office": lambda s, w, lim, r, st: metrics.breakdown(s, "office", w, lim),
        "breakdown_section": lambda s, w, lim, r, st: metrics.breakdown(s, "section", w, lim),
        "quality_by_rating": lambda s, w, lim, r, st: metrics.quality_by_rating(s, w, lim),
        "flag_frequency": lambda s, w, lim, r, st: metrics.flag_frequency(s, r, w),
        "dimension_breakdown": lambda s, w, lim, r, st: metrics.dimension_breakdown(s, r, w, st),
        "rule_trends": lambda s, w, lim, r, st: metrics.rule_trends(s, r, w),
        "highlights": lambda s, w, lim, r, st: metrics.highlights(s, w, lim),
        "entity_kinds": lambda s, w, lim, r, st: metrics.entity_kinds(s, w),
        "entities_all": lambda s, w, lim, r, st: metrics.entities_performance(s, "all", w),
        "hact_programmatic": lambda s, w, lim, r, st: metrics.hact_programmatic(s, w, lim),
        "action_points": lambda s, w, lim, r, st: metrics.action_points(s, w, lim),
        "map_visited": lambda s, w, lim, r, st: geo.map_points(s, "visited", when=w),
        "map_active": lambda s, w, lim, r, st: geo.map_points(s, "active", when=w),
        "card_order": lambda s, w, lim, r, st: _card_keys(s, lim),
        "narratives": lambda s, w, lim, r, st: facts.sample_narratives(s, 12),
        "drill": lambda s, w, lim, r, st: _drill(s),
    }
    for kind in ("pd", "cp_output", "partner", "other"):
        blocks[f"entities_{kind}"] = lambda s, w, lim, r, st, kind=kind: metrics.entities_performance(
            s, kind, w
        )
    return blocks


def _alone(query: str) -> dict[str, Any]:
    """Each block of the scope on a cold cache, from the records and checks themselves: the figures kept
    per visit are marked out of date meanwhile."""
    from neurodb.fmm.models import FigureState

    out = {}
    FigureState.objects.filter(pk=1).update(valid=False)
    try:
        for name, block in _blocks().items():
            cache.clear()
            scope, when, limits, rules, setting = _context(query)
            out[name] = _plain(block(scope, when, limits, rules, setting))
    finally:
        FigureState.objects.filter(pk=1).update(valid=True)
    return out


def _first_difference(found: Any, frozen: Any, path: str = "") -> str:
    if type(found) is not type(frozen):
        return f"{path}: {json.dumps(found)[:300]} != {json.dumps(frozen)[:300]}"
    if isinstance(found, dict):
        for key in sorted(set(found) | set(frozen)):
            if key not in found or key not in frozen:
                return f"{path}.{key}: only in {'the figures found' if key in found else 'the frozen ones'}"
            if found[key] != frozen[key] or json.dumps(found[key]) != json.dumps(frozen[key]):
                return _first_difference(found[key], frozen[key], f"{path}.{key}")
    if isinstance(found, list):
        if len(found) != len(frozen):
            return f"{path}: {len(found)} items != {len(frozen)}"
        for k, (a, b) in enumerate(zip(found, frozen, strict=True)):
            if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
                return _first_difference(a, b, f"{path}[{k}]")
    return f"{path}: {json.dumps(found)[:300]} != {json.dumps(frozen)[:300]}"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:32]


def test_every_figure_equals_the_frozen_figures(monkeypatch):
    from neurodb.assistant import agent
    from neurodb.fmm import refresh
    from neurodb.fmm.ai import checks
    from neurodb.fmm.models import (
        FigureState,
        RecordRuleResult,
        Visit,
        VisitActionPoint,
        VisitEntity,
        VisitFigures,
    )
    from neurodb.integrations import background
    from neurodb.watch import people

    monkeypatch.setattr(background, "start_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)
    people.forget()
    rng = random.Random(SEED)
    world = _world(rng)
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    fake = FakeChecks(VERDICTS)
    with override_settings(FMM_AI=True, AI_ASSISTANT_ENABLED=True):
        monkeypatch.setattr(agent, "client", fake.client)
        done = checks.run("test", limit=AI_CHECKS_MADE)
        assert done.status == SyncRun.Status.SUCCEEDED, done.details
        # the scores again on the day of the test, the checks not made still pending
        rescored = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
        assert rescored.status == SyncRun.Status.SUCCEEDED, rescored.error
    _after_refresh(rng)
    people.forget()
    # the figures kept per visit are those of the records and checks the refresh wrote
    assert FigureState.objects.get(pk=1).valid
    assert VisitFigures.objects.count() == Visit.objects.filter(entity_rows__isnull=False).distinct().count()
    # the planner's statistics of the rows this test inserted (in its own transaction), as autovacuum keeps
    # them: without them a filter's query may be planned blind and take seconds
    with connection.cursor() as cursor:
        for model in (Visit, VisitEntity, RecordRuleResult, VisitActionPoint, dm.ActionPoint, Location):
            cursor.execute(f"ANALYZE {model._meta.db_table}")

    # the world holds every kind of record, rating, status and check result
    records = VisitEntity.objects.all()
    assert Visit.objects.count() > 100 and records.count() > 2 * Visit.objects.count()
    assert set(records.values_list("kind", flat=True)) == {"pd", "cp_output", "partner", "other"}
    assert {"on_track", "constrained", "off_track", "not_monitored"} <= set(
        records.values_list("rating", flat=True)
    )
    assert {"pass", "fail", "na", "pending"} <= set(RecordRuleResult.objects.values_list("status", flat=True))
    assert records.filter(quality_score=None).exists() and records.filter(ai_pending__gt=0).exists()
    assert len(set(Visit.objects.values_list("status_group", flat=True))) >= 4

    found = {query: {"page": _page(query), "alone": _alone(query)} for query in _scopes(world)}
    dump = os.environ.get("FMM_FIGURES_DUMP")
    if dump:  # every figure found, to compare two commits' figures value by value
        with gzip.open(dump, "wt", encoding="utf-8") as out:
            json.dump(found, out, sort_keys=True, ensure_ascii=False)
    fingerprints = {
        query: {
            "kpis": parts["page"]["kpis"],
            **{
                part: {name: _fingerprint(value) for name, value in blocks.items()}
                for part, blocks in parts.items()
            },
        }
        for query, parts in found.items()
    }
    if os.environ.get("FMM_WRITE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(fingerprints, sort_keys=True, ensure_ascii=False, indent=1) + "\n")
        pytest.skip(f"figures frozen in {GOLDEN.name}")
    frozen = json.loads(GOLDEN.read_text())
    assert sorted(found) == sorted(frozen), "the filters differ from those the figures were frozen for"
    reference = os.environ.get("FMM_FIGURES_REFERENCE")  # a dump of the frozen commit, to say what differs
    if reference:
        with gzip.open(reference, "rt", encoding="utf-8") as stream:
            reference = json.load(stream)
    differences = []
    for query, parts in found.items():
        if parts["page"]["kpis"] != frozen[query]["kpis"]:
            differences.append(
                f"{query} · key figures: {_first_difference(parts['page']['kpis'], frozen[query]['kpis'])}"
            )
        for part, blocks in parts.items():
            for name, value in blocks.items():
                if fingerprints[query][part][name] == frozen[query][part].get(name):
                    continue
                why = "differs"
                if reference:
                    why = _first_difference(value, reference[query][part][name])
                differences.append(f"{query} · {part} · {name}: {why}")
    assert not differences, f"{len(differences)} figures differ:\n" + "\n".join(differences[:60])
