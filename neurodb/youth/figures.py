"""Reading the youth figures Compiler sent (see Compiler's youth/indicator_figures.py).

The figures are counts of unique young people for a fixed set of groupings: every combination of
master indicator, partner, donor and governorate (the "main" dimensions), each alone or with one
more detail (sub indicator, programme document, district, sex, age group or nationality), plus the
sub indicators of each programme document. A count is
never added up across rows: the same young person can be in two partners' programmes. A question is
answered from the one grouping that holds exactly its filters and its breakdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any

MAIN = ("master", "partner", "donor", "governorate")
DETAILS = ("sub", "program_document", "district", "sex", "age_group", "nationality")
AGE_ORDER = ("Under 15", "15-17", "18-24", "25 and over", "Not specified")


@dataclass
class Figures:
    payload: dict[str, Any]

    @property
    def year(self) -> str:
        return str(self.payload.get("year", ""))

    @cached_property
    def _groupings(self) -> dict[tuple[str, ...], list[list[Any]]]:
        return {tuple(g["by"]): g["rows"] for g in self.payload.get("figures", [])}

    def _index(self, key: str) -> dict[int, dict[str, Any]]:
        return {item["id"]: item for item in self.payload.get(key, [])}

    @cached_property
    def indicators(self) -> dict[tuple[str, int], dict[str, Any]]:
        return {(i["level"], i["id"]): i for i in self.payload.get("indicators", [])}

    @cached_property
    def partners(self) -> dict[int, dict[str, Any]]:
        return self._index("partners")

    @cached_property
    def donors(self) -> dict[int, dict[str, Any]]:
        return self._index("donors")

    @cached_property
    def locations(self) -> dict[int, dict[str, Any]]:
        return self._index("locations")

    @cached_property
    def nationalities(self) -> dict[int, dict[str, Any]]:
        return self._index("nationalities")

    @cached_property
    def program_documents(self) -> dict[int, dict[str, Any]]:
        return self._index("program_documents")

    @property
    def targets(self) -> list[dict[str, Any]]:
        return self.payload.get("targets", [])

    def masters(self) -> list[dict[str, Any]]:
        return [i for (level, _), i in self.indicators.items() if level == "master"]

    def subs_of(self, master_id: int) -> list[dict[str, Any]]:
        return [i for (level, _), i in self.indicators.items() if level == "sub" and i["master"] == master_id]

    def table(self, filters: dict[str, int], *by: str) -> dict[tuple, int] | None:
        """Youth per value of ``by`` among those matching ``filters`` (main dimensions only).

        None when Compiler sends no grouping for that question (two details at once)."""
        filters = {k: v for k, v in filters.items() if v is not None}
        grouping = tuple(sorted(set(filters) | set(by)))
        rows = self._groupings.get(grouping)
        if rows is None:
            return None
        where = [(grouping.index(k), v) for k, v in filters.items()]
        wanted = [grouping.index(d) for d in by]
        out: dict[tuple, int] = {}
        for row in rows:
            if all(row[i] == v for i, v in where):
                out[tuple(row[i] for i in wanted)] = row[-1]
        return out

    def count(self, filters: dict[str, int]) -> int:
        return (self.table(filters) or {}).get((), 0)

    # -------------------------------------------------------------------------- labels
    def label(self, dimension: str, value: Any) -> str:
        if value is None:
            return "Not specified"
        if dimension in ("master", "sub"):
            item = self.indicators.get((dimension, value))
            return indicator_label(item) if item else f"#{value}"
        if dimension == "partner":
            item = self.partners.get(value)
            return (item.get("short_name") or item["name"]) if item else f"#{value}"
        if dimension == "donor":
            item = self.donors.get(value)
            return item["name"] if item else f"#{value}"
        if dimension in ("governorate", "district"):
            item = self.locations.get(value)
            return item["name"] if item else f"#{value}"
        if dimension == "nationality":
            item = self.nationalities.get(value)
            return item["name"] if item else f"#{value}"
        if dimension == "program_document":
            item = self.program_documents.get(value)
            return (item["project_code"] or item["project_name"] or f"#{value}") if item else f"#{value}"
        return str(value)


def indicator_label(item: dict[str, Any]) -> str:
    number = (item.get("number") or "").strip()
    return f"{number} {item['name']}".strip() if number and number not in item["name"] else item["name"]
