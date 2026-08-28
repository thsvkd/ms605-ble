# ms605-ble

**English** | [한국어](README.ko.md)

An unofficial Python library and command-line application for controlling the
Meross MS605 presence sensor over its local BLE configuration channel. It can
read and clone configuration, tune zone thresholds, change sensitivity and
DND, monitor live radar/PIR output, and run single- or multi-sensor space
learning without a cloud account.

This repository publishes an independently written interoperability
specification, implementation, research tools, and synthetic tests. It does
**not** publish application binaries, decompiled vendor source, raw Bluetooth
captures, real device profiles, or other private research data.

> [!WARNING]
> This is an experimental, unaffiliated project. BLE writes can change device
> behavior. Use it only with hardware you own or are authorized to test, begin
> with read-only operations, and verify important changes on the device. The
> history APIs are explicitly experimental.

## Highlights

- Direct local BLE operation through [`bleak`](https://github.com/hbldh/bleak)
  on macOS, Linux, and Windows.
- Interactive app plus a lower-level scripting CLI.
- Reads and writes for sensitivity, zone thresholds, zone assignment, DND,
  time synchronization, sub-sensor settings, and live output.
- Single- and multi-sensor auto-calibration with keep-alive handling.
- Configuration profiles for selective backup and cloning.
- Pure frame/model modules and a radio-free test suite using synthetic fixtures
  plus non-identifying interoperability constants.
- Standalone research tools for scanning, GATT enumeration, notification
  logging, controlled writes, and offline btsnoop decoding.
- A privacy- and IP-safe reverse-engineering workflow documented below.

## Quick start

Python 3.10 or newer and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
./scripts/setup.sh
./scripts/test.sh
./scripts/run.sh
```

The MS605 normally accepts a connection only for a short period after its
physical button is pressed. Close other applications connected to the sensor,
press the button, then run a command:

```bash
uv run ms605                         # interactive UI
uv run ms605 read                    # discover and read configuration
uv run ms605 calibrate               # select one or more sensors
uv run ms605 clone --help            # selective configuration cloning
uv run ms605-driver scan             # low-level discovery
uv run ms605-driver --address <ADDR> read
```

Use `uv run ms605 --help` and `uv run ms605-driver --help` for the complete,
version-matched command reference.

### Library example

```python
import asyncio

from ms605 import MS605


async def main() -> None:
    devices = await MS605.scan(timeout=6)
    if not devices:
        raise RuntimeError("No MS605 found; press its button and retry")

    sensor = MS605(devices[0])
    await sensor.connect()
    try:
        config = await sensor.read_config()
        print(config.sensitivity, config.zone_thresholds)
    finally:
        await sensor.disconnect()


asyncio.run(main())
```

## Main commands

| Command | Purpose | Support status |
| --- | --- | --- |
| `ms605` | Interactive single-device workflow | Implemented |
| `ms605 read` | Read metadata and configuration | Implemented |
| `ms605 calibrate` | Run space learning on one or several sensors | Implemented; verify results |
| `ms605 clone` | Save or selectively apply writable settings | Implemented; review target first |
| `ms605 monitor` | Show live radar, PIR, zone, and sub-sensor state | Implemented |
| `ms605 set-*` | Change sensitivity, zones, DND, or sub-sensor settings | Implemented; mutating |
| `ms605 read-history` | Read presence or light history | Experimental; pagination unknown |
| `ms605-driver` | Low-level one-shot driver commands | Implemented |

Configuration profiles can contain device names or addresses. Treat profile
JSON and calibration logs as private local data. Save profiles under the
ignored `private-profiles/` directory or outside the checkout; arbitrary
`--save` paths are **not** ignored automatically. Contributors remain
responsible for reviewing every staged file.

## Repository layout

| Path | Responsibility |
| --- | --- |
| [`ms605/protocol.py`](ms605/protocol.py) | Frame envelope, CRC, tags, message IDs, chunking, and reassembly; no I/O. |
| [`ms605/models.py`](ms605/models.py) | Pure TLV value codecs and public dataclasses. |
| [`ms605/driver.py`](ms605/driver.py) | Async connection lifecycle and high-level operations. |
| [`ms605/cli/`](ms605/cli/) | Interactive and low-level CLIs. |
| [`docs/SPEC.md`](docs/SPEC.md) | Sanitized public protocol contract and confidence boundaries. |
| [`tools/`](tools/) | Independent research and diagnostic utilities. |
| [`tests/`](tests/) | Radio-free tests using synthetic fixtures and fixed, non-identifying protocol constants. |

The public evidence chain is intentionally small:

```text
private authorized observation
  -> independently written behavioral specification
  -> independently written implementation
  -> synthetic regression tests
```

Raw observations are temporary inputs to research, never repository assets or
build dependencies.

## How this project was reverse-engineered

This section specifies the repeatable method, including the controls that keep
the public project useful without retaining private or proprietary evidence.
It is a research protocol, not permission to bypass access controls or inspect
systems you are not authorized to test. Check the laws and terms that apply to
your work.

### 1. Start with a narrow interoperability question

Define the smallest behavior necessary for an independent client. For this
project the target was the device's local BLE configuration channel:
discovery, GATT roles, configuration reads and writes, notifications, and
space learning. Accounts, credentials, cloud APIs, and unrelated commissioning
flows were excluded unless needed to distinguish transports.

Write competing, falsifiable hypotheses before inspecting payloads. The
initial transport hypotheses were:

1. the older vendor BLE/JSON transport;
2. a standard commissioning transport;
3. a separate binary configuration service.

For each intended action, record four things in a private research notebook:

- the expected externally visible result;
- bytes or fields expected to remain constant;
- fields expected to change;
- an observation that would disprove the hypothesis.

A hypothesis becomes a protocol claim only when it explains several actions,
parses in both directions where applicable, and can be generated independently.

### 2. Keep a local provenance ledger

Use only an application copy and device that you are authorized to inspect.
Before analysis, record the following locally:

| Field | What to record |
| --- | --- |
| Application identity | Exact package/application identifier |
| Version | Version name, version code, and platform |
| Acquisition | Authorized source and date |
| Integrity | SHA-256 of the exact artifact |
| Packaging | Base package, split packages, and relevant bytecode containers |
| Environment | OS, runtime, decompiler/disassembler, and tool versions |
| Redistribution | `no` for binaries and vendor-derived source |

The ledger makes a result repeatable without redistributing the analyzed
artifact. Store it outside the repository if it includes local paths,
timestamps, account context, or device identifiers. Never invent a version or
hash for an older artifact; reacquire and repeat the analysis instead.

The original analysis ledger is not published in this sanitized repository, so
exact historical static-analysis reproduction is currently unavailable. A
contributor can reproduce the method with an independently acquired authorized
artifact, but must not claim it is the same artifact without matching published
version and SHA-256 metadata. Future research should publish those safe ledger
fields when known, without publishing the binary or local paths.

### 3. Use static analysis only to derive interface facts

Static analysis is a map of behavior, not source material for this project.
Trace one user action at a time:

1. identify scan filters and GATT service/characteristic selection;
2. follow the UI action into its data model and BLE call;
3. identify serialization, integrity checking, chunking, and response routing;
4. record only interface facts such as field order, width, byte order, and
   state transitions;
5. restate those facts in independent prose in [`docs/SPEC.md`](docs/SPEC.md).

Do not translate or paste vendor method bodies, comments, class layouts, or
decompiler output. A public implementation must be understandable and
maintainable without access to any proprietary source.

### 4. Isolate dynamic BLE experiments

Use controlled sessions on owned or authorized hardware:

- Prefer a dedicated test phone/profile.
- Disconnect watches, headphones, cars, and unrelated BLE accessories.
- Disable unrelated applications and background activity when practical.
- Start a fresh log and perform exactly one named action per session.
- Record `T+0`, `T+5`, and other relative times instead of wall-clock time.
- Change one setting at a time, within normal UI ranges, then restore it.
- Enumerate the target GATT service first so ATT handles have explicit roles.
- Stop immediately after the expected response or notification.
- Keep the raw session only in ignored, temporary local storage.

Isolation is a correctness control as well as a privacy control. A system-wide
Bluetooth log can contain unrelated device traffic, stable identifiers, names,
network information, locations, timestamps, and application activity.

### 5. Reconstruct the transport from the bottom up

Decode each layer independently instead of searching the byte stream for a
desired value:

```text
HCI ACL records
  -> L2CAP reassembly
  -> ATT writes and notifications
  -> application-chunk reassembly
  -> frame envelope
  -> TLV attributes
  -> operation semantics
```

Group writes and notifications by connection and characteristic role. Use
declared lengths and envelope boundaries for reassembly; packet timing alone
is not a reliable delimiter. Repeat an identical action to separate dynamic
fields such as message counters and checksums from semantic payload bytes.

### 6. Infer framing and TLV structure with differential tests

Test candidate layouts against every controlled session, not a single example.
A useful sequence is:

1. repeat one action to identify volatile fields;
2. change one enum through every allowed value;
3. change one zone or sub-sensor field while holding all others constant;
4. compare a read before and after a write;
5. test single- and multi-attribute messages;
6. reject any candidate that requires action-specific exceptions.

Determine tag width, length width, byte order, signedness, array stride, and
padding separately. Preserve unknown values as opaque bytes until an
experiment distinguishes their meaning.

### 7. Identify the integrity algorithm independently

Treat checksum selection and checksum coverage as two separate hypotheses.
For each candidate:

- test several structurally different frames;
- vary the proposed covered byte range;
- verify the standard algorithm check vector when one exists;
- flip one byte and confirm the check fails;
- generate a complete frame with an independently written encoder;
- parse the generated frame through a separate code path.

For the current public specification, CRC-16/CCITT-FALSE must produce `0x29B1`
for ASCII `123456789`. That standard vector is a canonical conformance check
for the local algorithm; it does not by itself prove correctness for every
input or device compatibility.

### 8. Establish semantics through an action matrix

Wire structure and human meaning are different claims. Use a matrix that
isolates one semantic variable:

| Experiment | Hold constant | Change | Establishes |
| --- | --- | --- | --- |
| Repeat one action | Visible setting | Message instance | Counters and volatile fields |
| Walk an enum | Device and screen | One ordered choice | Candidate enum encoding |
| Change one zone | Every other zone | One threshold pair | Array stride and byte order |
| Read -> write -> read | Connection and target | One property | Read/write correspondence |
| Start a long action | Initial device state | One command | Immediate response vs later push |

Avoid broad fuzzing and potentially destructive writes. Prefer normal UI
ranges and the smallest experiment that can discriminate between hypotheses.

### 9. Write from the specification, not the analyzed program

The implementation boundary is:

```text
observation notes -> behavioral spec -> independently written protocol code -> driver -> CLI
```

The runtime package must not import a private artifact or require a vendor
binary. The tools under [`tools/`](tools/) intentionally do not import the
runtime package, allowing shared framing and checksum invariants to be checked
through a second implementation path. This can expose implementation
divergence; it is not independent authorship or external device evidence.
Differences between a low-level tool and the runtime command contract must be
explicit rather than silently normalized.

Keep protocol parsing pure and separate from BLE I/O. Preserve unknown TLVs in
parsed frames so new firmware does not force speculative decoding.

### 10. Convert findings into synthetic regressions

Public tests must be constructible from the specification alone. Before a
vector can be committed:

1. replace message IDs and counters;
2. replace all timestamps with fixed synthetic epochs;
3. replace names, addresses, device/session-specific UUIDs (including
   CoreBluetooth identifiers), device IDs, and account-like values while
   retaining protocol UUID constants required for interoperability;
4. replace sensor readings and configuration values with deliberately chosen
   synthetic patterns;
5. rebuild lengths and CRCs from scratch;
6. verify round trips and negative cases offline;
7. confirm that no unchanged neighboring bytes came from the private session.

Masking a few visible fields inside a real packet is insufficient: checksums,
padding, timing, and adjacent values may still identify the private session.

The public suite covers standard CRC behavior, frame round trips, malformed
input, checksum failure reporting, chunking/reassembly, message-ID rollover,
advertisement matching, and synthetic command/response/push models.

### 11. Label confidence without overstating evidence

The public repository uses three labels:

| Label | Meaning |
| --- | --- |
| **Implemented / repository-verified** | The behavior exists in code and is exercised by synthetic fixtures or non-identifying protocol constants. This is not a universal hardware-compatibility claim. |
| **Experimental** | The code expresses a plausible model, but public evidence does not establish device behavior, semantics, pagination, or firmware coverage. |
| **Unknown / out of scope** | Evidence is absent or conflicting, or the behavior requires an excluded workflow. |

Deleting private evidence means the project must not claim that the current
repository independently proves a byte-for-byte device observation. A
maintainer may reproduce behavior privately, but a public confidence change
requires a sanitized report and a synthetic regression.

### 12. Reproduce a result without sharing raw data

A useful issue or pull request contains:

- application version and SHA-256, if disclosure is safe;
- firmware version without a stable device identifier;
- one action performed and its expected result;
- the sanitized field-level difference, using relative order rather than real
  timestamps;
- tool versions;
- a newly generated synthetic test vector;
- the requested confidence-label change.

Do **not** attach the capture, app package, decompiled source, bug report,
sysdiagnose archive, or real configuration profile. If reviewers cannot
evaluate the claim from a field-level description and synthetic regression,
the claim remains experimental.

### 13. Delete private evidence after derivation

Raw research artifacts are temporary and are not archived in a Git branch.
The completion gate for one finding is:

1. record the minimal derived interface fact in independent prose;
2. implement it without copying vendor code;
3. add a synthetic positive and, where useful, negative test;
4. record remaining ambiguity and compatibility limits;
5. delete the local raw session and temporary decompiler output.

This preserves the meaning of the research—the repeatable method, contract,
implementation, and falsifiable tests—without preserving personal or
proprietary data.

## Repository data policy

### Never commit

- APK, XAPK, IPA, DEX, firmware images, or decompiled vendor source.
- Raw HCI/btsnoop, PacketLogger, pcap, bugreport, or sysdiagnose files.
- Real BLE/MAC addresses, CoreBluetooth identifiers, device IDs, device names,
  account IDs, SSIDs/BSSIDs, coordinates, or local filesystem paths.
- Wall-clock session timestamps or unrelated device/application traffic.
- Real configuration profiles, calibration histories, or sensor telemetry.
- Payloads copied verbatim from a real session when a synthetic equivalent is
  possible.

### Allowed public artifacts

- Independently written protocol prose and diagrams.
- Interface constants necessary for interoperability.
- Independently written source code with no dependency on private evidence.
- Synthetic frames with regenerated lengths and integrity fields.
- Aggregate, non-identifying observations and explicit confidence labels.
- Reproduction steps that require contributors to acquire their own authorized
  inputs.

The `.gitignore` entries are a guardrail, not a privacy scanner. Review
`git diff --cached` before every commit and treat all new binary files as
suspect until identified.

### If private data is committed accidentally

Do not hide it with a follow-up deletion commit; Git history still contains the
object. Stop new pushes, identify every affected ref, rotate any exposed secret,
rewrite the affected history, update the remote with an exact lease, and verify
from a fresh clone that paths, strings, and object IDs are unreachable. Ask the
hosting provider to purge cached views or retained pull-request data when
needed. Contributors must discard old clones and branches rather than merging
contaminated history back.

## Development and verification

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check .

uv run python tools/scan.py --self-test
uv run python tools/enumerate.py --self-test
uv run python tools/notify_logger.py --self-test
uv run python tools/replay.py --self-test
uv run python tools/btsnoop_att.py --self-test
```

After dependencies are available, all tests and tool self-tests above are
network-free and radio-free. `uv sync --frozen` may access the package registry
when the local cache is incomplete. Hardware validation is a separate, opt-in
activity and must follow the isolation and data-handling rules in this README.

When changing protocol behavior:

1. update [`docs/SPEC.md`](docs/SPEC.md) and its confidence boundary;
2. add or change a synthetic regression test;
3. keep parsing/model logic independent from radio I/O;
4. run the complete offline verification set;
5. inspect the staged diff for private or copied material.

## Release and license status

This repository does not yet contain a `LICENSE` file. Until the maintainers
choose and add a license, the source is reviewable but no general reuse license
is granted; it must not be presented as release-ready open source. License
selection and matching package metadata are required before public release.

## Compatibility and non-affiliation

Behavior may vary by firmware, mobile OS BLE backend, and platform. The
repository test suite verifies the published software contract, not every
device/firmware combination. Meross is a trademark of its owner. This project
is not affiliated with, endorsed by, or supported by Meross.
