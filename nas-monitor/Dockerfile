FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/jowtron/internet-monitor
LABEL org.opencontainers.image.description="NAS Internet Monitor - monitors connectivity and reports to VPS"
LABEL org.opencontainers.image.licenses=MIT

# Install iputils-ping for ping command
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY monitor.py .
COPY config.yaml.example config.yaml

# Create logs directory
RUN mkdir -p /app/logs

# Environment variables (override in docker-compose or docker run)
ENV PING_TARGETS="1.1.1.1,8.8.8.8,9.9.9.9"
ENV PING_INTERVAL=30
ENV HEARTBEAT_INTERVAL=60
ENV VPS_URL="http://vps-monitor:5000"
ENV LOG_DIRECTORY="/app/logs"
ENV NTFY_TOPIC=""
ENV HIGH_LATENCY_THRESHOLD=200
ENV SLOW_SPEED_THRESHOLD=50
ENV HTTP_PORT=8080

VOLUME ["/app/logs"]

EXPOSE 8080

CMD ["python", "-u", "monitor.py"]
