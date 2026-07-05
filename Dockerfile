# Dockerfile for SigenStor Dashboard
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if needed for some libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Create directories that will be used as volumes
RUN mkdir -p data logs

# Expose NiceGUI default port
EXPOSE 8080

# Environment
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["python", "main.py"]