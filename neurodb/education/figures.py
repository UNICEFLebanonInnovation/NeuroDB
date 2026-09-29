"""Reading the education counts Compiler sent (Compiler's figures/education.py).

A payload holds blocks (registrations, services, education programmes, sites, staff, attendance),
each with the names of its measures (unique children, or attendance days) and one table per
grouping. A count of unique children is never added up across rows: a question is answered from the
grouping that holds exactly its filters and its breakdown.

Format 2 adds cubes (records counted per combination of the dashboards' slicers, see cube.py), the
centers (Makani) or schools (Bridging) with their place, type, emergency status and GPS point, the
lists that name the cubes' ids, and the Kobo outreach cubes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from .cube import Cube

AGE_ORDER = ("Under 6", "6-9", "10-14", "15-17", "18 and over", "Not specified")
LABELS = {
    "in_progress": "In progress",
    "dropout": "Dropped out",
    "graduated_to_Bridging_next_level": "Graduated to the next level",
    "graduated_to_Bridging_next_round_same_level": "Next round, same level",
    "referred_public_school": "Referred to a public school",
    "other": "Referred to another pathway",
}


@dataclass
class Block:
    data: dict[str, Any]

    @property
    def measures(self) -> list[str]:
        return self.data.get("measures", ["people"])

    @property
    def error(self) -> str:
        return self.data.get("error", "")

    @cached_property
    def _groupings(self) -> dict[tuple[str, ...], list[list[Any]]]:
        return {tuple(g["by"]): g["rows"] for g in self.data.get("figures", [])}

    def has(self, *names: str) -> bool:
        return tuple(sorted(names)) in self._groupings

    def table(self, filters: dict[str, Any], *by: str) -> dict[tuple, dict[str, Any]] | None:
        """Measures per value of ``by`` among the rows matching ``filters``; None when Compiler sends no
        grouping for that question."""
        filters = {k: v for k, v in filters.items() if v is not None}
        grouping = tuple(sorted(set(filters) | set(by)))
        rows = self._groupings.get(grouping)
        if rows is None:
            return None
        width = len(grouping)
        where = [(grouping.index(k), v) for k, v in filters.items()]
        wanted = [grouping.index(d) for d in by]
        out = {}
        for row in rows:
            if all(row[i] == v for i, v in where):
                out[tuple(row[i] for i in wanted)] = dict(zip(self.measures, row[width:], strict=False))
        return out

    def total(self, filters: dict[str, Any]) -> dict[str, Any]:
        return (self.table(filters) or {}).get((), {})


@dataclass
class Figures:
    payload: dict[str, Any]

    @property
    def label(self) -> str:
        return self.payload.get("label") or self.payload.get("programme", "")

    def block(self, name: str) -> Block:
        return Block(self.payload.get("blocks", {}).get(name, {}))

    @property
    def has_cubes(self) -> bool:
        """False for a payload of format 1 (sent by a Compiler without the cubes)."""
        return isinstance(self.payload.get("cubes"), dict)

    def cube(self, name: str) -> Cube:
        return Cube((self.payload.get("cubes") or {}).get(name))

    def outreach(self, name: str) -> Cube:
        return Cube(((self.payload.get("outreach") or {}).get("cubes") or {}).get(name))

    def _index(self, key: str) -> dict[Any, dict[str, Any]]:
        return {item["id"]: item for item in self.payload.get(key, [])}

    @cached_property
    def partners(self):
        return self._index("partners")

    @cached_property
    def locations(self):
        return self._index("locations")

    @cached_property
    def sites(self):
        return self._index("sites")

    @cached_property
    def nationalities(self):
        return self._index("nationalities")

    @cached_property
    def disabilities(self):
        return self._index("disabilities")

    @cached_property
    def rounds(self):
        return self._index("rounds")

    @cached_property
    def centers(self):
        return {**self.sites, **self._index("centers")}

    @cached_property
    def schools(self):
        return {**self.sites, **self._index("schools")}

    @cached_property
    def id_types(self):
        return self._index("id_types")

    @cached_property
    def trainings(self):
        return self._index("trainings")

    def label_of(self, dimension: str, value: Any) -> str:
        if value is None or value == "":
            return "Not specified"
        lookups = {
            "partner": self.partners,
            "governorate": self.locations,
            "district": self.locations,
            "cadaster": self.locations,
            "center": self.centers,
            "school": self.schools,
            "nationality": self.nationalities,
            "disability": self.disabilities,
            "round": self.rounds,
            "id_type": self.id_types,
            "training": self.trainings,
        }
        if dimension in lookups:
            item = lookups[dimension].get(value)
            if item is None:
                return f"#{value}"
            if dimension == "partner":
                return item.get("short_name") or item["name"]
            return item["name"]
        text = str(value)
        if dimension == "registration_level":
            return text.replace("_", " ").capitalize()
        return LABELS.get(text, text)
