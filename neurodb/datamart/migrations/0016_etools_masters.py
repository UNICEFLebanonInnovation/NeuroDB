"""The eTools master indicators: a section's master indicator (EtoolsMaster), its code and target for each
year (EtoolsMasterYear) and the PD indicators it is combined from (EtoolsMasterLink). Three new, empty tables
that people write to: schema only, no code runs, and the release before this one never reads them."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0015_record_people"),
        ("etools", "0003_partnerlink"),
    ]

    operations = [
        migrations.CreateModel(
            name="EtoolsMaster",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "section",
                    models.CharField(
                        help_text="the eTools section name, as on the PD indicators",
                        max_length=128,
                    ),
                ),
                ("name", models.CharField(max_length=500)),
                (
                    "unit",
                    models.CharField(
                        blank=True,
                        help_text="shown next to the figures (a label only)",
                        max_length=64,
                    ),
                ),
                (
                    "cp_output",
                    models.CharField(
                        blank=True,
                        help_text="the country programme output it is listed under",
                        max_length=500,
                        verbose_name="CP output",
                    ),
                ),
                (
                    "keywords",
                    models.CharField(
                        blank=True,
                        help_text="more words that help NeuroDB suggest PD indicators for it",
                        max_length=500,
                    ),
                ),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("sum", "Sum"),
                            ("percent", "Percentage (numerators over denominators)"),
                            ("ratio", "Ratio (numerators over denominators)"),
                            ("average", "Average"),
                            ("maximum", "Maximum"),
                            ("minimum", "Minimum"),
                        ],
                        default="sum",
                        max_length=16,
                    ),
                ),
                (
                    "pd_parts_add_up",
                    models.BooleanField(
                        default=False,
                        help_text="several indicators of one programme document count different people, so their figures are added; otherwise only the programme document's largest one counts",
                        verbose_name="parts of one PD add up",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("proposed", "Proposed"),
                            ("active", "Active"),
                            ("dismissed", "Dismissed"),
                            ("archived", "Archived"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                (
                    "origin",
                    models.CharField(
                        choices=[
                            ("manual", "Made by hand"),
                            ("proposed", "Proposed by NeuroDB"),
                            ("activityinfo", "From an ActivityInfo master indicator"),
                        ],
                        default="manual",
                        max_length=16,
                    ),
                ),
                (
                    "proposal_key",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="the structure key it was proposed from (a key any master holds is never proposed again)",
                        max_length=40,
                    ),
                ),
                (
                    "activityinfo_master_id",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="ActivityInfo master indicator",
                    ),
                ),
                (
                    "sequence",
                    models.PositiveSmallIntegerField(
                        default=0, help_text="its place in its section"
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=500)),
                ("updated_by", models.CharField(blank=True, max_length=150)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "master indicator (eTools)",
                "verbose_name_plural": "master indicators (eTools)",
                "ordering": ("section", "sequence", "name"),
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status__in", ["active", "archived"])),
                        fields=("section", "name"),
                        name="datamart_etoolsmaster_section_name",
                        violation_error_message="This section already has a master indicator with this name.",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="EtoolsMasterLink",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "ident",
                    models.CharField(
                        help_text="the indicator in its PD: s and its eTools id, else t and its title's hash",
                        max_length=48,
                    ),
                ),
                (
                    "title",
                    models.CharField(
                        blank=True,
                        help_text="the indicator's title when linked",
                        max_length=1024,
                    ),
                ),
                (
                    "pd_number",
                    models.CharField(
                        blank=True, max_length=64, verbose_name="PD number"
                    ),
                ),
                (
                    "unit_class",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("number", "Number"),
                            ("percentage", "Percentage"),
                            ("ratio", "Ratio"),
                            ("other", "Other"),
                        ],
                        help_text="the indicator's type when confirmed: another type in eTools stops it counting until it is confirmed again",
                        max_length=12,
                        verbose_name="type",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("total", "In the total"),
                            ("numerator", "Numerator"),
                            ("denominator", "Denominator"),
                            ("shown", "Shown only"),
                        ],
                        default="total",
                        max_length=16,
                        verbose_name="counts as",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("suggested", "Suggested"),
                            ("confirmed", "Confirmed"),
                            ("rejected", "Rejected"),
                        ],
                        db_index=True,
                        default="suggested",
                        max_length=16,
                    ),
                ),
                (
                    "score",
                    models.FloatField(
                        blank=True,
                        help_text="how close the suggestion is (at most 1)",
                        null=True,
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        blank=True, help_text="why NeuroDB suggested it", max_length=200
                    ),
                ),
                ("sequence", models.PositiveSmallIntegerField(default=0)),
                ("confirmed_by", models.CharField(blank=True, max_length=150)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "master",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="links",
                        to="datamart.etoolsmaster",
                    ),
                ),
                (
                    "pd",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="etools.pca",
                        verbose_name="programme document",
                    ),
                ),
            ],
            options={
                "verbose_name": "master indicator link (eTools)",
                "verbose_name_plural": "master indicator links (eTools)",
                "ordering": ("master", "sequence", "id"),
                "indexes": [
                    models.Index(
                        fields=["pd", "ident"], name="datamart_etoolsmasterlink_pd"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("master", "pd", "ident"),
                        name="datamart_etoolsmasterlink_key",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            ("role", "total"),
                            ("status__in", ["suggested", "confirmed"]),
                        ),
                        fields=("pd", "ident"),
                        name="datamart_etoolsmasterlink_one_total_home",
                        violation_error_message="This PD indicator already counts in the total of another master indicator: it can be in one total only (it may be shown, or be a numerator or a denominator, elsewhere).",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="EtoolsMasterYear",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "year",
                    models.PositiveSmallIntegerField(help_text="the calendar year"),
                ),
                (
                    "code",
                    models.CharField(
                        blank=True, help_text="the AWP/RWP code", max_length=32
                    ),
                ),
                (
                    "target",
                    models.FloatField(
                        blank=True,
                        help_text="the year's target (AWP/RWP figure) the status is measured against",
                        null=True,
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="untick to leave the master out of this year",
                        verbose_name="shown this year",
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=500)),
                ("updated_by", models.CharField(blank=True, max_length=150)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "master",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="years",
                        to="datamart.etoolsmaster",
                    ),
                ),
            ],
            options={
                "verbose_name": "master indicator year (eTools)",
                "verbose_name_plural": "master indicator years (eTools)",
                "ordering": ("master", "year"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("master", "year"),
                        name="datamart_etoolsmasteryear_master_year",
                    )
                ],
            },
        ),
    ]
