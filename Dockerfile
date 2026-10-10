FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER pwuser

CMD ["sh", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1280x800x24 -nolisten tcp -noreset & exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --worker-class gthread --workers 1 --threads 8 --graceful-timeout 30"]
