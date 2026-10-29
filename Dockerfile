# Build stage: install dependencies into an isolated venv
FROM python:3.14-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Native build stage: bookq_rs PyO3 wheel (Phase 1 W1, issue #163).
# The wheel is optional at runtime — app/scanner.py falls back to the Python
# reference impl on ImportError — but shipping it is how prod gets the native
# sort-key path. abi3-py314: one wheel covers every CPython >=3.14 GIL build.
FROM python:3.14-slim AS rsbuilder

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:$PATH"

WORKDIR /rs
COPY rust/bookq_rs/ ./
RUN pip install --no-cache-dir "maturin>=1.5,<2" && maturin build --release

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

# bookq_rs native sort-key core (Phase 1 W1, issue #163). Removing this
# install line is the image-level rollback: app runs the Python impl instead.
COPY --from=rsbuilder /rs/target/wheels/bookq_rs-*.whl /tmp/
RUN /opt/venv/bin/pip install --no-cache-dir /tmp/bookq_rs-*.whl \
    && rm -f /tmp/bookq_rs-*.whl

WORKDIR /app

# Copy application code
COPY app/ ./app/

# Create downloads mount point
RUN mkdir -p /app/downloads

EXPOSE 8080

ENTRYPOINT ["hypercorn", "app.main:app"]
CMD ["--bind", "0.0.0.0:8080"]
