FROM python:3.12-slim

# Ensure logs are streamed to Cloud Logging immediately and prevent .pyc files
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Security: Run as a non-privileged user
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

# Cloud Run sets the PORT environment variable automatically
ENV PORT 8080

# Using uvicorn to run the FastAPI app
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]