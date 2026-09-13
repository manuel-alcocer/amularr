FROM python:3.12-slim

LABEL org.opencontainers.image.title="amularr" \
      org.opencontainers.image.description="Torznab indexer and qBittorrent-compatible download client bridge for aMule" \
      org.opencontainers.image.source="https://github.com/manuel-alcocer/amularr"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY amularr ./amularr
RUN pip install --no-cache-dir . \
    && mkdir -p /data \
    && chown 1000:100 /data

USER 1000:100
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=4).status == 200 else 1)"

CMD ["amularr"]
