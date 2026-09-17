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

This repository's `v0.11.3` implementation enables notifications by writing
`01 00` to the notification value handle plus one. A historical
[Candela GATT capture](https://gist.github.com/yeahwangy/c52f7608859da7ef41c58cfb7f25e567)
shows value handle 33 but a purported configuration descriptor at 35 whose value
is the string `NOTIFY`, rather than a two-byte configuration. The actual legacy
enable write would target 34. Upstream reports also describe a missing descriptor
through ESPHome. This is evidence for a device-specific attribute-layout quirk,
not proof of the particular firmware layout on the user's lamp.

`candela.py` corrects only the characteristic object passed to ESPHome's notification
subscription when the configuration descriptor is missing, or when the descriptor
at +2 reads back as `NOTIFY`. A valid descriptor is left alone. Conflicting discovered
handles are rejected. Shared/cached service records are not modified. The existing
public Bleak/ESPHome subscribe and unsubscribe paths still own notification forwarding
and cleanup. The only private API access is a guarded backend type check, isolated
in this module. No raw ESPHome API connection or firmware modification is needed.

This targets ESPHome. BlueZ resolves notification descriptors internally and does
not use this correction. Candela units with the malformed layout connected through
local BlueZ may still fail. The old BlueZ-only assumption of successful pairing has
been removed; there is no silent switch to command-only state tracking. Bedside lamps
continue using the standard notification path.

The notification correction remains a hardware validation candidate. Tests establish
what the real ESPHome client writes and how it forwards notifications, but cannot
establish that a particular physical lamp accepts the write.

### First hardware report and version 1.4.3

The first installation on the user's ESPHome proxy failed with
`Candela notification configuration descriptor is ambiguous`. This is an error
raised by this integration before subscribing or sending the pairing command. It
means discovery already contains a descriptor with a different UUID at the proposed
legacy configuration handle. The original tests omitted this layout. The message
does not reveal the descriptor's UUID or value, so it does not establish that the
descriptor is a usable configuration setting.

Version 1.4.3 checks ownership before considering that descriptor. If it belongs to
the notification characteristic and is labelled as a user description (`0x2901`),
the integration reads it. A two-byte value with only notification/indication bits
set is treated as a candidate mislabelled configuration. This is a compatibility
heuristic, not a confirmed description of the user's firmware. The original service
cache is left unchanged and pairing and actual state replies remain mandatory.

Text values such as `NOTIFY`, unknown descriptor types, and descriptors belonging
to another characteristic are rejected without a write. Errors now include the
notification handle, descriptor UUIDs and, when inspected, the value in hex. Read
errors retain this layout information too. If the lamp has a text description at
that handle, this update will identify it but will not make the lamp available.
That layout still requires further protocol investigation. Physical validation of
the new candidate path is outstanding.

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
- No optimistic state mutation after failed commands. Action errors reach HA; repeated
  polling errors are reduced to debug after the first warning, with a recovery message.
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
state notifications, the real ESPHome backend's corrected descriptor write, timeouts,
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
- [Legacy notification enable implementation](https://github.com/hcoohb/hass-yeelightbt/blob/v0.11.3/custom_components/yeelight_bt/yeelightbt.py).
- [Upstream Candela/proxy proposal](https://github.com/hcoohb/hass-yeelightbt/pull/79) documents
  a command-only workaround; that approach is not used here.
