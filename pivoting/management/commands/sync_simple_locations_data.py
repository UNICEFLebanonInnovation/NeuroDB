__author__ = 'achamseddine'

from django.core.management.base import BaseCommand
from pivoting.tasks import sync_simple_locations_data


class Command(BaseCommand):
    help = 'sync_simple_locations_data'

    def handle(self, *args, **options):
        sync_simple_locations_data()
