"""Diagnostics: settings, the link, the last sync and the frames (no image data)."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .runtime import Th5ConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant,
                                              entry: Th5ConfigEntry) -> dict[str, Any]:
    rt = entry.runtime_data
    link = rt.link
    return {
        "entry": dict(entry.data),
        "link": {
            "node": link.node, "session_entity": link.session_entity_id,
            "temperature_entity": link.temperature_entity_id,
            "humidity_entity": link.humidity_entity_id,
            "history_entity": link.history_entity_id,
        },
        "settings": rt.settings,
        "effective_mode": rt.mode,
        "status": rt.status.as_dict(),
        "mcu": {
            "mcu_fw": f"{rt.mcu_fw:08x}" if rt.mcu_fw is not None else None,
            "boot_ver": rt.boot_ver,
            "bundled_fw": f"{rt.fw_latest.version:08x}" if rt.fw_latest else None,
            "queued_fw": f"{rt.fw_queued.version:08x}" if rt.fw_queued else None,
            "queued_attempts": rt.fw_attempts,
        },
        "display_up_to_date": rt.display_up_to_date,
        "frames": {
            **{f"forecast_{u}": f"{f.frame_id:016x}" for u, f in rt.forecast_frames.items()},
            "picture": f"{rt.picture_frame.frame_id:016x}" if rt.picture_frame else None,
        },
        "forecast_updated": rt.forecast_updated.isoformat() if rt.forecast_updated else None,
        "forecast_problem": rt.forecast_problem,
        "picture_options": rt.picture_options,
        "history": {"stored_hours": len(rt.history_inbox), "problem": rt.history_problem},
    }
