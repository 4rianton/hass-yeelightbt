"""Subscribe to the Candela's replies when its GATT table has no CCCD."""

import asyncio
import logging
from collections.abc import Callable

from aioesphomeapi import APIConnectionError, BluetoothProxyFeature
from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_esphome.backend.client import ESPHomeClient

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
USER_DESCRIPTION_UUID = "00002901-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "8f65073d-9f57-4aaa-afea-397d19d5bbeb"
_LOGGER = logging.getLogger(__name__)


async def start_notifications(
    client: BleakClient,
    characteristic: BleakGATTCharacteristic,
    callback: Callable[[BleakGATTCharacteristic, bytearray], None],
    *,
    timeout: float,
    trace: Callable[[str], None],
) -> str:
    """Keep standard subscriptions intact; recognize the observed Candela layout.

    The user's lamp has value handle 34, a user description at 35 containing
    NOTIFY, and no CCCD. Never invent a descriptor or write into that description.
    ESPHome's v3 API can register the listener separately from a CCCD write.
    Registration alone does not establish that the lamp will send replies.
    """
    backend = client._backend
    if not isinstance(backend, ESPHomeClient) or characteristic.get_descriptor(
        CCCD_UUID
    ):
        trace("notification mode=standard")
        await client.start_notify(characteristic, callback)
        return "standard"

    description = characteristic.get_descriptor(USER_DESCRIPTION_UUID)
    if (
        characteristic.uuid != NOTIFY_UUID
        or "notify" not in characteristic.properties
        or description is None
        or description.characteristic_handle != characteristic.handle
        or description.handle != characteristic.handle + 1
    ):
        raise BleakError(
            "Unsupported Candela notification layout: no CCCD or NOTIFY description"
        )
    value = bytes(await client.read_gatt_descriptor(description))
    trace(
        f"descriptor {description.handle}:{description.uuid} value={value[:32].hex()}"
    )
    if value.rstrip(b"\0") != b"NOTIFY":
        raise BleakError("Unsupported Candela notification description")
    if not backend._feature_flags & BluetoothProxyFeature.REMOTE_CACHING.value:
        raise BleakError(
            "Candela notification listener requires ESPHome v3 connections"
        )

    trace("notification mode=esphome-listener-without-cccd")
    await _start_proxy_listener(client, backend, characteristic, callback, timeout)
    trace("ESPHome acknowledged notification listener")
    return "esphome-listener-without-cccd"


async def _start_proxy_listener(
    client: BleakClient,
    backend: ESPHomeClient,
    characteristic: BleakGATTCharacteristic,
    callback: Callable[[BleakGATTCharacteristic, bytearray], None],
    timeout: float,
) -> None:
    """Use HA's existing API connection and the backend's notification cleanup.

    The private accesses are isolated here: backend API client, address, and
    cancellation registry. This matches bleak-esphome 4.0.0's start/stop_notify
    ownership, omitting only its CCCD write. No second proxy connection is opened.
    """
    handle = characteristic.handle
    if not client.is_connected:
        raise BleakError("Candela disconnected before notification registration")
    if handle in backend._notify_cancels:
        raise BleakError(
            f"Candela notifications already registered for handle {handle}"
        )
    api = backend._client
    address = backend._address_as_int
    active = True

    def receive(received_handle: int, data: bytearray) -> None:
        if active and received_handle == handle and client.is_connected:
            callback(characteristic, data)

    # aioesphomeapi 46.2.0 removes its in-progress callback on Exception, but
    # not CancelledError. Let its own bounded timeout finish registration,
    # then clean up before returning ownership to a reconnect. An abandoned
    # registration could otherwise overwrite the next connection's callback.
    pending = asyncio.create_task(
        api.bluetooth_gatt_start_notify(address, handle, receive, timeout),
        name=f"Candela notification registration {address}:{handle}",
    )

    def discard_registration(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            _, remove = task.result()
        except Exception:
            _LOGGER.debug(
                "Candela: Abandoned notification registration failed", exc_info=True
            )
            return  # The API removed its callback on this failed registration.
        remove()
        try:
            api.bluetooth_gatt_stop_notify(address, handle)
        except APIConnectionError:
            _LOGGER.debug(
                "Candela: Proxy disconnected during notification cleanup", exc_info=True
            )

    try:
        stop, remove = await asyncio.shield(pending)
        if not client.is_connected:
            raise BleakError("Candela disconnected during notification registration")

        async def stop_listener() -> None:
            nonlocal active
            active = False
            await stop()

        def remove_listener() -> None:
            nonlocal active
            active = False
            remove()

        backend._notify_cancels[handle] = (stop_listener, remove_listener)
    except BaseException as err:
        active = False
        # Drain the API's bounded request before the caller disconnects or
        # reconnects. Keep shielding it if unload cancellation arrives again.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                _LOGGER.debug(
                    "Candela: Notification registration ended during cleanup",
                    exc_info=True,
                )
                break
        discard_registration(pending)
        if isinstance(err, APIConnectionError):
            raise BleakError(
                f"ESPHome notification registration failed: {err}"
            ) from err
        raise
