import os
import datetime
import logging
import http.client as httplib
import json
from datetime import date
from django.db.models import Sum, Q
from django.apps import apps
from .client import Client
from .exports import get_database_data, read_file, get_xlsx
from .models import Database

logger = logging.getLogger(__name__)


def get_current_extraction_month(ai_db):
    current_year = date.today().year
    current_month = date.today().month
    reporting_year = ai_db.reporting_year.year

    if current_year - 1 == int(reporting_year) and current_month == 1:
        return 12
    else:
        return current_month


def r_script_command_line(ai_db):
    client = Client()
    filters = "LEFT(Month,4) == '{}'".format(ai_db.reporting_year.year)
    if ai_db.parent_id:
        get_database_data(
            client,
            database_id=ai_db.parent_id,
            resource_id=ai_db.db_id,
            database=ai_db,
            record_filter=filters,
        )
        get_xlsx(
            client,
            database_id=ai_db.parent_id,
            resource_id=ai_db.db_id,
            database=ai_db,
            record_filter=filters,
        )
    else:
        get_database_data(
            client, database_id=ai_db.db_id, database=ai_db, record_filter=filters
        )
        get_xlsx(client, database_id=ai_db.db_id, database=ai_db, record_filter=filters)


def read_data_from_file(ai_db, forced=False, report_type=None):
    from pivoting.models import ActivityReportNew

    result = 0

    if forced:
        model = ActivityReportNew.objects.none()
        ActivityReportNew.objects.filter(database_ai_id=ai_db.ai_id).delete()
        if ai_db.have_offices:
            result = add_rows_temp(ai_db=ai_db, model=model)
        else:
            result = add_rows(ai_db=ai_db, model=model)

    return result


def import_data_via_r_script(ai_db, report_type=None):
    r_script_command_line(ai_db)
    total = read_data_from_file(ai_db, True, report_type)
    return total


def get_awp_code(name):
    try:
        awp_code = (
            name.split(" ")[0]
            .split("_")[0]
            .split(":")[0]
            .split("-")[0]
            .split("#")[0]
            .split("%")[0]
        )
        return awp_code
    except TypeError as ex:
        return "None"
    return "None"


def get_label(data):
    try:
        if "------" in data["name"]:
            return data["description"]
    except TypeError as ex:
        pass
    return data["name"]


def set_tags(indicator, tags):
    for tag in tags:
        if tag.name in indicator.name or tag.name.title() in indicator.name:
            setattr(indicator, tag.tag_field, tag)
    indicator.save()


def clean_string(value, string):
    return value.replace(string, "")


def add_rows_temp(ai_db=None, model=None):
    month = get_current_extraction_month(ai_db)
    path = os.path.dirname(os.path.abspath(__file__))
    path2file = path + "/AIReports/" + str(ai_db.ai_id) + "_ai_data.txt"
    values = read_file(path2file)
    ctr = 0

    for row in values:
        indicator_value = 0
        if "Value" in row:
            indicator_value = row["Value"]

        try:
            indicator_value = float(indicator_value)
        except Exception:
            indicator_value = 0

        funded_by = "UNICEF"
        partner_label = "UNICEF"

        start_date = None
        if "month" in row and row["month"] and not row["month"] == "NA":
            start_date = "{}-01".format(row["month"])

        if ai_db.reporting_year.year not in start_date:
            continue

        gov_code = row["reporting_office"]
        gov_name = row["reporting_office"]

        model.create(
            month=month,
            database=row["Folder"],
            database_id=ai_db.ai_id,
            report_id=row["FormId"],
            indicator_id=row["Quantity Field ID"],
            indicator_name=row["Quantity Field"],
            indicator_awp_code="",
            month_name=row["month"] if "month" in row else "",
            partner_label=partner_label,
            location_adminlevel_caza_code=(
                row["caza.code"] if "caza.code" in row else ""
            ),
            location_adminlevel_caza=row["caza.name"] if "caza.name" in row else "",
            form=row["Form"] if "Form" in row else "",
            location_adminlevel_cadastral_area_code=(
                row["cadastral_area.cas_code"]
                if "cadastral_area.cas_code" in row
                else ""
            ),
            location_adminlevel_cadastral_area=(
                row["cadastral_area.name"] if "cadastral_area.name" in row else ""
            ),
            governorate=row["governorate"] if "governorate" in row else "",
            location_adminlevel_governorate_code=gov_code,
            location_adminlevel_governorate=gov_name,
            partner_description="UNICEF",
            project_start_date=(
                row["projects.start_date"]
                if "projects.start_date" in row
                and not row["projects.start_date"] == "NA"
                else None
            ),
            project_end_date=(
                row["projects.end_date"]
                if "projects.end_date" in row and not row["projects.start_date"] == "NA"
                else None
            ),
            project_label=(
                row["projects.project_code"] if "projects.project_code" in row else ""
            ),
            project_description=(
                row["projects.project_name"] if "projects.project_name" in row else ""
            ),
            funded_by=funded_by,
            indicator_value=indicator_value,
            indicator_units=row["units"] if "units" in row else "",
            reporting_section=(
                row["reporting_section"] if "reporting_section" in row else ""
            ),
            site_type=row["site_type"] if "site_type" in row else "",
            location_longitude=(
                row["ai_allsites.geographic_location.longitude"]
                if "ai_allsites.geographic_location.longitude" in row
                else ""
            ),
            location_latitude=(
                row["ai_allsites.geographic_location.latitude"]
                if "ai_allsites.geographic_location.latitude" in row
                else ""
            ),
            location_alternate_name=(
                row["ai_allsites.alternate_name"]
                if "ai_allsites.alternate_name" in row
                else ""
            ),
            location_name=row["ai_allsites.name"] if "ai_allsites.name" in row else "",
            partner_id=row["partner_id"] if "partner_id" in row else partner_label,
            start_date=start_date,
        )
        ctr += 1

    return ctr


def add_rows(ai_db=None, model=None):
    month = get_current_extraction_month(ai_db)
    path = os.path.dirname(os.path.abspath(__file__))
    path2file = path + "/AIReports/" + str(ai_db.ai_id) + "_ai_data.txt"
    database = Database.objects.filter(ai_id=ai_db.ai_id)[0]

    ctr = 0
    added = 0
    failed = 0

    with open(path2file, "r", encoding="utf-8") as f:
        header = f.readline().strip("\n").split("\u001f")
        for line in f:
            # row = row.strip()
            record = line.strip("\n").split("\u001f")
            row = dict(zip(header, record))

            indicator_value = 0
            if "Value" in row:
                indicator_value = row["Value"]

            try:
                indicator_value = float(indicator_value)
            except Exception:
                indicator_value = 0

            if indicator_value == 0:
                continue

            funded_by = (
                row["funded_by.funded_by"] if "funded_by.funded_by" in row else "UNICEF"
            )

            if funded_by.lower() != "unicef":
                if database.is_funded_by_unicef is True:
                    continue

            project_plan = ""
            if "projects.select_plan" in row:
                project_plan = row["projects.select_plan"]
            if project_plan == "" and "select_plan" in row:
                project_plan = row["select_plan"]

            project_label = ""
            if "projects.project_code" in row:
                project_label = row["projects.project_code"]
            if project_label == "" and "project_code" in row:
                project_label = row["project_code"]

            project = ""
            if "projects.please_select_related_project" in row:
                project = row["projects.please_select_related_project"]
            if project == "" and "please_select_related_project" in row:
                project = row["please_select_related_project"]

            partner_label = row["partner.name"] if "partner.name" in row else ""
            partner_label = partner_label.replace("-", "_")

            # partner_label = unicode(partner_label).encode('ascii',errors='ignore').replace('é', 'e').replace('à', 'a').replace('ù', 'u').replace('ô', 'o').replace("\r", " ").replace("\n", " ").replace("\t", '').replace("\"", "")
            # ZS: replace unicode with str for python3

            partner_label = (
                partner_label.replace("é", "e")
                .replace("à", "a")
                .replace("ù", "u")
                .replace("ô", "o")
                .replace("\r", " ")
                .replace("\n", " ")
                .replace("\t", "")
                .replace('"', "")
            )

            if partner_label == "UNICEF":
                funded_by = "UNICEF"

            gov_code = 0
            gov_name = ""
            if "governorate.code" in row:
                if row["governorate.code"] == "NA":
                    gov_code = 10
                else:
                    gov_code = row["governorate.code"]

            if "governorate.name" in row:
                if row["governorate.name"] == "NA":
                    gov_name = "National"
                else:
                    gov_name = row["governorate.name"]

            support_covid1 = False
            support_covid2 = False
            support_covid3 = False

            if "X4.2.3_covid_adaptation" in row:
                if row["X4.2.3_covid_adaptation"] == "Yes":
                    support_covid1 = True

            if "covid_adaptation" in row:
                if row["covid_adaptation"] == "Yes":
                    support_covid2 = True

            if "covid_adapted_sensitization" in row:
                if row["covid_adapted_sensitization"] == "Yes":
                    support_covid3 = True

            support_covid = support_covid1 or support_covid2 or support_covid3

            emergency_tag = ""
            emergency_keywords = {"Cross-Border", "Escalation", "Yes"}
            emergency = "No"

            if "emergency_tag" in row:
                emergency_tag = row["emergency_tag"]
            elif "emergency_reporting" in row:
                emergency_tag = row["emergency_reporting"]
            elif "tag" in row:
                emergency_tag = row["tag"]
            elif "Emergency Tag" in row:
                emergency_tag = row["Emergency Tag"]
            elif "Emergency Reporting" in row:
                emergency_tag = row["Emergency Reporting"]

            if any(keyword in emergency_tag for keyword in emergency_keywords):
                emergency = "Yes"
           
            indicator_name = row["Quantity Field"]
            awp_code = get_awp_code(indicator_name)
            month_name = row.get("month", "")

            # if nutrition thenformat he name differently
            year = int(str(ai_db.ai_id)[:4])  # Extract first 4 characters as an integer
            last_two = str(ai_db.ai_id)[-2:]  # Extract last 2 characters as a string
            if year >= 2025 and last_two == "18":
                # month_name = row.get("month_of_reporting", "")
                indicator_name = ""
                if "indicator_id" in row:
                    awp_code = row["indicator_id"]
                if "indicator_name" in row:
                    indicator_name = row["indicator_name"]
                if "Quantity Field" in row:
                    pass
                    # indicator_name = indicator_name.strip() + " " + row['Quantity Field']

            try:
                model.create(
                    month=month,
                    ai_folder=row["Folder"],
                    database_ai_id=ai_db.ai_id,
                    dbase=ai_db,
                    report_id=row["FormId"],
                    indicator_id=row["Quantity Field ID"],
                    indicator_name=indicator_name,
                    indicator_awp_code=awp_code,
                    month_name=month_name,
                    partner_label=partner_label,
                    # Locacation related fiedls
                    location_adminlevel_caza_code=row.get("caza.code", ""),
                    location_adminlevel_caza=row.get("caza.name", ""),
                    location_adminlevel_cadastral_area_code=row.get(
                        "cadastral_area.cas_code", ""
                    ),
                    location_adminlevel_cadastral_area=row.get(
                        "cadastral_area.name", ""
                    ),
                    location_adminlevel_governorate_code=gov_code,
                    location_adminlevel_governorate=gov_name,
                    location_longitude=row.get(
                        "ai_allsites.geographic_location.longitude", ""
                    ),
                    location_latitude=row.get(
                        "ai_allsites.geographic_location.latitude", ""
                    ),
                    location_alternate_name=row.get("ai_allsites.alternate_name", ""),
                    location_name=row.get("ai_allsites.name", ""),
                    form=row.get("Form", ""),
                    # governorate=row['governorate'] if 'governorate' in row else '',
                    partner_description=row.get("partner.partner_full_name", ""),
                    project_label=project_label[:245],
                    project_description=row.get("projects.project_name", "")[:245],
                    project=project[:245],
                    project_plan=project_plan,
                    funded_by=funded_by,
                    indicator_value=indicator_value,
                    # indicator_units=row['units'] if 'units' in row else '',
                    reporting_section=row.get("reporting_section", ""),
                    # site_type=row['site_type'] if 'site_type' in row else '',
                    partner_id=row.get("partner_id", partner_label),
                    support_covid=support_covid,
                    last_edited_time=(
                        datetime.datetime.strptime(
                            row["Record last edited time"][0:10], "%Y-%m-%d"
                        )
                        if "Record last edited time" in row
                        else None
                    ),
                    parent_form=row.get("ParentForm", ""),
                    emergency=emergency,
                )
                added = added + 1
            except Exception:
                failed = failed + 1

            ctr += 1

    print("end add rows, added: ", str(added), "failed: ", str(failed))

    return ctr


def generate_indicator_awp_code(ai_id):
    from pivoting.models import Indicator

    data = Indicator.objects.filter(activity__database__ai_id=ai_id)
    ctr = data.count()
    for item in data:
        item.awp_code = get_awp_code(item.name)
        item.save()

    return ctr


def set_indicator_tags(ai_id):
    from pivoting.models import IndicatorNew

    data = IndicatorNew.objects.filter(activity__database__ai_id=ai_id)
    ctr = data.count()
    for item in data:
        item.set_tags()

    return ctr


def generate_indicator_tag(ai_id):
    from pivoting.models import Indicator, IndicatorTag

    data = Indicator.objects.filter(activity__database__ai_id=ai_id)
    for item in data:
        item.tag_age = None
        item.tag_nationality = None
        item.tag_disability = None
        item.tag_programme = None
        item.tag_gender = None
        item.save()
    data = Indicator.objects.filter(
        activity__database__ai_id=ai_id,
        master_indicator=False,
        master_indicator_sub=False,
    )
    tags = IndicatorTag.objects.all()

    ctr = data.count()
    for item in data:
        set_tags(item, tags)

    return ctr


def generate_indicator_awp_code2(ai_id):
    from pivoting.models import ActivityReport

    data = ActivityReport.objects.filter(database_id=ai_id)
    ctr = data.count()
    for item in data:
        item.indicator_awp_code = get_awp_code(item.indicator_name)
        item.save()

    return ctr


def calculate_sum_target(ai_id):
    from pivoting.models import Indicator

    top_indicators = Indicator.objects.filter(
        master_indicator=True, activity__database_id=ai_id
    )
    sub_indicators = Indicator.objects.filter(
        master_indicator_sub=True, activity__database_id=ai_id
    )

    for item in sub_indicators:
        if not item.summation_sub_indicators.count():
            continue
        target_sum = item.summation_sub_indicators.exclude(
            master_indicator=True
        ).aggregate(Sum("target"))
        item.target = target_sum["target__sum"] if target_sum["target__sum"] else 0
        item.save()

    for item in top_indicators:
        target_sum = item.summation_sub_indicators.exclude(
            master_indicator_sub=False, master_indicator=False
        ).aggregate(Sum("target"))
        item.target = target_sum["target__sum"] if target_sum["target__sum"] else 0

        item.save()

    return top_indicators.count() + sub_indicators.count()


def generate_indicators_number(ai_db):
    from pivoting.models import Indicator

    for indicator in Indicator.objects.filter(activity__database__ai_id=ai_db.ai_id):
        indicator.ai_indicator = indicator.get_ai_indicator
        indicator.save()


def link_indicators_data(ai_db, report_type=None):
    result = 0
    result = link_indicators_activity_report(ai_db, report_type)
    # link_indicators_project(ai_db)
    # link_etools_partnerships(ai_db)

    return result


#  todo for review
def link_indicators_project(ai_db):
    from pivoting.models import Indicator
    from etools.models import PCA

    indicators = (
        Indicator.objects.filter(
            activity__database__ai_id=ai_db.ai_id, master_indicator=True
        )
        .exclude(project_code__isnull=True)
        .only("project")
    )

    for item in indicators.iterator():
        try:
            project = PCA.objects.get(reference_number=item.project_code)
            item.project = project
            item.save()
        except Exception:
            continue


def link_indicators_activity_report(ai_db, report_type=None):
    from pivoting.models import Indicator, ActivityReport, LiveActivityReport

    ctr = 0
    if report_type == "live":
        reports = LiveActivityReport.objects.filter(database_id=ai_db.ai_id)
    else:
        reports = ActivityReport.objects.filter(database_id=ai_db.ai_id)
    print(reports.count())
    reports = reports.exclude(ai_indicator__isnull=False)
    print(reports.count())

    # if ai_db.is_funded_by_unicef:
    #     reports = reports.filter(funded_by='UNICEF')

    indicators = (
        Indicator.objects.filter(activity__database__ai_id=ai_db.ai_id)
        .exclude(master_indicator=True)
        .exclude(master_indicator_sub=True)
    )

    for item in indicators:
        if not item.ai_indicator:
            continue
        ai_values = reports.filter(indicator_id=item.ai_indicator)

        if not ai_values.count():
            continue
        ctr += ai_values.count()
        ai_values.update(ai_indicator=item.id)

        try:
            item.project_code = ai_values.first().project_label
            item.project_name = ai_values.first().project_description

            item.save()

            main_master_indicator = item.main_master_indicator
            if main_master_indicator:
                main_master_indicator.project_code = item.project_code
                main_master_indicator.project_name = item.project_name
                main_master_indicator.save()
        except Exception:
            pass

    return ctr


def link_ai_locations(report_type=None):
    from pivoting.models import AdminLevelEntities, ActivityReport, LiveActivityReport

    ctr = 0
    if report_type == "live":
        indicators = LiveActivityReport.objects.all()
    else:
        indicators = ActivityReport.objects.all()

    location_cadastral = indicators.values(
        "location_adminlevel_cadastral_area_code"
    ).distinct()
    location_caza = indicators.values("location_adminlevel_caza_code").distinct()
    location_governorate = indicators.values(
        "location_adminlevel_governorate_code"
    ).distinct()

    for item in location_cadastral:
        ai_values = indicators.filter(
            location_adminlevel_cadastral_area_code=item[
                "location_adminlevel_cadastral_area_code"
            ]
        )

        if not ai_values.count():
            continue
        ctr += ai_values.count()
        try:
            ai_values.update(
                location_cadastral=AdminLevelEntities.objects.get(
                    code=item["location_adminlevel_cadastral_area_code"]
                )
            )
        except Exception as ex:
            print(item["location_adminlevel_cadastral_area_code"])
            pass

    for item in location_caza:
        ai_values = indicators.filter(
            location_adminlevel_caza_code=item["location_adminlevel_caza_code"]
        )

        if not ai_values.count():
            continue
        ctr += ai_values.count()
        try:
            ai_values.update(
                location_caza=AdminLevelEntities.objects.get(
                    code=item["location_adminlevel_caza_code"]
                )
            )
        except Exception as ex:
            print(item["location_adminlevel_caza_code"])
            pass

    for item in location_governorate:
        ai_values = indicators.filter(
            location_adminlevel_governorate_code=item[
                "location_adminlevel_governorate_code"
            ]
        )

        if not ai_values.count():
            continue
        ctr += ai_values.count()
        try:
            ai_values.update(
                location_governorate=AdminLevelEntities.objects.get(
                    code=item["location_adminlevel_governorate_code"]
                )
            )
        except Exception as ex:
            print(item["location_adminlevel_governorate_code"])
            pass

    return ctr


#  todo for review
def link_etools_partnerships(ai_db=None):
    from pivoting.models import ActivityReport
    from etools.models import PCA
    from etools.utils import get_interventions_details

    now = datetime.datetime.now()

    years = (now.year, now.year - 1)

    interventions = PCA.objects.filter(
        start__year__in=years,
    ).order_by("-start")

    # locations = PCA.objects.all()

    programmes = json.loads(get_interventions_details(interventions))

    ai_reports = ActivityReport.objects.filter(project_label__isnull=False)
    if ai_db:
        ai_reports = ai_reports.filter(database_id=ai_db.ai_id)

    for programme in programmes:
        reports = ai_reports.filter(project_label=programme["number"])
        print(programme["longitude"])

        if not reports.count():
            continue

        reports.update(programme_document_id=json.dumps(programme))


def assign_support_disability_master_indicator():
    from pivoting.models import Indicator

    # Level 4
    sub_indicators = Indicator.objects.filter(tag_disability__isnull=False)
    sub_indicators.update(support_disability=True)

    # Level 3
    sub_indicators2 = Indicator.objects.filter(
        master_indicator_sub_sub=True, sub_indicators__support_disability=True
    )
    sub_indicators2.update(support_disability=True)

    # Level 2
    sub_indicators3 = Indicator.objects.filter(
        master_indicator_sub=True, sub_indicators__support_disability=True
    )
    sub_indicators3.update(support_disability=True)

    # Level 1
    top_indicators = Indicator.objects.filter(master_indicator=True)

    for indicator in top_indicators.iterator():
        sub_indicators = indicator.sub_indicators.filter(tag_disability__isnull=False)
        sub_indicators.update(support_disability=True)

    # Level 2
    top_indicators1 = Indicator.objects.filter(master_indicator_sub=True).only(
        "sub_indicators",
        "support_disability",
    )

    for indicator in top_indicators1.iterator():
        sub_indicators = indicator.sub_indicators.all()
        sub_indicators.update(support_disability=indicator.support_disability)

    # Level 3
    top_indicators2 = Indicator.objects.filter(master_indicator_sub_sub=True).only(
        "sub_indicators",
        "support_disability",
    )

    for indicator in top_indicators2.iterator():
        sub_indicators = indicator.sub_indicators.all()
        sub_indicators.update(support_disability=indicator.support_disability)


def update_indicator_data(ai_db, ai_field_name, field_name):
    from pivoting.client import ActivityInfoClient
    from pivoting.models import Indicator

    # client = ActivityInfoClient(ai_db.username, ai_db.password)
    client = ActivityInfoClient('API_TOKEN', '838a484ff1f05e7753babf33b6ce420a')

    dbs = client.get_databases()
    db_ids = [db["id"] for db in dbs]
    if ai_db.ai_id not in db_ids:
        raise Exception("DB with ID {} not found in ActivityInfo".format(ai_db.ai_id))

    db_info = client.get_database(ai_db.ai_id)

    objects = 0
    try:
        for activity in db_info["activities"]:
            for indicator in activity["indicators"]:
                try:
                    ai_indicator = Indicator.objects.get(ai_id=indicator["id"])
                except Indicator.DoesNotExist:
                    continue

                objects += 1
                setattr(ai_indicator, ai_field_name, indicator[field_name])
                ai_indicator.save()

    except Exception as e:
        raise e

    return objects


def get_data(url, apifunc, token, protocol="HTTPS"):
    headers = {
        "Content-type": "application/json",
        "Authorization": token,
        "HTTP_REFERER": "etools.unicef.org",
        # "Cookie": "tfUDK97TJSCkB4Nlm2wuMx67XNOYWpKT18BeV3RNoeq6nO7FXemAZypct369yF9I",
        # "X-CSRFToken": 'tfUDK97TJSCkB4Nlm2wuMx67XNOYWpKT18BeV3RNoeq6nO7FXemAZypct369yF9I',
        # "username": "achamseddine@unicef.org", "password": "Alouche21!"
    }

    if protocol == "HTTPS":
        conn = httplib.HTTPSConnection(url)
    else:
        conn = httplib.HTTPConnection(url)

    conn.request("GET", apifunc, None, headers)
    response = conn.getresponse()
    result = response.read()

    if not response.status == 200:
        if response.status == 400 or response.status == 403:
            raise Exception(str(response.status) + response.reason + response.read())
        else:
            raise Exception(str(response.status) + response.reason)

    conn.close()

    return result
