"""Reading values out of eTools field monitoring records whose shape is not known in advance.

The key a value lives under is chosen by :mod:`neurodb.fmm.fields`; this module reads what is there,
whatever form it takes (:func:`value`):

- a dotted key ("question.text") walks objects, and through a list of objects it reads every element;
- an object yields the first of ``text``, ``title``, ``name``, ``label``, ``value``,
  ``reference_number``, ``id`` that suits the kind asked for (an id prefers ``id``);
- a list yields its elements joined with ", " for a text, and its first element otherwise;
- numbers become text for a text; true/false is a text "Yes" / "No";
- anything unreadable, or blank, is ``None``.

It also decides whether a checklist answer was given (:func:`read_answer`): an option code is first
replaced by its label (``fm_options``); an answer that is blank, or a placeholder such as "n/a" or
"see above", is not answered. Only the answer's code (a rating, yes or no) and word counts leave this
module, never its text.

For the pages and the chat, it reads the texts of a visit's answers back from their records
(:func:`visit_answers`) and searches the narratives and answers of a set of visits
(:func:`search_texts`): only the values of the answer, its label and its summary are searched, never a
key's name, another field or a value that holds a person.

Only this module, ``fields``, the visit builder and the visit page read records' ``data``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from neurodb.datamart import fm
from neurodb.watch import people

KINDS = ("text", "id", "int", "bool")
TEXT_KEYS = ("text", "title", "name", "label", "value", "reference_number", "id")
ID_KEYS = ("id", "text", "title", "name", "label", "value", "reference_number")  # an id prefers "id"
INT_KEYS = ("value", "id", "text", "title", "name", "label", "reference_number")
MAX_LIST = 100  # elements of a list read at most
PLACEHOLDERS = frozenset(
    {
        "n/a",
        "na",
        "-",
        "--",
        "none",
        "nil",
        ".",
        "no answer",
        "not applicable",
        "same as above",
        "see above",
        "test",
        "tbd",
    }
)
_INTEGER = re.compile(r"^-?\d+$")
_WORD = re.compile(r"[^\W_]+")
_NOT_WORD = re.compile(r"[^\w\s]|_")


# ------------------------------------------------------------------------------------------ values
def value(record: Any, key: str, kind: str = "text") -> Any:
    """The value of ``record`` under ``key`` read as ``kind`` (text, id, int or bool), or ``None``.
    A text is a non-blank ``str``; an id a positive ``int``; an int any ``int``; a bool a ``bool``."""
    if not isinstance(record, dict) or not key:
        return None
    return as_kind(walk(record, key), kind)


def walk(record: dict, key: str) -> Any:
    """The raw value under ``key``: the key itself when the record has it, else its dotted path."""
    if key in record:
        return record[key]
    return _walk(record, key.split("."))


def _walk(node: Any, parts: list[str]) -> Any:
    if not parts:
        return node
    head, rest = parts[0], parts[1:]
    if isinstance(node, dict):
        return _walk(node[head], rest) if head in node else None
    if isinstance(node, list):  # every element's value, e.g. "sections.name" over a list of sections
        found = [_walk(element, parts) for element in node[:MAX_LIST]]
        found = [f for f in found if f is not None]
        return found or None
    return None


def as_kind(raw: Any, kind: str = "text") -> Any:
    """``raw`` as a value of ``kind``, or ``None`` when it does not suit it."""
    if raw is None:
        return None
    if kind == "text":
        return _text(raw)
    if kind == "id":
        number = _integer(raw, ID_KEYS)
        return number if number is not None and number > 0 else None
    if kind == "int":
        return _integer(raw, INT_KEYS)
    if kind == "bool":
        if isinstance(raw, list):
            return as_kind(raw[0], "bool") if raw else None
        return raw if isinstance(raw, bool) else None
    raise ValueError(f"unknown kind {kind!r}")


def text_list(raw: Any) -> list[str]:
    """``raw`` as a list of texts, once each in their first order: a list gives each element's text
    (an object its name, label...), anything else its one text. Blank values are left out."""
    if raw is None:
        return []
    elements = raw[:MAX_LIST] if isinstance(raw, list) else [raw]
    out: list[str] = []
    for element in elements:
        text = _text(element)
        if text is not None and text not in out:
            out.append(text)
    return out


def fold(text: Any) -> str:
    """A text as the quality rules compare it: lower case, without accents, every character other than
    a letter, a digit or a space turned into a space, and runs of spaces made one."""
    return " ".join(_NOT_WORD.sub(" ", people.fold(str(text or ""))).split())


def question_key(question_id: Any, text: Any) -> str:
    """How a question is told apart: its id, else the sha1 of its folded text ("" when it has neither)."""
    number = as_kind(question_id, "id")
    if number is not None:
        return str(number)
    folded = fold(text)
    return hashlib.sha1(folded.encode(), usedforsecurity=False).hexdigest() if folded else ""


def _text(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return "Yes" if raw else "No"
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return str(int(raw)) if raw.is_integer() else str(raw)
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, dict):
        for name in TEXT_KEYS:
            if name in raw and (text := _text(raw[name])) is not None:
                return text
        return None
    if isinstance(raw, list):
        texts = [t for t in (_text(element) for element in raw[:MAX_LIST]) if t is not None]
        return ", ".join(texts) or None
    return None


def _integer(raw: Any, keys: tuple[str, ...]) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        text = raw.strip()
        return int(text) if _INTEGER.match(text) else None
    if isinstance(raw, dict):
        for name in keys:
            if name in raw and (number := _integer(raw[name], keys)) is not None:
                return number
        return None
    if isinstance(raw, list):
        return _integer(raw[0], keys) if raw else None
    return None


# ------------------------------------------------------------------------------------------ answers
def is_placeholder(text: Any) -> bool:
    """A text that stands for no answer: "n/a", "N/A.", "none", "see above", "-"..."""
    folded = " ".join(str(text or "").lower().split())
    return folded in PLACEHOLDERS or folded.rstrip(".!") in PLACEHOLDERS


def text_state(raw: Any) -> str:
    """'' for a blank or missing value, 'placeholder' for a placeholder, 'text' for anything else."""
    text = as_kind(raw, "text")
    if text is None:
        return ""
    return "placeholder" if is_placeholder(text) else "text"


def word_count(text: Any) -> int:
    return len(_WORD.findall(str(text or "")))


@dataclass(frozen=True)
class ParsedAnswer:
    """What NeuroDB keeps of one checklist answer: whether it was given, its code, never its text."""

    answered: bool
    placeholder: bool
    answer_code: str  # on_track | constrained | off_track | yes | no | ""
    rating: str  # a rating code when the answer is a rating word, else ""
    answer_words: int
    summary_words: int


def read_answer(
    answer: Any,
    label: Any = None,
    summary: Any = None,
    *,
    options: Mapping[tuple[str, str], str] | None = None,
    question_key: str = "",
) -> ParsedAnswer:
    """One answer, from the raw values under the chosen answer, answer label and summary keys.

    The answer shown is the label of its option (``options``: (question key, option value) -> label),
    else the answer label, else the answer itself. It is answered when it is not blank and not a
    placeholder; a blank answer with a summary counts as answered by its summary."""
    answer_text = as_kind(answer, "text") or ""
    label_text = as_kind(label, "text") or ""
    summary_text = as_kind(summary, "text") or ""
    shown = (options or {}).get((question_key, answer_text), "") if answer_text else ""
    shown = shown or label_text or answer_text
    state = text_state(shown) if shown else text_state(summary_text)
    answered = state == "text"
    rating = fm.normalize_rating(shown) if answered and shown else ""
    return ParsedAnswer(
        answered=answered,
        placeholder=state == "placeholder",
        answer_code=fm.normalize_answer(shown) if answered and shown else "",
        rating="" if rating == "other" else rating,
        answer_words=word_count(shown),
        summary_words=word_count(summary_text),
    )


# ------------------------------------------------------------------------------------------ texts
@dataclass(frozen=True)
class TextHit:
    """A narrative or an answer value of a visit holding the text searched for (the value only, raw)."""

    visit_key: str
    where: Literal["narrative", "answer", "summary"]
    text: str


ANSWER_TEXT_FIELDS = (("answer", "answer"), ("answer_label", "answer"), ("summary", "summary"))


def _folded(text: Any) -> str:
    return " ".join(people.fold(str(text or "")).split())


def _answer_keys() -> list[tuple[str, str]]:
    """(key, where) of the answer, answer label and summary fields of the checklist answers, as the
    last refresh chose them; a key that holds a person is never read."""
    from . import fields, privacy

    keys = []
    for field, where in ANSWER_TEXT_FIELDS:
        key = fields.key_for("fm_questions", field)
        if key and not privacy.person_like(key) and key not in {k for k, _ in keys}:
            keys.append((key, where))
    return keys


def search_texts(visit_keys: Collection[str], needle: str, limit: int) -> list[TextHit]:
    """The narratives and the answer values of the visits ``visit_keys`` that hold ``needle`` (folded:
    case and accents do not matter), newest visits first, at most ``limit``. An answer record counts
    only when the needle is in the value of its answer, answer label or summary: a record that holds
    it in a key's name, in a value that holds a person or in any other field does not."""
    from django.db.models import F, TextField
    from django.db.models.functions import Cast

    from neurodb.datamart.models import DatamartDocument, MonitoringFinding

    from .models import QuestionAnswer, Visit, VisitEntity

    wanted = _folded(needle)
    if not wanted or limit <= 0 or not visit_keys:
        return []
    order = {
        key: rank
        for rank, key in enumerate(
            Visit.objects.filter(key__in=list(visit_keys))
            .order_by(F("end_date").desc(nulls_last=True), "key")  # a visit without a date last
            .values_list("key", flat=True)
        )
    }
    if not order:
        return []
    hits: list[tuple[int, int, TextHit]] = []
    # 1. narratives
    rows = VisitEntity.objects.filter(visit__key__in=list(order)).exclude(finding_id=None)
    finding_keys = dict(rows.values_list("finding_id", "visit__key"))
    narratives = MonitoringFinding.objects.filter(
        pk__in=list(finding_keys), narrative_finding__icontains=needle.strip()
    ).values_list("pk", "narrative_finding")
    for pk, narrative in narratives:
        if wanted in _folded(narrative):
            key = finding_keys[pk]
            hits.append((order[key], 0, TextHit(key, "narrative", narrative)))
    # 2. answers: a cheap filter on the record's text, then the value itself
    keys = _answer_keys()
    if keys:
        answers = QuestionAnswer.objects.filter(visit__key__in=list(order))
        document_keys = dict(answers.values_list("document_id", "visit_key"))
        documents = DatamartDocument.objects.filter(dataset="fm_questions", pk__in=list(document_keys))
        if '"' not in needle and "\\" not in needle:
            documents = documents.annotate(text=Cast("data", TextField())).filter(
                text__icontains=needle.strip()
            )
        for pk, data in documents.values_list("pk", "data").iterator(chunk_size=2000):
            seen: set[str] = set()
            for key, where in keys:
                value = as_kind(walk(data, key) if isinstance(data, dict) else None, "text")
                if value and value not in seen and wanted in _folded(value):
                    seen.add(value)
                    visit_key = document_keys[pk]
                    hits.append((order[visit_key], 1, TextHit(visit_key, where, value)))
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    return [hit for _, _, hit in hits[:limit]]


def visit_answers(visit) -> list[tuple[str, str, str]]:
    """(question, answer, summary) of each checklist answer of ``visit``, in the checklist's order, read
    from the answer records: the answer shown is its option's label, else its label, else as written.
    For the visit page and the chat only, which clean what they show or send."""
    from neurodb.datamart.models import DatamartDocument

    from . import fields
    from .models import QuestionAnswer

    answers = list(
        QuestionAnswer.objects.filter(visit=visit).order_by("question_order", "question_key", "document_id")
    )
    if not answers:
        return []
    records = dict(
        DatamartDocument.objects.filter(
            dataset="fm_questions", pk__in=[a.document_id for a in answers]
        ).values_list("pk", "data")
    )
    answer_key = fields.key_for("fm_questions", "answer")
    label_key = fields.key_for("fm_questions", "answer_label")
    summary_key = fields.key_for("fm_questions", "summary")
    options = option_labels({a.question_key for a in answers})
    out = []
    for answer in answers:
        record = records.get(answer.document_id)
        if not isinstance(record, dict):
            continue
        written = value(record, answer_key) if answer_key else None
        shown = options.get((answer.question_key, written or ""), "") if written else ""
        shown = shown or (value(record, label_key) if label_key else None) or written or ""
        summary = (value(record, summary_key) if summary_key else None) or ""
        out.append((answer.question_text, shown, summary))
    return out


def answer_texts(document_ids: Collection[int], keys: Mapping[str, str | None]) -> dict[int, str]:
    """{record pk: the answer as written (its label first)} of the checklist answer records given, read
    under the answer and answer label keys of ``keys``; for the quality rules' placeholder check of
    the Q3 answers (R5). Kept in memory while a visit is scored, never stored or sent."""
    from neurodb.datamart.models import DatamartDocument

    label_key, answer_key = keys.get("answer_label"), keys.get("answer")
    if not document_ids or not (label_key or answer_key):
        return {}
    rows = DatamartDocument.objects.filter(dataset="fm_questions", pk__in=list(document_ids)).values_list(
        "pk", "data"
    )
    out: dict[int, str] = {}
    for pk, data in rows.iterator(chunk_size=2000):
        if not isinstance(data, dict):
            continue
        text = (value(data, label_key) if label_key else None) or (
            value(data, answer_key) if answer_key else None
        )
        if text:
            out[pk] = text
    return out


def option_labels(question_keys: Collection[str] | None = None) -> dict[tuple[str, str], str]:
    """{(question key, option value): label} of the answer options (``fm_options``), for the questions
    given (every question when None)."""
    from . import fields

    question = fields.key_for("fm_options", "question_id")
    code = fields.key_for("fm_options", "value")
    label = fields.key_for("fm_options", "label")
    if not (question and code and label):
        return {}
    wanted = set(question_keys) if question_keys is not None else None
    out: dict[tuple[str, str], str] = {}
    for _pk, record in fields.records("fm_options"):
        options_entry(record, question, code, label, out, wanted)
    return out


def options_entry(
    record: Any,
    question: str,
    code: str,
    label: str,
    into: dict[tuple[str, str], str],
    wanted: set[str] | None = None,
) -> None:
    """Add one answer option record to ``into`` ({(question key, option value): label})."""
    question_id = value(record, question, "id")
    option = value(record, code, "text")
    text = value(record, label, "text")
    if question_id is None or option is None or text is None:
        return
    key = str(question_id)
    if wanted is None or key in wanted:
        into.setdefault((key, option), text)
