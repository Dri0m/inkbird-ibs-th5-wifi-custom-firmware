"""Entities on the ESPHome device, the preview, actions, storage."""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.media_source import PlayMedia
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ha_fixtures import MAC, FakeTh5, th5_module

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ENTITIES = {
    "select.inkbird_th5_display_mode": "forecast",
    "select.inkbird_th5_clock_text": "greyscale",
    "number.inkbird_th5_sync_interval": "60",
    "number.inkbird_th5_clock_update": "1",
    "number.inkbird_th5_full_refresh_every": "30",
    "number.inkbird_th5_temperature_offset": "0.0",
    "number.inkbird_th5_humidity_offset": "0.0",
    "switch.inkbird_th5_24_hour_clock": "on",
    "switch.inkbird_th5_maintenance": "off",
    "switch.inkbird_th5_picture_top_bar": "off",
    "switch.inkbird_th5_single_reset_greyscale_band": "off",
    "button.inkbird_th5_redraw_forecast": "unknown",
    "sensor.inkbird_th5_last_sync": "unknown",
    "sensor.inkbird_th5_next_sync": "unknown",
    "sensor.inkbird_th5_sync_result": "unknown",
    "binary_sensor.inkbird_th5_display_up_to_date": "unknown",
    "update.inkbird_th5_mcu_firmware": "unknown",
}
PREVIEW = "image.inkbird_th5_display_preview"


def _png(color: tuple[int, int, int] = (40, 40, 40), size: tuple[int, int] = (640, 480)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


async def _preview(hass: HomeAssistant) -> Image.Image:
    entity = hass.data["entity_components"]["image"].get_entity(PREVIEW)
    return Image.open(io.BytesIO(await entity.async_image())).convert("L")


async def test_entities_live_on_the_esphome_device(hass: HomeAssistant, th5: FakeTh5,
                                                   entry: MockConfigEntry) -> None:
    for entity_id, state in ENTITIES.items():
        assert hass.states.get(entity_id).state == state, entity_id
    ent_reg = er.async_get(hass)
    ours = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert len(ours) == len(ENTITIES) + 2           # + preview, + frame id
    assert {e.device_id for e in ours} == {th5.device_id}
    # no second device for the same TH5
    assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) == []
    frame_id = ent_reg.async_get("sensor.inkbird_th5_frame_id")
    assert frame_id.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(PREVIEW) is not None


async def test_picture_mode_needs_a_picture(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("select", "select_option", {
            "entity_id": "select.inkbird_th5_display_mode", "option": "picture"}, blocking=True)


async def test_preview_draws_the_band_on_the_forecast(hass: HomeAssistant, th5: FakeTh5,
                                                      entry: MockConfigEntry) -> None:
    img = await _preview(hass)
    assert img.size == (832, 480)
    band = img.crop((0, 0, 832, 112))
    assert min(band.tobytes()) == 0                 # clock and reading drawn
    before = hass.states.get(PREVIEW).state
    hass.states.async_set("sensor.inkbird_th5_temperature", "22.0",
                          {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert hass.states.get(PREVIEW).state != before   # image_last_updated moved


async def test_band_controls_preview_and_reload(hass: HomeAssistant, th5: FakeTh5,
                                               entry: MockConfigEntry) -> None:
    select = "select.inkbird_th5_clock_text"
    switch = "switch.inkbird_th5_single_reset_greyscale_band"
    registry = er.async_get(hass)
    for entity_id in (select, switch):
        assert registry.async_get(entity_id).entity_category.value == "config"
    grey = (await _preview(hass)).crop((0, 0, 832, 112))
    assert {85, 170} <= set(grey.tobytes())
    await hass.services.async_call("select", "select_option", {
        "entity_id": select, "option": "monochrome"}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {
        "entity_id": switch}, blocking=True)
    assert th5.calls == []  # controls wait for the next device sync
    mono = (await _preview(hass)).crop((0, 0, 832, 112))
    assert set(mono.tobytes()) == {0, 255}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(select).state == "monochrome"
    assert hass.states.get(switch).state == "on"
    assert entry.runtime_data.settings["monochrome"] is True
    assert entry.runtime_data.settings["single_reset"] is True

    # A disabled experiment and the default text choice also survive reload.
    await hass.services.async_call("select", "select_option", {
        "entity_id": select, "option": "greyscale"}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(select).state == "greyscale"
    assert hass.states.get(switch).state == "off"


async def test_show_image_local_file(hass: HomeAssistant, th5: FakeTh5,
                                     entry: MockConfigEntry, tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "kids.png").write_bytes(_png((85, 85, 85)))     # exactly dark grey
    hass.config.allowlist_external_dirs = {str(media)}
    resolved = PlayMedia(url="/media/local/kids.png", mime_type="image/png",
                         path=media / "kids.png")
    with patch.object(th5_module("services"), "async_resolve_media",
                      AsyncMock(return_value=resolved)):
        await hass.services.async_call("th5_display", "show_image", {
            "device_id": th5.device_id, "fit": "cover", "dither": "atkinson",
            "media": {"media_content_id": "media-source://media_source/local/kids.png",
                      "media_content_type": "image/png"}}, blocking=True)
    await hass.async_block_till_done()
    rt = entry.runtime_data
    assert hass.states.get("select.inkbird_th5_display_mode").state == "picture"
    assert rt.picture_options == {"fit": "cover", "dither": "atkinson"}
    hdr, _ = th5_module("frame").parse_slot(rt.picture_frame.slot, require_commit=False)
    assert hdr.flags == 0

    # the same frame for both units, and no band in the preview
    assert rt.current_frame("C") is rt.current_frame("F") is rt.picture_frame
    img = await _preview(hass)
    assert set(img.tobytes()) == {85}               # no band drawn over it

    th5.calls.clear()
    th5.session(units="F")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert json.loads(th5.calls[0][1]["json"])["mode"] == "picture"
    assert th5.actions.count("th5_frame") == 7

    # stored: a reload keeps the picture and the mode
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.picture_frame == rt.picture_frame
    assert hass.states.get("select.inkbird_th5_display_mode").state == "picture"
    # back to the forecast
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.inkbird_th5_display_mode", "option": "forecast"}, blocking=True)
    assert entry.runtime_data.mode == "forecast"


async def _show(hass: HomeAssistant, th5: FakeTh5, tmp_path: Path, png: bytes,
                fit: str = "contain") -> None:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    (media / "pic.png").write_bytes(png)
    hass.config.allowlist_external_dirs = {str(media)}
    resolved = PlayMedia(url="/media/local/pic.png", mime_type="image/png", path=media / "pic.png")
    with patch.object(th5_module("services"), "async_resolve_media",
                      AsyncMock(return_value=resolved)):
        await hass.services.async_call("th5_display", "show_image", {
            "device_id": th5.device_id, "fit": fit, "dither": "none",
            "media": {"media_content_id": "media-source://media_source/local/pic.png",
                      "media_content_type": "image/png"}}, blocking=True)
    await hass.async_block_till_done()


async def test_picture_top_bar(hass: HomeAssistant, th5: FakeTh5,
                               entry: MockConfigEntry, tmp_path: Path) -> None:
    switch = "switch.inkbird_th5_picture_top_bar"
    assert er.async_get(hass).async_get(switch).entity_category.value == "config"
    await _show(hass, th5, tmp_path, _png((85, 85, 85), (520, 230)), "cover")
    rt = entry.runtime_data
    parse = th5_module("frame").parse_slot
    full = rt.picture_frame
    assert parse(full.slot, require_commit=False)[0].flags == 0

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    assert th5.calls == []                          # waits for the next sync
    bar = rt.picture_frame
    assert bar != full and rt.current_frame("F") is bar
    assert parse(bar.slot, require_commit=False)[0].flags == th5_module("frame").FRAME_FLAG_BAND
    img = await _preview(hass)                      # band approximated, picture below it
    assert min(img.crop((0, 0, 832, 112)).tobytes()) == 0
    assert set(img.crop((0, 114, 832, 118)).tobytes()) == {0}     # the rule, rows 57..58
    assert set(img.crop((0, 118, 832, 480)).tobytes()) == {85}    # the picture from row 59

    th5.session()
    await hass.async_block_till_done(wait_background_tasks=True)
    config = json.loads(th5.calls[0][1]["json"])
    assert config["mode"] == "forecast" and config["badge"] is False   # MCU draws the band
    assert th5.actions.count("th5_frame") == 7
    assert hass.states.get("select.inkbird_th5_display_mode").state == "picture"

    # both layouts are stored; a reload keeps the switch and the frame
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(switch).state == "on"
    assert entry.runtime_data.picture_frame == bar
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert entry.runtime_data.picture_frame == full
    th5.calls.clear()
    th5.session(frame_id=full.frame_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert json.loads(th5.calls[0][1]["json"])["mode"] == "picture"


async def test_badge_store_and_entity_migrate(hass: HomeAssistant, hass_storage: dict,
                                              th5: FakeTh5, entry: MockConfigEntry,
                                              tmp_path: Path) -> None:
    """A <= 0.2.5 install: "badge" in the settings, picture.bin only, the old switch."""
    await _show(hass, th5, tmp_path, _png((0, 0, 0)))
    rt = entry.runtime_data
    full = rt.picture_frame
    folder = rt._dir
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    key = f"th5_display.{entry.entry_id}"
    hass_storage[key]["data"]["settings"]["badge"] = True
    hass_storage[key]["data"]["settings"].pop("top_bar")
    (folder / "picture-top.bin").unlink()
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create("switch", "th5_display", f"{MAC}-badge",
                                      config_entry=entry, device_id=th5.device_id,
                                      suggested_object_id="inkbird_th5_picture_badge")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    rt = entry.runtime_data
    # badge on carries over as the top bar, which now shows the icons
    assert "badge" not in rt.settings and rt.settings["top_bar"] is True
    assert rt.picture_frame == rt.picture_frames[True] != full
    assert ent_reg.async_get(old.entity_id) is None
    assert hass.states.get("switch.inkbird_th5_picture_badge") is None
    assert (folder / "picture-top.bin").exists()   # re-rendered from source.png
    assert rt.picture_frames[True] != full


async def test_show_image_refuses_paths_outside_the_allowlist(
        hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry, tmp_path: Path) -> None:
    (tmp_path / "secret.png").write_bytes(_png())
    hass.config.allowlist_external_dirs = {str(tmp_path / "media")}
    resolved = PlayMedia(url="/x", mime_type="image/png", path=tmp_path / "secret.png")
    with (patch.object(th5_module("services"), "async_resolve_media",
                       AsyncMock(return_value=resolved)),
          pytest.raises(ServiceValidationError)):
        await hass.services.async_call("th5_display", "show_image", {
            "media": {"media_content_id": "media-source://media_source/local/secret.png",
                      "media_content_type": "image/png"}}, blocking=True)
    assert entry.runtime_data.picture_frame is None


async def test_show_image_camera_snapshot(hass: HomeAssistant, th5: FakeTh5,
                                         entry: MockConfigEntry) -> None:
    snapshot = types.SimpleNamespace(content=_png((250, 250, 250)), content_type="image/png")
    fake_camera = types.ModuleType("homeassistant.components.camera")
    fake_camera.async_get_image = AsyncMock(return_value=snapshot)
    with patch.dict(sys.modules, {"homeassistant.components.camera": fake_camera}):
        await hass.services.async_call("th5_display", "show_image", {
            "media": {"media_content_id": "media-source://camera/camera.garden",
                      "media_content_type": "image/jpeg"}}, blocking=True)
    fake_camera.async_get_image.assert_awaited_once_with(hass, "camera.garden")
    assert entry.runtime_data.mode == "picture"


async def test_bad_image_is_reported(hass: HomeAssistant, th5: FakeTh5,
                                     entry: MockConfigEntry) -> None:
    snapshot = types.SimpleNamespace(content=b"not an image", content_type="image/png")
    fake_camera = types.ModuleType("homeassistant.components.camera")
    fake_camera.async_get_image = AsyncMock(return_value=snapshot)
    with (patch.dict(sys.modules, {"homeassistant.components.camera": fake_camera}),
          pytest.raises(Exception, match="could not be read")):
        await hass.services.async_call("th5_display", "show_image", {
            "media": {"media_content_id": "media-source://camera/camera.garden",
                      "media_content_type": "image/jpeg"}}, blocking=True)


async def test_refresh_action_and_button(hass: HomeAssistant, th5: FakeTh5, weather,
                                         entry: MockConfigEntry) -> None:
    n = len(weather)
    await hass.services.async_call("th5_display", "refresh", {}, blocking=True)
    await hass.services.async_call("button", "press", {
        "entity_id": "button.inkbird_th5_redraw_forecast"}, blocking=True)
    assert len(weather) == n + 2
    assert weather[-1].data["type"] == "hourly"


async def test_weather_down_keeps_old_frames(hass: HomeAssistant, th5: FakeTh5,
                                             entry: MockConfigEntry) -> None:
    rt = entry.runtime_data
    frames = dict(rt.forecast_frames)
    hass.states.async_set("weather.home", "unavailable")
    with pytest.raises(Exception, match="forecast could not be updated"):
        await hass.services.async_call("button", "press", {
            "entity_id": "button.inkbird_th5_redraw_forecast"}, blocking=True)
    assert rt.forecast_frames == frames
    assert "unavailable" in rt.forecast_problem


async def test_diagnostics(hass: HomeAssistant, th5: FakeTh5, entry: MockConfigEntry) -> None:
    diag = await th5_module("diagnostics").async_get_config_entry_diagnostics(hass, entry)
    assert diag["link"]["node"] == "th5"
    assert diag["settings"]["sync_min"] == 60
    assert set(diag["frames"]) == {"forecast_C", "forecast_F", "picture"}


async def test_remove_entry_deletes_files(hass: HomeAssistant, th5: FakeTh5,
                                          entry: MockConfigEntry) -> None:
    folder = Path(hass.config.path(".storage", "th5_display", entry.entry_id))
    assert (folder / "frame-C.bin").is_file()
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not folder.exists()


async def test_setup_retries_while_esphome_is_not_ready(hass: HomeAssistant, th5: FakeTh5,
                                                        weather) -> None:
    for action in ("th5_config", "th5_frame", "th5_done"):
        hass.services.async_remove("esphome", f"th5_{action}")
    e = MockConfigEntry(domain="th5_display", unique_id="a4:c1:38:12:34:56",
                        data={"device_id": th5.device_id, "weather_entity": "weather.home",
                              "location": "Oslo"})
    e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id)
    assert e.state is ConfigEntryState.SETUP_RETRY


async def test_action_descriptions_load(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # Only this integration's services.yaml, through HA's own schema (an
    # invalid file loads as {}). async_get_all_descriptions would also load
    # every other integration's, which needs packages the harness lacks.
    from homeassistant.helpers.service import _load_services_file
    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, "th5_display")
    desc = await hass.async_add_executor_job(_load_services_file, integration)
    assert set(desc) == {"show_image", "refresh", "install_firmware"}
    assert desc["show_image"]["fields"]["media"]["required"] is True
