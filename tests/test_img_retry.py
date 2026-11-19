"""Strip image failure retry (#156).

A single failed page-image fetch used to leave the page black until a full
reload: the <img> stayed in ``stripImgs`` with no refetch path. The strip
now tracks failures per index, auto-retries with exponential backoff
(bounded by IMG_RETRY_MAX), then surfaces an on-page ``#img-retry``
button once the automatic budget is spent.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_img_retry.py -q
"""

from playwright.sync_api import expect


def _open_long_at_p2(page, server):
    page.goto(f"{server}/#/view/long?p=2")
    expect(page.locator("#counter")).to_contain_text("3 / 30")


def _img_for(page, idx):
    return page.evaluate(
        """(idx) => {
            const img = [...document.querySelectorAll('#img-track img')]
                .find(i => i.style.getPropertyValue('--i') === String(idx));
            return img ? {nw: img.naturalWidth, complete: img.complete,
                          cls: img.className} : null;
        }""",
        idx,
    )


def _wait_img_loaded(page, idx, timeout=10000):
    page.wait_for_function(
        """(idx) => {
            const img = [...document.querySelectorAll('#img-track img')]
                .find(i => i.style.getPropertyValue('--i') === String(idx));
            return !!(img && img.complete && img.naturalWidth > 0);
        }""",
        arg=idx,
        timeout=timeout,
    )


def test_single_image_failure_auto_retries(page, server):
    """One aborted fetch heals by itself — no reload, no navigation."""
    hits = {"n": 0}

    def handler(route):
        hits["n"] += 1
        if hits["n"] == 1:
            route.abort()
        else:
            route.continue_()

    page.route("**/image/long/p003.png*", handler)
    _open_long_at_p2(page, server)

    # The failure surfaces as ⚠ first, then the backoff retry heals it.
    expect(page.locator("#counter")).to_contain_text("⚠", timeout=5000)
    expect(page.locator("#counter")).not_to_contain_text("⚠", timeout=10000)
    assert hits["n"] >= 2, "the strip must re-fetch the failed image"
    _wait_img_loaded(page, 2)
    st = _img_for(page, 2)
    assert st and st["nw"] > 0
    expect(page.locator("#img-retry")).to_be_hidden()


def test_image_failure_cap_shows_manual_retry(page, server):
    """Persistent failure stops auto-retrying and offers a manual button;
    a user retry after the fault clears loads the page."""
    hits = {"n": 0}

    def handler(route):
        hits["n"] += 1
        route.abort()

    page.route("**/image/long/p003.png*", handler)
    _open_long_at_p2(page, server)

    # 1 initial fetch + IMG_RETRY_MAX(3) auto retries, then the button.
    expect(page.locator("#img-retry")).to_be_visible(timeout=20000)
    assert hits["n"] >= 4
    st = _img_for(page, 2)
    assert st and st["nw"] == 0

    page.unroute("**/image/long/p003.png*")
    page.click("#img-retry")
    expect(page.locator("#img-retry")).to_be_hidden(timeout=5000)
    expect(page.locator("#counter")).not_to_contain_text("⚠", timeout=5000)
    _wait_img_loaded(page, 2)
    st = _img_for(page, 2)
    assert st and st["nw"] > 0


def test_manual_retry_failure_restores_warning(page, server):
    """A manual retry that fails again must put the ⚠ back — the button's
    optimistic counter reset is not a heal."""
    hits = {"n": 0}

    def handler(route):
        hits["n"] += 1
        route.abort()

    page.route("**/image/long/p003.png*", handler)
    _open_long_at_p2(page, server)
    expect(page.locator("#img-retry")).to_be_visible(timeout=20000)
    expect(page.locator("#counter")).to_contain_text("⚠")

    before = hits["n"]
    page.click("#img-retry")
    # The refetch fails again → warning returns; the button stays hidden
    # because the manual attempt reset the automatic budget (count=1).
    page.wait_for_timeout(800)
    assert hits["n"] > before, "manual retry must re-fetch the image"
    expect(page.locator("#counter")).to_contain_text("⚠")
    expect(page.locator("#img-retry")).to_be_hidden()


def test_healthy_image_shows_no_retry_button(page, server):
    _open_long_at_p2(page, server)
    _wait_img_loaded(page, 2)
    st = _img_for(page, 2)
    assert st and st["nw"] > 0
    expect(page.locator("#img-retry")).to_be_hidden()
    expect(page.locator("#counter")).not_to_contain_text("⚠")
