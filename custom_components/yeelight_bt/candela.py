"""Compatibility with the Candela's legacy notification attribute layout."""

from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak_esphome.backend.client import ESPHomeClient

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


async def notification_characteristic(
    client: BleakClient, characteristic: BleakGATTCharacteristic
) -> BleakGATTCharacteristic:
    """Correct the Candela CCCD for ESPHome without modifying cached services.

    The legacy implementation (v0.11.3, enable_notifications) writes 01 00
    to notification_handle + 1. Candela firmware can instead advertise a
    CCCD at +2, whose value is the user description b"NOTIFY\\0". ESPHome
    also encounters this layout with the CCCD absent from discovery.

    Only ESPHome uses the descriptor from the supplied characteristic when
    subscribing. BlueZ resolves descriptors internally, so this correction
    must not be applied there. _backend is used solely for this type check;
    all I/O goes through Bleak's public APIs.
    """
    if not isinstance(client._backend, ESPHomeClient):
        return characteristic

    descriptor = characteristic.get_descriptor(CCCD_UUID)
    handle = characteristic.handle + 1
    if descriptor is not None:
        if descriptor.handle != handle + 1:
            return characteristic
        value = await client.read_gatt_descriptor(descriptor)
        if bytes(value).rstrip(b"\0") != b"NOTIFY":
            return characteristic

    # Never overwrite a discovered characteristic or unrelated descriptor.
    if client.services.get_characteristic(handle) is not None:
        raise BleakError("Candela notification configuration handle is occupied")
    existing = client.services.get_descriptor(handle)
    if existing is not None and existing.uuid != CCCD_UUID:
        raise BleakError("Candela notification configuration descriptor is ambiguous")

    service = client.services.get_service(characteristic.service_uuid)
    if service is None:
        raise BleakError("Candela notification service is missing")
    corrected = BleakGATTCharacteristic(
        characteristic.obj,
        characteristic.handle,
        characteristic.uuid,
        characteristic.properties,
        lambda: characteristic.max_write_without_response_size,
        service,
    )
    corrected.add_descriptor(BleakGATTDescriptor(None, handle, CCCD_UUID, corrected))
    return corrected
