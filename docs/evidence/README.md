# Public research record

This is the sanitized public record of the research behind `ms605-ble`. It
makes the implementation reviewable without publishing BLE captures, device
addresses, names, timestamps, application binaries, or vendor source. Raw
research material is retained privately outside Git for authorized review; this
repository contains independently written descriptions and synthetic tests.

## Discovery path

1. Test only hardware the operator owns or is authorized to test.
2. Put the sensor into its physical-button connection window and scan locally.
3. Treat advertised service, manufacturer-data shape, and name prefix as
   candidate signals, not proof of identity.
4. After connecting, verify the expected GATT service and write/notify roles.
5. Perform a read-only configuration exchange before any state change.
6. Rewrite the result as a field-level specification and synthetic regression.

The candidate predicate and GATT roles are documented in
[the protocol specification](../SPEC.md#3-discovery-and-gatt-profile). The
predicate is intentionally not presented as cryptographic device identity.

## Packet structure established

The independently implemented application envelope has these stable structural
claims:

| Byte range | Meaning |
| --- | --- |
| `0..1` | head magic `55 AA` |
| `2` | sub-device type |
| `3..4` | big-endian body length |
| `5` | trigger source |
| `6` | message ID |
| `7..(5 + L - 1)` | `tag, length, value` attributes |
| following two bytes | CRC-16/CCITT-FALSE |
| final two bytes | tail magic `AA 55` |

See [the protocol specification](../SPEC.md) for field definitions, command
trailer, CRC coverage, reassembly, and confidence boundaries. Public tests
generate frames independently; they validate this repository's contract, not
every firmware revision.

## Implemented command surface

| Surface | Representative operations |
| --- | --- |
| `ms605` | interactive control, `read`, `monitor`, `clone`, `calibrate` |
| Configuration | sensitivity, zones, DND, device time, sub-sensor settings |
| Profiles | selective save and apply of writable sections |
| `ms605-driver` | scan, one-shot reads/writes, history, diagnostics |
| `tools/` | scanning, GATT enumeration, notification collection, offline btsnoop decoding |

The complete current command reference is in the
[main README](../../README.md#main-commands). Mutating operations remain
experimental and are limited to authorized hardware.

## Sanitized authorized-device evidence

Two retained private configuration profiles from authorized devices produced
this non-identifying summary:

| Observation | Result |
| --- | --- |
| Configuration profile format | Version 1 profile with independently decoded sections |
| Zone threshold records | 7 records in each retained profile |
| Common decoded sections | sensitivity, detect mode, zone enablement, zone thresholds, sub-sensor enablement, zones, and timing |
| Public identifiers | None; device names and addresses are excluded |

The corresponding operator action is equivalent to:

```console
$ uv run ms605 --address <redacted-device> read
# connect, request configuration, decode the sections above, then disconnect
```

This is a sanitized derivative of private configuration results, not a raw
terminal transcript or a claim that every firmware exposes the same fields.

## Installation: wrapper-free versus automated

The project uses `uv`; no global Python package install is required.

| Step | Without project scripts | With project scripts |
| --- | --- | --- |
| Create/sync environment | `uv sync` | `./scripts/setup.sh` verifies `uv`, removes a stale legacy `venv/`, then runs `uv sync` |
| Run the app | `uv run ms605 ...` | `./scripts/run.sh ...`, which bootstraps `.venv` when needed |
| Run offline verification | `uv run pytest -q` and `uv run ruff check .` | `./scripts/test.sh`, which bootstraps then runs both checks |

The scripts do not install `uv` implicitly. They show the official installation
command when it is absent, keeping bootstrap behavior explicit.

## Reproducibility snapshot

For the public-release preparation snapshot, the offline suite ran on macOS
with Python 3.12 and reported **121 passing tests**; Ruff reported no findings.
This validates the Python package and synthetic model without claiming universal
hardware coverage.
