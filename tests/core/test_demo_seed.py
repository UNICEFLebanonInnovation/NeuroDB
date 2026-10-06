"""The demo seed writes figures that agree with each other (local copy only; never runs in production)."""

import datetime as dt

import pytest
from django.core.management import call_command
from django.db.models import F, Sum

from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.datamart import models as dm


@pytest.mark.django_db
def test_demo_seed_is_self_consistent():
    call_command("seed_demo", password="test-only-pass", months=1)

    # a run's rows read = rows written + rows failed (except the Monitoring insights refresh, which reads
    # finding rows and answer records and writes visits: several rows make one visit)
    runs = SyncRun.objects.exclude(rows_in=F("rows_written") + F("rows_failed")).exclude(
        job=SyncRun.Job.FMM_REFRESH
    )
    assert not runs.exists(), list(runs.values("job", "status", "rows_in", "rows_written", "rows_failed"))
    # national population = the sum of the governorates (and of the age bands)
    for category in ("total", "children"):
        base = PopulationFigure.objects.filter(year=2025, category=category, age_group="", sex="")
        national = dict(base.filter(level="national").values_list("nationality", "value"))
        governorates = dict(
            base.filter(level="governorate").values_list("nationality").annotate(total=Sum("value"))
        )
        assert national and national == governorates, category
    bands = dict(
        PopulationFigure.objects.filter(year=2025, level="national")
        .exclude(age_group="")
        .values_list("nationality")
        .annotate(total=Sum("value"))
    )
    assert bands == dict(
        PopulationFigure.objects.filter(
            year=2025, level="national", category="total", age_group=""
        ).values_list("nationality", "value")
    )
    # no action point completed after today
    assert dm.ActionPoint.objects.filter(date_of_completion__isnull=False).exists()
    assert not dm.ActionPoint.objects.filter(date_of_completion__date__gt=dt.date.today()).exists()


# ------------------------------------------------------------------------------ field monitoring
def _first_findings() -> list[dict]:
    """The 50 findings ``_demo_etools`` writes, by natural keys (auto ids differ between two seeds)."""
    rows = []
    for row in dm.MonitoringFinding.objects.filter(datamart_id__lte=50).select_related(
        "partner", "intervention", "monitoring_site"
    ):
        values = {f.attname: getattr(row, f.attname) for f in row._meta.concrete_fields}
        for auto in ("id", "synced_at", "partner_id", "intervention_id", "monitoring_site_id"):
            values.pop(auto)
        values["partner"] = row.partner.etl_id if row.partner else None
        values["intervention"] = row.intervention.number if row.intervention else None
        values["monitoring_site"] = row.monitoring_site.name if row.monitoring_site else None
        rows.append(values)
    return sorted(rows, key=lambda r: r["datamart_id"])


def _table_counts() -> dict[str, int]:
    from django.apps import apps
    from django.db import connection

    tables = set(connection.introspection.table_names())
    return {
        model._meta.label: model._base_manager.count()
        for model in apps.get_models(include_auto_created=True)
        if model._meta.db_table in tables
    }


@pytest.mark.django_db
def test_demo_field_monitoring_adds_without_moving_the_rest(monkeypatch):
    from django.db import transaction

    from neurodb.core.management.commands import _demo_fmm
    from neurodb.datamart import fm
    from neurodb.partnerships.models import PCA

    savepoint = transaction.savepoint()
    with monkeypatch.context() as patch:
        patch.setattr(_demo_fmm, "seed_fmm", lambda today: None)
        call_command("seed_demo", password="test-only-pass", months=1)
    without_fm, counts_without = _first_findings(), _table_counts()
    transaction.savepoint_rollback(savepoint)

    call_command("seed_demo", password="test-only-pass", months=1)
    # the first 50 findings are exactly as before; only the tables the FM demo adds to grow
    assert _first_findings() == without_fm and len(without_fm) == 50
    counts = _table_counts()
    grown = {label for label, n in counts.items() if n != counts_without.get(label)}
    assert grown == {
        "datamart.MonitoringFinding",
        "datamart.ActionPoint",
        "datamart.MonitoringSite",
        "datamart.DatamartDocument",
        PCA.locations.through._meta.label,
        # the visits seed_demo's Monitoring insights refresh builds from them, and what it reads
        "fmm.Visit",
        "fmm.VisitEntity",
        "fmm.QuestionAnswer",
        "fmm.VisitActionPoint",
        "fmm.VisitRuleResult",
        "fmm.VisitAICheck",  # the demo's AI check answers, written without any AI call
        "fmm.KeyProbe",
    }
    assert dm.ActionPoint.objects.filter(datamart_id__gt=8000).count() == (
        counts["datamart.ActionPoint"] - counts_without["datamart.ActionPoint"]
    )

    findings = dm.MonitoringFinding.objects.all()
    rows_per_visit = {}
    for activity_id in findings.values_list("monitoring_activity_id", flat=True):
        rows_per_visit[activity_id] = rows_per_visit.get(activity_id, 0) + 1
    assert sum(1 for n in rows_per_visit.values() if n >= 2) >= 30  # several entities per visit
    statuses = {fm.normalize_status(s) for s in findings.values_list("status", flat=True)}
    assert statuses == set(fm.STATUSES)
    ratings = {fm.normalize_rating(r) for r in findings.values_list("overall_finding_rating", flat=True)}
    assert {"on_track", "off_track", "not_monitored"} <= ratings and "constrained" not in ratings
    assert findings.filter(overall_finding_rating="").exists()
    assert dm.MonitoringSite.objects.count() == 16 and findings.exclude(monitoring_site=None).exists()
    assert findings.filter(location__type__admin_level=2, monitoring_site=None).exists()  # a district centre
    assert PCA.locations.through.objects.exists()
    new = findings.filter(datamart_id__gt=1000)
    assert not new.filter(visit_lead="").exists()
    assert all(
        "team_members" in data and "field_office" in data for data in new.values_list("data", flat=True)
    )
    # seed_demo ends with the Monitoring insights refresh: every visit built, nothing failed
    from neurodb.fmm.models import Visit

    refreshed = SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH, target="full").get()
    assert (refreshed.status, refreshed.target, refreshed.triggered_by) == ("succeeded", "full", "demo")
    assert Visit.objects.count() == fm.count_visits(findings) == refreshed.rows_written
    # the new rows are linked by the FM demo; the first 50 by the refresh, the same with or without it
    assert new.exclude(intervention=None).exists()
    assert findings.filter(datamart_id__lte=50).exclude(intervention=None).count() == 50
    # ... which scores them: red and amber visits, every rule evaluated somewhere, a reported visit with
    # nothing rated (a monitoring gap) and a PSEA-flagged visit
    from neurodb.fmm.models import VisitRuleResult

    assert refreshed.details["rules_version"] == 2 and refreshed.details["scored"] > 0
    assert (
        Visit.objects.filter(urgency_band="red").exists()
        and Visit.objects.filter(urgency_band="amber").exists()
    )
    # FMS Lebanon's rules, the AI checks with the demo's answers (written without any AI call)
    from neurodb.fmm.models import VisitAICheck

    assert VisitAICheck.objects.exists() and set(VisitAICheck.objects.values_list("model", flat=True)) == {
        "demo"
    }
    evaluated = set(
        VisitRuleResult.objects.filter(status__in=("pass", "fail")).values_list("rule", flat=True)
    )
    assert {"R1", "R2", "R3", "R5", "R6", "R7", "R8", "R32"} <= evaluated
    assert VisitRuleResult.objects.filter(status="fail").values("rule").distinct().count() >= 5
    assert Visit.objects.get(key="75").urgency_band == "red"
    assert not Visit.objects.filter(ai_pending__gt=0).exists()
    assert Visit.objects.filter(status_group="reported", entities_rated=0).exists()
    assert Visit.objects.filter(psea_flag=True).exists() and Visit.objects.filter(psea_flag=False).exists()
    assert set(Visit.objects.exclude(hact_q1="").values_list("hact_q1", flat=True)) == {
        "on_track",
        "constrained",
        "off_track",
    }

    # ... and maps them: a visit matched to a planned place by coordinates, a planned place not yet
    # visited, and the visit placed at a district's own centre never matched by coordinates
    from neurodb.fmm import geo
    from neurodb.fmm.scope import Scope

    every_date = {"period": "custom", "from": "2000-01-01", "to": "2100-12-31", "section": ""}
    mapped = geo.map_points(Scope.from_params(every_date))
    assert mapped["counts"]["coords"] >= 1 and mapped["counts"]["not_visited"] >= 1
    assert any(p.get("shape") == "ring" for p in mapped["config"]["points"])
    matches = {row["key"]: row["match"] for row in mapped["visit_rows"]}
    centres = list(Visit.objects.filter(located_by="location", located_level=2).values_list("key", flat=True))
    assert centres and all(key in matches and matches[key] != "coords" for key in centres)

    # documents in shape A
    questions = list(
        dm.DatamartDocument.objects.filter(dataset="fm_questions").values_list("data", flat=True)
    )
    shape = {"monitoring_activity_id", "monitoring_activity", "question_id", "question_text", "is_hact",
             "order", "entity", "entity_type", "answer", "summary", "method"}  # fmt: skip
    assert questions and all(shape <= set(q) for q in questions)
    assert {q["monitoring_activity_id"] for q in questions} <= set(rows_per_visit)
    q1 = [q for q in questions if q["question_id"] == 12]
    q1_codes = {fm.normalize_answer({"1": "On track", "2": "Constrained", "3": "Off track"}.get(q["answer"],
                q["answer"])) for q in q1}  # fmt: skip
    assert q1_codes == {"on_track", "constrained", "off_track"}  # Constrained only in Q1 answers
    assert any(q["answer"] in ("1", "2", "3") for q in q1)  # some answers arrive as option codes
    assert any(q["entity"] == "" for q in q1) and any(q["entity_type"] == "Partner" for q in q1)
    psea = {q["answer"] for q in questions if q["question_id"] == 15}
    assert psea == {"Yes", "No"}
    assert sum(1 for q in questions if q["question_id"] == 15 and q["answer"] == "Yes") == 2
    assert any(q["answer"] == "" for q in questions) and any(q["answer"] == "n/a" for q in questions)
    options = dm.DatamartDocument.objects.filter(dataset="fm_options").values_list("data", flat=True)
    assert {(o["question_id"], o["value"], o["label"]) for o in options} >= {(12, "2", "Constrained")}
    activities = dm.DatamartDocument.objects.filter(dataset="fm_programme_activities")
    assert activities.exists() and all(
        {"programme_activity", "cp_output", "intervention_number"} <= set(d)
        for d in activities.values_list("data", flat=True)
    )

    # action points raised from visits: by the activity id, by its reference only, and one unlinked
    fm_points = dm.ActionPoint.objects.filter(related_module="fm", datamart_id__gt=8000)
    by_id = fm_points.filter(related_module_id__in=list(rows_per_visit))
    assert by_id.exists()
    references = set(findings.values_list("monitoring_activity", flat=True))
    by_reference = fm_points.filter(related_module_id=None, module_reference_number__in=references)
    assert by_reference.values("module_reference_number").distinct().count() == 2
    assert (
        fm_points.filter(related_module_id=None).exclude(module_reference_number__in=references).count() == 1
    )
    assert fm_points.filter(status="open", high_priority=True, due_date__lt=dt.date.today()).exists()
