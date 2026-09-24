"""Read-only queries over every eTools Datamart dataset, for the AI assistant.

Each dataset of ``catalogue`` is either a table of its own or ``DatamartDocument`` rows; both keep the
whole record in ``data``, so one query language covers all of them: filters on record fields,
full-text search, date range, partner and programme document (through the links made at sync time),
grouping with counts and sums, and a field selection. Contact details are removed from every
output and long values are shortened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.apps import apps
from django.db.models import Count, Model, Q, QuerySet, Sum, TextField
from django.db.models.expressions import RawSQL
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, ExtractMonth, ExtractYear
from django.urls import reverse

from neurodb.partnerships.models import PCA, PartnerOrganization

from . import catalogue
from .models import DatamartDocument

MAX_ROWS = 50
MAX_GROUPS = 50
MAX_TEXT = 400
MAX_LIST = 15
MAX_OUTPUT_CHARS = 40_000  # one answer's worth of records; the model is told to narrow or pick fields
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
OPERATORS = ("eq", "ne", "contains", "gt", "gte", "lt", "lte", "isnull")
NOISE = {"id", "seen", "last_modify_date", "country_name", "schema_name", "area_code"}
NUMBER = r"^\s*-?[0-9]+(\.[0-9]+)?\s*$"


class QueryError(ValueError):
    """A question the data cannot answer as asked (unknown dataset or field, bad filter)."""


@dataclass
class Dataset:
    name: str
    description: str
    model: type[Model]
    date_field: str
    base: QuerySet
    spec: catalogue.Source | None = None  # for DatamartDocument datasets: which links the sync makes

    @property
    def partner_link(self) -> bool:
        if self.spec is not None:  # the partner comes from the record or from its programme document
            return any(self.spec.partner) or any(self.spec.intervention)
        return _has_field(self.model, "partner")

    @property
    def pd_link(self) -> str:
        if self.spec is not None:
            return "intervention" if any(self.spec.intervention) else ""
        if _has_field(self.model, "intervention"):
            return "intervention"
        if _has_field(self.model, "interventions"):
            return "interventions"
        return ""


def _has_field(model: type[Model], name: str) -> bool:
    return any(f.name == name for f in model._meta.get_fields())


def dataset(name: str) -> Dataset:
    if name in catalogue.TYPED:
        model_name, description, date_field = catalogue.TYPED[name]
        model = apps.get_model("datamart", model_name)
        return Dataset(name, description, model, date_field, model.objects.all())
    if name in catalogue.DOCUMENTS:
        spec = catalogue.DOCUMENTS[name]
        base = DatamartDocument.objects.filter(dataset=name)
        return Dataset(name, spec.description, DatamartDocument, "date" if spec.date else "", base, spec)
    raise QueryError(f"Unknown dataset '{name}'. Use etools_datasets to list them.")


# ------------------------------------------------------------------------------------ outputs
def shorten(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    if isinstance(value, list):
        items = [shorten(v) for v in value[:MAX_LIST]]
        return items + [f"… {len(value) - MAX_LIST} more"] if len(value) > MAX_LIST else items
    if isinstance(value, dict):
        return {k: shorten(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    return value


def public(data: dict[str, Any], fields: list[str] | None = None) -> dict[str, Any]:
    data = catalogue.scrub(data or {})
    if fields:
        return {f: shorten(data.get(f)) for f in fields}
    return {k: shorten(v) for k, v in data.items() if k not in NOISE and v not in (None, "", [], {})}


def _links(row: Model) -> dict[str, Any]:
    out: dict[str, Any] = {}
    partner = getattr(row, "partner", None)
    if partner is not None:
        out["neurodb_partner"] = {
            "id": partner.pk,
            "name": partner.name,
            "url": reverse("reports:partner_profile", args=[partner.pk]),
        }
    pd = getattr(row, "intervention", None)
    if pd is not None:
        out["neurodb_programme_document"] = {
            "number": pd.number,
            "url": reverse("reports:programme_detail", args=[pd.pk]),
        }
    return out


# ------------------------------------------------------------------------------------ catalogue
def describe(name: str | None = None) -> dict[str, Any]:
    """Without a name, every dataset with its description and size; with one, its fields and examples."""
    if not name:
        rows = []
        for key in catalogue.dataset_names():
            ds = dataset(key)
            rows.append(
                {
                    "dataset": key,
                    "description": ds.description,
                    "records": ds.base.count(),
                    "linked_to": [
                        x
                        for x, ok in (("partner", ds.partner_link), ("programme_document", ds.pd_link))
                        if ok
                    ],
                }
            )
        return {"datasets": rows, "not_read": catalogue.EXCLUDED}
    ds = dataset(name)
    fields: dict[str, Any] = {}
    for data in ds.base.order_by("-pk").values_list("data", flat=True)[:25]:
        for key, value in catalogue.scrub(data or {}).items():
            if key in NOISE or key in fields and fields[key] not in (None, "", [], {}):
                continue
            fields[key] = shorten(value) if not isinstance(value, str) else value[:80]
    return {
        "dataset": name,
        "description": ds.description,
        "records": ds.base.count(),
        "date_field": ds.date_field or None,
        "filter_by_partner": ds.partner_link,
        "filter_by_programme_document": bool(ds.pd_link),
        "fields_with_example_values": fields,
    }


# ------------------------------------------------------------------------------------ filtering
def _field(name: str) -> str:
    if not FIELD.match(name or ""):
        raise QueryError(f"Invalid field name '{name}'.")
    return name


def _numeric(ds: Dataset, field: str) -> RawSQL:
    """The field as a number where it holds one (the Datamart sends decimals as strings)."""
    column = f'"{ds.model._meta.db_table}"."data"'
    # The table name comes from the model; the field name and the pattern are bound parameters.
    return RawSQL(  # noqa: S611
        f"CASE WHEN ({column}->>%s) ~ %s THEN ({column}->>%s)::numeric END",
        (field, NUMBER, field),
    )


def _filter(ds: Dataset, qs: QuerySet, key: str, value: Any) -> QuerySet:
    field, _, op = key.partition("__")
    field, op = _field(field), op or "eq"
    if op not in OPERATORS:
        raise QueryError(f"Unknown operator '{op}' in '{key}'. Use one of {', '.join(OPERATORS)}.")
    text = KT(f"data__{field}")
    if op == "isnull":
        empty = Q(**{f"data__{field}__isnull": True}) | Q(**{f"data__{field}": ""})
        return qs.filter(empty) if value else qs.exclude(empty)
    if op in ("gt", "gte", "lt", "lte"):
        if isinstance(value, int | float) and not isinstance(value, bool):
            alias = f"_n_{field}"
            return qs.annotate(**{alias: _numeric(ds, field)}).filter(**{f"{alias}__{op}": value})
        return qs.annotate(**{f"_t_{field}": text}).filter(**{f"_t_{field}__{op}": str(value)})
    if op == "contains":
        return qs.annotate(**{f"_t_{field}": text}).filter(**{f"_t_{field}__icontains": str(value)})
    if isinstance(value, bool):
        match = Q(**{f"data__{field}": value})
    else:
        match = Q(**{f"_t_{field}__iexact": str(value)})
        qs = qs.annotate(**{f"_t_{field}": text})
    return qs.exclude(match) if op == "ne" else qs.filter(match)


def partners_matching(text: str) -> list[int]:
    text = text.strip()
    return list(
        PartnerOrganization.objects.filter(
            Q(name__icontains=text) | Q(short_name__icontains=text) | Q(vendor_number__iexact=text)
        ).values_list("pk", flat=True)[:50]
    )


def pds_matching(text: str) -> list[int]:
    return list(PCA.objects.filter(number__icontains=text.strip()).values_list("pk", flat=True)[:50])


def filtered(
    ds: Dataset,
    *,
    partner: str | None = None,
    programme_document: str | None = None,
    search: str | None = None,
    filters: dict[str, Any] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> QuerySet:
    qs = ds.base
    if partner:
        if not ds.partner_link:
            raise QueryError(f"'{ds.name}' is not linked to partners; filter on a partner field instead.")
        ids = partners_matching(partner)
        if not ids:
            raise QueryError(f"No partner matches '{partner}'.")
        qs = qs.filter(partner_id__in=ids)
    if programme_document:
        if not ds.pd_link:
            raise QueryError(
                f"'{ds.name}' is not linked to programme documents; filter on a PD field instead."
            )
        ids = pds_matching(programme_document)
        if not ids:
            raise QueryError(f"No programme document numbered like '{programme_document}'.")
        qs = qs.filter(**{f"{ds.pd_link}__in": ids}).distinct()
    if search:
        qs = qs.annotate(_text=Cast("data", TextField())).filter(_text__icontains=search.strip())
    for key, value in (filters or {}).items():
        qs = _filter(ds, qs, key, value)
    if date_from or date_to:
        if not ds.date_field:
            raise QueryError(f"'{ds.name}' has no main date; filter on a date field with gte/lte instead.")
        if date_from:
            qs = qs.filter(**{f"{ds.date_field}__gte": date_from})
        if date_to:
            qs = qs.filter(**{f"{ds.date_field}__lte": date_to})
    return qs


# ------------------------------------------------------------------------------------ queries
GROUPS = {"partner", "programme_document", "year", "month"}


def query(
    name: str,
    *,
    fields: list[str] | None = None,
    group_by: str | None = None,
    sum_field: str | None = None,
    order_by: str | None = None,
    limit: int = 20,
    **criteria: Any,
) -> dict[str, Any]:
    ds = dataset(name)
    qs = filtered(ds, **criteria)
    total = qs.count()
    if group_by:
        return {"dataset": name, "matching_records": total, **_groups(ds, qs, group_by, sum_field)}
    if order_by:
        descending, key = order_by.startswith("-"), order_by.lstrip("-")
        if key == "date" and ds.date_field:
            expr = ds.date_field
        else:
            qs = qs.annotate(_order=KT(f"data__{_field(key)}"))
            expr = "_order"
        qs = qs.order_by(f"-{expr}" if descending else expr, "-pk")
    else:
        qs = qs.order_by(f"-{ds.date_field}" if ds.date_field else "-pk", "-pk")
    related = [f for f in ("partner", "intervention") if _has_field(ds.model, f)]
    limit = max(1, min(int(limit or 20), MAX_ROWS))
    rows, size = [], 0
    selected = [_field(f) for f in fields or []] or None
    for row in qs.select_related(*related)[:limit]:
        item = {"record": _key(row), **_links(row), **public(row.data, selected)}
        size += len(json.dumps(item, default=str))
        if rows and size > MAX_OUTPUT_CHARS:
            break
        rows.append(item)
    out = {"dataset": name, "matching_records": total, "rows": rows}
    if total > len(rows):
        out["note"] = (
            f"Showing {len(rows)} of {total}; narrow the query, choose fields, or group the records."
        )
    if sum_field:
        out["sum"] = {sum_field: _sum(ds, qs, sum_field)}
    return out


def _key(row: Model) -> str:
    return row.record_key if isinstance(row, DatamartDocument) else str(row.datamart_id)


def _sum(ds: Dataset, qs: QuerySet, field: str) -> float | None:
    value = qs.order_by().aggregate(total=Sum(_numeric(ds, _field(field))))["total"]
    return float(value) if value is not None else None


def _groups(ds: Dataset, qs: QuerySet, group_by: str, sum_field: str | None) -> dict[str, Any]:
    if group_by == "partner":
        if not ds.partner_link:
            raise QueryError(f"'{ds.name}' is not linked to partners; group by a partner field instead.")
        qs, label = qs.annotate(_g=Cast("partner__name", TextField())), "partner"
    elif group_by == "programme_document":
        if not ds.pd_link:
            raise QueryError(f"'{ds.name}' is not linked to programme documents.")
        qs, label = qs.annotate(_g=Cast(f"{ds.pd_link}__number", TextField())), "programme_document"
    elif group_by in ("year", "month"):
        if not ds.date_field:
            raise QueryError(f"'{ds.name}' has no main date to group by.")
        extract = ExtractYear(ds.date_field) if group_by == "year" else ExtractMonth(ds.date_field)
        qs, label = qs.annotate(_g=extract), group_by
    else:
        qs, label = qs.annotate(_g=KT(f"data__{_field(group_by)}")), group_by
    annotations: dict[str, Any] = {"records": Count("pk", distinct=True)}
    if sum_field:
        annotations["total"] = Sum(_numeric(ds, _field(sum_field)))
    groups = list(qs.order_by().values("_g").annotate(**annotations).order_by("-records")[: MAX_GROUPS + 1])
    rows = [
        {
            label: g["_g"],
            "records": g["records"],
            **({f"sum_{sum_field}": shorten(g["total"])} if sum_field else {}),
        }
        for g in groups[:MAX_GROUPS]
    ]
    out: dict[str, Any] = {"groups": rows}
    if len(groups) > MAX_GROUPS:
        out["note"] = f"Only the {MAX_GROUPS} largest groups are listed."
    return out


def search_all(text: str) -> dict[str, Any]:
    """Which datasets mention a word or number (a name, a reference, a place), with a few titles each."""
    text = (text or "").strip()
    if len(text) < 3:
        raise QueryError("Search for at least 3 characters.")
    hits = []
    for name in catalogue.dataset_names():
        ds = dataset(name)
        qs = ds.base.annotate(_text=Cast("data", TextField())).filter(_text__icontains=text)
        count = qs.count()
        if count:
            examples = [str(row) for row in qs.order_by("-pk")[:3]]
            hits.append({"dataset": name, "records": count, "examples": examples})
    hits.sort(key=lambda h: -h["records"])
    return {"text": text, "datasets": hits}


def record(name: str, key: str) -> dict[str, Any]:
    ds = dataset(name)
    related = [f for f in ("partner", "intervention") if _has_field(ds.model, f)]
    qs = ds.base.select_related(*related)
    if ds.model is DatamartDocument:
        row = qs.filter(record_key=str(key)).first()
    else:
        row = qs.filter(datamart_id=int(key)).first() if str(key).isdigit() else None
    if row is None:
        raise QueryError(f"No record '{key}' in '{name}'.")
    return {"dataset": name, "record": _key(row), **_links(row), "data": public(row.data)}


def linked_counts(*, partner: PartnerOrganization | None = None, pd: PCA | None = None) -> dict[str, int]:
    """How many records of each dataset are linked to a partner or a programme document."""
    counts: dict[str, int] = {}
    for name in catalogue.dataset_names():
        ds = dataset(name)
        if partner is not None and ds.partner_link:
            n = ds.base.filter(partner=partner).count()
        elif pd is not None and ds.pd_link:
            n = ds.base.filter(**{ds.pd_link: pd}).count()
        else:
            continue
        if n:
            counts[name] = n
    return counts
