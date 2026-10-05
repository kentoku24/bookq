# Build stage: install dependencies into an isolated venv
FROM python:3.14-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Runtime stage: copy only the venv + application code
FROM python:3.14-slim

# RAR/CBR extraction backends for rarfile. 7-Zip alone cannot decode some
# proprietary RAR methods ("Unsupported Method" -> rarfile BadRarFile ->
# silent 404; seen on 58/150 entries of a real RAR5 volume), and the free
# variant only handles RAR<=2, so the reference decoder is RARLab unrar
# (Debian non-free, freeware; base image ships main only, hence the
# component opt-in below). rarfile prefers unrar when present and keeps
# 7zip as fallback.
RUN sed -i 's/^Components: main$/Components: main non-free/' \
        /etc/apt/sources.list.d/*.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends 7zip unrar \
    && rm -rf /var/lib/apt/lists/*

ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV BOOKQ_GIT_SHA=$GIT_SHA
ENV BOOKQ_BUILD_DATE=$BUILD_DATE
# Container-internal downloads mount point. warmup_cache.cache_key_for_path
# resolves DOWNLOADS_DIR at call time: with this set, out-of-scan callers
# (api_images, image serving) key cache entries r:-relative instead of the
# a: abspath fallback (scan_tree additionally threads its base explicitly).
ENV DOWNLOADS_DIR=/app/downloads

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application code
COPY app/ ./app/

# Create downloads mount point
RUN mkdir -p /app/downloads

EXPOSE 8080

ENTRYPOINT ["hypercorn", "app.main:app"]
CMD ["--bind", "0.0.0.0:8080"]
