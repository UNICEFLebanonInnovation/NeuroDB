FROM nginx

# Create Windows user in the container
RUN useradd neurodbuser
# Set it for subsequent commands
USER neurodbuser

# Copy configuration files to the container
COPY default.conf /etc/nginx/conf.d/default.conf
