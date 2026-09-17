# Candela stability review and local fix

Reviewed against repository commit `146e4d6` and Home Assistant Core 2026.9.0.
Target: a Bluetooth Yeelight Candela (`yeelight_ms*`) reached through an ESPHome
Bluetooth proxy. These are implementation defects; none requires concluding that
the user's proxy is faulty.

## How the protocol works

Home Assistant discovers the advertisement and supplies a `BLEDevice` that includes
its route through a connectable adapter or proxy. The integration opens a BLE GATT
connection, enables notifications, sends the Yeelight application-level pairing
command, and receives pairing and state frames. This pairing command is distinct
from operating-system Bluetooth bonding.

Control characteristic: `aa7d3f34-2d4f-41e0-807f-52fbf8cf7443`.
Notification characteristic: `8f65073d-9f57-4aaa-afea-397d19d5bbeb`.
Frames are 18 bytes, start with `43`, and contain a command and zero-padded payload:

| Prefix | Meaning |
| --- | --- |
| `43 67 02` | Request Yeelight pairing |
| `43 63 …` | Pairing response; `02` or `04` means accepted/already paired |
| `43 40 01` / `43 40 02` | Power on / off |
| `43 42 xx` | Brightness, device scale 0–100 |
| `43 44 02` | Request state |
| `43 45 …` | Actual state response; Candela power is byte 2 and brightness byte 3 |

A healthy persistent connection occupies one proxy connection slot and allows
physical changes at the lamp to reach Home Assistant. The problem is a connection
that occupies a slot without completing the protocol or being cleaned up.

## Findings in the original code

| Severity | Defect | Consequence |
| --- | --- | --- |
| Critical | `Lamp.connect()` initializes Candela only when a private backend type string exactly matches BlueZ. | An ESPHome connection remains UNPAIRED: connected at BLE level, unavailable in HA. Polling reconnects without completing setup. |
| Critical | Candela never subscribes to notifications; the BlueZ branch assumes successful pairing after sleeping. | No actual state or pairing confirmation; “connected” is mistaken for usable. |
| High | Candela advertises brightness-only but reports `color_mode=hs`. | HA 2026.9 raises an unsupported-color-mode error when serializing state. |
| High | No lock across connecting, polling, writes, or complete power-plus-brightness actions. | Concurrent actions can create competing clients and interrupt lamp transitions. |
| High | Pairing waits indefinitely; most errors are logged and suppressed. | Hung operations and HA controls that claim success after failed writes. |
| High | Disconnect callbacks from obsolete clients are accepted; stale BLEDevice objects are reused. | A delayed callback can invalidate a healthy connection; reconnection may use an outdated route. |
| Medium | Enabling DEBUG triggers reads of every characteristic and descriptor during connection. | Diagnostics change timing and add potentially failing I/O. |
| Medium | Discovery falls back to creating a local scanner. | Proxy-only deployments can take an inappropriate discovery path. |
| Medium | Unsupported effects/transitions are advertised; Bedside Kelvin/color-mode and brightness units are inconsistent. | Misleading controls and invalid or incorrect entity state. |
| Medium | No regression suite, incomplete unload cleanup, packet parsing assumes all input is valid. | Failures are difficult to detect before installation and reloads can leave work running. |

## Candela notification compatibility

The installed version 1.4.3 produced a concrete GATT report: notification value
handle **34**, with a single user-description descriptor (`0x2901`) at **35**,
containing `4e4f5449465900` (`NOTIFY\0`). No client configuration descriptor
(`0x2902`, CCCD) was present. Earlier assumptions based on historical captures
and backend handle numbering did not match this lamp. Writing a configuration
value to handle 35 would overwrite a description, not enable notifications.

Version 1.4.4 recognizes this layout and registers an ESPHome notification listener
without writing a descriptor. ESPHome 2026.9.0 separates local listener registration
from CCCD writes; bleak-esphome normally performs both. The compatibility adapter
uses HA's existing proxy API connection and installs cleanup callbacks in the
backend's existing notification registry. It does not fabricate descriptors or
modify cached service records. Unknown layouts are rejected. Lamps with a CCCD
and local Bluetooth backends retain the standard Bleak subscription path.

This adapter depends on private bleak-esphome backend fields, isolated in
`candela.py`; dependency updates may require adapting it. Tests exercise the real
aioesphomeapi registration and callback registry with mocked network responses.
Cancellation waits for the API's bounded registration request to finish and cleans
it up before allowing reconnection, preventing a late registration from replacing
a newer listener. This can delay cancellation by the remaining registration timeout
(up to 10 seconds).

Listener registration is not proof of successful communication. Pairing and actual
state notifications remain mandatory, and physical lamp changes still use received
state frames. Whether this lamp sends those replies through the new listener path
requires hardware validation. Local BlueZ compatibility for malformed GATT tables
remains limited.

### Failure diagnostics in version 1.4.4

Each failed connection or polling attempt produces a normal warning containing:

- Integration version, attempt number, failing protocol phase, connection state,
  notification count, subscription mode, and command response mode.
- Selected proxy/backend information and firmware version when available.
- Discovered characteristic handles, UUIDs, properties, and descriptor handles/UUIDs.
- The last 20 timestamped protocol events, including sent commands, received replies,
  inspected descriptor values, and disconnections.

GATT output is capped at 32 characteristics and eight descriptors per characteristic;
received packet and descriptor previews are capped at 32 bytes. Diagnostics do not
dump proxy API credentials. After a pairing or state reply timeout, a readable
notification characteristic is read once with a two-second timeout. Its value is
logged for diagnosis only and never accepted as confirmed state. Diagnostic failure
must not prevent connection cleanup.

Cooldown polls log only at DEBUG; each subsequent real failed attempt gets a fresh
warning, and recovery is logged. To report a failure, copy the full Yeelight warning
from Home Assistant logs, including `Yeelight diagnostics`, `Route`, `GATT`, and
`Recent activity`. Debug logging is not required for this report.

## Changes implemented

- One serialized connection/command pipeline per lamp, plus serialization of complete
  entity service actions and polls. Different lamps can operate independently.
- Fresh HA device lookup before reconnection, new clients, bounded connection attempts,
  a single retry of a failed absolute command, and 30–300 second recovery cooldowns.
- Bounded GATT, pairing, state, and disconnect waits. Failed initialization disconnects
  immediately; removal cancels in-flight I/O and prevents queued work from reconnecting.
- Subscribe → confirmed pairing → confirmed state on both lamp models. Missing replies
  are errors. State comes from notification frames, including physical lamp changes.
- GATT writes use an explicit response mode from the characteristic properties.
  Brightness/color transitions settle before the state request, under the same lock.
- No optimistic state mutation after failed commands. Action errors reach HA; actual failed
  attempts include warning diagnostics, cooldown polls stay at DEBUG, and recovery is logged.
- Candela exposes brightness-only mode; unsupported effects/transitions are removed.
  Bedside color temperature uses the Kelvin API and existing calibration mapping.
- Shared HA discovery history, duplicate filtering, and correct unload bookkeeping.
- Existing entity unique IDs are preserved. The compatibility baseline is now
  HA 2026.9.0 / Python 3.14.2+, recorded in HACS metadata and dependency requirements.

## Validation

Run with Python 3.14.2 or newer:

```sh
python3 -m venv .venv-test
.venv-test/bin/python -m pip install -r requirements-test.txt
.venv-test/bin/python -m pytest -q
.venv-test/bin/ruff check custom_components/yeelight_bt tests
```

The suite uses Home Assistant 2026.9.0 and its pinned Bluetooth libraries. Network
and radio calls are mocked. It covers actual HA state serialization, Candela physical
state notifications, the reported descriptor layout without a CCCD write, real API
callback registration and cleanup, late acknowledgements, warning diagnostics, timeouts,
rejected pairing, stale callbacks, malformed packets, concurrent commands, reconnection,
cooldowns, shutdown cancellation, discovery, Bedside behavior, and entity unloading.

Remaining physical checks: first pairing, on/off and brightness, turning the cylinder
and observing HA, lamp power loss and recovery, proxy restart, and HA integration reload.
Publishing this code does not install it on Home Assistant; deployment and physical
validation are separate steps.

## Reference implementation sources

- [HA Bluetooth integration guidance](https://developers.home-assistant.io/docs/bluetooth/).
- [HA 2026.9 light state validation](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/light/__init__.py).
- [Bleak write and notification APIs](https://bleak.readthedocs.io/en/latest/api/client.html).
- [ESPHome client implementation](https://github.com/Bluetooth-Devices/bleak-esphome/blob/v4.0.0/src/bleak_esphome/backend/client.py).
- [ESPHome 2026.9.0 listener registration](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/bluetooth_connection/bluetooth_connection_bluedroid.cpp).
- [aioesphomeapi 46.2.0 notification lifecycle](https://github.com/esphome/aioesphomeapi/blob/v46.2.0/aioesphomeapi/client.py).
- [Legacy notification enable implementation](https://github.com/hcoohb/hass-yeelightbt/blob/v0.11.3/custom_components/yeelight_bt/yeelightbt.py).
- [Upstream Candela/proxy proposal](https://github.com/hcoohb/hass-yeelightbt/pull/79) documents
  a command-only workaround; that approach is not used here.
