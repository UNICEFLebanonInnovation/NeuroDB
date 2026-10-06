"""Download a few stored field monitoring records, redacted, from the admin ("Fields found").

Administrators have no shell on App Service, so ``record_datamart_samples`` and
``fmm_redact_fixtures`` cannot be run there. This reads the records NeuroDB already stored from the
last Datamart sync (``MonitoringFinding.data``, the checklist documents, FM action points...),
passes them through the same redaction as ``fmm_redact_fixtures`` (people become "Person N", texts
are cleaned and cut) and returns a ZIP with one file per dataset, in the fixture format of
``tests/fixtures/datamart/`` ({"dataset", "results", ...}). The files show the real key names, which
is what the go-live check of the keys needs; read them before passing them on.
"""

from __future__ import annotations

import datetime
import io
import json
import zipfile
from typing import Any

from neurodb.datamart import catalogue
from neurodb.datamart import models as dm
from neurodb.fmm import privacy
from neurodb.fmm.management.commands.fmm_redact_fixtures import Redactor, person_texts

LIMIT = 25
DOCUMENTS = (
    "fm_questions",
    "fm_options",
    "fm_programme_activities",
    "offices",
    "sections",
    "intervention_locations",
    "location_sites",
)


def stored(limit: int = LIMIT) -> dict[str, list[dict[str, Any]]]:
    """The last ``limit`` stored records of every dataset Monitoring insights reads, unredacted but
    scrubbed of contact keys (the typed tables are stored unscrubbed)."""
    out: dict[str, list[dict[str, Any]]] = {}
    findings = dm.MonitoringFinding.objects.exclude(data={}).order_by("-end_date", "-pk")
    out["field_monitoring"] = [
        catalogue.scrub(row) for row in findings.values_list("data", flat=True)[:limit]
    ]
    points = dm.ActionPoint.objects.filter(related_module__iexact="fm").exclude(data={}).order_by("-pk")
    out["action_points"] = [catalogue.scrub(row) for row in points.values_list("data", flat=True)[:limit]]
    for dataset in DOCUMENTS:
        rows = dm.DatamartDocument.objects.filter(dataset=dataset).order_by("-date", "-pk")
        out[dataset] = [catalogue.scrub(row) for row in rows.values_list("data", flat=True)[:limit]]
    return out


def redacted(limit: int = LIMIT) -> dict[str, list[dict[str, Any]]]:
    """``stored`` with people replaced by "Person N" and every text cleaned, as ``fmm_redact_fixtures``."""
    records = stored(limit)
    found = [text for rows in records.values() for text in person_texts(rows)]
    redactor = Redactor(privacy.names() | privacy.name_forms(found))
    return {dataset: [redactor.value(row) for row in rows] for dataset, rows in records.items()}


def zip_bytes(limit: int = LIMIT) -> bytes:
    """A ZIP of ``<dataset>.json`` files in the fixture format, plus a short README."""
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for dataset, results in redacted(limit).items():
            sample = {
                "dataset": dataset,
                "recorded": now,
                "source": "stored records (Fields found download), redacted",
                "results": results,
            }
            archive.writestr(
                f"{dataset}.json", json.dumps(sample, indent=1, sort_keys=True, default=str) + "\n"
            )
        archive.writestr(
            "README.txt",
            "Field monitoring records as NeuroDB stored them from the eTools Datamart, at most "
            f"{limit} per dataset. People are replaced by 'Person N'; e-mail addresses, links, phone "
            "numbers and known names are removed from texts, and long texts are cut. Read the files "
            "before passing them on: a name NeuroDB does not know, written without a title in a short "
            "text, can remain.\n",
        )
    return buffer.getvalue()
