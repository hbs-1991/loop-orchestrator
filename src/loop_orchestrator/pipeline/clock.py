"""One clock for the whole pipeline package.

Every stage measures its budget against the same `monotonic`, and the tests
replace it with a fake one that the patched `asyncio.sleep` advances. The stage
modules import this module and call `clock.monotonic()` rather than binding the
name — one patch here reaches every stage instead of only the module that
happened to bind it first.
"""

from time import monotonic

__all__ = ["monotonic"]
