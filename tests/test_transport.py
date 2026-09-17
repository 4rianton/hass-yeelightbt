"""Regression tests for real failure and recovery paths."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from conftest import FakeClient

from custom_components.yeelight_bt import yeelightbt as protocol
from custom_components.yeelight_bt.candela import CCCD_UUID

pytestmark = pytest.mark.asyncio


async def test_proxy_pairs_and_requires_real_state(device, connect_peer):
    peer = FakeClient()
    connect_peer(peer)
    lamp = protocol.Lamp(device)
    await lamp.connect()
    assert lamp.available
    assert lamp.is_on and lamp.brightness == 40
    assert peer.notify_characteristic.get_descriptor(CCCD_UUID).handle == 34
    assert [bits[1] for bits, _ in peer.writes] == [0x67, 0x44]
    assert all(response for _, response in peer.writes)
    peer.brightness = 65
    peer.emit_state()  # Physical twist: no HA command is needed for this update.
    assert lamp.brightness == 65
    await lamp.close()
    assert not lamp.available


@pytest.mark.parametrize("failure", ["pair_timeout", "pair_rejected", "state_timeout"])
async def test_failed_setup_releases_proxy_slot(device, connect_peer, failure):
    peer = FakeClient()
    if failure == "pair_timeout":
        peer.pair_result = None
    elif failure == "pair_rejected":
        peer.pair_result = 3
    else:
        peer.reply_to_state = False
    connector = connect_peer(peer)
    lamp = protocol.Lamp(device)
    with pytest.raises((TimeoutError, BleakError)):
        await lamp.connect()
    assert not lamp.available
    peer.disconnect.assert_awaited_once()
    assert lamp._client is None
    with pytest.raises(BleakError, match="paused"):
        await lamp.connect()
    assert connector.await_count == 1


async def test_missing_characteristic_disconnects(device, connect_peer):
    peer = FakeClient()
    peer.services.characteristics.pop(30)
    connect_peer(peer)
    lamp = protocol.Lamp(device)
    with pytest.raises(BleakError, match="missing"):
        await lamp.connect()
    peer.disconnect.assert_awaited_once()


async def test_concurrent_operations_connect_once(device, connect_peer):
    peer = FakeClient()
    connector = connect_peer(peer)
    lamp = protocol.Lamp(device)
    await asyncio.gather(
        lamp.get_state(), lamp.set_brightness(15), lamp.set_brightness(80)
    )
    assert connector.await_count == 1
    assert peer.max_in_flight == 1
    assert lamp.brightness == 80
    commands = [bits[1] for bits, _ in peer.writes]
    assert commands.count(protocol.CMD_PAIR) == 1
    await lamp.close()


async def test_write_failure_reconnects_using_fresh_device(device, connect_peer):
    first, second = FakeClient(), FakeClient()
    first.fail_command = protocol.CMD_BRIGHTNESS
    connector = connect_peer(first, second)
    newer = BLEDevice(device.address, device.name, {"source": "replacement proxy"})
    resolve = Mock(side_effect=[device, newer])
    lamp = protocol.Lamp(device, device_callback=resolve)
    await lamp.set_brightness(75)
    assert connector.await_args_list[1].args[1] is newer
    first.disconnect.assert_awaited_once()
    assert lamp.available and lamp.brightness == 75
    # Late callbacks and notifications from the old connection are ignored.
    first.drop()
    first.brightness = 1
    first.emit_state()
    assert lamp.available and lamp.brightness == 75
    await lamp.close()


async def test_failed_command_keeps_confirmed_state_and_raises(device, connect_peer):
    first, second = FakeClient(), FakeClient()
    first.fail_command = second.fail_command = protocol.CMD_BRIGHTNESS
    connect_peer(first, second)
    lamp = protocol.Lamp(device)
    with pytest.raises(BleakError):
        await lamp.set_brightness(90)
    assert lamp.brightness == 40
    assert not lamp.available
    first.disconnect.assert_awaited_once()
    second.disconnect.assert_awaited_once()


async def test_drop_during_pair_wakes_waiter(device, connect_peer):
    peer = FakeClient()
    peer.pair_result = None
    connect_peer(peer)
    lamp = protocol.Lamp(device)
    task = asyncio.create_task(lamp.connect())
    await peer.write_entered.wait()
    await asyncio.sleep(0)
    peer.drop()
    with pytest.raises(BleakError, match="dropped"):
        await task
    assert not lamp.available
    peer.disconnect.assert_awaited_once()


async def test_unload_cancels_io_and_prevents_queued_reconnect(device, connect_peer):
    peer = FakeClient()
    peer.block_write = asyncio.Event()
    connector = connect_peer(peer)
    lamp = protocol.Lamp(device)
    first = asyncio.create_task(lamp.connect())
    await peer.write_entered.wait()
    queued = asyncio.create_task(lamp.get_state())
    await lamp.close()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(BleakError, match="shutting down"):
        await queued
    assert not lamp.available
    assert connector.await_count == 1
    peer.disconnect.assert_awaited_once()


async def test_hung_gatt_write_is_bounded(device, connect_peer):
    peer = FakeClient()
    peer.block_write = asyncio.Event()
    connect_peer(peer)
    lamp = protocol.Lamp(device)
    with pytest.raises(BleakError, match="Timed out writing"):
        await lamp.connect()
    peer.disconnect.assert_awaited_once()


async def test_recovery_after_backoff(device, connect_peer):
    failed, recovered = FakeClient(), FakeClient()
    failed.pair_result = 3
    connect_peer(failed, recovered)
    lamp = protocol.Lamp(device)
    with pytest.raises(BleakError):
        await lamp.connect()
    lamp._retry_at = 0  # Advance past the cooldown without sleeping in the test.
    await lamp.get_state()
    assert lamp.available
    assert lamp._failures == 0
    await lamp.close()


async def test_bedside_still_pairs_and_decodes_state(device, connect_peer):
    peer = FakeClient(bedside=True)
    connect_peer(peer)
    lamp = protocol.Lamp(device, model=protocol.MODEL_BEDSIDE)
    await lamp.connect()
    assert lamp.available
    assert lamp.temperature == 4080
    assert lamp.mode == lamp.MODE_WHITE
    assert lamp.color == (12, 34, 56)
    peer.read_gatt_descriptor.assert_not_awaited()
    await lamp.close()


@pytest.mark.parametrize(
    "data", [b"", b"C", bytes(18), bytes(19), b"CE\x01\xff" + bytes(14)]
)
async def test_invalid_notifications_do_not_change_state(device, data):
    lamp = protocol.Lamp(device)
    lamp.notification_handler(None, bytearray(data))
    assert lamp.brightness == 0
    assert not lamp._state_event.is_set()


async def test_unnamed_advertisement_is_safe():
    assert protocol.model_from_name(None) == protocol.MODEL_UNKNOWN


async def test_client_is_owned_before_connection_returns(device, monkeypatch):
    # Constructing the wrapper itself must not open a real Bluetooth backend.
    monkeypatch.setattr(
        protocol.bleak_retry_connector.BleakClientWithServiceCache,
        "__init__",
        lambda *args, **kwargs: None,
    )
    lamp = protocol.Lamp(device)
    client = protocol._owned_client_type(lamp)(device)
    assert lamp._client is client


async def test_cancel_during_connection_releases_client(
    device, monkeypatch, fast_protocol
):
    peer = FakeClient()
    started = asyncio.Event()

    def initialize(client, *args, **kwargs):
        client._backend = SimpleNamespace(disconnect=peer.disconnect)

    monkeypatch.setattr(
        protocol.bleak_retry_connector.BleakClientWithServiceCache,
        "__init__",
        initialize,
    )

    async def connect_in_progress(client_class, device, name, **kwargs):
        client_class(device)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(protocol, "establish_connection", connect_in_progress)
    lamp = protocol.Lamp(device)
    pending = asyncio.create_task(lamp.connect())
    await started.wait()
    await lamp.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    peer.disconnect.assert_awaited_once()
    assert lamp._client is None


async def test_client_class_resolved_after_ha_bluetooth_setup(device, monkeypatch):
    class HAClient:
        pass

    lamp = protocol.Lamp(device)
    monkeypatch.setattr(
        protocol.bleak_retry_connector, "BleakClientWithServiceCache", HAClient
    )
    assert issubclass(protocol._owned_client_type(lamp), HAClient)
