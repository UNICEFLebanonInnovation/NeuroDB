
import datetime
from this import d
from django.contrib import admin
from django.db.models import JSONField
from django_json_widget.widgets import JSONEditorWidget
from import_export import resources, fields
from import_export import fields
from import_export.admin import ImportExportModelAdmin
from django.db.models import Sum, Count, Q
from utils.arrayfield_filter import ArrayFieldListFilter
from utils.custom_model_admin import CustomModelAdmin
from utils.json_field_filter import JSONFieldFilter

from .models import (
    PartnerOrganization,
    Agreement,
    PCA,
    PartnerStaffMember,
    Travel,
    TravelActivity,
    ItineraryItem,
    Engagement,
    ActionPoint,
    Category,
    DonorFunding
)


class PartnerStaffMemberInline(admin.TabularInline):
    model = PartnerStaffMember
    max_num = 99
    min_num = 0
    extra = 1
    verbose_name = 'Partner staff member'
    verbose_name_plural = 'Partner staff members'

    fields = (
        'title',
        'first_name',
        'last_name',
        'email',
        'phone',
        'active',
    )


class PartnerOrganizationResource(resources.ModelResource):
    class Meta:
        model = PartnerOrganization
        fields = (
            'id',
            'vendor_number',
            'name',
            'short_name',
            'partner_type',
            'cso_type',
            'rating',
            'shared_partner',
            'email',
            'phone_number',
        )
        export_order = fields


class EngagementInline(admin.TabularInline):
    model = Engagement
    
    def has_add_permission(self, request, obj):
        return False
    
    def has_delete_permission(self, request, obj):
        return False
    
    def has_change_permission(self, request, obj):
        return False

    fields = (
        'unique_id',
        'engagement_type',
        'status',
        'date_of_final_report',
    )
    # readonly_fields = fields
    show_change_link = True


class TravelActivityInline(admin.TabularInline):
    model = TravelActivity
    
    def has_add_permission(self, request, obj):
        return False
    
    def has_delete_permission(self, request, obj):
        return False
    
    def has_change_permission(self, request, obj):
        return False

    fields = (
        'travel_type',
        'date',
        'travel',
    )
    # readonly_fields = fields
    show_change_link = True


class PartnerOrganizationAdmin(ImportExportModelAdmin, CustomModelAdmin):
    resource_class = PartnerOrganizationResource

    readonly_fields = (
        'vendor_number',
        'name',
        'short_name',
        'partner_type',
        'cso_type',
        'rating',
        'shared_partner',
        'email',
        'phone_number',
    )

    fields = (
        'vendor_number',
        'name',
        'short_name',
        'partner_type',
        'cso_type',
        'rating',
        'shared_partner',
        'email',
        'phone_number',
        'comments',
    )
    list_display = (
        'vendor_number',
        'name',
        'short_name',
        'partner_type',
        'cso_type',
        'rating',
        'email',
        'phone_number', 'programmatic_visit_count', 'micro_assessments_count',  'spot_checks_count', 'audits_count', 'special_audits_count', 'view_link', 'edit_link', 'delete_link'
    )
    list_filter = (
        'partner_type',
        'cso_type',
        'rating',
    )
    search_fields = (
        'name', 'short_name',
    )

    def get_queryset(self, request):
        queryset = super(PartnerOrganizationAdmin, self).get_queryset(request)
        today = datetime.date.today()
        spot_checks_filters = Q(engagement_set__engagement_type=Engagement.TYPE_SPOT_CHECK) & ~Q(engagement_set__status=Engagement.CANCELLED)
        special_audits_filters = Q(engagement_set__engagement_type=Engagement.TYPE_SPECIAL_AUDIT) & ~Q(engagement_set__status=Engagement.CANCELLED)
        micro_assessments_filters = Q(engagement_set__engagement_type=Engagement.TYPE_MICRO_ASSESSMENT) & ~Q(engagement_set__status=Engagement.CANCELLED)
        audits_filters = Q(engagement_set__engagement_type=Engagement.TYPE_AUDIT) & ~Q(engagement_set__status=Engagement.CANCELLED)
        programmatic_visit_filters = Q(travelactivity_set__travel_type__iexact='programmatic visit') # & Q(travelactivity_set__date__year=today.year)

        return queryset\
        .annotate(spot_checks_count=Count('engagement_set', filter=spot_checks_filters, distinct=True))\
        .annotate(special_audits_count=Count('engagement_set', filter=special_audits_filters, distinct=True))\
        .annotate(micro_assessments_count=Count('engagement_set', filter=micro_assessments_filters, distinct=True))\
        .annotate(audits_count=Count('engagement_set', filter=audits_filters, distinct=True))\
        .annotate(programmatic_visit_count=Count('travelactivity_set', filter=programmatic_visit_filters, distinct=True))
        
        # return queryset.annotate(spot_checks_count=Count('participants', filter=Q(participants__is_paid=True))


    def spot_checks_count(self, obj):
        return obj.spot_checks_count
    spot_checks_count.admin_order_field = "spot_checks_count"
    spot_checks_count.short_description = "Spot Checks"

    def special_audits_count(self, obj):
        return obj.special_audits_count
    special_audits_count.admin_order_field = "special_audits_count"
    special_audits_count.short_description = "Special Audits"

    def micro_assessments_count(self, obj):
        return obj.micro_assessments_count
    micro_assessments_count.admin_order_field = "micro_assessments_count"
    micro_assessments_count.short_description = "Micro Assessments"

    def audits_count(self, obj):
        return obj.audits_count
    audits_count.admin_order_field = "audits_count"
    audits_count.short_description = "Audits Count"

    def programmatic_visit_count(self, obj):
        return obj.programmatic_visit_count
    programmatic_visit_count.admin_order_field = "programmatic_visit_count"
    programmatic_visit_count.short_description = "Programmatic Visits"

    inlines = [PartnerStaffMemberInline, EngagementInline, TravelActivityInline]


class AgreementResource(resources.ModelResource):
    class Meta:
        model = Agreement
        fields = (
            'id',
            'partner_name',
            'agreement_type',
            'agreement_number',
            'start',
            'end',
            'signed_by_unicef_date',
            'signed_by_partner_date',
        )
        export_order = fields


class AgreementAdmin(ImportExportModelAdmin, CustomModelAdmin):
    search_fields = ['agreement_number', 'partner_name']
    resource_class = AgreementResource
    list_display = (
        'id',
        'partner_name',
        'agreement_type',
        'agreement_number',
        'start',
        'end',
        'signed_by_unicef_date',
        'signed_by_partner_date', 'view_link', 'edit_link', 'delete_link'
    )
    # date_hierarchy = 'end'
    list_filter = ('agreement_type', 'partner', 'end')


class PCAResource(resources.ModelResource):
    class Meta:
        model = PCA
        fields = (
            'id',
            'number',
            'document_type',
            'partner_name',
            'status',
            'title',
            'start',
            'end',
            'country_programme',
            'signed_by_unicef_date',
            'signed_by_partner_date',
        )
        export_order = fields


class DonorFilter(JSONFieldFilter):
    """
    """

    title = 'Donor'  # for admin sidebar (above the filter options)
    parameter_name = 'jsondonorsset'  # Parameter for the filter that will be used in the URL query
    json_field_name = 'donors_set'
    json_field_property_name = 'donor'  # property/field in json data

class SectionFilter(ArrayFieldListFilter):
    """An admin list filter for domains."""

    title = "Sections"
    parameter_name = "section_names"
 # and then in model admin class
 
class LocationFilter(ArrayFieldListFilter):
    """An admin list filter for domains."""

    title = "Location"
    parameter_name = "location_p_codes"
 # and then in model admin class

class PCAAdmin(ImportExportModelAdmin, CustomModelAdmin):
    resource_class = PCAResource
    formfield_overrides = {
        JSONField: {'widget': JSONEditorWidget(attrs={'initial': 'parsed'})},
    }
    date_hierarchy = 'start'

    list_display = (
        'number',
        'status',
        'title',
        'partner',
        'agreement',
        'document_type',
        'country_programme',
        'location_p_codes',
        'section_names',
        'start',
        'end', 'view_link', 'edit_link', 'delete_link'
    )

    list_filter = (
        'status',
        SectionFilter,
        'locations__name',
        'partner',
        'document_type',
        'country_programme',
        DonorFilter
    )

    search_fields = (
        'number',
        'partner_name',
    )
    autocomplete_fields = (
        'activities', 'locations'
    )
    def get_readonly_fields(self, request, obj=None):
        editable = ['activities', 'frs_details', 'donors_set']
        return [f.name for f in self.model._meta.fields if f.name not in editable]
    fields = (
        'title', 
        'status', 
        ('etl_id', 'number', ), 
        ('start', 'end'), 
        'partner', 
        'agreement', 
        
        ('document_type', 'country_programme'), 
        'donor_codes', 
        'donors', 
        
        'grants', 
        'location_p_codes', 
        'offices_set', 
        # 'offices_names', 
        # 'sections', 
        'section_names', 
        
        'unicef_focal_points', 

        'total_budget', 
        'total_unicef_budget', 
        'unicef_cash', 
        'frs_total_frs_amt', 
        'frs_total_intervention_amt', 
        'frs_total_outstanding_amt', 
        'actual_amount', 
        'cp_outputs', 
        'cso_contribution', 
        'donors_set', 
        'frs_details'
    )
            

class PartnerStaffMemberResource(resources.ModelResource):
    class Meta:
        model = PartnerStaffMember
        fields = (
        )
        export_order = fields


class PartnerStaffMemberAdmin(ImportExportModelAdmin, CustomModelAdmin):
    resource_class = PartnerStaffMemberResource
    fields = (
        'title',
        'first_name',
        'last_name',
        'email',
        'phone',
        'active',
    )
    list_display = (
        'partner',
        'title',
        'first_name',
        'last_name',
        'email',
        'phone',
        'active', 'view_link', 'edit_link', 'delete_link'
    )
    list_filter = (
        'partner',
        'active',
    )
    search_fields = (
        'partner__name',
        'first_name',
        'last_name',
        'email',
        'phone',
    )


@admin.register(Engagement)
class EngagementAdmin(CustomModelAdmin):
    list_display = [
        '__str__', 'status', 'partner', 'date_of_field_visit',
        'engagement_type', 'start_date', 'end_date',  'view_link', 'edit_link', 'delete_link'
    ]
    list_filter = [
        'status', 'start_date', 'end_date', 'status', 'engagement_type',
    ]
    readonly_fields = ('status', 'partner',)
    search_fields = 'partner__name', 'agreement__auditor_firm__name',
    fields = (
        'unique_id',
        'engagement_type',
        'status',
        'start_date',
        'end_date',
        'partner',
        'partner_contacted_at',
        'total_value',
        'exchange_rate',
        'date_of_field_visit',
        'date_of_draft_report_to_ip',
        'date_of_comments_by_ip',
        'date_of_draft_report_to_unicef',
        'date_of_comments_by_unicef',
        'date_of_report_submit',
        'date_of_final_report',
        'date_of_cancel',
        'cancel_comment',
        'internal_controls',
        'final_report',
        'audited_expenditure',
        'financial_findings',
        'audit_opinion',
        'description',
        'finding',
        'pending_unsupported_amount',
        'findings',
    )

    formfield_overrides = {
        JSONField: {'widget': JSONEditorWidget(attrs={'initial': 'parsed'})},
        # models.ManyToManyField: {'widget': FilteredSelectMultiple('indicator', is_stacked=False)}
    }


@admin.register(Travel)
class TravelAdmin(CustomModelAdmin):
    list_filter = (
        'status',
        'international_travel',
        'travel_type',
        'traveler_name',
        'section',
        'office',
        'start_date',

    )
    search_fields = (
        'reference_number',
    )
    list_display = (
        'reference_number',
        'traveler_name',
        'supervisor_name',
        'status',
        'start_date',
        'end_date',
        'section', 'office', 'view_link', 'edit_link', 'delete_link'
    )
    def get_readonly_fields(self, request, obj=None):
        editable = []
        return [f.name for f in self.model._meta.fields if f.name not in editable]

    date_hierarchy = 'start_date'

    formfield_overrides = {
        JSONField: {'widget': JSONEditorWidget(attrs={'initial': 'parsed'})},
    }

    fieldsets = [
        ('', {
            'fields': [ 'reference_number', 'purpose', 'start_date', 'end_date', 'traveler', 'traveler_name','supervisor','supervisor_name', 'section', 'office']
        }),
        ('Workflow', {
            'fields': ['status', 'first_submission_date', 'submitted_at', 'rejected_at', 'rejection_note', 'approved_at', 
              'canceled_at', 'cancellation_note', 'completed_at',  'certification_note', ]
        }),
        ('Expenses', {
            'fields': ['misc_expenses', 'estimated_travel_cost', 'preserved_expenses_local', 'preserved_expenses_usd', 'approved_cost_traveler', 'approved_cost_travel_agencies']
        }),
        ('Activities', {
            'fields': ['travel_type', 'report_note', 'additional_note', 'activities_set', 'attachments_set', 'attachments_sets', 'have_hact']
        }),
        ('Other Details', {
            'fields': [ 'international_travel', 'mode_of_travel', 'ta_required', 'hidden', 'is_driver', 'itinerary_set', ]
        }),
    ]

@admin.register(TravelActivity)
class TravelActivityAdmin(CustomModelAdmin):
    list_filter = (
        'travel_type',
        'partner',
        'date',
    )
    search_fields = (
        'primary_traveler__first_name',
        'primary_traveler__last_name',
    )
    list_display = (
        'travel',
        # 'primary_traveler',
        'travel_type',
        'date', 'partner',  'view_link', 'edit_link', 'delete_link'
    )
    date_hierarchy = 'date'
    autocomplete_fields = ['travel', 'travels', 'partner', 'partnership', 'locations']


@admin.register(ItineraryItem)
class ItineraryItemAdmin(CustomModelAdmin):
    list_filter = (
        'travel',
        'departure_date',
        'arrival_date',
        'origin',
        'destination'
    )
    search_fields = (
        'travel__reference_number',
    )
    list_display = (
        'travel',
        'departure_date',
        'arrival_date',
        'origin',
        'destination', 'view_link', 'edit_link', 'delete_link'
    )


class CategoryResource(resources.ModelResource):

    class Meta:
        model = Category
        fields = (
            'id',
            'module',
            'description',
        )
        export_order = fields


class CategoryAdmin(ImportExportModelAdmin, CustomModelAdmin):
    resource_class = CategoryResource
    list_display = ('module', 'description', 'view_link', 'edit_link', 'delete_link')
    list_filter = ('module', )
    search_fields = ('description', )


# SnapshotModelAdmin
class ActionPointAdmin(CustomModelAdmin):
    list_display = (
        'author_name',
        'assigned_to_name',
        'status',
        'date_of_completion',
        'related_module',
        'description',
        'engagement',  'view_link', 'edit_link', 'delete_link'
    )
    list_filter = ('status', 'related_module', )
    search_fields = ('author_name', 'assigned_to_name')
    readonly_fields = ('status', )
    raw_id_fields = ('section', 'office', 'location', 'partner', 'intervention',
                     'travel_activity', 'engagement', 'author', 'assigned_by', 'assigned_to')


@admin.register(DonorFunding)
class DonorFundingAdmin(CustomModelAdmin):
    list_display = ['donor', 'donor_code', 'year', 'total', 'view_link', 'edit_link', 'delete_link']
    list_filter = ['donor', 'year', ]

admin.site.register(ActionPoint, ActionPointAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(PartnerOrganization, PartnerOrganizationAdmin)
admin.site.register(Agreement, AgreementAdmin)
admin.site.register(PCA, PCAAdmin)
# admin.site.register(Travel)
# admin.site.register(Engagement)
admin.site.register(PartnerStaffMember, PartnerStaffMemberAdmin)