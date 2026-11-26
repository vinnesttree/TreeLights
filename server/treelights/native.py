"""The optional Rust kernels (server/native, module `treelights_native`).

`lib` is the native module, or None when it isn't installed or TREELIGHTS_NATIVE=0 is
set. Every caller keeps its pure-Python implementation as the fallback, so the server
works without the native module, only slower; the tests run both.
"""

from __future__ import annotations

import os

lib = None
if os.environ.get("TREELIGHTS_NATIVE", "1") != "0":
    try:
        import treelights_native as lib  # type: ignore[no-redef]
    except ImportError:
        lib = None


def available(name: str) -> bool:
    """True when the native module is loaded and provides `name`."""
    return lib is not None and hasattr(lib, name)
