"""The knowledge hub: every named thing NeuroDB holds, whatever its source, and how they are linked.

An ``Entity`` is a partner, a programme document, a donor, a grant, a section, a place, an
ActivityInfo database or master indicator, a Neuro report, a country programme result, a youth
indicator, a Makani centre, a document… An ``Edge`` says how two are linked ("implemented by",
"funded by", "takes place in", "mentions"…) and which source says so. The hub holds names and links,
never figures: the figures are read live by the assistant's lookups, so they are always exact.

It is rebuilt every night (``build_knowledge_hub``) from the source tables.
"""

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models


class Entity(models.Model):
    class Kind(models.TextChoices):
        PARTNER = "partner", "Partner"
        PROGRAMME = "programme_document", "Programme document"
        DONOR = "donor", "Donor"
        GRANT = "grant", "Grant"
        SECTION = "section", "Section"
        GOVERNORATE = "governorate", "Governorate"
        DISTRICT = "district", "District"
        DATABASE = "database", "ActivityInfo database"
        MASTER_INDICATOR = "master_indicator", "ActivityInfo master indicator"
        REPORT = "report", "Neuro / HPM report"
        CPD_CYCLE = "cpd_cycle", "Country programme cycle"
        CPD_OUTCOME = "cpd_outcome", "Country programme outcome"
        CPD_OUTPUT = "cpd_output", "Country programme output"
        CPD_INDICATOR = "cpd_indicator", "Country programme indicator"
        YOUTH_INDICATOR = "youth_indicator", "Youth indicator (Compiler)"
        EDUCATION_PROGRAMME = "education_programme", "Education programme (Compiler)"
        CENTRE = "makani_centre", "Makani centre"
        DOCUMENT = "document", "Document (knowledge base, library, CPD)"
        MAP = "map", "Map"
        FINDING = "review_finding", "Daily review finding"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    key = models.CharField(max_length=120, help_text="the id in its source")
    name = models.CharField(max_length=500)
    aliases = models.TextField(blank=True, help_text="other names, codes and numbers it is known by")
    description = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    attrs = models.JSONField(
        default=dict, blank=True, help_text="a few facts that identify it (year, status…)"
    )
    lookup = models.JSONField(
        default=dict, blank=True, help_text="the assistant lookup that gives its figures: {tool, args}"
    )
    search_vector = SearchVectorField(null=True)
    built_at = models.DateTimeField()

    class Meta:
        ordering = ("kind", "name")
        constraints = [models.UniqueConstraint(fields=["kind", "key"], name="graph_entity_kind_key")]
        indexes = [GinIndex(fields=["search_vector"], name="graph_entity_search")]
        verbose_name = "knowledge hub entity"
        verbose_name_plural = "knowledge hub entities"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"


class Edge(models.Model):
    source = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="out_edges")
    target = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="in_edges")
    relation = models.CharField(max_length=40, help_text="read as: source <relation> target")
    origin = models.CharField(max_length=40, help_text="the data that says so")
    weight = models.FloatField(default=1, help_text="e.g. how many records or mentions")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "target", "relation"], name="graph_edge_unique")
        ]
        indexes = [models.Index(fields=["target", "relation"])]
        verbose_name = "knowledge hub link"

    def __str__(self):
        return f"{self.source_id} {self.relation} {self.target_id}"
