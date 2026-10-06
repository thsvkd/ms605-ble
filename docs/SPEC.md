# MS605 BLE interoperability specification

Status: **experimental, implementation-defined, and sanitized for public use**

This document describes the wire model implemented by this repository. It is
an independently written interoperability contract backed by source code and
synthetic offline tests. It does not embed or cite raw device captures,
decompiled vendor source, or proprietary application code, and it does not
claim compatibility with every firmware revision.

The reverse-engineering and evidence-handling method is specified in the
[README](../README.md#how-this-project-was-reverse-engineered).

## 1. Confidence vocabulary

Claims in this document use these labels:

| Label | Public meaning |
| --- | --- |
| **Implemented / repository-verified** | The behavior is present in the repository and exercised by synthetic offline tests or structural source checks. |
| **Experimental** | A codec or operation exists, but public evidence does not establish hardware semantics, pagination, timing, accepted ranges, or firmware coverage. |
| **Unknown / out of scope** | The meaning is intentionally not asserted or the workflow is not implemented. |

“Repository-verified” is a software-contract claim, not a statement that the
public repository contains independent device evidence. Hardware results must
be reproduced by each authorized operator.

## 2. Byte notation

- Multi-byte integers are big-endian unless stated otherwise.
- `u8`, `u16`, and `u32` are unsigned integers of the stated width.
- `i16` is a signed two's-complement 16-bit integer.
- `bytes[n]` is an opaque byte sequence of length `n`.
- Tag numbers are decimal unless written with a `0x` prefix.
- `C -> D` means client to device; `D -> C` means device to client.

## 3. Discovery and GATT profile

### 3.1 Identifiers

| Role | UUID |
| --- | --- |
| Service | `99e7be30-0001-4c6b-98a2-70fcb3471a72` |
| Write characteristic | `99e7be30-0002-4c6b-98a2-70fcb3471a72` |
| Notify characteristic | `99e7be30-0003-4c6b-98a2-70fcb3471a72` |
| Client Characteristic Configuration | `00002902-0000-1000-8000-00805f9b34fb` |

The driver subscribes to the notify characteristic and writes serialized frame
chunks to the write characteristic with write-without-response semantics.

### 3.2 Advertisement predicate

The repository considers an advertisement a candidate MS605 if **any** of the
following holds:

1. the service UUID above is advertised;
2. manufacturer-specific data under company ID `0xFFFF` is non-empty and its
   first byte is `0xC0`;
3. the local name starts with `RFBL_` or `MRBL_`, case-insensitively.

This is an implemented discovery heuristic, not a uniqueness guarantee.
Operators should validate the expected service/characteristic layout after a
connection; the driver itself proceeds by subscribing to the configured notify
characteristic rather than performing a separate identity check.

### 3.3 Manufacturer MAC (experimental)

The observed 23-byte company `0xFFFF` payload has markers `C0 18 01` at
bytes 0–2, `1A 04` at 4–5 and `19 0A` at 10–11. Bytes 12–21 are an identity
record beginning `1C 00`; its bytes 2–7 contain the manufacturer MAC. The
current decoder accepts only this observed shape and Meross OUI `C4:E7:AE`.
Other shapes return unavailable. This is inferred from six authorized devices,
including a repeated scan of the same device, not a published vendor grammar.
The prefix assignment is listed in the [IEEE OUI registry](https://standards-oui.ieee.org/oui/oui.csv).
Raw captures and unit-specific addresses are not repository fixtures.

The OS connection address remains separate (CoreBluetooth UUID on macOS).
The observed tag30 response changed on the same physical device; the older
claim below that it is stable is superseded. MAC-capable sessions now use lowercase MAC hex as the registry key. Legacy
sessions without MAC still fall back to tag30 and cannot promise stable
identity. Existing legacy keys are not automatically migrated across transports.
The tag30 response must not be interpreted as a MAC or a stable sensor name.

## 4. Application frame

### 4.1 Envelope

```text
offset  width  field
0       2      head magic = 55 AA
2       1      subdevType
3       2      bodyLength (u16 BE)
5       1      triggerSrc
6       1      msgId
7       ...    zero or more TLV attributes
5+L     2      CRC-16/CCITT-FALSE (u16 BE)
7+L     2      tail magic = AA 55
```

`L` is `bodyLength`. It counts `triggerSrc`, `msgId`, and every serialized TLV,
but excludes the head, `subdevType`, length field, CRC, and tail. Therefore the
total serialized frame length is `5 + L + 4` bytes.

The implementation uses:

| Context | `subdevType` | `triggerSrc` | `msgId` |
| --- | --- | --- | --- |
| Outgoing command | `0xC0` | `0x11` | `1..255` |
| Matched response | device supplied | device supplied | echoes request |
| Unsolicited push | device supplied | normally `0x00` | `0` |

`msgId=0` is reserved for pushes. The client-side generator rolls from 255
back to 1 and never emits zero.

### 4.2 TLV attribute

```text
tag:u8 | valueLength:u16 BE | value:bytes[valueLength]
```

Attributes are ordered and tags may repeat. Parsers must retain unknown tags as
opaque values rather than failing or guessing their semantics.

### 4.3 Command trailer

Every high-level command built by `ms605.protocol.build_command()` ends with:

```text
tag = 1 | length = 1 | value = 06
```

The runtime treats this as a mandatory client-command contract. Responses and
pushes built or parsed by the repository do not require the trailer. Behavior
of commands without it is outside the supported contract.

### 4.4 CRC

The integrity field is CRC-16/CCITT-FALSE:

| Parameter | Value |
| --- | --- |
| Width | 16 |
| Polynomial | `0x1021` |
| Initial value | `0xFFFF` |
| Reflect input/output | false / false |
| Final XOR | `0x0000` |
| Stored byte order | big-endian |
| Covered bytes | exactly the `bodyLength` bytes starting at `triggerSrc` |

The standard check vector is:

```text
CRC16-CCITT-FALSE("123456789") = 0x29B1
```

Structural parser errors raise `FrameError`. A CRC mismatch is preserved as
`ParsedFrame.crc_ok == False` so offline diagnostic callers can inspect the
frame. The live driver drops every received frame whose `crc_ok` is false
before routing it: it logs a warning with the `msgId`, size and CRC values
only (the raw bytes go to debug level, since frames can carry IDs and
timestamps). A request whose response is dropped this way times out with
`MS605TimeoutError`.

## 5. BLE chunking and notification reassembly

Frames larger than the ATT payload are split into consecutive byte slices.
The default is `MTU - 3`, or 20 bytes for an MTU of 23. No additional
per-chunk sequence header is added by the runtime.

Writes for one frame are serialized under a lock so concurrent operations and
keep-alives cannot interleave chunks. The default inter-chunk delay is 20 ms.

Notification reassembly follows this algorithm:

1. append each notification chunk to a byte buffer;
2. discard bytes before the next `55 AA` head, preserving a possible trailing
   `55` partial magic byte;
3. wait until at least five bytes reveal `bodyLength`; if it exceeds
   `MAX_FRAME_BODY_LEN` (4096, a cap rather than a measured limit), log a
   warning, drop one byte and resynchronize from step 2;
4. wait for `5 + bodyLength + 4` total bytes;
5. if the last two of those bytes are not the `AA 55` tail, drop one byte and
   resynchronize from step 2, so a corrupt frame cannot swallow the frame
   after it;
6. emit the complete candidate and continue, allowing multiple frames per
   feed;
7. pass each candidate to the structural/CRC parser.

Declared length, not notification timing, is the application-frame boundary.

## 6. Request, response, and push routing

### 6.1 Reads

A read command contains one tag-2 attribute per requested property:

```text
tag = 2 | length = 1 | value = requestedTag:u8
```

Multiple read requests may share a command frame. `read_config()` requests
tags 41, 48, 49, 50, 51, 52, 53, and 61 in a stable order.

### 6.2 Responses

The driver assigns a nonzero `msgId`, registers a pending future, writes all
chunks, and waits for a response with the same ID. Tag 3 is interpreted as a
status byte: zero is success and nonzero is an operation error. Exact nonzero
error-code meanings are unknown.

How the device reports status for a multi-attribute write (one tag 3 per
written attribute, or a single tag 3 for the whole frame) and whether
attributes are applied individually are unverified, pending the M0
real-device measurements (see the `MEASURE` notes in `ms605/sim.py`, which
assumes one tag 3 per attribute). As a defensive policy the driver fails the
whole call with `MS605DeviceError` if any tag 3 in the response is nonzero,
carrying the first nonzero code (`ParsedFrame.first_error_status()`). After
such a failure, callers must not assume which attributes, if any, were
applied; re-read the device to find out.

### 6.3 Pushes

Frames with `msgId=0` are routed to push handlers and tag-specific waiters.
Known push-oriented tags include live radar output (55), PIR state (56),
experimental history data (58/60), space-learning result (62), and sub-sensor
status (64). Firmware may send other or repeated attributes; consumers must
not assume one attribute per push.

## 7. Tag registry

The table describes the model in the current source tree. Direction is the
implemented use, not a guarantee that the device rejects other directions.

| Tag | Direction | Public interpretation | Value model | Status |
| ---: | --- | --- | --- | --- |
| 1 | C -> D | System sub-command / command trailer | command value `0x06` | Implemented |
| 2 | C -> D | Read request | requested tag as `u8` | Implemented |
| 3 | D -> C | Status/error | first byte, `0` success | Implemented; error meanings unknown |
| 21 | D -> C | Version or supported-tags blob | opaque bytes | Experimental semantics |
| 23 | D -> C | Battery | first byte displayed as percent | Experimental semantics/range |
| 30 | D -> C | Device ID | opaque bytes | Implemented raw read; private value |
| 32 | both | Do-not-disturb | first byte as boolean | Codec/driver path implemented; hardware coverage limited |
| 33 | C -> D | Clock synchronization | Unix epoch seconds as `u32` | Serializer/driver path implemented; hardware coverage limited |
| 36 | D -> C | Ambient light | bytes interpreted as unsigned integer | Experimental units/range |
| 41 | both | Sub-sensor enable | bit `i` enables sub-sensor `i` | Codec/driver path implemented for three sensors |
| 48 | both | Sub-sensor zone map | four bytes; first three are 7-zone bitmasks, fourth reserved | Codec/driver path implemented |
| 49 | both | Presence/absence durations | four `presence:u16, absence:u16` slots; first three exposed | Codec/driver path implemented |
| 50 | both | Zone enable | one-byte bitmask, bits 0..6 | Codec/driver path implemented |
| 51 | both | Zone thresholds | seven `trigger:u16, maintain:u16` pairs, 28 bytes | Codec/driver path implemented |
| 52 | both | Detection mode / space learning | enum byte, see below | Codec/driver path implemented; mode 4 is mutating |
| 53 | D -> C | Zone boundary distances | each byte interpreted as 0.1 m | Experimental semantics/range |
| 54 | C -> D | Live output enable | boolean byte | Serializer/driver path implemented; hardware effect experimental |
| 55 | D -> C push | Live radar snapshot | presence mask plus seven 10-byte zone records | Codec exists; hardware semantics experimental |
| 56 | D -> C | PIR state | first byte as boolean | Codec/driver path implemented; hardware semantics experimental |
| 57 | D -> C | Light-history count | unsigned big-endian integer | Experimental |
| 58 | D -> C | Presence-history records | 9- or 37-byte records | Experimental; pagination unknown |
| 59 | D -> C | Presence-history count | unsigned big-endian integer | Experimental |
| 60 | D -> C | Light-history records | 8-byte records | Experimental; pagination unknown |
| 61 | both | Radar sensitivity | enum byte, see below | Codec/driver path implemented |
| 62 | D -> C push | Space-learning result | first byte `1` means success | Codec exists; hardware semantics experimental |
| 64 | D -> C | Sub-sensor status | presence bitmask plus two timestamp slot arrays | Codec exists; hardware semantics experimental |
| 98 | both | Per-sub-sensor sample interval | repeated `index:u8, seconds:u16` | Experimental codec/driver path |

Unknown tags must remain accessible through `ParsedFrame.attributes` and
`ParsedFrame.get()` without being assigned a speculative name.

## 8. Value codecs

### 8.1 Sensitivity (tag 61)

| Value | Name |
| ---: | --- |
| 1 | `LOW` |
| 2 | `MEDIUM` |
| 3 | `HIGH` |
| 4 | `CUSTOM` |

`set_sensitivity()` writes tag 61 only. Reference threshold presets in source
are not automatically written by that method; threshold changes require a
separate tag-51 write.

Observed on 1-2 devices (section 8.9): writing a preset level did not change
the tag-51 values read back, and writing tag 51 did not change tag 61, so the
two tags behaved independently over BLE.

### 8.2 Detection mode (tag 52)

| Value | Name | Effect in this implementation |
| ---: | --- | --- |
| 1 | `RADAR` | ordinary setting |
| 2 | `RADAR_WITH_PIR` | ordinary setting |
| 3 | `PIR_WITH_RADAR` | ordinary setting |
| 4 | `SPACE_LEARNING` | starts auto-calibration; not cloneable as a passive setting |

### 8.3 Zone thresholds (tag 51)

The value is exactly 28 bytes:

```text
repeat 7 times, zone 0 through zone 6:
    trigger:u16 BE | maintain:u16 BE
```

The codec accepts the full `u16` range. Device-accepted ranges and units are
not established by the public repository; operator-facing code should prefer
known-safe UI ranges.

### 8.4 Sub-sensor zone map (tag 48)

The wire value is four bytes. Bytes 0..2 correspond to the three exposed
sub-sensors. In each byte, bit `z` assigns zone `z` for `z=0..6`. The encoder
writes zero to the reserved fourth byte.

### 8.5 Presence/absence durations (tag 49)

The value is 16 bytes containing four slots:

```text
presenceSeconds:u16 BE | absenceSeconds:u16 BE
```

The public model exposes the first three slots and pads the reserved fourth
slot with zero when encoding.

### 8.6 Live radar output (tag 55)

For seven zones, the minimum modeled value is 71 bytes:

```text
subSensorPresenceMask:u8
repeat 7 times:
    currentTrigger:i16 BE
    currentMaintain:i16 BE
    enabled:u8
    triggerActive:u8
    triggerThreshold:i16 BE
    maintainThreshold:i16 BE
```

Signed decoding preserves negative transient values. Exact physical units and
cross-firmware record size are experimental.

### 8.7 Sub-sensor status (tag 64)

The modeled three-sensor payload contains:

```text
presenceMask:u8
presenceTimestamp[4]:u32 BE
absenceTimestamp[4]:u32 BE
```

Only slots 0..2 are exposed. Timestamps are interpreted as Unix epoch seconds.
Because they can reveal occupancy and real-world time, actual values must never
be used as public fixtures.

### 8.8 Experimental history and sample interval

The current heuristic presence-history codec accepts a 9-byte simple record or
a 37-byte detailed record. The heuristic light-history codec accepts 8-byte
records. The driver performs one count read and one data round trip; it does
not implement pagination or establish whether data arrives in a response,
push, or both on any particular firmware.

Tag 98 is modeled as repeated three-byte records:

```text
sensorIndex:u8 | seconds:u16 BE
```

All operations in this subsection remain experimental.

### 8.9 Observed device behavior (M0 measurements)

Operator measurements on one or two authorized devices. Under section 1 they
are **Experimental**: observed, not established across firmware or units, and
not reproducible from this repository alone. The simulator defaults in
`ms605/sim.py` follow these numbers.

| Observation | Result | Sample | Confidence |
| --- | --- | --- | --- |
| Connectable window after a button press | At least 117 s; never closed before the measuring tool's 120 s cap. First connect 1-2 s after the press. True upper bound unknown | 3 rounds, 1-2 devices | Experimental, lower bound only |
| Idle link drop with no central->device traffic | Dropped after 29.6 s | 1 sample | Experimental, weak |
| Keep-alive interval | A 25 s interval held the link; 30 s dropped it. The driver's 15 s default is within the margin | 1-2 devices | Experimental |
| Tag 51 write vs tag 61 | Writing tag 51 did not set tag 61 to `CUSTOM`; writing a preset level (tag 61 = 2, from `CUSTOM`) did not change the tag-51 values; re-writing tag 61 after a tag-51 write kept the user thresholds | 1-2 devices | Experimental |
| Read-after-write visibility of tag 51 | The write was acked, but an immediate read-back showed the old value; it was visible 1 s later. Verify-after-write must poll (about 3 s is a reasonable bound) rather than read once | 1-2 devices | Experimental, coarse (about 1 s) |
| Tag 30 response | 20 bytes; changed on repeat reads of the same device | 2 devices | Not a verified permanent identity |
| Concurrent links from one central | 2 of 2 available sensors held for 30 s | 1 run | Experimental, lower bound only |

Not measured: whether keep-alive traffic affects an auto-calibration in
progress, and the real duration of auto-calibration. The 180 s calibration
duration and the keep-alive/learning independence in the simulator remain
assumptions.

## 9. High-level operation contracts

| Operation | Frame behavior | Important constraint |
| --- | --- | --- |
| `read_config()` | Multi-read tags 41/48/49/50/51/52/53/61 | Fails if an expected tag is absent |
| `set_zone_thresholds()` | Writes one 28-byte tag-51 value | Exactly seven pairs |
| `apply_profile()` | Writes selected cloneable sections | Sensitivity precedes thresholds (a precaution; tags 61 and 51 were observed independent); mode 4 is skipped |
| `set_live_output(True)` | Writes the intended tag-55 enable flag through tag 54 | Hardware effect experimental; caller must clean up handlers |
| `ping()` | Sends a command containing only the tag-1 trailer | Used as a liveness/keep-alive operation |
| `start_auto_calibration()` | Writes tag 52 value 4, awaits tag-62 push | Default timeout 200 s; default keep-alive 15 s |
| `read_*_history()` | Count read followed by one data request/wait | Experimental; no pagination |

The driver serializes writes, matches nonzero response IDs, and dispatches
zero-ID pushes independently so a long-running operation can receive pushes
while keep-alives are in flight.

## 10. Error and safety behavior

- Invalid magic, impossible declared length, truncated TLV headers/values, and
  invalid builder ranges raise `FrameError`.
- CRC mismatch is reported by the parser; the live driver drops such frames
  (section 4.4), so a bad-CRC response surfaces as `MS605TimeoutError`.
- BLE failures are wrapped in the package exception hierarchy where practical.
- Auto-calibration treats keep-alive failure as connection loss, except a
  keep-alive answered with a nonzero status or left unanswered while the link
  is still up, which is only logged.
- When the link goes away (`disconnect()`, `reconnect()`, a peer/idle drop or
  a stalled write), every in-flight request and push waiter fails with
  `MS605ConnectionError`, not `CancelledError`. A request still waiting to be
  written then is never written to a later link.
- Configuration cloning excludes tag 53 and refuses to apply detection mode 4
  because it starts an action instead of copying a passive value.
- The parser does not make unknown tags fatal.

Hardware writes can have effects not represented by this model. Begin with
discovery and reads, use authorized hardware, and avoid fuzzing or values
outside normal device UI ranges.

## 11. Implementation map

| Contract area | Primary code | Synthetic checks |
| --- | --- | --- |
| Frame, CRC, tags, chunking | `ms605/protocol.py` | `tests/test_protocol.py` |
| Value codecs and profiles | `ms605/models.py` | `tests/test_models.py`, `tests/test_config_profile.py` |
| Request/push lifecycle | `ms605/driver.py` | `tests/test_driver.py`, `tests/test_live_link.py` |
| Discovery predicates/errors | `ms605/discovery.py` | `tests/test_discovery.py` |
| User workflows | `ms605/cli/` | `tests/test_cli_*.py`, `tests/test_ui.py` |
| Independent diagnostic path | `tools/` | each tool's `--self-test` |

The package and tests must remain usable without an app binary, decompiler,
capture, BLE adapter, or physical sensor.

## 12. Synthetic-vector requirements

A protocol fixture may enter the public repository only if it is generated
from the documented structure and contains synthetic values throughout.
Required transformations include identifiers, message IDs, timestamps,
telemetry, configuration, lengths, and CRCs. Redacting a real frame in place is
not sufficient.

Tests should establish both positive and negative properties:

- deterministic build/parse round trips;
- the standard CRC check vector and an altered-byte failure;
- malformed magic, length, and TLV handling;
- fragmented and concatenated reassembly;
- response/push message-ID separation;
- boundary validation for public encoders;
- experimental status for unverified semantics.

## 13. Known unknowns

- Compatibility across firmware versions and regional variants.
- Exact schema and meaning of tag 21.
- Complete device error-code registry.
- Authoritative physical units and accepted ranges for every sensor value.
- History pagination, record delivery mode, retention, and firmware variants.
- Whether tag 98 is supported consistently.
- Semantics of tags not listed in the registry.
- Unattended connection longevity beyond the driver's local keep-alive model.
- Upper bound of the connectable window after a button press (only a 117 s
  lower bound was observed), and the true idle-drop and concurrent-link limits
  (single or small samples; see section 8.9).
- Whether keep-alive traffic affects auto-calibration, and the real
  calibration duration.

New claims should narrow this list only after a controlled authorized
reproduction, an independently written spec change, and a fully synthetic
regression test. Raw evidence must remain outside Git and be deleted after the
derived finding is secured.
