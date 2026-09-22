import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.indicators.models import Activity, IndicatorNew
from neurodb.integrations.activityinfo.client import ActivityInfoClient
from neurodb.integrations.activityinfo.structure import apply_tags, group_resources, import_structure
from neurodb.integrations.http import make_session

pytestmark = pytest.mark.django_db

BASE = "https://ai.test"


def make_run(database) -> SyncRun:
    return SyncRun.objects.create(job=SyncRun.Job.ACTIVITYINFO_STRUCTURE, target=str(database.ai_id))


def tree(db_id: str) -> dict:
    return {
        "resources": [
            {"id": "folderA", "parentId": db_id, "type": "FOLDER", "label": "Folder A"},
            {"id": "formA1", "parentId": "folderA", "type": "FORM", "label": "Monthly Reporting"},
            {"id": "formRoot", "parentId": db_id, "type": "FORM", "label": "Root Form"},
            {"id": "formOther", "parentId": "elsewhere", "type": "FORM", "label": "Not ours"},
        ]
    }


def schema_with_subform(sub_id: str) -> dict:
    return {
        "elements": [
            {"id": "partner", "type": "reference"},
            {"id": "sub", "type": "subform", "typeParameters": {"formId": sub_id}},
            {"id": "visible", "type": "subform", "tableVisible": True, "typeParameters": {"formId": "ignored"}},
        ]
    }


def schema_with_quantities() -> dict:
    return {
        "elements": [
            {"id": "q1", "type": "quantity", "label": "1.1_SYR_Female_Under 5: # of girls", "description": "d"},
            {"id": "q2", "type": "calculated", "label": "1.2_LEB_Male_visual: # of boys"},
            {"id": "t1", "type": "text", "label": "comment"},
        ]
    }


def mock_api(db_id: str) -> None:
    responses.get(f"{BASE}/resources/databases/ck2yrizmo2", json=tree(db_id))
    responses.get(f"{BASE}/resources/form/formA1/schema", json=schema_with_subform("subA1"))
    responses.get(f"{BASE}/resources/form/subA1/schema", json=schema_with_quantities())
    responses.get(f"{BASE}/resources/form/formRoot/schema", json={"elements": []})


def test_group_resources_orders_folder_forms_first():
    pairs = group_resources(tree("db")["resources"], "db")
    assert [(f["id"] if f else None, form["id"]) for f, form in pairs] == [("folderA", "formA1"), (None, "formRoot")]


@responses.activate
def test_import_structure_upserts_activities_and_indicators(database):
    mock_api(database.db_id)
    client = ActivityInfoClient(BASE, "t", session=make_session("t", backoff=0))
    run = make_run(database)
    stats = import_structure(database, client=client, run=run)

    assert run.status == SyncRun.Status.SUCCEEDED
    assert (run.rows_in, run.rows_written, run.rows_failed) == (2, 2, 0)
    assert stats["indicators"] == 2 and stats["indicators_created"] == 2
    activity = Activity.objects.get(ai_form_id="formA1", database=database)
    assert (activity.name, activity.category, activity.ai_category_id) == ("Monthly Reporting", "Folder A", "folderA")
    assert Activity.objects.filter(database=database).count() == 2
    q1 = IndicatorNew.objects.get(ai_indicator="q1", database=database)
    assert (q1.awp_code, q1.nationality, q1.gender, q1.age_group, q1.type) == ("1.1", "SYR", "Female", "<5", "quantity")
    q2 = IndicatorNew.objects.get(ai_indicator="q2")
    assert (q2.disability, q2.activity_id) == ("Visual", activity.id)

    # Second run: same keys, nothing duplicated, nothing deleted.
    import_structure(database, client=client, run=make_run(database))
    assert IndicatorNew.objects.filter(database=database).count() == 2
    assert Activity.objects.filter(database=database).count() == 2


@responses.activate
def test_form_failure_is_partial_not_fatal(database):
    responses.get(f"{BASE}/resources/databases/ck2yrizmo2", json=tree(database.db_id))
    responses.get(f"{BASE}/resources/form/formA1/schema", status=500)
    responses.get(f"{BASE}/resources/form/formRoot/schema", json={"elements": []})
    client = ActivityInfoClient(BASE, "t", session=make_session("t", backoff=0))
    run = make_run(database)
    import_structure(database, client=client, run=run)
    assert run.status == SyncRun.Status.PARTIAL
    assert (run.rows_written, run.rows_failed) == (1, 1)
    assert not Activity.objects.filter(ai_form_id="formA1").exists()  # savepoint rolled back


@responses.activate
def test_tree_failure_is_fatal(database):
    responses.get(f"{BASE}/resources/databases/ck2yrizmo2", status=500)
    client = ActivityInfoClient(BASE, "t", session=make_session("t", backoff=0))
    run = make_run(database)
    with pytest.raises(Exception, match="HTTP 500"):
        import_structure(database, client=client, run=run)
    assert run.status == SyncRun.Status.FAILED


def test_apply_tags_keeps_v2_overwrite_rules(database):
    indicator = IndicatorNew(database=database, name="Total individuals", gender="Male", nationality="SYR")
    apply_tags(indicator)
    assert indicator.gender == "Male"  # only overwritten on a match
    assert indicator.nationality is None  # re-derived every time
