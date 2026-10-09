"""Demo field monitoring data for ``seed_demo``, so Monitoring insights has visits to show locally:
several monitored entities per visit, every eTools activity status, monitoring sites, programme
document locations, the question answers of each reported visit and the action points raised from
them.

**The question, option and programme activity records are invented** (shape A of the Monitoring
insights specification): no sample of the real fm-questions, fm-options or fm-programme-activities
records exists yet. They look like this::

    fm_questions   {"id": 50001, "monitoring_activity_id": 1722, "monitoring_activity": "FM-2026-022",
                    "monitoring_activity_end_date": "2026-05-12", "vendor_number": "2500212345",
                    "question_id": 12, "question_text": "Have the activities been implemented as planned
                    and reported by the implementing partner?", "is_hact": true, "order": 1,
                    "entity": "LEBA/PCA2023597/PD2025123", "entity_type": "PD/SSFA", "answer": "On track",
                    "summary": "...", "method": "Interview", "country_name": "Lebanon"}
    fm_options     {"id": 70001, "question_id": 12, "value": "1", "label": "On track"}
    fm_programme_activities
                   {"id": 80001, "monitoring_activity_id": 1722, "monitoring_activity": "FM-2026-022",
                    "monitoring_activity_end_date": "2026-05-12", "programme_activity": "BLN classes",
                    "cp_output": "2.2 INCREASED ACCESS TO EDUCATION",
                    "intervention_number": "LEB/PCA2023597/PD2025123"}

When the real records are read from production, the shapes here are changed to mirror them.

What is added (every identifier new, every random draw from its own ``random.Random(1722)``, so the
rest of the demo does not move; the 50 findings ``_demo_etools`` wrote stay exactly as they are):

- 2 monitoring sites per governorate near a cadaster (``datamart_id`` 9001+);
- the planned locations (``PCA.locations``) of every other programme document, one of them a cadaster
  with a site;
- 1-3 more monitored entities (``datamart_id`` 1001+) for activities 1-30: a CP output, the partner
  and another programme document of the partner written in the ``LEBA/`` form, rated On Track, Off
  Track, Not Monitored or not rated, with a visit lead, a team (names and e-mail addresses, as eTools
  sends them) and a field office in the record;
- 24 more activities (ids 51-74, ``datamart_id`` 2001+) going through all nine eTools statuses with
  2-4 entities each (the programme document, as is or in the ``LEBA/`` form, its CP output, the
  partner, sometimes a second PD of the partner; planned and in-progress ones not rated yet), at a
  site, the first reported one placed at a district's own centre point instead and the second rated
  on no entity;
- narratives from 8 texts: short ones, "n/a", one written for two visits, one On Track text naming
  delays and a suspension, one Off Track text naming only good points;
- the question answers of every reported activity: HACT Q1 (On track, Constrained or Off track, so
  Constrained exists only there) per entity on most visits, once for the partner on 2 and once for
  the whole visit on 2; Q2, Q3, PSEA ("No", "Yes" on 2 visits) and 4-9 other questions, one left
  unanswered and one "n/a" on about one visit in ten; the Q1 and PSEA options; the programme
  activities and CP outputs of each activity;
- action points raised from about half of the off-track or constrained visits (``datamart_id``
  8001+, linked by the activity id; some open and overdue, some high priority), 2 linked by the
  activity reference only, and 1 that matches no visit;
- one poorly written report that ended three days ago (activity 75, ``datamart_id`` 2201+, its own
  ``random.Random(1775)``): Off track with a one-line narrative and an "n/a" one, Q1 "On track"
  against it, Q3 "n/a" and most questions left unanswered, so the page has a red (high urgency)
  visit under FMS's urgency formula.

The new findings are linked to their programme documents as the Datamart sync would link them
(``datamart.fm.relink_findings``); the 50 findings of ``_demo_etools`` are linked by the Monitoring
insights refresh that ``seed_demo`` runs at its end, which also builds the visits.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.geo.models import Location
from neurodb.partnerships.models import PCA

COUNTRY = "Lebanon"
OFFICES = ["Beirut", "Zahle", "Tripoli", "Tyre"]
SITE_KINDS = ["public school", "community centre", "primary health care centre", "water station"]
METHODS = ["Interview", "Observation", "Document review", "Focus group discussion"]
STATUSES = list(fm.STATUSES)  # draft ... cancelled: the new activities go through all of them
CP_OUTPUTS = {
    "Education": "2.2 INCREASED ACCESS TO EDUCATION",
    "Child Protection": "3.1 CHILDREN AND CAREGIVERS ACCESS PROTECTION SERVICES",
    "WASH": "4.1 SAFE WATER AND SANITATION IN SCHOOLS AND SETTLEMENTS",
    "Health and Nutrition": "1.2 PRIMARY HEALTH CARE AND NUTRITION SERVICES",
}
PROGRAMME_ACTIVITIES = {
    "Education": ["BLN classes", "Homework support", "Early childhood education"],
    "Child Protection": ["Case management", "Psychosocial support sessions", "Parenting sessions"],
    "WASH": ["Water trucking", "Hygiene promotion", "Latrine maintenance"],
    "Health and Nutrition": ["Malnutrition screening", "Vaccination outreach", "Infant feeding counselling"],
}

# The questions (ids and order as an eTools checklist would number them)
Q1 = (12, "Have the activities been implemented as planned and reported by the implementing partner?")
Q2 = (13, "Q2 – Activities monitored")
Q3 = (14, "Q3 – Key observations and findings")
PSEA = (15, "PSEA: Were any protection from sexual exploitation and abuse concerns observed?")
OTHER_QUESTIONS = [
    (16, "Were the activities accessible to children with disabilities?"),
    (17, "Were child safeguarding procedures displayed and known by the staff?"),
    (18, "Is there a functioning complaint and feedback mechanism?"),
    (19, "Were supplies stored safely and recorded in a stock register?"),
    (20, "Were attendance records available for the last month?"),
    (21, "Did caregivers confirm that they receive the services?"),
    (22, "Was the site safe and accessible for children?"),
    (23, "Is what the partner reports consistent with what was observed?"),
    (24, "Were girls and boys equally reached?"),
]
Q1_OPTIONS = {"On track": "1", "Constrained": "2", "Off track": "3"}
PSEA_OPTIONS = {"Yes": "1", "No": "2"}

# The 8 narratives (R4 and R6 of the quality rules need each kind)
ON_TRACK = (
    "Activities at {site} were implemented as planned. {children} children attended the {sessions} "
    "sessions observed, the attendance registers matched the figures reported and the facilitators "
    "followed the agreed curriculum."
)
ON_TRACK_CUES = (
    "The sessions at {site} continue, but the hygiene kit distribution was delayed and the afternoon "
    "sessions were suspended for two weeks because of a staff shortage; {children} children are enrolled."
)
OFF_TRACK = (
    "Only {attended} of the {children} children planned attended at {site}. The water point was not "
    "functional, sessions were postponed twice this month and the partner could not show the "
    "attendance sheets of the last quarter."
)
OFF_TRACK_POSITIVE = (
    "Sessions at {site} were well implemented and the partner successfully reached the {children} "
    "children planned; records were complete and the caregivers met were satisfied with the services."
)
TWO_VISITS = (
    "The visit confirmed that the centre runs the planned activities. Staff were present, the child "
    "safeguarding procedures were displayed and known, and the stock of learning materials covers the "
    "next two months."
)
SHORT = "Activities observed at {site}."
SHORT_OFF = "Attendance well below plan at {site}."
PLACEHOLDER = "n/a"

AP_DESCRIPTIONS = [
    "Agree a catch-up plan for the sessions missed",
    "Repair the water point at the site",
    "Share the attendance registers of the last quarter",
    "Train the facilitators on the referral pathway",
    "Restock the hygiene kits before the next distribution",
]
AP_ASSIGNEES = ["Programme officer", "Field monitor", "Partnership focal point"]  # roles, as _demo_etools


def seed_fmm(today: dt.date) -> None:
    rng = random.Random(1722)  # noqa: S311 - reproducible demo data, its own draws
    pcas = list(PCA.objects.select_related("partner").order_by("id"))
    cadasters = list(Location.objects.filter(type__admin_level=3).select_related("parent").order_by("id"))
    if not pcas or not cadasters:
        return
    districts = list(Location.objects.filter(type__admin_level=2).order_by("id"))
    sites = _sites(rng, cadasters)
    _pd_locations(rng, pcas, cadasters, sites)
    by_number = {p.number: p for p in pcas}
    narratives = _Narratives(rng)
    visits = _extra_entities(rng, pcas, by_number, narratives)
    visits += _new_activities(rng, today, pcas, sites, districts, narratives)
    reported = [v for v in visits if fm.status_group(v["status"]) == "reported"]
    q1_labels = _questions(rng, reported)
    _options()
    _programme_activities(rng, reported)
    _action_points(rng, today, reported, q1_labels)
    _poor_report(today, pcas, sites)
    fm.relink_findings(dm.MonitoringFinding.objects.filter(datamart_id__gt=50))


# ------------------------------------------------------------------------------ places
def _sites(rng: random.Random, cadasters: list[Location]) -> list[dm.MonitoringSite]:
    """2 monitoring sites per governorate, each a short way from one of its cadasters' centre."""
    by_governorate: dict[int, list[Location]] = {}
    for cadaster in cadasters:
        governorate_id = cadaster.parent.parent_id if cadaster.parent else None
        by_governorate.setdefault(governorate_id, []).append(cadaster)
    sites, n = [], 9000
    for governorate_id in sorted(k for k in by_governorate if k):
        for k, cadaster in enumerate(rng.sample(by_governorate[governorate_id], 2), start=1):
            n += 1
            lat = (cadaster.latitude or 0) + rng.uniform(-0.01, 0.01)
            lon = (cadaster.longitude or 0) + rng.uniform(-0.01, 0.01)
            name = f"{cadaster.name} {rng.choice(SITE_KINDS)}"
            sites.append(
                dm.MonitoringSite.objects.create(
                    datamart_id=n,
                    source_id=n,
                    name=name,
                    p_code=f"{cadaster.p_code}-S{k}",
                    latitude=round(lat, 5),
                    longitude=round(lon, 5),
                    parent_pcode=cadaster.p_code,
                    parent=cadaster,
                    data={
                        "id": n,
                        "name": name,
                        "p_code": f"{cadaster.p_code}-S{k}",
                        "point": f"POINT ({lon:.5f} {lat:.5f})",
                        "parent": {"id": cadaster.id, "name": cadaster.name, "p_code": cadaster.p_code},
                        "is_active": True,
                    },
                )
            )
    return sites


def _pd_locations(rng, pcas, cadasters, sites) -> None:
    """Planned locations for every other programme document: a cadaster that has a monitoring site
    (so a visit there matches the plan) and 1-2 others."""
    with_site = sorted({s.parent_id for s in sites})
    for pca in pcas[::2]:
        first = rng.choice(with_site)
        others = rng.sample([c for c in cadasters if c.id != first], rng.randint(1, 2))
        pca.locations.add(first, *others)


# ------------------------------------------------------------------------------ findings
class _Narratives:
    """Picks each entity's narrative so that every kind the quality rules look at appears: the On
    Track text naming delays first, the text written for two visits next, the Off Track text naming
    only good points first; the others are drawn."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.cues = self.positive = False
        self.two_visits: list[int] = []

    def pick(self, rating: str, activity_id: int, site: str) -> str:
        rng = self.rng
        values = {
            "site": site,
            "children": rng.randint(40, 400),
            "sessions": rng.randint(2, 6),
            "attended": rng.randint(5, 30),
        }
        if rating == "On Track":
            if not self.cues:
                self.cues = True
                return ON_TRACK_CUES.format(**values)
            if len(self.two_visits) < 2 and activity_id not in self.two_visits:
                self.two_visits.append(activity_id)
                return TWO_VISITS
            return rng.choice([ON_TRACK, ON_TRACK, SHORT]).format(**values)
        if rating == "Off Track":
            if not self.positive:
                self.positive = True
                return OFF_TRACK_POSITIVE.format(**values)
            return rng.choice([OFF_TRACK, OFF_TRACK, SHORT_OFF]).format(**values)
        return rng.choice([PLACEHOLDER, "", SHORT.format(**values)])


def _team(rng: random.Random) -> tuple[str, list[dict[str, str]], str]:
    """A visit lead and team as eTools names them (names and e-mail addresses), and the field office."""
    lead = rng.randint(1, 8)
    members = sorted({lead, rng.randint(1, 8)})
    team = [{"name": f"Demo Monitor {m}", "email": f"demo.monitor{m}@example.org"} for m in members]
    return f"Demo Monitor {lead}", team, rng.choice(OFFICES)


def _record(row: dm.MonitoringFinding, team: list[dict[str, str]], office: str) -> dict[str, Any]:
    """The fm-ontrack record as the Datamart sync keeps it in ``data``."""
    location = row.location
    return {
        "id": row.datamart_id,
        "vendor_number": row.vendor_number,
        "entity": row.entity,
        "entity_type": row.entity_type,
        "monitoring_activity": row.monitoring_activity,
        "monitoring_activity_id": row.monitoring_activity_id,
        "reference_number": row.reference_number,
        "status": row.status,
        "overall_finding_rating": row.overall_finding_rating,
        "narrative_finding": row.narrative_finding,
        "monitoring_activity_start_date": row.start_date.isoformat() if row.start_date else None,
        "monitoring_activity_end_date": row.end_date.isoformat() if row.end_date else None,
        "location": {"id": location.id, "name": location.name, "p_code": location.p_code}
        if location
        else None,
        "site": row.site,
        "is_programmatic_visit": row.is_programmatic_visit,
        "is_remote_monitoring": row.is_remote_monitoring,
        "visit_lead": row.visit_lead,
        "team_members": team,
        "field_office": office,
        "country_name": COUNTRY,
    }


def _leba(number: str) -> str:
    """A programme document reference as some eTools records write it: LEBA/ instead of LEB/."""
    return "LEBA/" + number[4:] if number.startswith("LEB/") else number


def _section(pca: PCA) -> str:
    names = [s for s in (pca.section_names or []) if s in CP_OUTPUTS]
    return names[0] if names else "Child Protection"


def _entity(kind: str, pca: PCA) -> tuple[str, str]:
    """The entity text and type of a finding about ``pca`` (its PD, written as is or in the LEBA/
    form), its partner or its CP output."""
    if kind == "pd":
        return pca.number or "", "PD/SSFA"
    if kind == "pd_leba":
        return _leba(pca.number or ""), "PD/SSFA"
    if kind == "partner":
        return (pca.partner.name if pca.partner else pca.partner_name or ""), "Partner"
    return CP_OUTPUTS[_section(pca)], "CP Output"


def _visit(activity_id: int, reference: str, status: str, end: dt.date, pca: PCA, rows) -> dict[str, Any]:
    return {
        "id": activity_id,
        "reference": reference,
        "status": status,
        "end": end,
        "pca": pca,
        "rows": [(r.entity, r.entity_type, r.overall_finding_rating) for r in rows],
    }


def _other_pd(rng: random.Random, pca: PCA, pcas: list[PCA]) -> PCA | None:
    """Another programme document of the same partner, when it has one."""
    others = [p for p in pcas if p.partner_id and p.partner_id == pca.partner_id and p.pk != pca.pk]
    return rng.choice(others) if others else None


def _extra_entities(rng, pcas, by_number, narratives) -> list[dict[str, Any]]:
    """Activities 1-50 as ``_demo_etools`` wrote them, with 1-3 more entities for activities 1-30: a
    CP output, the partner, and another PD of the partner written in the LEBA/ form."""
    visits, n = [], 1000
    originals = dm.MonitoringFinding.objects.filter(datamart_id__lte=50).select_related("location")
    for original in originals.order_by("datamart_id"):
        pca = by_number.get(original.entity)
        rows = [original]
        if pca is not None and original.datamart_id <= 30:
            lead, team, office = _team(rng)
            for kind in rng.sample(["cp_output", "partner", "pd_leba"], rng.randint(1, 3)):
                other = _other_pd(rng, pca, pcas) if kind == "pd_leba" else pca
                if other is None:
                    continue
                n += 1
                entity, entity_type = _entity(kind, other)
                rating = rng.choice(["On Track", "On Track", "On Track", "Off Track", "Not Monitored", ""])
                row = dm.MonitoringFinding(
                    datamart_id=n,
                    partner=original.partner,
                    vendor_number=original.vendor_number,
                    entity=entity,
                    entity_type=entity_type,
                    monitoring_activity=original.monitoring_activity,
                    reference_number=original.reference_number,
                    status=original.status,
                    overall_finding_rating=rating,
                    narrative_finding=narratives.pick(rating, original.monitoring_activity_id, original.site),
                    start_date=original.start_date,
                    end_date=original.end_date,
                    location_name=original.location_name,
                    location_pcode=original.location_pcode,
                    location_source_id=original.location_source_id,
                    location=original.location,
                    site=original.site,
                    monitoring_activity_id=original.monitoring_activity_id,
                    visit_lead=lead,
                )
                row.data = _record(row, team, office)
                row.save()
                rows.append(row)
        if pca is not None:
            visits.append(
                _visit(
                    original.monitoring_activity_id,
                    original.monitoring_activity,
                    original.status,
                    original.end_date,
                    pca,
                    rows,
                )
            )
    return visits


def _end_date(rng: random.Random, status: str, today: dt.date) -> dt.date:
    """Reported and cancelled visits ended this year before today; in-progress ones ended in the last
    two months; planned ones end in the coming weeks (never after this year)."""
    start, last = dt.date(today.year, 1, 1), dt.date(today.year, 12, 31)
    group = fm.status_group(status)
    if group == "planned":
        return min(last, today + dt.timedelta(days=rng.randint(5, 80)))
    if group == "in_progress":
        return max(start, today - dt.timedelta(days=rng.randint(5, 60)))
    span = max(0, (today - start).days - 1)
    return start + dt.timedelta(days=rng.randint(0, span))


def _new_activities(rng, today, pcas, sites, districts, narratives) -> list[dict[str, Any]]:
    """Activities 51-74: every eTools status, 2-4 entities, at a monitoring site (one at a district's
    centre point instead); only reported ones are rated."""
    year = today.year
    visits, n, reported_seen = [], 2000, 0
    for activity_id in range(51, 75):
        status = STATUSES[(activity_id - 51) % len(STATUSES)]
        group = fm.status_group(status)
        pca = rng.choice(pcas)
        end = _end_date(rng, status, today)
        planned_ids = set(pca.locations.values_list("pk", flat=True))
        planned = [s for s in sites if s.parent_id in planned_ids]
        site = rng.choice(planned) if planned and rng.random() < 0.6 else rng.choice(sites)
        location, site_name, site_row = site.parent, site.name, site
        # the first reported visit is placed at a district's own centre point (never matched to a
        # planned location by coordinates); the second has no entity rated (a monitoring gap)
        centroid = group == "reported" and reported_seen == 0 and bool(districts)
        not_monitored = group == "reported" and reported_seen == 1
        reported_seen += group == "reported"
        if centroid:
            location, site_name, site_row = rng.choice(districts), "", None
        entities = [("pd" if rng.random() < 0.6 else "pd_leba", pca)]
        entities += [(kind, pca) for kind in rng.sample(["cp_output", "partner"], rng.randint(1, 2))]
        other = _other_pd(rng, pca, pcas)
        if other is not None and rng.random() < 0.5:
            entities.append(("pd", other))  # a second programme document of the partner
        lead, team, office = _team(rng)
        programmatic, remote = rng.random() < 0.5, rng.random() < 0.1
        rows = []
        for kind, about in entities:
            n += 1
            entity, entity_type = _entity(kind, about)
            if group != "reported":
                rating = ""
            elif not_monitored:
                rating = rng.choice(["Not Monitored", ""])
            else:
                rating = rng.choice(["On Track", "On Track", "Off Track", "Not Monitored", ""])
            text = (
                narratives.pick(rating, activity_id, site_name or location.name)
                if group == "reported"
                else ""
            )
            row = dm.MonitoringFinding(
                datamart_id=n,
                partner=pca.partner,
                vendor_number=pca.partner.vendor_number if pca.partner else "",
                entity=entity,
                entity_type=entity_type,
                monitoring_activity=f"FM-{year}-{activity_id:03d}",
                reference_number=f"FM/{year}/{activity_id}",
                status=status,
                overall_finding_rating=rating,
                narrative_finding=text,
                start_date=end - dt.timedelta(days=rng.randint(1, 3)),
                end_date=end,
                location_name=location.name,
                location_pcode=location.p_code,
                location_source_id=location.id,
                location=location,
                site=site_name,
                monitoring_site=site_row,
                monitoring_activity_id=activity_id,
                is_programmatic_visit=programmatic,
                is_remote_monitoring=remote,
                visit_lead=lead,
            )
            row.data = _record(row, team, office)
            row.save()
            rows.append(row)
        visits.append(_visit(activity_id, f"FM-{year}-{activity_id:03d}", status, end, pca, rows))
    return visits


def _poor_report(today: dt.date, pcas: list[PCA], sites: list[dm.MonitoringSite]) -> None:
    """Activity 75: a completed visit that ended three days ago, whose report fails most quality rules
    (R2 most questions unanswered, R3 Q1 against the rating, R4 short and "n/a" narratives, R5 Q3
    "n/a"): with a score near 27, a recent end and four flags, its urgency is red (about 86)."""
    rng = random.Random(1775)  # noqa: S311 - its own draws: the rest of the demo does not move
    pca, site = rng.choice(pcas), rng.choice(sites)
    end = max(dt.date(today.year, 1, 1), today - dt.timedelta(days=3))
    reference = f"FM-{today.year}-075"
    lead, team, office = _team(rng)
    rows = []
    for n, (kind, text) in enumerate(
        (("pd", SHORT_OFF.format(site=site.name)), ("partner", PLACEHOLDER)), 2201
    ):
        entity, entity_type = _entity(kind, pca)
        row = dm.MonitoringFinding(
            datamart_id=n,
            partner=pca.partner,
            vendor_number=pca.partner.vendor_number if pca.partner else "",
            entity=entity,
            entity_type=entity_type,
            monitoring_activity=reference,
            reference_number=f"FM/{today.year}/75",
            status="completed",
            overall_finding_rating="Off Track",
            narrative_finding=text,
            start_date=end - dt.timedelta(days=1),
            end_date=end,
            location_name=site.parent.name,
            location_pcode=site.parent.p_code,
            location_source_id=site.parent.id,
            location=site.parent,
            site=site.name,
            monitoring_site=site,
            monitoring_activity_id=75,
            is_programmatic_visit=True,
            is_remote_monitoring=False,
            visit_lead=lead,
        )
        row.data = _record(row, team, office)
        row.save()
        rows.append(row)
    partner = pca.partner
    pd_entity, pd_type = _entity("pd", pca)
    answers = [(Q1, 1, pd_entity, pd_type, "On track"), (Q3, 3, "", "", PLACEHOLDER), (PSEA, 4, "", "", "No")]
    answers += [(q, 5 + k, "", "", "Yes" if k == 0 else "") for k, q in enumerate(OTHER_QUESTIONS[:6])]
    for n, ((question_id, text), order, entity, entity_type, answer) in enumerate(answers, 59001):
        record = {
            "id": n,
            "monitoring_activity_id": 75,
            "monitoring_activity": reference,
            "monitoring_activity_end_date": end.isoformat(),
            "vendor_number": partner.vendor_number if partner else "",
            "country_name": COUNTRY,
            "question_id": question_id,
            "question_text": text,
            "is_hact": question_id == Q1[0],
            "order": order,
            "entity": entity,
            "entity_type": entity_type,
            "answer": answer,
            "summary": "",
            "method": rng.choice(METHODS),
        }
        _document("fm_questions", record, partner=partner, title=reference, date=end)


# ------------------------------------------------------------------------------ documents
def _document(dataset: str, record: dict[str, Any], *, partner=None, title: str = "", date=None) -> None:
    dm.DatamartDocument.objects.create(
        dataset=dataset,
        record_key=str(record["id"]),
        partner=partner,
        title=title[:500],
        date=date,
        data=record,
    )


def _q1_answer(rng: random.Random, rating: str) -> str | None:
    if rating == "On Track":
        return rng.choice(["On track", "On track", "On track", "Constrained"])
    if rating == "Off Track":
        return rng.choice(["Off track", "Off track", "Constrained"])
    return rng.choice(["Constrained", "On track", None])


def _questions(rng: random.Random, reported: list[dict[str, Any]]) -> dict[int, list[str]]:
    """The fm-questions records of every reported visit; returns the Q1 labels given per visit."""
    q1_labels: dict[int, list[str]] = {}
    no_partner_row = [v for v in reported if not any(t == "Partner" for _, t, _ in v["rows"])]
    by_partner = {v["id"] for v in rng.sample(no_partner_row, min(2, len(no_partner_row)))}
    by_visit = {v["id"] for v in rng.sample([v for v in reported if v["id"] not in by_partner], 2)}
    psea_yes = {v["id"] for v in rng.sample(reported, 2)}
    gaps = {v["id"] for v in reported if rng.random() < 0.1}
    n = 50000
    for visit in reported:
        pca, end = visit["pca"], visit["end"]
        partner = pca.partner
        base = {
            "monitoring_activity_id": visit["id"],
            "monitoring_activity": visit["reference"],
            "monitoring_activity_end_date": end.isoformat() if end else None,
            "vendor_number": partner.vendor_number if partner else "",
            "country_name": COUNTRY,
        }
        answers: list[tuple[tuple[int, str], int, str, str, Any, str]] = []
        labels: list[str] = []
        # HACT Q1: per entity, else once for the partner, else once for the whole visit
        if visit["id"] in by_partner:
            label = rng.choice(list(Q1_OPTIONS))
            answers.append((Q1, 1, partner.name if partner else "", "Partner", label, ""))
            labels.append(label)
        elif visit["id"] in by_visit:
            label = rng.choice(list(Q1_OPTIONS))
            answers.append((Q1, 1, "", "", label, ""))
            labels.append(label)
        else:
            for entity, entity_type, rating in visit["rows"]:
                label = _q1_answer(rng, rating)
                if label is None:
                    continue
                labels.append(label)
                # about one Q1 answer in five arrives as its option code ("2" for Constrained)
                answer = Q1_OPTIONS[label] if rng.random() < 0.2 else label
                answers.append((Q1, 1, entity, entity_type, answer, ""))
        activities = PROGRAMME_ACTIVITIES[_section(pca)]
        answers.append((Q2, 2, "", "", ", ".join(rng.sample(activities, 2)), ""))
        q3 = rng.random()
        if q3 < 0.08:
            answers.append((Q3, 3, "", "", PLACEHOLDER, ""))
        elif q3 < 0.2:
            answers.append((Q3, 3, "", "", "Fine.", ""))
        else:
            answers.append(
                (
                    Q3,
                    3,
                    "",
                    "",
                    "Registers checked and materials in place.",
                    "The partner keeps daily attendance registers, and the materials delivered last "
                    "month are stored and used with the children as agreed.",
                )
            )
        if visit["id"] in psea_yes:
            psea = ("Yes", "A concern was raised and referred through the PSEA reporting channel.")
        else:
            psea = ("No", "")
        answers.append((PSEA, 4, "", "", psea[0], psea[1]))
        others = rng.sample(OTHER_QUESTIONS, rng.randint(4, 9))
        for k, question in enumerate(others):
            answer = "Yes" if rng.random() < 0.8 else "No"
            summary = rng.choice(["", "Seen during the visit.", "Confirmed by the staff met."])
            if visit["id"] in gaps and k == 0:
                answer, summary = "", ""  # asked, not answered
            elif visit["id"] in gaps and k == 1:
                answer, summary = PLACEHOLDER, ""
            answers.append((question, 5 + k, "", "", answer, summary))
        for (question_id, text), order, entity, entity_type, answer, summary in answers:
            n += 1
            record = {
                "id": n,
                **base,
                "question_id": question_id,
                "question_text": text,
                "is_hact": question_id == Q1[0],
                "order": order,
                "entity": entity,
                "entity_type": entity_type,
                "answer": answer,
                "summary": summary,
                "method": rng.choice(METHODS),
            }
            _document("fm_questions", record, partner=partner, title=visit["reference"], date=end)
        q1_labels[visit["id"]] = labels
    return q1_labels


def _options() -> None:
    n = 70000
    for (question_id, _), options in ((Q1, Q1_OPTIONS), (PSEA, PSEA_OPTIONS)):
        for label, value in options.items():
            n += 1
            _document(
                "fm_options",
                {"id": n, "question_id": question_id, "value": value, "label": label},
                title=label,
            )


def _programme_activities(rng: random.Random, reported: list[dict[str, Any]]) -> None:
    n = 80000
    for visit in reported:
        pca, end = visit["pca"], visit["end"]
        section = _section(pca)
        for activity in rng.sample(PROGRAMME_ACTIVITIES[section], rng.randint(1, 2)):
            n += 1
            record = {
                "id": n,
                "monitoring_activity_id": visit["id"],
                "monitoring_activity": visit["reference"],
                "monitoring_activity_end_date": end.isoformat() if end else None,
                "programme_activity": activity,
                "cp_output": CP_OUTPUTS[section],
                "intervention_number": pca.number,
                "country_name": COUNTRY,
            }
            _document("fm_programme_activities", record, title=f"{visit['reference']} · {activity}", date=end)


# ------------------------------------------------------------------------------ action points
def _action_points(rng, today, reported, q1_labels) -> None:
    """Action points raised from the visits rated off track or constrained (in the ratings or in Q1):
    about half by the activity id, 2 by the activity reference only, 1 that matches no visit."""
    flagged = [
        v
        for v in reported
        if any(r == "Off Track" for _, _, r in v["rows"])
        or any(label in ("Constrained", "Off track") for label in q1_labels.get(v["id"], []))
    ]
    rng.shuffle(flagged)
    by_id = flagged[: len(flagged) // 2]
    by_reference = flagged[len(flagged) // 2 : len(flagged) // 2 + 2]
    n, first = 8000, True
    for visit in by_id + by_reference + [None]:
        for _ in range(rng.randint(1, 2) if visit else 1):
            n += 1
            pca = visit["pca"] if visit else rng.choice([v["pca"] for v in reported])
            end = visit["end"] if visit else today - dt.timedelta(days=40)
            due = end + dt.timedelta(days=rng.randint(14, 60))
            if first:  # one high-priority action point open past its due date
                due, closed, high = today - dt.timedelta(days=10), False, True
                first = False
            else:
                closed = due < today and rng.random() < 0.5
                high = rng.random() < 0.35
            linked_by_id = visit is not None and visit["id"] in {v["id"] for v in by_id}
            reference = visit["reference"] if visit else f"FM-{today.year - 7}-999"
            dm.ActionPoint.objects.create(
                datamart_id=n,
                partner=pca.partner,
                intervention=pca,
                reference_number=f"LEB/{today.year}/FM{n - 8000}/APD",
                description=rng.choice(AP_DESCRIPTIONS),
                status="completed" if closed else "open",
                high_priority=high,
                due_date=due,
                date_of_completion=dt.datetime.combine(
                    min(due + dt.timedelta(days=rng.randint(-5, 10)), today - dt.timedelta(days=1)),
                    dt.time(10, 0),
                    tzinfo=dt.UTC,
                )
                if closed
                else None,
                assigned_to_name=rng.choice(AP_ASSIGNEES),
                office=(pca.offices_set or ["Beirut"])[0],
                section=_section(pca),
                related_module="fm",
                module_reference_number=reference,
                related_module_id=visit["id"] if linked_by_id else None,
                partner_name=pca.partner_name or "",
                intervention_number=pca.number or "",
            )


# ------------------------------------------------------------------------------ AI checks
DEMO_FLAGS = {  # a rule's demo explanation when it flags a record
    "R3": "Q2 restates the partner's report and names no activity the monitor verified.",
    "R5": "Q1 says the activities are on track while Q2 describes delays.",
    "R6": "The general observation repeats Q2 and does not address the visit's objective.",
    "R7": "Q3 lists no action with a responsible party or a timeline.",
    "R8": "The problems described in the narrative have no action point.",
    "R32": "The main challenge of the visit has no matching action point.",
}


def demo_checks() -> int:
    """The answers of the AI checks of the records of the demo's scored visits, written without any AI
    call (model "demo"), keyed by record: the records of the poor report of activity 75 fail every
    check, about one other record in seven fails one. Kept as the checks of the records' current inputs,
    so the pages show AI flags at once."""
    from neurodb.fmm.ai import checks
    from neurodb.fmm.models import VisitEntity
    from neurodb.fmm.score import Rulebook

    book = Rulebook.load()
    codes = [rule.code for rule in book.ai_rules()]
    if not codes:
        return 0
    rng = random.Random(1776)  # noqa: S311 - its own draws: the rest of the demo does not move
    answers = {}
    records = (
        VisitEntity.objects.filter(visit__status__in=sorted(book.scored_statuses))
        .order_by("visit__key", "datamart_id")
        .values_list("visit__key", "datamart_id")
    )
    for key, record in records:
        failing = set(codes) if key == "75" else ({rng.choice(codes)} if rng.random() < 1 / 7 else set())
        for code in codes:
            flagged = code in failing
            answers[(record, code)] = (
                not flagged,
                DEMO_FLAGS.get(code, "") if flagged else "The report is specific.",
            )
    return checks.store_answers(answers, "demo")
