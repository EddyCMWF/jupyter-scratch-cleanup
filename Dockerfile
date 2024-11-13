# FROM condaforge/miniforge3

# WORKDIR /src

# COPY cleanup.py cleanup.py

# # CMD ["python3", "cleanup.py", ".", "--test-run"]


# SHELL ["/bin/bash", "-c"]

# Start from a base image with Python
FROM python:3.13-slim

# Set the working directory in the container
WORKDIR /app

# Copy your Python script into the container
COPY cleanup.py /app/cleanup.py

# Install any dependencies your script might have (e.g., requests, pandas, etc.)
# RUN pip install -r requirements.txt

# Command to run when the container starts
ENTRYPOINT ["python", "/app/cleanup.py"]
