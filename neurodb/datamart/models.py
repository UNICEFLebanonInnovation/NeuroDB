"""eTools Datamart datasets that the v2 ``etools_*`` tables have no place for (new in v3).

Each row is one Datamart record for the configured country, keyed by the Datamart's own ``id``
(``datamart_id``) and refreshed by ``sync_etools_datamart``. ``source_id`` is the record's id in
eTools. Rows link to the existing programme documents (``etools.PCA``, by the eTools intervention
id or reference number) and partners (``etools.PartnerOrganization``, by the eTools partner id or
vendor number). Those tables belong to the v2 schema, so the links carry no database constraint:
a partner or programme document removed upstream leaves the link empty instead of blocking.
``data`` keeps the whole record as received, for the fields that have no column.
"""

from django.db import models

MONEY = {"max_digits": 20, "decimal_places": 2, "null": True, "blank": True}


def _partner(related_name):
    return models.ForeignKey(
        "etools.PartnerOrganization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name=related_name,
    )


def _intervention(related_name):
    return models.ForeignKey(
        "etools.PCA",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name=related_name,
        verbose_name="programme document",
    )


class DatamartRecord(models.Model):
    datamart_id = models.BigIntegerField(unique=True, help_text="the record's id in the Datamart")
    source_id = models.BigIntegerField(
        null=True, blank=True, db_index=True, help_text="the record's id in eTools"
    )
    last_modify_date = models.DateTimeField(null=True, blank=True, help_text="changed in the Datamart")
    data = models.JSONField(default=dict, blank=True, help_text="the record as received")
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class FundsReservation(DatamartRecord):
    """One line of a funds reservation (FR): the donor, grant and amounts behind a programme document."""

    intervention = _intervention("funds_reservations")
    pd_reference_number = models.CharField(max_length=256, blank=True)
    fr_number = models.CharField("FR number", max_length=20, blank=True, db_index=True)
    line_item = models.IntegerField(null=True, blank=True)
    line_item_text = models.CharField(max_length=255, blank=True)
    fr_type = models.CharField(max_length=50, blank=True)
    vendor_code = models.CharField(max_length=20, blank=True)
    donor = models.CharField(max_length=256, blank=True, db_index=True)
    donor_code = models.CharField(max_length=30, blank=True)
    grant_number = models.CharField(max_length=20, blank=True, db_index=True)
    fund = models.CharField(max_length=10, blank=True)
    wbs = models.CharField("WBS", max_length=30, blank=True)
    currency = models.CharField(max_length=50, blank=True)
    overall_amount = models.DecimalField("line amount (USD)", **MONEY)
    overall_amount_dc = models.DecimalField("line amount (document currency)", **MONEY)
    total_amt = models.DecimalField("FR total", **MONEY)
    intervention_amt = models.DecimalField("FR amount for the PD", **MONEY)
    actual_amt = models.DecimalField("FR actual (disbursed)", **MONEY)
    outstanding_amt = models.DecimalField("FR outstanding", **MONEY)
    document_date = models.DateField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    completed_flag = models.BooleanField(default=False)

    class Meta:
        ordering = ("fr_number", "line_item")
        verbose_name = "funds reservation line"

    def __str__(self):
        return f"{self.fr_number} line {self.line_item}"


class Grant(DatamartRecord):
    name = models.CharField(max_length=128, db_index=True)
    donor = models.CharField(max_length=128, blank=True, db_index=True)
    expiry = models.DateField(null=True, blank=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("donor", "name")

    def __str__(self):
        return self.name


class PDIndicator(DatamartRecord):
    """A programme document indicator. The Datamart repeats an indicator once per location and
    disaggregation; ``source_id`` is the indicator's id, the same on each of those rows."""

    intervention = _intervention("pd_indicators")
    pd_reference_number = models.CharField(max_length=256, blank=True, db_index=True)
    title = models.CharField(max_length=1024, blank=True)
    unit = models.CharField(max_length=10, blank=True)
    display_type = models.CharField(max_length=10, blank=True)
    baseline_numerator = models.DecimalField(**MONEY)
    baseline_denominator = models.DecimalField(**MONEY)
    target_numerator = models.DecimalField(**MONEY)
    target_denominator = models.DecimalField(**MONEY)
    section_name = models.CharField(max_length=128, blank=True)
    lower_result_name = models.CharField("output", max_length=500, blank=True)
    cluster_name = models.CharField(max_length=512, blank=True)
    location_name = models.CharField(max_length=254, blank=True)
    location_pcode = models.CharField(max_length=32, blank=True)
    disaggregation_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_high_frequency = models.BooleanField(default=False)

    class Meta:
        ordering = ("pd_reference_number", "title")
        verbose_name = "PD indicator"

    def __str__(self):
        return self.title[:80]


class PartnerAssessment(DatamartRecord):
    """A HACT assessment of a partner (micro-assessment, simplified checklist, ...)."""

    partner = _partner("datamart_assessments")
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=30, blank=True, db_index=True)
    type = models.CharField(max_length=50, blank=True)
    rating = models.CharField(max_length=50, blank=True)
    requested_date = models.DateField(null=True, blank=True)
    planned_date = models.DateField(null=True, blank=True)
    completed_date = models.DateField(null=True, blank=True)
    current = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-completed_date",)
        verbose_name = "partner assessment"

    def __str__(self):
        return f"{self.type} {self.partner_name}"


class PSEAAssessment(DatamartRecord):
    """A protection from sexual exploitation and abuse (PSEA) assessment of a partner."""

    partner = _partner("psea_assessments")
    partner_name = models.CharField(max_length=255, blank=True)
    vendor_number = models.CharField(max_length=30, blank=True, db_index=True)
    reference_number = models.CharField(max_length=100, blank=True)
    overall_rating = models.IntegerField(null=True, blank=True)
    assessment_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ("-assessment_date",)
        verbose_name = "PSEA assessment"
        verbose_name_plural = "PSEA assessments"

    def __str__(self):
        return f"PSEA {self.partner_name}"


class AuditEngagement(DatamartRecord):
    """An assurance engagement: audit, special audit, spot check or micro-assessment."""

    partner = _partner("datamart_engagements")
    interventions = models.ManyToManyField(
        "etools.PCA", blank=True, related_name="datamart_engagements", db_constraint=False
    )
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=30, blank=True, db_index=True)
    reference_number = models.CharField(max_length=300, blank=True)
    engagement_type = models.CharField(max_length=30, blank=True, db_index=True)
    status = models.CharField(max_length=30, blank=True, db_index=True)
    auditor = models.CharField(max_length=255, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    date_of_field_visit = models.DateField(null=True, blank=True)
    date_of_final_report = models.DateField(null=True, blank=True)
    year_of_audit = models.IntegerField(null=True, blank=True)
    total_value = models.DecimalField(**MONEY)
    amount_tested = models.DecimalField(**MONEY)
    financial_findings = models.DecimalField(**MONEY)
    audit_opinion = models.CharField(max_length=100, blank=True)
    rating = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ("-start_date",)
        verbose_name = "audit engagement"

    def __str__(self):
        return self.reference_number or f"{self.engagement_type} {self.partner_name}"


class ActionPoint(DatamartRecord):
    partner = _partner("datamart_action_points")
    intervention = _intervention("datamart_action_points")
    reference_number = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=30, blank=True, db_index=True)
    high_priority = models.BooleanField(default=False)
    due_date = models.DateField(null=True, blank=True)
    date_of_completion = models.DateTimeField(null=True, blank=True)
    assigned_to_name = models.CharField(max_length=200, blank=True)
    office = models.CharField(max_length=64, blank=True)
    section = models.CharField(max_length=64, blank=True)
    category = models.CharField(max_length=300, blank=True)
    related_module = models.CharField(max_length=64, blank=True, help_text="audit, tpm, fm, t2f, ...")
    module_reference_number = models.CharField(max_length=300, blank=True)
    partner_name = models.CharField(max_length=300, blank=True)
    intervention_number = models.CharField(max_length=64, blank=True)
    location_name = models.CharField(max_length=254, blank=True)

    OPEN_STATUSES = ("open",)

    class Meta:
        ordering = ("-due_date",)
        verbose_name = "action point"

    def __str__(self):
        return self.reference_number or self.description[:80]


class TPMVisit(DatamartRecord):
    """A third-party monitoring (TPM) visit."""

    partner = _partner("tpm_visits")
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=120, blank=True, db_index=True)
    reference_number = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=300, blank=True, db_index=True)
    tpm_name = models.CharField("TPM partner", max_length=300, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    date_of_unicef_approved = models.DateField(null=True, blank=True)
    author_name = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ("-start_date",)
        verbose_name = "TPM visit"

    def __str__(self):
        return self.reference_number or f"TPM visit {self.datamart_id}"


class MonitoringFinding(DatamartRecord):
    """A field monitoring (FM) finding on an entity: whether it is on track, from a monitoring activity."""

    partner = _partner("monitoring_findings")
    vendor_number = models.CharField(max_length=30, blank=True, db_index=True)
    entity = models.CharField(max_length=255, blank=True)
    entity_type = models.CharField(max_length=100, blank=True)
    monitoring_activity = models.CharField(max_length=64, blank=True)
    reference_number = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, blank=True)
    overall_finding_rating = models.CharField(max_length=50, blank=True, db_index=True)
    narrative_finding = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    location_name = models.CharField(max_length=254, blank=True)
    site = models.CharField(max_length=254, blank=True)
    is_programmatic_visit = models.BooleanField(default=False)
    is_remote_monitoring = models.BooleanField(default=False)
    visit_lead = models.CharField(max_length=254, blank=True)

    class Meta:
        ordering = ("-end_date",)
        verbose_name = "field monitoring finding"

    def __str__(self):
        return f"{self.monitoring_activity} {self.entity}"


class HACTAggregate(DatamartRecord):
    """Country-level HACT assurance totals for one year."""

    year = models.IntegerField(db_index=True)
    microassessments_total = models.IntegerField(default=0)
    programmaticvisits_total = models.IntegerField(default=0)
    followup_spotcheck = models.IntegerField(default=0)
    completed_spotcheck = models.IntegerField(default=0)
    completed_hact_audits = models.IntegerField(default=0)
    completed_special_audits = models.IntegerField(default=0)

    class Meta:
        ordering = ("-year",)
        verbose_name = "HACT year"

    def __str__(self):
        return f"HACT {self.year}"
