"""ActivityInfo structure import: forms -> ``Activity``, quantity fields -> ``IndicatorNew``.

Ports ``pivoting/utilities.py::import_data_v4`` and ``get_list_indicators_v4``: the database
tree of ``parent_id`` (falling back to ``db_id``) is walked for the folders and forms whose
parent is ``db_id``; each form's schema is read, every ``subform`` element (without
``tableVisible``) is followed and its ``quantity``/``calculated`` fields become indicators.

Upsert keys are the v2 ones: ``Activity`` by (``ai_form_id``, ``database``) and ``IndicatorNew``
by (``ai_indicator``, ``database``, ``activity``). Nothing is ever deleted or marked deleted.

Tags: v2 ``IndicatorNew.set_tags`` re-derived nationality and age group on every import (a
non-match cleared them) but only overwrote gender, programme and disability when a spelling
matched. That behaviour is kept verbatim (see ``apply_tags``) because the legacy schema has no
way to tell a hand-edited value from a derived one.

Fixed v2 defects: token literal -> settings; per-form exceptions aborted the command -> each
form is its own savepoint, failures are logged with the form id, counted and the run ends PARTIAL.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from neurodb.core.models import SyncRun
from neurodb.indicators.models import Activity, Database, IndicatorNew
from neurodb.integrations.activityinfo.client import ActivityInfoClient
from neurodb.integrations.activityinfo.rows import awp_code
from neurodb.integrations.activityinfo.tags import parse_tags
from neurodb.integrations.runs import describe_error, fail, finish_by_counts

logger = logging.getLogger(__name__)

INDICATOR_TYPES = ("quantity", "calculated")


def apply_tags(indicator: IndicatorNew) -> None:
    """Write the derived tags onto ``indicator`` with the v2 overwrite rules (not saved)."""
    tags = parse_tags(indicator.name or "")
    indicator.nationality = tags["nationality"]
    indicator.age_group = tags["age_group"]
    for field in ("gender", "programme", "disability"):
        if tags[field] is not None:
            setattr(indicator, field, tags[field])


def group_resources(resources: list[dict[str, Any]], db_id: str | None) -> list[tuple[dict | None, dict]]:
    """(folder or None, form) pairs under ``db_id`` in v2 order: folder forms first, then root forms."""
    folders = [item for item in resources if item.get("parentId") == db_id and item.get("type") == "FOLDER"]
    root_forms = [item for item in resources if item.get("parentId") == db_id and item.get("type") == "FORM"]
    pairs: list[tuple[dict | None, dict]] = []
    for folder in folders:
        pairs.extend(
            (folder, item)
            for item in resources
            if item.get("parentId") == folder["id"] and item.get("type") == "FORM"
        )
    pairs.extend((None, form) for form in root_forms)
    return pairs


def upsert_activity(database: Database, form: dict[str, Any], folder: dict[str, Any] | None) -> Activity:
    activity, _ = Activity.objects.get_or_create(
        ai_form_id=form["id"], database=database, defaults={"name": form["label"], "label": form["label"]}
    )
    activity.name = form["label"]
    activity.label = form["label"]
    if folder is not None:
        activity.category = folder["label"]
        activity.ai_category_id = form.get("parentId")
    activity.save()
    return activity


def sub_form_ids(schema: dict[str, Any]) -> list[str]:
    """Form ids of the sub-form elements of a schema (v2 ``get_list_indicators_v4`` filter)."""
    ids = []
    for element in schema.get("elements", []):
        if "subform" in str(element.get("type", "")) and "tableVisible" not in element:
            form_id = (element.get("typeParameters") or {}).get("formId")
            if form_id:
                ids.append(form_id)
    return ids


def upsert_indicator(
    database: Database, activity: Activity, field: dict[str, Any]
) -> tuple[IndicatorNew, bool]:
    label = field.get("label", "")
    indicator, created = IndicatorNew.objects.get_or_create(
        ai_indicator=field["id"], database=database, activity=activity, defaults={"name": label}
    )
    indicator.description = field.get("description", "")
    indicator.label = label
    indicator.name = label
    indicator.type = field.get("type")
    indicator.units = (field.get("typeParameters") or {}).get("units", "")
    indicator.awp_code = awp_code(label)
    apply_tags(indicator)
    indicator.save()
    return indicator, created


def import_form(
    database: Database, client: ActivityInfoClient, form: dict[str, Any], folder: dict[str, Any] | None
) -> dict[str, int]:
    """Upsert one form and all indicators of its sub-forms inside one savepoint."""
    stats = {"indicators": 0, "indicators_created": 0}
    with transaction.atomic():
        activity = upsert_activity(database, form, folder)
        schema = client.get_form_schema(form["id"])
        for sub_form_id in sub_form_ids(schema):
            sub_schema = client.get_form_schema(sub_form_id)
            for field in sub_schema.get("elements", []):
                if field.get("type") not in INDICATOR_TYPES:
                    continue
                _, created = upsert_indicator(database, activity, field)
                stats["indicators"] += 1
                stats["indicators_created"] += int(created)
    return stats


def import_structure(
    database: Database, *, client: ActivityInfoClient | None = None, run: SyncRun
) -> dict[str, Any]:
    """Import the forms and indicators of ``database`` and finish ``run``.

    Returns the stats also written to ``run.details``. Raises after finishing the run FAILED when
    the database tree itself cannot be read.
    """
    client = client or ActivityInfoClient()
    stats: dict[str, Any] = {"forms": 0, "forms_failed": 0, "indicators": 0, "indicators_created": 0}
    try:
        tree = client.get_database(database.parent_id or database.db_id)
        pairs = group_resources(tree.get("resources", []), database.db_id)
    except Exception as exc:
        fail(run, exc, **stats)
        raise
    for folder, form in pairs:
        run.rows_in += 1
        try:
            form_stats = import_form(database, client, form, folder)
        except Exception as exc:
            run.rows_failed += 1
            stats["forms_failed"] += 1
            logger.warning(
                "structure %s: form %s failed: %s", database.ai_id, form.get("id"), describe_error(exc)
            )
            continue
        run.rows_written += 1
        stats["forms"] += 1
        stats["indicators"] += form_stats["indicators"]
        stats["indicators_created"] += form_stats["indicators_created"]
    finish_by_counts(run, **stats)
    stats["status"] = run.status
    return stats
