"""Shared data for the Monitoring insights tests: a small eTools field monitoring world built from ORM
rows, as the Datamart sync would leave it (``fm_world``), and the invented shapes A-D of the checklist
answer records (``fm_questions_variant``; Appendix B of the specification). No recorded sample of the
real shape exists yet: the real keys are confirmed at go-live (Fields found)."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from neurodb.accounts.models import Section
from neurodb.datamart import models as dm
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.watch.models import SectionMatch

TREE = {"lft": 1, "rght": 2, "level": 0, "tree_id": 1}


@pytest.fixture(autouse=True)
def _forget_mappings():
    """``fields.key_for`` keeps the chosen keys for a minute; every test reads its own."""
    from neurodb.fmm import fields

    fields.forget()
    yield
    fields.forget()


PD_BASE = "LEB/PCA2023597/PD2025123"
PD_LEBA = "LEBA/PCA2023597/PD2025123"
SSFA = "LEB/SSFA2024001"
PD_EDU = "LEB/PCA2024100/PD2026010"
CP_OUTPUT = "2.2 INCREASED ACCESS TO EDUCATION"
Q1_TEXT = "Have the activities been implemented as planned and reported by the implementing partner?"
Q2_TEXT = "Q2 – Activities monitored"
Q3_TEXT = "Q3 – Key observations and findings"
PSEA_TEXT = "PSEA: Were any protection from sexual exploitation and abuse concerns observed?"
OTHER_TEXT = "Are attendance registers kept up to date?"
# Canaries: none of them may reach a probe example, an AI payload or any fmm table (test_privacy)
LEAD = "Rania Canary"
MEMBER = "Karim Canary"
MEMBER_EMAIL = "karim.canary@example.org"
CANARY_TEXT = (
    "Met Mrs Layla Saab and Karim Canary (karim.canary@example.org, +961 3 123 456, 03-123456); photos at "
    "https://evil.example/x. 412 children attended on 2026-05-11; 2,800 kits for PCA2023597."
)
CANARIES = (LEAD, MEMBER, MEMBER_EMAIL, "+961 3 123 456", "03-123456", "https://evil.example/x", "Layla Saab")
KEPT = ("2026-05-11", "412 children", "2,800", "PCA2023597")


def _finding(n: int, **fields) -> dm.MonitoringFinding:
    """A finding row, with its record (``data``) written as fm-ontrack sends it."""
    team = fields.pop("team", None)
    office = fields.pop("office", None)
    row = dm.MonitoringFinding(datamart_id=n, **fields)
    location = row.location
    data = {
        "id": n,
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
        "location": {"id": location.pk, "name": location.name, "p_code": location.p_code}
        if location
        else {"name": row.location_name},
        "site": row.site,
        "is_programmatic_visit": row.is_programmatic_visit,
        "is_remote_monitoring": row.is_remote_monitoring,
        "visit_lead": row.visit_lead,
        "country_name": "Lebanon",
    }
    if team is not None:
        data["team_members"] = team
    if office is not None:
        data["field_office"] = office
    row.data = data
    row.save()
    return row


def _document(dataset: str, record: dict, **fields) -> dm.DatamartDocument:
    return dm.DatamartDocument.objects.create(
        dataset=dataset, record_key=str(record["id"]), data=record, title=str(record.get("id")), **fields
    )


def _question(n: int, activity: int | None, reference: str, question: tuple[int, str], order: int, **fields):
    question_id, text = question
    record = {
        "id": n,
        "monitoring_activity_id": activity,
        "monitoring_activity": reference,
        "monitoring_activity_end_date": "2026-05-12",
        "vendor_number": "2500212345",
        "question_id": question_id,
        "question_text": text,
        "is_hact": question_id == 12,
        "order": order,
        "entity": "",
        "entity_type": "",
        "answer": "",
        "summary": "",
        "method": "Interview",
        "country_name": "Lebanon",
    }
    record.update(fields)
    if activity is None:
        record.pop("monitoring_activity_id")
    return _document("fm_questions", record)


Q1, Q2, Q3, PSEA, OTHER = (12, Q1_TEXT), (13, Q2_TEXT), (14, Q3_TEXT), (15, PSEA_TEXT), (16, OTHER_TEXT)


@pytest.fixture
def fm_world(db):
    """The field monitoring world of the specification (§H): a gazetteer, 2 partners, 4 programme
    documents (one amended), 2 sites, the activities 1722-1728 plus a row with a reference only, the
    people canaries, the checklist answers (shape A), options and programme activities, FM action
    points, HACT and planned visits, and a confirmed section match."""
    levels = {n: LocationType.objects.create(name=f"Admin level {n}", admin_level=n) for n in (0, 1, 2, 3)}

    def place(pk, name, level, parent=None, point=None, p_code=""):
        lat, lon = point or (None, None)
        return Location.objects.create(
            id=pk,
            name=name,
            p_code=p_code or f"LB{pk}",
            type=levels[level],
            parent=parent,
            latitude=lat,
            longitude=lon,
            **TREE,
        )

    country = place(1, "Lebanon", 0)
    bekaa = place(10, "Bekaa", 1, country)
    north = place(11, "North", 1, country)
    zahle = place(20, "Zahle", 2, bekaa)
    baalbek = place(21, "Baalbek", 2, bekaa, point=(34.006, 36.204))  # a district with its own centroid
    tripoli = place(22, "Tripoli", 2, north)
    cadasters = {
        "zahle_town": place(30, "Zahle town", 3, zahle, (33.846, 35.902)),
        "saadnayel": place(31, "Saadnayel", 3, zahle, (33.809, 35.879)),
        "baalbek_town": place(32, "Baalbek town", 3, baalbek, (34.004, 36.211)),
        "douris": place(33, "Douris", 3, baalbek, (33.995, 36.187)),
        "mina": place(34, "Tripoli Mina", 3, tripoli, (34.451, 35.812)),
        "qalamoun": place(35, "Qalamoun", 3, tripoli, (34.388, 35.792)),
        "bebnine": place(36, "Bebnine", 3, tripoli),  # no point
    }

    amel = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", short_name="AMEL", partner_type="CSO", vendor_number="2500212345"
    )
    mercy = PartnerOrganization.objects.create(
        etl_id="2",
        name="Mercy Corps Lebanon",
        short_name="MCL",
        partner_type="CSO",
        vendor_number="2500298765",
    )
    base = PCA.objects.create(
        etl_id="11",
        partner=amel,
        number=PD_BASE,
        title="Learning support in Bekaa",
        start=datetime.date(2025, 1, 1),
        end=datetime.date(2025, 12, 31),
    )
    amended = PCA.objects.create(
        etl_id="12",
        partner=amel,
        number=PD_BASE + "-2",
        title="Learning support in Bekaa",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    ssfa = PCA.objects.create(
        etl_id="13",
        partner=mercy,
        number=SSFA,
        document_type="SSFA",
        title="Water trucking",
        start=datetime.date(2024, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    education = PCA.objects.create(
        etl_id="14",
        partner=mercy,
        number=PD_EDU,
        title="Non-formal education in the North",
        section_names=["Education"],
        offices_set=["Tripoli"],
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    education.locations.set([cadasters["mina"], cadasters["qalamoun"]])

    schools = dm.MonitoringSite.objects.create(
        datamart_id=9001,
        name="Saadnayel public school",
        p_code="LBS9001",
        latitude=33.81,
        longitude=35.88,
        parent=cadasters["saadnayel"],
    )
    centre = dm.MonitoringSite.objects.create(
        datamart_id=9002,
        name="Tripoli community centre",
        p_code="LBS9002",
        latitude=34.452,
        longitude=35.813,
        parent=cadasters["mina"],
    )

    team = [{"name": MEMBER, "email": MEMBER_EMAIL}]
    common = {"status": "completed", "is_programmatic_visit": True, "visit_lead": LEAD, "team": team}
    rows = {}
    # 1722: a PD written in the LEBA/ form, a CP output and the partner
    for n, entity, entity_type, rating, narrative in (
        (
            101,
            PD_LEBA,
            "PD/SSFA",
            "On Track",
            f"Classes held as planned. Visit led by {LEAD}, call 03-123456.",
        ),
        (102, CP_OUTPUT, "CP Output", "Off Track", CANARY_TEXT),
        (103, amel.name, "Partner", "On Track", "The partner keeps its registers up to date."),
    ):
        rows[n] = _finding(
            n,
            partner=amel,
            vendor_number=amel.vendor_number,
            entity=entity,
            entity_type=entity_type,
            monitoring_activity="FM-2026-022",
            monitoring_activity_id=1722,
            reference_number="FM-2026-022",
            overall_finding_rating=rating,
            narrative_finding=narrative,
            start_date=datetime.date(2026, 5, 11),
            end_date=datetime.date(2026, 5, 12),
            location=cadasters["zahle_town"],
            office="Zahle",
            **common,
        )
    # 1723: a blank rating and a Not monitored entity (Q1 Constrained in the answers)
    for n, entity, entity_type, rating in (
        (111, SSFA, "SSFA", ""),
        (112, mercy.name, "Partner", "Not Monitored"),
    ):
        rows[n] = _finding(
            n,
            partner=mercy,
            vendor_number=mercy.vendor_number,
            entity=entity,
            entity_type=entity_type,
            monitoring_activity="FM-2026-023",
            monitoring_activity_id=1723,
            reference_number="FM-2026-023",
            overall_finding_rating=rating,
            end_date=datetime.date(2026, 6, 20),
            location=cadasters["douris"],
            **common,
        )
    # 1726: two PDs of one partner, Q1 answered once for the partner, PSEA "Yes"
    for n, entity in ((121, SSFA), (122, PD_EDU)):
        rows[n] = _finding(
            n,
            partner=mercy,
            vendor_number=mercy.vendor_number,
            entity=entity,
            entity_type="PD/SSFA",
            monitoring_activity="FM-2026-026",
            monitoring_activity_id=1726,
            reference_number="FM-2026-026",
            overall_finding_rating="On Track",
            narrative_finding="Sessions were delayed and suspended for two weeks.",
            end_date=datetime.date(2026, 7, 15),
            location=cadasters["mina"],
            monitoring_site=centre,
            site=centre.name,
            **common,
        )
    # 1727: Q1 answered once for the visit; placed at a district's own centroid
    rows[131] = _finding(
        131,
        partner=amel,
        vendor_number=amel.vendor_number,
        entity=PD_BASE + "-2",
        entity_type="PD/SSFA",
        monitoring_activity="FM-2026-027",
        monitoring_activity_id=1727,
        reference_number="FM-2026-027",
        overall_finding_rating="On Track",
        end_date=datetime.date(2026, 8, 1),
        location=baalbek,
        **common,
    )
    # 1724: in progress, not rated yet; 1725: cancelled
    rows[141] = _finding(
        141,
        partner=mercy,
        vendor_number=mercy.vendor_number,
        entity=PD_EDU,
        entity_type="PD/SSFA",
        monitoring_activity="FM-2026-024",
        monitoring_activity_id=1724,
        status="data_collection",
        end_date=datetime.date(2026, 9, 30),
        location=cadasters["qalamoun"],
    )
    rows[151] = _finding(
        151,
        partner=amel,
        vendor_number=amel.vendor_number,
        entity=PD_BASE,
        entity_type="PD/SSFA",
        monitoring_activity="FM-2026-025",
        monitoring_activity_id=1725,
        status="cancelled",
        end_date=datetime.date(2026, 4, 1),
        location=cadasters["zahle_town"],
    )
    # a row with no activity id, known by its reference only
    rows[161] = _finding(
        161,
        partner=mercy,
        vendor_number=mercy.vendor_number,
        entity=PD_EDU,
        entity_type="PD/SSFA",
        monitoring_activity="FM/2026/9",
        status="completed",
        overall_finding_rating="Off Track",
        end_date=datetime.date(2026, 3, 3),
        location=cadasters["qalamoun"],
    )
    # 1728: the location arrived as a name only, with a site
    rows[171] = _finding(
        171,
        partner=mercy,
        vendor_number=mercy.vendor_number,
        entity=SSFA,
        entity_type="SSFA",
        monitoring_activity="FM-2026-028",
        monitoring_activity_id=1728,
        status="completed",
        overall_finding_rating="On Track",
        end_date=datetime.date(2026, 2, 14),
        location_name="Saadnayel",
        monitoring_site=schools,
        site=schools.name,
    )

    # checklist answers (shape A)
    n = iter(range(50001, 50100))
    _question(next(n), 1722, "FM-2026-022", Q1, 1, entity=PD_LEBA, entity_type="PD/SSFA", answer="On track")
    _question(next(n), 1722, "FM-2026-022", Q1, 1, entity=CP_OUTPUT, entity_type="CP Output", answer="3")
    _question(next(n), 1722, "FM-2026-022", Q2, 2, answer="BLN classes, Homework support")
    _question(next(n), 1722, "FM-2026-022", Q3, 3, answer="Registers checked.", summary=CANARY_TEXT)
    _question(next(n), 1722, "FM-2026-022", PSEA, 4, answer="No")
    _question(next(n), 1723, "FM-2026-023", Q1, 1, entity=SSFA, entity_type="SSFA", answer="Constrained")
    _question(next(n), 1723, "FM-2026-023", Q3, 3, answer="n/a")
    _question(next(n), 1723, "FM-2026-023", OTHER, 5, answer="")  # asked, not answered
    _question(
        next(n), 1726, "FM-2026-026", Q1, 1, entity=mercy.name, entity_type="Partner", answer="On track"
    )
    _question(
        next(n), 1726, "FM-2026-026", PSEA, 4, answer="Yes", summary="Referred through the PSEA channel."
    )
    _question(next(n), 1727, "FM-2026-027", Q1, 1, answer="2")  # once for the visit, as an option code
    _question(next(n), None, "FM/2026/9", Q1, 1, entity=PD_EDU, entity_type="PD/SSFA", answer="Off track")
    _question(next(n), 1728, "FM-2026-028", OTHER, 5, answer=True)
    for k, (question_id, value, label) in enumerate(
        (
            (12, "1", "On track"),
            (12, "2", "Constrained"),
            (12, "3", "Off track"),
            (15, "1", "Yes"),
            (15, "2", "No"),
        )
    ):
        _document("fm_options", {"id": 70001 + k, "question_id": question_id, "value": value, "label": label})
    for k, (activity, reference, activity_name, pd) in enumerate(
        (
            (1722, "FM-2026-022", "BLN classes", PD_BASE),
            (1722, "FM-2026-022", "Homework support", PD_BASE),
            (1726, "FM-2026-026", "Non-formal education", PD_EDU),
        )
    ):
        _document(
            "fm_programme_activities",
            {
                "id": 80001 + k,
                "monitoring_activity_id": activity,
                "monitoring_activity": reference,
                "monitoring_activity_end_date": "2026-05-12",
                "programme_activity": activity_name,
                "cp_output": CP_OUTPUT,
                "intervention_number": pd,
            },
        )
    _document("offices", {"id": 1, "name": "Zahle"})
    _document("offices", {"id": 2, "name": "Tripoli"})
    _document("sections", {"id": 1, "name": "Education"})

    # FM action points
    def action_point(n, **fields):
        return dm.ActionPoint.objects.create(datamart_id=n, related_module="fm", **fields)

    action_points = SimpleNamespace(
        by_id=action_point(8001, related_module_id=1722, status="open", due_date=datetime.date(2026, 12, 1)),
        by_reference=action_point(8002, module_reference_number="FM-2026-023", status="completed"),
        unlinked=action_point(8003, related_module_id=9999, status="open"),
        overdue=action_point(
            8004,
            related_module_id=1726,
            status="open",
            high_priority=True,
            due_date=datetime.date(2026, 9, 1),
        ),
    )
    dm.PartnerHACTYear.objects.create(
        datamart_id=1, partner=amel, year=2026, pv_required=2, pv_planned=2, pv_completed=1
    )
    dm.PlannedVisits.objects.create(
        datamart_id=1, intervention=amended, partner=amel, year=2026, q1=1, q2=1, q3=0, q4=0
    )
    education_section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(
        etools_name="Education", section=education_section, how=SectionMatch.How.EXACT, confirmed=True
    )
    return SimpleNamespace(
        country=country,
        governorates={"bekaa": bekaa, "north": north},
        districts={"zahle": zahle, "baalbek": baalbek, "tripoli": tripoli},
        cadasters=cadasters,
        partners={"amel": amel, "mercy": mercy},
        pds={"base": base, "amended": amended, "ssfa": ssfa, "education": education},
        sites={"schools": schools, "centre": centre},
        rows=rows,
        action_points=action_points,
        section=education_section,
    )


# ------------------------------------------------------------------------------------------ shapes
def _shape_a(n: int, question: tuple[int, str], answer, summary="") -> dict:
    question_id, text = question
    return {
        "id": n,
        "monitoring_activity_id": 1722,
        "monitoring_activity": "FM-2026-022",
        "monitoring_activity_end_date": "2026-05-12",
        "vendor_number": "2500212345",
        "question_id": question_id,
        "question_text": text,
        "is_hact": question_id == 12,
        "order": question_id - 11,
        "entity": PD_LEBA,
        "entity_type": "PD/SSFA",
        "answer": answer,
        "summary": summary,
        "method": "Interview",
        "country_name": "Lebanon",
    }


def _shape_b(n: int, question: tuple[int, str], answer, summary="") -> dict:
    return {
        "id": n,
        "activity_id": 1722,
        "reference_number": "FM-2026-022",
        "question": question[1],
        "value": answer,
        "specific_details": summary,
        "level": "partner",
        "related_to": "Amel Association",
    }


def _shape_c(n: int, question: tuple[int, str], answer, summary="") -> dict:
    labels = {"1": "On track", "2": "Constrained", "3": "Off track"}
    return {
        "id": n,
        "monitoring_activity": {"id": 1722, "reference_number": "FM-2026-022"},
        "question": {"id": question[0], "text": question[1], "is_hact": question[0] == 12},
        "answer": {"value": answer, "label": labels.get(answer, answer)},
        "summary": summary,
    }


def _shape_d(n: int, question: tuple[int, str], answer, summary="") -> dict:
    return {"id": n, "foo": "x", "bar": {"baz": 2}}


SHAPES = {"A": _shape_a, "B": _shape_b, "C": _shape_c, "D": _shape_d}
OPTIONS = {
    "A": lambda n, value, label: {"id": n, "question_id": 12, "value": value, "label": label},
    "B": lambda n, value, label: {"id": n, "question": 12, "value": value, "label": label},
    "C": lambda n, value, label: {"id": n, "question": {"id": 12}, "value": value, "label": label},
    "D": lambda n, value, label: {"id": n, "foo": value},
}
# (question, answer, summary): one answered by an option code, one placeholder, one left blank
VARIANT_ANSWERS = (
    (Q1, "2", ""),
    (Q2, "BLN classes", ""),
    (Q3, "Registers checked and materials in place.", "Daily registers are kept."),
    (PSEA, "No", ""),
    (OTHER, "n/a", ""),
    ((17, "Is the space safe for children?"), "", ""),
)


@pytest.fixture
def fm_questions_variant(db):
    """``make(shape)``: replace the checklist answer and option records with shape A, B, C or D."""

    def make(shape: str) -> list[dm.DatamartDocument]:
        dm.DatamartDocument.objects.filter(dataset__in=("fm_questions", "fm_options")).delete()
        docs = [
            _document("fm_questions", SHAPES[shape](60001 + k, question, answer, summary))
            for k, (question, answer, summary) in enumerate(VARIANT_ANSWERS)
        ]
        for k, (value, label) in enumerate((("1", "On track"), ("2", "Constrained"), ("3", "Off track"))):
            _document("fm_options", OPTIONS[shape](71001 + k, value, label))
        return docs

    return make
