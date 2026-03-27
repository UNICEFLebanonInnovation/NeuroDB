from pivoting.models import Cadasters, CadasterLocation, GovernorateLocation, DistrictLocation
import csv
import sys
from django.core.management.base import BaseCommand

maxInt = sys.maxsize

while True:
    # decrease the maxInt value by factor 10 
    # as long as the OverflowError occurs.

    try:
        csv.field_size_limit(maxInt)
        break
    except OverflowError:
        maxInt = int(maxInt/10)

class Command(BaseCommand):

    def handle(self, *args, **options):

        with open('pivoting/governorates.csv') as file:
            reader = csv.reader(file)
            next(reader)  # Advance past the header
            GovernorateLocation.objects.all().delete()
            for row in reader:
                wkt_points = row[7].replace('MULTIPOLYGON Z ','').replace('POLYGON Z ','').replace('(','').replace(')','').split(', ')
                polygon = []
                for point in wkt_points:
                    parts = point.split(' ')
                    polygon.append([float(parts[0]),float(parts[1])])
                
                shortened = []
                shortened.append(polygon[0])
                for i in range(len(polygon)-1):
                    if i%3 == 0:
                        shortened.append(polygon[i])
                shortened.append(polygon[len(polygon)-1])
                ai_id = 0
                if row[3] == 'LBN1':
                    ai_id = 7
                elif row[3] == 'LBN2':
                    ai_id = 6
                elif row[3] == 'LBN3':
                    ai_id = 5
                elif row[3] == 'LBN4':
                    ai_id = 9
                elif row[3] == 'LBN5':
                    ai_id = 4
                elif row[3] == 'LBN6':
                     ai_id = 8
                elif row[3] == 'LBN7':
                    ai_id = 2
                elif row[3] == 'LBN8':
                    ai_id = 3
                else:
                    item.ai_db = 0


                # match row[3]:
                #     case 'LBN1':
                #         ai_id = 7
                #     case 'LBN2':
                #         ai_id = 6
                #     case 'LBN3':
                #         ai_id = 5
                #     case 'LBN4':
                #         ai_id = 9
                #     case 'LBN5':
                #         ai_id = 4
                #     case 'LBN6':
                #         ai_id = 8
                #     case 'LBN7':
                #         ai_id = 2
                #     case 'LBN8':
                #         ai_id = 3
                    # If an exact match is not confirmed, this last case will be used if provided
                    # case _:
                    #     item.ai_db = 0
                item = GovernorateLocation(code=row[3],name=row[1],ai_id=ai_id,polygon_coordinates=polygon)
                item.save()

        with open('pivoting/districts.csv', newline='', encoding='utf-8') as file:
            reader = csv.reader(file)
            next(reader)  # Advance past the header
            DistrictLocation.objects.all().delete()
            for row in reader:
                wkt_points = row[9].replace('MULTIPOLYGON Z ','').replace('POLYGON Z ','').replace('(','').replace(')','').split(', ')
                polygon = []
                for point in wkt_points:
                    parts = point.split(' ')
                    polygon.append([float(parts[0]),float(parts[1])])

                item = DistrictLocation(gov_code=row[6],code=row[3],name=row[7],polygon_coordinates=polygon)
                item.save()


        with open('pivoting/cadasters-layer.csv', newline='', encoding='utf-8') as file:
            reader = csv.reader(file)
            next(reader)  # Advance past the header
            CadasterLocation.objects.all().delete()
            for row in reader:
                wkt_points = row[15].replace('MULTIPOLYGON Z ','').replace('POLYGON Z ','').replace('(','').replace(')','').split(', ')
                polygon = []
                for point in wkt_points:
                    parts = point.split(' ')
                    polygon.append([float(parts[0]),float(parts[1])])

                cadaster = CadasterLocation(gov_code=row[1],dist_code=row[2],code=row[3],name=row[4],polygon_coordinates=polygon)
                cadaster.save()

