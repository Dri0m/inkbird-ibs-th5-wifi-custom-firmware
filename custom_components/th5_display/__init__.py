"""TH5 Display: forecast and picture frames for a de-clouded Inkbird IBS-TH5-WIFI.

The integration glue lives in entry.py. The renderer modules (forecast,
frame, picture, preview, session, sources, timeinfo) are plain Python and are
imported without Home Assistant by ha/tools, ha/tests and tools/th5link, so
the glue is only loaded when Home Assistant is installed.
"""
from __future__ import annotations

from importlib.util import find_spec

if find_spec("homeassistant") is not None:
    from .entry import (  # noqa: F401
        CONFIG_SCHEMA,
        async_remove_entry,
        async_setup,
        async_setup_entry,
        async_unload_entry,
    )
