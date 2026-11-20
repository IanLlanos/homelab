FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPER_DATA=/data \
    PAPER_CONFIG=/app/config.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY paper ./paper
RUN useradd --system --uid 10001 paper && mkdir /data && chown paper /data
USER paper

EXPOSE 8790
# Liveness only. Whether today's paper exists is reported by /health for Uptime Kuma.
HEALTHCHECK --interval=5m --timeout=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/static/paper.css', timeout=5)"

CMD ["python", "-m", "paper", "serve"]
