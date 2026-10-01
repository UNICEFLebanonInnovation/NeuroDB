"""The knowledge hub: every named thing NeuroDB holds, whatever its source, and how they are linked.

An ``Entity`` is a partner, a programme document, a donor, a grant, a section, a place, an
ActivityInfo database or master indicator, a Neuro report, a country programme result, a youth
indicator, a Makani centre, a document… An ``Edge`` says how two are linked ("implemented by",
"funded by", "takes place in", "mentions"…) and which source says so. The hub holds names and links;
the figures are read live by the assistant's lookups, so they are always exact (a few headline figures
are kept in ``snapshot`` only to notice when they move).

It is rebuilt every morning and whenever a sync brings new data (``build_knowledge_hub``). Each build
is compared with the previous one: what is new, gone, newly linked or moved becomes a ``Change``, the
"what's new" of the whole platform, whatever the source.
"""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
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
    snapshot = models.JSONField(
        default=dict, blank=True, help_text="headline figures at the last build, kept to notice changes"
    )
    builder = models.CharField(max_length=40, blank=True, help_text="the source that added it to the hub")
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
    builder = models.CharField(max_length=40, blank=True, help_text="the source that added it to the hub")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "target", "relation"], name="graph_edge_unique")
        ]
        indexes = [models.Index(fields=["target", "relation"])]
        verbose_name = "knowledge hub link"

    def __str__(self):
        return f"{self.source_id} {self.relation} {self.target_id}"


class Change(models.Model):
    """One thing that changed between two builds of the hub."""

    class Op(models.TextChoices):
        ADDED = "added", "New"
        CHANGED = "changed", "Changed"
        REMOVED = "removed", "Gone"
        LINKED = "linked", "Newly linked"
        UNLINKED = "unlinked", "No longer linked"

    detected_at = models.DateTimeField(db_index=True)
    run = models.ForeignKey("core.SyncRun", null=True, blank=True, on_delete=models.SET_NULL)
    entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.SET_NULL, related_name="changes"
    )
    kind = models.CharField(max_length=24, choices=Entity.Kind.choices)
    key = models.CharField(max_length=120)
    name = models.CharField(max_length=500)
    url = models.CharField(max_length=300, blank=True)
    op = models.CharField(max_length=10, choices=Op.choices)
    fields = models.JSONField(default=dict, blank=True, help_text="{field: [before, after]}")
    link = models.JSONField(default=dict, blank=True, help_text="the link that appeared or went")
    sections = ArrayField(
        models.IntegerField(), default=list, blank=True, help_text="section ids it concerns"
    )
    notable = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ("-detected_at", "-id")
        indexes = [
            models.Index(fields=["kind", "key"]),
            GinIndex(fields=["sections"], name="graph_change_sections"),
        ]

    def __str__(self):
        return f"{self.get_op_display()} {self.get_kind_display()}: {self.name}"


class RefreshRequest(models.Model):
    """New data arrived (a sync finished, a document was read): the hub is rebuilt soon after."""

    requested_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=120)

    def __str__(self):
        return f"{self.requested_at:%Y-%m-%d %H:%M} {self.reason}"


class Digest(models.Model):
    """The daily "what's new" note of one section (``section_id`` empty: every section)."""

    date = models.DateField()
    section_id = models.IntegerField(null=True, blank=True)
    section_name = models.CharField(max_length=200, blank=True)
    text = models.TextField()
    written_by = models.CharField(max_length=100, help_text="the AI model, or template")
    changes = models.PositiveIntegerField(default=0)
    emailed_to = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date", "section_name")
        verbose_name = "what's new note"
        constraints = [
            models.UniqueConstraint(fields=["date", "section_id"], name="graph_digest_one_per_day"),
            models.UniqueConstraint(
                fields=["date"], condition=models.Q(section_id__isnull=True), name="graph_digest_one_overall"
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.section_name or 'all sections'}"


class DigestSubscription(models.Model):
    """A person who gets the daily note of their section by email."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="digest")
    email = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "what's new email"

    def __str__(self):
        return f"{self.user} ({'email' if self.email else 'no email'})"
