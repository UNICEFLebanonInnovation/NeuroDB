# Installing spatial dependencies
    
apt-get update -qq && apt-get install binutils libproj-dev gdal-bin -yqq
# apt-get update -qq && apt-get install binutils libproj-dev -yqq

# Installing cron
apt-get update -qq && apt-get install cron -yqq
service cron start
mkdir /home/ImportLogs
# (crontab -l 2>/dev/null; echo "*/5 * * * * cp /home/LogFiles/*.log /home/BackupLogs")|crontab

# * * * * *
# | | | | |
# | | | | +---- Day of the Week   (range: 0-6, 0 standing for Sunday)
# | | | +------ Month of the Year (range: 1-12)
# | | +-------- Day of the Month  (range: 1-31)
# | +---------- Hour              (range: 0-23)
# +------------ Minute            (range: 0-59)

#(crontab -l 2>/dev/null; echo "* */2 * * * $APP_PATH/antenv/bin/python $APP_PATH/manage.py import_data_v2")|crontab

# synchronize etools daily at 8:30 PM
(crontab -l 2>/dev/null; echo "30 20 * * * $APP_PATH/antenv/bin/python $APP_PATH/manage.py sync_etools_data")|crontab

# synchronize interventions daily at 6:00 PM
(crontab -l 2>/dev/null; echo "0 18 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,20,21,22 * * $APP_PATH/antenv/bin/python $APP_PATH/manage.py import_data_v2")|crontab

# synchronize locations at 5:00 AM
(crontab -l 2>/dev/null; echo "0 5 * * * $APP_PATH/antenv/bin/python $APP_PATH/manage.py sync_locations_data")|crontab

# start web server
python manage.py collectstatic --no-input
python manage.py makemigrations pivoting, survey
python manage.py migrate pivoting survey
python manage.py migrate survey
gunicorn --workers 2 --threads 4 --timeout 60 --access-logfile \
    '-' --error-logfile '-' --bind=0.0.0.0:8000 \
     --chdir=/home/site/wwwroot azureproject.wsgi

