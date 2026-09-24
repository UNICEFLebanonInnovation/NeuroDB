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
    numerator_label = models.CharField(max_length=256, blank=True)
    denominator_label = models.CharField(max_length=256, blank=True)
    means_of_verification = models.CharField(max_length=255, blank=True)
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

    class Meta:
        ordering = ("-date",)
        verbose_name = "staff trip activity"

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
    agreements) and of the per-type audit records. ``record_key`` is the Datamart id, or a hash of the
    record for the few datasets without one. Contact details (e-mail addresses, phone numbers) are
    removed before storing.
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
