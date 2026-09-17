"""Exercise the descriptor correction through the real ESPHome backend."""

from unittest.mock import AsyncMock, Mock

import pytest
from aioesphomeapi import BluetoothProxyFeature
from bleak import BleakError
from conftest import FakeClient, make_services

from custom_components.yeelight_bt.candela import CCCD_UUID, notification_characteristic
from custom_components.yeelight_bt.yeelightbt import NOTIFY_UUID

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("cccd", [None, 35])
async def test_legacy_layout_writes_correct_handle_through_esphome(cccd):
    peer = FakeClient()
    peer.services = make_services(cccd)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    corrected = await notification_characteristic(peer, original)
    assert corrected.handle == 33
    assert corrected.get_descriptor(CCCD_UUID).handle == 34
    assert (
        original.get_descriptor(CCCD_UUID) is None
        if cccd is None
        else original.get_descriptor(CCCD_UUID).handle == 35
    )

    # Real bleak-esphome 4.0.0 code, with only the network API mocked.
    backend = peer._backend
    backend._is_connected = True
    backend._description = "test proxy"
    backend._address_as_int = 123
    backend._notify_cancels = {}
    backend._feature_flags = BluetoothProxyFeature.REMOTE_CACHING.value
    backend._client = Mock()
    backend._client.bluetooth_gatt_start_notify = AsyncMock(
        return_value=(AsyncMock(), Mock())
    )
    backend._client.bluetooth_gatt_write_descriptor = AsyncMock()
    received = Mock()
    await backend.start_notify(corrected, received)
    args = backend._client.bluetooth_gatt_write_descriptor.await_args.args
    assert args[:3] == (123, 34, b"\x01\x00")
    notification_callback = backend._client.bluetooth_gatt_start_notify.await_args.args[
        2
    ]
    notification_callback(33, b"lamp state")
    received.assert_called_once_with(b"lamp state")
    await backend.stop_notify(corrected)
    assert not backend._notify_cancels


async def test_standard_descriptor_is_not_changed():
    peer = FakeClient()
    peer.services = make_services(34)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    assert await notification_characteristic(peer, original) is original
    peer.read_gatt_descriptor.assert_not_awaited()


async def test_unrecognized_descriptor_content_is_not_changed():
    peer = FakeClient()
    peer.read_gatt_descriptor.return_value = b"\0\0"
    original = peer.services.get_characteristic(NOTIFY_UUID)
    assert await notification_characteristic(peer, original) is original


async def test_local_backend_is_not_patched():
    peer = FakeClient(bedside=True)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    assert await notification_characteristic(peer, original) is original
    peer.read_gatt_descriptor.assert_not_awaited()


async def test_conflicting_handle_is_rejected():
    peer = FakeClient()
    original = peer.services.get_characteristic(NOTIFY_UUID)
    peer.services.characteristics[34] = Mock()
    with pytest.raises(BleakError, match="occupied"):
        await notification_characteristic(peer, original)
