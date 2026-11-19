"""Exceptions for the Safari driver layer."""

import pytest


class SafariUnsupported(pytest.skip.Exception):
    """Feature has no WebDriver/Safari equivalent.

    Subclasses pytest's Skipped exception, so raising it anywhere — inside a
    fixture, helper, or the test itself — reports the test as skipped rather
    than failed, with this message as the reason.
    """
