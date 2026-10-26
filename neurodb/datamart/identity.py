"""Which programme document indicator a partner report row belongs to, decided one way for every page.

An *identity* is one PD indicator of its programme document: its eTools id (``PDIndicator.source_id``), else
its title (:func:`ident_of`); the Datamart lists it once per location and disaggregation. A partner report
row (``ReportedIndicator``) belongs, in this order, to the indicator whose eTools id it carries
(``etools_indicator_id``), else to the one indicator of its PD with its title, else, for a title two or more
indicators of the PD share, to the one with the row's PD output and the title (:meth:`Idents.of`).

The rows of a title and an output two or more indicators of a PD share (``"shared_output"``; the partner
reports carry no PD indicator id) are then told apart by **tie-breakers** (:data:`BREAKERS`, in that
order): what the partner reports and eTools both say of an indicator. They decide per *placement group*
(:func:`group_key`: the rows of one progress report with one title, one output and one signature), never
per row, so a report is never half placed, and only:

* when exactly one indicator matches, and every other tie-breaker in use that can say something agrees;
* when no two rows of the group at one location carry different values (both indicators may be in it);
* when no row is at a place another indicator plans and the chosen one does not;
* when the chosen indicator gets no other rows of that progress report (Reached would take the larger);
* and once the data proves the tie-breaker (:func:`calibrate`): on the groups placed by id, title or output
  it was compared at least :data:`CALIBRATION_MIN` times and never differed. The sync stores that proof in
  its run (:func:`calibration`); without it no tie-breaker is used, and the rows stay left out as before.

Nothing here reads a title, an output or a name beyond comparing it (case and spaces aside, ``reached.norm``).
"""

from __future__ import annotations

import functools
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q, QuerySet
from django.db.models.fields.json import KT, KeyTransform
from django.utils import timezone

from neurodb.core import compute

from . import disaggregation
from . import models as dm
from . import reached as rc
from .reached import norm

# the tie-breakers, in the order they are tried (B.1.6 of the design): each compares a partner report key
# with a PD indicator field
BREAKERS = ("high_frequency", "cluster", "target", "indicator_type", "disaggregations", "locations", "active")
DIAGNOSTIC_ONLY = ("means_of_verification", "section")  # free text, or the PD's: check 7 shows them, no page
LABELS = {
    "high_frequency": "High frequency",
    "cluster": "Cluster indicator",
    "target": "Target",
    "indicator_type": "Indicator type",
    "disaggregations": "Disaggregations",
    "locations": "Planned locations",
    "active": "Active",
    "means_of_verification": "Means of verification",
    "section": "Section",
}
# the report row key and the PD indicator field each compares (check 7's Table A)
KEYS = {
    "high_frequency": ("high_frequency", "is_high_frequency"),
    "cluster": ("cluster_indicator", "cluster_name"),
    "target": ("target", "target_numerator, target_denominator"),
    "indicator_type": ("indicator_type", "display_type, unit"),
    "disaggregations": ("disaggregation", "disaggregation_name"),
    "locations": ("p_code", "location_pcode"),
    "active": ("", "is_active"),
    "means_of_verification": ("means_of_verification", "means_of_verification"),
    "section": ("section", "section_name"),
}
PLACED_BY_KEY = ("id", "title", "output")  # the ways today's rule places a row
# each way a row is placed or left out (:class:`Placed`), in plain words: check 7's table and the figure trail
WAY_WORDS = {
    "id": "matched to a PD indicator by its id",
    "title": "matched by title (one indicator of the PD has it)",
    "output": "matched by PD output and title (a title several indicators of the PD share)",
    "shared_output": "two or more indicators of the PD have the title and the output",
    "keys_disagree": "two of the row's fields name different indicators",
    "two_for_one": "two indicator reports would go to one indicator",
    "side_by_side": "two rows of the report at one location carry different values (both indicators may be "
    "in it)",
    "output_unmatched": "a title several indicators of the PD share, and a PD output none of them has",
    "unknown": "no indicator of the PD has the title",
}
CALIBRATION_MIN = 20  # placed groups a tie-breaker is compared on before it is used: a few prove nothing
CALIBRATION_VERSION = 1  # bumped when a tie-breaker's rule, key or the gate changes: an old proof is ignored
TARGET_TOLERANCE = 1e-6  # two targets within this (relative) are one: a decimal copy, never another target
CALIBRATED_BY = ("partner_reports", "pd_indicators")  # the sync runs that store the proof
SIGNATURE_CHUNK = 5000  # rows whose signature is read in one query
CALIBRATION_CHUNK = 2000  # partner report rows :func:`calibrate` reads at a time
INDICATOR_CHUNK = 2000  # PD indicator rows :class:`Idents` reads at a time
ACTIVE_NOTE = (
    "Its programme document also holds an inactive indicator with this title and output: the partner "
    "reports are read as this one's."
)
# PRP's cluster_indicator: the words for no, and placeholders that say nothing (other text names a cluster)
NO = frozenset({"false", "0", "no", "f", "n"})
NOTHING = frozenset({"", "none", "null", "n/a", "na", "-"})
VOCABULARY = re.compile(r"[a-z0-9_ .-]{1,40}")  # an indicator type kept in the proof: a word, never a text

# the raw keys of a partner report record a group is told apart by (read only for the rows that need them)
SIGNATURE_ANNOTATIONS = {"_cluster": KT("data__cluster_indicator"), "_type": KT("data__indicator_type")}
# the row fields :meth:`Idents.place` reads (``_cluster`` and ``_type`` too when given, else it reads them)
PLACE_FIELDS = (
    "datamart_id",
    *rc.IDENTITY_FIELDS,
    "progress_report",
    "etools_indicator_id",
    "indicator",
    "pd_output",
    "high_frequency",
    "target",
    "disaggregation",
    "achievement_in_period",
    "total_cumulative_progress_in_location",
    "previous_location_progress",
    "calculation_across_periods",
    "p_code",
    "location",
    "section",
)
# the PD indicator fields an identity is read from (:class:`Candidate`, :meth:`Idents.definition`)
_FIELDS = (
    "intervention_id",
    "source_id",
    "title",
    "lower_result_name",
    "is_high_frequency",
    "cluster_name",
    "target_numerator",
    "target_denominator",
    "display_type",
    "unit",
    "disaggregation_name",
    "location_pcode",
    "is_active",
    "means_of_verification",
    "section_name",
)


def ident_of(pd_id: int, source_id: int | None, title: str | None) -> tuple[int, str]:
    """An indicator's identity in its PD: its eTools id, else its title (when it has no id)."""
    return (pd_id, f"s{source_id}") if source_id else (pd_id, f"t{norm(title)}")


def target_of(row: dict[str, Any]) -> float | None:
    """A PD indicator's target as Partner monitoring shows it: a percentage × 100, a ratio over its
    denominator, else the numerator."""
    numerator = rc.number(row["target_numerator"])
    denominator = rc.number(row["target_denominator"])
    if row["display_type"] in ("percentage", "ratio") and numerator is not None and denominator:
        return (
            numerator / denominator * 100 if row["display_type"] == "percentage" else numerator / denominator
        )
    return numerator


def with_signature(queryset: QuerySet) -> QuerySet:
    """Partner report rows with their signature keys (``_cluster``, ``_type``): for the rows that need them
    only, as ``KT`` over ``data`` reads each row's whole record."""
    return queryset.annotate(**SIGNATURE_ANNOTATIONS)


def _agreed(values: Iterable[Any]) -> Any:
    """The one value every item gives, else None (none given, or two)."""
    found = set(values)
    return found.pop() if len(found) == 1 else None


def _pair(numerator: Any, denominator: Any) -> tuple[float, float] | None:
    """A target as (value, denominator), a missing or zero denominator read as 1."""
    v, d = rc.number(numerator), rc.number(denominator)
    return None if v is None else (v, d if d else 1.0)


def _same_target(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return math.isclose(a[0], b[0], rel_tol=TARGET_TOLERANCE) and math.isclose(
        a[1], b[1], rel_tol=TARGET_TOLERANCE
    )


def flag(text: Any) -> bool | None:
    """PRP's ``cluster_indicator`` as yes or no: true, 1, yes or any other text yes; false, 0 or no no; empty,
    none, null, n/a or a dash not given."""
    word = norm(str(text)) if text is not None else ""
    if word in NOTHING:
        return None
    return word not in NO


def _value_ids(found: Any) -> frozenset[int] | None:
    """The disaggregation value ids a row's values name (the location total "()" names none); None when it
    has no value at all or names one in words (its disaggregation cannot be known)."""
    if not isinstance(found, dict) or not found:
        return None
    ids: set[int] = set()
    for key in found:
        parts = disaggregation.parse_key(key)
        if parts == ():
            continue
        if not parts or not all(isinstance(p, int) for p in parts):
            return None
        ids.update(parts)
    return frozenset(ids)


def group_key(row: dict[str, Any]) -> tuple:
    """A placement group: one progress report (``reached.report_key``), one title, one PD output and one
    signature (high frequency, PRP's ``cluster_indicator`` and ``indicator_type``): two PRP indicators side
    by side with different signatures are two groups."""
    return (
        rc.report_key(row),
        norm(row.get("indicator")),
        norm(row.get("pd_output")),
        bool(row.get("high_frequency")),
        row.get("_cluster"),
        row.get("_type"),
    )


def _location(row: dict[str, Any]) -> str:
    return (
        rc.location_key(row.get("p_code"), row.get("location"))
        if (row.get("p_code") or row.get("location"))
        else ""
    )


class Group:
    """The rows of one placement group (:func:`group_key`), as the tie-breakers read them."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.pd = row["intervention_id"]
        self.title, self.output = row.get("indicator") or "", row.get("pd_output") or ""
        self.report = rc.report_key(row)
        self.report_type = (row.get("report_type") or "").strip().upper()
        self.high_frequency = bool(row.get("high_frequency"))
        self.cluster, self.kind = row.get("_cluster"), row.get("_type")
        self.rows: list[int] = []  # their places in the rows given to :meth:`Idents.place`
        self.count = 0
        self.targets: set[tuple[float, float] | None] = set()
        self.keys: set[frozenset[int] | None] = set()  # each row's disaggregation value ids
        self.places: set[str] = set()  # the rows' P-codes
        self.unplaced = False  # a row without a P-code
        self.means: set[str] = set()
        self.sections: set[str] = set()
        self.values: dict[str, set[float | None]] = defaultdict(set)  # location -> the values its rows carry

    def add(self, row: dict[str, Any], index: int | None = None) -> None:
        self.count += 1
        if index is not None:
            self.rows.append(index)
        found = rc.parts(row.get("target"))
        self.targets.add(_pair(found.v, found.d) if found else None)
        self.keys.add(_value_ids(row.get("disaggregation")))
        code = (row.get("p_code") or "").strip().upper()
        if code:
            self.places.add(code)
        else:
            self.unplaced = True
        if norm(row.get("_means")):
            self.means.add(norm(row.get("_means")))
        self.sections.add(norm(row.get("section")))
        self.values[_location(row)].add(rc.location_value(row))

    @property
    def target(self) -> tuple[float, float] | None:
        """The target every row of the group gives (None when one gives none or two differ)."""
        return next(iter(self.targets)) if len(self.targets) == 1 else None

    @property
    def side_by_side(self) -> bool:
        """Two rows at one location carry different values: the group may hold both indicators' rows."""
        return any(len(found) > 1 for found in self.values.values())

    def disaggregation_types(self, labels: disaggregation.Labels) -> frozenset[str] | None:
        """The disaggregations (their names, normalised) the group's values name, when every row names the
        same and each value's label and disaggregation are read; else None."""
        found: set[frozenset[str]] = set()
        for ids in self.keys:
            if ids is None:
                return None
            names = set()
            for pk in ids:
                value = labels.value(pk)
                name = norm(labels.types.get(value[1])) if value else ""
                if not name:
                    return None
                names.add(name)
            found.add(frozenset(names))
        return found.pop() if len(found) == 1 else None


class Seen:
    """What the PD indicator rows of one identity say for the tie-breakers (:class:`Candidate`): the values
    each field takes over its rows, gathered as they are read. The rows themselves are never kept: the
    Datamart lists an indicator once per location and disaggregation, hundreds of thousands of rows."""

    __slots__ = (
        "active",
        "cluster",
        "disaggregations",
        "high_frequency",
        "kinds",
        "means",
        "planned",
        "sections",
        "targets",
    )

    def __init__(self) -> None:
        self.high_frequency: set[bool] = set()
        self.cluster = False
        self.targets: set[tuple[float, float] | None] = set()
        self.kinds: set[tuple[str, str]] = set()
        self.disaggregations: set[str] = set()
        self.planned: set[str] = set()
        self.active: set[bool] = set()
        self.means: set[str] = set()
        self.sections: set[str] = set()

    def add(self, row: tuple) -> None:
        """One row of the identity (its :data:`_FIELDS`)."""
        (_pd, _source, _title, _output, high_frequency, cluster, numerator, denominator, display_type, unit,
         disaggregation_name, p_code, active, means, section) = row  # fmt: skip
        self.high_frequency.add(bool(high_frequency))
        self.cluster = self.cluster or bool((cluster or "").strip())
        self.targets.add(_pair(numerator, denominator))
        self.kinds.add((norm(display_type), norm(unit)))
        if norm(disaggregation_name):
            self.disaggregations.add(norm(disaggregation_name))
        if (p_code or "").strip():
            self.planned.add(p_code.strip().upper())
        self.active.add(bool(active))
        if norm(means):
            self.means.add(norm(means))
        if norm(section):
            self.sections.add(norm(section))


@dataclass(frozen=True)
class Candidate:
    """A PD indicator as the tie-breakers read it, from every row of its identity (None: its rows disagree, or
    give nothing)."""

    ident: tuple[int, str]
    high_frequency: bool | None
    cluster: bool
    target: tuple[float, float] | None
    kind: tuple[str, str] | None  # (display type, unit)
    disaggregations: frozenset[str]
    planned: frozenset[str]  # P-codes
    active: bool | None
    means: str
    section: str

    @classmethod
    def of(cls, ident: tuple[int, str], seen: Seen) -> Candidate:
        return cls(
            ident=ident,
            high_frequency=_agreed(seen.high_frequency),
            cluster=seen.cluster,
            target=_agreed(seen.targets),
            kind=_agreed(seen.kinds),
            disaggregations=frozenset(seen.disaggregations),
            planned=frozenset(seen.planned),
            active=_agreed(seen.active),
            means=_agreed(seen.means) or "",
            section=_agreed(seen.sections) or "",
        )

    def value(self, name: str) -> Any:
        """What it says for a tie-breaker (check 7 compares the indicators of a shared title and output)."""
        return {
            "high_frequency": self.high_frequency,
            "cluster": self.cluster,
            "target": self.target,
            "indicator_type": self.kind,
            "disaggregations": self.disaggregations,
            "locations": self.planned,
            "active": self.active,
            "means_of_verification": self.means or None,
            "section": self.section or None,
        }[name]


@dataclass(frozen=True)
class Placed:
    """Where a row went: ``ident`` (None: left out) and ``way``: ``"id"``, ``"title"``, ``"output"``, a
    tie-breaker's name (:data:`BREAKERS`), or why it is left out: ``"shared_output"`` (no tie-breaker tells
    the indicators apart), ``"keys_disagree"`` (two keys name different indicators, or a row is at a place
    only another indicator plans), ``"two_for_one"`` (two indicator reports of one report would go to one
    indicator), ``"side_by_side"`` (two rows at one location carry different values), ``"output_unmatched"``,
    ``"unknown"`` (see :meth:`Idents.how`)."""

    ident: tuple[int, str] | None
    way: str


@dataclass(frozen=True)
class Calibration:
    """The proof of the tie-breakers (:func:`calibrate`), as a sync run stored it: ``enabled``, the
    tie-breakers the pages use; ``types``, each PRP indicator type's (display type, unit); ``stats``, per
    tie-breaker, the placed groups ``compared``, the ``same``, those that ``differ`` and those ``not_given``.
    Empty (no tie-breaker) when nothing is stored, or it was stored under another
    :data:`CALIBRATION_VERSION`."""

    enabled: frozenset[str] = frozenset()
    types: dict[str, tuple[str, str]] = field(default_factory=dict)
    stats: dict[str, dict[str, int]] = field(default_factory=dict)
    computed_at: str = ""
    run_id: int | None = None

    @classmethod
    def of(cls, stored: Any, run_id: int | None = None) -> Calibration:
        if not isinstance(stored, dict) or stored.get("version") != CALIBRATION_VERSION:
            return cls(run_id=run_id)
        types = {
            str(kind): (str(pair[0]), str(pair[1]))
            for kind, pair in (stored.get("types") or {}).items()
            if isinstance(pair, list | tuple) and len(pair) == 2
        }
        stats = {
            str(name): {k: int(v) for k, v in found.items() if isinstance(v, int)}
            for name, found in (stored.get("stats") or {}).items()
            if isinstance(found, dict)
        }
        return cls(
            enabled=frozenset(name for name in stored.get("enabled") or () if name in BREAKERS),
            types=types,
            stats=stats,
            computed_at=str(stored.get("computed_at") or ""),
            run_id=run_id,
        )


def calibration() -> Calibration:
    """The proof the newest finished partner reports or PD indicators sync stored (``details["identity"]``),
    read once per request: the same on every page. None stored (a new install, a run before this rule, a
    failed proof): no tie-breaker."""

    def read() -> Calibration:
        from neurodb.core.models import SyncRun

        found = (
            SyncRun.objects.filter(
                job=SyncRun.Job.ETOOLS_DATAMART,
                target__in=CALIBRATED_BY,
                status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
            )
            .order_by("-started_at", "-pk")
            .annotate(_identity=KeyTransform("identity", "details"))
            .values_list("pk", "_identity")
            .first()
        )
        return Calibration.of(found[1], run_id=found[0]) if found else Calibration()

    return compute.per_request("datamart_identity_calibration", read)


def _stored() -> Calibration:
    return calibration()  # what :class:`Idents` reads when given none (its argument hides the name)


def indicators_read_whole() -> bool:
    """Whether the latest finished PD indicators sync read them whole: it succeeded, removed what the Datamart
    no longer holds (no ``removal_skipped``, no ``empty_response``) and its answer was not cut
    (``cap_suspected``; a round count read in one answer, not in windows): only then is a place no indicator
    plans in eTools truly unplanned. Read once per request."""

    def read() -> bool:
        from neurodb.core.models import SyncRun
        from neurodb.integrations.etools.datamart_sync import capped

        found = (
            SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART, target="pd_indicators")
            .exclude(status=SyncRun.Status.RUNNING)
            .order_by("-started_at", "-pk")
            .values_list("status", "details")
            .first()
        )
        if found is None:
            return False
        status, details = found[0], found[1] if isinstance(found[1], dict) else {}
        return (
            status == SyncRun.Status.SUCCEEDED
            and not details.get("removal_skipped")
            and not details.get("empty_response")
            and not details.get("cap_suspected")
            and not (capped(details.get("datamart_count")) and not details.get("window_bound"))
        )

    return compute.per_request("datamart_indicators_read_whole", read)


def _signatures(rows: list[dict[str, Any]]) -> dict[int, tuple[Any, Any, Any]]:
    """The signature keys and the whole disaggregation of these rows, by Datamart id: read only for the rows
    under a shared title and output (``KT`` over ``data`` reads each row's whole record)."""
    ids = sorted({row["datamart_id"] for row in rows})
    out: dict[int, tuple[Any, Any, Any]] = {}
    for start in range(0, len(ids), SIGNATURE_CHUNK):
        chunk = dm.ReportedIndicator.objects.filter(datamart_id__in=ids[start : start + SIGNATURE_CHUNK])
        found = with_signature(chunk.order_by()).values_list(
            "datamart_id", "_cluster", "_type", "disaggregation"
        )
        for pk, cluster, kind, values in found:
            out[pk] = (cluster, kind, values)
    return out


def group_labels(groups: Iterable[Group]) -> disaggregation.Labels:
    """The labels of the disaggregation values these groups name (one query)."""
    ids = {pk for group in groups for keys in group.keys if keys for pk in keys}
    return disaggregation.load_labels(ids)


class Idents:
    """Which PD indicator a report row belongs to (the module docstring): built over every indicator of the
    PDs, not only those a filter keeps, so a row of a filtered-out indicator is skipped instead of falling
    back to another indicator's title. ``calibration``: the tie-breakers' proof (None: the one stored,
    :func:`calibration`)."""

    def __init__(self, pd_ids: Iterable[int], calibration: Calibration | None = None) -> None:
        self.calibration = calibration if calibration is not None else _stored()
        self.by_source: dict[tuple[int, str], tuple[int, str]] = {}
        titles: dict[tuple[int, str], set[tuple[int, str]]] = defaultdict(set)
        outputs: dict[tuple[int, str, str], set[tuple[int, str]]] = defaultdict(set)
        # each identity's rows as the tie-breakers read them, and its definition (its first row's, in the
        # order Partner monitoring reads them): never the rows themselves
        self._seen: dict[tuple[int, str], Seen] = defaultdict(Seen)
        self._definitions: dict[tuple[int, str], tuple[str, str, float | None]] = {}
        self._order: dict[tuple[int, str], int] = {}  # each identity's first row, in the definition order
        self._candidates: dict[tuple[int, str], Candidate] = {}
        rows = (
            dm.PDIndicator.objects.filter(intervention_id__in=sorted(set(pd_ids)))
            .order_by("lower_result_name", "title", "datamart_id")
            .values_list(*_FIELDS)
        )
        for row in rows.iterator(chunk_size=INDICATOR_CHUNK):
            pd_id, source_id, title, output = row[:4]
            ident = ident_of(pd_id, source_id, title)
            if source_id:
                self.by_source.setdefault((pd_id, str(source_id)), ident)
            titles[(pd_id, norm(title))].add(ident)
            outputs[(pd_id, norm(output), norm(title))].add(ident)
            self._seen[ident].add(row)
            if ident not in self._order:
                self._order[ident] = len(self._order)
                first = dict(zip(_FIELDS, row, strict=True))
                self._definitions[ident] = (first["display_type"], first["unit"], target_of(first))
        self._titles = titles
        self.by_title = {key: next(iter(found)) for key, found in titles.items() if len(found) == 1}
        # the titles two or more indicators of a PD share: a row is then placed by its output and title,
        # when one of them has both, else told apart by the tie-breakers or left out
        self.shared = {key for key, found in titles.items() if len(found) > 1}
        self.by_output = {
            key: next(iter(found))
            for key, found in outputs.items()
            if len(found) == 1 and (key[0], key[2]) in self.shared
        }
        # an output and a title two or more indicators of a PD share: only a tie-breaker tells them apart
        self.shared_output = {
            key: sorted(found, key=self._order.__getitem__)
            for key, found in outputs.items()
            if len(found) > 1 and (key[0], key[2]) in self.shared
        }

    # --------------------------------------------------------------------------- today's rule, per row
    def of(
        self, pd_id: int, etools_id: Any, title: str | None, output: str | None = None
    ) -> tuple[int, str] | None:
        source = str(etools_id or "").strip()
        if source and (pd_id, source) in self.by_source:
            return self.by_source[(pd_id, source)]
        found = self.by_title.get((pd_id, norm(title)))
        if found is None and output:
            found = self.by_output.get((pd_id, norm(output), norm(title)))
        return found

    def how(self, pd_id: int, etools_id: Any, title: str | None, output: str | None = None) -> str:
        """How :meth:`of` reads a row (the eTools reporting checks and Data quality count them): ``"id"``,
        ``"title"``, ``"output"`` (a title several indicators share, told apart by the output), and the rows
        it leaves out: ``"shared_output"`` (two or more indicators of the PD share the title and the row's
        output: :meth:`place` may tell them apart), ``"output_unmatched"`` (the title is several indicators',
        and the row's PD output is empty or matches none of their outputs in eTools) or ``"unknown"`` (no
        indicator of the PD has the title)."""
        source = str(etools_id or "").strip()
        if source and (pd_id, source) in self.by_source:
            return "id"
        key = (pd_id, norm(title))
        if key in self.by_title:
            return "title"
        if key not in self.shared:
            return "unknown"
        place = (pd_id, norm(output), norm(title))
        if output and place in self.by_output:
            return "output"
        return "shared_output" if place in self.shared_output else "output_unmatched"

    # ------------------------------------------------------------------------------- the indicators
    def candidate(self, ident: tuple[int, str]) -> Candidate:
        found = self._candidates.get(ident)
        if found is None:
            found = self._candidates[ident] = Candidate.of(ident, self._seen.get(ident) or Seen())
        return found

    def candidates(self, pd_id: Any, output: str | None, title: str | None) -> list[Candidate]:
        """The indicators sharing this title and output in the PD (none: they are not shared)."""
        return [self.candidate(i) for i in self.shared_output.get((pd_id, norm(output), norm(title)), ())]

    def titled(self, pd_id: Any, title: str | None) -> list[tuple[int, str]]:
        """The identities of the PD with this title, in the order Partner monitoring reads them."""
        return sorted(self._titles.get((pd_id, norm(title)), ()), key=self._order.__getitem__)

    def definition(self, ident: tuple[int, str]) -> tuple[str, str, float | None] | None:
        """(display type, unit, target) of an indicator: its first row, in the order Partner monitoring
        reads them (output, title, Datamart id)."""
        return self._definitions.get(ident)

    @functools.cached_property
    def read_whole(self) -> bool:
        return indicators_read_whole()

    # ------------------------------------------------------------------------------ the tie-breakers
    def answer(
        self, name: str, group: Group, cands: list[Candidate], labels: disaggregation.Labels
    ) -> tuple[list[Candidate], bool] | None:
        """One tie-breaker's answer for a group: the candidates it matches and whether it decides (exactly
        one); None when it does not apply (the group or a candidate lacks the value)."""
        if name == "high_frequency":
            if any(c.high_frequency is None for c in cands):
                return None
            # NeuroDB's own rule for monthly reports: an indicator neither high frequency nor a cluster's
            hr = group.report_type == "HR"
            found = [
                c
                for c in cands
                if c.high_frequency == group.high_frequency
                and not (hr and not c.high_frequency and not c.cluster)
            ]
        elif name == "cluster":
            value = flag(group.cluster)
            if value is None:
                return None
            found = [c for c in cands if c.cluster == value]
        elif name == "target":
            value = group.target
            if value is None or any(c.target is None for c in cands):
                return None
            found = [c for c in cands if _same_target(value, c.target)]
        elif name == "indicator_type":
            pair = self.calibration.types.get(norm(group.kind)) if norm(group.kind) else None
            if pair is None or any(c.kind is None for c in cands):
                return None
            found = [c for c in cands if c.kind == pair]
        elif name == "disaggregations":
            value = group.disaggregation_types(labels)
            if value is None:
                return None
            found = [c for c in cands if c.disaggregations == value]
        elif name == "locations":
            if not self.read_whole or group.unplaced or not group.places or any(not c.planned for c in cands):
                return None
            found = [c for c in cands if group.places <= c.planned]
            # decides only when every row is at a place the one indicator plans and no other does
            alone = len(found) == 1 and not any(group.places & c.planned for c in cands if c is not found[0])
            return found, alone
        elif name == "active":
            if any(c.active is None for c in cands):
                return None
            found = [c for c in cands if c.active]
        elif name in DIAGNOSTIC_ONLY:
            value = (
                _agreed(group.means) if name == "means_of_verification" else _agreed(group.sections - {""})
            )
            mine = [c.means if name == "means_of_verification" else c.section for c in cands]
            if not value or not all(mine):
                return None
            found = [c for c, theirs in zip(cands, mine, strict=True) if theirs == value]
        else:
            raise KeyError(name)
        return found, len(found) == 1

    def try_each(
        self, group: Group, labels: disaggregation.Labels
    ) -> dict[str, tuple[list[Candidate], bool] | None]:
        """Every tie-breaker's own answer for a group, used on the pages or not (check 7)."""
        cands = self.candidates(group.pd, group.output, group.title)
        return {name: self.answer(name, group, cands, labels) for name in (*BREAKERS, *DIAGNOSTIC_ONLY)}

    def _decide(self, group: Group, labels: disaggregation.Labels) -> tuple[Candidate | None, str]:
        cands = self.candidates(group.pd, group.output, group.title)
        answers: list[list[Candidate]] = []
        winner, way = None, "shared_output"
        for name in BREAKERS:
            if name not in self.calibration.enabled:
                continue
            found = self.answer(name, group, cands, labels)
            if found is None:
                continue
            answers.append(found[0])
            if found[1] and winner is None:
                winner, way = found[0][0], name
        if winner is None:
            return None, "shared_output"
        if group.side_by_side:
            return None, "side_by_side"
        # every tie-breaker in use that says something must match the winner (one that matches none, or only
        # others, finds the group unlike every placed one); and no row at a place only another one plans, by
        # the places held whatever the last PD indicators read was: this only keeps rows out (the planned
        # locations tie-breaker, which places them, waits for a whole read)
        if any(all(c.ident != winner.ident for c in found) for found in answers):
            return None, "keys_disagree"
        if any(
            code not in winner.planned and any(code in c.planned for c in cands if c.ident != winner.ident)
            for code in group.places
        ):
            return None, "keys_disagree"
        return winner, way

    def groups(self, rows: list[dict[str, Any]], indices: Iterable[int]) -> dict[tuple, Group]:
        """The placement groups of these rows (their places in ``rows``), the signature read for those that do
        not carry it."""
        indices = list(indices)
        extra = _signatures([rows[n] for n in indices if "_cluster" not in rows[n]])
        out: dict[tuple, Group] = {}
        for n in indices:
            row = rows[n]
            more = extra.get(row["datamart_id"])
            if more is not None:
                row = {**row, "_cluster": more[0], "_type": more[1], "disaggregation": more[2]}
            key = group_key(row)
            group = out.get(key)
            if group is None:
                group = out[key] = Group(row)
            group.add(row, n)
        return out

    def place(self, rows: list[dict[str, Any]]) -> list[Placed]:
        """Where each row goes (aligned with ``rows``): today's rule per row (:meth:`of`), then the
        tie-breakers the calibration enables for the rows of a shared title and output, per placement group.
        A tie-breaker that gives one indicator two groups of one progress report, or a group of a report
        whose other rows it already has, places none of them (``"two_for_one"``): Reached never takes the
        larger of two."""
        out: list[Placed] = []
        shared: list[int] = []
        for n, row in enumerate(rows):
            pd, eid, title, output = (
                row["intervention_id"],
                row.get("etools_indicator_id"),
                row.get("indicator"),
                row.get("pd_output"),
            )
            way = self.how(pd, eid, title, output)
            out.append(Placed(self.of(pd, eid, title, output) if way in PLACED_BY_KEY else None, way))
            if way == "shared_output":
                shared.append(n)
        if not shared or not self.calibration.enabled:
            return out
        groups = self.groups(rows, shared)
        labels = group_labels(groups.values()) if "disaggregations" in self.calibration.enabled else None
        chosen: dict[tuple[str, tuple[int, str]], list[tuple[Group, str]]] = defaultdict(list)
        for group in groups.values():
            winner, way = self._decide(group, labels or disaggregation.Labels())
            if winner is None:
                for n in group.rows:
                    out[n] = Placed(None, way)
            else:
                chosen[(group.report, winner.ident)].append((group, way))
        wanted = {ident for _, ident in chosen}
        direct = {(rc.report_key(rows[n]), p.ident) for n, p in enumerate(out) if p.ident in wanted}
        for (report, ident), found in chosen.items():
            twice = len(found) > 1 or (report, ident) in direct
            for group, way in found:
                for n in group.rows:
                    out[n] = Placed(None, "two_for_one") if twice else Placed(ident, way)
        return out


# --------------------------------------------------------------------------------- the definitions
def _whole_reports(rows: list[dict[str, Any]], reports: set[str]) -> list[dict[str, Any]]:
    """Every row of these progress reports (``reached.report_key``), not only the given ones a page's filter
    kept (an emergency, section or search filter can keep one group of a report and drop another), in the
    order Partner monitoring reads them: one query over their PDs and period ends."""
    mine = [row for row in rows if row.get("intervention_id") and rc.report_key(row) in reports]
    ends = {row.get("period_end") for row in mine}
    when = Q(period_end__in=[end for end in ends if end is not None])
    if None in ends:
        when |= Q(period_end=None)
    found = dm.ReportedIndicator.objects.filter(
        when, intervention_id__in=sorted({row["intervention_id"] for row in mine})
    )
    return [
        row for row in found.order_by(*rc.ROW_ORDER).values(*PLACE_FIELDS) if rc.report_key(row) in reports
    ]


def definitions(rows: list[dict[str, Any]]) -> Callable[[list[dict[str, Any]]], tuple | None]:
    """For Report/Location, Report/Disaggregation and the report pop-up: the (display type, unit, target) of
    the indicator an indicator report's rows belong to, ``rows`` being every row the page reads. The
    indicator :meth:`Idents.place` gives all of them, placing whole progress reports as Partner monitoring
    does (a tie-breaker decides from every group of a report, whatever the page's filter keeps); else, for
    rows under a title and an output two or more indicators share, the definition those indicators have in
    common (none when they differ: the rows are read as numbers, as a row with no indicator is); else the
    first indicator of the PD with the title, in the order Partner monitoring reads them."""
    pd_ids = sorted({row["intervention_id"] for row in rows if row.get("intervention_id")})
    idents = Idents(pd_ids)
    # a report holding a shared title and output is placed from all its rows (a tie-breaker can place it);
    # the others row by row, as they come
    shared: set[str] = set()
    if idents.calibration.enabled:
        keys = ("intervention_id", "etools_indicator_id", "indicator", "pd_output")
        shared = {rc.report_key(r) for r in rows if idents.how(*(r.get(k) for k in keys)) == "shared_output"}
    read = [row for row in rows if not shared or rc.report_key(row) not in shared]
    read += _whole_reports(rows, shared) if shared else []
    placed = {row["datamart_id"]: p.ident for row, p in zip(read, idents.place(read), strict=True)}

    def of(group: list[dict[str, Any]]) -> tuple | None:
        first = group[0]
        pd_id = first.get("intervention_id")
        if not pd_id:
            return None
        found = {placed.get(row["datamart_id"]) for row in group}
        if len(found) == 1 and None not in found:
            return idents.definition(next(iter(found)))
        shared = idents.candidates(pd_id, first.get("pd_output"), first.get("indicator"))
        if shared:
            known = {idents.definition(c.ident) for c in shared}
            return known.pop() if len(known) == 1 else None
        titled = idents.titled(pd_id, first.get("indicator"))
        return idents.definition(titled[0]) if titled else None

    return of


# --------------------------------------------------------------------------------- the calibration
def _compare(name: str, group: Group, cand: Candidate, labels: disaggregation.Labels) -> bool | None:
    """Whether a placed group and its indicator say the same for a tie-breaker (None: one says nothing)."""
    if name == "high_frequency":
        return None if cand.high_frequency is None else group.high_frequency == cand.high_frequency
    if name == "cluster":
        value = flag(group.cluster)
        return None if value is None else value == cand.cluster
    if name == "target":
        value = group.target
        return None if value is None or cand.target is None else _same_target(value, cand.target)
    if name == "disaggregations":
        value = group.disaggregation_types(labels)
        return None if value is None else value == cand.disaggregations
    if name == "locations":
        return (
            None if group.unplaced or not group.places or not cand.planned else group.places <= cand.planned
        )
    if name == "active":
        return cand.active
    value = _agreed(group.means) if name == "means_of_verification" else _agreed(group.sections - {""})
    theirs = cand.means if name == "means_of_verification" else cand.section
    return None if not value or not theirs else value == theirs


def calibrate() -> dict[str, Any]:
    """The proof of the tie-breakers, over every partner report row of a PD NeuroDB holds (the same answer on
    every page, whatever its filter): the rows placed by id, title or output, grouped as :meth:`Idents.place`
    groups them, compared with the indicator they were placed under. A tie-breaker is enabled when it was
    compared on :data:`CALIBRATION_MIN` groups or more and never differed; the indicator type when, besides,
    each type seen goes with one (display type, unit) and no pair with two types. Counts and fixed words only
    (the indicator types), stored by the sync in its run's details (``identity``)."""
    rows = dm.ReportedIndicator.objects.exclude(intervention=None).order_by()
    pd_ids = sorted(set(rows.values_list("intervention_id", flat=True).distinct()))
    idents = Idents(pd_ids, Calibration())
    groups: dict[tuple, Group] = {}
    read = 0
    values = (
        with_signature(rows)
        .annotate(_means=KT("data__means_of_verification"))
        .values(*PLACE_FIELDS, "_cluster", "_type", "_means")
    )
    for row in values.iterator(chunk_size=CALIBRATION_CHUNK):
        read += 1
        pd, eid, title, output = (
            row["intervention_id"],
            row["etools_indicator_id"],
            row["indicator"],
            row["pd_output"],
        )
        if idents.how(pd, eid, title, output) not in PLACED_BY_KEY:
            continue
        key = (*group_key(row), idents.of(pd, eid, title, output))
        group = groups.get(key)
        if group is None:
            group = groups[key] = Group(row)
        group.add(row)
    labels = group_labels(groups.values())
    tally: dict[str, Counter[str]] = {name: Counter() for name in (*BREAKERS, *DIAGNOSTIC_ONLY)}
    pairs: Counter[tuple[str, tuple[str, str]]] = Counter()
    for key, group in groups.items():
        cand = idents.candidate(key[-1])
        for name, counts in tally.items():
            if name == "indicator_type":
                kind = norm(group.kind)
                if not kind or cand.kind is None:
                    counts["not_given"] += 1
                else:
                    counts["compared"] += 1
                    pairs[(kind, cand.kind)] += 1
                continue
            same = _compare(name, group, cand, labels)
            if same is None:
                counts["not_given"] += 1
            else:
                counts["compared"] += 1
                counts["same" if same else "differ"] += 1
    kinds: dict[str, set[tuple[str, str]]] = defaultdict(set)
    named: dict[tuple[str, str], set[str]] = defaultdict(set)
    for kind, pair in pairs:
        kinds[kind].add(pair)
        named[pair].add(kind)
    types = {
        kind: next(iter(found))
        for kind, found in kinds.items()
        if len(found) == 1 and len(named[next(iter(found))]) == 1
    }
    counts = tally["indicator_type"]
    counts["same"] = sum(n for (kind, _), n in pairs.items() if kind in types)
    counts["differ"] = counts["compared"] - counts["same"]
    enabled = [n for n in BREAKERS if tally[n]["compared"] >= CALIBRATION_MIN and not tally[n]["differ"]]
    return {
        "version": CALIBRATION_VERSION,
        "computed_at": timezone.now().isoformat(),
        "rows": read,
        "groups": len(groups),
        "enabled": enabled,
        "types": {kind: list(pair) for kind, pair in sorted(types.items()) if VOCABULARY.fullmatch(kind)},
        "stats": {
            name: {k: counts[k] for k in ("compared", "same", "differ", "not_given")}
            for name, counts in tally.items()
        },
    }
