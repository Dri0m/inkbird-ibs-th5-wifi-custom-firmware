"""Shared fixtures. The test_ha_* modules need Home Assistant's test harness:

    uv run --python 3.14 --with pytest-homeassistant-custom-component==0.13.373 \
        pytest ha/tests

Without it (`uv run --with pytest --with pillow pytest ha/tests`) they are
skipped and only the pure renderer and session tests run.
"""
from __future__ import annotations

from importlib.util import find_spec

if find_spec("homeassistant") is None or find_spec("pytest_homeassistant_custom_component") is None:
    collect_ignore_glob = ["test_ha_*.py"]
else:
    from ha_fixtures import *  # noqa: E402, F403

    def pytest_configure(config):
        # The harness's async fixtures need pytest-asyncio's auto mode; set here
        # rather than in an ini file, which the pure run would warn about.
        config.option.asyncio_mode = "auto"
