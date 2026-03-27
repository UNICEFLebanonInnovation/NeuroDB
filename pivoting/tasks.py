import json
import logging
import threading
from datetime import datetime, date
from pivoting.utils import get_data

logger = logging.getLogger(__name__)


def import_activity_data():
    from pivoting.models import Database
    from .utils import import_data_via_r_script

    # databases = Database.objects.filter(reporting_year__current=True)
    databases = Database.objects.filter(reporting_year__year=datetime.now().year)
    print(databases.count())
    for db in databases:
        logger.info("1. Import report: " + db.name)
        import_data_via_r_script(db)


def import_data_and_generate_monthly_report(database):
    # To do calculation for all databases: python manage.py import_data_and_generate_monthly_report
    # python manage.py import_data_and_generate_monthly_report --database=ID (ai_id field, ex: 202114 for PPL)
    from pivoting.models import Database
    from .utils import import_data_via_r_script

    # databases = Database.objects.filter(reporting_year__year=datetime.now().year)
    databases = Database.objects.filter(reporting_year__current=True)
    if database:
        databases = Database.objects.filter(ai_id=int(database))
    for db in databases:
        db.last_monthly_update_date = datetime.now()
        db.save()
        logger.info("1. Import report: " + db.name)
        import_data_via_r_script(db)


def clone(instance):
    instance.pk = None
    instance.save()
    return instance


def replicate_ai_indicators(db_source, db_destination):
    from pivoting.models import (
        Database,
        Activity,
        IndicatorNew,
        MasterIndicator,
        MasterSubIndicator,
        SubIndicator,
    )

    for activity in Activity.objects.filter(database_id=db_source):
        activity.pk = None
        activity.database_id = db_destination
        activity.save()
        logger.info("Replicated Activity " + str(activity.pk))
        

    for indicator in IndicatorNew.objects.filter(database=db_source):
        indicator.pk = None
        indicator.database_id = db_destination
        if indicator.activity:
            new_activity = Activity.objects.get(
                database_id=db_destination,
                name=indicator.activity.name,
                ai_id=indicator.activity.ai_id,
                ai_form_id=indicator.activity.ai_form_id,
            )
            indicator.activity = new_activity
        indicator.save()
        logger.info("Replicated indicator " + str(indicator.pk))

    for master_indicator in MasterIndicator.objects.filter(database=db_source):
        old_id = master_indicator.id
        old_tags = master_indicator.tags.all()
        new = clone(master_indicator)
        new.database_id = db_destination
        new.old_id = old_id
        new.save()
        new.tags.add(*old_tags)
        logger.info("Replicated master_indicator " + str(new.pk))

    for sub_indicator in SubIndicator.objects.filter(database=db_source):
        old_indicators = sub_indicator.indicators.all()
        old_id = sub_indicator.id
        new = clone(sub_indicator)
        new.database_id = db_destination
        new.old_id = old_id
        
        new_activity = Activity.objects.get(
                database_id=db_destination,
                name=sub_indicator.activity.name,
                ai_id=sub_indicator.activity.ai_id,
                ai_form_id=sub_indicator.activity.ai_form_id,
            )
        new.activity = new_activity
        new.save()
        logger.info("Replicated sub_indicator " + str(new.pk))

        for old_indicator in old_indicators:
            try:
                new_indicator = IndicatorNew.objects.get(ai_indicator=old_indicator.ai_indicator, database_id=db_destination, name=old_indicator.name)
                new.indicators.add(new_indicator)
                logger.info("Replicated sub_indicator indicator " + str(old_indicator.pk))
            except Exception:
                logger.info("Replicated sub_indicator indicator FAILED for" + str(old_indicator.pk))

        new.save()

    for master_sub_indicator in MasterSubIndicator.objects.filter(
        master__database=db_source
    ):
        try:
            master_sub_indicator.master = MasterIndicator.objects.get(
                old_id=master_sub_indicator.master.id
            )
            master_sub_indicator.sub = SubIndicator.objects.get(
                old_id=master_sub_indicator.sub.id
            )
            master_sub_indicator.pk = None
            master_sub_indicator.save()
            logger.info("Replicated master_sub_indicator " + str(master_sub_indicator.pk))
        except Exception:
            logger.info("Replicated master_sub_indicator FAILED for: " + str(master_sub_indicator.pk))


class ReplicationThreading(object):
    def __init__(self, interval=1, db_source=None, db_destination=None):
        self.interval = interval
        self.db_source = db_source
        self.db_destination = db_destination
        thread = threading.Thread(target=self.run, args=())

        thread.daemon = True
        thread.start()

    def run(self):
        replicate_ai_indicators(self.db_source, self.db_destination)


def run_database_replication(db_source, db_destination):
    tr = ReplicationThreading(1, db_source, db_destination)


class ImportThreading(object):
    def __init__(self, interval=1, db=None, report_type=None):
        self.interval = interval
        self.db = db
        self.report_type = report_type
        thread = threading.Thread(target=self.run, args=())

        thread.daemon = True
        thread.start()

    def run(self):
        import_data_and_generate_monthly_report(self.db)


def run_database_import(db, report_type=None):
    tr = ImportThreading(1, db, report_type)


def sync_simple_locations_data():
    from .models import SimpleLocation

    instances = get_data(
        "etools.unicef.org",
        "/api/locations/",
        "Token 36f06547a4b930c6608e503db49f1e45305351c2",
    )
    instances = json.loads(instances)
    count = 0
    for item in instances:
        instance, new_instance = SimpleLocation.objects.get_or_create(
            id=int(item["id"])
        )
        try:
            instance.name = item["name"]
            instance.p_code = item["p_code"]

            if item["geo_point"] != "":
                coordinates = item["geo_point"].split("(")[1][:-1].split(" ")
                instance.longitude = coordinates[0]
                instance.latitude = coordinates[1]

            if len(item["p_code"].split("-")) == 3:
                instance.cas_code = item["p_code"].split("-")[0]

            if len(item["p_code"].split("LB_CAS_")) == 2:
                instance.cas_code = item["p_code"].split("LB_CAS_")[1]

            instance.save()
            count = count + 1
        except Exception as ex:
            print(ex)
            continue
