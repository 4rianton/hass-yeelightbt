"""Exercise the observed descriptor-free layout through real ESPHome API code."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aioesphomeapi import APIConnectionError
from aioesphomeapi.api_pb2 import BluetoothGATTNotifyRequest
from bleak import BleakError
from conftest import FakeClient, make_services

from custom_components.yeelight_bt.candela import NOTIFY_UUID, start_notifications

pytestmark = pytest.mark.asyncio


async def subscribe(peer, received):
    return await start_notifications(
        peer,
        peer.services.get_characteristic(NOTIFY_UUID),
        received,
        timeout=0.05,
        trace=Mock(),
    )


async def test_observed_layout_listens_without_any_descriptor_write(observed_peer):
    peer, api = observed_peer.peer, observed_peer.api
    original = peer.services.get_characteristic(NOTIFY_UUID)
    received = Mock()
    assert await subscribe(peer, received) == "esphome-listener-without-cccd"
    send = api._send_bluetooth_message_await_response
    send.assert_awaited_once()
    request = send.await_args.args[2]
    assert isinstance(request, BluetoothGATTNotifyRequest)
    assert request.enable and request.handle == 34 and request.address == 123
    assert len(original.descriptors) == 1
    assert original.descriptors[0].handle == 35
    peer.emit(b"real reply")
    received.assert_called_once_with(original, bytearray(b"real reply"))
    await peer._backend.stop_notify(original)
    assert not peer._backend._notify_cancels
    assert not api._notify_callbacks
    observed_peer.remove.assert_called_once()
    api.bluetooth_gatt_stop_notify.assert_called_once_with(123, 34)
    peer.emit(b"late reply")
    assert received.call_count == 1


async def test_disconnect_removes_listener_and_rejects_late_notifications(
    observed_peer,
):
    peer = observed_peer.peer
    received = Mock()
    await subscribe(peer, received)
    await peer.disconnect()
    assert not peer._backend._notify_cancels
    assert not observed_peer.api._notify_callbacks
    observed_peer.remove.assert_called_once()
    peer.emit(b"late reply")
    received.assert_not_called()


@pytest.mark.parametrize("bedside", [False, True])
async def test_standard_and_local_backends_keep_public_bleak_path(bedside):
    peer = FakeClient(bedside=bedside)
    received = Mock()
    peer.start_notify = AsyncMock()
    assert await subscribe(peer, received) == "standard"
    peer.start_notify.assert_awaited_once_with(
        peer.services.get_characteristic(NOTIFY_UUID), received
    )
    peer.read_gatt_descriptor.assert_not_awaited()


async def test_missing_descriptor_does_not_invent_a_handle():
    peer = FakeClient()
    peer.services = make_services(None)
    with pytest.raises(BleakError, match="Unsupported"):
        await subscribe(peer, Mock())
    peer.read_gatt_descriptor.assert_not_awaited()


@pytest.mark.parametrize("value", [b"\x00\x00", b"Other", b""])
async def test_unrecognized_description_is_not_reinterpreted(observed_peer, value):
    observed_peer.peer.read_gatt_descriptor.return_value = value
    with pytest.raises(BleakError, match="Unsupported"):
        await subscribe(observed_peer.peer, Mock())
    observed_peer.api._send_bluetooth_message_await_response.assert_not_awaited()


async def test_legacy_proxy_does_not_use_descriptor_free_listener(observed_peer):
    observed_peer.peer._backend._feature_flags = 0
    with pytest.raises(BleakError, match="v3"):
        await subscribe(observed_peer.peer, Mock())
    observed_peer.api._send_bluetooth_message_await_response.assert_not_awaited()


async def test_api_error_releases_callback_and_is_a_bleak_error(observed_peer):
    observed_peer.api._send_bluetooth_message_await_response.side_effect = (
        APIConnectionError("proxy offline")
    )
    with pytest.raises(BleakError, match="proxy offline"):
        await subscribe(observed_peer.peer, Mock())
    observed_peer.remove.assert_called_once()
    assert not observed_peer.api._notify_callbacks
    assert not observed_peer.peer._backend._notify_cancels


async def test_cancelled_registration_cleans_up_after_late_ack(observed_peer):
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_ack(*args):
        started.set()
        await release.wait()

    observed_peer.api._send_bluetooth_message_await_response.side_effect = delayed_ack
    received = Mock()
    waiter = asyncio.create_task(subscribe(observed_peer.peer, received))
    await started.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()  # Retain ownership until registration is cleaned up.
    observed_peer.peer.emit(b"reply after cancel")
    received.assert_not_called()
    pending = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("Candela notification registration")
    ]
    assert len(pending) == 1 and not pending[0].cancelled()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    observed_peer.remove.assert_called_once()
    assert not observed_peer.api._notify_callbacks
    assert not observed_peer.peer._backend._notify_cancels


async def test_disconnect_while_registering_does_not_install_listener(observed_peer):
    async def disconnected(*args):
        observed_peer.peer.is_connected = False

    observed_peer.api._send_bluetooth_message_await_response.side_effect = disconnected
    with pytest.raises(BleakError, match="disconnected during"):
        await subscribe(observed_peer.peer, Mock())
    observed_peer.remove.assert_called_once()
    assert not observed_peer.api._notify_callbacks
    assert not observed_peer.peer._backend._notify_cancels
