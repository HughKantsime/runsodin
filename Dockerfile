# ============================================================
# O.D.I.N. — Orchestrated Dispatch & Inventory Network
# Single-container Docker image (Home Assistant pattern)
# 
# Contains: FastAPI backend, React frontend (static), 
#           MQTT monitor, Moonraker monitor, go2rtc
# ============================================================

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

# Install go2rtc (camera proxy) — SHA256 and ELF machine verified for this image's architecture.
# The architecture comes from the image itself, so no build argument or default can select
# another architecture's binary.
ARG GO2RTC_VERSION=1.9.4
# Hashes for v1.9.4. e_machine bytes 3e00 are x86-64, b700 AArch64. An unknown architecture stops the build.
RUN go2rtc_arch="$(dpkg --print-architecture)" \
    && case "${go2rtc_arch}" in \
         amd64) go2rtc_sha256=8d86510e64e0deadee40d550d46323ddec9f62e14cec56de3a9df0f2f7fe9ada; go2rtc_elf_machine=3e00 ;; \
         arm64) go2rtc_sha256=ebea4cf3a0bc3a12190ebba1f2c4b3c8cf4aac099fadb4cd50a669429b074f1c; go2rtc_elf_machine=b700 ;; \
         *)     echo "no go2rtc checksum for arch=${go2rtc_arch:-unset}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${go2rtc_arch}" \
    -o /usr/local/bin/go2rtc \
    && echo "${go2rtc_sha256}  /usr/local/bin/go2rtc" | sha256sum -c - \
    && test "$(od -An -tx1 -j18 -N2 /usr/local/bin/go2rtc | tr -d ' ')" = "${go2rtc_elf_machine}" \
    && chmod +x /usr/local/bin/go2rtc

# ── SQLite with the WAL-reset fix ──
# Debian's libsqlite3 doesn't carry the fix for the WAL-reset corruption bug
# (https://sqlite.org/wal.html, section 11): 3.46.1-7 in this base image and 3.46.1-7+deb13u2,
# trixie's latest, have no patch touching src/wal.c. 3.51.3 is the oldest published release
# with the fix, and the last before 3.53 changed how REAL values convert to text. The flags are
# Debian's (debian/rules); docker/sqlite_image_check.py holds the result to the same option list.
FROM python:3.11-slim@sha256:0b23cfb7425d065008b778022a17b1551c82f8b4866ee5a7a200084b7e2eafbf AS sqlite-build

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /build/sqlite

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils \
    ca-certificates \
    curl \
    dpkg-dev \
    gcc \
    libc6-dev \
    make \
    && rm -rf /var/lib/apt/lists/*

# The SHA-256 pins the download; the SHA3-256 is the sqlite3.c hash sqlite.org publishes for 3.51.3.
RUN curl -fsSL "https://sqlite.org/2026/sqlite-autoconf-3510300.tar.gz" -o /tmp/sqlite.tar.gz \
    && echo "81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a  /tmp/sqlite.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/sqlite.tar.gz --strip-components=1 \
    && python3 -c "import hashlib, sys; sys.exit(hashlib.sha3_256(open('sqlite3.c', 'rb').read()).hexdigest() != '32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e')"

# _HAVE_SQLITE_CONFIG_H makes sqlite3.c read what configure found (fdatasync, usleep, isnan and
# so on), as Debian's build does; the bundle's makefile leaves it off.
RUN export DEB_BUILD_MAINT_OPTIONS="hardening=+all future=+lfs" \
    && ./configure --soname=legacy --disable-static --disable-readline --disable-carray \
       --fts4 --fts5 --rtree --session --dbpage --dbstat --update-limit \
       CFLAGS="$(dpkg-buildflags --get CFLAGS) $(dpkg-buildflags --get CPPFLAGS) -O2 -fno-strict-aliasing \
         -D_HAVE_SQLITE_CONFIG_H \
         -DSQLITE_SECURE_DELETE -DSQLITE_ENABLE_COLUMN_METADATA \
         -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS3_PARENTHESIS \
         -DSQLITE_ENABLE_RTREE=1 -DSQLITE_SOUNDEX=1 \
         -DSQLITE_ENABLE_UNLOCK_NOTIFY \
         -DSQLITE_ALLOW_ROWID_IN_VIEW \
         -DSQLITE_ENABLE_LOAD_EXTENSION \
         -DSQLITE_LIKE_DOESNT_MATCH_BLOBS \
         -DSQLITE_ENABLE_FTS3_TOKENIZER=1 \
         -DSQLITE_USE_URI=1 \
         -DSQLITE_MAX_SCHEMA_RETRY=25 \
         -DSQLITE_ENABLE_PREUPDATE_HOOK \
         -DSQLITE_ENABLE_STMTVTAB \
         -DSQLITE_STRICT_SUBTYPE=1 \
         -DSQLITE_MAX_VARIABLE_NUMBER=250000 \
         -DSQLITE_MAX_DEFAULT_PAGE_SIZE=32768 \
         -DSQLITE_MAX_FUNCTION_ARG=127" \
       LDFLAGS="$(dpkg-buildflags --get LDFLAGS) -Wl,--as-needed" \
    && make -j"$(nproc)" libsqlite3.so \
    && strip --strip-unneeded libsqlite3.so

# ── Node build stage for frontend ──
FROM node:20-slim@sha256:c6585df72c34172bebd8d36abed961e231d7d3b5cee2e01294c4495e8a03f687 AS frontend-build

WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY design/ /build/design/
COPY VERSION /build/VERSION
RUN npm run build
ARG ODIN_BUILD_OWNER=unowned
LABEL com.runsodin.validation-image-owner=${ODIN_BUILD_OWNER}

# ── Final image ──
FROM backend-base

WORKDIR /app

# ld.so.conf.d is read in name order, and on arm64 Debian's own directory sorts before
# /usr/local/lib. This file sorts first on both architectures, so _sqlite3 loads this library.
COPY --from=sqlite-build /build/sqlite/libsqlite3.so /usr/local/lib/odin-sqlite/libsqlite3.so.3.51.3
COPY docker/sqlite_image_check.py /usr/local/libexec/odin-sqlite-image-check.py
RUN echo /usr/local/lib/odin-sqlite > /etc/ld.so.conf.d/000-odin-sqlite.conf \
    && ldconfig \
    && chmod 0444 /usr/local/libexec/odin-sqlite-image-check.py \
    && python3 -I -S /usr/local/libexec/odin-sqlite-image-check.py

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
COPY docker/entrypoint.sh /usr/local/bin/odin-entrypoint
COPY docker/runtime_identity.py /usr/local/libexec/odin-runtime-identity.py
COPY docker/branding_defaults.py /usr/local/libexec/odin-branding-defaults.py
COPY docker/seed_edu_if_enabled.sh /app/seed_edu_if_enabled.sh
RUN chmod 0555 /usr/local/bin/odin-entrypoint \
    && chmod 0444 /usr/local/libexec/odin-runtime-identity.py /usr/local/libexec/odin-branding-defaults.py

# Create non-root user for runtime (supervisord drops to this user)
# entrypoint.sh still runs as root to handle secret generation and chown
RUN groupadd -r -g 10001 odin \
    && useradd -r -u 10001 -g odin -d /app -s /sbin/nologin odin

# Create data directories (will be mounted as volumes)
RUN mkdir -p /data /data/backups /data/uploads /data/static/branding /app/go2rtc \
    && mkdir -p /usr/local/share/odin/branding-defaults \
    && cp -a /app/backend/static/branding/. /usr/local/share/odin/branding-defaults/ \
    && rm -rf /app/backend/static/branding \
    && ln -s /data/static/branding /app/backend/static/branding \
    && chmod -R go-w /app \
    && chown -R odin:odin /app/go2rtc /data

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

ENTRYPOINT ["/usr/local/bin/odin-entrypoint"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/odin.conf"]
ARG ODIN_BUILD_OWNER=unowned
LABEL com.runsodin.validation-image-owner=${ODIN_BUILD_OWNER}
