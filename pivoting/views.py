import datetime
import io
import json
import operator
import random
import numpy as np
import os
import calendar
from collections import Counter
from decimal import Decimal
from django.views.generic import ListView, TemplateView
from django.db.models import F
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator, EmptyPage
from .models import Database, ReportingYear, ActivityReportNew
from .models import SimpleLocation, CadasterLocation
from .models import CadasterLocation, GovernorateLocation, DistrictLocation
from .models import NeuroReport, NeuroReportComment
from .models import Resource, ResourceTag, ResourceTopic, ResourceType, Map
from django.http import HttpResponse, JsonResponse, FileResponse
from django.db.models import Q, Sum, Max, Count
from etools.models import PCA, PartnerOrganization
from .utils import *
from .queries import DATABASE_ACTIVITYINFO, NEUROREPORT, NEUROREPORT_ACTIVITYINFO, ACTIVITYINFO_SUMMARY, ACTIVITYINFO_PCA_SUMMARY
from .queries import DONOR_ACTIVITYINFO_PURE, PCA_ACTIVITYINFO_PURE
from .queries import PCA_SUMMARY_PARTNERS, PCA_ENDING_SOON, PCAS_MASTER_INDICATORS, PCA_UNNESTED_DONORS
from .queries import MASTER_INDICATORS_COUNT, MASTER_INDICATORS_AVERAGE, MASTER_INDICATORS_MAXIMUM
from .queries import MASTER_INDICATORS_SUM, MASTER_SUB_INDICATORS, ALL_MASTER_INDICATORS
from .queries import ETOOLS_LOCATIONS, PCA_SUMMARY_SECTIONS
from .templatetags.util_tagsv2 import get_array_as
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from openpyxl import Workbook
from django.db import connection
import csv
import pandas as pd
from django.utils import timezone


def find_values_with_highest_occurrences(dictionaries, target_key):
    occurrences = {}
    for dictionary in dictionaries:
        if target_key in dictionary:
            value = dictionary[target_key]
            if value in occurrences:
                occurrences[value] += 1
            else:
                occurrences[value] = 1

    if occurrences:
        max_occurrences = max(occurrences.values())
        most_common_values = [value for value, count in occurrences.items() if count == max_occurrences]
        return most_common_values

    return []


def load_database_activityinfo(request):
    id = request.GET.get('id', 0)
    from django.db import connection
    cursor = connection.cursor()
    cursor.execute(DATABASE_ACTIVITYINFO, [id])
    result = cursor.fetchall()
    items = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]       
    
    return JsonResponse({'items': json.dumps(items, indent=4, sort_keys=True, default=str)})


def load_neuroreport_activityinfo(request):
    id = request.GET.get('id', 0)
    emergency = request.GET.get('emergency', None)
    from django.db import connection
    cursor = connection.cursor()

    emergency_values = "'Yes','No'"

    if emergency:
        emergency_values = "'" + emergency.title() + "'"

    cursor.execute(NEUROREPORT_ACTIVITYINFO.replace('EMERGENCY_VALUES', emergency_values), [id])
    result = cursor.fetchall()
    items = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]
    return JsonResponse({'items': json.dumps(items, indent=4, sort_keys=True, default=str)})


def load_sub_indicators(request):
    id = request.GET.get('id', 0)
    from django.db import connection
    cursor = connection.cursor()
    cursor.execute(MASTER_SUB_INDICATORS, [id])
    result = cursor.fetchall()
    items = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
    return JsonResponse({'items': json.dumps(items, indent=4, sort_keys=True, default=str)})


def load_partner_staff(request):
    id = request.GET.get('id', 0)
    partership = PartnerOrganization.objects.get(id=id)
    items = []
    if partership.staff_members:
        items = partership.staff_members
    return JsonResponse({'items': json.dumps(items, indent=4, sort_keys=True, default=str)})


def load_pca_details(request):
    id = request.GET.get('id', 0)
    pca = PCA.objects.get(id=id)
    data = {
        'title': pca.title,
        'sections': pca.section_names,
        'budget_currency': pca.budget_currency,
        'offices': pca.offices_set,
        'donors': pca.donors,
        'donors_set': pca.donors_set,
        'interventions': ActivityReportNew.objects.filter(project_label=pca.number).count()
    }
    return JsonResponse(data)
    # return JsonResponse({'items': json.dumps(data, indent=4, sort_keys=True, default=str)})


def load_pca_activityinfo(request):
    id = request.GET.get('id', 0)
    from django.db import connection
    cursor = connection.cursor()
    cursor.execute(PCA_ACTIVITYINFO_PURE, [id])
    result = cursor.fetchall()
    items = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]
    return JsonResponse({'items': json.dumps(items, indent=4, sort_keys=True, default=str)})

def load_donors_mapping_data(request):
    selected_donors = request.GET.getlist('donors[]')
    selected_pcas = request.GET.getlist('pcas[]')
    selected_partners = request.GET.getlist('partners[]')
    selected_csotypes = request.GET.getlist('csotypes[]')
    selected_sections = request.GET.getlist('sections[]')
    selected_statuses = request.GET.getlist('statuses[]')
    selected_offices = request.GET.getlist('offices[]')
    selected_grants = request.GET.getlist('grants[]')
    selected_governorates = request.GET.getlist('governorates[]')
    selected_years = request.GET.get('years')

    pca = PCA.objects.exclude(status__in=['draft',]).only(
        'id', 'partner_name', 'document_type', 'country_programme', 'number',
        'partner_id', 'title', 'project_type', 'status', 'start', 'end', 
        'donor_codes', 'donors', 'location_p_codes', 'section_names',
        'donors_set', 'offices_set')
    if selected_partners:
        pca = pca.filter(partner__short_name__in=selected_partners)
    if selected_csotypes:
        pca = pca.filter(partner__cso_type__in=selected_csotypes)
    if selected_sections:
        # pca = pca.exclude(section_names=[]).filter(section_names__contained_by=selected_sections)
        query = Q()
        for value in selected_sections:
            query |= Q(section_names__contains=[value])
        pca = pca.filter(query)
    
    if selected_grants:
        query = Q()
        for value in selected_grants:
            query |= Q(grants__contains=[value])
        pca = pca.filter(query)

    if selected_offices:
        query = Q()
        for value in selected_offices:
            query |= Q(offices_set__contains=[value])
        pca = pca.filter(query)

    if selected_pcas:
        pca = pca.filter(number__in=selected_pcas)

    if selected_statuses:
        pca = pca.filter(status__in=selected_statuses)

    if selected_donors:
        query = Q()
        for value in selected_donors:
            query |= Q(donors__contains=[value])
        pca = pca.filter(query)

    if selected_years:  # '2014,2023'
        start_year = int(selected_years.split(',')[0])
        end_year = int(selected_years.split(',')[1])
        pca = pca.filter(start__year__gte=start_year)
        pca = pca.filter(start__year__lte=end_year)

    total_programs = pca.count()

    pca_ids = list(set(pca.values_list('id', flat=True)))
    
    pca_numbers = []

    for item in pca.values():
        item_number = item['number']
        pca_numbers.append(item_number)
        if '-' in item_number:
            pca_numbers.append(item_number.split('-')[0])

    programs = pca.values()

    for item in programs:
        item['donations'] = sum(float(x['value']) for x in item['donors_set'])
        item['interventions'] = ActivityReportNew.objects.filter(project_label=item['number'].split('-')[0]).count()
        item['sections'] = get_array_as(item['section_names'])
        item['offices'] = get_array_as(item['offices_set'])
    programs = list(programs)

    unnseted_programs = []
    if len(pca_ids) > 0:
        ids_as_string = (',').join([str(id) for id in pca_ids])
        from django.db import connection
        cursor = connection.cursor()
        cursor.execute(PCA_UNNESTED_DONORS.replace('IDS', ids_as_string), [])
        result = cursor.fetchall()
        unnseted_programs = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]
    total_donations = sum(float(item['Funds']) for item in unnseted_programs)

    planned_locations_count = 0
    planned_locations_count_unique = 0
    planned_locations = []
    if len(pca_ids) > 0:
        # count projects per location
        for program in pca:
            for location in program.location_p_codes:
                planned_locations.append(location)

        planned_locations_count = len(planned_locations)
        planned_locations_count_unique = len(set(planned_locations))

    interventions = ActivityReportNew.objects.filter(project_label__in=pca_numbers)

    if selected_governorates:
        interventions = interventions.filter(location_adminlevel_governorate__in=selected_governorates)
    if selected_statuses:
        pca = pca.filter(status__in=selected_statuses)

    total_interventions = interventions.count()
    unique_locations_count = len(set(list(interventions.values_list('location_latitude', 'location_longitude'))))

    # get all master indicates that are linked to the select pcs through the leaf indicators
    master_indicators = []
    reported_indicators_count = 0
    if len(pca_ids) > 0:
        ids_as_string = (',').join([str(id) for id in pca_ids])
        pca_numbers_as_string = (',').join(["'" + pca_number + "'" for pca_number in pca_numbers])

        from django.db import connection
        cursor = connection.cursor()
        cursor.execute(PCAS_MASTER_INDICATORS.replace('PNUMBERS', pca_numbers_as_string), [])
        result = cursor.fetchall()
        master_indicators = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]
        master_indicators = [item for item in master_indicators if item['Value'] and item['Value'] > 0]

        if len(master_indicators) > 0:
            reported_indicators_count = len(set([sub['Id'] for sub in master_indicators]))

    return_value = {
        'programs': json.dumps(programs, indent=4, sort_keys=True, default=str),
        'unnseted_programs': json.dumps(unnseted_programs, indent=4, sort_keys=True, default=str),
        'master_indicators': json.dumps(master_indicators, indent=4, sort_keys=True, default=str),
        'total_programs': total_programs,
        'total_donations': total_donations,
        'total_interventions': total_interventions,
        'planned_locations_count': planned_locations_count,
        'planned_locations_count_unique': planned_locations_count_unique,
        'unique_locations_count': unique_locations_count,
        'reported_indicators_count': reported_indicators_count,
        }
    return JsonResponse(return_value)

def load_donors_mapping_locations(request):
    selected_donors = request.GET.getlist('donors[]')
    selected_pcas = request.GET.getlist('pcas[]')
    selected_partners = request.GET.getlist('partners[]')
    selected_sections = request.GET.getlist('sections[]')
    selected_statuses = request.GET.getlist('statuses[]')
    selected_offices = request.GET.getlist('offices[]')
    selected_years = request.GET.get('years')

    pca = PCA.objects.exclude(status__in=['draft',])
    if selected_partners:
        pca = pca.filter(partner__short_name__in=selected_partners)
    if selected_sections:
        pca = pca.exclude(section_names=[]).filter(section_names__contained_by=selected_sections)
    if selected_offices:
        pca = pca.exclude(offices_set=[]).filter(offices_set__contained_by=selected_offices)
    if selected_pcas:
        pca = pca.filter(number__in=selected_pcas)
    if selected_statuses:
        pca = pca.filter(status__in=selected_statuses)
    if selected_donors:
        pca = pca.exclude(donors_set=[]).filter(donors__contained_by=selected_donors)
    if selected_years:
        start_year = int(selected_years.split(',')[0])
        end_year = int(selected_years.split(',')[1])
        pca = pca.filter(start__year__gte=start_year)
        pca = pca.filter(start__year__lte=end_year)

    pca_ids = list(set(pca.values_list('id', flat=True)))
    pca_numbers = list(item.split('-')[0] for item in set(pca.values_list('number', flat=True)))
    # pca = pca.values('id','number','title', 'value','section_names','donors_set','start','end','partner_short_name','budget_currency','total_budget','status','offices_set')
    programs = pca.values()

    programs = list(programs)

    locations = []
    planned_locations = []
    planned_locations_cadasters = []
    if len(pca_ids) > 0:
        # count projects per location
        for program in pca:
            for location in program.location_p_codes:
                locations.append(location)
               
        locations_count = dict(Counter(locations))
        for key in locations_count:
            location_record = SimpleLocation.objects.filter(p_code=key).first()
            if location_record:
                if location_record.cas_code != '':
                    if next((item for item in planned_locations_cadasters if item["code"] == location_record.cas_code), None) is None:
                        try:
                            cadaster = CadasterLocation.objects.get(code=location_record.cas_code)
                            planned_locations_cadasters.append({
                                'code': cadaster.code,
                                'location_name': cadaster.name,
                                'paths': cadaster.polygon_coordinates,
                                'pcount': str(locations_count[key]),
                                # 'coloor': '#2222FF'
                            })
                        except:
                            pass
                    else:
                        for item in planned_locations_cadasters:
                            if item['code'] == location_record.cas_code:
                                item['pcount'] = str(int(item['pcount']) + locations_count[key])

    if len(planned_locations) > 200:
        planned_locations = planned_locations[0:200]

    reports = ActivityReportNew.objects.filter(project_label__in=pca_numbers)

    interventions_per_governorates_codes = list(reports.exclude(location_adminlevel_governorate_code='').values('location_adminlevel_governorate_code').annotate(interventions=Count('location_adminlevel_governorate_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))

    interventions_per_cazas_codes = list(reports.exclude(location_adminlevel_caza_code='').values('location_adminlevel_caza_code').annotate(interventions=Count('location_adminlevel_caza_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))

    interventions_per_cadastrals_codes = list(reports.exclude(location_adminlevel_cadastral_area_code='').values('location_adminlevel_cadastral_area_code').annotate(interventions=Count('location_adminlevel_cadastral_area_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))

    for site in interventions_per_governorates_codes:
        site['code'] = site.pop('location_adminlevel_governorate_code')
        governorate = GovernorateLocation.objects.get(ai_id=site['code'])
        site['location_name'] = governorate.name
        site['paths'] = governorate.polygon_coordinates
        site['type'] = 'governorate'

    for site in interventions_per_cazas_codes:
        site['code'] = site.pop('location_adminlevel_caza_code')
        try:
            caza = DistrictLocation.objects.get(code=site['code'])
            site['location_name'] = caza.name
            site['paths'] = caza.polygon_coordinates
            site['type'] = 'caza'
        except:
            pass

    for site in interventions_per_cadastrals_codes:
        site['code'] = site.pop('location_adminlevel_cadastral_area_code')
        cadaster = CadasterLocation.objects.get(code=site['code'])
        site['location_name'] = cadaster.name
        site['paths'] = cadaster.polygon_coordinates
        site['type'] = 'cadaster'

    if len(interventions_per_cadastrals_codes) > 500:
        interventions_per_cadastrals_codes = interventions_per_cadastrals_codes[0:500]

    return JsonResponse(
        {
            'planned_locations': json.dumps(planned_locations, indent=4, sort_keys=True, default=str),
            'planned_locations_cadasters': json.dumps(planned_locations_cadasters, indent=4, sort_keys=True, default=str),
            'interventions_per_governorates_codes': json.dumps(interventions_per_governorates_codes, indent=4, sort_keys=True, default=str),
            'interventions_per_cazas_codes': json.dumps(interventions_per_cazas_codes, indent=4, sort_keys=True, default=str),
            'interventions_per_cadastrals_codes': json.dumps(interventions_per_cadastrals_codes, indent=4, sort_keys=True, default=str),
        }
    )    

def load_ry_master_indicators(request):
    # load master indicators for all databases in a reporting year
    ryid = request.GET.get('ryid', 0)
    selected_donors = request.GET.getlist('donors[]')
    selected_pcas = request.GET.getlist('pcas[]')
    selected_partners = request.GET.getlist('partners[]')
    selected_sections = request.GET.getlist('sections[]')
    selected_statuses = request.GET.getlist('statuses[]')
    selected_offices = request.GET.getlist('offices[]')
    selected_years = request.GET.get('years')

    pca = PCA.objects.exclude(status__in=['draft',]).only('id',	'partner_name',	'document_type', 'country_programme', 'number','partner_id','title',	'project_type','status','start','end','donor_codes','donors','location_p_codes','section_names','donors_set','offices_set')
    if selected_partners:
        pca = pca.filter(partner__short_name__in=selected_partners)
    if selected_sections:
        pca = pca.exclude(section_names=[]).filter(section_names__contained_by=selected_sections)
    if selected_offices:
        pca = pca.exclude(offices_set=[]).filter(offices_set__contained_by=selected_offices)
    if selected_pcas:
        pca = pca.filter(number__in=selected_pcas)
    if selected_statuses:
        pca = pca.filter(status__in=selected_statuses)
    if selected_donors:
        pca = pca.exclude(donors_set=[]).filter(donors__contained_by=selected_donors)
    if selected_years:
        start_year = int(selected_years.split(',')[0])
        end_year = int(selected_years.split(',')[1])
        pca = pca.filter(start__year__gte=start_year)
        pca = pca.filter(start__year__lte=end_year)

    pca_numbers = list(item.split('-')[0] for item in set(pca.values_list('number', flat=True)))
    interventions = ActivityReportNew.objects.filter(project_label__in=pca_numbers)
    total_interventions = interventions.count()
    unique_locations_count = len(set(list(interventions.values_list('location_adminlevel_cadastral_area_code',))))

    databases = Database.objects.filter(reporting_year__id=ryid)

    reported_indicators = []

    if databases.count() > 0:
        from django.db import connection
        reported_indicators = []
        ids_as_string = (',').join([str(database.id) for database in databases])
        pca_numbers_as_string = (',').join(["'" + pca_number + "'" for pca_number in pca_numbers])
        cursor = connection.cursor()
        cursor.execute(ALL_MASTER_INDICATORS.replace('IDS', ids_as_string).replace('PNUMBERS', pca_numbers_as_string), [])
        result = cursor.fetchall()
        reported_indicators = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        reported_indicators = [item for item in reported_indicators if item['value'] is not None]

    return_value = {
        'reported_indicators': json.dumps(reported_indicators, indent=4, sort_keys=True, default=str),
        'total_interventions': total_interventions,
        'unique_locations_count':unique_locations_count,
        }
    return JsonResponse(return_value)


def load_donor_activityinfo(request):
    id = request.GET.get('id', 'xxx')
    from django.db import connection
    cursor = connection.cursor()
    cursor.execute(DONOR_ACTIVITYINFO_PURE, [id])
    result = cursor.fetchall()
    items = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]
    return JsonResponse({'items': json.dumps(items, indent=4, sort_keys=True, default=str)})


def load_database_intervention_locations(request):
    id = request.GET.get('id', 0)
    selectd_partners = request.GET.getlist('partners[]')
    selectd_months = request.GET.getlist('months[]')
    selectd_governorates = request.GET.getlist('governorates[]')
    selectd_cazas = request.GET.getlist('cazas[]')
    selectd_programs = request.GET.getlist('programs[]')

    reports = ActivityReportNew.objects.filter(dbase_id=id, funded_by='UNICEF')

    if selectd_partners:
        reports = reports.filter(partner_label__in=selectd_partners)

    if selectd_months:
        reports = reports.filter(month_name__in=selectd_months)

    if selectd_governorates:
        reports = reports.filter(location_adminlevel_governorate__in=selectd_governorates)

    if selectd_cazas:
        reports = reports.filter(location_adminlevel_caza__in=selectd_cazas)

    if selectd_programs:
        reports = reports.filter(project_label__in=selectd_programs)

    interventions_per_site = list(reports.values('location_name').annotate(interventions=Count('location_name'), location_latitude=Max('location_latitude'), location_longitude=Max('location_longitude'), indicator_values=Sum('indicator_value')))
    
    interventions_per_governorates_codes = list(reports.exclude(location_adminlevel_governorate_code='').values('location_adminlevel_governorate_code').annotate(interventions=Count('location_adminlevel_governorate_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))

    interventions_per_cazas_codes = list(reports.exclude(location_adminlevel_caza_code='').values('location_adminlevel_caza_code').annotate(interventions=Count('location_adminlevel_caza_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))

    interventions_per_cadastrals_codes = list(reports.exclude(location_adminlevel_cadastral_area_code='').values('location_adminlevel_cadastral_area_code').annotate(interventions=Count('location_adminlevel_cadastral_area_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    
    for site in interventions_per_governorates_codes:
        site['code'] = site.pop('location_adminlevel_governorate_code')
        governorate = GovernorateLocation.objects.get(ai_id=site['code'])
        site['location_name'] = governorate.name
        site['paths'] = governorate.polygon_coordinates
        site['type'] = 'governorate'

    for site in interventions_per_cazas_codes:
        site['code'] = site.pop('location_adminlevel_caza_code')
        try:
            caza = DistrictLocation.objects.get(code=site['code'])
            site['location_name'] = caza.name
            site['paths'] = caza.polygon_coordinates
            site['type'] = 'caza'
        except:
            pass
        
    for site in interventions_per_cadastrals_codes:
        site['code'] = site.pop('location_adminlevel_cadastral_area_code')
        cadaster = CadasterLocation.objects.get(code=site['code'])
        site['location_name'] = cadaster.name
        site['paths'] = cadaster.polygon_coordinates
        site['type'] = 'cadaster'
    
    for site in interventions_per_site:
        site['locations'] = ""
        try:
            site['location_latitude'] = float(site['location_latitude'])
            site['location_longitude'] = float(site['location_longitude'])
        except:
            site['location_latitude'] = 0
            site['location_longitude'] = 0

    # for site in interventions_per_site:
    #     site['location_latitude'] = float(site_location['latitude'])
    #     site['location_longitude'] = float(site_location['longitude'])

    return JsonResponse({
        # 'items': json.dumps(pca, indent=4, sort_keys=True, default=str),
        'interventions_per_governorates_codes': json.dumps(interventions_per_governorates_codes, indent=4, sort_keys=True, default=str),
        'interventions_per_cazas_codes': json.dumps(interventions_per_cazas_codes, indent=4, sort_keys=True, default=str),
        'interventions_per_cadastrals_codes': json.dumps(interventions_per_cadastrals_codes, indent=4, sort_keys=True, default=str),
        'interventions_per_site': json.dumps(interventions_per_site, indent=4, sort_keys=True, default=str),
    })


def load_intervention_mapping_dataxxx(request):
    id = request.GET.get('id', 0)
    selectd_partners = request.GET.getlist('partners[]')
    selectd_months = request.GET.getlist('months[]')
    selectd_governorates = request.GET.getlist('governorates[]')
    selectd_cazas = request.GET.getlist('cazas[]')
    selectd_programs = request.GET.getlist('programs[]')

    reports = ActivityReportNew.objects.filter(dbase_id=id, funded_by='UNICEF')

    if selectd_partners:
        reports = reports.filter(partner_label__in=selectd_partners)

    if selectd_months:
        reports = reports.filter(month_name__in=selectd_months)

    if selectd_governorates:
        reports = reports.filter(location_adminlevel_governorate__in=selectd_governorates)

    if selectd_cazas:
        reports = reports.filter(location_adminlevel_caza__in=selectd_cazas)

    if selectd_programs:
        reports = reports.filter(project_label__in=selectd_programs)

    interventions_per_governorates = list(reports.exclude(location_adminlevel_governorate='').values('location_adminlevel_governorate').annotate(interventions=Count('location_adminlevel_governorate'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    interventions_per_cazas = list(reports.exclude(location_adminlevel_caza='').values('location_adminlevel_caza').annotate(interventions=Count('location_adminlevel_caza'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    interventions_per_cadastrals = list(reports.exclude(location_adminlevel_cadastral_area='').values('location_adminlevel_cadastral_area').annotate(interventions=Count('location_adminlevel_cadastral_area'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    interventions_per_site = list(reports.values('location_name').annotate(interventions=Count('location_name'), location_latitude=Max('location_latitude'), location_longitude=Max('location_longitude'), indicator_values=Sum('indicator_value')))
    from .administrative_locations import GOVERNORATES, CAZAS, CADASTRALS
    for site in interventions_per_governorates:
        site_location = [item for item in GOVERNORATES if item["name"] == site['location_adminlevel_governorate']][0]
        site['location_name'] = site.pop('location_adminlevel_governorate')
        site['location_latitude'] = site_location['latitude']
        site['location_longitude'] = site_location['longitude']

    for site in interventions_per_cazas:
        site_location = [item for item in CAZAS if item["name"] == site['location_adminlevel_caza']][0]
        site['location_name'] = site.pop('location_adminlevel_caza')
        site['location_latitude'] = site_location['latitude']
        site['location_longitude'] = site_location['longitude']
    
    for site in interventions_per_cadastrals:
        site['location_name'] = site.pop('location_adminlevel_cadastral_area')
        try:
            site_location = [item for item in CADASTRALS if item["name"] == site['location_name']][0]
            site['location_latitude'] = site_location['latitude']
            site['location_longitude'] = site_location['longitude']
        except:
            site['location_latitude'] = 0
            site['location_longitude'] = 0
    
    for site in interventions_per_site:
        site['locations'] = ""
        try:
            site['location_latitude'] = float(site['location_latitude'])
            site['location_longitude'] = float(site['location_longitude'])
        except:
            site['location_latitude'] = 0
            site['location_longitude'] = 0

    # for site in interventions_per_site:
    #     site['location_latitude'] = float(site_location['latitude'])
    #     site['location_longitude'] = float(site_location['longitude'])

    return JsonResponse(
        {
            'interventions_per_governorates': json.dumps(interventions_per_governorates, indent=4, sort_keys=True, default=str),
            'interventions_per_cazas': json.dumps(interventions_per_cazas, indent=4, sort_keys=True, default=str),
            'interventions_per_site': json.dumps(interventions_per_site, indent=4, sort_keys=True, default=str),
            'interventions_per_cadastrals': json.dumps(interventions_per_cadastrals, indent=4, sort_keys=True, default=str),
        }
    )


def load_database_snapshot(request):
    id = request.GET.get('id', 0)

    reports = ActivityReportNew.objects.filter(dbase_id=id, funded_by='UNICEF')

    interventions_per_site = list(reports.values('location_name').annotate(interventions=Count('location_name'), location_latitude=Max('location_latitude'), location_longitude=Max('location_longitude'), indicator_values=Sum('indicator_value')))
    interventions_per_governorates_codes = list(reports.exclude(location_adminlevel_governorate_code='').values('location_adminlevel_governorate_code').annotate(interventions=Count('location_adminlevel_governorate_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    interventions_per_cazas_codes = list(reports.exclude(location_adminlevel_caza_code='').values('location_adminlevel_caza_code').annotate(interventions=Count('location_adminlevel_caza_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    interventions_per_cadastrals_codes = list(reports.exclude(location_adminlevel_cadastral_area_code='').values('location_adminlevel_cadastral_area_code').annotate(interventions=Count('location_adminlevel_cadastral_area_code'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    # COUNT INTERVENTIONS PER PARTNER
    interventions_per_partner = list(reports.exclude(location_adminlevel_caza_code='').values('partner_label').annotate(interventions=Count('partner_label'), locations=Count('location_name', distinct=True), indicator_values=Sum('indicator_value')))
    interventions_per_partner = sorted(interventions_per_partner, key=operator.itemgetter('interventions'), reverse=True)
    for site in interventions_per_governorates_codes:
        site['code'] = site.pop('location_adminlevel_governorate_code')
        governorate = GovernorateLocation.objects.get(ai_id=site['code'])
        site['location_name'] = governorate.name
        site['paths'] = governorate.polygon_coordinates
        site['type'] = 'governorate'

    for site in interventions_per_cazas_codes:
        site['code'] = site.pop('location_adminlevel_caza_code')
        try:
            caza = DistrictLocation.objects.get(code=site['code'])
            site['location_name'] = caza.name
            site['paths'] = caza.polygon_coordinates
            site['type'] = 'caza'
        except:
            pass

    for site in interventions_per_cadastrals_codes:
        site['code'] = site.pop('location_adminlevel_cadastral_area_code')
        cadaster = CadasterLocation.objects.get(code=site['code'])
        site['location_name'] = cadaster.name
        site['paths'] = cadaster.polygon_coordinates
        site['type'] = 'cadaster'

    for site in interventions_per_site:
        site['locations'] = ""
        try:
            site['location_latitude'] = float(site['location_latitude'])
            site['location_longitude'] = float(site['location_longitude'])
        except:
            site['location_latitude'] = 0
            site['location_longitude'] = 0

    from django.db import connection
    cursor = connection.cursor()
    cursor.execute(DATABASE_ACTIVITYINFO, [id])
    result = cursor.fetchall()
    items = [dict(zip([key[0].title() for key in cursor.description], row)) for row in result]
    return JsonResponse(
        {
            'interventions_per_governorates_codes': json.dumps(interventions_per_governorates_codes, indent=4, sort_keys=True, default=str),
            'interventions_per_cazas_codes': json.dumps(interventions_per_cazas_codes, indent=4, sort_keys=True, default=str),
            'interventions_per_cadastrals_codes': json.dumps(interventions_per_cadastrals_codes, indent=4, sort_keys=True, default=str),
            'interventions_per_site': json.dumps(interventions_per_site, indent=4, sort_keys=True, default=str),
            'interventions_per_partner': json.dumps(interventions_per_partner, indent=4, sort_keys=True, default=str),
            'items': json.dumps(items, indent=4, sort_keys=True, default=str)
        }
    )


def getGovernorate(district):
    governorates = {
        'Akkar': ['Akkar'],
        'Baalbek-El Hermel': ['Baalbek', 'El Hermel'],
        'Beirut': ['Beirut'],
        'Bekaa': ['Rachaya', 'West Bekaa', 'Zahle'],
        'Mount Lebanon': ['Aley', 'Baabda', 'Chouf', 'El Meten', 'Jbeil', 'Kesrwane'],
        'El Nabatieh': ['Bent Jbeil', 'El Nabatieh', 'Hasbaya', 'Marjaayoun'],
        'North': ['Bcharre', 'El Batroun', 'El Koura', 'El Minieh-Dennie', 'Tripoli', 'Zgharta'],
        'South': ['Jezzine', 'Saida', 'Sour',]
    }

    for key, value in governorates.items():
        if district in value:
            return key


def load_population_figures(request):
    file_path = os.path.join(settings.ROOT_DIR, 'pivoting/uploads/Population_figures_2026_NeuroDB.json')
    population_figures = []
    with open(file_path, 'r') as f:
        my_json_obj = json.load(f)

        age_groups = ['0 - 4', '5 - 9', '10 - 14', '15 - 19', '20 - 24', '25 - 29', '30 - 34', '35 - 39', '40 - 44', 
                      '45 - 49', '50 - 54', '55 - 59', '60 - 64', '65 - 69', '70 - 74', '75 - 79', '80 - 84', '85 and above']
        for rec in my_json_obj['LEB_BY_DISTRICT']:
            for age_group in age_groups:
                new_rec = {}
                new_rec['Nationality'] = 'LEB'
                new_rec['District'] = rec['District']
                new_rec['Age Group'] = age_group
                new_rec['Value'] = int(rec.get(age_group, 0))
                population_figures.append(new_rec)

        for rec in my_json_obj['SYR_BY_DISTRICT']:
            for age_group in age_groups:
                new_rec = {}
                new_rec['Nationality'] = 'SYR'
                new_rec['District'] = rec['District']
                new_rec['Age Group'] = age_group
                # new_rec['Value'] = int(rec[age_group])
                new_rec['Value'] = int(rec.get(age_group, 0))
                population_figures.append(new_rec)

        for rec in my_json_obj['PAL_BY_DISTRICT']:
            for age_group in age_groups:
                new_rec = {}
                new_rec['Nationality'] = 'PAL'
                new_rec['District'] = rec['Governorate']
                new_rec['Age Group'] = age_group
                new_rec['Value'] = int(rec.get(age_group, 0))
                population_figures.append(new_rec)
        
        for rec in my_json_obj['ALL_BY_DISTRICT']:
            new_rec = {}
            new_rec['Nationality'] = 'MIG'
            new_rec['District'] = rec['Governorate']
            new_rec['Age Group'] = "undefined"
            new_rec['Value'] = int(rec["TOTAL MIGRANTS"])
            population_figures.append(new_rec)

        for rec in population_figures:
            rec['Governorate'] = getGovernorate(rec['District'])

    return JsonResponse({
        'population_figures': json.dumps(population_figures, indent=4, sort_keys=True, default=str)
    })


def load_most_vulnerable(request):
    import openpyxl

    path = os.path.join(settings.ROOT_DIR, 'pivoting/uploads/list of 332 localities data_2022_08_19.xlsx')
    wb_obj = openpyxl.load_workbook(path)
    sheet_obj = wb_obj.active
    m_row = sheet_obj.max_row
    most_vulnerable = []
    for row in range(2, m_row + 1):
        governorate = sheet_obj.cell(row=row, column=2).value
        district = sheet_obj.cell(row=row, column=3).value
        cadaster = sheet_obj.cell(row=row, column=4).value
        vulnerability = sheet_obj.cell(row=row, column=10).value
        syr = {}
        prs = {}
        prl = {}
        leb = {}
        syr['Governorate'] = governorate
        syr['District'] = district
        syr['Cadaster'] = cadaster
        syr['Vulnerability Level'] = vulnerability
        syr['Nationality'] = "SYR"
        syr['Value'] = int(sheet_obj.cell(row=row, column=5).value)
        most_vulnerable.append(syr)
        prs['Governorate'] = governorate
        prs['District'] = district
        prs['Cadaster'] = cadaster
        prs['Vulnerability Level'] = vulnerability
        prs['Nationality'] = "PRS"
        prs['Value'] = int(sheet_obj.cell(row=row, column=6).value)
        most_vulnerable.append(prs)
        
        prl['Governorate'] = governorate
        prl['District'] = district
        prl['Cadaster'] = cadaster
        prl['Vulnerability Level'] = vulnerability
        prl['Nationality'] = "PRL"
        prl['Value'] = int(sheet_obj.cell(row=row, column=8).value)
        most_vulnerable.append(prl)
        leb['Governorate'] = governorate
        leb['District'] = district
        leb['Cadaster'] = cadaster
        leb['Vulnerability Level'] = vulnerability
        leb['Nationality'] = "LEB"
        leb['Value'] = int(sheet_obj.cell(row=row, column=9).value)
        most_vulnerable.append(leb)
    return JsonResponse(
        {
            'most_vulnerable': json.dumps(most_vulnerable, indent=4, sort_keys=True, default=str)
        }
    )


def percentageDaysPassed(reporting_year):
    # Calculate the percentage of days that have passed from current year
    current_date = datetime.date.today()
    total_days_in_year = (datetime.date(current_date.year + 1, 1, 1) - datetime.date(current_date.year, 1, 1)).days
    # days_passed = (current_date - datetime.date(current_date.year, 1, 1)).days
    days_passed = (current_date - datetime.date(int(reporting_year), 1, 1)).days
    result = (days_passed / total_days_in_year) * 100
    if result > 100:
        return 100
    return result


def setTrackingStatus(items, reporting_year):
    percentage_passed = int(percentageDaysPassed(reporting_year))
    for item in items:
        if not item['value'] or item['value'] == '' or item['value'] == '-':
            item['value'] = 0

        if item['awp_target'] and item['awp_target'] > 0:
            item['achieved'] = item['value'] * 100 / item['awp_target']
            if item['achieved'] - percentage_passed >= 10:
                item['tracking'] = 'over target'
            elif percentage_passed - item['achieved'] >= 10:
                item['tracking'] = 'off track'
            else:
                item['tracking'] = 'on track'
        else:
            item['achieved'] = ''
            item['tracking'] = 'no target'  

    return items


class DatabaseAnalyticalView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/database-analytical.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)
        database = Database.objects.get(id=id)
        section_databases = Database.objects.filter(section=database.section).order_by('-id')
        return {
            'database': database,
            'section_databases': section_databases,
        }


class DatabaseDashboardView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/database-dashboard.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)
        database = Database.objects.get(id=id)
        reporting_year = database.reporting_year.year
        section_databases = Database.objects.filter(section=database.section).order_by('-id')
        from django.db import connection
        cursor = connection.cursor()

        cursor.execute(MASTER_INDICATORS_SUM, [id, id])
        result = cursor.fetchall()
        items_sum = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        cursor.execute(MASTER_INDICATORS_MAXIMUM, [id, id])
        result = cursor.fetchall()
        items_maximum = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        cursor.execute(MASTER_INDICATORS_AVERAGE, [id, id])
        result = cursor.fetchall()
        items_average = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        cursor.execute(MASTER_INDICATORS_COUNT, [id, id])
        result = cursor.fetchall()
        items_count = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        items = items_sum + items_maximum + items_average + items_count

        items = setTrackingStatus(items=items, reporting_year=reporting_year)

        reported = sum(1 for item in items if item.get('value', 0) is not None and item.get('value', 0) > 0)
        percentage = 0

        if len(items) > 0:
            percentage = int(100 * reported / len(items))
        percentage_color = 'badge-danger'
        if percentage > 75:
            percentage_color = 'badge-success'
        elif percentage > 45:
            percentage_color = 'badge-warning'

        return {
            'database': database,
            'section_databases': section_databases,
            'items': items,
            'reported': reported,
            'percentage': percentage,
            'percentage_color': percentage_color
        }


class DatabaseInterventionMapView(LoginRequiredMixin, TemplateView):
    template_name = 'pivoting/database_intervention_map.html'

    def get_context_data(self, **kwargs):
        id = int(self.request.GET.get('id', 0))
        database = Database.objects.get(id=id)
        reporting_year = database.reporting_year.year
        reports = ActivityReportNew.objects.filter(dbase_id=id, funded_by='UNICEF')
        partners = sorted(list(set(reports.values_list('partner_label', flat=True))))
        programs = sorted(list(set(reports.values_list('project_label', flat=True))))
        months = sorted(x[0:7] for x in list(set(reports.values_list('month_name', flat=True))))
        governorates = sorted(list(set(reports.values_list('location_adminlevel_governorate', flat=True))))
        cazas = sorted(list(set(reports.values_list('location_adminlevel_caza', flat=True))))
        
        interventions = PCA.objects.filter(number__in=list(reports.values_list('project_label', flat=True))).distinct()

        return {
            'database': database,
            'current_month': datetime.datetime.now().strftime("%B"),
            'reporting_year': str(reporting_year),
            'interventions': interventions,
            'partners': partners,
            'governorates': governorates,
            'cazas': cazas,
            'months': months,
            'programs': programs,
        }


class DatabaseSnapshotView(LoginRequiredMixin, TemplateView):
    template_name = 'pivoting/database-snapshot.html'

    def get_context_data(self, **kwargs):
        id = int(self.request.GET.get('id', 0))
        database = Database.objects.get(id=id)
        reporting_year = database.reporting_year.year
        reports = ActivityReportNew.objects.filter(dbase_id=id, funded_by='UNICEF')
        partners = sorted(list(set(reports.values_list('partner_label', flat=True))))
        programs = sorted(list(set(reports.values_list('project_label', flat=True))))
        months = sorted(x[0:7] for x in list(set(reports.values_list('month_name', flat=True))))
        governorates = sorted(list(set(reports.values_list('location_adminlevel_governorate', flat=True))))
        cazas = sorted(list(set(reports.values_list('location_adminlevel_caza', flat=True))))
        interventions = PCA.objects.filter(number__in=list(reports.values_list('project_label', flat=True))).distinct()

        # GET MASTER INDICATORS
        from django.db import connection
        cursor = connection.cursor()

        cursor.execute(MASTER_INDICATORS_SUM, [id, id])
        result = cursor.fetchall()
        items_sum = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        cursor.execute(MASTER_INDICATORS_MAXIMUM, [id, id])
        result = cursor.fetchall()
        items_maximum = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        cursor.execute(MASTER_INDICATORS_AVERAGE, [id, id])
        result = cursor.fetchall()
        items_average = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        
        cursor.execute(MASTER_INDICATORS_COUNT, [id, id])
        result = cursor.fetchall()
        items_count = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        items = items_sum + items_maximum + items_average + items_count

        items = setTrackingStatus(items=items, reporting_year=reporting_year)

        reported = sum(1 for item in items if item.get('value', 0) is not None and item.get('value', 0) > 0)
        percentage = int(100 * reported / len(items))
        percentage_color = 'badge-danger'
        if percentage > 75:
            percentage_color = 'badge-success'
        elif percentage > 45:
            percentage_color = 'badge-warning'
        
        return {
            'database': database,
            'current_month': datetime.datetime.now().strftime("%B"),
            'reporting_year': str(reporting_year),
            'interventions': interventions,
            'partners': partners,
            'governorates': governorates,
            'cazas': cazas,
            'months': months,
            'programs': programs,
            'items': items,
            'reported': reported,
            'percentage': percentage,
            'percentage_color': percentage_color,
        }


class HPMNeuroReportView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/hpm.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)
        month = int(self.request.GET.get('month', 0))
        quarter = self.request.GET.get('quarter', "0")

        neuro_report = NeuroReport.objects.get(id=id)
        hpm_files = []
        if neuro_report.is_hpm == True:
            dir_path = str(settings.ROOT_DIR /'static/hpm_files')
            for path in os.listdir(dir_path):
                # check if current path is a file
                if os.path.isfile(os.path.join(dir_path, path)):
                    hpm_files.append(path)
        hpm_files.sort(reverse=True)
        today = datetime.date.today()

        current_month = today.month
        current_day = int(today.strftime("%d"))
        current_year = today.year

        if current_day >= 15:
            current_month = current_month - 1
        else:
            current_month = current_month - 2

        if current_month <= 0:
            current_month = current_month + 12
            current_year = current_year - 1

        if month == 0:
            month = current_month

        months = []
        for i in range(1, current_month + 1):
            months.append((i, datetime.date(2008, i, 1).strftime('%B')))

        is_current_year = True
        title = ""
        table_title = ""

        instance = ReportingYear.objects.get(current=True)
        reporting_year = self.request.GET.get('rep_year', instance.year)

        if quarter == '1' or quarter == '2' or quarter == '3' or quarter == '4':
            selected_month_name = 'Quarter ' + quarter
            table_title = ""
        else:
            selected_month_name = calendar.month_name[month]

        if quarter == '1':
            month = 3
        elif quarter == '2':
            month = 6
        elif quarter == '3':
            month = 9
        elif quarter == '4':
            month = 12

        month_name = calendar.month_name[month]

        dbids = list(set(NeuroReport.objects.filter(id=id).values_list(
            'neuroreportmasterindicator__master__database', flat=True)))
        databases = Database.objects.filter(id__in=dbids)

        if month == 1 and quarter == "":
            title = '{} | Data of January | {}'.format(neuro_report.name, str(reporting_year))
            table_title = '{} {} {}'.format('SUMMARY OF LEBANON RESPONSE PLAN | January | ',
                                            str(reporting_year), 'SITREP-LEBANON')
        else:
            title = '{} | Data of January to {} | {}'.format(neuro_report.name, str(month_name), str(reporting_year))
            table_title = '{} {} {} {} {}'.format(
                'SUMMARY OF LEBANON RESPONSE PLAN | January to', month_name, '|', reporting_year, 'SITREP-LEBANON')

        from django.db import connection
        last_edited_time = datetime.date.today()
        comments = NeuroReportComment.objects.filter(report=id, related_month__lte=str(month).zfill(2))
        hpm_query = NEUROREPORT
        hpm_query = hpm_query.replace('[REPORT_ID]', id)
        hpm_query = hpm_query.replace('[MONTH]', str(month))
        hpm_query = hpm_query.replace('[LAST_EDITED_TIME]', last_edited_time.strftime("%Y-%m-%d"))     
        cursor = connection.cursor()
        cursor.execute(hpm_query, [])
        result = cursor.fetchall()
        items = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        for item in items:
            item['change'] = 0
            if item['value'] and item['value'] > 0:
                item['males_percentage'] = int(item['males'] * 100 / item['value']) if item['males'] and item['males'] >= 0 else 0
                item['females_percentage'] = int(item['females'] * 100 / item['value']) if item['females'] and item['females'] >= 0 else 0

        month_change = 0
        if int(quarter) > 0:
            month_change = month - 3
        else:
            month_change = month - 1

        last_edited_time = datetime.date.today()

        hpm_query = NEUROREPORT
        hpm_query = hpm_query.replace('[REPORT_ID]', id)
        hpm_query = hpm_query.replace('[MONTH]', str(month_change))
        hpm_query = hpm_query.replace('[LAST_EDITED_TIME]', last_edited_time.strftime("%Y-%m-%d"))

        cursor = connection.cursor()
        cursor.execute(hpm_query, [])
        result = cursor.fetchall()
        items2 = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        for item in items:
            for item2 in items2:
                if item['master_id'] == item2['master_id']:
                    item['change'] = item['value'] - item2['value'] if item['value'] and item2['value'] else 0

        return {
            'neuro_report': neuro_report,
            'items': items,
            'dbs': databases,
            'month_name': month_name,
            'month': month,
            'months': months,
            'reporting_year': reporting_year,
            'is_current_year': is_current_year,
            'title': title,
            'table_title': table_title,
            'selected_month': selected_month_name,
            'current_month': current_month,
            'quarter': quarter,
            'comments': comments,
            'hpm_files': hpm_files
        }


class NeuroReportDashboardView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/neuroreport-dashboard.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)

        neuro_report = NeuroReport.objects.get(id=id)
        reporting_year = neuro_report.ryear.year
        today = datetime.date.today()

        current_month = today.month
        current_day = int(today.strftime("%d"))
        current_year = today.year

        if current_day >= 15:
            current_month = current_month - 1
        else:
            current_month = current_month - 2

        if current_month <= 0:
            current_month = current_month + 12
            current_year = current_year - 1

        month = current_month

        from django.db import connection

        last_edited_time = datetime.date.today()

        report_query = NEUROREPORT
        report_query = report_query.replace('[REPORT_ID]', id)
        report_query = report_query.replace('[MONTH]', str(month))
        report_query = report_query.replace('[LAST_EDITED_TIME]', last_edited_time.strftime("%Y-%m-%d"))

        cursor = connection.cursor()
        cursor.execute(report_query, [])
        result = cursor.fetchall()
        items = [dict(zip([key[0] for key in cursor.description], row)) for row in result]

        all_definitions = NeuroReport.objects.filter(report_code=neuro_report.report_code).order_by('-id')
        last_monthly_update_date = Database.objects.filter(reporting_year=neuro_report.ryear).first().last_monthly_update_date

        items = setTrackingStatus(items=items, reporting_year=reporting_year)
        return {
            'neuro_report': neuro_report,
            'all_definitions': all_definitions,
            'last_monthly_update_date': last_monthly_update_date,
            'items': items,
        }


class NeuroReportAnalyticalView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/neuroreport-analytical.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)
        neuro_report = NeuroReport.objects.get(id=id)
        all_definitions = NeuroReport.objects.filter(report_code=neuro_report.report_code).order_by('-id')
        last_monthly_update_date = Database.objects.filter(reporting_year=neuro_report.ryear).first().last_monthly_update_date
        return {
            'neuro_report': neuro_report,
            'last_monthly_update_date': last_monthly_update_date,
            'all_definitions': all_definitions,
        }


class ExportViewSet(ListView):

    def get(self, request, *args, **kwargs):
        id = self.request.GET.get('id', 0)
        instance = Database.objects.get(id=id)

        today = datetime.date.today()
        first = today.replace(day=1)
        last_month = first - datetime.timedelta(days=1)
        month_name = last_month.strftime("%B")
        path = os.path.dirname(os.path.abspath(__file__))

        path2file = path + '/AIReports/' + str(instance.ai_id) + '_ai_data.xlsx'
        filename = '{}_{}_{}_Raw Data.xlsx'.format(instance.label, month_name, instance.reporting_year.name)

        if path2file.split('.')[-1] == 'xlsx':
            with open(path2file, mode='rb', ) as f:
                response = HttpResponse(
                    f.read(),
                    headers={
                        'Content-Type': 'application/vnd.ms-excel',
                        'Content-Disposition': 'attachment; filename="{}"'.format(filename),
                    }
                )
                from os.path import getsize
                response['Content-Length'] = getsize(path2file)
        else:
            with open(path2file, mode='r', ) as f:
                response = HttpResponse(f.read(), content_type='text/csv')
                response['Content-Disposition'] = 'attachment; filename=%s;' % filename
                
        return response


class PCADashboardView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/pca-dashboard.html'

    def get_context_data(self, **kwargs):

        pca = PCA.objects.exclude(status__in=['draft',]).select_related('partner').annotate(partner_short_name=F('partner__short_name')).annotate(partnerid=F('partner__id'))
        # get filters initial values
        sections = sorted(set([item for sublist in pca.values_list('section_names', flat=True).distinct() for item in sublist]))
        offices = sorted(set([item for sublist in pca.filter(offices_set__isnull=False).values_list('offices_set', flat=True).distinct() for item in sublist]))
        partners = sorted(set(pca.exclude(partner__short_name__isnull=True).values_list('partner__short_name', flat=True)))
        ptypes = sorted(set(pca.exclude(partner__partner_type__isnull=True).values_list('partner__partner_type', flat=True)))
        csotypes = sorted(set(pca.exclude(partner__cso_type__isnull=True).values_list('partner__cso_type', flat=True)))
        statuses = sorted(set(pca.values_list('status', flat=True)))
        donors = sorted(set([item for sublist in pca.filter(donors__isnull=False).values_list('donors', flat=True).distinct() for item in sublist]))
        grants = sorted(set([item for sublist in pca.filter(grants__isnull=False).values_list('grants', flat=True).distinct() for item in sublist]))

        # get filters selected values
        selected_grants = self.request.GET.getlist('grants')
        selected_donors = self.request.GET.getlist('donors')
        selected_partners = self.request.GET.getlist('partners')
        selected_sections = self.request.GET.getlist('sections')
        selected_statuses = self.request.GET.getlist('statuses')
        selected_offices = self.request.GET.getlist('offices')
        selected_years = self.request.GET.get('years')
        selected_ptypes = self.request.GET.getlist('ptypes')
        selected_csotypes = self.request.GET.getlist('csotypes')

        # pca = PCA.objects.filter( status='active')

        if selected_partners and not (len(selected_partners) == 1 and selected_partners[0] == ''):
            pca = pca.filter(partner__short_name__in=selected_partners)

        if selected_ptypes and not (len(selected_ptypes) == 1 and selected_ptypes[0] == ''):
            pca = pca.filter(partner__partner_type__in=selected_ptypes)

        if selected_csotypes and not (len(selected_csotypes) == 1 and selected_csotypes[0] == ''):
            pca = pca.filter(partner__cso_type__in=selected_csotypes)

        if selected_sections:
            pca = pca.exclude(section_names=[]).filter(section_names__contained_by=selected_sections)

        if selected_offices:
            pca = pca.exclude(offices_set=[]).filter(offices_set__contained_by=selected_offices)
        
        if selected_donors:
            # pca = pca.filter(donors__contained_by=selected_donors)
            query = Q()
            for value in selected_donors:
                # query &= Q((donors__contains=[value])
                query |= Q(donors__contains=[value])
            pca = pca.filter(query)
        
        if selected_grants:
            # pca = pca.filter(donors__contained_by=selected_donors)
            query = Q()
            for value in selected_grants:
                query |= Q(grants__contains=[value])
            pca = pca.filter(query)

        if selected_statuses:
            pca = pca.filter(status__in=selected_statuses)

        if selected_years:
            start_year = int(selected_years.split(',')[0])
            end_year = int(selected_years.split(',')[1])
            pca = pca.filter(start__year__gte=start_year)
            pca = pca.filter(start__year__lte=end_year)

        # pca = pca.aggregate(donations=Sum('donors_set__value'))
        pca = pca.values()
        ending_active_pds = []
        for item in pca:
            item['donations'] = sum(float(x['value']) for x in item['donors_set'])
            if item['status'] == 'active':
                try:
                    item['days_to_end'] = (item['end'] - datetime.date.today()).days
                    if item['days_to_end'] <= 30 and item['days_to_end'] >= 0:
                        ending_active_pds.append(item['number'])
                except:
                    item['days_to_end'] = 0
            else:
                item['days_to_end'] = 'Ended'
         
        return {
            'donors': donors,
            'grants': grants,
            'partners': partners,
            'offices': offices,
            'sections': sections,
            'statuses': statuses,
            'ptypes': ptypes,
            'csotypes': csotypes,
            'last_update': pca[0]['updated_at'] if len(pca) > 0 else '',

            'selected_partners': selected_partners,
            'selected_sections': selected_sections,
            'selected_offices': selected_offices,
            'selected_donors': selected_donors,
            'selected_grants': selected_grants,
            'selected_statuses': selected_statuses,
            'selected_ptypes': selected_ptypes,
            'selected_csotypes': selected_csotypes,
            'start_year': datetime.date.today().year - 9,
            'current_year': datetime.date.today().year,
            'items': pca,
        }


class PCASummaryActiveView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/pca-summary-active.html'

    def get_context_data(self, **kwargs):
        from django.db import connection
        cursor = connection.cursor()
        cursor.execute(PCA_SUMMARY_PARTNERS.replace('STATUSR', "'active'"), [])
        result = cursor.fetchall()
        summary_partners = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        cursor.execute(PCA_SUMMARY_SECTIONS.replace('STATUSR', "'active'"), [])
        result = cursor.fetchall()
        summary_sections = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        cursor.execute(PCA_ENDING_SOON, [])
        result = cursor.fetchall()
        ending_soon = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        pca = PCA.objects.all().order_by('-updated_at').first()
        return {
            'last_update': pca.updated_at,
            'start_year': datetime.date.today().year - 9,
            'current_year': datetime.date.today().year,
            'summary_partners': summary_partners,
            'summary_sections': summary_sections,
            'ending_soon': ending_soon,
        }


class PCASummaryAllView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/pca-summary-all.html'

    def get_context_data(self, **kwargs):
        from django.db import connection
        cursor = connection.cursor()
        cursor.execute(PCA_SUMMARY_PARTNERS.replace('STATUSR', "'active','closed','ended'"), [])
        result = cursor.fetchall()
        summary_partners = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        cursor.execute(PCA_SUMMARY_SECTIONS.replace('STATUSR', "'active','closed','ended'"), [])
        result = cursor.fetchall()
        summary_sections = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        cursor.execute(PCA_ENDING_SOON, [])
        result = cursor.fetchall()
        ending_soon = [dict(zip([key[0] for key in cursor.description], row)) for row in result]
        pca = PCA.objects.all().order_by('-updated_at').first()
        return {
            'last_update': pca.updated_at,
            'start_year': datetime.date.today().year - 9,
            'current_year': datetime.date.today().year,
            'summary_partners': summary_partners,
            'summary_sections': summary_sections,
            'ending_soon': ending_soon,
        }


class PCAAnalyticalView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/pca-analytical.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)
        pca = PCA.objects.get(id=id)
        return {
            'pca': pca,
        }


class DonorDashboardView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/donor-dashboard.html'

    def get_context_data(self, **kwargs):

        pca = PCA.objects.exclude(status__in=['draft',])
        sections = sorted(set([item for sublist in pca.values_list('section_names', flat=True).distinct() for item in sublist]))
        grants = sorted(set([item for sublist in pca.values_list('grants', flat=True).distinct() for item in sublist]))
        offices = sorted(set([item for sublist in pca.filter(offices_set__isnull=False).values_list('offices_set', flat=True).distinct() for item in sublist]))
        partners = sorted(set(pca.exclude(Q(partner__short_name__isnull=True) | Q(partner__short_name="")).values_list('partner__short_name', flat=True)))
        csotypes = sorted(set(pca.exclude(partner__cso_type__isnull=True).values_list('partner__cso_type', flat=True)))
        statuses = sorted(set(pca.values_list('status', flat=True)))
        pca_numbers = sorted(set(pca.values_list('number', flat=True)))
        donors = sorted(set([item for sublist in pca.filter(donors__isnull=False).values_list('donors', flat=True).distinct() for item in sublist]))
        years = [str(year) for year in range(datetime.date.today().year - 9, datetime.date.today().year + 1)]

        # get the last two reporing years to allow the user to swichch between there reported indicators. Older years are not required
        reporting_years = list(ReportingYear.objects.all().order_by('-id').values())[0:3]

        if pca.count() > 0:
            # last_update = pca.first().updated_at
            last_update = pca.order_by('-updated_at').first().updated_at
        else:
            last_update = None

        return {
            'donors': donors,
            'pca_numbers': pca_numbers,
            'partners': partners,
            'csotypes': csotypes,
            'offices': offices,
            'grants': grants,
            'sections': sections,
            'statuses': statuses,
            'last_update': last_update,
            'years': years,
            'start_year': datetime.date.today().year - 9,
            'current_year': datetime.date.today().year,
            'reporting_years': reporting_years
        }


class PartnershipsView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/partnerships.html'

    def get_context_data(self, **kwargs):
        selected_ptypes = self.request.GET.getlist('ptypes')
        selected_csotypes = self.request.GET.getlist('csotypes')

        items = PartnerOrganization.objects.exclude(Q(deleted_flag=True) | Q(hidden=True) | Q(short_name='') | Q(name=''))
        items = items.annotate(programs=Count('interventions')).annotate(aprograms=Count('interventions', filter=Q(interventions__status='active')))

        ptypes = sorted(set(items.values_list('partner_type', flat=True)))
        csotypes = sorted(set(items.exclude(cso_type__isnull=True).values_list('cso_type', flat=True)))

        if selected_ptypes and not (len(selected_ptypes) == 1 and selected_ptypes[0] == ''):
            items = items.filter(partner_type__in=selected_ptypes)
        if selected_csotypes and not (len(selected_csotypes) == 1 and selected_csotypes[0] == ''):
            items = items.filter(cso_type__in=selected_csotypes)

        return {
            'items': items,
            'ptypes': ptypes,
            'csotypes': csotypes,
            'selected_ptypes': selected_ptypes,
            'last_update': PCA.objects.order_by('-updated_at').first().updated_at,
        }


class PartnershipProfileView(LoginRequiredMixin, TemplateView):

    template_name = 'pivoting/partnership-profile.html'

    def get_context_data(self, **kwargs):
        id = self.request.GET.get('id', 0)
        partnership = PartnerOrganization.objects.get(id=id)
        ma_count = partnership.engagement_set.filter(engagement_type='ma').count()
        sc_count = partnership.engagement_set.filter(engagement_type='sc').count()
        audit_count = partnership.engagement_set.filter(engagement_type='audit').count()
        sa_count = partnership.engagement_set.filter(engagement_type='sa').count()

        return {
            'partnership': partnership,
            'ma_count': ma_count,
            'sc_count': sc_count,
            'audit_count': audit_count,
            'sa_count': sa_count,
        }


class HomeView(TemplateView):
    template_name = 'pivoting/home.html'

    def get_context_data(self, **kwargs):
        instance = ReportingYear.objects.get(current=True)
        reporting_year = self.request.GET.get('rep_year', instance.year)

        image1 = str(random.randint(1, 67)).zfill(2)
        image2 = str(random.randint(1, 67)).zfill(2)
        image3 = str(random.randint(1, 67)).zfill(2)
        return {
            'reporting_year': reporting_year,
            'image1': image1,
            'image2': image2,
            'image3': image3,
        }

def round_decimals_in_dict(data):
    for key, value in data.items():
        if isinstance(value, float):
            data[key] = round(value)
        elif isinstance(value, dict):
            round_decimals_in_dict(value)
        elif isinstance(value, list):
            data[key] = [round(x) if isinstance(x, float) else x for x in value]
    return data


class PopulationFiguresView(LoginRequiredMixin, TemplateView):
    template_name = 'pivoting/population_figures.html'

    def get_context_data(self, **kwargs):
        data = {
            'total_population': 0,
            'leb_all': 0,
            'prl_all': 0,
            'prs_all': 0,
            'syr_all': 0,
            'mig_all': 0,
            'leb_all_perc': 0,
            'prl_all_perc': 0,
            'prs_all_perc': 0,
            'syr_all_perc': 0,
            'mig_all_perc': 0,
            'total_children': 0,
            'leb_children': 0,
            'prl_children': 0,
            'prs_children': 0,
            'pal_children': 0,
            'syr_children': 0,
            'leb_children_perc': 0,
            'prl_children_perc': 0,
            'prs_children_perc': 0,
            'syr_children_perc': 0,
            'source_leb': [],
            'source_syr': [],
            'source_pal': [],
            'leb_by_age': [],
            'mv_leb_all': 0,
            'mv_leb_children': 0,
            'mv_leb_poor': 0,
            'mv_leb_extpoor': 0,
            'mv_syr_all': 0,
            'mv_syr_children': 0,
            'mv_syr_poor': 0,
            'mv_pal_all': 0,
            'mv_pal_children': 0,
            'mv_pal_poor': 0,
            'mv_all': 0,
            'mv_children': 0,
            'mv_poor': 0,

        }
        my_json_obj = None

        file_path = os.path.join(settings.ROOT_DIR, 'pivoting/uploads/Population_figures_2026_NeuroDB.json')

        with open(file_path, 'r') as f:
            my_json_obj = json.load(f)

        for rec in my_json_obj['ALL_BY_GOVERNORATE']:
            data['total_population'] += (rec['TOTAL POPULATION'])
            data['total_children'] += ((rec['0 - 4']) + (rec['5 - 9']) + (rec['10 - 14']) + (rec['15 - 19']))
            data['leb_all'] += (rec['TOTAL LEBANESE'])
            data['prl_all'] += (rec['Palestinian Refugees in Lebanon (PRL)'])
            data['prs_all'] += (rec['Palestinian Refugees from Syrian (PRS)'])
            data['syr_all'] += (rec['TOTAL SYRIANS'])
            data['mig_all'] += (rec['TOTAL MIGRANTS'])

        data['leb_all_perc'] = Decimal(100 * data['leb_all'] / data['total_population'])
        data['prl_all_perc'] = Decimal(100 * data['prl_all'] / data['total_population'])
        data['prs_all_perc'] = Decimal(100 * data['prs_all'] / data['total_population'])
        data['syr_all_perc'] = Decimal(100 * data['syr_all'] / data['total_population'])
        data['mig_all_perc'] = Decimal(100 * data['mig_all'] / data['total_population'])

        for rec in my_json_obj['LEB_BY_GOVERNORATE']:
            data['leb_children'] += ((rec['0 - 4']) + (rec['5 - 9']) + (rec['10 - 14']) + (rec['15 - 19']))
        for rec in my_json_obj['SYR_BY_GOVERNORATE']:
            data['syr_children'] += ((rec['0 - 4']) + (rec['5 - 9']) + (rec['10 - 14']) + (rec['15 - 19']))
        for rec in my_json_obj['PAL_BY_GOVERNORATE']:
            data['pal_children'] += ((rec['0 - 4']) + (rec['5 - 9']) + (rec['10 - 14']) + (rec['15 - 19']))

        # for rec in my_json_obj['LEB_BY_GOVERNORATE']:
        #     data['leb_children'] += int((rec['0 - 4'] + rec['5 - 9']  + rec['10 - 14'] + rec['15 - 19']))

        data['leb_children_perc'] = Decimal(100 * data['leb_children'] / data['total_children'])
        # data['prl_children_perc'] = Decimal(100*data['total_prl']/data['total_population'])
        # data['prs_children_perc'] = Decimal(100*['total_prs']/data['total_population'])
        data['pal_children_perc'] = Decimal(100 * data['pal_children'] / data['total_children'])
        data['syr_children_perc'] = Decimal(100 * data['syr_children'] / data['total_children'])

        for source in my_json_obj['LEB_SOURCES']:
            key = list(source.keys())[0]
            data['source_leb'].append(source[key])

        for source in my_json_obj['PAL_SOURCES']:
            key = list(source.keys())[0]
            data['source_pal'].append(source[key])

        for source in my_json_obj['SYR_SOURCES']:
            key = list(source.keys())[0]
            data['source_syr'].append(source[key])


        def add_entries(entries, column):
            value = 0
            for entry in entries:
                value += (entry[column])
            return value

        # Children Chart data

        def add_columns(entry, columns):
            value = 0
            for column in columns:
                value += (entry[column])
            return value
        # children_age_groups = ['0 - 4', '5 - 9', '10 - 14', '15 - 19']

        return {
            'data': round_decimals_in_dict(data),
        }


class ResourcesView(LoginRequiredMixin, TemplateView):
    template_name = 'pivoting/resources.html'

    def get_context_data(self, **kwargs):

        items = Resource.objects.filter(published=True)

        publication_years = sorted(set(items.values_list('publication_year', flat=True)))
        types = ResourceType.objects.all()
        topics = ResourceTopic.objects.all()
        tags = ResourceTag.objects.all()
        sections = sorted(set(items.values_list('section', flat=True)))

        selected_publication_years = self.request.GET.getlist('publication_years')
        selected_types = self.request.GET.getlist('types')
        selected_topics = self.request.GET.getlist('topics')
        selected_sections = self.request.GET.getlist('sections')
        selected_tags = self.request.GET.getlist('tags')
        page_number = self.request.GET.get('page', 0)
        search_query = self.request.GET.get('search_query', "")

        if selected_publication_years:
            items = items.filter(publication_year__in=selected_publication_years)

        if selected_types:
            items = items.filter(type__in=selected_types)

        if selected_topics:
            items = items.filter(topic__in=selected_topics)

        if selected_sections:
            items = items.filter(section__in=selected_sections)

        if selected_tags:
            items = items.filter(tags__in=selected_tags)

        if search_query and search_query != "":
            items = items.filter(
                Q(title__icontains=search_query) |
                Q(description__icontains=search_query) |
                Q(resource_link__icontains=search_query) |
                Q(resource_file_name__icontains=search_query) |
                Q(resource_image_name__icontains=search_query)
            )

        paginator = Paginator(items, per_page=12)  # Specify the number of items per page

        try:
            page_obj = paginator.page(page_number)
        except EmptyPage:
            # If the page is out of range, return an empty page
            page_obj = paginator.page(1)

        return {
            'items': items,
            'page_obj': page_obj,
            'page_number': page_number,
            'publication_years': publication_years,
            'types': types,
            'topics': topics,
            'sections': sections,
            'tags': tags,
            'selected_publication_years': selected_publication_years,
            'selected_types': selected_types,
            'selected_topics': selected_topics,
            'selected_sections': selected_sections,
            'selected_tags': selected_tags,
            'search_query': search_query,
        }


def resource_file(request, pk):
    # this url is for download
    try:
        obj = Resource.objects.get(pk=pk)
    except Resource.DoesNotExist as exc:
        return JsonResponse({'status_message': 'No Resource Found'})

    get_binary = obj.resource_file

    if get_binary is None:
        return JsonResponse({'status_message': 'Resource does not contian image'})

    if isinstance(get_binary, memoryview):
        binary_io = io.BytesIO(get_binary.tobytes())
    else:
        binary_io = io.BytesIO(get_binary)

    response = FileResponse(binary_io)
    response['Content-Type'] = 'application/x-binary'
    response['Content-Disposition'] = 'attachment; filename="{}"'.format(obj.resource_file_name) # You can set custom filename, which will be visible for clients.
    return response


def ActivityInfoSummaryDownload(request):
    # Perform your raw SQL query to fetch data from the database
    with connection.cursor() as cursor:
        cursor.execute(ACTIVITYINFO_SUMMARY)
        results = cursor.fetchall()
    
    # Create the HTTP response with the appropriate content type
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=ActivityInfo_Intervention_Locations_All_Programs.csv'

    # Create a CSV writer
    writer = csv.writer(response)

    # Write headers
    headers = [desc[0] for desc in cursor.description]
    writer.writerow(headers)

    # Write data rows
    for row in results:
        writer.writerow(row)

    return response


def ActivityInfoPCASummaryDownload(request):
    # Perform your raw SQL query to fetch data from the database
    with connection.cursor() as cursor:
        cursor.execute(ACTIVITYINFO_PCA_SUMMARY)
        results = cursor.fetchall()
    
    # Create the HTTP response with the appropriate content type
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=ActivityInfo_Intervention_Locations_All_Programs.csv'

    # Create a CSV writer
    writer = csv.writer(response)

    # Write headers
    headers = [desc[0] for desc in cursor.description]
    writer.writerow(headers)

    # Write data rows
    for row in results:
        writer.writerow(row)

    return response


def EtoolsSummaryExcel(request):
    # Perform your raw SQL query to fetch data from the database
    with connection.cursor() as cursor:
        cursor.execute(ETOOLS_LOCATIONS)
        results = cursor.fetchall()

    # Create a new Excel workbook and add a worksheet
    wb = Workbook()
    ws = wb.active

    # Write headers to the worksheet
    headers = [desc[0] for desc in cursor.description]
    for col_num, header in enumerate(headers, 1):
        ws.cell(row=1, column=col_num, value=header)

    # Write data to the worksheet
    for row_num, row_data in enumerate(results, 2):
        for col_num, value in enumerate(row_data, 1):
            ws.cell(row=row_num, column=col_num, value=value)

    # Create a response with the Excel file
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=Etools_Planned_Locations_All_Programs.xlsx'
    wb.save(response)

    return response


class MapsView(LoginRequiredMixin, TemplateView): 
    template_name = 'pivoting/maps.html'

    def get_context_data(self, **kwargs):
        items = Map.objects.filter(status="Completed")
        return {
            'maps': items,
        }

def get_wrong_pds(request):
    # Calculate the start of the previous year
    current_year = datetime.datetime.now().year
    start_of_previous_year = f"{current_year - 1}-01-01"

    # Raw SQL query with dynamic date
    raw_query = f"""
        SELECT DISTINCT project_label 
        FROM pivoting_activityreportnew 
        WHERE last_edited_time > '{start_of_previous_year}' 
          AND project_label != '' 
          AND project_label NOT IN (
              SELECT 
                  regexp_replace(number, '-[^-]*$', '') AS trimmed_value
              FROM etools_pca
          );
    """
    
    with connection.cursor() as cursor:
        cursor.execute(raw_query)
        # Fetch all rows
        rows = cursor.fetchall()
        # Convert to a simple list of project labels
        result = [row[0] for row in rows]
    
    # Return the result as a plain text response
    return HttpResponse("\n".join(result), content_type="text/plain")

