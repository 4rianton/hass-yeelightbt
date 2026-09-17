"""In-memory GATT peer; no test opens a Bluetooth connection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aioesphomeapi import APIClient, BluetoothProxyFeature
from aioesphomeapi.api_pb2 import BluetoothGATTNotifyDataResponse
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection
from bleak_esphome.backend.client import ESPHomeClient

from custom_components.yeelight_bt import yeelightbt as protocol
from custom_components.yeelight_bt.candela import CCCD_UUID, USER_DESCRIPTION_UUID


@pytest.fixture
def device():
    return BLEDevice("AA:BB:CC:DD:EE:FF", "yeelight_ms", {"source": "proxy"})


def make_services(cccd=34, notify_handle=33):
    services = BleakGATTServiceCollection()
    service = BleakGATTService(None, 29, "8e2f0cbd-1a66-4b53-ace6-b494e25f87bd")
    services.add_service(service)
    control = BleakGATTCharacteristic(
        None, 30, protocol.CONTROL_UUID, ["write"], lambda: 20, service
    )
    notify = BleakGATTCharacteristic(
        None,
        notify_handle,
        protocol.NOTIFY_UUID,
        ["read", "notify"],
        lambda: 20,
        service,
    )
    services.add_characteristic(control)
    services.add_characteristic(notify)
    if cccd is not None:
        services.add_descriptor(BleakGATTDescriptor(None, cccd, CCCD_UUID, notify))
    return services


def state_packet(on=True, brightness=40, bedside=False, mode=2):
    data = bytearray(18)
    data[:3] = bytes([0x43, 0x45, 1 if on else 2])
    if bedside:
        data[3:9] = bytes([mode, 12, 34, 56, 0, brightness])
        data[9:11] = (4080).to_bytes(2, "big")
    else:
        data[3:5] = bytes([brightness, 2])
    return data


class FakeClient:
    def __init__(self, *, bedside=False):
        self.services = make_services()
        # The real backend class gates the Candela workaround.
        self._backend = object.__new__(ESPHomeClient) if not bedside else object()
        if not bedside:
            self._backend._cancel_connection_state = None
            self._backend._loop = Mock()
            self._backend._loop.is_closed.return_value = True
            self._backend._notify_cancels = {}
            self._backend._is_connected = True
            self._backend._feature_flags = BluetoothProxyFeature.REMOTE_CACHING.value
            self._backend._address_as_int = 123
            self._backend._description = "test proxy"
            self._backend._disconnect_callbacks = set()
            self._backend._bluetooth_device = Mock()
        self.is_connected = True
        self.notify_callback = None
        self.disconnected_callback = None
        self.bedside = bedside
        self.on = True
        self.brightness = 40
        self.mode = 2
        self.pair_result = 4
        self.reply_to_state = True
        self.fail_command = None
        self.writes = []
        self.read_gatt_descriptor = AsyncMock(return_value=b"NOTIFY\0")
        self.read_gatt_char = AsyncMock(return_value=b"\x43\xff" + bytes(16))
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.block_write = None
        self.write_entered = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def start_notify(self, characteristic, callback):
        self.notify_characteristic = characteristic
        self.notify_callback = callback

    def emit(self, data):
        if self.notify_callback:
            self.notify_callback(self.notify_characteristic, data)

    def emit_state(self):
        self.emit(state_packet(self.on, self.brightness, self.bedside, self.mode))

    def drop(self):
        self.is_connected = False
        if isinstance(self._backend, ESPHomeClient):
            self._backend._async_disconnected_cleanup()
        self.disconnected_callback(self)

    async def _disconnect(self):
        self.is_connected = False
        if isinstance(self._backend, ESPHomeClient):
            self._backend._async_disconnected_cleanup()
        if self.disconnected_callback:
            self.disconnected_callback(self)

    async def write_gatt_char(self, characteristic, bits, *, response):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.write_entered.set()
            if self.block_write:
                await self.block_write.wait()
            await asyncio.sleep(0)
            self.writes.append((bytes(bits), response))
            command = bits[1]
            if command == self.fail_command:
                raise protocol.BleakError("Connection reset by peer")
            if command == protocol.CMD_PAIR:
                if self.pair_result is not None:
                    self.emit(bytearray([0x43, 0x63, self.pair_result]) + bytearray(15))
            elif command == protocol.CMD_GETSTATE:
                if self.reply_to_state:
                    self.emit_state()
            elif command == protocol.CMD_POWER:
                self.on = bits[2] == 1
            elif command == protocol.CMD_BRIGHTNESS:
                self.brightness = bits[2]
            elif command == protocol.CMD_TEMP:
                self.mode, self.brightness = 2, bits[4]
            elif command == protocol.CMD_RGB:
                self.mode, self.brightness = 1, bits[6]
        finally:
            self.in_flight -= 1


@pytest.fixture
def fast_protocol(monkeypatch):
    for key in (
        "GATT_TIMEOUT",
        "PAIR_TIMEOUT",
        "STATE_TIMEOUT",
        "CONNECT_TIMEOUT",
        "DISCONNECT_TIMEOUT",
        "DIAGNOSTIC_TIMEOUT",
    ):
        monkeypatch.setattr(protocol, key, 0.05)
    monkeypatch.setattr(protocol, "RETRY_DELAY", 0)
    monkeypatch.setattr(protocol, "TRANSITION_SETTLE_TIME", 0)
    return protocol


@pytest.fixture
def connect_peer(monkeypatch, fast_protocol):
    clients = []
    connector = AsyncMock()

    def configure(*peers):
        clients.extend(peers)

        async def connect(*args, **kwargs):
            peer = clients.pop(0)
            peer.disconnected_callback = kwargs["disconnected_callback"]
            return peer

        connector.side_effect = connect
        monkeypatch.setattr(protocol, "establish_connection", connector)
        return connector

    return configure


@pytest_asyncio.fixture
async def observed_peer():
    """The user's exact layout, with real ESPHome API callback registration.

    Only the API network send and receive boundary is mocked. All API callback
    ownership and the ESPHome backend's disconnect cleanup are real methods.
    """
    peer = FakeClient()
    peer.services = make_services(None, notify_handle=34)
    notify = peer.services.get_characteristic(protocol.NOTIFY_UUID)
    peer.services.add_descriptor(
        BleakGATTDescriptor(None, 35, USER_DESCRIPTION_UUID, notify)
    )
    api = APIClient("test-proxy.local", 6053)
    connection = Mock()
    remove = connection.add_message_callback.return_value
    api._get_connection = Mock(return_value=connection)
    api._send_bluetooth_message_await_response = AsyncMock()
    api.bluetooth_gatt_stop_notify = Mock(wraps=api.bluetooth_gatt_stop_notify)
    peer._backend._client = api

    def emit(data):
        handler = connection.add_message_callback.call_args.args[0]
        handler(
            BluetoothGATTNotifyDataResponse(address=123, handle=34, data=bytes(data))
        )

    peer.emit = emit
    return SimpleNamespace(peer=peer, api=api, connection=connection, remove=remove)
