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

from django.conf import settings

from . import comparison, powerbi_filters
from . import powerbi_exports as px
from . import powerbi_words as words
from .models import PowerBIDifference, PowerBIExport, PowerBIUpload
from .powerbi_links import reverse

TODO_LINES = 4
BUSY = (PowerBIUpload.Status.QUEUED, PowerBIUpload.Status.READING, PowerBIUpload.Status.COMPARING)
TODO_CLAUSES = 4  # clauses "what to do now" names
CANNOT = ("cannot", "unknown_field", "unknown_value", "unread")  # the clauses NeuroDB does not apply
NOT_READ = words.COULD_NOT_READ.split("{")[0]  # how the sentence of an export NeuroDB could not read starts
# the verdicts, worst first: a Power BI page takes its worst export's
SEVERITY = (
    "failed",
    "not_read",
    "stopped",
    "waiting",
    "look",
    "cut",
    "list_cut",
    "check",
    "wait",
    "other_country",
    "empty",
    "not_shown",
    "counted",
    "owed",
    "same",
    "not_uploaded",
)
# the expected reasons that are a rule or a reading of NeuroDB ("Same data, counted another way": an inactive
# indicator partner reports name, which NeuroDB lists by the user's decision of 28 Oct 2026, among them); the
# others (APART) are rows NeuroDB cannot hold yet or never holds: "Same, apart from reports still owed: n"
COUNTED_REASONS = ("rule", "status", "repeated", "left_out", "inactive")
APART_REASONS = ("owed", "before_history", "moved_on")
# the choice of Choices for you a rule's lines and an export's verdict point to (no other rule has one)
RULE_CHOICES = {
    "reached": "reached",
    "accepted_only": "reached",
}
# the choices the user decided on 26 Oct 2026 (NeuroDB counts Power BI's way there since): an upload compared
# before keeps them, and its pages no longer offer them. Such a comparison wrote no line of these rules: it
# stated them in notes (words.NOTES_DECIDED) and counted the follow-up list's Due rows of reports not yet due
# (``due_not_yet``)
DECIDED_CHOICES = frozenset({"progress_reports", "due", "emergency"})
# not in the Datamart read: the PD indicators' exports check the PD in eTools, as any row of a PD NeuroDB
# does not hold; the others a report in PRP
PD_ROWS = frozenset({"pd_indicators", "dq_list", "fund_monitoring", "funded_programmes"})
XLSX_SHEET_ROWS = 100_000  # rows of one sheet of the Excel file (the last says how many more)
XLSX_ROWS = 300_000  # rows of the whole file
ON_SCREEN = 5_000  # lines an export's page lists (the Excel file holds the rest)
PAGE_SIZE = 100


# ------------------------------------------------------------------------------------------------- verdicts
def country(text: str) -> str:
    """A sentence naming the one country NeuroDB holds."""
    return text.format(country=settings.ETOOLS_DATAMART_COUNTRY)


def read_failed(export: PowerBIExport) -> bool:
    """An export NeuroDB could not read (export it again); any other failure is a comparison that stopped
    (press Compare again): its file was read."""
    return export.state == "failed" and (export.error or "").startswith(NOT_READ)


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
    """An export's verdict: the worst group of its lines (:data:`powerbi_match.GROUP_ORDER`: needs a look,
    check in eTools, wait for the sync, NeuroDB cannot show this yet); with expected lines only, counted
    another way (a rule or a reading of NeuroDB's) or the same apart from rows NeuroDB cannot hold (before its
    history, moved on since Power BI's refresh; an earlier upload's reports still owed, which it wrote as
    expected); else same."""
    if export.state == "failed":
        return "failed"
    if export.state == "not_compared":
        return "other_country"
    if export.state == "empty":
        return "empty"
    if export.state != "compared":
        return export.verdict or ""
    found = groups(export)
    reasons = (export.counts or {}).get("reasons") or {}
    if found.get(4):  # every line that needs a look beyond a cut Power BI may have made: export it in parts
        cut, list_cut = reasons.get("cut", 0), reasons.get("list_cut", 0)
        if cut + list_cut >= found[4]:
            # cut itself, or a counted visual whose gaps wait for the report list exported in parts
            return "cut" if cut else "list_cut"
        return "look"
    for group, code in ((3, "check"), (2, "wait"), (5, "not_shown")):
        if found.get(group):
            return code
    if found.get(1):
        return "owed" if not any(reasons.get(r) for r in COUNTED_REASONS) else "counted"
    return "same"


def code_of(upload: PowerBIUpload, export: PowerBIExport) -> str:
    """An export's verdict on its upload's pages (:func:`verdict_code`). One uploaded that the comparison has
    not reached is waiting while the comparison runs, and once it stopped, was stopped before (never "Not
    uploaded": its file was uploaded, and read when its rows are kept)."""
    code = export.verdict or verdict_code(export)
    if code or export.state not in ("staged", "read"):
        return code
    if upload.status in BUSY:
        return "waiting"
    return "stopped" if upload.status == PowerBIUpload.Status.FAILED else code


def stopped(upload: PowerBIUpload, exports: Iterable[PowerBIExport]) -> tuple[str, str]:
    """An export a stopped comparison never reached (:func:`code_of`), in words, and what to do: compare
    again, or upload the files again when a copy waiting to be read was emptied (a stopped upload's are) or
    the upload's rows were removed since (trimmed: Compare again is refused)."""
    if upload.trimmed or any(e.state == "staged" for e in exports):
        return words.VERDICT_WORDS["stopped"], words.NOT_READ_ACTION
    return words.VERDICT_WORDS["stopped"], words.VERDICT_ACTIONS["stopped"]


def upload_error(upload: PowerBIUpload) -> str:
    """The upload's error as its pages say it: once its rows were removed (trimmed), Compare again is refused,
    so a comparison that stopped says to upload the files again (its error was kept before)."""
    if upload.trimmed:
        return (upload.error or "").replace(words.AGAIN, words.UPLOAD_AGAIN)
    return upload.error or ""


def apart(export: PowerBIExport) -> tuple[str, str]:
    """The expected rows NeuroDB cannot hold, in words, and what they need ("reports still owed: 3", "The
    partners have not sent these reports yet."): empty when none."""
    reasons = (export.counts or {}).get("reasons") or {}
    found = [(r, reasons[r]) for r in APART_REASONS if reasons.get(r)]
    parts = "; ".join(words.VERDICT_APART[r].format(n=n) for r, n in found)
    return parts, " ".join(words.VERDICT_APART_ACTIONS[r] for r, _n in found)


def asked(upload: PowerBIUpload) -> list[dict[str, Any]]:
    """The choices Choices for you offers on this upload: those it kept that are still asked (an upload
    compared before the user's decision of 26 Oct 2026 kept three choices decided since)."""
    return [c for c in upload.choices or [] if c.get("code") in words.CHOICE_QUESTIONS]


def decided(upload: PowerBIUpload) -> bool:
    """Whether the upload was compared before the user's decision of 26 Oct 2026 and asked about it."""
    return any(c.get("code") in DECIDED_CHOICES for c in upload.choices or [])


def offered(upload: PowerBIUpload) -> frozenset[str]:
    """The choices Choices for you offers on this upload (their codes)."""
    return frozenset(c.get("code", "") for c in asked(upload))


def decided_rule(export: PowerBIExport) -> bool:
    """Whether the export, compared before the user's decision of 26 Oct 2026, counted rows another way by a
    rule decided since: Power BI's Due covering reports not yet due on the follow-up list (the only one such a
    comparison counted in rows; it stated the others in notes, :func:`notes`)."""
    return bool(((export.counts or {}).get("extra") or {}).get("due_not_yet"))


def notes(export: PowerBIExport) -> list[str]:
    """The export's notes as its page and the Excel file show them: the read's, then the comparison's. A note
    a comparison made before the user's decision of 26 Oct 2026 stored about a rule decided then (NeuroDB's
    own table counting indicator reports, its Emergency indicator being the indicator's high frequency) is
    said as it is since (``words.NOTES_DECIDED``)."""
    found = [*(export.notes or []), *((export.counts or {}).get("notes") or [])]
    return [words.NOTES_DECIDED.get(note, note) for note in found]


def has_choice(export: PowerBIExport, choices: frozenset[str] | None) -> bool:
    """Whether Choices for you has a choice about this export's rules (None: not known, said so)."""
    if choices is None:
        return True
    counts = export.counts or {}
    rules = counts.get("rules") or {}
    return any(RULE_CHOICES.get(rule) in choices for rule in rules)


def verdict_words(
    export: PowerBIExport, trimmed: bool = False, offered: frozenset[str] | None = None
) -> tuple[str, str]:
    """(the verdict in words, what to do). ``trimmed``: the upload kept its verdicts, not its lines (no Excel
    file to send). ``offered``: the upload's choices (:func:`offered`): "counted another way" points to
    Choices for you only when one is about the export."""
    code = export.verdict or verdict_code(export)
    counts = export.counts or {}
    found = groups(export)
    if code == "failed":
        error = export.error or ""
        if read_failed(export):  # never read: export it again
            return words.VERDICT_WORDS["not_read"], words.VERDICT_ACTIONS["not_read"]
        found = counts.get("failure") or {}
        # its rows removed since (trimmed): Compare again is refused
        action = words.TRIMMED_FAILED_ACTION if trimmed else words.VERDICT_ACTIONS["failed"]
        if found.get("cls") == "MemoryError":  # out of memory: the job's peak and limit, never a step
            return words.VERDICT_WORDS["failed_memory"].format(what=words.out_of_memory(found)), action
        if found.get("step") in words.EXPORT_STEPS:  # the step it stopped at, and what stopped it
            step = words.EXPORT_STEPS[found["step"]]
            return words.VERDICT_WORDS["failed"].format(step=step, what=words.failure(found)), action
        cls = error.rsplit(": ", 1)[-1].rstrip(".") or "Error"  # one that failed before its step was kept
        return words.VERDICT_WORDS["failed_before"].format(cls=cls), action
    if code == "cut":
        return words.VERDICT_WORDS["cut"].format(n=export.rows_read), words.VERDICT_ACTIONS["cut"]
    if code == "list_cut":
        return words.VERDICT_WORDS["list_cut"].format(n=found.get(4, 0)), words.VERDICT_ACTIONS["list_cut"]
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
        action = words.TRIMMED_ACTION if trimmed and code == "look" else words.VERDICT_ACTIONS[code]
        return words.VERDICT_WORDS[code].format(n=n), action
    if code == "not_shown":
        n = found.get(5, 0)
        key = "not_shown_one" if n == 1 else "not_shown"
        return words.VERDICT_WORDS[key].format(n=n), words.VERDICT_ACTIONS[key]
    if code in ("owed", "counted"):
        parts, actions = apart(export)
        if code == "owed":
            verdict = words.VERDICT_WORDS["owed"].format(parts=parts)
            return verdict, f"{words.VERDICT_ACTIONS['owed']} {actions}"
        if has_choice(export, offered):
            counted = words.VERDICT_ACTIONS["counted"]
        else:
            counted = words.COUNTED_DECIDED if decided_rule(export) else words.COUNTED_NO_CHOICE
        if parts:
            verdict = words.VERDICT_COUNTED_ALSO.format(verdict=words.VERDICT_WORDS["counted"], parts=parts)
            return verdict, f"{counted} {actions}"
        return words.VERDICT_WORDS["counted"], counted
    if code in words.VERDICT_WORDS:
        return words.VERDICT_WORDS[code], country(words.VERDICT_ACTIONS[code])
    action = country(words.STATE_ACTIONS.get(export.state, ""))
    return words.STATE_WORDS.get(export.state, export.state), action


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
        codes = [code_of(upload, e) for e in mine]
        code = worst(codes) if mine else "not_uploaded"
        chosen = next((e for e, found in zip(mine, codes, strict=True) if found == code), None)
        if code == "stopped":
            verdict, action = stopped(upload, exports)
        elif code == "waiting":
            verdict, action = words.VERDICT_WORDS["waiting"], words.VERDICT_ACTIONS["waiting"]
        elif chosen is not None and code not in ("", "not_uploaded"):
            verdict, action = verdict_words(chosen, trimmed=upload.trimmed, offered=offered(upload))
        else:
            verdict, action = words.VERDICT_WORDS["not_uploaded"], words.VERDICT_ACTIONS["not_uploaded"]
        look = sum(groups(e).get(4, 0) for e in mine)
        # a trimmed upload lists no line: no link to its lines
        first_look = None if upload.trimmed else next((e for e in mine if written_groups(e).get(4)), None)
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
    """The lines of every export, by group and by reason, the expected ones by reason (each reason's group as
    the export wrote it, ``counts["reason_groups"]``; an export compared before they were kept wrote its
    reports still owed as expected, and every reason in the group of ``REASON_GROUPS`` then, owed's 1), and
    the PDs to check in eTools."""
    by_group: Counter = Counter()
    by_reason: Counter = Counter()
    expected: Counter = Counter()
    pds: set[str] = set()
    for export in exports:
        counts = export.counts or {}
        by_group.update(groups(export))
        reasons = counts.get("reasons") or {}
        by_reason.update(reasons)
        stored = counts.get("reason_groups")
        for reason, n in reasons.items():
            group = stored.get(reason) if stored is not None else EARLIER_GROUPS.get(reason)
            if group == 1:
                expected[reason] += n
        pds.update(counts.get("check_pds") or [])
    return {"groups": by_group, "reasons": by_reason, "expected": expected, "check_pds": pds}


# the expected reasons of an upload compared before each export kept its reasons' groups: owed was one
EARLIER_GROUPS = {**words.REASON_GROUPS, "owed": 1}


def to_clear(clause: powerbi_filters.Clause, kind: str = "") -> str:
    """A filter NeuroDB cannot apply, as the user finds it on the report: a slicer by its name on the export's
    page (``kind``'s; any page's when none), another field (or one the export's page has no slicer for) as
    the Filters pane shows it (Power BI's field names stay on the export's page)."""
    from . import powerbi_names

    found = px.KINDS.get(kind)
    pages = (found.visual[0],) if found else powerbi_names.POWERBI_PAGE_ORDER
    slicer = clause.field in powerbi_names.FIELD_SLICERS and powerbi_names.slicer(clause.field, pages)
    if slicer:
        return words.TODO_SLICER.format(slicer=slicer)
    if clause.field:
        return words.TODO_PANE.format(field=clause.field)
    return words.TODO_UNKNOWN_FIELD if clause.state == "unknown_field" else words.TODO_UNREAD


def before(upload: PowerBIUpload, exports: list[PowerBIExport]) -> list[str]:
    """What to do now about the files themselves (on the upload's page and in the Excel file alike): those not
    read (refused in the request, or in the job), the exports whose comparison stopped, the visuals not
    uploaded, the filters to clear."""
    lines = []
    not_read = len(upload.refused or []) + sum(1 for e in exports if read_failed(e))
    if not_read:
        lines.append(words.plural(not_read, words.TODO_NOT_READ_ONE, words.TODO_NOT_READ))
    stopped = sum(1 for e in exports if e.state == "failed" and not read_failed(e))
    if stopped:  # its rows removed since (trimmed): Compare again is refused
        one, many = (
            (words.TODO_NOT_COMPARED_TRIMMED_ONE, words.TODO_NOT_COMPARED_TRIMMED)
            if upload.trimmed
            else (words.TODO_NOT_COMPARED_ONE, words.TODO_NOT_COMPARED)
        )
        lines.append(words.plural(stopped, one, many))
    missing = sum(len(page["missing"]) for page in page_lines(upload, exports))
    if missing:
        lines.append(words.plural(missing, words.TODO_MISSING_ONE, words.TODO_MISSING))
    clear = dict.fromkeys(
        to_clear(c, e.kind)
        for e in exports
        for c in powerbi_filters.Filter.from_json(e.filters).clauses
        if c.state in CANNOT
    )
    if clear:
        lines.append(words.TODO_CANNOT.format(clauses="; ".join(list(clear)[:TODO_CLAUSES])))
    return lines


def not_read(upload: PowerBIUpload, exports: list[PowerBIExport]) -> list[str]:
    """Why each file was not read: refused in the request, or an export the job could not read."""
    return [*(upload.refused or []), *(e.error for e in exports if read_failed(e))]


def settled(export: PowerBIExport) -> bool:
    """An export with nothing to do: the same, counted another way, the same apart from rows NeuroDB cannot
    hold, or empty on both sides."""
    code = export.verdict or verdict_code(export)
    if code == "empty":
        return not (export.counts or {}).get("neurodb_rows")
    return code in ("same", "counted", "owed")


def todo(upload: PowerBIUpload, exports: list[PowerBIExport], before: list[str]) -> list[dict[str, str]]:
    """What to do now, at most four lines, the most pressing first: the upload's error, the differences that
    need a look, the PDs to check in eTools, the differences waiting for the sync, the exports to export again
    (cut by Power BI, empty while NeuroDB has rows, another country), then ``before`` (:func:`before`: the
    files not read, the visuals not uploaded, the filters to clear); then, always, one more line on what
    NeuroDB cannot show yet (reports still owed, draft values: no Power BI page's verdict names them while a
    worse one is there, so four more pressing lines must not hide them), and a closing line on what is
    expected (its parts by the groups each export wrote) while fewer than four lines are said.
    Each line with the address of its lines (none on a trimmed upload: it keeps no line)."""
    found = totals(exports)
    lines: list[dict[str, str]] = []
    if upload.status == PowerBIUpload.Status.FAILED and upload.error:
        lines.append({"text": upload_error(upload), "url": ""})

    def first(group: int) -> str:
        """The lines of the first export that lists some of this group (never a link to an empty list)."""
        if upload.trimmed:
            return ""
        export = next((e for e in exports if written_groups(e).get(group)), None)
        return export_url(upload, export, group=group) if export else ""

    cut_lines = found["reasons"].get("cut", 0) + found["reasons"].get("list_cut", 0)
    look = found["groups"].get(4, 0) - cut_lines
    if look > 0:
        action = words.TRIMMED_ACTION if upload.trimmed else words.GROUP_ACTIONS[4]
        text = f"{words.plural(look, words.TODO_LOOK_ONE, words.TODO_LOOK)} {action}"
        unlisted = found["groups"].get(4, 0) - sum(written_groups(e).get(4, 0) for e in exports)
        if unlisted > 0 and not upload.trimmed:
            text += " " + words.plural(min(unlisted, look), words.TODO_UNLISTED_ONE, words.TODO_UNLISTED)
        lines.append({"text": text, "url": first(4)})
    check = len(found["check_pds"]) or found["groups"].get(3, 0)
    if check:
        lines.append({"text": words.plural(check, words.TODO_CHECK_ONE, words.TODO_CHECK), "url": first(3)})
    wait = found["groups"].get(2, 0)
    if wait:
        lines.append({"text": words.plural(wait, words.TODO_WAIT_ONE, words.TODO_WAIT), "url": first(2)})
    codes = {e.pk: e.verdict or verdict_code(e) for e in exports}
    # the exports Power BI may have cut: those with rows beyond a round count, and the report list when a
    # counted visual's gaps wait for it (it may have no line of its own)
    cut = [e for e in exports if ((e.counts or {}).get("reasons") or {}).get("cut")]
    if found["reasons"].get("list_cut"):
        cut += [e for e in exports if e.kind == "report_list" and e not in cut]
    with_lines = [e for e in cut if ((e.counts or {}).get("reasons") or {}).get("cut")]
    again = (
        (
            cut,
            words.TODO_CUT_ONE,
            words.TODO_CUT,
            {"reason": "cut"} if with_lines and with_lines[0] is cut[0] else {},
        ),
        (
            [e for e in exports if codes[e.pk] == "empty" and (e.counts or {}).get("neurodb_rows")],
            words.TODO_EMPTY_ONE,
            words.TODO_EMPTY,
            {},
        ),
        (
            [e for e in exports if codes[e.pk] == "other_country"],
            country(words.TODO_OTHER_COUNTRY_ONE),
            words.TODO_OTHER_COUNTRY.replace("{country}", settings.ETOOLS_DATAMART_COUNTRY),
            {},
        ),
    )
    for found_exports, one, many, query in again:
        if found_exports:
            url = "" if upload.trimmed and query else export_url(upload, found_exports[0], **query)
            lines.append({"text": words.plural(len(found_exports), one, many), "url": url})
    lines += [{"text": text, "url": ""} for text in before]
    lines = lines[:TODO_LINES]
    not_shown = found["groups"].get(5, 0)
    if not_shown:
        text = words.plural(not_shown, words.TODO_NOT_SHOWN_ONE, words.TODO_NOT_SHOWN)
        lines.append({"text": text, "url": first(5)})
    expected = found["groups"].get(1, 0)
    if expected and len(lines) < TODO_LINES:
        parts = ", ".join(
            words.TODO_EXPECTED_PART[r].format(n=n)
            for r, n in found["expected"].most_common()
            if r in words.TODO_EXPECTED_PART
        )
        text = words.either(expected, words.TODO_EXPECTED_ONE, words.TODO_EXPECTED)
        lines.append({"text": text.format(n=expected, parts=parts or words.GROUP_NAMES[1]), "url": first(1)})
    if not lines and upload.status == PowerBIUpload.Status.DONE and all(settled(e) for e in exports):
        lines.append({"text": words.TODO_NOTHING, "url": ""})
    return lines


def clear_majority(fits: int, of: int) -> bool:
    """A rule fits more than half the rows: only then is it offered as a choice."""
    return bool(of) and fits * 2 > of


def reached_fit(exports: Iterable[PowerBIExport]) -> tuple[str, bool]:
    """Program Performance's Reached fit, in one sentence, and whether it is a choice to offer: the rule that
    fits best fits more than half the rows where Power BI shows a value."""
    for export in exports:
        if export.kind != "program_performance" or export.state != "compared":
            continue
        for table in export.fits or []:
            if table.get("title") != "reached":
                continue
            best = next((r for r in table["rows"] if r.get("chosen")), None)
            if best is None:
                return words.REACHED_NONE, False
            rule = words.CANDIDATE_WORDS[best["candidate"]]
            shown = best.get("positive", best["of"])  # the rows where Power BI shows a value
            if not clear_majority(best["above"], shown):
                return words.REACHED_WEAK.format(words=rule, fits=best["above"], of=shown), False
            return words.REACHED_SENTENCE.format(words=rule, fits=best["fits"], of=best["of"]), True
    return "", False


def reached_sentence(exports: Iterable[PowerBIExport]) -> str:
    """Program Performance's Reached fit, in one sentence (:func:`reached_fit`)."""
    return reached_fit(exports)[0]


def choices(exports: list[PowerBIExport]) -> list[dict[str, Any]]:
    """Choices for you: each rule difference the comparison found evidence for (the rule fits more than half
    the rows: :func:`clear_majority`), with its evidence, one plain question and NeuroDB's rule until the user
    decides. Reporting Progress counting progress reports, the follow-up list's Due and the Emergency
    indicator are no choice since the user decided them (26 Oct 2026): NeuroDB counts them Power BI's way."""
    out = []
    sentence, offered = reached_fit(exports)
    if offered:
        out.append({"code": "reached", "evidence": sentence})
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
_PARTNERS = ("reports:partner_reporting", {"tab": "partners"})
_REPORTS = ("reports:partner_reporting", {})
_QUALITY = ("reports:pd_monitoring", {"view": "quality"})
NEURODB_PAGE = {  # the NeuroDB page an export is compared with: its address and its tab or view
    "program_performance": ("reports:pd_monitoring", {"view": "table"}),
    **dict.fromkeys(("partners_count", "partners_reported", "partners_not_reported"), _PARTNERS),
    "partner_cards": _PARTNERS,
    "follow_up": _PARTNERS,
    **dict.fromkeys(("accepted_report", "submitted_report", "due_report", "overdue_report"), _REPORTS),
    "by_sector": _REPORTS,
    "by_month": _REPORTS,
    **dict.fromkeys(
        (
            "pd_count",
            "active_pds",
            "ended_pds",
            "dq_total",
            "dq_with_reached",
            "dq_with_target",
            "dq_without_target",
        ),
        _QUALITY,
    ),
    "dq_card": _QUALITY,
    "missing_targets": _QUALITY,
    "dq_list": _QUALITY,
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
    "missing_targets": ("pd", "indicator_title"),
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


def group_action(group: int, reason: str) -> str:
    """A group's sentence, or nothing where the reason's own step replaces it (a cut export: export it again,
    nothing to send)."""
    return "" if reason in words.OWN_ACTION else words.GROUP_ACTIONS.get(group, "")


def what_to_do(
    line: PowerBIDifference, history: str, offered: frozenset[str] | None = None, kind: str | None = None
) -> str:
    """A line's group sentence, and its reason's step when it adds one: a row of a PD NeuroDB does not hold,
    or of the PD indicators' exports, is checked in eTools (a report in PRP); a different rule points to
    Choices for you only when one is offered about it (``offered``: the upload's choices, None not known). An
    inactive indicator's line an upload compared before the user's decision of 28 Oct 2026 wrote as needs a
    look (no partner report names it) keeps its step, and says to compare again
    (``words.INACTIVE_EARLIER``)."""
    extra = words.REASON_ACTIONS.get(line.reason, "")
    if line.reason == "inactive" and line.group != words.REASON_GROUPS["inactive"]:
        extra = words.INACTIVE_EARLIER
    if line.reason == "not_in_read":
        if line.group != 4:
            extra = ""
        elif line.detail.startswith(words.DETAIL_NO_PD) or (kind or line.export.kind) in PD_ROWS:
            extra = words.NOT_IN_READ_PD
    if line.reason == "rule" and offered is not None and RULE_CHOICES.get(line.rule) not in offered:
        extra = words.RULE_NO_CHOICE
    base = group_action(line.group, line.reason)
    return " ".join(x for x in (base, extra.format(history=history)) if x)


FIT_OF_RULE = {"reached": "reached", "target": "target", "contribution": "financial_contribution"}


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


def line_view(
    export: PowerBIExport, line: PowerBIDifference, history: str, offered: frozenset[str] | None = None
) -> dict[str, Any]:
    """One line as the export's page shows it (``offered``: the upload's choices)."""
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
        "todo": what_to_do(line, history, offered, export.kind),
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
        columns, rows = _export_rows(export, history, budget, base_url, offered(upload))
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
    for line in todo(upload, exports, before(upload, exports)):
        yield {"Item": line["text"]}
    refused = not_read(upload, exports)
    if refused:
        yield {}
        yield {"Item": "Files not read"}
        for sentence in refused:
            yield {"Item": f"  {sentence}"}
    yield {}
    for page in page_lines(upload, exports):
        yield {"Item": page["name"], "Value": page["verdict"], "What to do": page["action"]}
        for visual in page["missing"]:
            missing = {"Value": words.NOT_UPLOADED, "What to do": words.NOT_UPLOADED_ACTION}
            yield {"Item": f"  {visual}", **missing}
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
            verdict, action = verdict_words(export, offered=offered(upload))
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
            for note in notes(export):
                yield {"Item": f"    {note}"}
            for table in fit_tables(export, history):
                yield {"Item": f"    {table['title']}"}
                for row in table["rows"]:
                    mark = f" ({words.FIT_CHOSEN})" if row["chosen"] else ""
                    yield {"Item": f"      {row['words']}{mark}", "Value": row["fits"]}
    choices_ = asked(upload)
    if choices_ or decided(upload):
        yield {}
        yield {"Item": words.CHOICES_TITLE}
        if decided(upload):  # compared before the user's decision: its three choices are no longer asked
            yield {"Item": words.CHOICES_DECIDED}
        for choice in choices_:
            yield {
                "Item": choice.get("question"),
                "Value": choice.get("evidence"),
                "What to do": choice.get("default"),
            }


def _export_rows(
    export: PowerBIExport,
    history: str,
    budget: list[int],
    base_url: str = "",
    choices: frozenset[str] | None = None,
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
                "What to do": what_to_do(line, history, choices, export.kind),
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
