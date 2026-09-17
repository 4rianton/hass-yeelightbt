"""Yeelight Bluetooth protocol.

Creator: hcoohb. License: MIT.
Source: https://github.com/hcoohb/hass-yeelightbt
"""

from __future__ import annotations

import asyncio
import enum
import logging
import struct
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import bleak_retry_connector
from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .candela import notification_characteristic

NOTIFY_UUID = "8f65073d-9f57-4aaa-afea-397d19d5bbeb"
CONTROL_UUID = "aa7d3f34-2d4f-41e0-807f-52fbf8cf7443"
COMMAND_STX = 0x43
CMD_PAIR = 0x67
CMD_PAIR_ON = 0x02
RES_PAIR = 0x63
CMD_POWER = 0x40
CMD_POWER_ON = 0x01
CMD_POWER_OFF = 0x02
CMD_COLOR = CMD_RGB = 0x41
CMD_BRIGHTNESS = 0x42
CMD_TEMP = 0x43
CMD_GETSTATE = 0x44
CMD_GETSTATE_SEC = 0x02
RES_GETSTATE = 0x45
CMD_GETNAME = 0x52
RES_GETNAME = 0x53
CMD_GETVER = 0x5C
RES_GETVER = 0x5D
CMD_GETSERIAL = 0x5E
RES_GETSERIAL = 0x5F
RES_GETTIME = 0x62
MODEL_BEDSIDE = "Bedside"
MODEL_CANDELA = "Candela"
MODEL_UNKNOWN = "Unknown"

CONNECT_TIMEOUT = 30.0
GATT_TIMEOUT = 10.0
PAIR_TIMEOUT = 15.0
STATE_TIMEOUT = 5.0
DISCONNECT_TIMEOUT = 5.0
COMMAND_SETTLE_TIME = 0.5
TRANSITION_SETTLE_TIME = 0.7
RETRY_DELAY = 0.5
BACKOFF_BASE = 30.0
BACKOFF_MAX = 300.0
TRANSPORT_ERRORS = (BleakError, TimeoutError, OSError)
_LOGGER = logging.getLogger(__name__)


class Conn(enum.Enum):
    DISCONNECTED = 1
    UNPAIRED = 2
    PAIRING = 3
    PAIRED = 4


def model_from_name(ble_name: str | None) -> str:
    """Identify a supported lamp, including advertisements with no name."""
    name = ble_name or ""
    if name.startswith("XMCTD_"):
        return MODEL_BEDSIDE
    if name.startswith("yeelight_ms"):
        return MODEL_CANDELA
    return MODEL_UNKNOWN


def _owned_client_type(owner: Lamp) -> type[BleakClient]:
    """Use HA's current client wrapper and own it before connecting.

    HA replaces the connector's client class during Bluetooth setup. Resolving
    it here also works if a config flow imported this module before setup.
    """

    class OwnedClient(bleak_retry_connector.BleakClientWithServiceCache):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            owner._client = self

    return OwnedClient


class Lamp:
    """Own one connection and serialize all operations for a lamp."""

    MODE_COLOR = 0x01
    MODE_WHITE = 0x02
    MODE_FLOW = 0x03

    def __init__(
        self,
        ble_device: BLEDevice,
        device_callback: Callable[[], BLEDevice | None] | None = None,
        model: str | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._device_callback = device_callback
        self._mac = ble_device.address
        self._model = model or model_from_name(ble_device.name)
        self._client: BleakClient | None = None
        self._control: BleakGATTCharacteristic | None = None
        self._write_response = True
        self._conn = Conn.DISCONNECTED
        self._operation_lock = asyncio.Lock()
        self._active_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._pair_resp_event = asyncio.Event()
        self._state_event = asyncio.Event()
        self._has_state = False
        self._failures = 0
        self._retry_at = 0.0
        self._is_on = False
        self._rgb = (0, 0, 0)
        self._brightness = 0
        self._temperature = 0
        self._mode: int | None = None
        self.versions: tuple[int, ...] | None = None
        self.serial: int | None = None
        self._state_callbacks: list[Callable[[], None]] = []

    def add_callback_on_state_changed(
        self, func: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe until the returned function is called."""
        self._state_callbacks.append(func)
        return lambda: self._state_callbacks.remove(func)

    def run_state_changed_cb(self) -> None:
        for func in tuple(self._state_callbacks):
            try:
                func()
            except Exception:
                _LOGGER.exception("%s: State callback failed", self._mac)

    def _disconnected(self, client: BleakClient) -> None:
        if client is not self._client:
            return
        _LOGGER.debug("%s: Bluetooth disconnected", self._mac)
        self._conn = Conn.DISCONNECTED
        self._has_state = False
        # Wake pending requests; they check connection state before returning.
        self._pair_resp_event.set()
        self._state_event.set()
        self.run_state_changed_cb()

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        async with self._operation_lock:
            if self._closed:
                raise BleakError("Yeelight connection is shutting down")
            remaining = self._retry_at - asyncio.get_running_loop().time()
            if remaining > 0:
                raise BleakError(
                    f"Yeelight reconnect paused for {remaining:.0f}s after a failure"
                )
            self._active_task = asyncio.current_task()
            try:
                yield
            except asyncio.CancelledError:
                await self._disconnect()
                raise
            except Exception:
                await self._disconnect()
                self._failures = min(self._failures + 1, 5)
                self._retry_at = asyncio.get_running_loop().time() + min(
                    BACKOFF_MAX, BACKOFF_BASE * 2 ** (self._failures - 1)
                )
                raise
            else:
                self._failures = 0
                self._retry_at = 0.0
            finally:
                self._active_task = None

    async def connect(self) -> None:
        """Connect, subscribe, pair, and confirm the lamp's actual state."""
        async with self._operation():
            await self._connect()

    async def _connect(self) -> None:
        if self.available:
            return
        await self._disconnect()
        if self._device_callback is not None:
            device = self._device_callback()
            if device is None:
                raise BleakError(f"No connectable Bluetooth route to {self._mac}")
            self._ble_device = device
        if self._model == MODEL_UNKNOWN:
            self._model = model_from_name(self._ble_device.name)
        if self._model == MODEL_UNKNOWN:
            raise BleakError(f"Unknown Yeelight model for {self._mac}")

        _LOGGER.debug("%s: Connecting to %s", self._mac, self._model)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                client = await establish_connection(
                    _owned_client_type(self),
                    self._ble_device,
                    self._mac,
                    disconnected_callback=self._disconnected,
                    max_attempts=2,
                    timeout=10.0,
                )
        except TimeoutError as err:
            raise BleakError(
                "Timed out establishing the Yeelight BLE connection"
            ) from err
        self._client = client
        self._conn = Conn.UNPAIRED
        self._has_state = False
        self._control = client.services.get_characteristic(CONTROL_UUID)
        notify = client.services.get_characteristic(NOTIFY_UUID)
        if self._control is None or notify is None:
            raise BleakError(
                "Yeelight control or notification characteristic is missing"
            )
        # Be explicit: Bleak's default changed in 0.21. Respect GATT properties.
        self._write_response = "write" in self._control.properties
        if (
            not self._write_response
            and "write-without-response" not in self._control.properties
        ):
            raise BleakError("Yeelight control characteristic is not writable")

        try:
            async with asyncio.timeout(GATT_TIMEOUT):
                if self._model == MODEL_CANDELA:
                    notify = await notification_characteristic(client, notify)
                await client.start_notify(
                    notify,
                    lambda sender, data: self._notification_from(client, sender, data),
                )
        except TimeoutError as err:
            raise BleakError("Timed out subscribing to Yeelight notifications") from err
        await self._pair()
        await self._request_state()
        _LOGGER.debug("%s: Paired and state confirmed", self._mac)

    async def _pair(self) -> None:
        self._pair_resp_event.clear()
        self._conn = Conn.PAIRING
        await self._write(struct.pack("BBB15x", COMMAND_STX, CMD_PAIR, CMD_PAIR_ON))
        try:
            async with asyncio.timeout(PAIR_TIMEOUT):
                await self._pair_resp_event.wait()
        except TimeoutError as err:
            raise BleakError(
                "Timed out waiting for Yeelight pairing confirmation"
            ) from err
        if (
            self._conn != Conn.PAIRED
            or not self._client
            or not self._client.is_connected
        ):
            raise BleakError("Yeelight pairing was rejected or the connection dropped")

    async def _write(self, bits: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected or self._control is None:
            raise BleakError("Yeelight is disconnected")
        _LOGGER.debug("%s: Sending %s", self._mac, bits.hex())
        try:
            async with asyncio.timeout(GATT_TIMEOUT):
                await client.write_gatt_char(
                    self._control, bits, response=self._write_response
                )
        except TimeoutError as err:
            raise BleakError(
                f"Timed out writing Yeelight command 0x{bits[1]:02x}"
            ) from err

    async def _request_state(self) -> None:
        self._state_event.clear()
        await self._write(
            struct.pack("BBB15x", COMMAND_STX, CMD_GETSTATE, CMD_GETSTATE_SEC)
        )
        try:
            async with asyncio.timeout(STATE_TIMEOUT):
                await self._state_event.wait()
        except TimeoutError as err:
            raise BleakError(
                "Timed out waiting for a Yeelight state notification"
            ) from err
        if not self.available:
            raise BleakError("Yeelight disconnected before returning its state")

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._control = None
        changed = self._conn != Conn.DISCONNECTED or self._has_state
        self._conn = Conn.DISCONNECTED
        self._has_state = False
        self._pair_resp_event.set()
        self._state_event.set()
        if changed:
            self.run_state_changed_cb()
        if client is not None:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()
            except TRANSPORT_ERRORS:
                _LOGGER.debug("%s: Disconnect failed", self._mac, exc_info=True)

    async def disconnect(self) -> None:
        """Disconnect explicitly without preventing future reconnects."""
        async with self._operation_lock:
            await self._disconnect()

    async def close(self) -> None:
        """Cancel pending I/O, release the proxy slot, and prevent reconnects."""
        self._closed = True
        if (
            self._active_task is not None
            and self._active_task is not asyncio.current_task()
        ):
            self._active_task.cancel()
        async with self._operation_lock:
            await self._disconnect()

    @property
    def mac(self) -> str:
        return self._mac

    @property
    def available(self) -> bool:
        return bool(
            self._client
            and self._client.is_connected
            and self._conn == Conn.PAIRED
            and self._has_state
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def mode(self) -> int | None:
        return self._mode

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def temperature(self) -> int:
        return self._temperature

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def color(self) -> tuple[int, int, int]:
        return self._rgb

    def get_prop_min_max(self) -> dict[str, Any]:
        return {
            "brightness": {"min": 0, "max": 100},
            "temperature": {"min": 1700, "max": 6500},
            "color": {"min": 0, "max": 255},
        }

    async def send_cmd(
        self, bits: bytes, wait_notif: float = COMMAND_SETTLE_TIME
    ) -> bool:
        """Send an absolute command, retry one failed write, then confirm state.

        Connection establishment has its own bounded retries. Retrying a command
        requires a new client; no failed client or service object is reused.
        """
        async with self._operation():
            for attempt in range(2):
                await self._connect()
                try:
                    if bits[1] == CMD_GETSTATE:
                        await self._request_state()
                    else:
                        await self._write(bits)
                        await asyncio.sleep(wait_notif)
                        await self._request_state()
                    return True
                except TRANSPORT_ERRORS:
                    await self._disconnect()
                    if attempt:
                        raise
                    _LOGGER.debug("%s: Retrying command after reconnect", self._mac)
                    await asyncio.sleep(RETRY_DELAY)
        return False  # Both attempts return or raise.

    async def get_state(self) -> None:
        await self.send_cmd(
            struct.pack("BBB15x", COMMAND_STX, CMD_GETSTATE, CMD_GETSTATE_SEC)
        )

    async def turn_on(self) -> None:
        await self.send_cmd(struct.pack("BBB15x", COMMAND_STX, CMD_POWER, CMD_POWER_ON))

    async def turn_off(self) -> None:
        await self.send_cmd(
            struct.pack("BBB15x", COMMAND_STX, CMD_POWER, CMD_POWER_OFF)
        )

    async def set_brightness(self, brightness: int) -> None:
        brightness = min(100, max(0, int(brightness)))
        await self.send_cmd(
            struct.pack("BBB15x", COMMAND_STX, CMD_BRIGHTNESS, brightness),
            wait_notif=TRANSITION_SETTLE_TIME,
        )

    async def set_temperature(self, kelvin: int, brightness: int | None = None) -> None:
        brightness = self._brightness if brightness is None else brightness
        await self.send_cmd(
            struct.pack(
                ">BBhB13x",
                COMMAND_STX,
                CMD_TEMP,
                min(6500, max(1700, int(kelvin))),
                min(100, max(0, brightness)),
            ),
            wait_notif=TRANSITION_SETTLE_TIME,
        )

    async def set_color(
        self, red: int, green: int, blue: int, brightness: int | None = None
    ) -> None:
        brightness = self._brightness if brightness is None else brightness
        await self.send_cmd(
            struct.pack(
                "BBBBBBB11x",
                COMMAND_STX,
                CMD_RGB,
                red,
                green,
                blue,
                0x01,
                min(100, max(0, brightness)),
            ),
            wait_notif=TRANSITION_SETTLE_TIME,
        )

    async def get_name(self) -> None:
        await self.send_cmd(struct.pack("BB16x", COMMAND_STX, CMD_GETNAME))

    async def get_version(self) -> None:
        await self.send_cmd(struct.pack("BB16x", COMMAND_STX, CMD_GETVER))

    async def get_serial(self) -> None:
        await self.send_cmd(struct.pack("BB16x", COMMAND_STX, CMD_GETSERIAL))

    def _notification_from(
        self, client: BleakClient, sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        if client is self._client and not self._closed:
            self.notification_handler(sender, data)

    def notification_handler(
        self, sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Accept complete protocol frames; ignore malformed or unrelated data."""
        _LOGGER.debug("%s: Received %s", self._mac, data.hex())
        if len(data) != 18 or data[0] != COMMAND_STX:
            _LOGGER.debug("%s: Ignoring invalid notification frame", self._mac)
            return
        response = data[1]
        if response == RES_GETSTATE:
            state = struct.unpack(">xxBBBBBBBhx6x", data)
            brightness = state[1] if self._model == MODEL_CANDELA else state[6]
            if state[0] not in (CMD_POWER_ON, CMD_POWER_OFF) or brightness > 100:
                return
            if self._model != MODEL_CANDELA and state[1] not in (
                self.MODE_COLOR,
                self.MODE_WHITE,
                self.MODE_FLOW,
            ):
                return
            self._is_on = state[0] == CMD_POWER_ON
            self._brightness = brightness
            if self._model == MODEL_CANDELA:
                self._mode = self.MODE_WHITE
            else:
                self._mode = state[1]
                self._rgb = (state[2], state[3], state[4])
                self._temperature = state[7]
            self._has_state = True
            self._state_event.set()
            self.run_state_changed_cb()
        elif response == RES_PAIR:
            result = data[2]
            if result == 0x01:
                action = (
                    "turn the cylinder"
                    if self._model == MODEL_CANDELA
                    else "press the small button"
                )
                _LOGGER.warning(
                    "%s: Pairing requested; %s on the lamp", self._mac, action
                )
                self._conn = Conn.PAIRING
            elif result in (0x02, 0x04):
                self._conn = Conn.PAIRED
                self._pair_resp_event.set()
            else:
                self._conn = Conn.UNPAIRED
                self._pair_resp_event.set()
                self.run_state_changed_cb()
        elif response == RES_GETVER:
            self.versions = struct.unpack("xxBHHHH6x", data)
        elif response == RES_GETSERIAL:
            self.serial = data[2]


async def find_device_by_address(
    address: str, timeout: float = 20.0
) -> BLEDevice | None:
    return await BleakScanner.find_device_by_address(address.upper(), timeout=timeout)


async def discover_yeelight_lamps(
    scanner: BleakScanner | None = None,
) -> list[dict[str, Any]]:
    scanner = scanner if scanner is not None else BleakScanner()
    return [
        {"ble_device": device, "model": model_from_name(device.name)}
        for device in await scanner.discover()
        if model_from_name(device.name) != MODEL_UNKNOWN
    ]
