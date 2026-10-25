"""eTools Datamart datasets that the v2 ``etools_*`` tables have no place for (new in v3).

Each row is one Datamart record for the configured country, keyed by the Datamart's own ``id``
(``datamart_id``) and refreshed by ``sync_etools_datamart``. ``source_id`` is the record's id in
eTools. Rows link to the existing programme documents (``etools.PCA``, by the eTools intervention
id or reference number) and partners (``etools.PartnerOrganization``, by the eTools partner id or
vendor number). Those tables belong to the v2 schema, so the links carry no database constraint:
a partner or programme document removed upstream leaves the link empty instead of blocking.
``data`` keeps the whole record as received, for the fields that have no column.
"""

from django.conf import settings
from django.db import models

MONEY = {"max_digits": 20, "decimal_places": 2, "null": True, "blank": True}
TAG = {"max_length": 30, "blank": True, "db_index": True}


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


def _location(related_name):
    """The eTools location (``locations.Location``, id = eTools id) resolved from the record's
    location source id or P-code; never from its name."""
    return models.ForeignKey(
        "locations.Location",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name=related_name,
        verbose_name="eTools location",
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
    location_source_id = models.BigIntegerField(null=True, blank=True)
    location_level = models.IntegerField(null=True, blank=True)
    location_levelname = models.CharField(max_length=80, blank=True)
    location = _location("pd_indicators")
    numerator_label = models.CharField(max_length=256, blank=True)
    denominator_label = models.CharField(max_length=256, blank=True)
    means_of_verification = models.CharField(max_length=255, blank=True)
    cp_output = models.CharField(
        "CP output",
        max_length=500,
        blank=True,
        help_text="the country programme output, from the indicator's partner reports (not in pd-indicators)",
    )
    # Whom the indicator counts, read from its title (neurodb.datamart.tags)
    tag_gender = models.CharField(**TAG)
    tag_age_group = models.CharField(**TAG)
    tag_nationality = models.CharField(**TAG)
    tag_disability = models.CharField(**TAG)

    class Meta:
        ordering = ("pd_reference_number", "title")
        verbose_name = "PD indicator"
        indexes = [models.Index(fields=["intervention", "title"])]

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
    # From the per-type detail datasets (audit results, audits, spot checks, micro-assessments, special
    # audits), matched by reference number; ``details`` keeps each of those records by dataset.
    risk_rating = models.CharField(max_length=100, blank=True)
    audited_expenditure = models.DecimalField(**MONEY)
    amount_refunded = models.DecimalField(**MONEY)
    pending_unsupported_amount = models.DecimalField(**MONEY)
    financial_findings_count = models.IntegerField(null=True, blank=True)
    high_priority_findings = models.IntegerField(null=True, blank=True)
    key_control_weaknesses = models.IntegerField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

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
    location_pcode = models.CharField(max_length=32, blank=True)
    location_source_id = models.BigIntegerField(null=True, blank=True)
    location_level = models.IntegerField(null=True, blank=True)
    location_levelname = models.CharField(max_length=80, blank=True)
    location = _location("datamart_action_points")
    # the eTools record the action point was raised from, by its eTools id
    related_module_id = models.BigIntegerField(null=True, blank=True)
    tpm_activity_source_id = models.BigIntegerField(null=True, blank=True)
    engagement_source_id = models.BigIntegerField(null=True, blank=True)
    travel_activity_source_id = models.BigIntegerField(null=True, blank=True)
    tpm_activity = models.ForeignKey(
        "TPMActivity", null=True, blank=True, on_delete=models.SET_NULL, related_name="action_points"
    )
    engagement = models.ForeignKey(
        AuditEngagement,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="datamart_action_points",
    )

    # an action point being worked on is still open (FMS counts "in progress" as open): every open count,
    # overdue count and follow-up check reads these
    OPEN_STATUSES = ("open", "in_progress", "in progress", "in-progress")
    COMPLETED_STATUSES = ("completed", "closed", "resolved")  # lower case, as eTools writes them

    class Meta:
        ordering = ("-due_date",)
        verbose_name = "action point"

    def __str__(self):
        return self.reference_number or self.description[:80]

    @property
    def is_open(self) -> bool:
        """Open or in progress (``OPEN_STATUSES``): it can be overdue."""
        return (self.status or "").strip().lower() in self.OPEN_STATUSES


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
    location_pcode = models.CharField(max_length=32, blank=True)
    location_source_id = models.BigIntegerField(null=True, blank=True)
    location = _location("monitoring_findings")
    site = models.CharField(max_length=254, blank=True)
    monitoring_site = models.ForeignKey(
        "MonitoringSite", null=True, blank=True, on_delete=models.SET_NULL, related_name="findings"
    )
    monitoring_activity_id = models.BigIntegerField(null=True, blank=True, help_text="eTools activity id")
    is_programmatic_visit = models.BooleanField(default=False)
    is_remote_monitoring = models.BooleanField(default=False)
    visit_lead = models.CharField(max_length=254, blank=True)
    # the programme document the entity is (or names), found from the entity's text by
    # datamart.fm.PDResolver at sync time and by the Monitoring insights refresh
    intervention = _intervention("monitoring_findings")
    pd_match = models.CharField(
        max_length=8,
        blank=True,
        help_text="how the programme document was found: exact, token, base, title, or blank",
    )

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


class FundsReservationHeader(DatamartRecord):
    """One funds reservation (FR): amounts reserved, disbursed and outstanding for a programme document."""

    intervention = _intervention("fr_headers")
    pd_reference_number = models.CharField(max_length=256, blank=True)
    fr_number = models.CharField("FR number", max_length=20, blank=True, db_index=True)
    fr_type = models.CharField(max_length=50, blank=True)
    vendor_code = models.CharField(max_length=20, blank=True, db_index=True)
    document_text = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=50, blank=True)
    total_amt = models.DecimalField("reserved", **MONEY)
    intervention_amt = models.DecimalField("amount for the PD", **MONEY)
    actual_amt = models.DecimalField("disbursed", **MONEY)
    outstanding_amt = models.DecimalField("outstanding", **MONEY)
    document_date = models.DateField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    completed_flag = models.BooleanField(default=False)

    class Meta:
        ordering = ("-start_date", "fr_number")
        verbose_name = "funds reservation"

    def __str__(self):
        return self.fr_number


class AuditFinding(DatamartRecord):
    """A financial finding of an audit, spot check or special audit (ineligible or unsupported spending)."""

    partner = _partner("audit_findings")
    engagement = models.ForeignKey(
        AuditEngagement,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="findings",
    )
    reference_number = models.CharField(max_length=300, blank=True, db_index=True)
    engagement_type = models.CharField(max_length=30, blank=True)
    engagement_status = models.CharField(max_length=30, blank=True)
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=30, blank=True)
    finding_number = models.IntegerField(null=True, blank=True)
    title = models.CharField(max_length=300, blank=True)
    amount = models.DecimalField("amount (USD)", **MONEY)
    local_amount = models.DecimalField(**MONEY)
    description = models.TextField(blank=True)
    recommendation = models.TextField(blank=True)
    ip_comments = models.TextField("partner comments", blank=True)
    created = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ("-created", "reference_number", "finding_number")
        verbose_name = "audit finding"

    def __str__(self):
        return f"{self.reference_number} #{self.finding_number}"


class ReportedIndicator(DatamartRecord):
    """Partner reporting (PRP): one indicator of one progress report, in one location. The progress
    report itself (number, period, status, narrative) repeats on each of its indicator rows."""

    partner = _partner("reported_indicators")
    intervention = _intervention("reported_indicators")
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=30, blank=True)
    pd_reference_number = models.CharField(max_length=256, blank=True, db_index=True)
    progress_report = models.CharField(max_length=300, blank=True, db_index=True)
    indicator_report = models.CharField(
        max_length=64, blank=True, db_index=True, help_text="PRP's id of this indicator in this report"
    )
    report_number = models.CharField(max_length=30, blank=True)
    report_type = models.CharField(max_length=30, blank=True, db_index=True)
    report_status = models.CharField(max_length=50, blank=True, db_index=True)
    report_accepted_status = models.CharField(max_length=50, blank=True)
    is_report_final = models.BooleanField(default=False)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True, db_index=True)
    due_date = models.DateField(null=True, blank=True)
    submission_date = models.DateField(null=True, blank=True)
    acceptance_date = models.DateField(null=True, blank=True)
    submitted_by = models.CharField(max_length=300, blank=True)
    narrative = models.TextField(blank=True)
    section = models.CharField(max_length=300, blank=True)
    pd_output = models.CharField(max_length=500, blank=True)
    pd_output_progress_status = models.CharField(max_length=50, blank=True)
    indicator = models.CharField(max_length=1024, blank=True)
    baseline = models.CharField(max_length=100, blank=True)
    target = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=254, blank=True)
    p_code = models.CharField(max_length=32, blank=True)
    location_ref = _location("reported_indicators")
    achievement_in_period = models.CharField(max_length=100, blank=True)
    total_cumulative_progress = models.CharField(max_length=100, blank=True)
    total_cumulative_progress_in_location = models.CharField(max_length=100, blank=True)
    previous_location_progress = models.CharField(max_length=100, blank=True)
    admin_level = models.IntegerField(null=True, blank=True)
    high_frequency = models.BooleanField(default=False)
    calculation_across_locations = models.CharField(max_length=20, blank=True)
    calculation_across_periods = models.CharField(max_length=20, blank=True)
    etools_indicator_id = models.CharField(max_length=64, blank=True, db_index=True)
    etools_pd_result_id = models.CharField(max_length=64, blank=True)
    cp_output = models.CharField("CP output", max_length=500, blank=True)
    etools_cp_output_id = models.CharField("eTools CP output id", max_length=64, blank=True)
    narrative_assessment = models.TextField(blank=True)
    disaggregation = models.JSONField(default=dict, blank=True)
    tag_gender = models.CharField(**TAG)
    tag_age_group = models.CharField(**TAG)
    tag_nationality = models.CharField(**TAG)
    tag_disability = models.CharField(**TAG)

    class Meta:
        ordering = ("-period_end", "pd_reference_number", "indicator")
        verbose_name = "reported indicator"
        indexes = [
            models.Index(fields=["intervention", "indicator"]),
            models.Index(fields=["report_type", "period_end"]),
        ]

    def __str__(self):
        return f"{self.pd_reference_number} {self.report_number} {self.indicator[:60]}"


class TPMActivity(DatamartRecord):
    """One activity of a third-party monitoring visit: the programme document, place and date monitored."""

    partner = _partner("tpm_activities")
    intervention = _intervention("tpm_activities")
    visit_reference_number = models.CharField(max_length=300, blank=True, db_index=True)
    task_reference_number = models.CharField(max_length=300, blank=True)
    visit_status = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=50, blank=True)
    tpm_name = models.CharField("TPM partner", max_length=300, blank=True)
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=120, blank=True)
    pd_reference_number = models.CharField(max_length=300, blank=True)
    section = models.CharField(max_length=300, blank=True)
    locations = models.CharField(max_length=1000, blank=True)
    location_pcodes = models.JSONField(default=list, blank=True, help_text="P-codes of the places monitored")
    location_links = models.ManyToManyField(
        "locations.Location", blank=True, db_constraint=False, related_name="tpm_activities"
    )
    visit = models.ForeignKey(
        TPMVisit, null=True, blank=True, on_delete=models.SET_NULL, related_name="activities"
    )
    date = models.DateField(null=True, blank=True, db_index=True)
    is_programmatic_visit = models.BooleanField(default=False)

    class Meta:
        ordering = ("-date",)
        verbose_name = "TPM activity"
        verbose_name_plural = "TPM activities"

    def __str__(self):
        return self.task_reference_number or self.visit_reference_number


class ProgrammaticVisit(DatamartRecord):
    """A trip activity of UNICEF staff (programmatic visit, spot check, meeting...) from eTools Trips."""

    partner = _partner("programmatic_visits")
    intervention = _intervention("programmatic_visits")
    travel_reference_number = models.CharField(max_length=200, blank=True)
    travel_type = models.CharField(max_length=200, blank=True, db_index=True)
    date = models.DateField(null=True, blank=True, db_index=True)
    partner_name = models.CharField(max_length=200, blank=True)
    partnership_number = models.CharField(max_length=200, blank=True)
    primary_traveler = models.CharField(max_length=200, blank=True)
    location_name = models.CharField(max_length=254, blank=True)
    location_pcode = models.CharField(max_length=32, blank=True)
    location = _location("programmatic_visits")

    class Meta:
        ordering = ("-date",)
        verbose_name = "staff trip activity"
        verbose_name_plural = "staff trip activities"

    def __str__(self):
        return f"{self.travel_reference_number} {self.travel_type}"


class PlannedVisits(DatamartRecord):
    """Programmatic visits planned per quarter for a programme document and year."""

    intervention = _intervention("planned_visits_by_year")
    partner = _partner("planned_visit_years")
    pd_reference_number = models.CharField(max_length=256, blank=True)
    year = models.IntegerField(null=True, blank=True)
    q1 = models.IntegerField(default=0)
    q2 = models.IntegerField(default=0)
    q3 = models.IntegerField(default=0)
    q4 = models.IntegerField(default=0)

    class Meta:
        ordering = ("-year",)
        verbose_name = "planned visits"
        verbose_name_plural = "planned visits"

    def __str__(self):
        return f"{self.pd_reference_number} {self.year}"

    @property
    def total(self):
        return self.q1 + self.q2 + self.q3 + self.q4


class PartnerHACTYear(DatamartRecord):
    """A partner's HACT year: cash transfers, risk rating, and assurance planned, required and done."""

    partner = _partner("hact_years")
    partner_name = models.CharField(max_length=300, blank=True)
    vendor_number = models.CharField(max_length=30, blank=True, db_index=True)
    year = models.IntegerField(null=True, blank=True, db_index=True)
    risk_rating = models.CharField(max_length=100, blank=True)
    assessment_type = models.CharField(max_length=100, blank=True)
    cash_transfers = models.DecimalField("cash transfers (Jan-Dec)", **MONEY)
    liquidations = models.DecimalField("liquidations (Oct-Sep)", **MONEY)
    pv_required = models.IntegerField("programmatic visits required", null=True, blank=True)
    pv_planned = models.IntegerField("programmatic visits planned", null=True, blank=True)
    pv_completed = models.IntegerField("programmatic visits completed", null=True, blank=True)
    sc_required = models.IntegerField("spot checks required", null=True, blank=True)
    sc_planned = models.IntegerField("spot checks planned", null=True, blank=True)
    sc_completed = models.IntegerField("spot checks completed", null=True, blank=True)
    audits_required = models.IntegerField(null=True, blank=True)
    audits_completed = models.IntegerField(null=True, blank=True)
    outstanding_findings = models.IntegerField("audits with outstanding findings", null=True, blank=True)
    expiring_threshold = models.BooleanField(default=False)
    approaching_threshold = models.BooleanField(default=False)

    class Meta:
        ordering = ("-year", "partner_name")
        verbose_name = "partner HACT year"

    def __str__(self):
        return f"{self.partner_name} {self.year}"


class PDActivity(DatamartRecord):
    """A workplan activity of a programme document, with its UNICEF and partner cash."""

    intervention = _intervention("workplan_activities")
    pd_reference_number = models.CharField(max_length=256, blank=True, db_index=True)
    result = models.CharField("PD output", max_length=500, blank=True)
    result_code = models.CharField(max_length=50, blank=True)
    code = models.CharField(max_length=50, blank=True)
    name = models.CharField(max_length=1000, blank=True)
    unicef_cash = models.DecimalField(**MONEY)
    cso_cash = models.DecimalField("partner cash", **MONEY)

    class Meta:
        ordering = ("pd_reference_number", "result_code", "code")
        verbose_name = "PD workplan activity"
        verbose_name_plural = "PD workplan activities"

    def __str__(self):
        return f"{self.code} {self.name[:60]}"


class DatamartDocument(models.Model):
    """Any eTools Datamart record, kept whole for the AI assistant and linked to NeuroDB.

    Every dataset of ``neurodb.datamart.catalogue`` that has no table of its own lands here (the
    PD ePD narratives, reviews, locations of PDs, attachments, FM questions, PRP reports...), plus a
    raw copy of the records that update the eTools tables (partners, programme documents, budgets,
    agreements) and of the per-type audit records. ``record_key`` is the Datamart id; for the few datasets
    whose records carry none, a key the data proves unique (``"prp_id:77"``, ``datamart_sync.identity_key``)
    or else a hash of the record. Contact details (e-mail addresses, phone numbers) are removed before
    storing.
    """

    dataset = models.CharField(max_length=64, db_index=True)
    record_key = models.CharField(max_length=64)
    source_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    partner = _partner("datamart_documents")
    intervention = _intervention("datamart_documents")
    title = models.CharField(max_length=500, blank=True)
    date = models.DateField(null=True, blank=True, db_index=True, help_text="the record's main date")
    data = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("dataset", "-date")
        constraints = [
            models.UniqueConstraint(fields=["dataset", "record_key"], name="datamart_document_key")
        ]
        indexes = [
            models.Index(fields=["dataset", "partner"]),
            models.Index(fields=["dataset", "intervention"]),
        ]
        verbose_name = "Datamart record"

    def __str__(self):
        return f"{self.dataset}: {self.title or self.record_key}"


class DisaggregationLabel(models.Model):
    """The name of a PRP disaggregation ("Sex") or of one of its values ("female"). The partner reports
    key their disaggregation values by PRP ids (``"(12,)"``, a cross-tab cell ``"(12, 31)"``, the
    location total ``"()"``); the eTools Datamart sync reads the labels of the ids it sees there, one
    by one (``sources/prp/indicatordisaggregationvalue/<id>/`` and ``indicatordisaggregation/<id>/``).
    Labels only: no value, no person."""

    class Kind(models.TextChoices):
        VALUE = "value", "disaggregation value"
        TYPE = "type", "disaggregation"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    prp_id = models.BigIntegerField(help_text="the id in the Partner Reporting Portal")
    label = models.CharField(max_length=255, blank=True)
    disaggregation_id = models.BigIntegerField(
        null=True, blank=True, help_text="a value's disaggregation (its type), as a PRP id"
    )
    active = models.BooleanField(default=True)
    not_found = models.BooleanField(default=False, help_text="PRP answered that the id does not exist")
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "prp_id"], name="datamart_disaggregationlabel_key")
        ]
        ordering = ("kind", "label")
        verbose_name = "disaggregation label"

    def __str__(self):
        return f"{self.get_kind_display()} {self.prp_id}: {self.label or '?'}"


class MonitoringSite(DatamartRecord):
    """A field-monitoring site of eTools (``location-sites``): a named point inside an admin location."""

    name = models.CharField(max_length=254)
    p_code = models.CharField(max_length=32, blank=True, db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    parent_pcode = models.CharField(max_length=32, blank=True)
    parent = _location("monitoring_sites")

    class Meta:
        ordering = ("name",)
        verbose_name = "monitoring site"

    def __str__(self):
        return self.name


class IndicatorFlag(models.Model):
    """What a section says about one indicator, set once and kept across syncs. Two roles:

    * **Counts children.** The overview's children indicators are those whose title names children
      (the age-group tag) unless a flag says otherwise: ``counts_children=True`` adds an indicator
      the title rule misses, ``False`` removes one it wrongly includes, empty follows the title rule.
    * **Unique beneficiaries** (eTools PD indicators only). The one indicator of a programme document
      that counts each child or person it reached once: the PD's headline "children / beneficiaries
      reached" (``neurodb.datamart.headline``). One per PD, set on the PD page by an Administrator or a
      Section editor of the PD's section, or here.

    Keyed by the source (eTools PD indicator or ActivityInfo master indicator) and the indicator's own
    id there, so the Datamart sync, which rewrites the indicator rows, never loses it.
    """

    class Source(models.TextChoices):
        ETOOLS = "etools", "eTools PD indicator"
        ACTIVITYINFO = "activityinfo", "ActivityInfo master indicator"

    source = models.CharField(max_length=16, choices=Source.choices)
    key = models.CharField(
        max_length=64,
        help_text="eTools indicator id (source_id) or ActivityInfo master indicator id",
    )
    label = models.CharField(max_length=1024, blank=True, help_text="the indicator title, for the list")
    counts_children = models.BooleanField(
        null=True,
        blank=True,
        default=True,
        help_text="whether the indicator counts children; empty: the title decides",
    )
    unique_beneficiaries = models.BooleanField(
        "unique beneficiaries",
        default=False,
        help_text=(
            "the programme document's unique beneficiaries indicator: each child or person counted once, "
            "the PD's figure in every children reached total (one per programme document)"
        ),
    )
    intervention = _intervention("indicator_flags")
    note = models.CharField(max_length=500, blank=True)
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "key"], name="datamart_indicatorflag_key"),
            models.UniqueConstraint(
                fields=["intervention"],
                condition=models.Q(unique_beneficiaries=True),
                name="datamart_indicatorflag_one_unique_per_pd",
            ),
        ]
        ordering = ("source", "label")
        verbose_name = "indicator flag"
        verbose_name_plural = "indicator flags"

    def __str__(self):
        if self.counts_children is None:
            verdict = "title rule"
        else:
            verdict = "counts children" if self.counts_children else "does not count children"
        if self.unique_beneficiaries:
            verdict += ", unique beneficiaries of its programme document"
        return f"{self.get_source_display()} {self.key}: {verdict}"


class VisitPlanComment(models.Model):
    """A section's comment on a programme document's HACT programmatic visit plan for one year (the workbook's
    "Section Comments"), written on the Field monitoring page's HACT visit plan and kept across syncs.

    Shown to every signed-in staff member and written into the plan's Excel and CSV files; never sent to
    the AI. Written by an Administrator or a Section editor of one of the PD's sections
    (``datamart.hact_plan.can_comment``)."""

    # PCA rows are updated, never deleted, by the syncs
    intervention = _intervention("visit_plan_comments")
    pd_reference = models.CharField(
        "PD reference", max_length=64, blank=True, help_text="the PD's reference number when written"
    )
    year = models.PositiveSmallIntegerField(help_text="the HACT year (calendar year) of the plan")
    text = models.CharField(max_length=500)
    # the username: for the admin only, never shown on a page, exported or sent to the AI
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["intervention", "year"], name="datamart_visitplancomment_pd_year")
        ]
        ordering = ("-year", "pd_reference")
        verbose_name = "visit plan comment"
        verbose_name_plural = "visit plan comments"

    def __str__(self):
        return f"{self.pd_reference or f'PD {self.intervention_id}'} {self.year}"


class RecordPeople(models.Model):
    """The person names the eTools records hold under their keys that name a person
    (``query.datamart_people``), read from every record at the end of each eTools Datamart sync. One row,
    in a table of its own: no cull of the page-figure cache, no emptied page figure and no restart loses
    it. Never shown, exported or sent to the AI: NeuroDB only takes these names out of what it sends."""

    names = models.JSONField(default=list)
    read_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "names the records hold"
        verbose_name_plural = "names the records hold"

    def __str__(self):
        return f"{len(self.names)} names, read {self.read_at:%Y-%m-%d %H:%M}"


class RawFieldReview(models.Model):
    """A field of an eTools source a superuser reviewed on Raw eTools data (Admin → Data and sync): its values
    are shown there and written in its CSVs, through the people rule (``datamart.raw_people``). A field
    neither reviewed here nor in ``datamart.raw.REVIEWED_KEYS`` shows only numbers, yes/no and dates."""

    source = models.CharField(max_length=64)
    key = models.CharField(max_length=64)
    reviewed_at = models.DateTimeField(auto_now_add=True)
    # the username: for the admin only, never shown on a page, exported or sent to the AI
    reviewed_by = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "key"], name="datamart_rawfieldreview_key")]
        verbose_name = "reviewed field"
        verbose_name_plural = "reviewed fields"

    def __str__(self):
        return f"{self.source}.{self.key}"


class RestRecord(models.Model):
    """A record of the eTools REST API, kept whole as it came, for Raw eTools data (Admin → Data and sync):
    the partners, agreements, programme documents, trips, engagements and action points the eTools REST sync
    reads, list and detail apart (``entity``: ``raw.REST_ENTITIES``). Written by the job "Read the eTools REST
    API into Raw eTools data" (``integrations.etools.rest_raw``) and by the eTools REST sync as it reads;
    nothing else reads it but Raw eTools data and the names NeuroDB takes out of texts
    (``query.datamart_people``). Contact details are removed before storing (``catalogue.scrub``)."""

    entity = models.CharField(max_length=32, db_index=True)
    # the record's eTools id, else a hash of the record (``rest_raw.record_key``)
    record_key = models.CharField(max_length=64)
    source_id = models.CharField(max_length=64, blank=True, help_text="the eTools id")
    reference = models.CharField(max_length=255, blank=True, help_text="its number: never a person")
    title = models.CharField(
        max_length=255, blank=True, help_text="its name or title: never a trip's purpose"
    )
    parent_key = models.CharField(max_length=64, blank=True, help_text="a detail: the list record it details")
    data = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["entity", "record_key"], name="datamart_restrecord_key")
        ]
        indexes = [models.Index(fields=["entity", "-synced_at"], name="datamart_restrecord_synced")]
        verbose_name = "eTools REST record"

    def __str__(self):
        return f"{self.entity}: {self.reference or self.record_key}"


class EtoolsMaster(models.Model):
    """A section's master indicator over the partners' eTools PD indicators, like an ActivityInfo master
    indicator: one figure a year, combined from its sub-indicators (:class:`EtoolsMasterLink`, one PD
    indicator each) by its method, by :func:`neurodb.datamart.masters.figures` when a page is read (no
    figure is stored).

    Only confirmed links count: a link NeuroDB suggests is listed, never added. The master has no year: each
    year's code and target are an :class:`EtoolsMasterYear`, so links of a PD that runs several years carry on
    by themselves. Kept across syncs, as the indicator flags: links name a PD indicator by its PD and its
    identity, never by a Datamart row.

    ``updated_by`` is for the admin only: never shown on a page, exported or sent to the AI."""

    class Method(models.TextChoices):
        SUM = "sum", "Sum"
        PERCENT = "percent", "Percentage (numerators over denominators)"
        RATIO = "ratio", "Ratio (numerators over denominators)"
        AVERAGE = "average", "Average"
        MAXIMUM = "maximum", "Maximum"
        MINIMUM = "minimum", "Minimum"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ACTIVE = "active", "Active"
        DISMISSED = "dismissed", "Dismissed"
        ARCHIVED = "archived", "Archived"

    class Origin(models.TextChoices):
        MANUAL = "manual", "Made by hand"
        PROPOSED = "proposed", "Proposed by NeuroDB"
        ACTIVITYINFO = "activityinfo", "From an ActivityInfo master indicator"

    section = models.CharField(max_length=128, help_text="the eTools section name, as on the PD indicators")
    name = models.CharField(max_length=500)
    unit = models.CharField(max_length=64, blank=True, help_text="shown next to the figures (a label only)")
    cp_output = models.CharField(
        "CP output", max_length=500, blank=True, help_text="the country programme output it is listed under"
    )
    keywords = models.CharField(
        max_length=500, blank=True, help_text="more words that help NeuroDB suggest PD indicators for it"
    )
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.SUM)
    pd_parts_add_up = models.BooleanField(
        "parts of one PD add up",
        default=False,
        help_text=(
            "several indicators of one programme document count different people, so their figures are "
            "added; otherwise only the programme document's largest one counts"
        ),
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.MANUAL)
    proposal_key = models.CharField(
        max_length=40,
        blank=True,
        db_index=True,
        help_text="the structure key it was proposed from (a key any master holds is never proposed again)",
    )
    # the ActivityInfo master indicator it was made from (no foreign key: the archive is read-only)
    activityinfo_master_id = models.PositiveIntegerField(
        "ActivityInfo master indicator", null=True, blank=True
    )
    sequence = models.PositiveSmallIntegerField(default=0, help_text="its place in its section")
    note = models.CharField(max_length=500, blank=True)
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["section", "name"],
                condition=models.Q(status__in=["active", "archived"]),
                name="datamart_etoolsmaster_section_name",
                violation_error_message="This section already has a master indicator with this name.",
            )
        ]
        ordering = ("section", "sequence", "name")
        verbose_name = "master indicator (eTools)"
        verbose_name_plural = "master indicators (eTools)"

    def __str__(self):
        return f"{self.section}: {self.name}"

    def clean(self):
        from . import masters

        masters.check_master(self)


class EtoolsMasterYear(models.Model):
    """A master indicator's settings for one calendar year: its AWP/RWP code and the yearly target staff type.
    The status is measured against this target alone ("No target" until it is typed); the programme documents'
    own targets are shown for reference only. ``updated_by`` is for the admin only."""

    master = models.ForeignKey(EtoolsMaster, on_delete=models.CASCADE, related_name="years")
    year = models.PositiveSmallIntegerField(help_text="the calendar year")
    code = models.CharField(max_length=32, blank=True, help_text="the AWP/RWP code")
    target = models.FloatField(
        null=True, blank=True, help_text="the year's target (AWP/RWP figure) the status is measured against"
    )
    is_active = models.BooleanField(
        "shown this year", default=True, help_text="untick to leave the master out of this year"
    )
    note = models.CharField(max_length=500, blank=True)
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["master", "year"], name="datamart_etoolsmasteryear_master_year")
        ]
        ordering = ("master", "year")
        verbose_name = "master indicator year (eTools)"
        verbose_name_plural = "master indicator years (eTools)"

    def __str__(self):
        return f"{self.year}: {self.code or 'no code'}"


class EtoolsMasterLink(models.Model):
    """One partner's PD indicator in a master indicator (a sub-indicator), and what it counts as there, the
    ActivityInfo way: in the total, a numerator, a denominator, or shown only (never counted).

    The indicator is named by its programme document and its identity there (``ident``,
    :func:`neurodb.datamart.masters.ident_code`: its eTools id, else a hash of its title), never by the page's
    key, so the Datamart sync, which rewrites the indicator rows, never loses it. ``title``, ``pd_number``
    and ``unit_class`` are what the indicator was when linked: an indicator no longer in eTools, or of
    another type since, is labelled and not counted until someone confirms it again.

    A PD indicator counts in one total at most (``one_total_home``); it may still be shown, or be a numerator
    or a denominator, in another master. ``confirmed_by`` is for the admin only: never shown on a page,
    exported or sent to the AI."""

    class Role(models.TextChoices):
        TOTAL = "total", "In the total"
        NUMERATOR = "numerator", "Numerator"
        DENOMINATOR = "denominator", "Denominator"
        SHOWN = "shown", "Shown only"

    class Status(models.TextChoices):
        SUGGESTED = "suggested", "Suggested"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"

    class UnitClass(models.TextChoices):
        NUMBER = "number", "Number"
        PERCENTAGE = "percentage", "Percentage"
        RATIO = "ratio", "Ratio"
        OTHER = "other", "Other"

    master = models.ForeignKey(EtoolsMaster, on_delete=models.CASCADE, related_name="links")
    # PCA rows are updated, never deleted, by the syncs
    pd = models.ForeignKey(
        "etools.PCA",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="+",
        verbose_name="programme document",
    )
    ident = models.CharField(
        max_length=48, help_text="the indicator in its PD: s and its eTools id, else t and its title's hash"
    )
    title = models.CharField(max_length=1024, blank=True, help_text="the indicator's title when linked")
    pd_number = models.CharField("PD number", max_length=64, blank=True)
    unit_class = models.CharField(
        "type",
        max_length=12,
        choices=UnitClass.choices,
        blank=True,
        help_text=(
            "the indicator's type when confirmed: another type in eTools stops it counting until it is "
            "confirmed again"
        ),
    )
    role = models.CharField("counts as", max_length=16, choices=Role.choices, default=Role.TOTAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUGGESTED, db_index=True)
    score = models.FloatField(null=True, blank=True, help_text="how close the suggestion is (at most 1)")
    reason = models.CharField(max_length=200, blank=True, help_text="why NeuroDB suggested it")
    sequence = models.PositiveSmallIntegerField(default=0)
    confirmed_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["master", "pd", "ident"], name="datamart_etoolsmasterlink_key"),
            models.UniqueConstraint(
                fields=["pd", "ident"],
                condition=models.Q(role="total", status__in=["suggested", "confirmed"]),
                name="datamart_etoolsmasterlink_one_total_home",
                violation_error_message=(
                    "This PD indicator already counts in the total of another master indicator: it can be "
                    "in one total only (it may be shown, or be a numerator or a denominator, elsewhere)."
                ),
            ),
        ]
        indexes = [models.Index(fields=["pd", "ident"], name="datamart_etoolsmasterlink_pd")]
        ordering = ("master", "sequence", "id")
        verbose_name = "master indicator link (eTools)"
        verbose_name_plural = "master indicator links (eTools)"

    def __str__(self):
        pd = self.pd_number or f"PD {self.pd_id}"
        return f"{pd} {self.ident}: {self.get_role_display()} ({self.get_status_display()})"

    def clean(self):
        from . import masters

        masters.check_link(self)


# ------------------------------------------------------------------------------------- Compare with Power BI
class PowerBIUpload(models.Model):
    """One upload of the PRP Power BI exports for Compare with Power BI (Admin → Data and sync), and its
    comparison: a summary per Power BI page and plain sentences, never a cell's value, a file or a zip
    (``datamart.powerbi_read``). The newest :data:`powerbi_compare.KEEP_FULL` uploads keep their rows; older
    ones keep their summary only."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Waiting to start"
        READING = "reading", "Reading the files"
        COMPARING = "comparing", "Comparing"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # set on upload and on Compare again: a queued upload is aged by it (powerbi_compare.close_stale)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    compared_at = models.DateTimeField(null=True, blank=True)
    day = models.DateField(null=True, blank=True)  # the day the statuses are read for
    powerbi_as_of = models.DateTimeField(null=True, blank=True)  # Power BI's refresh, from the files
    synced_at = models.DateTimeField(
        null=True, blank=True
    )  # the partner reports sync NeuroDB's data comes from
    sync_moved = models.BooleanField(default=False)  # a partner reports sync finished during the comparison
    refused = models.JSONField(default=list, blank=True)  # plain sentences, at most 100; never a cell's value
    summary = models.JSONField(default=dict, blank=True)  # counts per Power BI page, the hidden files skipped
    choices = models.JSONField(default=list, blank=True)  # "Choices for you", with their evidence counts
    trimmed = models.BooleanField(
        default=False
    )  # its rows and lines removed (only the newest uploads keep them)
    error = models.CharField(max_length=300, blank=True)  # our sentence and the exception's class name only
    peak_memory_mb = models.FloatField(null=True, blank=True)
    seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ("-uploaded_at", "-id")
        verbose_name = "Power BI upload"

    def __str__(self):
        return f"Power BI upload {self.pk} ({self.get_status_display()})"


class PowerBIExport(models.Model):
    """One Power BI export of an upload, recognised by its columns: its rows as NeuroDB compares them (gzip
    JSON lines of the kind's columns, never a column that names people), its parsed filters and plain notes.
    The repacked copy waiting for the job (``content``) is emptied as soon as it is read."""

    upload = models.ForeignKey(PowerBIUpload, on_delete=models.CASCADE, related_name="exports")
    order = models.PositiveSmallIntegerField()  # Power BI's page order, then the visual's
    kind = models.CharField(max_length=30)  # powerbi_exports.KINDS
    file_name = models.CharField(max_length=255)  # its base name, cleaned
    state = models.CharField(max_length=12)  # staged, read, compared, empty, not_compared, failed
    sheet = models.CharField(max_length=40, blank=True)  # the sheet it is read from
    content = models.BinaryField(null=True, editable=False)  # the repacked one-sheet copy, until read
    rows = models.BinaryField(null=True, editable=False)  # gzip JSON lines: the kept columns only
    rows_read = models.PositiveIntegerField(default=0)
    set_aside = models.PositiveIntegerField(default=0)  # rows about periods before NeuroDB's history
    columns = models.JSONField(default=list, blank=True)  # the kept columns' names, in the rows' order
    dropped = models.JSONField(default=list, blank=True)  # sentences: the columns that name people, dropped
    not_compared = models.JSONField(default=list, blank=True)  # known captions not kept
    unknown_columns = models.PositiveSmallIntegerField(default=0)  # captions NeuroDB does not know (counted)
    filters = models.JSONField(default=dict, blank=True)  # the parsed filter (powerbi_filters.Filter)
    clauses = models.JSONField(default=list, blank=True)  # each clause's words and how NeuroDB applies it
    counts = models.JSONField(default=dict, blank=True)
    fits = models.JSONField(default=list, blank=True)
    verdict = models.CharField(max_length=30, blank=True)
    notes = models.JSONField(default=list, blank=True)  # plain sentences
    error = models.CharField(max_length=300, blank=True)  # our sentence and the exception's class name only

    class Meta:
        ordering = ("upload", "order", "id")
        verbose_name = "Power BI export"

    def __str__(self):
        return f"{self.kind} ({self.file_name})"


class PowerBIDifference(models.Model):
    """One difference between a Power BI export's row and NeuroDB's: its values as shown (never a person), its
    reason and what to do. Written by the comparison, at most ``powerbi_compare.LINE_LIMIT`` per export."""

    export = models.ForeignKey(PowerBIExport, on_delete=models.CASCADE, related_name="differences")
    n = models.PositiveIntegerField()  # its order within the export
    group = (
        models.PositiveSmallIntegerField()
    )  # 1 expected, 2 wait for the sync, 3 check the PD, 4 needs a look
    kind = models.CharField(max_length=12)  # different, only_powerbi, only_neurodb
    reason = models.CharField(max_length=20)
    rule = models.CharField(max_length=20, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    match = models.JSONField(default=list, blank=True)  # the values the row is matched by, as shown
    found = models.JSONField(default=dict, blank=True)  # {column: [Power BI, NeuroDB, another reading]}
    link = models.CharField(max_length=300, blank=True)  # "Check in NeuroDB"; empty when withheld

    class Meta:
        indexes = [models.Index(fields=["export", "group", "reason", "n"], name="datamart_powerbidiff_group")]
        verbose_name = "Power BI difference"

    def __str__(self):
        return f"{self.kind}: {self.reason}"
