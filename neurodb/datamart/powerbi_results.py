"""What Compare with Power BI tells the user (its pages and its Excel file): each export's and each Power BI
page's verdict with what to do, "What to do now" (at most four lines), the "Choices for you" the comparison
found evidence for, the fit tables in words, and the Excel file of every difference.

Everything here is read from what the comparison kept (``PowerBIExport.counts``, ``fits``, the lines): counts,
Power BI's captions, NeuroDB's own sentences and the values of the lines (never a person: the files' people
columns were dropped as they were read, NeuroDB's side passed ``powerbi_page.shown``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from typing import Any

from django.urls import reverse

from . import comparison
from . import powerbi_exports as px
from . import powerbi_words as words
from .models import PowerBIDifference, PowerBIExport, PowerBIUpload

TODO_LINES = 4
# the verdicts, worst first: a Power BI page takes its worst export's
SEVERITY = (
    "failed",
    "not_read",
    "look",
    "check",
    "wait",
    "other_country",
    "empty",
    "counted",
    "owed",
    "same",
    "not_uploaded",
)
# the expected reasons that are a rule or a reading of NeuroDB ("Same data, counted another way"); the others
# (APART) are rows NeuroDB cannot hold yet or never holds: "Same, apart from reports still owed: n"
COUNTED_REASONS = ("rule", "status", "repeated", "left_out")
APART_REASONS = ("owed", "before_history", "moved_on")
XLSX_SHEET_ROWS = 100_000  # rows of one sheet of the Excel file (the last says how many more)
XLSX_ROWS = 300_000  # rows of the whole file
ON_SCREEN = 5_000  # lines an export's page lists (the Excel file holds the rest)
PAGE_SIZE = 100


# ------------------------------------------------------------------------------------------------- verdicts
def groups(export: PowerBIExport) -> Counter:
    return Counter({int(k): v for k, v in ((export.counts or {}).get("groups") or {}).items()})


def written_groups(export: PowerBIExport) -> Counter:
    """An export's lines written (listed on its page), by group: those its rows link to. An earlier comparison
    kept only how many it wrote in all, needs a look first: read so."""
    from . import powerbi_match

    counts = export.counts or {}
    if "written_groups" in counts:
        return Counter({int(k): v for k, v in (counts["written_groups"] or {}).items()})
    found, left, out = groups(export), counts.get("written", 0), Counter()
    for group in powerbi_match.GROUP_ORDER:
        out[group] = min(found.get(group, 0), left)
        left -= out[group]
    return +out


def verdict_code(export: PowerBIExport) -> str:
    """An export's verdict: the worst group of its lines; with expected lines only, counted another way (a
    rule or a reading of NeuroDB's) or the same apart from rows NeuroDB cannot hold (reports still owed,
    before its history, moved on since Power BI's refresh); else same."""
    if export.state == "failed":
        return "failed"
    if export.state == "not_compared":
        return "other_country"
    if export.state == "empty":
        return "empty"
    if export.state != "compared":
        return export.verdict or ""
    found = groups(export)
    for group, code in ((4, "look"), (3, "check"), (2, "wait")):
        if found.get(group):
            return code
    if found.get(1):
        reasons = (export.counts or {}).get("reasons") or {}
        return "owed" if not any(reasons.get(r) for r in COUNTED_REASONS) else "counted"
    return "same"


def apart(export: PowerBIExport) -> tuple[str, str]:
    """The expected rows NeuroDB cannot hold, in words, and what they need ("reports still owed: 3", "The
    partners have not sent these reports yet."): empty when none."""
    reasons = (export.counts or {}).get("reasons") or {}
    found = [(r, reasons[r]) for r in APART_REASONS if reasons.get(r)]
    parts = "; ".join(words.VERDICT_APART[r].format(n=n) for r, n in found)
    return parts, " ".join(words.VERDICT_APART_ACTIONS[r] for r, _n in found)


def verdict_words(export: PowerBIExport) -> tuple[str, str]:
    """(the verdict in words, what to do)."""
    code = export.verdict or verdict_code(export)
    counts = export.counts or {}
    found = groups(export)
    if code == "failed":
        error = export.error or ""
        if error.startswith(words.COULD_NOT_READ.split("{")[0]):  # never read: export it again
            return words.VERDICT_WORDS["not_read"], words.VERDICT_ACTIONS["not_read"]
        cls = error.rsplit(": ", 1)[-1].rstrip(".") or "Error"
        return words.VERDICT_WORDS["failed"].format(cls=cls), words.VERDICT_ACTIONS["failed"]
    if code == "empty":
        n = counts.get("neurodb_rows", 0)
        key = "empty_none" if not n else ("empty_one" if n == 1 else "empty")
        return words.VERDICT_WORDS[key].format(n=n), words.VERDICT_ACTIONS[key]
    if code == "check":
        n = len(counts.get("check_pds") or []) or found.get(3, 0)
        key = "check_one" if n == 1 else "check"
        return words.VERDICT_WORDS[key].format(n=n), words.VERDICT_ACTIONS[key]
    if code in ("look", "wait"):
        n = found.get(4 if code == "look" else 2, 0)
        return words.VERDICT_WORDS[code].format(n=n), words.VERDICT_ACTIONS[code]
    if code in ("owed", "counted"):
        parts, actions = apart(export)
        if code == "owed":
            verdict = words.VERDICT_WORDS["owed"].format(parts=parts)
            return verdict, f"{words.VERDICT_ACTIONS['owed']} {actions}"
        if parts:
            verdict = words.VERDICT_COUNTED_ALSO.format(verdict=words.VERDICT_WORDS["counted"], parts=parts)
            return verdict, f"{words.VERDICT_ACTIONS['counted']} {actions}"
    if code in words.VERDICT_WORDS:
        return words.VERDICT_WORDS[code], words.VERDICT_ACTIONS[code]
    return words.STATE_WORDS.get(export.state, export.state), words.STATE_ACTIONS.get(export.state, "")


def worst(codes: Iterable[str]) -> str:
    found = [c for c in codes if c in SEVERITY]
    return min(found, key=SEVERITY.index) if found else "not_uploaded"


# ----------------------------------------------------------------------------------------- the upload page
def page_lines(upload: PowerBIUpload, exports: list[PowerBIExport]) -> list[dict[str, Any]]:
    """One line per Power BI page, in Power BI's order: its verdict (its worst export's), what to do, its
    needs-a-look lines and its exports (or its visuals not uploaded)."""
    by_kind: dict[str, list[PowerBIExport]] = {}
    for export in exports:
        by_kind.setdefault(export.kind, []).append(export)
    out = []
    for name in comparison.POWERBI_PAGE_ORDER:
        kinds = [k for k in px.KINDS.values() if k.visual[0] == name]
        mine = [e for k in kinds for e in by_kind.get(k.key, [])]
        missing = [k.visual[1] for k in kinds if not by_kind.get(k.key)]
        codes = [e.verdict or verdict_code(e) for e in mine]
        code = worst(codes) if mine else "not_uploaded"
        chosen = next((e for e in mine if (e.verdict or verdict_code(e)) == code), None)
        if chosen is not None and code not in ("", "not_uploaded"):
            verdict, action = verdict_words(chosen)
        else:
            verdict, action = words.VERDICT_WORDS["not_uploaded"], words.VERDICT_ACTIONS["not_uploaded"]
        look = sum(groups(e).get(4, 0) for e in mine)
        first_look = next((e for e in mine if written_groups(e).get(4)), None)
        out.append(
            {
                "name": name,
                "code": code,
                "verdict": verdict,
                "action": action,
                "look": look,
                "look_url": export_url(upload, first_look, group=4) if first_look else "",
                "missing": missing,
                "exports": mine,
            }
        )
    return out


def export_url(upload: PowerBIUpload, export: PowerBIExport, **query: Any) -> str:
    from urllib.parse import urlencode

    url = reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk])
    query = {k: v for k, v in query.items() if v not in (None, "")}
    return f"{url}?{urlencode(query)}" if query else url


def totals(exports: Iterable[PowerBIExport]) -> dict[str, Any]:
    """The lines of every export, by group and by reason, and the PDs to check in eTools."""
    by_group: Counter = Counter()
    by_reason: Counter = Counter()
    pds: set[str] = set()
    for export in exports:
        counts = export.counts or {}
        by_group.update(groups(export))
        by_reason.update(counts.get("reasons") or {})
        pds.update(counts.get("check_pds") or [])
    return {"groups": by_group, "reasons": by_reason, "check_pds": pds}


def todo(upload: PowerBIUpload, exports: list[PowerBIExport], before: list[str]) -> list[dict[str, str]]:
    """What to do now, at most four lines, the most pressing first: the upload's error, the rows that need a
    look, the PDs to check in eTools, the rows waiting for the sync, then ``before`` (the files not read,
    the visuals not uploaded, the filters to clear), and a closing line on what is expected. Each line with
    the address of its rows."""
    found = totals(exports)
    lines: list[dict[str, str]] = []
    if upload.status == PowerBIUpload.Status.FAILED and upload.error:
        lines.append({"text": upload.error, "url": ""})

    def first(group: int) -> str:
        """The rows of the first export that lists some of this group (never a link to an empty list)."""
        export = next((e for e in exports if written_groups(e).get(group)), None)
        return export_url(upload, export, group=group) if export else ""

    look = found["groups"].get(4, 0)
    if look:
        text = words.plural(look, words.TODO_LOOK_ONE, words.TODO_LOOK)
        unlisted = look - sum(written_groups(e).get(4, 0) for e in exports)
        if unlisted > 0:
            text += " " + words.plural(unlisted, words.TODO_UNLISTED_ONE, words.TODO_UNLISTED)
        lines.append({"text": text, "url": first(4)})
    check = len(found["check_pds"]) or found["groups"].get(3, 0)
    if check:
        lines.append({"text": words.plural(check, words.TODO_CHECK_ONE, words.TODO_CHECK), "url": first(3)})
    wait = found["groups"].get(2, 0)
    if wait:
        lines.append({"text": words.plural(wait, words.TODO_WAIT_ONE, words.TODO_WAIT), "url": first(2)})
    lines += [{"text": text, "url": ""} for text in before]
    expected = found["groups"].get(1, 0)
    if expected and len(lines) < TODO_LINES:
        reasons = found["reasons"]
        parts = ", ".join(
            words.TODO_EXPECTED_PART[r].format(n=n)
            for r, n in reasons.most_common()
            if r in words.TODO_EXPECTED_PART and words.REASON_GROUPS.get(r) == 1
        )
        text = words.either(expected, words.TODO_EXPECTED_ONE, words.TODO_EXPECTED)
        lines.append({"text": text.format(n=expected, parts=parts or words.GROUP_NAMES[1]), "url": first(1)})
    if not lines and upload.status == PowerBIUpload.Status.DONE:
        lines.append({"text": words.TODO_NOTHING, "url": ""})
    return lines[:TODO_LINES]


def reached_sentence(exports: Iterable[PowerBIExport]) -> str:
    """Program Performance's Reached fit, in one sentence."""
    for export in exports:
        if export.kind != "program_performance" or export.state != "compared":
            continue
        for table in export.fits or []:
            if table.get("title") != "reached":
                continue
            best = next((r for r in table["rows"] if r.get("chosen")), None)
            if best is None:
                return words.REACHED_NONE
            return words.REACHED_SENTENCE.format(
                words=words.CANDIDATE_WORDS[best["candidate"]], fits=best["fits"], of=best["of"]
            )
    return ""


def choices(exports: list[PowerBIExport]) -> list[dict[str, Any]]:
    """Choices for you: each rule difference the comparison found evidence for, with its evidence, one plain
    question and NeuroDB's rule until the user decides."""
    out = []
    by_kind = {e.kind: e for e in exports if e.state == "compared"}

    def extra(kind: str) -> dict[str, Any]:
        export = by_kind.get(kind)
        return ((export.counts or {}).get("extra") or {}) if export else {}

    sentence = reached_sentence(exports)
    if sentence and sentence != words.REACHED_NONE:
        out.append({"code": "reached", "evidence": sentence})
    fits = [0, 0]
    for kind in ("by_sector", "by_month"):
        found = extra(kind).get("progress_fit")
        if found:
            fits = [fits[0] + found[0], fits[1] + found[1]]
    if fits[1]:
        evidence = words.CHOICE_EVIDENCE["progress_reports"].format(fits=fits[0], of=fits[1])
        out.append({"code": "progress_reports", "evidence": evidence})
    due = extra("follow_up").get("due_not_yet")
    if due:
        out.append({"code": "due", "evidence": words.CHOICE_EVIDENCE["due"].format(n=due)})
    emergency = extra("program_performance").get("emergency_fit")
    if emergency and emergency[1]:
        evidence = words.CHOICE_EVIDENCE["emergency"].format(fits=emergency[0], of=emergency[1])
        out.append({"code": "emergency", "evidence": evidence})
    for item in out:
        item["question"] = words.CHOICE_QUESTIONS[item["code"]]
        item["default"] = words.CHOICE_DEFAULTS[item["code"]]
    return out


def summary(upload: PowerBIUpload, exports: list[PowerBIExport]) -> dict[str, Any]:
    """What the upload keeps once its lines are removed (retention): each Power BI page's verdict, the lines
    by group, the Reached sentence."""
    found = totals(exports)
    return {
        "pages": [
            {
                "name": p["name"],
                "code": p["code"],
                "verdict": p["verdict"],
                "action": p["action"],
                "look": p["look"],
            }
            for p in page_lines(upload, exports)
        ],
        "groups": {str(k): v for k, v in sorted(found["groups"].items())},
        "look": found["groups"].get(4, 0),
        "check_pds": len(found["check_pds"]),
        "reached": reached_sentence(exports),
    }


# ----------------------------------------------------------------------------------------------- an export
NEURODB_PAGE = {  # the NeuroDB page an export is compared with: its address and its tab or view
    "program_performance": ("reports:pd_monitoring", {"view": "table"}),
    "partner_cards": ("reports:partner_reporting", {"tab": "partners"}),
    "follow_up": ("reports:partner_reporting", {"tab": "partners"}),
    "by_sector": ("reports:partner_reporting", {}),
    "by_month": ("reports:partner_reporting", {}),
    "dq_card": ("reports:pd_monitoring", {"view": "quality"}),
    "dq_list": ("reports:pd_monitoring", {"view": "quality"}),
    "pd_indicators": ("reports:programmes", {"tab": "indicators"}),
    "report_list": ("reports:partner_reporting", {}),
    "locations": ("reports:partner_reporting", {"tab": "locations"}),
    "disaggregations": ("reports:partner_reporting", {"tab": "disaggregations"}),
    "fund_monitoring": ("reports:funds", {"tab": "contributions"}),
    "funded_programmes": ("reports:funds", {"tab": "programmes"}),
}


def neurodb_url(export: PowerBIExport) -> str:
    """The NeuroDB page an export is compared with, for Power BI's filter (its PD statuses, year and sections;
    "every status" written out, so the page's default never applies)."""
    from urllib.parse import urlencode

    from . import powerbi_filters

    found = NEURODB_PAGE.get(export.kind)
    if found is None:
        return ""
    name, extra = found
    parsed = powerbi_filters.Filter.from_json(export.filters)
    status = "status" if export.kind == "pd_indicators" else "pd_status"
    query: list[tuple[str, str]] = [(status, s) for s in parsed.pd_statuses] or [(status, "")]
    if export.kind not in ("pd_indicators", "fund_monitoring", "funded_programmes"):
        if len(parsed.years) == 1:
            query.append(("year", str(parsed.years[0])))
        elif name == "reports:pd_monitoring":
            query.append(("year", "all"))
        query += [("section", s) for s in parsed.sections]
    query += list(extra.items())
    return f"{reverse(name)}?{urlencode(query)}"


def caption(export: PowerBIExport, name: str) -> str:
    kind = px.KINDS.get(export.kind)
    if kind is None:
        return name
    found = next((c for c in kind.columns if c.name == name), None)
    return found.caption if found else name


def column_type(export: PowerBIExport, name: str) -> str:
    kind = px.KINDS.get(export.kind)
    found = next((c for c in kind.columns if c.name == name), None) if kind else None
    return found.type if found else ""


def match_captions(export: PowerBIExport) -> list[str]:
    """The captions of the values a line of this kind is matched by."""
    names = MATCH.get(export.kind, ())
    return [caption(export, n) for n in names]


MATCH = {
    "program_performance": ("partner", "indicator_title"),
    "follow_up": ("pd", "status", "due_date"),
    "by_sector": ("section",),
    "by_month": ("month",),
    "dq_list": ("pd", "indicator_title", "pd_output"),
    "pd_indicators": ("pd", "indicator_title"),
    "report_list": ("pd", "report", "period_end"),
    "locations": ("pd", "report", "indicator_title", "p_code"),
    "disaggregations": (
        "indicator_title",
        "p_code",
        "period_start",
        "period_end",
        "report_type",
        "report",
        "disaggregation",
    ),
    "fund_monitoring": ("pd",),
    "funded_programmes": ("pd", "grant"),
}


def display(export: PowerBIExport, name: str, value: Any) -> str:
    """A value as the page shows it: a fraction as a percent, a number with its digits, a list joined."""
    if value is None or value == "":
        return "—"
    kind = column_type(export, name)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if kind == px.FRACTION:
            return f"{value * 100:,.10g} %"
        if kind in (px.COUNT,) or float(value).is_integer():
            return f"{value:,.0f}"
        return f"{value:,.10g}"
    return str(value)


def fit_tables(export: PowerBIExport, history: str = "") -> list[dict[str, Any]]:
    """The export's fit tables in words."""
    out = []
    for table in export.fits or []:
        rows = []
        for row in table.get("rows", []):
            if table.get("set"):
                line = words.FIT_SET_LINE.format(fits=row["fits"], of=row["of"])
            else:
                line = words.FIT_LINE.format(fits=row["fits"], of=row["of"], above=row["above"])
            rows.append(
                {
                    "words": words.CANDIDATE_WORDS.get(row["candidate"], row["candidate"]),
                    "fits": line,
                    "chosen": bool(row.get("chosen")),
                }
            )
        aside = table.get("set_aside") or 0
        out.append(
            {
                "title": words.FIT_TITLES.get(table.get("title"), table.get("title")),
                "rows": rows,
                "aside": words.FIT_SET_ASIDE.format(n=aside, history=history) if aside else "",
            }
        )
    return out


def reason_words(reason: str, history: str) -> str:
    return words.REASON_WORDS.get(reason, reason).format(history=history)


def what_to_do(line: PowerBIDifference, history: str) -> str:
    """A line's group sentence, and its reason's step when it adds one."""
    extra = words.REASON_ACTIONS.get(line.reason, "")
    if line.reason == "not_in_read" and line.group != 4:
        extra = ""
    base = words.GROUP_ACTIONS.get(line.group, "")
    return " ".join(x for x in (base, extra.format(history=history)) if x)


FIT_OF_RULE = {"reached": "reached", "target": "target", "financial_contribution": "financial_contribution"}


def chosen_words(export: PowerBIExport | None, rule: str) -> str:
    """The words of the candidate the export's fit table chose for a rule (Power BI's way)."""
    title = FIT_OF_RULE.get(rule)
    for table in (export.fits or []) if export is not None and title else []:
        if table.get("title") == title:
            best = next((r for r in table.get("rows", []) if r.get("chosen")), None)
            if best:
                return words.CANDIDATE_WORDS.get(best["candidate"], "")
    return words.ANOTHER_WAY


def rule_words(rule: str, history: str, export: PowerBIExport | None = None) -> str:
    if not rule:
        return ""
    text = words.RULE_WORDS.get(rule, rule)
    return text.replace("{words}", chosen_words(export, rule)).format(history=history)


def line_view(export: PowerBIExport, line: PowerBIDifference, history: str) -> dict[str, Any]:
    """One line as the export's page and the Excel file show it."""
    columns = [
        {
            "caption": caption(export, name),
            "powerbi": display(export, name, values[0] if values else None),
            "neurodb": display(export, name, values[1] if len(values) > 1 else None),
            "alternative": words.ALTERNATIVE.format(value=display(export, name, values[2]))
            if len(values) > 2 and values[2] is not None
            else "",
        }
        for name, values in (line.found or {}).items()
    ]
    return {
        "kind": words.LINE_KINDS.get(line.kind, line.kind),
        "group": words.GROUP_NAMES.get(line.group, ""),
        "reason": reason_words(line.reason, history),
        "rule": rule_words(line.rule, history, export),
        "detail": line.detail,
        "todo": what_to_do(line, history),
        "match": [
            display(export, n, v) for n, v in zip(MATCH.get(export.kind, ()), line.match or [], strict=False)
        ]
        or [str(v) for v in line.match or []],
        "columns": columns,
        "link": line.link,
    }


# ------------------------------------------------------------------------------------------------ the Excel
def workbook(upload: PowerBIUpload, exports: list[PowerBIExport], history: str, base_url: str = "") -> bytes:
    """Every difference of the upload: a Summary sheet, then one sheet per export with lines (needs-a-look
    first): a line only in one side in wide form (one row, the kind's columns), a different line in long form
    (one row per differing column). Every text is written as a text (never a formula), numbers stay numbers;
    at most :data:`XLSX_SHEET_ROWS` rows a sheet and :data:`XLSX_ROWS` in the file."""
    from neurodb.reports import xlsx

    budget = [XLSX_ROWS]
    sheets = [(words.XLSX_SUMMARY, ["Item", "Value", "What to do"], _summary_rows(upload, exports, history))]
    for export in exports:
        if not (export.counts or {}).get("written"):
            continue
        kind = px.KINDS.get(export.kind)
        title = f"{export.order + 1} {kind.visual[0] if kind else export.kind}"
        columns, rows = _export_rows(export, history, budget, base_url)
        sheets.append((title, columns, rows))
    return xlsx.workbook(sheets)


def _summary_rows(
    upload: PowerBIUpload, exports: list[PowerBIExport], history: str
) -> Iterator[dict[str, Any]]:
    yield {"Item": words.PAGE_TITLE, "Value": comparison.POWERBI_REPORT}
    if upload.day:
        yield {"Item": "Statuses read for", "Value": upload.day}
    if upload.powerbi_as_of:
        yield {"Item": "Power BI refreshed on", "Value": upload.powerbi_as_of}
    if upload.synced_at:
        yield {"Item": "NeuroDB's last partner reports sync", "Value": upload.synced_at}
    yield {}
    yield {"Item": "What to do now"}
    for line in todo(upload, exports, []):
        yield {"Item": line["text"]}
    yield {}
    for page in page_lines(upload, exports):
        yield {"Item": page["name"], "Value": page["verdict"], "What to do": page["action"]}
        for export in page["exports"]:
            kind = px.KINDS.get(export.kind)
            counts = export.counts or {}
            words_ = words.EXPORT_COUNTS.format(
                rows=counts.get("rows", 0),
                same=counts.get("same", 0) + counts.get("same_zero", 0),
                different=counts.get("different", 0),
                only_powerbi=counts.get("only_powerbi", 0),
                only_neurodb=counts.get("only_neurodb", 0),
            )
            verdict, action = verdict_words(export)
            yield {
                "Item": f"  {kind.visual[1] if kind else export.kind} ({export.file_name})",
                "Value": verdict,
                "What to do": action,
            }
            if export.state == "compared":
                yield {"Item": "    " + words_}
            for clause in export.clauses or []:
                state = words.CLAUSE_STATES.get(clause.get("state"), clause.get("state"))
                yield {"Item": f"    {clause.get('words') or '—'}", "Value": state}
            for note in [*export.notes, *(counts.get("notes") or [])]:
                yield {"Item": f"    {note}"}
            for table in fit_tables(export, history):
                yield {"Item": f"    {table['title']}"}
                for row in table["rows"]:
                    mark = f" ({words.FIT_CHOSEN})" if row["chosen"] else ""
                    yield {"Item": f"      {row['words']}{mark}", "Value": row["fits"]}
    choices_ = upload.choices or []
    if choices_:
        yield {}
        yield {"Item": words.CHOICES_TITLE}
        for choice in choices_:
            yield {
                "Item": choice.get("question"),
                "Value": choice.get("evidence"),
                "What to do": choice.get("default"),
            }


def _export_rows(
    export: PowerBIExport, history: str, budget: list[int], base_url: str = ""
) -> tuple[list[str], Iterator[dict[str, Any]]]:
    kind = px.KINDS.get(export.kind)
    match = match_captions(export)
    names = [c.name for c in kind.columns] if kind else []
    captions = [c.caption for c in kind.columns] if kind else []
    head = ["Kind", "Group", "Reason", "What to do", "Detail", *match]
    wide = [*captions, "Column", words.POWER_BI, words.NEURODB, "Alternative", words.CHECK_IN_NEURODB]
    columns = list(dict.fromkeys([*head, *wide]))

    def rows() -> Iterator[dict[str, Any]]:
        written = 0
        lines = (
            PowerBIDifference.objects.filter(export=export)
            .order_by("n")
            .only("kind", "group", "reason", "rule", "detail", "match", "found", "link")
        )
        total = (export.counts or {}).get("lines", 0)
        seen = 0
        for line in lines.iterator(chunk_size=2000):
            seen += 1
            base = {
                "Kind": words.LINE_KINDS.get(line.kind, line.kind),
                "Group": words.GROUP_NAMES.get(line.group, ""),
                "Reason": " · ".join(
                    x
                    for x in (reason_words(line.reason, history), rule_words(line.rule, history, export))
                    if x
                ),
                "What to do": what_to_do(line, history),
                "Detail": line.detail,
                words.CHECK_IN_NEURODB: f"{base_url}{line.link}" if line.link.startswith("/") else line.link,
            }
            for name, caption_, value in zip(
                MATCH.get(export.kind, ()), match, line.match or [], strict=False
            ):
                base[caption_] = _cell(value, column_type(export, name))
            out = []
            if line.kind == "different":
                for name, values in (line.found or {}).items():
                    out.append(
                        {
                            **base,
                            "Column": caption(export, name),
                            words.POWER_BI: _cell(values[0] if values else None, column_type(export, name)),
                            words.NEURODB: _cell(
                                values[1] if len(values) > 1 else None, column_type(export, name)
                            ),
                            "Alternative": _cell(
                                values[2] if len(values) > 2 else None, column_type(export, name)
                            ),
                        }
                    )
            else:
                side = 0 if line.kind == "only_powerbi" else 1
                row = dict(base)
                for name, values in (line.found or {}).items():
                    if name in names:
                        row[captions[names.index(name)]] = _cell(
                            values[side] if len(values) > side else None, column_type(export, name)
                        )
                out.append(row)
            for row in out:
                if written >= XLSX_SHEET_ROWS - 1 or budget[0] <= 1:
                    yield {"Kind": words.XLSX_MORE.format(n=max(total - seen + 1, 1))}
                    return
                written += 1
                budget[0] -= 1
                yield row
        if total > seen:
            yield {"Kind": words.XLSX_MORE.format(n=total - seen)}

    return columns, rows()


def _cell(value: Any, kind: str = "") -> Any:
    """A value for the Excel file: a date of a date column as a date, a list joined, the rest as kept (a text
    stays a text, never a formula: the writer writes every text as one)."""
    from . import powerbi_match as pm

    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if kind in (px.DATE, px.STAMP) and isinstance(value, str):
        return pm.as_date(value) or value
    return value
