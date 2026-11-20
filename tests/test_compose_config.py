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
    # churn. #573: the runtime is distroless (no /bin/sh, no curl), so the
    # probe is the viewer-rs binary itself — `--healthcheck` does a loopback
    # GET /api/v1/health and exits 0 on 200.
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "urllib.request" not in compose
        # CMD-SHELL cannot work: exec "/bin/sh" fails in the distroless image.
        assert "/dev/tcp" not in compose
        assert not re.search(r"^\s*-\s*(?:bash|sh)\s*$", compose, re.M)
        assert re.search(
            r"healthcheck:\s*\n\s*test:\s*\n\s*-\s*CMD\s*\n"
            r"\s*-\s*/usr/local/bin/viewer-rs\s*\n\s*-\s*--healthcheck",
            compose,
        )


def test_dockerfile_runtime_is_distroless():
    # #573: no shell/apt/curl in the final stage. `cc` (not `static`) because
    # the binary is glibc-linked and the bundled unrar C++ needs
    # libstdc++/libgcc. distroless has no ldconfig, so the PDF engine is
    # located explicitly — without it pages silently answer 501.
    assert "FROM gcr.io/distroless/cc-debian12" in DOCKERFILE
    assert "PDFIUM_LIB_PATH=/usr/local/lib/libpdfium.so" in DOCKERFILE


# ── #223: RAR content serving needs the reference decoder ─────────────
#
# 7-Zip alone cannot decode some proprietary RAR methods. The reference
# decoder is RARLab unrar — viewer-rs compiles the bundled unrar C++
# (bookq-core `unrar` dep) at build time and links libstdc++6 at runtime.


def test_dockerfile_builds_bundled_unrar():
    assert "unrar" in DOCKERFILE
    # libstdc++6 is no longer apt-installed by the runtime stage (#573): the
    # distroless `cc` base supplies it — which is also why `static-debian12`
    # (no libstdc++) cannot be used.
    assert "distroless/cc-debian12" in DOCKERFILE


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


def test_dev_compose_library_stay_writable():
    # Library editing (upload/delete via /api/v1/library/*) writes into the
    # library tree — prod stays rw.
    assert ":/app/library:rw" in PROD_COMPOSE


def test_dev_compose_library_defaults_readonly():
    # Dev runs BOOKQ_TEST_MODE=true (auth bypass), so a writable mount of the
    # real library is opt-in: the mode interpolates to ro unless
    # DEV_LIBRARY_MODE is set (dev.sh --fixture / --writable-library → rw).
    assert ":/app/library:${DEV_LIBRARY_MODE:-ro}" in DEV_COMPOSE


def test_user_state_is_not_a_cache_mount():
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "/var/lib/bookq" in compose
        # state volume is distinct from the warmup/thumb cache mounts
        assert "user-state:/var/cache" not in compose

