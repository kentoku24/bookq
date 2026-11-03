"""Regression tests for #113: viewer runs single-worker hypercorn; WEB_CONCURRENCY is dead.

hypercorn never reads the WEB_CONCURRENCY env var (gunicorn convention). The
Dockerfile CMD passes --workers 0, which serves inside the entrypoint process
itself instead of under a multiprocessing spawn supervisor — same single-worker
concurrency, minus the ~40MB supervisor + resource_tracker overhead. These
tests pin the compose files and Dockerfile to that reality: no dead env,
healthcheck + restart + init intact.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PROD_COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text()
DEV_COMPOSE = (REPO_ROOT / "docker-compose.dev.yml").read_text()
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text()


def test_prod_compose_has_no_web_concurrency():
    assert "WEB_CONCURRENCY" not in PROD_COMPOSE


def test_dev_compose_has_no_web_concurrency():
    assert "WEB_CONCURRENCY" not in DEV_COMPOSE


def test_dockerfile_runs_hypercorn_in_process():
    # --workers 0 keeps the same single-worker concurrency while dropping the
    # multiprocessing spawn supervisor (~30MB) and resource_tracker (~12MB)
    # that hypercorn's default workers=1 model parks next to the real worker.
    assert re.search(r'"--workers",\s*"0"', DOCKERFILE)


def test_prod_compose_keeps_healthcheck_and_restart():
    assert "healthcheck:" in PROD_COMPOSE
    assert "restart: unless-stopped" in PROD_COMPOSE
    assert "init: true" in PROD_COMPOSE


def test_dev_compose_keeps_healthcheck_and_restart():
    assert "healthcheck:" in DEV_COMPOSE
    assert "restart: unless-stopped" in DEV_COMPOSE
    assert "init: true" in DEV_COMPOSE


def test_viewer_healthcheck_does_not_spawn_python():
    # The healthcheck fires every 2s for the container's lifetime; spawning a
    # python interpreter each time costs ~10-15MB transient plus fork/exec
    # churn. The bash /dev/tcp probe (same shape as viewer-rs) answers the
    # same /api/health 200 contract without a second interpreter.
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "urllib.request" not in compose
        assert "/dev/tcp" in compose
        assert "/api/health" in compose


# ── #223: RAR content serving needs the reference decoder ─────────────
#
# 7-Zip alone cannot decode some proprietary RAR methods ("Unsupported
# Method" -> rarfile BadRarFile "Failed the read enough data" -> silent
# 404; 58/150 entries of Made_in_Abyss_v15.rar in a 7zip-only container).
# The reference decoder is RARLab unrar (Debian non-free; the -free variant
# handles RAR<=2 only, verified useless against RAR5 on trixie).


def test_dockerfile_installs_unrar_from_nonfree():
    install = re.search(r"apt-get install[^\n]*", DOCKERFILE)
    assert install and "unrar" in install.group(0)
    assert "non-free" in DOCKERFILE


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


def test_dev_compose_downloads_stay_readonly():
    assert ":/app/downloads:ro" in DEV_COMPOSE


def test_user_state_is_not_a_cache_mount():
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "/var/lib/bookq" in compose
        # state volume is distinct from the warmup/thumb cache mounts
        assert "user-state:/var/cache" not in compose

