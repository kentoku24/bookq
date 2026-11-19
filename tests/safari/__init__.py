"""WebDriver-backed Safari driver for the browser test suite.

Playwright cannot drive real Safari — Safari only speaks WebDriver via
``safaridriver``. This package implements the Playwright-subset API the test
suite uses (``page``/``locator``/``expect`` shapes) on top of selenium, so the
existing browser tests run unchanged under ``BROWSER=safari``.
"""

from tests.safari.exceptions import SafariUnsupported  # noqa: F401
