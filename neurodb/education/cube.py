"""Reading a cube Compiler sends (payload format 2): records counted per combination of categories.

``{"dims": ["center", "sex", ...], "measures": ["registrations", ...], "rows": [[12, "Female", ..., 3]]}``:
each row is one combination of the dimensions' values (ids, category names or null) followed by its
measures. Unlike the blocks of unique children (figures.py), a cube's counts add up: any combination
of slicers is answered by keeping the matching rows and summing them. A row is never a person.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MANY = (set, frozenset, list, tuple)


class Cube:
    __slots__ = ("dims", "measures", "rows", "error")

    def __init__(self, data: dict[str, Any] | None = None):
        data = data or {}
        self.dims: list[str] = list(data.get("dims") or [])
        self.measures: list[str] = list(data.get("measures") or [])
        self.rows: list[list[Any]] = data.get("rows") or []  # read only: never changed in place
        self.error: str = data.get("error") or ""

    def __len__(self) -> int:
        return len(self.rows)

    def _with(self, rows: list[list[Any]]) -> Cube:
        cube = Cube()
        cube.dims, cube.measures, cube.error, cube.rows = self.dims, self.measures, self.error, rows
        return cube

    def filter(self, **where: Any) -> Cube:
        """The rows whose ``dim`` equals the value given (``None``: unknown), or is one of them when a
        set, list or tuple is given. A dimension the cube does not have matches no row."""
        tests: list[Callable[[list[Any]], bool]] = []
        for dim, value in where.items():
            if dim not in self.dims:
                return self._with([])
            at = self.dims.index(dim)
            if isinstance(value, MANY):
                allowed = frozenset(value)
                tests.append(lambda row, at=at, allowed=allowed: row[at] in allowed)
            else:
                tests.append(lambda row, at=at, value=value: row[at] == value)
        if not tests:
            return self
        return self._with([row for row in self.rows if all(test(row) for test in tests)])

    def _measure_at(self, measure: str) -> int | None:
        if measure not in self.measures:
            return None
        return len(self.dims) + self.measures.index(measure)

    def sum(self, measure: str) -> int:
        """The measure over the rows (0 for a measure the cube does not have)."""
        at = self._measure_at(measure)
        if at is None:
            return 0
        return sum(row[at] or 0 for row in self.rows)

    def by(self, dim: str, measure: str) -> dict[Any, int]:
        """The measure per value of ``dim`` (``None``: unknown); values adding up to 0 are left out."""
        at = self._measure_at(measure)
        if at is None or dim not in self.dims:
            return {}
        key = self.dims.index(dim)
        out: dict[Any, int] = {}
        for row in self.rows:
            out[row[key]] = out.get(row[key], 0) + (row[at] or 0)
        return {value: total for value, total in out.items() if total}

    def distinct(self, dim: str, measure: str | None = None) -> set[Any]:
        """The known values of ``dim`` on rows where ``measure`` is above 0 (any row without a measure)."""
        if dim not in self.dims:
            return set()
        key = self.dims.index(dim)
        at = self._measure_at(measure) if measure else None
        if measure and at is None:
            return set()
        return {row[key] for row in self.rows if row[key] is not None and (at is None or (row[at] or 0) > 0)}

    def values(self, dim: str) -> set[Any]:
        """Every value of ``dim`` found in the rows, ``None`` included (a slicer's options)."""
        if dim not in self.dims:
            return set()
        key = self.dims.index(dim)
        return {row[key] for row in self.rows}
