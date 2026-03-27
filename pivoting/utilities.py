from .client import ActivityInfoClient
from .models import IndicatorNew, Activity

"""  Reformatting json structure for each database  """


def import_data_v4(ai_db):
    # client = ActivityInfoClient(ai_db.username, ai_db.password)
    client = ActivityInfoClient('API_TOKEN', '838a484ff1f05e7753babf33b6ce420a')

    # 838a484ff1f05e7753babf33b6ce420a
    # main_db_id = ReportingYear.objects.get(current=True).database_id
    # main_db_id = ai_db.reporting_year.database_id
    # db_info = client.get_databases_v4(main_db_id)
    db_info = client.get_databases_v4(ai_db.parent_id)

    resources = db_info['resources']
    new_data = {}

    for item in resources:
        if item['parentId'] == ai_db.db_id:
            if item['type'] == 'FOLDER':
                if 'Folders' not in new_data:
                    new_data['Folders'] = []
                new_data['Folders'].append(item)
            if item['type'] == 'FORM':
                if 'Forms' not in new_data:
                    new_data['Forms'] = []
                new_data['Forms'].append(item)

    if 'Folders' in new_data:
        for entry in new_data['Folders']:
            for item in resources:
                if item['parentId'] == entry['id'] and item['type'] == 'FORM':
                    if 'Forms' not in entry:
                        entry['Forms'] = []
                    entry['Forms'].append(item)

    if 'Folders' not in new_data:
        if 'Forms' in new_data:
            for form in new_data['Forms']:
                for item in resources:
                    if item['parentId'] == form['id'] and item['type'] == 'SUB_FORM':
                        if 'Sub_Forms' not in form:
                            form['Sub_Forms'] = []
                        form['Sub_Forms'].append(item)

    if 'Folders' in new_data:
        for folder in new_data['Folders']:
            if 'Forms' in folder:
                for form in folder['Forms']:
                    for item in resources:
                        if item['parentId'] == form['id'] and item['type'] == 'SUB_FORM':
                            if 'Sub_Forms' not in form:
                                form['Sub_Forms'] = []
                            form['Sub_Forms'].append(item)
    json_data = new_data

    if 'Folders' in json_data:
        for folder in json_data['Folders']:
            if 'Forms' in folder:
                for form in folder['Forms']:
                    ai_activity, created = Activity.objects.get_or_create(ai_form_id=form['id'],
                                                                          database_id=ai_db.id)
                    ai_activity.name = form['label']
                    ai_activity.label = form['label']
                    # ai_activity.database = ai_db
                    ai_activity.category = folder['label']
                    ai_activity.ai_category_id = form['parentId']
                    ai_activity.save()
                    """ get indicators list for each form"""
                    get_list_indicators_v4(ai_db, form['id'], ai_activity)

    if 'Forms' in json_data:
        for form in json_data['Forms']:
            ai_activity, created = Activity.objects.get_or_create(ai_form_id=form['id'],
                                                                  database_id=ai_db.id)

            ai_activity.name = form['label']
            ai_activity.label = form['label']
            # ai_activity.database = ai_db
            ai_activity.save()
            """ get indicators list for each form"""
            get_list_indicators_v4(ai_db, form['id'], ai_activity)

    return len(new_data)


def write_to_log(text):
    with open('logfile.txt', 'a') as file:
        file.write(text)


def get_list_indicators_v4(ai_db, form_id, ai_activity):
    from .utils import get_awp_code
    # client = ActivityInfoClient(ai_db.username, ai_db.password)
    client = ActivityInfoClient('API_TOKEN', '838a484ff1f05e7753babf33b6ce420a')
    
    form_info = client.get_database_indicators_v4(form_id)
    form_elements = form_info['elements']
    sub_form_ids = list(filter(lambda x: "subform" in x['type'] and "tableVisible" not in x, form_elements))

    for sub_form_id in sub_form_ids:

    # if sub_form_id is not None and len(sub_form_id) > 0:
        indicator_id = sub_form_id['typeParameters']['formId']

        indicator_all_info = client.get_database_indicators_v4(indicator_id)
        indicator_list = indicator_all_info['elements']
        sub_indicators = [x for x in indicator_list if x['type'] == 'quantity' or x['type'] == 'calculated']

        for sub_indicator in sub_indicators:
            
            ai_sub_indicator, created = IndicatorNew.objects.get_or_create(
                ai_indicator=sub_indicator['id'],
                database_id=ai_db.id,
                activity_id=ai_activity.id
            )
            ai_sub_indicator.description = sub_indicator.get('description', '')
            ai_sub_indicator.label = sub_indicator['label']
            ai_sub_indicator.name = sub_indicator['label']
            ai_sub_indicator.type = sub_indicator['type']
            ai_sub_indicator.units = sub_indicator.get("typeParameters", {}).get("units", "")
            ai_sub_indicator.awp_code = get_awp_code(sub_indicator['label'])
            ai_sub_indicator.save()
            ai_sub_indicator.set_tags()
