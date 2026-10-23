# syntax=docker/dockerfile:1.7
# Server-only image. Signed Debian runtime packages avoid PyPI CDN dependence.
FROM debian:trixie-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VIEWER_DATA_DIR=/data VIEWER_PORT=8080
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python-is-python3 python3-flask python3-waitress \
    && rm -rf /var/lib/apt/lists/*
# Explicit code/asset COPY: no local/, deamon, tests, histories, keys or databases.
COPY server/*.py ./server/
COPY server/contracts/*.py ./server/contracts/
COPY server/static/index.html server/static/favicon.ico ./server/static/
COPY server/static/js/*.js ./server/static/js/
COPY server/static/css/*.css ./server/static/css/
COPY server/static/*.js server/static/*.css ./server/static/
RUN useradd --uid 10001 --create-home viewer \
    && mkdir -p /data && chown viewer:viewer /data && chmod 700 /data
ARG VCS_REF=unknown
ENV VIEWER_BUILD_REVISION=${VCS_REF}
LABEL org.opencontainers.image.title="Agent Session Viewer — server" \
      org.opencontainers.image.source="https://github.com/idyllic84/agent-session-viewer" \
      org.opencontainers.image.revision=${VCS_REF}
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "server.healthcheck"]
ENTRYPOINT ["python", "-m", "server.container_entry"]
