FROM postgres:13.10

# Create Windows user in the container
RUN useradd neurodbuser
# Set it for subsequent commands
USER neurodbuser

COPY ./compose/production/postgres/maintenance /usr/local/bin/maintenance
RUN chmod +x /usr/local/bin/maintenance/*
RUN mv /usr/local/bin/maintenance/* /usr/local/bin \
    && rmdir /usr/local/bin/maintenance
