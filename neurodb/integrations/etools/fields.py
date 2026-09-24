"""Coercion of eTools JSON values onto the legacy ``etools_*`` columns.

v2 assigned API values straight onto model attributes; Django converted most of them but a
``None`` for a NOT NULL text column, a ``''`` for a date (``sync_trip_data``) or a list of dicts
for an ``ArrayField(CharField)`` raised at save time and the item was silently dropped. These
helpers make the assignments explicit: ``None`` -> ``''``/``0`` for non-null columns, ``''`` ->
``None`` for dates, dict/list array elements JSON-encoded, anything else stringified the way the
CharField would have (``str(value)``, so a list stored in a CharField keeps its v2 repr).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime


def parse_iso_date(value: Any) -> dt.date | None:
    """``"YYYY-MM-DD"`` (or a longer ISO timestamp) -> date; empty/None -> None (v2 stored '')."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value)
    day = parse_date(text[:10])
    if day is not None:
        return day
    parsed = parse_datetime(text)
    return parsed.date() if parsed else None


def parse_iso_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    parsed = parse_datetime(str(value))
    if parsed is None:
        day = parse_date(str(value)[:10])
        parsed = dt.datetime.combine(day, dt.time.min) if day else None
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _array_element(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, default=str)
    return str(value)


def coerce(field: models.Field, value: Any) -> Any:
    """Convert ``value`` for ``field`` so that ``save()`` cannot fail on type or nullness."""
    if isinstance(field, ArrayField):
        if value is None:
            return None if field.null else []
        if isinstance(value, str):
            value = [value]
        # PostgreSQL enforces varchar(n) on each element of a varchar(n)[] column: cut like the CharField.
        limit = getattr(field.base_field, "max_length", None)
        return [_array_element(item)[:limit] if limit else _array_element(item) for item in value]
    if isinstance(field, models.JSONField):
        return value
    if isinstance(field, models.DateTimeField):
        return parse_iso_datetime(value)
    if isinstance(field, models.DateField):
        return parse_iso_date(value)
    if isinstance(field, models.BooleanField):
        return bool(value) if value is not None else (None if field.null else False)
    if isinstance(field, models.DecimalField):
        if value in (None, ""):
            return None if field.null else Decimal(0)
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None if field.null else Decimal(0)
    if isinstance(field, models.IntegerField):
        if value in (None, ""):
            return None if field.null else 0
        return int(value)
    if isinstance(field, models.CharField | models.TextField):
        if value is None:
            return None if field.null else ""
        text = value if isinstance(value, str) else str(value)
        limit = getattr(field, "max_length", None)
        return text[:limit] if limit else text
    return value


def assign(instance: models.Model, data: dict[str, Any], fields: Iterable[str] | dict[str, str]) -> None:
    """Copy ``data`` keys onto ``instance`` fields (``{model_field: api_key}`` or same-name list).

    Keys absent from ``data`` are left untouched (v2 ``if key in data`` guards).
    """
    mapping = fields if isinstance(fields, dict) else {name: name for name in fields}
    meta = instance._meta
    for field_name, key in mapping.items():
        if key not in data:
            continue
        setattr(instance, field_name, coerce(meta.get_field(field_name), data[key]))
