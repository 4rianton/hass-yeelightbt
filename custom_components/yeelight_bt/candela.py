"""Compatibility with the Candela's legacy notification attribute layout."""

import logging

from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak_esphome.backend.client import ESPHomeClient

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
USER_DESCRIPTION_UUID = "00002901-0000-1000-8000-00805f9b34fb"
_LOGGER = logging.getLogger(__name__)


def _layout(characteristic: BleakGATTCharacteristic) -> str:
    """Include discovered handles in normal warnings, not only debug logs."""
    descriptors = ", ".join(
        f"{descriptor.handle}:{descriptor.uuid}"
        for descriptor in characteristic.descriptors
    )
    return f"notify handle={characteristic.handle}, descriptors=[{descriptors}]"


async def _read_descriptor(
    client: BleakClient,
    characteristic: BleakGATTCharacteristic,
    descriptor: BleakGATTDescriptor,
) -> bytes:
    """Preserve the layout when a probe fails; the caller bounds its duration."""
    try:
        return bytes(await client.read_gatt_descriptor(descriptor))
    except (BleakError, TimeoutError, OSError) as err:
        raise BleakError(
            f"Could not inspect Candela descriptor {descriptor.handle}: {err} "
            f"({_layout(characteristic)})"
        ) from err


async def notification_characteristic(
    client: BleakClient, characteristic: BleakGATTCharacteristic
) -> BleakGATTCharacteristic:
    """Correct the Candela CCCD for ESPHome without modifying cached services.

    The legacy implementation (v0.11.3, enable_notifications) writes 01 00
    to notification_handle + 1. Candela firmware can instead advertise a
    CCCD at +2, whose value is the user description b"NOTIFY\\0". ESPHome
    also encounters this layout with the CCCD absent from discovery.
    A descriptor at +1 labelled as a user description is only considered
    a mislabelled CCCD if it belongs to this characteristic and reads as
    a two-byte configuration with no reserved bits set. This remains a
    compatibility heuristic; actual pairing and state replies are required.

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
        value = await _read_descriptor(client, characteristic, descriptor)
        if value.rstrip(b"\0") != b"NOTIFY":
            return characteristic

    # Never overwrite a discovered characteristic or unrelated descriptor.
    if client.services.get_characteristic(handle) is not None:
        raise BleakError(
            f"Candela notification configuration handle {handle} is occupied "
            f"({_layout(characteristic)})"
        )
    existing = client.services.get_descriptor(handle)
    if existing is not None:
        if existing.characteristic_handle != characteristic.handle:
            raise BleakError(
                f"Candela descriptor {handle}:{existing.uuid} belongs to "
                f"characteristic {existing.characteristic_handle} "
                f"({_layout(characteristic)})"
            )
        if existing.uuid != CCCD_UUID:
            if existing.uuid != USER_DESCRIPTION_UUID:
                raise BleakError(
                    f"Unsupported Candela descriptor {handle}:{existing.uuid} "
                    f"({_layout(characteristic)})"
                )
            value = await _read_descriptor(client, characteristic, existing)
            if len(value) != 2 or int.from_bytes(value, "little") & ~0x0003:
                raise BleakError(
                    f"Candela notification configuration descriptor is missing: "
                    f"{handle}:{existing.uuid} has value={value[:32].hex()} "
                    f"({_layout(characteristic)})"
                )
            _LOGGER.debug(
                "Candela: Trying descriptor %s as a mislabelled notification "
                "configuration (uuid=%s, value=%s)",
                handle,
                existing.uuid,
                value.hex(),
            )

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
    _LOGGER.debug(
        "Candela: Using notification configuration handle %s (%s)",
        handle,
        _layout(characteristic),
    )
    return corrected
