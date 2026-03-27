import json
from pivoting.utils import get_data


def sync_location_type_data():
    from locations.models import LocationType
    instances = get_data('etools.unicef.org', '/api/locations-types/', 'Token 36f06547a4b930c6608e503db49f1e45305351c2')
    instances = json.loads(instances)

    for item in instances:

        instance, new_instance = LocationType.objects.get_or_create(id=int(item['id']))
        instance.name = item['name']
        instance.admin_level = item['admin_level']

        instance.save()

def sync_locations_data():
    from locations.models import Location
    instances = get_data('etools.unicef.org', '/api/locations/', 'Token 36f06547a4b930c6608e503db49f1e45305351c2')
    instances = json.loads(instances)
    for item in instances:
        instance, new_instance = Location.objects.get_or_create(id=int(item['id']))
        try:
            instance.name = item['name']
            instance.p_code = item['p_code']

            print(item['gateway']['id'])
            # instance.type_id = int(item['gateway']['id'])
            # instance.parent_id = item['parent']
            instance.point = item['geo_point']

            coordinates = item['geo_point'].split('(')[1][:-1].split(' ')
            instance.longitude = coordinates[0]
            instance.latitude = coordinates[1]
            
            if len(item['p_code'].split('-')) == 3:
                instance.cas_code = item['p_code'].split('-')[0]
            # ZS:29 Mar 2023
            if len(item['p_code'].split('LB_CAS_')) == 2:
                instance.cas_code = item['p_code'].split('LB_CAS_')[1]
                print(instance.cas_code)
            # instance.cas_code = item

            instance.save()
            count = count + 1
        except Exception as ex:
            continue
    
