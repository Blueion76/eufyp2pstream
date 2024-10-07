# Use a base image
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy all files to the container
COPY . .

# Install any necessary dependencies
RUN apt-get update && \
    apt-get install -y jq && \
    pip install --no-cache-dir -r requirements.txt

# Ensure run.sh is executable
RUN chmod +x /app/run.sh
RUN chmod +x /app/*.py
# Set the entry point for the application
ENTRYPOINT ["/app/run.sh"]
