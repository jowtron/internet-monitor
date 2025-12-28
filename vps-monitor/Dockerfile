FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/jowtron/internet-monitor
LABEL org.opencontainers.image.description="VPS Internet Monitor - receives heartbeats and sends ntfy notifications"
LABEL org.opencontainers.image.licenses=MIT

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY monitor.py .
COPY config.yaml.example config.yaml

# Environment variables (override in docker-compose or docker run)
ENV LISTEN_HOST="0.0.0.0"
ENV LISTEN_PORT=5000
ENV HEARTBEAT_TIMEOUT=180
ENV NTFY_SERVER_URL="https://ntfy.sh"
ENV NTFY_TOPIC=""
ENV STARTUP_GRACE=120

EXPOSE 5000

CMD ["python", "-u", "monitor.py"]
