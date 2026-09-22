"""Generate unmanaged Django models that map onto the existing NeuroDB (v2) tables.

Usage: python scripts/gen_legacy_models.py legacy_schema.json
The JSON is produced from the v2 codebase (see docs/DATA_MIGRATION.md). Every table,
column and many-to-many through table keeps its v2 name so the production database is
used as-is. Regenerate rather than editing the output files by hand.
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "neurodb"

APP_OF = {
    "users.Section": "accounts",
    "users.Office": "accounts",
    "users.User": "accounts",
    "pivoting.ReportingYear": "indicators",
    "pivoting.Database": "indicators",
    "pivoting.Activity": "indicators",
    "pivoting.IndicatorNew": "indicators",
    "pivoting.SubIndicator": "indicators",
    "pivoting.MasterIndicatorTag": "indicators",
    "pivoting.MasterIndicator": "indicators",
    "pivoting.MasterSubIndicator": "indicators",
    "pivoting.NeuroReport": "indicators",
    "pivoting.NeuroReportMasterIndicator": "indicators",
    "pivoting.NeuroReportComment": "indicators",
    "pivoting.ActivityReportNew": "facts",
    "pivoting.CadasterLocation": "geo",
    "pivoting.DistrictLocation": "geo",
    "pivoting.GovernorateLocation": "geo",
    "pivoting.SimpleLocation": "geo",
    "locations.LocationType": "geo",
    "locations.Location": "geo",
    "pivoting.ResourceType": "library",
    "pivoting.ResourceTopic": "library",
    "pivoting.ResourceTag": "library",
    "pivoting.Resource": "library",
    "pivoting.Map": "library",
    "etools.PartnerOrganization": "partnerships",
    "etools.PartnerStaffMember": "partnerships",
    "etools.Agreement": "partnerships",
    "etools.PCA": "partnerships",
    "etools.Travel": "partnerships",
    "etools.TravelActivity": "partnerships",
    "etools.Engagement": "partnerships",
    "etools.Category": "partnerships",
    "etools.ActionPoint": "partnerships",
    "etools.DonorFunding": "partnerships",
    "auth.Group": "auth",
    "auth.Permission": "auth",
}
DROP = {
    "pivoting.AddSubIndicatorsWizard",
    "pivoting.AddMasterIndicatorsWizard",
    "pivoting.Cadasters",
    "etools.ItineraryItem",
    "etools.TravelAttachment",
    "etools.Finding",
    "etools.DetailedFindingInfo",
    "etools.FinancialFinding",
    "locations.LocationsMasterList",
}
ABSTRACT_USER_FIELDS = {
    "id",
    "password",
    "last_login",
    "is_superuser",
    "username",
    "first_name",
    "last_name",
    "email",
    "is_staff",
    "is_active",
    "date_joined",
    "groups",
    "user_permissions",
}
PATH_MAP = {
    "model_utils.fields.AutoCreatedField": "models.DateTimeField",
    "model_utils.fields.AutoLastModifiedField": "models.DateTimeField",
    "mptt.fields.TreeForeignKey": "models.ForeignKey",
    "django.contrib.postgres.fields.ArrayField": "ArrayField",
    "django.contrib.postgres.fields.array.ArrayField": "ArrayField",
}
DROP_KWARGS = {"serialize", "auto_created", "validators", "error_messages"}


def new_label(label):
    """Relations reference the v2 app label (kept so content types, permissions and migration history stay valid)."""
    return label


def py(v):
    if isinstance(v, str):
        m = re.match(r"<function (\w+) at", v)
        if m:
            return {
                "CASCADE": "models.CASCADE",
                "PROTECT": "models.PROTECT",
                "SET_NULL": "models.SET_NULL",
                "now": "timezone.now",
            }.get(m.group(1), None)
        if v == "<class 'dict'>":
            return "dict"
        if v == "<class 'list'>":
            return "list"
        if re.match(r"<(class|function|django|bound|object)", v):
            return None
        if v.startswith("[(") and v.endswith(")]"):  # model_utils Choices were dumped as their repr
            try:
                return py([tuple(x) for x in ast.literal_eval(v)])
            except (ValueError, SyntaxError):
                pass
        if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
            v = v[1:-1]  # lazy translation strings were repr'd
        return repr(v)
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "(" + ", ".join(py(x) for x in v) + ("," if len(v) == 1 else "") + ")"
    return repr(v)


def render_field(f, model_label):
    path = PATH_MAP.get(f["path"], f["path"].replace("django.db.models.", "models."))
    kw = dict(f["kwargs"])
    if f["path"] == "model_utils.fields.AutoCreatedField":
        kw = {
            "default": "<function now at 0>",
            "editable": False,
            "verbose_name": str(kw.get("verbose_name", f["name"])).strip("'"),
        }
    if f["path"] == "model_utils.fields.AutoLastModifiedField":
        kw = {
            "auto_now": True,
            "editable": False,
            "verbose_name": str(kw.get("verbose_name", f["name"])).strip("'"),
        }
    if (
        f["path"].endswith("FileField")
        and isinstance(kw.get("upload_to"), str)
        and kw["upload_to"].startswith("<")
    ):
        kw["upload_to"] = "legacy/"
    parts = []
    if f["kind"] == "fk":
        parts.append(repr(new_label(f["to"])))
        kw.pop("to", None)
    if "base_field" in kw:
        b = kw.pop("base_field")
        bpath = b["path"].replace("django.db.models.", "models.")
        bkw = ", ".join(f"{k}={py(v)}" for k, v in b["kwargs"].items() if py(v) is not None)
        parts.append(f"{bpath}({bkw})")
        if kw.get("size") is None:
            kw.pop("size", None)
    for k, v in kw.items():
        if k in DROP_KWARGS:
            continue
        if k == "primary_key" and not v:
            continue
        r = py(v)
        if r is None:
            continue
        parts.append(f"{k}={r}")
    return f"    {f['name']} = {path}({', '.join(parts)})"


def render_m2m(f):
    parts = [repr(new_label(f["to"])), f"db_table={f['db_table']!r}"]
    if f.get("related_name"):
        parts.append(f"related_name={f['related_name']!r}")
    if f.get("blank"):
        parts.append("blank=True")
    parts.append(f"verbose_name={f['verbose_name']!r}")
    return f"    {f['name']} = models.ManyToManyField({', '.join(parts)})"


def render_model(m):
    is_user = m["label"] == "users.User"
    base = "AbstractUser" if is_user else "models.Model"
    lines = [
        f"class {m['name']}({base}):",
        f'    """Maps to the v2 table ``{m["db_table"]}`` (read/write, schema owned by the database)."""',
    ]
    for f in m["fields"]:
        if is_user and f["name"] in ABSTRACT_USER_FIELDS:
            continue
        lines.append(render_m2m(f) if f["kind"] == "m2m" else render_field(f, m["label"]))
    if is_user:
        lines.append(
            "    groups = models.ManyToManyField('auth.Group', db_table='users_user_groups', related_name='user_set', related_query_name='user', blank=True, verbose_name='groups')"
        )
        lines.append(
            "    user_permissions = models.ManyToManyField('auth.Permission', db_table='users_user_user_permissions', related_name='user_set', related_query_name='user', blank=True, verbose_name='user permissions')"
        )
    lines += [
        "",
        "    class Meta:",
        f"        app_label = {m['app']!r}",
        f"        db_table = {m['db_table']!r}",
        "        managed = LEGACY_MANAGED",
    ]
    if m["ordering"]:
        lines.append(f"        ordering = {tuple(m['ordering'])!r}")
    if m["unique_together"]:
        lines.append(f"        unique_together = {tuple(tuple(x) for x in m['unique_together'])!r}")
    lines.append(f"        verbose_name = {m['verbose_name']!r}")
    lines.append(f"        verbose_name_plural = {m['verbose_name_plural']!r}")
    lines.append("")
    lines.append("    def __str__(self):")
    name_field = next(
        (
            f["name"]
            for f in m["fields"]
            if f["name"] in ("name", "label", "title", "number", "username", "reference_number")
        ),
        None,
    )
    lines.append(
        f"        return str(self.{name_field}) if self.{name_field} else f'{m['name']} {{self.pk}}'"
        if name_field
        else f"        return f'{m['name']} {{self.pk}}'"
    )
    return "\n".join(lines)


HEADER = '''"""GENERATED by scripts/gen_legacy_models.py from the v2 schema. Do not edit by hand.

These models map onto the existing NeuroDB tables so that all historical data stays in
place. They are unmanaged in production (LEGACY_MANAGED=False): Django never creates,
alters or drops these tables. Tests set LEGACY_MANAGED=True so the test database can be
built from these definitions.
"""
from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone
{imports}
LEGACY_MANAGED = getattr(settings, "LEGACY_TABLES_MANAGED", False)

'''


def main(src):
    data = json.load(open(src))
    by_app = {}
    for m in data:
        if m["label"] in DROP:
            continue
        by_app.setdefault(APP_OF[m["label"]], []).append(m)
    for app, models_ in by_app.items():
        out = ROOT / app / "models" / "legacy.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        imports = "from django.contrib.auth.models import AbstractUser\n" if app == "accounts" else ""
        body = "\n\n\n".join(render_model(m) for m in models_)
        out.write_text(HEADER.format(imports=imports) + body + "\n")
        print(f"{out}: {len(models_)} models")


if __name__ == "__main__":
    main(sys.argv[1])
