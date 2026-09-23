"""Load population figures from the UNICEF workbook export (the v2 JSON layout) into PopulationFigure.

v2 read ``pivoting/uploads/Population_figures_<year>_NeuroDB.json`` on every request and needed a
code change each January. v3 loads the same layout into a table once:
keys ``<NAT>_BY_GOVERNORATE`` / ``<NAT>_BY_DISTRICT`` (NAT in ALL, LEB, SYR, PAL) with one row per
area holding nationality totals, female/male totals and 5-year age bands.

The published years ship with the code in ``neurodb/core/data/population/``; ``--bundled`` loads
every bundled year that is not in the table yet (the container runs it at start, so a new
deployment or an empty database gets the figures without anyone running a command).
"""

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from neurodb.core.models import PopulationFigure, SyncRun

AGE_BAND = re.compile(r"^(\d+) - (\d+)$|^(\d+) and above$")
CHILD_BANDS = {"0 - 4", "5 - 9", "10 - 14", "15 - 19"}
TOTAL_COLUMNS = {
    "ALL": {
        "ALL": "TOTAL POPULATION",
        "LEB": "TOTAL LEBANESE",
        "SYR": "TOTAL SYRIANS",
        "OTH": "TOTAL MIGRANTS",
    },
    "LEB": {"LEB": "TOTAL LEBANESE"},
    "SYR": {"SYR": "Syrian_Est"},
    "PAL": {"PRL": "Total PRL", "PRS": "Total PRS"},
}
SEX_COLUMNS = {
    "LEB": ("All Lebanese Female", "All Lebanese Male"),
    "SYR": ("All Syrians Female", "All Syrians Male"),
    "PAL": ("All Palestinian Female", "All Palestinian Male"),
    "ALL": (None, None),
}
NAT_OF_KEY = {
    "ALL": "ALL",
    "LEB": "LEB",
    "SYR": "SYR",
    "PAL": "PRL",
}  # age bands of PAL rows are stored under PRL+PRS = PAL; keep PRL
# Governorate spellings that differ between sheets of the same workbook (2026: SYR and PAL sheets).
# Governorate level only: "Nabatieh" is also a district.
GOVERNORATE_ALIASES = {"Nabatieh": "El Nabatieh"}
BUNDLED_DIR = Path(__file__).resolve().parents[2] / "data" / "population"
BUNDLED_NAME = re.compile(r"^Population_figures_(\d{4})_NeuroDB\.json$")
UNIQUE_FIELDS = PopulationFigure._meta.unique_together[0]


class Command(BaseCommand):
    help = "Load a year's population figures from the v2-style JSON file (or every bundled year)."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", help="JSON file; omit with --bundled")
        parser.add_argument("--year", type=int, help="year of the figures in PATH")
        parser.add_argument(
            "--bundled",
            action="store_true",
            help="load the files shipped in neurodb/core/data/population/ for years not loaded yet",
        )
        parser.add_argument("--replace", action="store_true", help="delete existing rows for the year first")

    def handle(self, *args, **options):
        if options["bundled"]:
            self._load_bundled(replace=options["replace"])
            return
        if not options["path"] or not options["year"]:
            raise CommandError("give PATH and --year, or --bundled")
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"{path} not found")
        self._load(path, options["year"], replace=options["replace"], triggered_by="command")

    def _load_bundled(self, replace):
        loaded = set(PopulationFigure.objects.values_list("year", flat=True).distinct())
        for path in sorted(BUNDLED_DIR.glob("*.json")):
            match = BUNDLED_NAME.match(path.name)
            if not match:
                continue
            year = int(match.group(1))
            if year in loaded and not replace:
                self.stdout.write(f"Population figures for {year} already loaded")
                continue
            self._load(path, year, replace=True, triggered_by="bundled")

    def _load(self, path: Path, year: int, *, replace: bool, triggered_by: str):
        data = json.loads(path.read_text(encoding="utf-8"))
        run = SyncRun.objects.create(job=SyncRun.Job.POPULATION, target=str(year), triggered_by=triggered_by)
        try:
            rows = _rows(data, year)
            run.rows_in = len(rows)
            with transaction.atomic():
                if replace:
                    PopulationFigure.objects.filter(year=year).delete()
                PopulationFigure.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)
        except Exception as exc:
            run.finish(SyncRun.Status.FAILED, error=str(exc), file=path.name)
            raise
        run.rows_written = len(rows)
        run.finish(SyncRun.Status.SUCCEEDED, file=path.name)
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(rows)} population rows for {year}"))


def _rows(data: dict, year: int) -> list[PopulationFigure]:
    rows: list[PopulationFigure] = []
    sources = "; ".join(
        str(list(x.values())[0]) for k, v in data.items() if k.endswith("_SOURCES") for x in v
    )[:200]
    for key, items in data.items():
        if not key.endswith(("_BY_GOVERNORATE", "_BY_DISTRICT")):
            continue
        nat_key, level = key.split("_BY_")
        level = level.lower()
        for item in items:
            # District sheets name the area under "District" or, in some sheets, "Governorate".
            area = (item.get("District") or item.get("Governorate") or "").strip()
            if not area:
                continue
            if level == "governorate":
                area = GOVERNORATE_ALIASES.get(area, area)
            # The area code is part of the unique key; without one every area of a level collides.
            base = dict(year=year, level=level, area_code=slugify(area)[:20], area_name=area, source=sources)
            for nat, column in TOTAL_COLUMNS.get(nat_key, {}).items():
                if item.get(column) is not None:
                    rows.append(
                        PopulationFigure(**base, nationality=nat, category="total", value=int(item[column]))
                    )
            female, male = SEX_COLUMNS.get(nat_key, (None, None))
            nat = NAT_OF_KEY[nat_key]
            if female and item.get(female) is not None:
                rows.append(
                    PopulationFigure(
                        **base, nationality=nat, category="total", sex="female", value=int(item[female])
                    )
                )
                rows.append(
                    PopulationFigure(
                        **base, nationality=nat, category="total", sex="male", value=int(item[male] or 0)
                    )
                )
            children = 0
            for column, value in item.items():
                if AGE_BAND.match(column) and value is not None:
                    rows.append(
                        PopulationFigure(
                            **base,
                            nationality=nat,
                            category="total",
                            age_group=column.replace(" ", ""),
                            value=int(value),
                        )
                    )
                    if column in CHILD_BANDS:
                        children += int(value)
            if children:
                rows.append(PopulationFigure(**base, nationality=nat, category="children", value=children))
    # The same figure appears in several sheets (e.g. Lebanese totals in ALL_ and LEB_BY_...):
    # keep the first so national totals below are not counted twice.
    unique: dict[tuple, PopulationFigure] = {}
    for r in rows:
        unique.setdefault(tuple(getattr(r, f) for f in UNIQUE_FIELDS), r)
    rows = list(unique.values())
    # national totals = sum over governorates
    national: dict[tuple, int] = {}
    for r in rows:
        if r.level == "governorate":
            k = (r.nationality, r.category, r.age_group, r.sex)
            national[k] = national.get(k, 0) + r.value
    for (nat, cat, age, sex), value in national.items():
        rows.append(
            PopulationFigure(
                year=year,
                level="national",
                area_code="lebanon",
                area_name="Lebanon",
                nationality=nat,
                category=cat,
                age_group=age,
                sex=sex,
                value=value,
                source=sources,
            )
        )
    return rows
