from __future__ import absolute_import, unicode_literals
import json
import datetime
from django.db.models import Q, Sum
from django.views.generic import ListView,TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse

from locations.models import Location
from etools.models import PartnerOrganization, PCA, Engagement, Travel, TravelType, TravelActivity
from .utils import get_partner_profile_details, get_trip_details, get_interventions_details
from users.models import Section, Office

from pivoting.models import ActivityReportNew,Database


class PartnershipView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/partnerships.html'

    def get_context_data(self, **kwargs):

        return {}


class DonorMappingView(LoginRequiredMixin,TemplateView):
    """
    This is the main view and is used only to get the filters. The view data is retreived by separate ajax calls when the user selects a donor and/or some filters
    """
    template_name = 'etools/donor_mapping.html'

    def get_context_data(self, **kwargs):

        databases_ids = [str(d.ai_id) for d in Database.objects.filter(is_funded_by_unicef=True)]
        pca = PCA.objects.filter(donors__isnull=False, donors__len__gt=0, status='active').exclude(status__in=['draft',])
        sections = sorted(set([item for sublist in  pca.values_list('section_names', flat=True).distinct() for item in sublist]))
        offices = sorted(set([item for sublist in  pca.values_list('offices_set', flat=True).distinct() for item in sublist]))
        partners = sorted(set(pca.exclude(partner__short_name__isnull=True).values_list('partner__short_name', flat=True)))
        statuses = sorted(set(pca.values_list('status', flat=True)))

        selected_partners = self.request.GET.getlist('partners')
        selected_sections = self.request.GET.getlist('sections')
        selected_offices = self.request.GET.getlist('offices')

        if selected_partners:
            pca = pca.filter(partner__short_name__in=selected_partners)

        if selected_sections:
            pca = pca.filter(section_names__contained_by=selected_sections)

        if selected_offices:
            pca = pca.filter(offices_set__contained_by=selected_offices)

        donors = sorted(set([item for sublist in  pca.values_list('donors', flat=True).distinct() for item in sublist]))
        programmes = sorted(pca.values_list('number', flat=True))

        return {
            'donors': donors, 
            'partners': partners,
            'offices': offices,
            'sections':sections,
            'programmes':programmes,
            'last_update':  pca.first().updated_at if pca.exists() else '',
            'statuses':statuses,
            'selected_partners': selected_partners,
            'selected_sections': selected_sections,
            'selected_offices': selected_offices,
        }


class DonorInterventionsView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/interventions_block.html'

    def get_context_data(self, **kwargs):
        selected_donor = self.request.GET.get('donor', 'G45301')
        selected_programs = self.request.GET.getlist('selected_programs[]')
        selected_partners = self.request.GET.getlist('selected_partners[]')
        selected_sections = self.request.GET.getlist('selected_sections[]')
        selected_offices = self.request.GET.getlist('selected_offices[]')

        now = datetime.datetime.now()
        pca = PCA.objects.filter( status='active')
        
        if selected_partners:
            pca = pca.filter(partner__short_name__in=selected_partners)

        if selected_sections:
            pca = pca.filter(section_names__contained_by=selected_sections)

        if selected_offices:
            pca = pca.filter(offices_set__contained_by=selected_offices)
        
        if selected_donor:
            pca = pca.filter(donors__contains=[selected_donor])

        q_objects = Q() # Create an empty Q object to start with
        if selected_programs:
            if 'future' in selected_programs:
                q_objects |= Q(start__gt=now, end__gt=now)
            if 'current' in selected_programs:
                q_objects |= Q(start__lte=now, end__gte=now)
            if 'previous' in selected_programs:
                q_objects |= Q(start__lt=now, end__lt=now)

            pca = PCA.objects.filter(q_objects).extra(where=["'"+selected_donor+"' = ANY (donor_codes)"]).order_by('-start')
        # select the current interventions:

        return {
            'interventions': pca,
            'donor': selected_donor,
        }


def get_filtered_activtyrepot(request):
    selected_donor = request.GET.get('donor', 'G45301')
    selected_programs = request.GET.getlist('selected_programs[]')
    selected_partners = request.GET.getlist('selected_partners[]')
    selected_sections = request.GET.getlist('selected_sections[]')
    selected_offices = request.GET.getlist('selected_offices[]')
    pca = PCA.objects.filter( status='active')
    if selected_partners:
        pca = pca.filter(partner__short_name__in=selected_partners)
    if selected_sections:
        pca = pca.filter(section_names__contained_by=selected_sections)
    if selected_offices:
        pca = pca.filter(offices_set__contained_by=selected_offices)
    if selected_donor:
        pca = pca.filter(donors__contains=[selected_donor])
    
    if selected_partners:
        partner_labels = selected_partners
    else:
        partner_labels = [item.partner.short_name for item in pca]
    
    reports = ActivityReportNew.objects.filter(funded_by='UNICEF', partner_label__in=partner_labels)
           
    if selected_sections:
        databases_ids = [str(d.ai_id) for d in Database.objects.filter(is_funded_by_unicef=True, section__name__in=selected_sections)]
        reports = reports.filter(database_id__in=databases_ids)   

    return reports


def load_donor_locations(request):
    selected_donor = request.GET.get('donor', 'G45301')
    selected_programs = request.GET.getlist('selected_programs[]')
    selected_partners = request.GET.getlist('selected_partners[]')
    selected_sections = request.GET.getlist('selected_sections[]')
    selected_offices = request.GET.getlist('selected_offices[]')
    pca = PCA.objects.filter( status='active')

    if selected_partners:
        pca = pca.filter(partner__short_name__in=selected_partners)
    if selected_sections:
        pca = pca.filter(section_names__contained_by=selected_sections)
    if selected_offices:
        pca = pca.filter(offices_set__contained_by=selected_offices)
    
    if selected_donor:
        pca = pca.filter(donors__contains=[selected_donor])

    locations = get_interventions_details(pca)

    reports = get_filtered_activtyrepot(request)
    # reports_locations = [x['location_alternate_name'] for x in reports.exclude(location_alternate_name='').values('location_alternate_name').distinct()]
    
    # served_locations = list(reports.values('id', 'site_id', 'location_name', 'location_longitude', 'location_latitude',\
    # 'indicator_units', 'location_adminlevel_governorate', 'location_adminlevel_caza', \
    # 'location_adminlevel_caza_code', 'location_adminlevel_cadastral_area', 'location_adminlevel_cadastral_area_code', \
    # 'partner_label', 'indicator_value', ))
    served_locations = reports.values('location_longitude', 'location_latitude',\
     'location_adminlevel_cadastral_area', 'location_adminlevel_cadastral_area_code', \
    ).distinct()

    for sl in served_locations:
        if sl['location_adminlevel_cadastral_area_code'] in ['NA', '', None, ' '] and sl['location_longitude'] not in ['NA', '', None, ' ']:
            served_locations['location_longitude'] = None
            served_locations['location_latitude'] = None
            try:
                served_locations['location_adminlevel_cadastral_area_code'] = Location.objects.filter(longitude=sl['location_longitude'], latitude=sl['location_latitude']).first().cas_code
                served_locations['location_longitude'] = None
                served_locations['location_latitude'] = None
            except:
                pass
    served_locations = [item for item in served_locations if item['location_adminlevel_cadastral_area_code']!= None or item['location_longitude']!= None]

    # return JsonResponse({'planned_locations': locations, 'served_locations': {}})
    print(len(served_locations))
    return JsonResponse({'planned_locations': locations, 'served_locations': json.dumps(served_locations)})


class DonorProgrammeResultsView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/donor_results.html'

    def get_context_data(self, **kwargs):      

        reports = get_filtered_activtyrepot(self.request)
        indicators = reports.filter(ai_indicator__main_master_indicator__master_indicator=True)\
            .values('ai_indicator__main_master_indicator__id',
                    'ai_indicator__main_master_indicator__measurement_type',
                    'ai_indicator__main_master_indicator__name',
                    'ai_indicator__activity__database__section__logo',
                    'ai_indicator__main_master_indicator__units',
                    'ai_indicator__main_master_indicator__reporting_level',
                    'ai_indicator__main_master_indicator__target',
                    'ai_indicator__main_master_indicator__status_color',
                    'ai_indicator__main_master_indicator__status',
                    'ai_indicator__main_master_indicator__cumulative_values',
                    'ai_indicator__main_master_indicator__values_tags').annotate(ftotal=Sum('indicator_value')).distinct()       
        return {
            'indicators': indicators,
            'count': indicators.count(),
        }


class DonorFundingView(LoginRequiredMixin,TemplateView):
    '''
    ZS: Extract donor funding values from etools PCA
    '''
    template_name = 'etools/donor_funding.html'

    def get_context_data(self, **kwargs):
        selected_donor = self.request.GET.get('donor', 'G45301')
        current_year = datetime.date.today().year
        datan = []
        pointStart = 2013
        for y in range(pointStart, current_year + 1):
            year_total = 0
            for pca in PCA.objects.filter(start__year = y):
                for donation in pca.donors_set:
                    if donation['donor'] == selected_donor:
                        year_total += float(donation['value'])
            datan.append([y, int(year_total)])
        
        result = {}
        result[selected_donor] = { 'name':selected_donor, 'data':datan }
        return { 'data_set1': json.dumps(list(result.values())), 'pointStart': pointStart, 'currentYear': current_year}


class PartnerProfileView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/partner_profile.html'

    def get_context_data(self, **kwargs):

        partners_info = []
        sections = {}
        donors = {}
        now = datetime.datetime.now()
        # sections = Section.objects.filter(etools=True)
        donors_set = PCA.objects.filter(donors__len__gt=0).values('donors')
        for item in donors_set:
            for donor in item['donors']:
                donors[donor] = donor

        sections_set = PCA.objects.filter(section_names__len__gt=0).values('section_names')
        for item in sections_set:
            for section in item['section_names']:
                sections[section] = section

        years = (now.year, now.year - 1)
        engagements = Engagement.objects.filter(start_date__year__in=years).exclude(status=Engagement.CANCELLED)
        # engagements = Engagement.objects.exclude(status=Engagement.CANCELLED) 
        spot_checks = engagements.filter(engagement_type='sc')
        audits = engagements.filter(engagement_type='audit')
        micro_assessments = engagements.filter(engagement_type='ma')
        special_audits = engagements.filter(engagement_type='sa')

        interventions = PCA.objects.filter(end__year__in=years).exclude(status=PCA.CANCELLED)
        
        # interventions = PCA.objects.filter(end__year=now.year).exclude(status=PCA.CANCELLED)
        active_interventions = interventions.filter(status=PCA.ACTIVE)

        interventions_pd = interventions.filter(document_type=PCA.PD)
        active_interventions_pd = interventions_pd.filter(status=PCA.ACTIVE)
        
        interventions_sffa = interventions.filter(document_type=PCA.SSFA)
        active_interventions_sffa = interventions_sffa.filter(status=PCA.ACTIVE)

        # visits = TravelActivity.objects.filter(travel_type=TravelType.PROGRAMME_MONITORING, travel__start_date__year=now.year)
        visits = TravelActivity.objects.filter(travel_type=TravelType.PROGRAMME_MONITORING, travel__start_date__year__in=years)
        programmatic_visits = visits.exclude(travel__status=Travel.CANCELLED).exclude(travel__status=Travel.REJECTED)
        programmatic_visits_planned = visits.filter(travel__status=Travel.PLANNED)
        programmatic_visits_submitted = visits.filter(travel__status=Travel.SUBMITTED)
        programmatic_visits_approved = visits.filter(travel__status=Travel.APPROVED)
        programmatic_visits_completed = visits.filter(travel__status=Travel.COMPLETED)

        partners = PartnerOrganization.objects.exclude(hidden=True).exclude(deleted_flag=True).exclude(short_name='')

        partners_ids_and_names = []
        partners_ids = []
        for partner in partners:
            partners_ids_and_names.append({'id': partner.id, 'text': partner.short_name,})
            partners_ids.append(partner.id)

        partners_info = get_partner_profile_details()
               
        return {
            'donors': donors,
            'sections': sections,
            'partners_ids_and_names': json.dumps(partners_ids_and_names),
            'partners_ids': json.dumps(partners_ids),
            'nbr_interventions': interventions.count(),
            'nbr_active_interventions': active_interventions.count(),
            'nbr_pds': interventions_pd.count(),
            'nbr_active_pds': active_interventions_pd.count(),
            'nbr_sffas': interventions_sffa.count(),
            'nbr_active_sffas': active_interventions_sffa.count(),
            'nbr_partners': len(partners_ids),
            'nbr_spot_checks': spot_checks.count(),
            'nbr_audits': audits.count(),
            'nbr_micro_assessments': micro_assessments.count(),
            'nbr_special_audits': special_audits.count(),
            'programmatic_visits': programmatic_visits.count(),
            'programmatic_visits_planned': programmatic_visits_planned.count(),
            'programmatic_visits_submitted': programmatic_visits_submitted.count(),
            'programmatic_visits_approved': programmatic_visits_approved.count(),
            'programmatic_visits_completed': programmatic_visits_completed.count(),
            'partners_info': partners_info,
            'nbr_other_filters': micro_assessments.count() + spot_checks.count() + audits.count() + special_audits.count(),
            'partners_info': json.dumps(partners_info, indent=4, sort_keys=True, default=str),
            'partners_info_raw': partners_info,
        }


class PartnerProfileMapView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/partner_profile_map.html'

    def get_context_data(self, **kwargs):

        selected_partner = self.request.GET.get('partner_id', 0)
        partner = PartnerOrganization.objects.get(id=selected_partner)

        now = datetime.datetime.now()

        data_set = PCA.objects.filter(partner_id=partner.etl_id,
                                      end__year=now.year).exclude(status=PCA.CANCELLED)

        # locations = get_interventions_details(data_set)

        from django.db import connection
        cursor = connection.cursor()

        cursor.execute(
            "SELECT DISTINCT ar.site_id, ar.location_name, ar.location_longitude, ar.location_latitude, "
            "ar.indicator_units, ar.location_adminlevel_governorate, ar.location_adminlevel_caza, "
            "ar.location_adminlevel_caza_code, ar.location_adminlevel_cadastral_area, "
            "ar.location_adminlevel_cadastral_area_code, ar.partner_label, ai.name AS indicator_name, "
            "ai.cumulative_values ->> 'months'::text AS cumulative_value "
            "FROM public.activityinfo_indicator ai "
            "INNER JOIN public.activityinfo_activityreport ar ON ai.id = ar.ai_indicator_id "
            "INNER JOIN public.activityinfo_activity aa ON aa.id = ai.activity_id "
            "INNER JOIN public.etools_pca pmp ON pmp.id = aa.programme_document_id "
            "INNER JOIN public.etools_partnerorganization po ON po.id = pmp.partner_id "
            "WHERE pmp.partner_id = %s ",
            [int(selected_partner)])
        rows = cursor.fetchall()

        locations = {}
        ctr = 0
        for item in rows:
            if not item[2] or not item[3]:
                continue
            if item[0] not in locations:
                ctr += 1
                locations[item[0]] = {
                    'location_name': item[1],
                    'location_longitude': item[2],
                    'location_latitude': item[3],
                    'governorate': item[5],
                    'caza': '{}-{}'.format(item[6], item[7]),
                    'cadastral': '{}-{}'.format(item[8], item[9]),
                    'indicators': []
                }

            try:
                cumulative_value = "{:,}".format(round(float(item[12]), 1))
            except Exception:
                cumulative_value = 0

            locations[item[0]]['indicators'].append({
                'indicator_units': item[4].upper(),
                'partner_label': item[10],
                'indicator_name': item[11],
                'cumulative_value': cumulative_value,
            })

        locations = json.dumps(locations.values())
        # print(locations)

        return {
            'selected_partner': selected_partner,
            'partner': partner,
            'locations': locations,
        }


class InterventionsView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/interventions.html'

    def get_context_data(self, **kwargs):

        document_type = self.request.GET.get('document_type', 'all')
        status = self.request.GET.get('status', 'all')
        now = datetime.datetime.now()

        interventions = PCA.objects.filter(end__year=now.year).exclude(status=PCA.CANCELLED)
        active_interventions = interventions.filter(status=PCA.ACTIVE)

        interventions_pd = interventions.filter(document_type=PCA.PD)
        active_interventions_pd = interventions_pd.filter(status=PCA.ACTIVE)

        interventions_sffa = interventions.filter(document_type=PCA.SSFA)
        active_interventions_sffa = interventions_sffa.filter(status=PCA.ACTIVE)

        data_set = PCA.objects.filter(end__year=now.year).exclude(status=PCA.CANCELLED)

        if not document_type == 'all':
            data_set = data_set.filter(document_type=document_type)

        if status == 'active':
            data_set = data_set.filter(status=PCA.ACTIVE)

        locations = get_interventions_details(data_set)

        return {
            'locations': locations,
            'nbr_interventions': interventions.count(),
            'nbr_active_interventions': active_interventions.count(),
            'nbr_pds': interventions_pd.count(),
            'nbr_active_pds': active_interventions_pd.count(),
            'nbr_sffas': interventions_sffa.count(),
            'nbr_active_sffas': active_interventions_sffa.count(),
        }


class InterventionExportViewSet(ListView):

    model = PCA
    queryset = PCA.objects.all()

    def get(self, request, *args, **kwargs):

        now = datetime.datetime.now()

        interventions = PCA.objects.filter(end__year=now.year).exclude(status=PCA.CANCELLED)
        locations = get_interventions_details(interventions, all_locations=True, json_dumps=False)
        # print(locations)

        filename = "extraction.csv"

        fields = locations[0].keys()
        header = locations[0].values()
        meta = {
            'file': filename,
            # 'file': '/{}/{}'.format('tmp', filename),
            'queryset': locations,
            'fields': fields,
            'header': fields
        }
        from pivoting.gistfile import get_model_as_csv_file_response
        return get_model_as_csv_file_response(meta, content_type='text/csv', filename=filename)


class TripsMonitoringView(LoginRequiredMixin,TemplateView):

    template_name = 'etools/trip_monitoring2.html'

    def get_context_data(self, **kwargs):
        now = datetime.datetime.now()
        travel_status = self.request.GET.get('travel_status', 0)
        selected_month = self.request.GET.get('month', 0)
        selected_section = self.request.GET.get('section', 0)
        selected_year = self.request.GET.get('year', 0)
        selected_partner = self.request.GET.get('partner', 0)
        selected_donor = self.request.GET.get('donor', 0)

        sections = Section.objects.filter(etools=True)
        offices = Office.objects.all()
        donors_set = PCA.objects.filter(end__year=now.year,
                                        donors__isnull=False,
                                        donors__len__gt=0).values('number', 'donors').distinct()

        donors = {}
        for item in donors_set:
            for donor in item['donors']:
                donors[donor] = donor

        visits_no_partner = TravelActivity.objects.filter(travel_type=TravelType.PROGRAMME_MONITORING,
                                                          travel__start_date__year=now.year,
                                                          partner__isnull=True)\
            .exclude(travel__status=Travel.CANCELLED).exclude(travel__status=Travel.REJECTED)

        visits = TravelActivity.objects.filter(travel_type=TravelType.PROGRAMME_MONITORING,
                                               travel__start_date__year=now.year,
                                               partner__isnull=False)\
            .exclude(travel__status=Travel.CANCELLED).exclude(travel__status=Travel.REJECTED)

        if selected_month and not selected_month == '0':
            visits = visits.filter(travel__start_date__month=selected_month)
        if selected_year and not selected_year == '0':
            visits = visits.filter(travel__start_date__year=selected_year)
        if selected_section and not selected_section == '0':
            visits = visits.filter(travel__section=selected_section)
        if selected_donor and not selected_donor == '0':
            pass
            # visits = visits.filter(partnership__donors__values__contains=selected_donor)

        partners = visits.values('partner_id', 'partner__name').distinct()

        trips = visits
        if travel_status and not travel_status == '0':
            trips = visits.filter(travel__status=travel_status)
        if travel_status == 'completed_report':
            trips = visits.filter(travel__have_hact__gt=0)

        programmatic_visits = visits
        programmatic_visits_planned = visits.filter(travel__status=Travel.PLANNED)
        programmatic_visits_submitted = visits.filter(travel__status=Travel.SUBMITTED)
        programmatic_visits_approved = visits.filter(travel__status=Travel.APPROVED)
        programmatic_visits_completed = visits.filter(travel__status=Travel.COMPLETED)
        programmatic_visits_completed_no_report = programmatic_visits_completed.filter(travel__have_hact=0)
        programmatic_visits_completed_report = programmatic_visits_completed.filter(travel__have_hact__gt=0)

        trip_details = get_trip_details(trips)

        months = []
        for i in range(1, 13):
            months.append({
                'month': i,
                'month_name': (datetime.date(2008, i, 1).strftime('%B'))
            })

        trips_per_month = {Travel.SUBMITTED: [], Travel.APPROVED: [], Travel.COMPLETED: []}

        for item in [Travel.SUBMITTED, Travel.APPROVED, Travel.COMPLETED]:
            instances = visits.filter(travel__status=item)
            for m in months:
                ctr = instances.filter(travel__start_date__month=m['month'])
                trips_per_month[item].append({
                    'time': '{}-{}-{}'.format(now.year, m['month'], now.day),
                    'y': ctr.count(),
                    # 'y': random.randint(1, 50),
                    'x': m['month_name'],
                    'type': item.upper()
                })

        trips_per_month = json.dumps(trips_per_month.values())

        return {
            'months': months,
            'sections': sections,
            'offices': offices,
            'partners': partners,
            'trip_details': trip_details,
            'travel_status': travel_status,
            'selected_month': selected_month,
            'selected_year': selected_year,
            'selected_section': selected_section,
            'selected_partner': selected_partner,
            'selected_donor': selected_donor,
            'trips_per_month': trips_per_month,
            'donors': donors,
            'programmatic_visits': programmatic_visits.count(),
            'programmatic_visits_planned': programmatic_visits_planned.count(),
            'programmatic_visits_submitted': programmatic_visits_submitted.count(),
            'programmatic_visits_approved': programmatic_visits_approved.count(),
            'programmatic_visits_completed': programmatic_visits_completed.count(),
            'programmatic_visits_completed_report': programmatic_visits_completed_report.count(),
            'programmatic_visits_completed_no_report': programmatic_visits_completed_no_report.count()
        }
