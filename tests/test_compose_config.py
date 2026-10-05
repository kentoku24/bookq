"""Regression tests for #113: viewer runs single-worker hypercorn; WEB_CONCURRENCY is dead.

hypercorn never reads the WEB_CONCURRENCY env var (gunicorn convention), and the
Dockerfile ENTRYPOINT/CMD passes no --workers flag, so the viewer is effectively
single-process. These tests pin the compose files to that reality: no dead env,
healthcheck + restart intact.
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


def test_dockerfile_passes_no_workers_flag():
    # Todo4 never touches the Dockerfile in either branch; the viewer entrypoint
    # stays on hypercorn's single-worker default.
    assert "--workers" not in DOCKERFILE


def test_prod_compose_keeps_healthcheck_and_restart():
    assert "healthcheck:" in PROD_COMPOSE
    assert "restart: unless-stopped" in PROD_COMPOSE


def test_dev_compose_keeps_healthcheck_and_restart():
    assert "healthcheck:" in DEV_COMPOSE
    assert "restart: unless-stopped" in DEV_COMPOSE


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
