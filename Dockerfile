# الانتقال لنسخة slim لدعم المتصفحات بشكل أفضل
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV ENV_MODE production
WORKDIR /app

# تثبيت المكتبات اللازمة لـ WeasyPrint وأتمتة المتصفح (Debian style)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    # مكتبات PDF (WeasyPrint)
    libpango-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    # مكتبات المتصفح (Playwright/Puppeteer)
    libnss3 \
    libatk1.0-0 \
    libdbus-1-3 \
    libgbm1 \
    && rm -rf /var/lib/apt/lists/*

# تثبيت الاعتمادات باستخدام uv
COPY pyproject.toml uv.lock ./
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --quiet

# تثبيت المتصفحات (ضروري جداً لمشروعك)
# RUN uv run playwright install chromium --with-deps

COPY . .

ENV PYTHONPATH=/app
EXPOSE 8000

# تصحيح الـ Entrypoint بناءً على هيكل مجلداتك (api:app)
CMD ["sh", "-c", "uv run gunicorn api:app -w ${WORKERS:-4} -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout ${TIMEOUT:-75} --graceful-timeout 30 --keep-alive 65"]
