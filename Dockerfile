# ============================================================
# O.D.I.N. — Orchestrated Dispatch & Inventory Network
# Single-container Docker image (Home Assistant pattern)
# 
# Contains: FastAPI backend, React frontend (static), 
#           MQTT monitor, Moonraker monitor, go2rtc
# ============================================================

FROM python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf AS backend-base

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    wget \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install go2rtc (camera proxy) — SHA256 verified per architecture
ARG GO2RTC_VERSION=1.9.4
ARG TARGETARCH=amd64
# Hashes for v1.9.4: amd64 and arm64 (add others if new arches are targeted)
RUN wget -q "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}" \
    -O /usr/local/bin/go2rtc \
    && case "${TARGETARCH}" in \
         amd64) echo "8d86510e64e0deadee40d550d46323ddec9f62e14cec56de3a9df0f2f7fe9ada  /usr/local/bin/go2rtc" | sha256sum -c - ;; \
         arm64) echo "ebea4cf3a0bc3a12190ebba1f2c4b3c8cf4aac099fadb4cd50a669429b074f1c  /usr/local/bin/go2rtc" | sha256sum -c - ;; \
         *)     echo "WARNING: no SHA256 checksum configured for arch=${TARGETARCH}" >&2 ;; \
       esac \
    && chmod +x /usr/local/bin/go2rtc

# ── Node build stage for frontend ──
FROM node:20-slim@sha256:c6585df72c34172bebd8d36abed961e231d7d3b5cee2e01294c4495e8a03f687 AS frontend-build

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

# Create non-root user for runtime (supervisord drops to this user)
# entrypoint.sh still runs as root to handle secret generation and chown
RUN groupadd -r odin && useradd -r -g odin -d /app -s /sbin/nologin odin

# Create data directories (will be mounted as volumes)
RUN mkdir -p /data /data/backups /data/uploads /data/static/branding /app/go2rtc \
    && chown -R odin:odin /app /data 2>/dev/null || true

# Default environment
ENV PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/odin.db \
    ENCRYPTION_KEY="" \
    JWT_SECRET_KEY="" \
    API_KEY="" \
    HOST=0.0.0.0 \
    PORT=8000

# Ports: 8000 (API+frontend), 8555 (go2rtc WebRTC)
# Port 1984 (go2rtc HLS/API) is bound to 127.0.0.1 inside the container and not exposed
EXPOSE 8000 8555

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
