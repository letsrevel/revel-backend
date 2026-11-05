# CI base image: every system dependency the test/lint jobs need, so no job
# ever runs apt-get (GitHub's Azure mirror randomly trickles for 5-18 min).
# Built and pushed by .github/workflows/ci-image.yaml, tagged by the hash of
# THIS file, so editing it (including this comment) publishes a new image.
# The Python venv is deliberately not baked in: `uv sync` stays per-job.
#
# Security refresh: 2026-11 (bump this date to force a rebuild with fresh packages).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

LABEL org.opencontainers.image.source=https://github.com/letsrevel/revel-backend

# git: actions/checkout + the `git diff uv.lock` check.
# build-essential: zopfli (transitive, mkdocs-minify) has no cp314 wheel, so it builds from sdist.
# WeasyPrint libs: same list as the prod Dockerfile builder stage.
# fonts-dejavu-core: slim has no fonts; PDF tests render text.
# libmagic1: python-magic. gdal-bin: Django GIS loads libgdal via ctypes. gettext: compilemessages.
# ca-certificates + zstd: HTTPS for actions and fast actions/cache extraction.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gdal-bin \
      gettext \
      libmagic1 \
      git \
      build-essential \
      libcairo2 \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libgdk-pixbuf2.0-0 \
      libglib2.0-0 \
      libffi-dev \
      fonts-dejavu-core \
      ca-certificates \
      zstd \
 && rm -rf /var/lib/apt/lists/*
