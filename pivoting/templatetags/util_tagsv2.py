from django import template
import logging

register = template.Library()
logger = logging.getLogger(__name__)


@register.simple_tag
def get_databasesv2(is_sector=False):
    from pivoting.models import Database
    try:
        databases = Database.objects.filter(reporting_year__current=True, display=True).exclude(ai_id=10240).order_by('label')
        if is_sector:
            databases = databases.filter(is_sector=True)
        return databases
    except Exception as ex:
        print('get_databases error' + str(ex))
        return []


@register.simple_tag
def get_cholera_db(is_sector=False):
    from pivoting.models import Database
    try:
        cholera_database = Database.objects.filter(reporting_year__current=True, parent_id='c6l11t5laaw8wag2').exclude(ai_id=10240).order_by('label').first()
        return cholera_database
    except Exception as ex:
        print('ge_cholera_db_id error' + str(ex))
        return []


@register.simple_tag
def get_neuroreports(is_sector=False):
    from pivoting.models import NeuroReport
    try:
        # reports = NeuroReport.objects.filter(ryear__current=True, is_hpm=False)
        reports = NeuroReport.objects.filter(is_active=True, is_hpm=False)
        return reports
    except Exception as ex:
        print('get_neuroreports error' + str(ex))
        return []


@register.simple_tag
def get_hpm_neuroreports(is_sector=False):
    from pivoting.models import NeuroReport
    try:
        # reports = NeuroReport.objects.filter(ryear__current=True, is_hpm=True)
        reports = NeuroReport.objects.filter(is_active=True, is_hpm=True)
        return reports
    except Exception as ex:
        print('get_hpm_neuroreports error' + str(ex))
        return []


@register.simple_tag
def get_array_as(value):
    if value is None:
        return ''
    if len(value) == 0:
        return ''
    elif len(value) == 1:
        return value[0]
    else:
        value_as = ''
        for item in value:
            value_as += item + ', '
        return value_as[:-2]


@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)