from django.db import models


class PopulationFigure(models.Model):
    """Population figures per year, nationality, admin area and age group (new v3 table).

    Replaces v2's year-stamped JSON/Excel files read from the package directory on every request.
    Loaded by ``manage.py load_population_figures``; the admin can upload a new year's workbook.
    """

    class Nationality(models.TextChoices):
        LEB = "LEB", "Lebanese"
        SYR = "SYR", "Syrian"
        PRS = "PRS", "Palestinian refugees from Syria"
        PRL = "PRL", "Palestinian refugees in Lebanon"
        OTH = "OTH", "Other"
        ALL = "ALL", "All"

    class Level(models.TextChoices):
        NATIONAL = "national", "National"
        GOVERNORATE = "governorate", "Governorate"
        DISTRICT = "district", "District"
        CADASTER = "cadaster", "Cadaster"

    year = models.PositiveSmallIntegerField(db_index=True)
    nationality = models.CharField(max_length=3, choices=Nationality.choices)
    level = models.CharField(max_length=12, choices=Level.choices)
    area_code = models.CharField(max_length=20, blank=True, db_index=True)
    area_name = models.CharField(max_length=120, blank=True)
    parent_name = models.CharField(max_length=120, blank=True, help_text="governorate of a district, etc.")
    age_group = models.CharField(
        max_length=20, blank=True, help_text="e.g. 0-4, 5-17, 18-59, 60+ ; empty = all ages"
    )
    sex = models.CharField(max_length=6, blank=True, help_text="male/female; empty = both")
    category = models.CharField(max_length=24, default="total", help_text="total | children | vulnerable")
    vulnerability_level = models.CharField(max_length=24, blank=True)
    value = models.BigIntegerField()
    source = models.CharField(max_length=200, blank=True)

    class Meta:
        indexes = [models.Index(fields=["year", "category", "level"])]
        unique_together = (
            (
                "year",
                "category",
                "nationality",
                "level",
                "area_code",
                "age_group",
                "sex",
                "vulnerability_level",
            ),
        )

    def __str__(self):
        area = self.area_name or self.level
        return f"{self.year} {self.category} {self.nationality} {area} {self.age_group}: {self.value}"
