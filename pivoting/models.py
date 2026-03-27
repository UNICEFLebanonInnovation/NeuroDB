from django.utils.translation import gettext_lazy as _
from model_utils import Choices
from django.db import models
from django.conf import settings
from model_utils.models import TimeStampedModel
from django.db.models import JSONField
from django.template.defaultfilters import truncatechars  # or truncatewords
from users.models import Section
from django.contrib.postgres.fields import ArrayField
import base64
from django.core.exceptions import ValidationError

REPORTING_LEVELS = (
    ("DISTRICT", "DISTRICT"),
    ("CADASTRAL", "CADASTRAL"),
    ("GOVERNORATE", "GOVERNORATE"),
    ("NATIONAL", "NATIONAL"),
    ("GATEWAY", "DISTRICT"),
    ("INSTITUTIONAL", "INSTITUTIONAL"),
    ("SITE_LEVEL", "SITE_LEVEL"),
    ("MUNICIPALITY", "MUNICIPALITY"),
)
MASTER_AGGREGATION_METHODS = (
    ("SUM", "SUM"),
    ("AVERAGE", "AVERAGE"),
    ("MINIMUM", "MINIMUM"),
    ("MAXIMUM", "MAXIMUM"),
    ("SUM_OVER_SUM", "SUM_OVER_SUM"),
    ("COUNT", "COUNT"),
)
SUB_AGGREGATION_METHODS = (
    ("SUM", "SUM"),
    ("AVERAGE", "AVERAGE"),
)
SUB_MASTER_EFFECT = (
    ("TOTAL", "TOTAL"),
    ("NUMERATOR", "NUMERATOR"),
    ("DENOMINATOR", "DENOMINATOR"),
    ("NO_EFFECT", "NO_EFFECT"),
)

MONTHS = [
    ("01", "Jan"),
    ("02", "Feb"),
    ("03", "Mar"),
    ("04", "Apr"),
    ("05", "May"),
    ("06", "Jun"),
    ("07", "Jul"),
    ("08", "Aug"),
    ("09", "Sep"),
    ("10", "Oct"),
    ("11", "Nov"),
    ("12", "Dec"),
]

age_groups = [
    {"substrings": ["<=18", "0-18", "<18 or below18"], "value": "<=18"},
    {"substrings": ["1-18 years"], "value": "1-18"},
    {"substrings": ["3-7 years", "3-7"], "value": "3-7"},
    {"substrings": ["Under18", "Under 18", "Under-18", "Age<18", "U18", "<18"], "value": "<18"},
    {"substrings": ["Under 5", "Under5", "<5"], "value": "<5"},
    {"substrings": ["<60"], "value": "<60"},
    {"substrings": ["12+", "+12",], "value": ">12"},
    {"substrings": ["+14", "14+"], "value": ">14"},
    {"substrings": [">=18", "18+", "+18", ">18 or above18"], "value": ">=18"},
    {"substrings": ["5 and above", "+5", "5+", ">=5"], "value": ">=5"},
    {"substrings": ["60 and above", ">=60"], "value": ">=60"},
    {"substrings": [">14", "+14", "14+", "Above 14", "Above14"], "value": ">14"},
    {"substrings": ["Above18", "Above 18", "Above-18", "Age>18", ">18"], "value": ">18"},
    {"substrings": ["Above5", "Above 5", ">5"], "value": ">5"},
    {"substrings": [">60", "Above60", "Above 60",], "value": ">60"},
    {"substrings": [" 0-17 ", "_0-17_"], "value": "0-17"},
    {"substrings": ["10-14"], "value": "10-14"},
    {"substrings": ["12-17"], "value": "12-17"},
    {"substrings": ["12-18"], "value": "12-18"},
    {"substrings": ["14-17"], "value": "14-17"},
    {"substrings": ["14-18"], "value": "14-18"},
    {"substrings": ["15-17"], "value": "15-17"},
    {"substrings": ["15-18"], "value": "15-18"},
    {"substrings": ["15-19"], "value": "15-19"},
    {"substrings": ["15-20"], "value": "15-20"},
    {"substrings": ["18-24"], "value": "18-24"},
    {"substrings": ["18-25"], "value": "18-25"},
    {"substrings": ["18-59"], "value": "18-59"},
    {"substrings": ["20-24"], "value": "20-24"},
    {"substrings": ["25-49"], "value": "25-49"},
    {"substrings": ["26-59"], "value": "26-59"},
    {"substrings": [" 3-5 ", "_3-5_", "(3-5)"], "value": "3-5"},
    {"substrings": ["50-64"], "value": "50-64"},
    {"substrings": ["5-17"], "value": "5-17"},
    {"substrings": ["5-18"], "value": "5-18"},
    {"substrings": ["6-10"], "value": "6-10"},
    {"substrings": ["6-11"], "value": "6-11"},
    {"substrings": ["6-13"], "value": "6-13"},
    {"substrings": ["6-14"], "value": "6-14"},
    {"substrings": ["6-15"], "value": "6-15"},
    {"substrings": ["6-18"], "value": "6-18"},
    {"substrings": ["0-59 months"], "value": "0-59 months"},
    {"substrings": ["6-59 months"], "value": "6-59 months"},
    {"substrings": ["0-23 months"], "value": "0-23 months"},
    {"substrings": ["6-23 months"], "value": "6-23 months"},
    {"substrings": ["6-9"], "value": "6-9"},
    {"substrings": [" 0-1 ", "_0-1_"], "value": "0-1"},
    {"substrings": [" 0-19 ", "_0-19_"], "value": "<=19"},
    {"substrings": [" 0-3 ", "_0-3_"], "value": "0-3"},
    {"substrings": [" 0-4 ", "_0-4_"], "value": "0-4"},
    {"substrings": [" 0-5 ", "_0-5_", "<=5"], "value": "0-5"},
]


nationalities = [
    {"substrings": ["non_leb", "non-leb", "non lebanese"], "value": "NONLEB"},
    {"substrings": ["_syr"], "value": "SYR"},
    {"substrings": ["_leb", "of lebanese"], "value": "LEB"},
    {"substrings": ["_prs"], "value": "PRS"},
    {"substrings": ["_prl"], "value": "PRL"},
    {"substrings": ["_oth", "_mig"], "value": "OTH"},
]


def map_tags(input_string, mappings):
    # Convert the input string to lowercase and strip extra spaces for consistent comparison
    input_string = input_string.lower().strip()
    
    # Iterate through each mapping rule
    for mapping in mappings:
        # Check each substring for the current mapping
        for substring in mapping["substrings"]:
            # Normalize substring for consistent comparison
            normalized_substring = substring.lower().strip()
            # Use the 'in' operator to check if the substring is in the input string
            if normalized_substring in input_string:
                return mapping["value"]
    
    # If no mapping matches, return the original input or a default value
    return None


def is_substring(string, substrings):
    for substring in substrings:
        if substring in string:
            return True
    return False


class ReportingYear(models.Model):
    name = models.CharField(max_length=254)
    year = models.CharField(max_length=254, null=True)
    current = models.BooleanField(default=False)
    database_id = models.CharField(max_length=254, null=True, blank=True)
    form_id = models.CharField(max_length=254, null=True, blank=True)
    old_id = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
    
    def clean(self):
        if self.current:
            if ReportingYear.objects.exclude(pk=self.pk).filter(current=True).exists():
                raise ValidationError("Only one record can be current at a time.")

    def save(self, *args, **kwargs):
        self.full_clean()  # Run clean() before saving
        super().save(*args, **kwargs)
    


class Database(models.Model):
    ai_id = models.PositiveIntegerField(unique=True, verbose_name="ID")
    parent_id = models.CharField(
        max_length=254,
        null=True,
        blank=True,
        # unique=True,
        verbose_name="ActivityInfo Parent ID",
    )
    db_id = models.CharField(
        max_length=254,
        null=True,
        blank=True,
        # unique=True,
        verbose_name="ActivityInfo ID",
    )
    name = models.CharField(max_length=254)
    sector_label = models.CharField(max_length=254, blank=True, null=True)
    label = models.CharField(max_length=254, null=True, blank=True)
    hpm_label = models.CharField(max_length=254, null=True, blank=True)
    hpm_sequence = models.PositiveIntegerField(default=0)
    username = models.CharField(max_length=254)
    password = models.CharField(max_length=254)
    focal_point = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="+",
    )
    focal_point_sector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="+",
    )

    # read only fields
    description = models.CharField(max_length=1500, null=True)
    country_name = models.CharField(max_length=254, null=True)
    dashboard_link = models.URLField(max_length=1500, null=True, blank=True)
    ai_country_id = models.PositiveIntegerField(null=True)
    section = models.ForeignKey(
        Section,
        on_delete=models.SET_NULL,
        related_name="pdatabases",
        null=True,
        blank=True,
    )
    mapped_db = models.BooleanField(default=True)
    mapping_extraction1 = JSONField(blank=True, null=True)
    mapping_extraction2 = JSONField(blank=True, null=True)
    mapping_extraction3 = JSONField(blank=True, null=True)
    is_funded_by_unicef = models.BooleanField(default=False)
    display = models.BooleanField(
        default=True,
        verbose_name="Show on dashboard"
    )
    is_sector = models.BooleanField(default=False)
    # used to differentiate between databases
    # that need extraction till current month
    is_current_extraction = models.BooleanField(default=False)
    have_partners = models.BooleanField(default=True)
    have_governorates = models.BooleanField(default=True)
    have_covid = models.BooleanField(default=True)
    have_offices = models.BooleanField(default=False)
    have_internal_reporting = models.BooleanField(default=True)
    support_covid = models.BooleanField(default=False)
    have_sections = models.BooleanField(default=False)
    configs = JSONField(blank=True, null=True, default=dict)
    reporting_year = models.ForeignKey(
        ReportingYear, on_delete=models.SET_NULL, blank=True, null=True
    )
    old_id = models.PositiveIntegerField(default=0)
    year = models.CharField(
        max_length=250,
        blank=True,
        null=True,
        choices=Choices(
            ("2017", "2017"),
            ("2018", "2018"),
            ("2019", "2019"),
            ("2020", "2020"),
            ("2021", "2021"),
            ("2022", "2022"),
            ("2023", "2023"),
            ("2024", "2024"),
            ("2025", "2025"),
            ("2026", "2026"),
            ("2027", "2027"),
            ("2028", "2028"),
            ("2029", "2029"),
            ("2030", "2030"),
        ),
    )
    last_update_date = models.DateField(blank=True, null=True)
    last_live_update_date = models.DateTimeField(blank=True, null=True)
    last_monthly_update_date = models.DateTimeField(blank=True, null=True)
    last_weekly_update_date = models.DateTimeField(blank=True, null=True)

    @property
    def reporting_year_name(self):
        if self.reporting_year:
            return self.reporting_year.year
        return ""

    @property
    def full_name(self):
        return "{} - {}".format(self.name, self.reporting_year_name)

    def __str__(self):
        return self.full_name

    class Meta:
        ordering = ["name", "-ai_id"]


class Activity(models.Model):
    ai_id = models.PositiveIntegerField(null=True, blank=True)
    ai_form_id = models.CharField(max_length=254, null=True)
    database = models.ForeignKey(
        Database,
        on_delete=models.CASCADE,
    )
    none_ai_database = models.ForeignKey(
        Database,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="+",
        verbose_name="TBD",
    )
    name = models.CharField(max_length=1500, verbose_name="Form Name")
    label = models.CharField(max_length=1500, verbose_name="Form Name/Label")
    location_type = models.CharField(max_length=254, null=True)
    programme_document = models.ForeignKey(
        "etools.pca", on_delete=models.SET_NULL,
        blank=True, null=True, related_name="+"
    )
    programme_documents = models.ManyToManyField(
        "etools.pca", blank=True, related_name="+"
    )
    category = models.CharField(
        max_length=254, blank=True, null=True, verbose_name="AI Folder"
    )
    ai_category_id = models.CharField(
        max_length=254, null=True, blank=True, verbose_name="AI Folder ID"
    )

    def __str__(self):
        return "{}-{}-{}".format(self.database.ai_id, self.name, self.database.name)
        # return '{} - {}'.format(truncatechars(self.name, 100), self.database.name)

    @property
    def database_name(self):
        return self.database.name

    @property
    def database_reporting_year(self):
        if self.database.reporting_year:
            return self.database.reporting_year.name
        return ""

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "activities"


AGE_GROUPS = (
    ("0-1", "0-1"),
    ("0-3", "0-3"),
    ("0-4", "0-4"),
    ("0-5", "0-5"),
    ("10-14", "10-14"),
    ("12-17", "12-17"),
    ("14-17", "14-17"),
    ("15-17", "15-17"),
    ("15-19", "15-19"),
    ("15-20", "15-20"),
    (">=18", ">=18"),
    ("18-24", "18-24"),
    ("18-25", "18-25"),
    ("18-59", "18-59"),
    ("20-24", "20-24"),
    ("25-49", "25-49"),
    ("26-59", "26-59"),
    ("3-5", "3-5"),
    ("5-17", "5-17"),
    ("50-64", "50-64"),
    ("6-11", "6-11"),
    ("6-13", "6-13"),
    ("6-59 months", "6-59 months"),
    (
        "6-9",
        "6-9",
    ),
    (">=60", ">=60"),
    (">18", ">18"),
    (">5", ">5"),
    (">60", ">60"),
    ("<18", "<18"),
    ("<5", "<5"),
    ("<60" "<60"),
)


def contains_element(string, elements):
    for element in elements:
        if element in string:
            return True
    return False


class IndicatorNew(models.Model):
    ai_indicator = models.CharField(max_length=30, blank=True, null=True)
    database = models.ForeignKey(
        Database,
        on_delete=models.CASCADE,
    )
    activity = models.ForeignKey(
        Activity,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="activity_form_indicators",
    )
    name = models.CharField(max_length=500)
    label = models.CharField(max_length=500, blank=True, null=True)
    description = models.CharField(max_length=500, blank=True, null=True)
    awp_code = models.CharField(
        max_length=500, blank=True, null=True, verbose_name="RWP Code"
    )
    type = models.CharField(max_length=50, blank=True, null=True)
    units = models.CharField(max_length=50, blank=True, null=True)
    gender = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        choices=Choices(
            ("male", "Male"),
            ("female", "Female"),
        ),
    )
    age_group = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=Choices(
            ("0-1", "0-1"),
            ("0-3", "0-3"),
            ("0-4", "0-4"),
            ("0-5", "0-5"),
            ("10-14", "10-14"),
            ("12-17", "12-17"),
            ("14-17", "14-17"),
            ("15-17", "15-17"),
            ("15-19", "15-19"),
            ("15-20", "15-20"),
            (">=18", ">=18"),
            ("18-24", "18-24"),
            ("18-25", "18-25"),
            ("18-59", "18-59"),
            ("20-24", "20-24"),
            ("25-49", "25-49"),
            ("26-59", "26-59"),
            ("3-5", "3-5"),
            ("5-17", "5-17"),
            ("50-64", "50-64"),
            ("6-11", "6-11"),
            ("6-13", "6-13"),
            ("6-59 months", "6-59 months"),
            (
                "6-9",
                "6-9",
            ),
            (">=60", ">=60"),
            (">18", ">18"),
            (">5", ">5"),
            (">60", ">60"),
            ("<18", "<18"),
            ("<5", "<5"),
            ("<60" "<60"),
        ),
    )
    programme = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        choices=Choices(
            ("ALP", "ALP"),
            ("BLN", "BLN"),
            ("ABLN", "ABLN"),
            ("CBECE", "CBECE"),
        ),
    )
    disability = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        choices=Choices(
            ("Speaking", "Speaking"),
            ("Intellectual", "Intellectual"),
            ("Audio", "Audio (Hearing)"),
            ("Visual", "Visual (Seeing)"),
            ("Motor", "Motor/Mobility"),
        ),
    )
    nationality = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        choices=Choices(
            ("LEB", "Lebanese"),
            ("PRL", "Palestinian from Lebanon"),
            ("PRS", "Palestinian from Syria"),
            ("SYR", "Syrian"),
            ("OTH", "Other Nationality"),
        ),
    )

    class Meta:
        verbose_name = "indicator"
        verbose_name_plural = "indicators"
        ordering = ['nationality', 'awp_code',]

    def __str__(self):
        # return '{} - {}'.format(self.database.id, self.name)
        return "{} - {} [{}]".format(self.database.id, self.name, self.ai_indicator)

    def set_tags(self):
        lcname = self.name.lower()

        self.nationality = map_tags(lcname, nationalities)
        self.age_group = map_tags(lcname, age_groups)

        if "_male" in lcname:
            self.gender = "Male"
        elif "_female" in lcname:
            self.gender = "Female"

        if "_BLN" in self.name:
            self.programme = "BLN"
        elif "_ALP" in self.name:
            self.programme = "ALP"
        elif "_CBECE" in self.name:
            self.programme = "CBECE"

        if "_speaking" in lcname:
            self.disability = "Speaking"
        elif "_intellectual" in lcname:
            self.disability = "Intellectual"
        elif "_audio" in lcname:
            self.disability = "Audio"
        elif "_visual" in lcname:
            self.disability = "Visual"
        elif "_motor" in lcname:
            self.disability = "Motor"
        elif "_mobility" in lcname:
            self.disability = "Motor"

        self.save()

    def database_name(self):
        return self.database.__str__()

    database_name.short_description = _("Database")

    # def save(self):
    #     self.awp_code = self.awp_code.split(' ')[0]
    #     return super().save()


class SubIndicator(models.Model):
    database = models.ForeignKey(
        Database,
        on_delete=models.CASCADE,
    )
    activity = models.ForeignKey(
        Activity, on_delete=models.SET_NULL, blank=True, null=True
    )
    name = models.CharField(max_length=500)
    awp_code = models.CharField(max_length=500, verbose_name="RWP Code")
    target = models.PositiveIntegerField(blank=True, null=True)
    indicators = models.ManyToManyField(
        IndicatorNew, blank=True, verbose_name=_("Indicators")
    )
    aggregation_method = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=SUB_AGGREGATION_METHODS,
        default="SUM",
    )
    old_id = models.PositiveIntegerField(default=0)
    sequence = models.PositiveIntegerField(default=0)
    sector_equivalent = models.CharField(max_length=500, blank=True, null=True)

    class Meta:
        ordering = ["awp_code", "sequence"]

    def __str__(self):
        return "{}-{}".format(self.database.id, self.name)

    def clean(self):
        if self.activity and self.database != self.activity.database:
            raise Exception(
                "The selected activity database does not match the selected database"
            )
        return super().clean()

    @property
    def activity_indicators(self):
        # Get the number of available ManyToManyModel records
        return IndicatorNew.objects.filter(activity=self.activity).count()

    @property
    def chosen_indicators(self):
        # Get the number of chosen ManyToManyModel records based on the foreign key value
        return self.indicators.count()


class MasterIndicatorTag(models.Model):
    name = models.CharField(max_length=20)

    class Meta:
        verbose_name = _("Tag")
        verbose_name_plural = _("Tags")

    def __str__(self):
        return self.name


class MasterIndicator(models.Model):
    database = models.ForeignKey(
        Database,
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=5000)
    awp_code = models.CharField(max_length=500, verbose_name="RWP Code")
    aggregation_method = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=MASTER_AGGREGATION_METHODS,
        default="SUM",
    )
    awp_target = models.PositiveIntegerField(default=0, blank=True, null=True)
    indicator_type = models.CharField(
        max_length=250, blank=True, null=True, verbose_name="Category"
    )
    reporting_level = models.CharField(
        max_length=254, blank=True, null=True, choices=REPORTING_LEVELS
    )
    ram_result = models.PositiveIntegerField(
        default=0, verbose_name="RAM Result", blank=True, null=True
    )
    tags = models.ManyToManyField(MasterIndicatorTag, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    old_id = models.PositiveIntegerField(default=0)
    sequence = models.PositiveIntegerField(default=0)
    unit = models.CharField(max_length=100, blank=True, null=True)
    sector_equivalent = models.CharField(max_length=500, blank=True, null=True)

    class Meta:
        ordering = ["awp_code"]

    def __str__(self):
        return "{}-{}".format(self.database.id, self.name)

    @property
    def subIndicatorsCount(self):
        cnt = self.subindicators.all().count()
        return cnt if cnt > 9 else "0" + str(cnt)

    def create_add_indicators_wizard(self):
        wizard = AddSubIndicatorsWizard.objects.create(master=self)
        return wizard

    def _add_indicators(self, indicators, effect):
        for indicator in indicators:
            if MasterSubIndicator.objects.filter(master=self, sub=indicator).exists():
                continue
            master_sub = MasterSubIndicator(
                master=self,
                sub=indicator,
                effect=effect,
                label=indicator.name,
                target=indicator.target,
            )
            master_sub.save()


class AddSubIndicatorsWizard(models.Model):
    master = models.ForeignKey(
        MasterIndicator, on_delete=models.CASCADE, verbose_name=_("Master Indicator")
    )
    effect = models.CharField(
        max_length=254,
        blank=True,
        null=True,
        choices=SUB_MASTER_EFFECT,
        default="TOTAL",
        verbose_name="Effect on Master value",
    )
    indicators = models.ManyToManyField(
        SubIndicator, verbose_name=_("Sub Indicators"), blank=True
    )

    class Meta:
        verbose_name = _("Select Sub Indicators Wizard")
        verbose_name_plural = _("Select Sub Indicators Wizard")

    def confirm_action(self):
        self.master._add_indicators(self.indicators.all(), self.effect)
        self.delete()
        return


class MasterSubIndicator(models.Model):
    master = models.ForeignKey(
        MasterIndicator,
        on_delete=models.CASCADE,
        verbose_name="Master indicator",
        related_name="subindicators",
    )
    sub = models.ForeignKey(
        SubIndicator, on_delete=models.CASCADE, verbose_name="Sub indicator"
    )
    listed = models.BooleanField(default=True, verbose_name="Dashboard")
    effect = models.CharField(
        max_length=254,
        blank=True,
        null=True,
        choices=SUB_MASTER_EFFECT,
        default="TOTAL",
        verbose_name="Effect",
    )
    label = models.CharField(max_length=500, null=True, blank=True)
    target = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
    )
    sequence = models.PositiveIntegerField(default=0, verbose_name="Position")

    class Meta:
        ordering = ["sequence"]


class NeuroReport(models.Model):
    name = models.CharField(max_length=5000)
    report_code = models.CharField(
        max_length=20,
        default="",
        help_text="Used to identify the similar reports that are defined for different reporting years",
    )
    ryear = models.ForeignKey(
        ReportingYear, on_delete=models.SET_NULL, blank=True, null=True
    )
    is_active = models.BooleanField(default=True)
    is_hpm = models.BooleanField(
        default=False, help_text="HPM reports are viewed using HPM report template"
    )

    def __str__(self):
        return "{}-{}".format(self.ryear, self.name)

    def create_add_indicators_wizard(self):
        wizard = AddMasterIndicatorsWizard.objects.create(report=self)
        return wizard

    def _add_indicators(self, indicators, tags, add_by):
        if add_by == "Tags":
            for tag in tags:
                for indicator in MasterIndicator.objects.filter(
                    tags=tag, database__reporting_year=self.ryear
                ):
                    if NeuroReportMasterIndicator.objects.filter(
                        report=self, master=indicator
                    ).exists():
                        continue
                    report_master = NeuroReportMasterIndicator(
                        report=self,
                        master=indicator,
                        label=indicator.name,
                        target=indicator.awp_target,
                        ram_result=indicator.ram_result,
                    )
                    report_master.save()
        else:
            for indicator in indicators:
                if NeuroReportMasterIndicator.objects.filter(
                    report=self, master=indicator
                ).exists():
                    continue
                report_master = NeuroReportMasterIndicator(
                    report=self,
                    master=indicator,
                    label=indicator.name,
                    target=indicator.awp_target,
                    ram_result=indicator.ram_result,
                )
                report_master.save()


class AddMasterIndicatorsWizard(models.Model):
    report = models.ForeignKey(
        NeuroReport, on_delete=models.CASCADE, verbose_name=_("Neuro Report")
    )
    add_by = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=(("Tags", "Tags"), ("Selection", "Selection")),
        default="Tags",
        verbose_name="Add By",
    )
    indicators = models.ManyToManyField(
        MasterIndicator, verbose_name=_("Master Indicators"), blank=True
    )
    tags = models.ManyToManyField(MasterIndicatorTag, blank=True, null=True)

    class Meta:
        verbose_name = _("Select Master Indicators Wizard")
        verbose_name_plural = _("Select Master Indicators Wizard")

    def confirm_action(self):
        self.report._add_indicators(self.indicators.all(), self.tags.all(), self.add_by)
        self.delete()
        return


class NeuroReportMasterIndicator(models.Model):
    report = models.ForeignKey(
        NeuroReport, on_delete=models.CASCADE, blank=True, null=True
    )
    master = models.ForeignKey(
        MasterIndicator, on_delete=models.CASCADE, blank=True, null=True
    )
    label = models.CharField(
        max_length=5000, blank=True, null=True, verbose_name="Label"
    )
    category = models.CharField(
        max_length=100, blank=True, null=True, verbose_name="Category"
    )
    target = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
    )
    ram_result = models.PositiveIntegerField(
        default=0, blank=True, null=True, verbose_name="RAM Result"
    )

    @property
    def section(self):
        return self.master.database
    
    def __str__(self):
        return self.label


class NeuroReportComment(models.Model):
    report = models.ForeignKey(
        NeuroReport, on_delete=models.CASCADE, blank=True, null=True
    )
    master = models.ForeignKey(
        NeuroReportMasterIndicator, on_delete=models.CASCADE, blank=True, null=True
    )
    comment = models.CharField(max_length=5000, blank=True, null=True)
    related_month = models.CharField(max_length=2, default="01", choices=MONTHS)
    entry_date = models.DateTimeField(auto_now_add=True)
    last_update = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "comment"
        verbose_name_plural = "comments"

    def __str__(self):
        if len(self.comment) <= 50:
            return self.comment
        return self.comment[0:50] + "..."


class ActivityReportNew(TimeStampedModel):
    form = models.CharField(max_length=1000, blank=True, null=True, db_index=True)
    indicator_id = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    indicator_name = models.CharField(
        max_length=1000, blank=True, null=True, db_index=True
    )
    indicator_value = models.FloatField(verbose_name="Value", blank=True, null=True)
    indicator_awp_code = models.CharField(
        max_length=254, blank=True, null=True, db_index=True
    )
    location_adminlevel_cadastral_area = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    location_adminlevel_cadastral_area_code = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    location_adminlevel_caza = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    location_adminlevel_caza_code = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    location_adminlevel_governorate = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    location_adminlevel_governorate_code = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    location_alternate_name = models.CharField(max_length=250, blank=True, null=True)
    location_latitude = models.CharField(max_length=250, blank=True, null=True)
    location_longitude = models.CharField(max_length=250, blank=True, null=True)
    location_name = models.CharField(
        max_length=250, verbose_name="Location", blank=True, null=True
    )
    partner_description = models.CharField(max_length=250, blank=True, null=True)
    partner_id = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    partner_label = models.CharField(
        max_length=250, verbose_name="Partner", blank=True, null=True, db_index=True
    )
    project_description = models.CharField(max_length=250, blank=True, null=True)
    project_label = models.CharField(
        max_length=250, verbose_name="Project", blank=True, null=True, db_index=True
    )
    funded_by = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    report_id = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    database_ai_id = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    dbase = models.ForeignKey(
        Database,
        on_delete=models.CASCADE,
    )
    ai_folder = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    month = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    month_name = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    reporting_section = models.CharField(
        max_length=250, blank=True, null=True, db_index=True
    )
    support_covid = models.BooleanField(default=False)
    last_edited_time = models.DateTimeField(blank=True, null=True)
    parent_form = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    project_plan = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    project = models.CharField(max_length=250, blank=True, null=True, db_index=True)
    emergency = models.CharField(max_length=10, default="no", blank=True, null=True)
    
    @property
    def _form(self):
        return truncatechars(self.form, 40)

    @property
    def _ai_folder(self):
        return truncatechars(self.ai_folder, 40)

    @property
    def _indicator(self):
        return "{}/{}/{}".format(
            self.indicator_id,
            self.indicator_awp_code,
            truncatechars(self.ai_indicator, 40),
        )


class Cadasters(models.Model):
    GOV_CODE = models.CharField(max_length=50, db_index=True)
    DISTRICT_C = models.CharField(max_length=50, db_index=True)
    CAS_CODE = models.CharField(max_length=50, db_index=True)
    CADASTER_N = models.CharField(max_length=50, db_index=True)
    GOV_N = models.CharField(max_length=50, db_index=True)
    COUNTRY_N = models.CharField(max_length=50, db_index=True)
    COUNTRY_C = models.CharField(max_length=50, db_index=True)
    GOV_C = models.CharField(max_length=50, db_index=True)
    DISTRICT_N = models.CharField(max_length=50, db_index=True)
    MUNI_N = models.CharField(max_length=50, db_index=True)
    MUNI_C = models.CharField(max_length=50, db_index=True)
    MUNI_C_1 = models.CharField(max_length=50, db_index=True)
    MUNI_C_UPD = models.CharField(max_length=50, db_index=True)
    NAME = models.CharField(max_length=200, db_index=True)
    WKT = models.CharField(max_length=50, db_index=True)
    polygon = ArrayField(
        models.CharField(
            max_length=64500,
            blank=True,
        ),
        blank=True,
        null=True,
    )

    class Meta:
        verbose_name = "Cadaster"
        verbose_name_plural = "Cadasters"

    def __str__(self):
        return self.NAME


class CadasterLocation(models.Model):
    code = models.CharField(max_length=10, db_index=True)
    gov_code = models.CharField(max_length=10, db_index=True)
    dist_code = models.CharField(max_length=10, db_index=True)
    name = models.CharField(max_length=100, db_index=True)
    polygon_coordinates = ArrayField(
        models.CharField(
            max_length=64500,
            blank=True,
        ),
        blank=True,
        null=True,
    )
    fill_color = models.CharField(max_length=10, db_index=True, blank=True, null=True)


class DistrictLocation(models.Model):
    code = models.CharField(max_length=10, db_index=True)
    gov_code = models.CharField(max_length=10, db_index=True)
    name = models.CharField(max_length=100, db_index=True)
    polygon_coordinates = ArrayField(
        models.CharField(
            max_length=64500,
            blank=True,
        ),
        blank=True,
        null=True,
    )
    fill_color = models.CharField(max_length=10, db_index=True, blank=True, null=True)


class GovernorateLocation(models.Model):
    code = models.CharField(max_length=10, db_index=True)
    name = models.CharField(max_length=100, db_index=True)
    polygon_coordinates = ArrayField(
        models.CharField(
            max_length=64500,
            blank=True,
        ),
        blank=True,
        null=True,
    )
    fill_color = models.CharField(max_length=10, db_index=True, blank=True, null=True)
    ai_id = models.PositiveSmallIntegerField()


class ResourceType(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ("name",)
        verbose_name = _("Resource Type")
        verbose_name_plural = _("Resource Types")

    def __str__(self):
        return self.name


class ResourceTopic(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ("name",)
        verbose_name = _("Resource Topic")
        verbose_name_plural = _("Resource Topics")

    def __str__(self):
        return self.name


class ResourceTag(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        ordering = ("name",)
        verbose_name = _("Resource Tag")
        verbose_name_plural = _("Resource Tags")

    def __str__(self):
        return self.name


class Resource(models.Model):
    SECTOR_CHOICES = (
        ("Basic Assistance", "Basic Assistance"),
        ("Child Protection", "Child Protection"),
        ("Communication", "Communication"),
        ("Drivers", "Drivers"),
        ("Education", "Education"),
        ("Field Operations", "Field Operations"),
        ("Field Ops", "Field Ops"),
        ("Food Security", "Food Security"),
        ("Health and Nutrition", "Health and Nutrition"),
        ("Humanitarian Coordination", "Humanitarian Coordination"),
        ("Livelihoods", "Livelihoods"),
        ("Operations", "Operations"),
        ("PRIME", "PRIME"),
        ("Palestinian Programme", "Palestinian Programme"),
        ("Representative Office", "Representative Office"),
        ("SBC", "SBC"),
        ("SGBV", "SGBV"),
        ("Security", "Security"),
        ("Social Policy", "Social Policy"),
        ("Social Protection", "Social Protection"),
        ("Social Stability", "Social Stability"),
        ("WASH", "WASH"),
        ("Winter", "Winter"),
        ("Youth & Adolescent", "Youth & Adolescent"),
        (
            "Cross-sectoral (Gender, Disability, Youth, etc.)",
            "Cross-sectoral (Gender, Disability, Youth, etc.)",
        ),
    )

    title = models.CharField(
        "Title",
        max_length=500,
        blank=False,
        null=False,
        help_text="The full title of the research",
    )

    description = models.TextField(
        "Description/Abstract",
        blank=True,
        null=True,
        max_length=1500,
        help_text="A brief narative on the resource objectives.",
    )

    publication_year = models.CharField(
        "Year of publication",
        max_length=10,
        blank=False,
        null=False,
        help_text="The year the research was published or made available for public use",
    )

    type = models.ForeignKey(
        ResourceType,
        verbose_name="Resource Type",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )

    topic = models.ForeignKey(
        ResourceTopic,
        verbose_name="Resource Topic",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )

    section = models.CharField(
        "Program",
        max_length=100,
        blank=False,
        null=False,
        choices=SECTOR_CHOICES,
    )

    tags = models.ManyToManyField(ResourceTag, blank=True, null=True)

    resource_link = models.URLField(
        "Resource Link",
        blank=True,
        null=True,
        max_length=2500,
        help_text="URL link to access the resource",
    )

    resource_file = models.BinaryField(
        "Resource File", blank=True, null=True, editable=True
    )

    resource_file_name = models.CharField(
        "Resource File",
        max_length=500,
        blank=True,
        null=True,
    )

    resource_image = models.BinaryField(
        "Resource Cover Image", blank=True, null=True, editable=True
    )

    resource_image_name = models.CharField(
        "Resource Cover Image",
        max_length=500,
        blank=True,
        null=True,
    )

    published = models.BooleanField(default=True,)
    
    class Meta:
        ordering = ("-id",)
        verbose_name = "Resource"
        verbose_name_plural = "Resources"

    def __str__(self):
        return self.title

    @property
    def get_resource_file(self):
        object_data = base64.b64encode(self.resource_file).decode("utf-8")
        return object_data

    @property
    def get_resource_image(self):
        object_data = base64.b64encode(self.resource_image).decode("utf-8")
        return object_data


class SimpleLocation(models.Model):
    name = models.CharField(verbose_name=_("Name"), max_length=254)
    latitude = models.FloatField(
        verbose_name=_("Latitude"),
        null=True,
        blank=True,
    )
    longitude = models.FloatField(
        verbose_name=_("Longitude"),
        null=True,
        blank=True,
    )
    p_code = models.CharField(
        verbose_name=_("P Code"),
        max_length=32,
        blank=True,
        default="",
    )
    cas_code = models.CharField(
        verbose_name=_("Cas Code"),
        max_length=32,
        blank=True,
        default="",
    )

    def __str__(self):
        return self.name


class Map(TimeStampedModel):
    name = models.CharField(max_length=500)
    description = models.TextField(null=True, blank=True)
    link = models.URLField(null=True, blank=True)
    status = models.CharField(
        'Status',
        max_length=1500,
        choices=(
            ('Draft', 'Draft'),
            ('In progress', 'In progress'),
            ('Completed', 'Completed'),
            ('Archived', 'Archived'),
        ),
        blank=True, null=True,
        default='Draft'
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name