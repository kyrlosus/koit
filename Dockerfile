# 1. Base Image
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV ENV_MODE production
ENV PYTHONUNBUFFERED=1 
ENV UV_LINK_MODE=copy
ENV PYTHONPATH=/app
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 2. Install System Dependencies + Redis
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    python3-dev \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

# 3. Setup UV and Install Dependencies
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --quiet

# 4. Install Playwright
RUN . .venv/bin/activate && pip install playwright && playwright install chromium --with-deps

# 5. Copy Application Code
COPY . .

# Setup Permissions
RUN useradd -m -u 1000 user
RUN mkdir -p /var/lib/redis && chown -R user:user /var/lib/redis /etc/redis /var/log/redis
RUN chown -R user:user /app

USER user

# ⚠️ التغيير الأول: خلينا البورت 8000 عشان يتوافق مع Koyeb
EXPOSE 8000

# 6. Start Command
# ⚠️ التغيير الثاني: الأمر ده بيشغل Redis الأول وبعدين يشغل التطبيق على بورت 8000
CMD ["sh", "-c", "redis-server --daemonize yes && uv run uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1"]
