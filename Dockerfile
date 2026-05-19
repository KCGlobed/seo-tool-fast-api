# Use a lightweight, official Python runtime as the base image
FROM python:3.13-slim

# Set environment variables for production reliability
# - PYTHONUNBUFFERED=1 ensures logs are sent to stdout/stderr immediately (essential for Cloud Run logging)
# - PYTHONDONTWRITEBYTECODE=1 prevents Python from writing .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies (if any are needed for SQLite or HTTP operations)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to leverage Docker build cache layers
COPY requirements.txt .

# Install dependencies securely and keep image size minimal
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Create a non-privileged system user for enhanced security in production
# (Cloud Run containers run securely as root by default, but non-root is a best practice)
RUN addgroup --system appgroup && adduser --system --group appuser \
    && chown -R appuser:appgroup /app
USER appuser

# Expose port 8080 (Cloud Run's default port, which is overriden by the $PORT env variable at runtime)
EXPOSE 8080

# Start the FastAPI application using Uvicorn
# We bind to 0.0.0.0 and listen on the dynamic $PORT environment variable injected by Google Cloud Run.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
