# ============================================================
# O.D.I.N. — Orchestrated Dispatch & Inventory Network
# Single-container Docker image (Home Assistant pattern)
# 
# Contains: FastAPI backend, React frontend (static), 
#           MQTT monitor, Moonraker monitor, go2rtc
# ============================================================

FROM python:3.11-slim AS backend-base

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    wget \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install go2rtc (camera proxy)
ARG GO2RTC_VERSION=1.9.4
ARG TARGETARCH=amd64
RUN wget -q "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}" \
    -O /usr/local/bin/go2rtc && chmod +x /usr/local/bin/go2rtc

# ── Node build stage for frontend ──
FROM node:20-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY VERSION /build/VERSION
RUN npm run build

# ── Final image ──
FROM backend-base

WORKDIR /app

# Python dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/
COPY VERSION /app/VERSION

# Copy built frontend into backend static serving directory
COPY --from=frontend-build /build/frontend/dist ./frontend/dist

# Copy go2rtc default config
COPY docker/go2rtc.yaml /app/go2rtc/go2rtc.yaml

# Copy supervisord config
COPY docker/supervisord.conf /etc/supervisor/conf.d/odin.conf

# Copy entrypoint
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create data directories (will be mounted as volumes)
RUN mkdir -p /data /data/backups /data/uploads /data/static/branding /app/go2rtc

# Default environment
ENV PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/odin.db \
    ENCRYPTION_KEY="" \
    JWT_SECRET_KEY="" \
    API_KEY="" \
    HOST=0.0.0.0 \
    PORT=8000

# Ports: 8000 (API), 3000 (frontend/proxy), 1984 (go2rtc)
EXPOSE 8000 1984

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# v1.0.10
