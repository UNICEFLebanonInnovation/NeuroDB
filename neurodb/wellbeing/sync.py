"""Ask BMA to work out the Makani wellbeing flags, wait until it is done, then read the flags (changed
since the last run) and the centre summaries."""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error
from neurodb.youth.compiler import CompilerClient, calculate, calculation_problem

from .models import CenterSummary, Flag, SyncState

PAGE = 500
SUMMARY_MONTHS = 12


def flag_fields(item: dict[str, Any]) -> dict[str, Any]:
    follow = item.get("follow_up") or {}
    child = item.get("child") or {}
    return {
        "registration": item["registration"],
        "kind": item["kind"],
        "kind_label": item.get("kind_label") or item["kind"],
        "status": item["status"],
        "urgent": bool(item.get("urgent")),
        "priority": bool(item.get("priority")),
        "reason": item.get("reason") or "",
        "evidence": item.get("evidence") or {},
        "as_of": parse_date(item.get("as_of") or ""),
        "opened_on": parse_date(item["opened_on"]),
        "last_seen_on": parse_date(item.get("last_seen_on") or ""),
        "resolved_on": parse_date(item.get("resolved_on") or ""),
        "followed_up_on": parse_date(follow.get("on") or ""),
        "follow_up_type": follow.get("type") or "",
        "result": follow.get("result") or "",
        "result_label": follow.get("result_label") or "",
        "note": follow.get("note") or "",
        "followed_up_by_name": follow.get("by") or "",
        "center_id": (item.get("center") or {}).get("id"),
        "center_name": (item.get("center") or {}).get("name") or "",
        "partner_id": (item.get("partner") or {}).get("id"),
        "partner_name": (item.get("partner") or {}).get("name") or "",
        "round_name": (item.get("round") or {}).get("name") or "",
        "child_gender": child.get("gender") or "",
        "child_age_band": child.get("age_band") or "",
        "child_nationality": child.get("nationality") or "",
        "bma_path": item.get("bma_path") or "",
        "bma_modified": parse_datetime(item.get("modified") or ""),
    }


def store_flag(item: dict[str, Any]) -> Flag:
    flag, _ = Flag.objects.update_or_create(bma_id=item["id"], defaults=flag_fields(item))
    return flag


def sync(
    triggered_by: str = "schedule",
    client: CompilerClient | None = None,
    full: bool = False,
    *,
    calculate_first: bool = True,
) -> SyncRun:
    """``calculate_first``: ask BMA to work out the flags first (it keeps no schedule of its own); a
    calculation that fails or takes too long is noted and what BMA has is read all the same."""
    run = new_run(SyncRun.Job.COMPILER_WELLBEING, "compiler", triggered_by)
    state = SyncState.current()
    calculation = None
    try:
        client = client or CompilerClient()
        if calculate_first:
            calculation = calculate(client, "wellbeing")
            run.details["calculation"] = calculation
        since = None if full else (state.flags_modified_since or None)
        latest, after, pages = since or "", None, 0
        while True:
            page = client.wellbeing_flags(modified_since=since, after=after, limit=PAGE)
            pages += 1
            for item in page["flags"]:
                run.rows_in += 1
                try:
                    store_flag(item)
                except Exception as exc:
                    run.rows_failed += 1
                    note_error(run, f"flag {item.get('id')}", exc)
                else:
                    run.rows_written += 1
                    latest = max(latest, item.get("modified") or "")
            after = page.get("next_after")
            if not after:
                break
        state.settings = page.get("settings") or state.settings
        state.kinds = page.get("kinds") or state.kinds
        state.results = page.get("results") or state.results
        if not run.rows_failed:
            state.flags_modified_since = latest
        summaries = client.wellbeing_summaries()
        months = [m for m in summaries.get("months", [])][:SUMMARY_MONTHS]
        stored_months = []
        for month in months:
            data = summaries if month == summaries.get("month") else client.wellbeing_summaries(month)
            for row in data["summaries"]:
                CenterSummary.objects.update_or_create(
                    center_id=row["center"]["id"],
                    round_id=(row.get("round") or {}).get("id"),
                    month=parse_date(data["month"]),
                    defaults={
                        "center_name": row["center"].get("name") or "",
                        "governorate": row["center"].get("governorate") or "",
                        "partner_id": (row.get("partner") or {}).get("id"),
                        "partner_name": (row.get("partner") or {}).get("name") or "",
                        "round_name": (row.get("round") or {}).get("name") or "",
                        "figures": row.get("figures") or {},
                        "computed_at": parse_datetime(row.get("computed_at") or ""),
                    },
                )
            stored_months.append(month)
        state.synced_at = timezone.now()
        state.save()
    except Exception as exc:
        return fail(run, exc)
    problem = calculation_problem(calculation)
    return finish_by_counts(run, partial=bool(problem), error=problem, pages=pages, months=stored_months)
