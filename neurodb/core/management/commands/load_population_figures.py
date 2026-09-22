"""Load population figures from the UNICEF workbook export (the v2 JSON layout) into PopulationFigure.

v2 read ``pivoting/uploads/Population_figures_<year>_NeuroDB.json`` on every request and needed a
code change each January. v3 loads the same layout into a table once:
keys ``<NAT>_BY_GOVERNORATE`` / ``<NAT>_BY_DISTRICT`` (NAT in ALL, LEB, SYR, PAL) with one row per
area holding nationality totals, female/male totals and 5-year age bands.
"""

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from neurodb.core.models import PopulationFigure, SyncRun

AGE_BAND = re.compile(r"^(\d+) - (\d+)$|^(\d+) and above$")
CHILD_BANDS = {"0 - 4", "5 - 9", "10 - 14", "15 - 19"}
TOTAL_COLUMNS = {
    "ALL": {"ALL": "TOTAL POPULATION", "LEB": "TOTAL LEBANESE", "SYR": "TOTAL SYRIANS", "OTH": "TOTAL MIGRANTS"},
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
NAT_OF_KEY = {"ALL": "ALL", "LEB": "LEB", "SYR": "SYR", "PAL": "PRL"}  # age bands of PAL rows are stored under PRL+PRS = PAL; keep PRL


class Command(BaseCommand):
    help = "Load a year's population figures from the v2-style JSON file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--replace", action="store_true", help="delete existing rows for the year first")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"{path} not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        year = options["year"]
        run = SyncRun.objects.create(job=SyncRun.Job.POPULATION, target=str(year), triggered_by="command")
        rows: list[PopulationFigure] = []
        sources = "; ".join(str(list(x.values())[0]) for k, v in data.items() if k.endswith("_SOURCES") for x in v)[:200]
        for key, items in data.items():
            if not key.endswith(("_BY_GOVERNORATE", "_BY_DISTRICT")):
                continue
            nat_key, level = key.split("_BY_")
            level = level.lower()
            for item in items:
                area = item.get("Governorate") or item.get("District") or ""
                if not area:
                    continue
                base = dict(year=year, level=level, area_code="", area_name=area, source=sources)
                for nat, column in TOTAL_COLUMNS.get(nat_key, {}).items():
                    if item.get(column) is not None:
                        rows.append(PopulationFigure(**base, nationality=nat, category="total", value=int(item[column])))
                female, male = SEX_COLUMNS.get(nat_key, (None, None))
                nat = NAT_OF_KEY[nat_key]
                if female and item.get(female) is not None:
                    rows.append(PopulationFigure(**base, nationality=nat, category="total", sex="female", value=int(item[female])))
                    rows.append(PopulationFigure(**base, nationality=nat, category="total", sex="male", value=int(item[male] or 0)))
                children = 0
                for column, value in item.items():
                    if AGE_BAND.match(column) and value is not None:
                        rows.append(PopulationFigure(**base, nationality=nat, category="total", age_group=column.replace(" ", ""), value=int(value)))
                        if column in CHILD_BANDS:
                            children += int(value)
                if children:
                    rows.append(PopulationFigure(**base, nationality=nat, category="children", value=children))
        # national totals = sum over governorates
        national = {}
        for r in rows:
            if r.level == "governorate":
                k = (r.nationality, r.category, r.age_group, r.sex)
                national[k] = national.get(k, 0) + r.value
        for (nat, cat, age, sex), value in national.items():
            rows.append(PopulationFigure(year=year, level="national", area_code="", area_name="Lebanon", nationality=nat, category=cat, age_group=age, sex=sex, value=value, source=sources))
        run.rows_in = len(rows)
        with transaction.atomic():
            if options["replace"]:
                PopulationFigure.objects.filter(year=year).delete()
            PopulationFigure.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)
        run.rows_written = len(rows)
        run.finish(SyncRun.Status.SUCCEEDED, file=str(path))
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(rows)} population rows for {year}"))
