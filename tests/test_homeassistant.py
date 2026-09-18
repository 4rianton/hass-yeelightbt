"""Use real Home Assistant 2026.9 entities and config entry APIs."""

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest import FakeClient
from homeassistant.components.light import ColorMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components import yeelight_bt as integration
from custom_components.yeelight_bt.config_flow import Yeelight_btConfigFlow
from custom_components.yeelight_bt.const import DOMAIN, VERSION
from custom_components.yeelight_bt.light import YeelightBT
from custom_components.yeelight_bt.yeelightbt import (
    CMD_BRIGHTNESS,
    CMD_POWER,
    MODEL_BEDSIDE,
)

pytestmark = pytest.mark.asyncio


def make_platform(hass):
    return EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="light",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    yield instance
    await instance.async_stop(force=True)


async def test_candela_state_is_valid_in_homeassistant(hass, device, connect_peer):
    peer = FakeClient()
    connect_peer(peer)
    entity = YeelightBT("Candela", device)
    entity.add_to_platform_start(hass, make_platform(hass), None)
    entity.entity_id = "light.candela"
    await entity.add_to_platform_finish()
    await entity.async_update()
    state = hass.states.get(entity.entity_id)
    assert state.state == "on"
    assert state.attributes["color_mode"] == ColorMode.BRIGHTNESS
    assert state.attributes["supported_color_modes"] == [ColorMode.BRIGHTNESS]
    assert state.attributes["brightness"] == 102
    assert state.attributes["supported_features"] == 0
    peer.brightness = 70
    peer.emit_state()
    assert hass.states.get(entity.entity_id).attributes["brightness"] == 178
    await entity.async_will_remove_from_hass()


async def test_homekit_brightness_burst_uses_latest_request(hass, device, connect_peer):
    peer = FakeClient()
    connect_peer(peer)
    entity = YeelightBT("Candela", device)
    entity.add_to_platform_start(hass, make_platform(hass), None)
    entity.entity_id = "light.candela"
    await entity.add_to_platform_finish()

    await entity._command_lock.acquire()
    old = asyncio.create_task(entity.async_turn_on(brightness=70 * 255 // 100))
    latest = asyncio.create_task(entity.async_turn_on(brightness=85 * 255 // 100))
    await asyncio.sleep(0)
    entity._command_lock.release()
    await asyncio.gather(old, latest)

    brightness_writes = [
        bits[2] for bits, _ in peer.writes if bits[1] == CMD_BRIGHTNESS
    ]
    assert brightness_writes == [85]
    assert entity.brightness == round(255 * 85 / 100)
    await entity.async_will_remove_from_hass()


async def test_service_failure_is_not_reported_as_success(hass, device, connect_peer):
    first, second = FakeClient(), FakeClient()
    first.fail_command = second.fail_command = CMD_BRIGHTNESS
    connect_peer(first, second)
    entity = YeelightBT("Candela", device)
    entity.add_to_platform_start(hass, make_platform(hass), None)
    entity.entity_id = "light.candela"
    await entity.add_to_platform_finish()
    with pytest.raises(HomeAssistantError, match="Could not control"):
        await entity.async_turn_on(brightness=200)
    assert entity.brightness == 102
    assert hass.states.get(entity.entity_id).state == "unavailable"
    await entity.async_will_remove_from_hass()


async def test_failed_attempt_logs_diagnostics_at_warning_level(
    device, connect_peer, observed_peer, caplog
):
    peer = observed_peer.peer
    peer.pair_result = None
    connector = connect_peer(peer)
    entity = YeelightBT("Candela", device)
    with caplog.at_level(logging.WARNING):
        await entity.async_update()
        report = caplog.records[-1].getMessage()
        assert "Could not update lamp" in report
        assert "Yeelight diagnostics: version=1.4.4" in report
        assert "phase=pairing" in report
        assert "notifications=0" in report
        assert "diagnostic read handle=34" in report
        before = len(caplog.records)
        await entity.async_update()  # Cooldown must not repeat the warning.
        assert len(caplog.records) == before
        assert connector.await_count == 1
        # A new actual failure must still report its new reason and stage.
        entity._dev._retry_at = 0
        replacement = FakeClient()
        replacement.pair_result = 3
        connect_peer(replacement)
        await entity.async_update()
        assert len(caplog.records) == before + 1
        assert "rejected" in caplog.records[-1].getMessage()
    await entity._dev.close()


async def test_diagnostic_version_matches_manifest():
    manifest = Path(integration.__file__).with_name("manifest.json")
    assert json.loads(manifest.read_text())["version"] == VERSION


async def test_low_brightness_is_not_rounded_to_off(device, connect_peer):
    peer = FakeClient()
    connect_peer(peer)
    entity = YeelightBT("Candela", device)
    await entity.async_turn_on(brightness=1)
    assert peer.brightness == 1
    assert entity.brightness == 3
    await entity._dev.close()


async def test_zero_brightness_turns_off(device, connect_peer):
    peer = FakeClient()
    connect_peer(peer)
    entity = YeelightBT("Candela", device)
    await entity.async_turn_on(brightness=0)
    assert not entity.is_on
    assert not any(bits[1] == CMD_BRIGHTNESS for bits, _ in peer.writes)
    await entity._dev.close()


async def test_bedside_color_mode_and_kelvin(hass, device, connect_peer):
    peer = FakeClient(bedside=True)
    connect_peer(peer)
    entity = YeelightBT("Bedside", device, model=MODEL_BEDSIDE)
    entity.add_to_platform_start(hass, make_platform(hass), None)
    entity.entity_id = "light.bedside"
    await entity.add_to_platform_finish()
    await entity.async_update()
    assert entity.color_mode == ColorMode.COLOR_TEMP
    assert entity.color_temp_kelvin == 2740
    await entity.async_turn_on(hs_color=(45, 60), brightness=255)
    assert entity.brightness == 255
    assert entity.color_mode == ColorMode.HS
    await entity.async_will_remove_from_hass()


async def test_turn_on_sequence_cannot_interleave_with_turn_off(device, connect_peer):
    peer = FakeClient()
    peer.on = False
    connect_peer(peer)
    entity = YeelightBT("Candela", device)
    await asyncio.gather(entity.async_turn_on(brightness=128), entity.async_turn_off())
    commands = [
        (bits[1], bits[2])
        for bits, _ in peer.writes
        if bits[1] in (CMD_POWER, CMD_BRIGHTNESS)
    ]
    assert commands == [(CMD_POWER, 1), (CMD_BRIGHTNESS, 50), (CMD_POWER, 2)]
    assert not entity.is_on
    await entity._dev.close()


async def test_discovery_uses_proxy_history_only(hass):
    flow = Yeelight_btConfigFlow()
    flow.hass = hass
    infos = [
        SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="yeelight_ms"),
        SimpleNamespace(address="AA:BB:CC:DD:EE:02", name=None),
        SimpleNamespace(address="AA:BB:CC:DD:EE:03", name="XMCTD_test"),
    ]
    with (
        patch(
            "custom_components.yeelight_bt.config_flow.async_discovered_service_info",
            return_value=infos,
        ) as discovered,
        patch.object(flow, "_async_current_ids", return_value={"aa:bb:cc:dd:ee:03"}),
        patch.object(flow, "async_step_device", new_callable=AsyncMock),
    ):
        await flow.async_step_scan({})
    discovered.assert_called_once_with(hass, connectable=True)
    assert flow.devices == ["AA:BB:CC:DD:EE:01 (Candela)"]


async def test_unload_cleans_only_its_own_entry():
    hass = Mock()
    hass.data = {DOMAIN: {"one": object(), "two": object()}}
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    assert await integration.async_unload_entry(hass, SimpleNamespace(entry_id="one"))
    assert set(hass.data[DOMAIN]) == {"two"}
    assert await integration.async_unload_entry(hass, SimpleNamespace(entry_id="two"))
    assert DOMAIN not in hass.data
