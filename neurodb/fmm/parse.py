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

Only this module, ``fields``, the visit builder and the visit page read records' ``data``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from neurodb.datamart import fm

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
