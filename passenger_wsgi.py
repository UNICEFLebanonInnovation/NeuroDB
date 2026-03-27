import imp
import os
import sys


sys.path.insert(0, os.path.dirname(__file__))

wsgi = imp.load_source('wsgi', 'manage.py')
application = wsgi./home/p3neurodb/my_django_app/neurodb/azureproject/wsgi.py
