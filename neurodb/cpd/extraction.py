"""AI-suggested results framework: read a CPD (PDF) and propose its outcomes, outputs and indicators.

The document goes to the OpenAI API (the same key and model as Ask NeuroDB) with a strict JSON
schema; the answer is stored as a FrameworkProposal. Nothing enters the framework until an
administrator ticks the items to keep on the review page and applies them; applied items are
marked "AI-suggested" until someone edits them. A CPD is a public document; the request is not
stored by OpenAI (store=False).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import CPDocument, FrameworkProposal, Indicator, Origin, Outcome, Output

logger = logging.getLogger(__name__)

MAX_PDF_MB = 30
MAX_OUTPUT_TOKENS = 32000

_INDICATOR = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "code",
        "title",
        "unit",
        "direction",
        "baseline",
        "baseline_year",
        "target",
        "means_of_verification",
    ],
    "properties": {
        "code": {"type": "string", "description": "the indicator's number in the document, or empty"},
        "title": {"type": "string"},
        "unit": {"type": "string", "enum": ["number", "percent"]},
        "direction": {"type": "string", "enum": ["increase", "decrease"]},
        "baseline": {"type": ["number", "null"]},
        "baseline_year": {"type": ["integer", "null"]},
        "target": {"type": ["number", "null"], "description": "end-of-cycle target"},
        "means_of_verification": {"type": "string"},
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outcomes"],
    "properties": {
        "outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "title", "indicators", "outputs"],
                "properties": {
                    "code": {"type": "string"},
                    "title": {"type": "string"},
                    "indicators": {"type": "array", "items": _INDICATOR},
                    "outputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "title", "indicators"],
                            "properties": {
                                "code": {"type": "string"},
                                "title": {"type": "string"},
                                "indicators": {"type": "array", "items": _INDICATOR},
                            },
                        },
                    },
                },
            },
        }
    },
}
PROMPT = (
    "This is a UNICEF Country Programme Document (or its results and resources framework). Extract its "
    "results framework exactly as written: every outcome with its code and title, every output under its "
    "outcome, and every indicator under the outcome or output it measures, with its baseline (value and "
    "year), its end-of-cycle target and its means of verification. Use the document's own numbering for "
    "codes. Use null when a value is not stated; never estimate. A percentage indicator has unit "
    "'percent' and its numbers as percentages (e.g. 45 for 45%). Direction is 'decrease' only when the "
    "target is lower than the baseline (e.g. a rate to reduce)."
)


class ExtractionError(Exception):
    pass


def extract(document: CPDocument) -> dict[str, Any]:
    """The proposed framework of a PDF document (raises ExtractionError)."""
    from neurodb.assistant.agent import AssistantUnavailable, client

    if not document.filename.lower().endswith(".pdf"):
        raise ExtractionError("Only PDF documents can be read; import the framework from Excel instead.")
    if document.file.size > MAX_PDF_MB * 1024 * 1024:
        raise ExtractionError(f"The PDF is larger than {MAX_PDF_MB} MB.")
    try:
        api = client()
    except AssistantUnavailable as exc:
        raise ExtractionError("The AI assistant is not configured (OPENAI_API_KEY).") from exc
    with document.file.open("rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")
    response = api.responses.create(
        model=settings.AI_ASSISTANT_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": document.filename,
                        "file_data": f"data:application/pdf;base64,{data}",
                    },
                    {"type": "input_text", "text": PROMPT},
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "cpd_results_framework",
                "schema": SCHEMA,
                "strict": True,
            }
        },
        reasoning={"effort": settings.AI_ASSISTANT_EFFORT},
        max_output_tokens=MAX_OUTPUT_TOKENS,
        store=False,
    )
    try:
        items = json.loads(response.output_text)
    except (TypeError, ValueError) as exc:
        raise ExtractionError("The answer could not be read as a framework.") from exc
    if not isinstance(items, dict) or not isinstance(items.get("outcomes"), list):
        raise ExtractionError("The answer holds no outcomes.")
    return items


def run(proposal: FrameworkProposal) -> FrameworkProposal:
    try:
        proposal.items = extract(proposal.document)
        proposal.status = FrameworkProposal.Status.READY
        proposal.error = ""
    except ExtractionError as exc:
        proposal.status, proposal.error = FrameworkProposal.Status.FAILED, str(exc)
    except Exception as exc:  # the API failing in any way: say so, keep the traceback in the log
        logger.exception("CPD framework proposal %s failed", proposal.pk)
        proposal.status, proposal.error = (
            FrameworkProposal.Status.FAILED,
            f"{type(exc).__name__}: {exc}"[:2000],
        )
    proposal.save(update_fields=["items", "status", "error"])
    return proposal


def flatten(items: dict[str, Any]) -> list[dict[str, Any]]:
    """The proposal as rows for the review page, each with a key the form ticks."""
    rows = []
    for i, outcome in enumerate(items.get("outcomes", [])):
        rows.append({"key": f"o{i}", "level": "Outcome", "depth": 0, "item": outcome})
        for k, ind in enumerate(outcome.get("indicators", [])):
            rows.append(
                {"key": f"o{i}i{k}", "level": "Indicator", "depth": 1, "item": ind, "parent": f"o{i}"}
            )
        for j, output in enumerate(outcome.get("outputs", [])):
            rows.append(
                {"key": f"o{i}p{j}", "level": "Output", "depth": 1, "item": output, "parent": f"o{i}"}
            )
            for k, ind in enumerate(output.get("indicators", [])):
                rows.append(
                    {
                        "key": f"o{i}p{j}i{k}",
                        "level": "Indicator",
                        "depth": 2,
                        "item": ind,
                        "parent": f"o{i}p{j}",
                    }
                )
    return rows


def apply(proposal: FrameworkProposal, keys: set[str]) -> dict[str, int]:
    """Create the ticked items (a child is kept only with its parent). Items whose code already
    exists in the cycle are left as they are."""
    programme = proposal.document.programme
    made = {"created": 0, "skipped": 0}
    parents: dict[str, Any] = {}
    with transaction.atomic():
        for row in flatten(proposal.items):
            if row["key"] not in keys or (row.get("parent") and row["parent"] not in parents):
                continue
            item = row["item"]
            code, title = (item.get("code") or "").strip()[:20], (item.get("title") or "").strip()
            if row["level"] == "Outcome":
                obj, created = Outcome.objects.get_or_create(
                    programme=programme,
                    code=code or f"AI{row['key']}",
                    defaults={"title": title, "origin": Origin.AI},
                )
            elif row["level"] == "Output":
                existing = (
                    Output.objects.filter(outcome__programme=programme, code=code).first() if code else None
                )
                created = existing is None
                obj = existing or Output.objects.create(
                    outcome=parents[row["parent"]],
                    code=code or f"AI{row['key']}",
                    title=title,
                    origin=Origin.AI,
                )
            else:
                existing = Indicator.objects.filter(
                    programme=programme, code=item.get("code") or "", title=title
                ).first()
                created = existing is None
                parent = parents[row["parent"]]
                obj = existing or Indicator.objects.create(
                    programme=programme,
                    outcome=parent if isinstance(parent, Outcome) else None,
                    output=parent if isinstance(parent, Output) else None,
                    code=(item.get("code") or "")[:30],
                    title=title,
                    unit=item.get("unit") if item.get("unit") in ("number", "percent") else "number",
                    direction=item.get("direction")
                    if item.get("direction") in ("increase", "decrease")
                    else "increase",
                    baseline=item.get("baseline"),
                    baseline_year=item.get("baseline_year"),
                    target=item.get("target"),
                    means_of_verification=item.get("means_of_verification") or "",
                    origin=Origin.AI,
                )
            parents[row["key"]] = obj
            made["created" if created else "skipped"] += 1
        proposal.status, proposal.applied_at = FrameworkProposal.Status.APPLIED, timezone.now()
        proposal.save(update_fields=["status", "applied_at"])
    return made
