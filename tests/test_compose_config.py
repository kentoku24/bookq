"""Regression tests for #113: viewer runs as a single in-process worker.

Post-#503 the backend is the single viewer-rs binary (Python + hypercorn
deleted). These tests pin the compose files and Dockerfile.viewer-rs to
that reality: no dead env, healthcheck + restart + init intact.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PROD_COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
DEV_COMPOSE = (REPO_ROOT / "docker-compose.dev.yml").read_text()
DOCKERFILE = (REPO_ROOT / "Dockerfile.viewer-rs").read_text()


def test_prod_compose_has_no_web_concurrency():
    assert "WEB_CONCURRENCY" not in PROD_COMPOSE


def test_dev_compose_has_no_web_concurrency():
    assert "WEB_CONCURRENCY" not in DEV_COMPOSE


def test_dockerfile_runs_viewer_rs_in_process():
    # The whole backend is one static binary — no interpreter, no worker
    # supervisor, no spawn overhead.
    assert re.search(r'ENTRYPOINT \["/usr/local/bin/viewer-rs"\]', DOCKERFILE)


def test_prod_compose_keeps_healthcheck_and_restart():
    assert "healthcheck:" in PROD_COMPOSE
    assert "restart: unless-stopped" in PROD_COMPOSE


def test_dev_compose_keeps_healthcheck_and_restart():
    assert "healthcheck:" in DEV_COMPOSE
    assert "restart: unless-stopped" in DEV_COMPOSE


def test_viewer_healthcheck_does_not_spawn_python():
    # The healthcheck fires every 2s for the container's lifetime; spawning a
    # python interpreter each time costs ~10-15MB transient plus fork/exec
    # churn. The bash /dev/tcp probe answers the /api/v1/health 200 contract
    # without a second interpreter.
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "urllib.request" not in compose
        assert "/dev/tcp" in compose
        assert "/api/v1/health" in compose


# ── #223: RAR content serving needs the reference decoder ─────────────
#
# 7-Zip alone cannot decode some proprietary RAR methods. The reference
# decoder is RARLab unrar — viewer-rs compiles the bundled unrar C++
# (bookq-core `unrar` dep) at build time and links libstdc++6 at runtime.


def test_dockerfile_builds_bundled_unrar():
    assert "unrar" in DOCKERFILE
    assert "libstdc++6" in DOCKERFILE


# ── folder-visibility user state: dedicated volume, not a cache ───────
#
# Per-user hidden-root settings live in $BOOKQ_STATE_DIR/folder-visibility.sqlite3.
# They must survive container recreation (unlike /tmp) and must NOT ride the
# warmup/thumb cache volumes — a cache wipe would silently un-hide folders.


def test_prod_compose_mounts_user_state_volume():
    assert "BOOKQ_STATE_DIR=/var/lib/bookq" in PROD_COMPOSE
    assert "user-state:/var/lib/bookq" in PROD_COMPOSE
    assert re.search(r"^volumes:\n(?:\s+\S.*\n)*\s{2}user-state:", PROD_COMPOSE, re.M)


def test_dev_compose_mounts_user_state_volume():
    assert "BOOKQ_STATE_DIR=/var/lib/bookq" in DEV_COMPOSE
    assert "user-state:/var/lib/bookq" in DEV_COMPOSE
    assert re.search(r"^volumes:\n(?:\s+\S.*\n)*\s{2}user-state:", DEV_COMPOSE, re.M)


def test_dev_compose_downloads_stay_writable():
    # Library editing (upload/delete via /api/v1/library/*) writes into the
    # downloads tree — the mount must stay rw in dev and prod.
    assert ":/app/downloads:rw" in DEV_COMPOSE
    assert ":/app/downloads:rw" in PROD_COMPOSE


def test_user_state_is_not_a_cache_mount():
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "/var/lib/bookq" in compose
        # state volume is distinct from the warmup/thumb cache mounts
        assert "user-state:/var/cache" not in compose

