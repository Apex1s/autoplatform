# Runs on any Docker host: Back4App Containers (free, no card), Render, Hugging Face Spaces (PRO).
FROM python:3.15-slim

# Production defaults below are not secrets; override any of them in the host's env settings.
# Secrets (DATABASE_URL, UPSTASH_REDIS_URL, GROQ_API_KEY, GEMINI_API_KEY, ADMIN_TOKEN, CRON_TOKEN)
# are set on the host, never here.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860 \
    APP_ENV=production \
    RUN_EMBEDDED_WORKER=true \
    WORKER_CONCURRENCY=2 \
    WORKER_POLL_TIMEOUT=40 \
    HEARTBEAT_INTERVAL=120 \
    WORKER_HEALTH_PERSIST_SECONDS=0 \
    CRON_OIDC_REPOSITORY=Apex1s/autoplatform

# HF Spaces runs containers as uid 1000.
RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=user:user . .
USER user

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/livez' % os.environ.get('PORT','7860'), timeout=4)"

CMD ["sh", "scripts/start.sh"]
