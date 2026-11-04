# Runs on Render (free Docker web service, $PORT) and Hugging Face Spaces (Docker SDK; needs PRO).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860 \
    APP_ENV=production

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
