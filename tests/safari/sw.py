"""Service-worker interception probe.

safaridriver activates service workers fine (registration, activation, even
``navigator.serviceWorker.controller === true``) but never dispatches
FetchEvent to them — verified on Safari 27.2: controlled ``fetch()`` calls
leave every cache empty. Offline tests therefore cannot pass on real Safari;
they skip honestly via :func:`ensure_sw_interception`.
"""

from .exceptions import SafariUnsupported

_PROBE_JS = """(async () => {
  try {
    const c = await caches.open('bookq-api-v1');
    await fetch('/api/v1/tree', {cache: 'no-store'});
    for (let i = 0; i < 30; i++) {
      if ((await c.keys()).length) return true;
      await new Promise(r => setTimeout(r, 100));
    }
  } catch (e) {}
  return false;
})()"""


def ensure_sw_interception(page) -> None:
    """Skip the test when SW fetch interception cannot work on this driver."""
    if page.__class__.__name__ != "SafariPage":
        return
    cached = getattr(page, "_sw_intercepts", None)
    if cached is None:
        try:
            cached = bool(page.evaluate(_PROBE_JS))
        except Exception:
            cached = False
        page._sw_intercepts = cached
    if not cached:
        raise SafariUnsupported(
            "service-worker fetch events never fire under safaridriver "
            "(register/activate work; interception does not)"
        )
