"""Exercise the descriptor correction through the real ESPHome backend."""

from unittest.mock import AsyncMock, Mock

import pytest
from aioesphomeapi import BluetoothProxyFeature
from bleak import BleakError
from bleak.backends.descriptor import BleakGATTDescriptor
from conftest import FakeClient, make_services

from custom_components.yeelight_bt.candela import CCCD_UUID, notification_characteristic
from custom_components.yeelight_bt.yeelightbt import NOTIFY_UUID

pytestmark = pytest.mark.asyncio
USER_DESCRIPTION_UUID = "00002901-0000-1000-8000-00805f9b34fb"


@pytest.mark.parametrize("cccd", [None, 35])
@pytest.mark.parametrize("mislabelled_value", [None, b"\x00\x00", b"\x01\x00"])
async def test_legacy_layout_writes_correct_handle_through_esphome(
    cccd, mislabelled_value
):
    peer = FakeClient()
    peer.services = make_services(cccd)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    if mislabelled_value is not None:
        peer.services.add_descriptor(
            BleakGATTDescriptor(None, 34, USER_DESCRIPTION_UUID, original)
        )
        peer.read_gatt_descriptor.side_effect = lambda descriptor: (
            mislabelled_value if descriptor.handle == 34 else b"NOTIFY\0"
        )
    corrected = await notification_characteristic(peer, original)
    assert corrected.handle == 33
    assert corrected.get_descriptor(CCCD_UUID).handle == 34
    assert (
        original.get_descriptor(CCCD_UUID) is None
        if cccd is None
        else original.get_descriptor(CCCD_UUID).handle == 35
    )
    if mislabelled_value is not None:
        assert original.get_descriptor(34).uuid == USER_DESCRIPTION_UUID
        assert peer.services.get_descriptor(34).uuid == USER_DESCRIPTION_UUID

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


@pytest.mark.parametrize("value", [b"NOTIFY\0", b"\x04\x00", b"\x00", b"\x00\x01"])
async def test_description_or_invalid_config_is_not_overwritten(value):
    peer = FakeClient()
    peer.services = make_services(None)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    peer.services.add_descriptor(
        BleakGATTDescriptor(None, 34, USER_DESCRIPTION_UUID, original)
    )
    peer.read_gatt_descriptor.return_value = value
    with pytest.raises(BleakError) as exc:
        await notification_characteristic(peer, original)
    message = str(exc.value)
    assert "notify handle=33" in message
    assert f"34:{USER_DESCRIPTION_UUID}" in message
    assert f"value={value.hex()}" in message
    assert original.get_descriptor(34).uuid == USER_DESCRIPTION_UUID


async def test_descriptor_owned_by_another_characteristic_is_rejected():
    peer = FakeClient()
    peer.services = make_services(None)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    control = peer.services.get_characteristic(30)
    peer.services.add_descriptor(
        BleakGATTDescriptor(None, 34, USER_DESCRIPTION_UUID, control)
    )
    with pytest.raises(BleakError, match="belongs to characteristic 30"):
        await notification_characteristic(peer, original)
    peer.read_gatt_descriptor.assert_not_awaited()


async def test_other_descriptor_types_are_not_reinterpreted():
    peer = FakeClient()
    peer.services = make_services(None)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    unrelated_uuid = "00002900-0000-1000-8000-00805f9b34fb"
    peer.services.add_descriptor(
        BleakGATTDescriptor(None, 34, unrelated_uuid, original)
    )
    with pytest.raises(BleakError, match=unrelated_uuid):
        await notification_characteristic(peer, original)
    peer.read_gatt_descriptor.assert_not_awaited()


async def test_descriptor_read_error_includes_layout():
    peer = FakeClient()
    peer.services = make_services(None)
    original = peer.services.get_characteristic(NOTIFY_UUID)
    peer.services.add_descriptor(
        BleakGATTDescriptor(None, 34, USER_DESCRIPTION_UUID, original)
    )
    peer.read_gatt_descriptor.side_effect = BleakError("Read not permitted")
    with pytest.raises(BleakError) as exc:
        await notification_characteristic(peer, original)
    message = str(exc.value)
    assert "Read not permitted" in message
    assert "notify handle=33" in message
    assert f"34:{USER_DESCRIPTION_UUID}" in message
