FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOCK_HOST=0.0.0.0 \
    STOCK_PORT=8795 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 stock \
    && useradd --uid 10001 --gid stock --no-create-home --shell /usr/sbin/nologin stock

COPY --chown=stock:stock app.py ./
COPY --chown=stock:stock analysis/ ./analysis/
COPY --chown=stock:stock data/__init__.py data/kline_fetcher.py ./data/
COPY --chown=stock:stock dashboard/ ./dashboard/
COPY --chown=stock:stock libs/ ./libs/

USER stock

EXPOSE 8795

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port=os.environ.get('STOCK_PORT', '8795'); urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3).close()"]

CMD ["python", "app.py"]
