import base64

from import_export.admin import ExportMixin
from import_export.widgets import *
from django.contrib import admin, messages
from django.db.models import JSONField
from django_json_widget.widgets import JSONEditorWidget
from django.contrib.admin.utils import unquote
from django.utils.translation import gettext_lazy as _
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.db.models import Count
from django.template import loader
from django.forms import ClearableFileInput
from django.db import models
from django.utils.html import linebreaks
from django.utils.safestring import mark_safe

# change .....
from utils.custom_model_admin import CustomModelAdmin

from .models import (
    Activity,
    ActivityReportNew,
    CadasterLocation, Cadasters,
    Database, DistrictLocation,
    GovernorateLocation,
    IndicatorNew, MASTER_AGGREGATION_METHODS,
    MasterIndicator, MasterIndicatorTag, MasterSubIndicator,
    MONTHS, NeuroReport, NeuroReportMasterIndicator, NeuroReportComment,
    ReportingYear, REPORTING_LEVELS, Resource, ResourceTag, ResourceTopic, ResourceType,
    Section, SimpleLocation, SUB_AGGREGATION_METHODS, SUB_MASTER_EFFECT, SubIndicator,
    AddSubIndicatorsWizard, AddMasterIndicatorsWizard, Map
)

from .utils import *

from .tasks import run_database_import, replicate_ai_indicators

admin.site.site_header = "Neuro-DB"


@admin.register(Activity)
class ActivityAdmin(CustomModelAdmin):
    search_fields = ["name"]
    list_filter = (
        "database__reporting_year",
        "database",
    )
    list_display = (
        "name",
        "category",
        "ai_form_id",
        "ai_category_id",
        "database",
        "view_link",
        "edit_link",
        "delete_link",
    )
    # list_editable = ('ai_form_id',)
    readonly_fields = (
        "ai_id",
        "name",
        "database",
        "category",
        "location_type",
    )
    fields = (
        # 'ai_id',
        "database",
        "name",
        "label",
        "category",
        "ai_category_id",
        "ai_form_id",
        "location_type",
    )


# @admin.register(ActivityReportNew)
class ActivityReportAdmin(CustomModelAdmin):
    list_filter = (
        "location_adminlevel_governorate",
        "funded_by",
        # 'year',
        # 'month_name',
        "master_indicator",
        "project_label",
    )
    list_display = (
        "id",
        "partner_label",
        "location_name",
        # 'form_category',
        # 'indicator_id',
        "indicator_value",
        # 'indicator_awp_code',
        "reporting_section",
        "project_label",
        "funded_by",
        "view_link",
        "edit_link",
        "delete_link",
    )
    search_fields = (
        "indicator_id",
        "indicator_name",
        "indicator_awp_code",
        "partner_id",
    )

    date_hierarchy = "start_date"


class MasterIndicatorInline(admin.StackedInline):
    model = MasterIndicator
    verbose_name = "master-indicator"
    verbose_name_plural = "master-indicators"
    readonly_fields = []
    fields = (
        "is_active",
        "sequence",
        "awp_code",
        "name",
        "indicator_type",
        "awp_target",
        "aggregation_method",
        "reporting_level",
        "tags",
    )

    class Meta:
        ordering = ["sequence"]

    def has_add_permission(self, request, obj):
        return False


@admin.register(Database)
class DatabaseAdmin(CustomModelAdmin):
    list_filter = (
        "section",
        "reporting_year",
        "is_funded_by_unicef",
    )
    search_fields = ("ai_id", "name", "label",)
    list_display = (
        "ai_id",
        "name",
        "label",
        "reporting_year",
        "focal_point",
        "parent_id",
        "is_funded_by_unicef",
        "last_update_date",
        "view_link",
        "edit_link",
        "delete_link",
    )
    inlines = [MasterIndicatorInline]
    actions = [
        "import_partners",
        "import_database_structure",
        "replicate_database_indicators",
        "import_link_calculate",
        "update_partner_data",
        "update_indicator_list_header",
        "update_indicator_name",
        "set_indicator_tags",
    ]

    fieldsets = [
        (
            None,
            {
                "fields": [
                    "ai_id",
                    "parent_id",
                    "db_id",
                    "name",
                    "label",
                    "username",
                    "password",
                    "section",
                    "reporting_year",
                    "focal_point",
                    "focal_point_sector",
                    "is_funded_by_unicef",
                    "description",
                    "display",
                    "last_update_date",
                ]
            },
        ),
    ]

    def import_database_structure(self, request, queryset):
        from .utilities import import_data_v4

        objects = 0
        for db in queryset:
            objects += import_data_v4(db)
            self.message_user(request, "{} objects created.".format(objects))

    def replicate_database_indicators(self, request, queryset):
        if queryset.count() == 2:
            db_source = queryset[0]
            db_dest = queryset[1]

            if db_source.id > db_dest.id:
                db_source = queryset[1]
                db_dest = queryset[0]

            if db_source.db_id == db_dest.db_id:
                if (
                    Activity.objects.filter(database=db_dest.id).exists()
                    or MasterIndicator.objects.filter(database=db_dest.id).exists()
                    or SubIndicator.objects.filter(database=db_dest.id).exists()
                ):
                    self.message_user(
                        request,
                        "Replication form {} to {} could not start because destination has data.".format(
                            db_source, db_dest
                        ),
                    )
                else:
                    replicate_ai_indicators(db_source.id, db_dest.id)
                    self.message_user(
                        request,
                        "You have just kickoff the data replication form {} to {} ".format(
                            db_dest, db_source
                        ),
                    )
            else:
                self.message_user(
                    request,
                    "Replication form {} to {} could not start because IDs are different".format(
                        db_source.db_id, db_dest.db_id
                    ),
                )

    def import_partners(self, request, queryset):
        from .utilities import import_partners

        objects = 0
        for db in queryset:
            objects += import_partners(db)
        self.message_user(request, "Partners imported successfully")

    def import_data(self, request, queryset):
        for db in queryset:
            r_script_command_line(db)
            self.message_user(
                request, "Script R executed for database {}".format(db.name)
            )

    def import_link_calculate(self, request, queryset):
        for db in queryset:
            run_database_import(db.ai_id)
            self.message_user(
                request,
                "You have just kickoff the data import for the database: {} ".format(
                    db.name
                ),
            )

    import_link_calculate.short_description = "Update Dashboard Monthly values"

    def import_reports(self, request, queryset):
        for db in queryset:
            reports = read_data_from_file(db.ai_id)
            self.message_user(
                request,
                "{} Data imported from the file for database {}".format(
                    reports, db.name
                ),
            )

    def generate_awp_code(self, request, queryset):
        reports = 0
        for db in queryset:
            reports = generate_indicator_awp_code(db.ai_id)
        self.message_user(request, "{} reports updated.".format(reports))

    def set_indicator_tags(self, request, queryset):
        reports = 0
        for db in queryset:
            reports = set_indicator_tags(db.ai_id)
        self.message_user(request, "{} indicators updated.".format(reports))

    def update_indicator_name(self, request, queryset):
        reports = 0
        for db in queryset:
            reports = update_indicator_data(
                ai_db=db, ai_field_name="name", field_name="name"
            )
            self.message_user(
                request,
                "{} indicators status calculated for database {}".format(
                    reports, db.name
                ),
            )

    def update_indicator_list_header(self, request, queryset):
        reports = 0
        for db in queryset:
            reports = update_indicator_data(
                ai_db=db, ai_field_name="list_header", field_name="listHeader"
            )
            self.message_user(
                request,
                "{} indicators status calculated for database {}".format(
                    reports, db.name
                ),
            )

    formfield_overrides = {
        JSONField: {"widget": JSONEditorWidget(attrs={"initial": "parsed"})},
    }


@admin.register(ReportingYear)
class ReportingYearAdmin(CustomModelAdmin):
    fields = ["name", "year", "current"]
    list_display = ("name", "current", "view_link", "edit_link", "delete_link")


class UsedInSubIndicator(admin.SimpleListFilter):
    title = 'Used in a SubIndicator'
    parameter_name = 'used_in_subindicator'

    def lookups(self, request, model_admin):
        return (
            ('1', 'Yes'),
            ('0', 'No'),
        )

    def queryset(self, request, queryset):
        if self.value() == '1':
            # Filter objects that are used in the many-to-many relation
            return queryset.filter(subindicator__isnull=False).distinct()
        elif self.value() == '0':
            # Filter objects that are not used in the many-to-many relation
            return queryset.filter(subindicator__isnull=True)
        return queryset
 

class CommonValuesFilter(admin.SimpleListFilter):
    title = 'Common Values Filter'
    parameter_name = 'common_values'

    def lookups(self, request, model_admin):
        return (
            ('common', 'Duplicate Values'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'common':
            # Concatenate the values of the three columns
            from django.db import connection
            cursor = connection.cursor()
            cursor.execute("select database_id, ai_indicator, name,  count(*) as cc from pivoting_indicatornew group by database_id, ai_indicator, name having count(*) > 1", [])
            result = cursor.fetchall()
            duplicates = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
            
            ids = [IndicatorNew.objects.filter(database_id=duplicate["database_id"], name=duplicate["name"], ai_indicator=duplicate["ai_indicator"]).values_list('id', flat=True) for duplicate in duplicates]
            ids_set = set(element for sub_array in ids for element in sub_array)
            return queryset.filter(id__in=ids_set)


@admin.register(IndicatorNew)
class IndicatorNewAdmin(CustomModelAdmin):
    search_fields = ["name", "awp_code", "ai_indicator"]
    list_filter = (
        "units",
        "type",
        "gender",
        "nationality",
        "disability",
        "age_group",
        "programme",
        "activity__database",
        "activity__database__reporting_year",
        UsedInSubIndicator,
        CommonValuesFilter,
    )
    fields = (
        "ai_indicator",
        "activity",
        "awp_code",
        "name",
        "units",
        "type",
        "gender",
        "nationality",
        "disability",
        "age_group",
        "programme",
    )
    list_display = (
        "id",
        "database_name",
        "ai_indicator",
        "awp_code",
        "name",
        "units",
        "type",
    )


class UsedInMasterIndicator(admin.SimpleListFilter):
    title = 'Used in a MasterIndicator'
    parameter_name = 'used_in_masterindicator'

    def lookups(self, request, model_admin):
        return (
            ('1', 'Yes'),
            ('0', 'No'),
        )

    def queryset(self, request, queryset):
        if self.value() == '1':
            # Filter objects that are used in the many-to-many relation
            return queryset.filter(mastersubindicator__isnull=False).distinct()
        elif self.value() == '0':
            # Filter objects that are not used in the many-to-many relation
            return queryset.filter(mastersubindicator__isnull=True)
        return queryset

@admin.register(SubIndicator)
class SubIndicatorAdmin(ExportMixin, CustomModelAdmin):
    list_filter = (("database", admin.RelatedOnlyFieldListFilter), UsedInMasterIndicator)
    list_display = (
        "id",
        "formatted_name",
        "awp_code",
        "target",
        "database",
        "_num_indicators",
    )
    list_display_links = ("id",)
    list_editable = [
        "awp_code",
        "target",
    ]
    search_fields = ["awp_code", "name"]
    filter_horizontal = ["indicators",]

    def get_form(self, request, obj=None, **kwargs):
        if obj:
            request.database = obj.database
            request.activity = obj.activity
        return super().get_form(request, obj, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "indicators":
            if hasattr(request, "activity"):
                kwargs["queryset"] = IndicatorNew.objects.filter(
                    activity=request.activity
                )
        return super(SubIndicatorAdmin, self).formfield_for_manytomany(
            db_field, request, **kwargs
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "activity":
            if hasattr(request, "database"):
                kwargs["queryset"] = Activity.objects.filter(database=request.database)

        return super(SubIndicatorAdmin, self).formfield_for_foreignkey(
            db_field, request, **kwargs
        )

    def get_fields(self, request, obj):
        if obj:
            if obj.activity:
                return (
                    "database",
                    "activity",
                    "awp_code",
                    "name",
                    "target",
                    "sector_equivalent",
                    "activity_indicators",
                    "chosen_indicators",
                    "indicators",
                )
            if obj.database:
                return (
                    "database",
                    "activity",
                )
        else:
            return ("database",)

        return super().get_fields(request, obj)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            if obj.indicators and (obj.indicators.count() > 0):
                return (
                    "database",
                    "activity",
                    "activity_indicators", 
                    "chosen_indicators"
                )
            else:
                return ("database", "activity_indicators", "chosen_indicators")
        else:
            return ("indicators", "activity_indicators", "chosen_indicators")

    def _num_indicators(self, obj):
        return obj._indicators

    _num_indicators.admin_order_field = "_indicators"
    _num_indicators.short_description = "{}#".format(_("Indicators"))

    def get_queryset(self, request):
        return (
            super(SubIndicatorAdmin, self)
            .get_queryset(
                request,
            )
            .annotate(_indicators=Count("indicators"))
        )

    def formatted_name(self, obj):
        words = obj.name.split(" ")
        result = ""
        i = 0
        for w in words:
            result = result + w + " "
            i = i + 1
            if i == 10:
                result = result + "<br />"
                i = 0
        return mark_safe(result)

    formatted_name.short_description = _("name")


class MasterSubIndicatorInline(admin.StackedInline):
    model = MasterSubIndicator
    verbose_name = "Sub-indicator"
    verbose_name_plural = "Sub-indicators"
    readonly_fields = ["sub"]
    fields = (
        "sub",
        (
            "effect",
            "target",
        ),
        "label",
        "sequence",
    )

    def has_add_permission(self, request, obj):
        return False


@admin.register(MasterIndicatorTag)
class MasterIndicatorTagAdmin(CustomModelAdmin):
    pass

   
@admin.register(MasterIndicator)
class MasterIndicatorAdmin(ExportMixin, CustomModelAdmin):
    list_display = (
        "id",
        "formatted_name",
        "awp_code",
        "awp_target",
        "reporting_level",
        "_tags",
        "is_active",
        "ram_result",
        "database",
        "_num_subindicators",
    )
    list_display_links = ("id",)
    list_editable = [
        "awp_code",
        "reporting_level",
        "awp_target",
    ]
    search_fields = [
        "awp_code",
        "name",
        "reporting_level",
    ]
    list_filter = (
        ("database", admin.RelatedOnlyFieldListFilter),
        "aggregation_method",
        "reporting_level",
        "tags",
        "is_active",
        
    )

    def add_view(self, request, form_url="", extra_context=None):
        self.inlines = []
        self.readonly_fields = []
        self.fields = [
            "database",
            "awp_code",
            "name",
            "indicator_type",
            ("awp_target", "ram_result"),
            "aggregation_method",
            "reporting_level",
            "tags",
            "unit",
            "sector_equivalent",
        ]
        return super().add_view(request, form_url, extra_context)

    def get_form(self, request, obj=None, **kwargs):
        if obj:
            request.database = obj.database
        return super().get_form(request, obj, **kwargs)

    # def get_queryset(self, request):
    #     queryset = super(MasterIndicatorAdmin, self).get_queryset(request)
    #     queryset = queryset.prefetch_related("tags")
    #     return queryset

    def _num_subindicators(self, obj):
        return obj._subindicators

    _num_subindicators.admin_order_field = "_subindicators"
    _num_subindicators.short_description = "{}#".format(_("Sub Indctrs"))

    def get_queryset(self, request):
        return (
            super(MasterIndicatorAdmin, self)
            .get_queryset(
                request,
            )
            .annotate(_subindicators=Count("subindicators"))
        )

    def _tags(self, obj):
        return (", ").join([tag.name for tag in obj.tags.all()])

    def change_view(self, request, object_id, form_url="", extra_context=None):
        self.inlines = [MasterSubIndicatorInline]
        self.readonly_fields = ["database", "holdings", "old_id"]
        self.fields = [
            "database",
            "is_active",
            "awp_code",
            "name",
            "indicator_type",
            ("awp_target", "ram_result"),
            "aggregation_method",
            "reporting_level",
            "tags",
            "unit",
            "sector_equivalent",
            "holdings",
            "old_id",
        ]

        obj = self.get_object(request, unquote(object_id))
        buttons = []
        buttons.append(
            {
                "name": "_add_idicators",
                "label": _("AddSubIndicators"),
                "title": "",
            }
        )
        extra_context = {
            "objx": obj,
            "extra_buttons": buttons,
        }
        return super(MasterIndicatorAdmin, self).change_view(
            request, object_id, form_url, extra_context
        )

    def add_indicators(self, request, obj):
        wizard = obj.create_add_indicators_wizard()
        return HttpResponseRedirect(
            reverse(
                "admin:pivoting_%s_change" % "addsubindicatorswizard",
                kwargs={"object_id": wizard.id},
            )
        )

    def response_change(self, request, obj):
        if "_add_idicators" in request.POST:
            return self.add_indicators(request, obj)
        return super(MasterIndicatorAdmin, self).response_change(request, obj)

    def holdings(self, obj):
        header_t = loader.get_template("admin/simple_table.html")
        header_c = {
            "header": [_("Label"), _("Target"), _("Effect")],
            "values": obj.subindicators.all()
            .order_by("effect")
            .values_list("label", "target", "effect"),
        }
        return header_t.render(header_c)

    holdings.short_description = _("Defined sub indiators")

    def formatted_name(self, obj):
        words = obj.name.split(" ")
        result = ""
        i = 0
        for w in words:
            result = result + w + " "
            i = i + 1
            if i == 10:
                result = result + "<br />"
                i = 0
        return mark_safe(result)

    formatted_name.short_description = _("name")


@admin.register(AddSubIndicatorsWizard)
class AddPSubIndicatorsWizardAdmin(CustomModelAdmin):
    change_form_template = "admin/wizard_confirm_or_cancel_template.html"
    filter_horizontal = ["indicators"]

    def has_add_permission(self, request):
        return False

    def get_model_perms(self, request):
        return {}

    def get_form(self, request, obj=None, **kwargs):
        if obj:
            request.database = obj.master.database
        return super().get_form(request, obj, **kwargs)

    readonly_fields = ("master",)
    fieldsets = ((None, {"fields": ("master", "effect", "indicators")}),)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "indicators":
            kwargs["queryset"] = SubIndicator.objects.filter(database=request.database)
        return super(AddPSubIndicatorsWizardAdmin, self).formfield_for_manytomany(
            db_field, request, **kwargs
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, unquote(object_id))
        extra_context = {
            "title": _(
                "Select the sub-indicators you want to add to the master indicator."
            ),
            "cancel_url": reverse(
                "admin:pivoting_%s_change" % "masterindicator",
                kwargs={"object_id": obj.master.id},
            ),
        }
        return super().change_view(
            request, object_id=object_id, form_url=form_url, extra_context=extra_context
        )

    def response_change(self, request, obj):
        obj.confirm_action()
        messages.add_message(
            request, messages.INFO, _("Selected sub-indicatos were added successfuly")
        )
        return HttpResponseRedirect(
            reverse(
                "admin:pivoting_%s_change" % "masterindicator",
                kwargs={"object_id": obj.master.id},
            )
        )


class NeuroReportMasterIndicatorInline(admin.StackedInline):
    model = NeuroReportMasterIndicator
    verbose_name = "Master indicator"
    verbose_name_plural = "Master indicators"
    exclude = ["ram_result"]
    fields = ["label", "category", "target", "master", "section",]
    readonly_fields = ["master", "section"]

    def has_add_permission(self, request, obj):
        return False


class NeuroReportCommentInline(admin.StackedInline):
    model = NeuroReportComment
    verbose_name = "Comment"
    verbose_name_plural = "Comments"
    readonly_fields = ["entry_date", "last_update"]
    extra = 0

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        obj_id = request.META["PATH_INFO"].rstrip("/").split("/")[-2]
        if db_field.name == "master" and obj_id.isdigit():
            obj = NeuroReport.objects.get(id=obj_id)
            if obj:
                related = list(
                    NeuroReportMasterIndicator.objects.filter(
                        report_id=obj.id
                    ).values_list("id", flat=True)
                )
                kwargs["queryset"] = NeuroReportMasterIndicator.objects.filter(
                    id__in=related
                )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(NeuroReport)
class NeuroReportAdmin(CustomModelAdmin):
    fields = ["name", "report_code", "ryear", "is_hpm", "is_active"]
    list_display = (
        "id",
        "ryear",
        "name",
        "is_hpm",
        "is_active",
        "view_link",
        "edit_link",
        "delete_link",
    )
    list_filter = ["ryear", "is_hpm", "is_active"]
    search_fields = [
        "name",
        "report_code",
    ]

    def get_form(self, request, obj=None, **kwargs):
        if obj:
            request.ryear = obj.ryear
            request.report = obj.id
        return super().get_form(request, obj, **kwargs)

    def add_view(self, request, form_url="", extra_context=None):
        self.inlines = []
        self.readonly_fields = []
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, unquote(object_id))
        self.inlines = [NeuroReportMasterIndicatorInline, NeuroReportCommentInline]
        self.readonly_fields = ["ryear"]
        buttons = []
        buttons.append(
            {
                "name": "_add_idicators",
                "label": _("Add Master Indicators"),
                "title": "",
            }
        )
        extra_context = {
            "objx": obj,
            "extra_buttons": buttons,
        }
        return super(NeuroReportAdmin, self).change_view(
            request, object_id, form_url, extra_context
        )

    def add_indicators(self, request, obj):
        wizard = obj.create_add_indicators_wizard()
        return HttpResponseRedirect(
            reverse(
                "admin:pivoting_%s_change" % "addmasterindicatorswizard",
                kwargs={"object_id": wizard.id},
            )
        )

    def response_change(self, request, obj):
        if "_add_idicators" in request.POST:
            return self.add_indicators(request, obj)
        return super(NeuroReportAdmin, self).response_change(request, obj)


@admin.register(AddMasterIndicatorsWizard)
class AddMasterIndicatorsWizardAdmin(CustomModelAdmin):
    change_form_template = "admin/wizard_confirm_or_cancel_template.html"
    filter_horizontal = ["indicators"]

    def has_add_permission(self, request):
        return False

    def get_model_perms(self, request):
        return {}

    def get_form(self, request, obj=None, **kwargs):
        if obj:
            request.reporting_year = obj.report.ryear
        return super().get_form(request, obj, **kwargs)

    readonly_fields = ("report",)
    fieldsets = ((None, {"fields": ("report", "add_by", "tags", "indicators")}),)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "indicators":
            kwargs["queryset"] = MasterIndicator.objects.filter(
                database__reporting_year=request.reporting_year
            )
        return super(AddMasterIndicatorsWizardAdmin, self).formfield_for_manytomany(
            db_field, request, **kwargs
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, unquote(object_id))
        extra_context = {
            "title": _(
                "Select the master-indicators you want to add to the Neuro Report."
            ),
            "cancel_url": reverse(
                "admin:pivoting_%s_change" % "neuroreport",
                kwargs={"object_id": obj.report.id},
            ),
        }
        return super().change_view(
            request, object_id=object_id, form_url=form_url, extra_context=extra_context
        )

    def response_change(self, request, obj):
        obj.confirm_action()
        messages.add_message(
            request,
            messages.INFO,
            _("Selected master indicatos were added successfuly"),
        )
        return HttpResponseRedirect(
            reverse(
                "admin:pivoting_%s_change" % "neuroreport",
                kwargs={"object_id": obj.report.id},
            )
        )


class BinaryFileInput(ClearableFileInput):
    def is_initial(self, value):
        """
        Return whether value is considered to be initial value.
        """
        return bool(value)

    def format_value(self, value):
        """Format the size of the value in the db.
        We can't render it's name or url, but we'd like to give some information
        as to wether this file is not empty/corrupt.
        """
        if self.is_initial(value):
            return f"{len(value)} bytes"

    def value_from_datadict(self, data, files, name):
        """Return the file contents so they can be put in the db."""
        # print(data)
        if name == "resource_file" and "resource_file-clear" in data:
            return None
        if name == "resource_image" and "resource_image-clear" in data:
            return None
        else:
            upload = super().value_from_datadict(data, files, name)
            if upload:
                binary_file_data = upload.read()
                image_data = base64.b64encode(binary_file_data).decode("utf-8")
                return image_data
            else:
                if Resource.objects.filter(title=data["title"]).exists():
                    if name == "resource_file":
                        return Resource.objects.get(
                            title=data["title"]
                        ).get_resource_file
                    if name == "resource_image":
                        return Resource.objects.get(
                            title=data["title"]
                        ).get_resource_image
                else:
                    return None


@admin.register(ResourceTag)
class ResourceTagAdmin(CustomModelAdmin):
    pass


@admin.register(ResourceType)
class ResourceTypeAdmin(CustomModelAdmin):
    pass


@admin.register(ResourceTopic)
class ResourceTopicAdmin(CustomModelAdmin):
    pass


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    formfield_overrides = {
        models.BinaryField: {"widget": BinaryFileInput()},
    }
    list_display = ("title", "publication_year", "type", "topic", "section")
    list_filter = ("type", "topic", "section", "publication_year")
    search_fields = (
        "description",
        "title",
    )
    fields = (
        "title",
        "description",
        "publication_year",
        "topic",
        "type",
        "section",
        "resource_link",
        "tags",
        "resource_file_name",
        "resource_file",
        "resource_image_name",
        "resource_image",
        "published"
    )
    readonly_fields = ("resource_file_name", "resource_image_name")

    def save_model(self, request, obj, form, change):
        if request.FILES != {}:
            if "resource_file" in request.FILES:
                file_name = request.FILES["resource_file"]
                obj.resource_file_name = file_name

            if "resource_image" in request.FILES:
                image_name = request.FILES["resource_image"]
                obj.resource_image_name = image_name
        else:
            if obj.pk:
                if (
                    "resource_file-clear" in form.data
                    and form.data["resource_file-clear"] == "on"
                ):
                    obj.resource_file_name = None
                if (
                    "resource_image-clear" in form.data
                    and form.data["resource_image-clear"] == "on"
                ):
                    obj.resource_image_name = None

        return super().save_model(request, obj, form, change)


@admin.register(Map)
class MapAdmin(admin.ModelAdmin):

    list_display = ('name', 'description', 'status')
    date_hierarchy = 'created'
    list_filter = ('status', )