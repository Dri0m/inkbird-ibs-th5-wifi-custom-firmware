"""A full mocked sync: the Session sensor changes, the esphome.* actions follow."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ha_fixtures import SESSION, FakeTh5, fail_with, stall, th5_module

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# The device gives HA 3 s for the first call and 3 s after each answer.
FIRST_CALL_WITHIN = 0.5
GAP_WITHIN = 0.5


async def _sync(hass: HomeAssistant, th5: FakeTh5, **session) -> None:
    th5.calls.clear()
    th5.session(**session)
    await hass.async_block_till_done(wait_background_tasks=True)


def _state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


async def test_first_sync_sends_config_frame_done(hass: HomeAssistant, th5: FakeTh5,
                                                  entry: MockConfigEntry) -> None:
    rt = entry.runtime_data
    await _sync(hass, th5)
    assert th5.actions == ["th5_config"] + ["th5_frame"] * 7 + ["th5_done"]

    # timing: answered at once, no gap the device would time out on
    times = [t for _, _, t in th5.calls]
    assert times[0] - th5.triggered_at < FIRST_CALL_WITHIN
    assert max(b - a for a, b in zip(times, times[1:], strict=False)) < GAP_WITHIN

    config = json.loads(th5.calls[0][1]["json"])
    assert config["mode"] == "forecast" and config["sync_min"] == 60
    assert config["maintenance"] is False and abs(config["utc"] - hass.loop.time()) > 0

    frame, session = th5_module("frame"), th5_module("session")
    frames = [d for a, d, _ in th5.calls if a == "th5_frame"]
    assert [d["offset"] for d in frames] == [i * session.CHUNK for i in range(7)]
    slot = b"".join(base64.b64decode(d["data"]) for d in frames)
    hdr, _ = frame.parse_slot(slot, require_commit=False)
    assert not hdr.committed and hdr.flags == frame.FRAME_FLAG_BAND
    assert hdr.frame_id == rt.forecast_frames["C"].frame_id

    assert rt.status.result == "frame_sent"
    assert _state(hass, "sensor.inkbird_th5_sync_result") == "frame_sent"
    assert _state(hass, "sensor.inkbird_th5_last_sync") != "unknown"
    assert _state(hass, "sensor.inkbird_th5_next_sync") != "unknown"
    # sent, but not yet confirmed by the device
    assert _state(hass, "binary_sensor.inkbird_th5_display_up_to_date") == "off"


async def test_known_frame_is_not_sent_again(hass: HomeAssistant, th5: FakeTh5,
                                             entry: MockConfigEntry) -> None:
    fid = entry.runtime_data.forecast_frames["C"].frame_id
    await _sync(hass, th5, frame_id=fid)
    assert th5.actions == ["th5_config", "th5_done"]
    assert _state(hass, "sensor.inkbird_th5_sync_result") == "ok"
    assert _state(hass, "binary_sensor.inkbird_th5_display_up_to_date") == "on"


async def test_fahrenheit_device_gets_the_f_frame(hass: HomeAssistant, th5: FakeTh5,
                                                  entry: MockConfigEntry) -> None:
    rt = entry.runtime_data
    assert rt.forecast_frames["C"].frame_id != rt.forecast_frames["F"].frame_id
    await _sync(hass, th5, units="F")
    slot = b"".join(base64.b64decode(d["data"]) for a, d, _ in th5.calls if a == "th5_frame")
    assert slot == rt.forecast_frames["F"].slot


async def test_failed_config_still_ends_with_done(hass: HomeAssistant, th5: FakeTh5,
                                                  entry: MockConfigEntry) -> None:
    th5.behaviour["th5_config"] = fail_with("TIME refused: BAD_VALUE, detail 0")
    await _sync(hass, th5)
    assert th5.actions == ["th5_config", "th5_done"]
    state = hass.states.get("sensor.inkbird_th5_sync_result")
    assert state.state == "failed"
    assert "BAD_VALUE" in state.attributes["error"]


async def test_stalled_chunk_times_out_and_ends(hass: HomeAssistant, th5: FakeTh5,
                                                entry: MockConfigEntry) -> None:
    th5.behaviour["th5_frame"] = stall(30)
    with patch.object(th5_module("runtime"), "CALL_TIMEOUT", 0.2):
        await _sync(hass, th5)
    assert th5.actions == ["th5_config", "th5_frame", "th5_done"]
    assert "no answer" in entry.runtime_data.status.error


async def test_session_budget_caps_the_whole_sync(hass: HomeAssistant, th5: FakeTh5,
                                                  entry: MockConfigEntry) -> None:
    th5.behaviour["th5_frame"] = stall(0.1)
    with patch.object(th5_module("runtime"), "SESSION_BUDGET", 0.25):
        await _sync(hass, th5)
    # a few chunks fit the budget, then th5_done still goes out (DONE_GRACE)
    assert th5.actions[0] == "th5_config" and th5.actions[-1] == "th5_done"
    assert 1 <= th5.actions.count("th5_frame") < 7
    assert entry.runtime_data.status.result == "failed"


@pytest.mark.parametrize("value", ["unknown", STATE_UNAVAILABLE, "garbage"])
async def test_ignored_session_values(hass: HomeAssistant, th5: FakeTh5,
                                      entry: MockConfigEntry, value: str) -> None:
    th5.calls.clear()
    hass.states.async_set(SESSION, value)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.calls == []


async def test_same_nonce_and_attribute_change_ignored(hass: HomeAssistant, th5: FakeTh5,
                                                       entry: MockConfigEntry) -> None:
    await _sync(hass, th5, nonce="00c0ffee")
    assert th5.calls
    value = hass.states.get(SESSION).state
    th5.calls.clear()
    hass.states.async_set(SESSION, value, {"icon": "mdi:x"})          # attributes only
    hass.states.async_set(SESSION, STATE_UNAVAILABLE)
    hass.states.async_set(SESSION, value)                               # same nonce again
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.calls == []


async def test_settings_reach_the_next_sync(hass: HomeAssistant, th5: FakeTh5,
                                            entry: MockConfigEntry) -> None:
    await hass.services.async_call("number", "set_value", {
        "entity_id": "number.inkbird_th5_sync_interval", "value": 15}, blocking=True)
    await hass.services.async_call("number", "set_value", {
        "entity_id": "number.inkbird_th5_temperature_offset", "value": -0.3}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {
        "entity_id": "switch.inkbird_th5_24_hour_clock"}, blocking=True)
    assert th5.calls == []                         # nothing is pushed while it sleeps
    await _sync(hass, th5)
    config = json.loads(th5.calls[0][1]["json"])
    assert (config["sync_min"], config["t_off"], config["clock_24h"]) == (15, -30, False)


async def test_band_options_wait_for_upgrade_and_survive_maintenance(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.inkbird_th5_clock_text", "option": "monochrome"}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": ["switch.inkbird_th5_single_reset_greyscale_band"]}, blocking=True)
    for version in (None, 0x00020001):
        await _sync(hass, th5, mcu_fw=version)
        config = json.loads(th5.calls[0][1]["json"])
        assert "monochrome" not in config and "single_reset" not in config
        assert entry.runtime_data.settings["monochrome"] is True
        assert entry.runtime_data.settings["single_reset"] is True
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": "switch.inkbird_th5_maintenance"}, blocking=True)
    await _sync(hass, th5, mcu_fw=0x00020002)
    config = json.loads(th5.calls[0][1]["json"])
    assert all(config[k] is True for k in ("monochrome", "single_reset", "maintenance"))

    th5.calls.clear()
    await hass.services.async_call("switch", "turn_off", {
        "entity_id": "switch.inkbird_th5_maintenance"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    config = json.loads(th5.calls[0][1]["json"])
    assert config["maintenance"] is False
    assert all(config[k] is True for k in ("monochrome", "single_reset"))


async def test_maintenance_hold_and_early_end(hass: HomeAssistant, th5: FakeTh5,
                                              entry: MockConfigEntry) -> None:
    rt = entry.runtime_data
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": "switch.inkbird_th5_maintenance"}, blocking=True)
    await _sync(hass, th5)
    assert json.loads(th5.calls[0][1]["json"])["maintenance"] is True
    assert rt.held

    th5.calls.clear()
    await hass.services.async_call("switch", "turn_off", {
        "entity_id": "switch.inkbird_th5_maintenance"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.actions == ["th5_config", "th5_done"]
    config = json.loads(th5.calls[0][1]["json"])
    assert config["maintenance"] is False and config["badge"] is False
    assert not rt.held


async def test_maintenance_clears_itself_after_the_hold(hass: HomeAssistant, th5: FakeTh5,
                                                        entry: MockConfigEntry) -> None:
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    HOLD = th5_module("const").HOLD
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": "switch.inkbird_th5_maintenance"}, blocking=True)
    await _sync(hass, th5)
    th5.calls.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + HOLD)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, "switch.inkbird_th5_maintenance") == "off"
    assert th5.calls == []                          # the HOLD ran out on the device by itself


async def test_new_session_cancels_a_stuck_one(hass: HomeAssistant, th5: FakeTh5,
                                               entry: MockConfigEntry) -> None:
    th5.behaviour["th5_frame"] = stall(30)
    th5.calls.clear()
    th5.session()
    await hass.async_block_till_done()
    del th5.behaviour["th5_frame"]
    fid = entry.runtime_data.forecast_frames["C"].frame_id
    th5.session(frame_id=fid)                       # the device rebooted mid-session
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.actions[-2:] == ["th5_config", "th5_done"]
    assert entry.runtime_data.status.result == "ok"


async def test_status_survives_reload(hass: HomeAssistant, th5: FakeTh5,
                                      entry: MockConfigEntry) -> None:
    await _sync(hass, th5)
    last = _state(hass, "sensor.inkbird_th5_last_sync")
    await hass.services.async_call("number", "set_value", {
        "entity_id": "number.inkbird_th5_sync_interval", "value": 30}, blocking=True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _state(hass, "sensor.inkbird_th5_last_sync") == last
    assert _state(hass, "number.inkbird_th5_sync_interval") == "30"
    # the session already handled before the reload is not run again
    th5.calls.clear()
    hass.states.async_set(SESSION, STATE_UNAVAILABLE)
    hass.states.async_set(SESSION, entry.runtime_data.status.session_value)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.calls == []


async def test_repeated_nonce_after_a_reboot_still_syncs(hass: HomeAssistant, th5: FakeTh5,
                                                         entry: MockConfigEntry) -> None:
    await _sync(hass, th5, nonce="00c0ffee")
    assert th5.calls
    # random_uint32() gave the same nonce on the next cold boot; the boot
    # counter (5th field) differs, so this is a new session.
    await _sync(hass, th5, nonce="00c0ffee")
    assert th5.actions[0] == "th5_config"


async def test_failed_session_resent_after_reconnect_is_retried(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    th5.behaviour["th5_config"] = fail_with("API connection lost")
    await _sync(hass, th5)
    assert entry.runtime_data.status.result == "failed"
    del th5.behaviour["th5_config"]
    value = hass.states.get(SESSION).state
    th5.calls.clear()
    hass.states.async_set(SESSION, STATE_UNAVAILABLE)                  # API dropped
    hass.states.async_set(SESSION, value)                               # resent on resubscribe
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.actions[0] == "th5_config"
    assert entry.runtime_data.status.result != "failed"


async def test_renamed_session_entity_keeps_the_trigger(hass: HomeAssistant, th5: FakeTh5,
                                                        entry: MockConfigEntry) -> None:
    from homeassistant.helpers import entity_registry as er

    new_id = "sensor.th5_session_renamed"
    er.async_get(hass).async_update_entity(SESSION, new_entity_id=new_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.link.session_entity_id == new_id
    hass.states.async_set(new_id, hass.states.get(SESSION).state)   # the entity, re-added
    th5.calls.clear()
    hass.states.async_set(new_id, "0badcafe;C;0000000000000000;1a2b3c4d;00000063")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.actions[0] == "th5_config"


async def test_disabled_session_entity_is_not_ready(hass: HomeAssistant, th5: FakeTh5,
                                                    entry: MockConfigEntry) -> None:
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import entity_registry as er

    er.async_get(hass).async_update_entity(
        SESSION, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_time_zone_change_reaches_the_next_sync(hass: HomeAssistant, th5: FakeTh5,
                                                      entry: MockConfigEntry) -> None:
    await hass.config.async_update(time_zone="Asia/Kolkata")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert str(entry.runtime_data.tz) == "Asia/Kolkata"
    await _sync(hass, th5)
    config = json.loads(th5.calls[0][1]["json"])
    assert (config["offset_min"], config["next_change_utc"]) == (330, 0)
