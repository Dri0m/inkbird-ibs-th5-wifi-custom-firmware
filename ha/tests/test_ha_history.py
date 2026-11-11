"""History is saved before acknowledgement and imported idempotently into HA."""
from __future__ import annotations

import base64
import datetime as dt
import json
import struct
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ha_fixtures import HISTORY, MAC, FakeTh5, th5_module

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "history_entity")


@pytest.fixture
def mock_recorder_before_hass(request) -> None:
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_db_url")


@pytest.fixture
def history_entity(hass: HomeAssistant, th5: FakeTh5) -> None:
    er.async_get(hass).async_get_or_create(
        "sensor", "esphome", f"{MAC.upper()}/0/text_sensor/History",
        config_entry=th5.esphome_entry, device_id=th5.device_id,
        original_name="History", suggested_object_id=HISTORY.split(".")[1])
    hass.states.async_set(HISTORY, "unknown")


def _history(mean: int = -123) -> tuple[str, int]:
    hour = int(dt_util.utcnow().timestamp()) // 3600 * 3600 - 3600
    data = b"\x01\x00" + struct.pack("<IhhhHHHB", hour, mean, -456, 123, 5000, 4500, 6000, 60)
    return base64.b64encode(data).decode(), hour


def _bound(th5: FakeTh5, value: str, nonce: str | None = None) -> str:
    return f"{nonce or f'{0xC0FFEE00 + th5.n + 1:08x}'}/{th5.n + 1:08x};{value}"


async def _sync(hass: HomeAssistant, th5: FakeTh5, value: str | None,
                version: int | None = 0x00020003, *, bind: bool = True,
                nonce: str | None = None) -> dict:
    th5.calls.clear()
    if value is not None:
        hass.states.async_set(HISTORY, _bound(th5, value, nonce) if bind else value)
    th5.session(mcu_fw=version, nonce=nonce)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert th5.calls[0][0] == "th5_config" and th5.calls[-1][0] == "th5_done"
    return json.loads(th5.calls[0][1]["json"])


def _inbox(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    return Path(hass.config.path(".storage", "th5_display", entry.entry_id, "history.json"))


async def test_history_saved_and_queued_before_config(hass: HomeAssistant, th5: FakeTh5,
                                                     entry: MockConfigEntry) -> None:
    value, hour = _history()
    assert entry.runtime_data.link.history_entity_id == HISTORY
    saved = []

    def queue(hass, metadata, rows):
        assert not th5.calls  # both imports precede the first config
        assert _inbox(hass, entry).is_file()
        saved.append((metadata, rows))

    with patch.object(th5_module("statistics"), "async_add_external_statistics", side_effect=queue):
        config = await _sync(hass, th5, value)
    assert config["history_ack"] is True
    assert entry.runtime_data.status.result == "frame_sent"
    assert json.loads(await hass.async_add_executor_job(_inbox(hass, entry).read_text)) == [
        [hour, -123, -456, 123, 5000, 4500, 6000, 60]]
    assert len(saved) == 2
    temp, humidity = saved
    assert temp[0]["statistic_id"] == f"th5_display:{entry.entry_id.lower()}_temperature"
    assert temp[0]["source"] == "th5_display" and temp[0]["unit_class"] == "temperature"
    assert temp[0]["mean_type"].name == "ARITHMETIC" and temp[0]["has_sum"] is False
    assert temp[1] == [{"start": dt.datetime.fromtimestamp(hour, dt.UTC),
                        "mean": -1.23, "min": -4.56, "max": 1.23, "mean_weight": 60}]
    assert humidity[0]["unit_of_measurement"] == "%" and humidity[0]["unit_class"] is None
    assert humidity[1][0]["mean"] == 50.0


async def test_history_snapshot_is_captured_at_session_event(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    value, _ = _history()
    with patch.object(th5_module("statistics"), "async_add_external_statistics"):
        async with entry.runtime_data._link_lock:
            hass.states.async_set(HISTORY, _bound(th5, value))
            th5.session(mcu_fw=0x00020003)
            await hass.async_block_till_done()
            hass.states.async_set(HISTORY, "a later sensor state")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert json.loads(th5.calls[0][1]["json"])["history_ack"] is True
    assert len(entry.runtime_data.history_inbox) == 1


async def test_history_entity_can_appear_after_a_cbu_upgrade(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    from dataclasses import replace

    # The integration was loaded before the upgraded CBU registered History.
    entry.runtime_data.link = replace(entry.runtime_data.link, history_entity_id=None)
    with patch.object(th5_module("statistics"), "async_add_external_statistics"):
        config = await _sync(hass, th5, _history()[0])
    assert config["history_ack"] is True
    assert entry.runtime_data.link.history_entity_id == HISTORY


@pytest.mark.parametrize("prefix", ["deadbeef/00000001;", "c0ffee01/00000000;",
                                    "c0ffee01;", "", "c0ffee01/00000001;extra;"])
async def test_unbound_or_wrong_session_history_is_not_saved_or_acknowledged(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry, prefix: str) -> None:
    with (patch.object(th5_module("runtime"), "_write_history_inbox") as save,
          patch.object(th5_module("statistics"), "async_add_external_statistics") as queue):
        config = await _sync(hass, th5, prefix + _history()[0], bind=False)
    assert "history_ack" not in config
    assert entry.runtime_data.history_inbox == []
    assert entry.runtime_data.status.result == "frame_sent"
    save.assert_not_called()
    queue.assert_not_called()


async def test_repeated_nonce_with_new_boot_rejects_previous_snapshot(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    nonce = "7d150e72"  # CBU cold boots can repeat their random nonce
    value, _ = _history()
    stale = _bound(th5, value, nonce)
    with patch.object(th5_module("statistics"), "async_add_external_statistics"):
        config = await _sync(hass, th5, value, nonce=nonce)
        assert config["history_ack"] is True
        before = list(entry.runtime_data.history_inbox)
        with patch.object(th5_module("runtime"), "_write_history_inbox") as save:
            config = await _sync(hass, th5, stale, bind=False, nonce=nonce)
    assert "history_ack" not in config
    assert entry.runtime_data.history_inbox == before
    save.assert_not_called()


@pytest.mark.parametrize("value", ["garbage", "unknown", "", "AAE=", "AgA="])
async def test_malformed_history_keeps_normal_sync(hass: HomeAssistant, th5: FakeTh5,
                                                  entry: MockConfigEntry, value: str) -> None:
    with patch.object(th5_module("statistics"), "async_add_external_statistics") as queue:
        config = await _sync(hass, th5, value)
    assert "history_ack" not in config
    assert entry.runtime_data.status.result == "frame_sent"
    queue.assert_not_called()


@pytest.mark.parametrize("version", [None, 0x00020002])
async def test_old_mcu_never_gets_history_ack(hass: HomeAssistant, th5: FakeTh5,
                                           entry: MockConfigEntry, version: int | None) -> None:
    value, _ = _history()
    with patch.object(th5_module("statistics"), "async_add_external_statistics") as queue:
        config = await _sync(hass, th5, value, version)
    assert "history_ack" not in config
    queue.assert_not_called()


async def test_failed_save_never_acknowledges(hass: HomeAssistant, th5: FakeTh5,
                                             entry: MockConfigEntry) -> None:
    with (patch.object(th5_module("runtime"), "_write_history_inbox", side_effect=OSError("disk full")),
          patch.object(th5_module("statistics"), "async_add_external_statistics") as queue):
        config = await _sync(hass, th5, _history()[0])
    assert "history_ack" not in config and entry.runtime_data.history_inbox == []
    assert "disk full" in entry.runtime_data.history_problem
    assert entry.runtime_data.status.result == "frame_sent"
    queue.assert_not_called()


async def test_failed_recorder_keeps_durable_inbox_for_restart(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    with patch.object(th5_module("statistics"), "async_add_external_statistics",
                      side_effect=HomeAssistantError("recorder unavailable")):
        config = await _sync(hass, th5, _history()[0])
    assert "history_ack" not in config
    assert "recorder unavailable" in entry.runtime_data.history_problem
    before = list(entry.runtime_data.history_inbox)
    assert len(before) == 1
    with patch.object(th5_module("statistics"), "async_add_external_statistics") as queue:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert queue.call_count == 2
        assert entry.runtime_data.history_inbox == before
        config = await _sync(hass, th5, _history()[0])
    assert config["history_ack"] is True
    assert entry.runtime_data.history_inbox == before  # replay never duplicates an hour
    assert _inbox(hass, entry).is_file()  # queue success retains durable retry copy


async def test_recorder_absent_keeps_sync_working(hass: HomeAssistant, th5: FakeTh5,
                                                 entry: MockConfigEntry) -> None:
    config = await _sync(hass, th5, _history()[0])
    assert "history_ack" not in config and len(entry.runtime_data.history_inbox) == 1
    assert entry.runtime_data.status.result == "frame_sent"


async def test_real_recorder_replaces_duplicate_hour(recorder_mock, hass: HomeAssistant,
                                                    th5: FakeTh5, entry: MockConfigEntry) -> None:
    from homeassistant.components.recorder.statistics import statistics_during_period

    value, hour = _history()
    config = await _sync(hass, th5, value)
    assert config["history_ack"] is True
    await recorder_mock.async_block_till_done()
    config = await _sync(hass, th5, _history(mean=0)[0])
    assert config["history_ack"] is True
    await recorder_mock.async_block_till_done()
    ids = {f"th5_display:{entry.entry_id.lower()}_{kind}" for kind in ("temperature", "humidity")}
    result = await recorder_mock.async_add_executor_job(partial(
        statistics_during_period, hass, dt.datetime.fromtimestamp(hour, dt.UTC), None,
        ids, "hour", None, {"mean", "min", "max"}))
    temp = result[f"th5_display:{entry.entry_id.lower()}_temperature"]
    humidity = result[f"th5_display:{entry.entry_id.lower()}_humidity"]
    assert len(temp) == len(humidity) == 1
    assert temp[0]["mean"] == 0.0 and temp[0]["min"] == -4.56 and temp[0]["max"] == 1.23
    assert humidity[0]["mean"] == 50.0
