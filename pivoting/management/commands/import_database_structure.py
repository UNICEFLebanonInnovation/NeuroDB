__author__ = 'achamseddine'

from django.core.management.base import BaseCommand
from pivoting.utilities import import_data_v4
from pivoting.models import Database

class Command(BaseCommand):
    help = 'import_database_structure'

    def add_arguments(self, parser):
        parser.add_argument('--database', default=None)

    def handle(self, *args, **options):
        database = Database.objects.filter(ai_id=int(options['database'])).first()
        import_data_v4(database)
