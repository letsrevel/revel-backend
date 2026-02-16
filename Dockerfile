# syntax=docker/dockerfile:1.4
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# ── install WeasyPrint dependencies ──────────────────────────────────────────
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libcairo2 \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libgdk-pixbuf2.0-0 \
      libglib2.0-0 \
      libffi-dev \
      gdal-bin \
      libgdal-dev \
      # libmagic for python-magic (MIME type detection)
      libmagic1 \
 && rm -rf /var/lib/apt/lists/*
# ───────────────────────────────────────────────────────────────────────────────

# Environment variables for the builder stage
ENV UV_SYSTEM_PYTHON=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PYTHONUNBUFFERED=1
ENV DOCKER_BUILD=1
# These are used by manage.py commands during build
ENV SECRET_KEY=whatever
ENV FERNET_KEY=whatever
ENV SALT_KEY=whatever
ENV TELEGRAM_BOT_TOKEN=whatever
ENV DEBUG=1

WORKDIR /app

# Copy dependency descriptors
COPY pyproject.toml uv.lock ./

# Install Python dependencies (excluding the project itself initially for caching)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-editable

# Copy the entire project
COPY . /app

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable

# Define static root and collect static files
ENV STATIC_ROOT=/app/staticfiles
RUN mkdir -p ${STATIC_ROOT}
RUN uv run python src/manage.py collectstatic --noinput

# Install Playwright browser binaries (not system dependencies here)
# RUN uv run playwright install chromium


# ─────────────────────────────────────────────

FROM python:3.13-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies needed by Playwright/Chromium
RUN rm -rf /var/lib/apt/lists/* && \
    apt-get update && \
    apt-get install -y \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    # fonts-noto-cjk is large. If CJK characters are not strictly needed by your app's rendering,
    # consider a smaller font package like fonts-liberation.
    fonts-noto-cjk \
    libgcc1 \
    libgbm1 \
    libglib2.0-0 \
    # libgobject-2.0-0 \
    libgdk-pixbuf-2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxrender1 \
    libxshmfence1 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    # Additional dependencies often required by Playwright/Chromium on Debian
    libdrm2 \
    libatspi2.0-0 \
    libxkbcommon0 \
    libfreetype6 \
    libx11-6 \
    libxcb1 \
    gdal-bin \
    libgdal-dev \
    # libmagic for python-magic (MIME type detection)
    libmagic1 \
    --no-install-recommends && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user 'appuser' with a home directory
RUN useradd --create-home --system --shell /bin/bash appuser

# Copy built Python virtual environment
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Copy application source code
COPY --from=builder --chown=appuser:appuser /app/src /app/src

# Copy collected static files (ensure STATIC_ROOT in builder matches source here)
COPY --from=builder --chown=appuser:appuser /app/staticfiles /app/staticfiles

# Copy entrypoint script
COPY --from=builder --chown=appuser:appuser /app/entrypoint.sh /app/entrypoint.sh

# Copy Playwright browser cache to the appuser's home directory cache
# COPY --from=builder --chown=appuser:appuser /root/.cache/ms-playwright /home/appuser/.cache/ms-playwright

# Set environment variables for the runtime environment
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV DOCKER_BUILD=1
# Explicitly tell Playwright where to find browsers, matching the COPY destination
ENV PLAYWRIGHT_BROWSERS_PATH=/home/appuser/.cache/ms-playwright

WORKDIR /app/src
USER appuser

RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]