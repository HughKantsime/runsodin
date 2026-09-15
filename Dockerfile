# ============================================================
# O.D.I.N. — Orchestrated Dispatch & Inventory Network
# Single-container Docker image (Home Assistant pattern)
# 
# Contains: FastAPI backend, React frontend (static), 
#           MQTT monitor, Moonraker monitor, go2rtc
# ============================================================

ARG ODIN_BUILD_OWNER=unowned

FROM python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf AS backend-base

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# System deps — versions intentionally unpinned to track Debian security updates
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gnupg \
    supervisor \
    curl \
    ffmpeg \
    && install -d -m 0755 /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76  /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc" | sha256sum -c - \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# Install go2rtc (camera proxy) — SHA256 verified per architecture
ARG GO2RTC_VERSION=1.9.4
ARG TARGETARCH=amd64
# Hashes for v1.9.4: amd64 and arm64 (add others if new arches are targeted)
RUN curl -fsSL "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}" \
    -o /usr/local/bin/go2rtc \
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
COPY design/ /build/design/
COPY VERSION /build/VERSION
RUN npm run build
ARG ODIN_BUILD_OWNER
LABEL com.runsodin.validation-image-owner=${ODIN_BUILD_OWNER}

# ── Final image ──
FROM backend-base

WORKDIR /app

# Python dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/
COPY VERSION /app/VERSION
COPY ops/demo/demo_publisher.py /app/ops/demo/demo_publisher.py
COPY ops/edu_sandbox/tcp_proxy.py /app/ops/edu_sandbox/tcp_proxy.py
COPY tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl /app/tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl

# Copy built frontend into backend static serving directory
COPY --from=frontend-build /build/frontend/dist ./frontend/dist

# Copy go2rtc default config
COPY docker/go2rtc.yaml /app/go2rtc/go2rtc.yaml

# Copy supervisord config
COPY docker/supervisord.conf /etc/supervisor/conf.d/odin.conf
COPY docker/supervisord.api.conf /etc/supervisor/conf.d/api.conf
COPY docker/supervisord.monitors.conf /etc/supervisor/conf.d/monitors.conf
COPY docker/supervisord.vision.conf /etc/supervisor/conf.d/vision.conf
COPY docker/supervisord.reports.conf /etc/supervisor/conf.d/reports.conf

# Copy entrypoint
COPY docker/entrypoint.sh /app/entrypoint.sh
COPY docker/seed_edu_if_enabled.sh /app/seed_edu_if_enabled.sh
RUN chmod +x /app/entrypoint.sh

# Create non-root user for runtime (supervisord drops to this user)
# entrypoint.sh still runs as root to handle secret generation and chown
RUN groupadd -r -g 10001 odin \
    && useradd -r -u 10001 -g odin -d /app -s /sbin/nologin odin

# Create data directories (will be mounted as volumes)
RUN mkdir -p /data /data/backups /data/uploads /data/static/branding /app/go2rtc \
    && chown -R odin:odin /app /data 2>/dev/null || true

# Default environment
ENV PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/odin.db \
    HOST=0.0.0.0 \
    PORT=8000

# Ports: 8000 (API+frontend), 8555 (go2rtc WebRTC)
# Port 1984 (go2rtc HLS/API) is bound to 127.0.0.1 inside the container and not exposed
EXPOSE 8000 8555

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/odin.conf"]
ARG ODIN_BUILD_OWNER
LABEL com.runsodin.validation-image-owner=${ODIN_BUILD_OWNER}
