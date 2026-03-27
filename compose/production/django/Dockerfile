#FROM python:3.11-slim-buster
# FROM python:3.10-slim-buster
FROM python:3.10-slim-bookworm
# Create Windows user in the container
#RUN useradd ali
# Set it for subsequent commands
#USER ali


# SSH support on Azure
# ENV SSH_PASSWD "root:Docker!"
# RUN apt-get update \
#     && apt-get install -y --no-install-recommends dialog \
# 	&& apt-get install -y --no-install-recommends openssh-server \
# 	&& echo "$SSH_PASSWD" | chpasswd

# COPY ./compose/production/django/sshd_config /etc/ssh/

RUN apt-get update -qq && apt-get install binutils libproj-dev -yqq
WORKDIR /code/
COPY ./requirements/production.txt  /code/requirements/production.txt
COPY ./requirements/base.txt  /code/requirements/base.txt
#COPY requirements.txt .
RUN pip install -r /code/requirements/production.txt
RUN pip install honcho

# This is required to django_json_widget just to create empty jsoneditor.map file

RUN python -c "import django_json_widget as _; from pathlib import Path; p=Path(_.__path__[0]) / 'static' / 'dist' / 'jsoneditor.map'; p.touch()"

ADD . /code/

# Call collectstatic (customize the following line with the minimal environment variables needed for manage.py to run):
RUN python manage.py collectstatic --noinput --ignore '*.txt'
# RUN python manage.py collectstatic --noinput --settings=azureproject.test --ignore '*.txt'

# migrate pivoting

# uWSGI will listen on this port
EXPOSE 2222 80

#RUN service ssh start

# Start
COPY ./compose/production/django/entrypoint.sh /code/compose/django/entrypoint.sh
RUN chmod +x /code/compose/django/entrypoint.sh
ENTRYPOINT ["sh", "/code/compose/django/entrypoint.sh"]

COPY ./compose/production/django/gunicorn.sh /code/compose/django/gunicorn.sh
RUN chmod +x /code/compose/django/gunicorn.sh
CMD ["sh", "/code/compose/django/gunicorn.sh"]

