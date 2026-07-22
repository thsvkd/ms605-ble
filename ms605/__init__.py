"""ms605 -- BLE control driver for the Meross MS605 presence sensor.

Public API re-exported here for convenience::

    from ms605 import MS605, MS605Config, MS605Error

Submodules (import directly for lower-level access):
    ms605.protocol   -- frame build/parse, CRC, TLV tag constants
    ms605.models     -- decoded value dataclasses + encode/decode helpers
    ms605.errors     -- exception hierarchy
    ms605.discovery  -- advertisement matching, BLE error diagnostics
    ms605.driver     -- the MS605 class itself
"""

from .driver import MS605
from .errors import (
    FrameError,
    MS605ConnectionError,
    MS605DeviceError,
    MS605Error,
    MS605TimeoutError,
)
from .models import (
    ConfigProfile,
    LightSample,
    MS605Config,
    PresenceHistoryRecord,
    RadarOutputSnapshot,
    RadarZoneLive,
    SubSensorStatus,
    ZoneThreshold,
    decode_config,
    decode_radar_output,
)
from .protocol import (
    DetectMode,
    ParsedFrame,
    Sensitivity,
    build_command,
    parse_frame,
)

__all__ = [
    "MS605",
    "MS605Config",
    "ConfigProfile",
    "MS605Error",
    "FrameError",
    "MS605TimeoutError",
    "MS605DeviceError",
    "MS605ConnectionError",
    "ZoneThreshold",
    "SubSensorStatus",
    "PresenceHistoryRecord",
    "LightSample",
    "RadarZoneLive",
    "RadarOutputSnapshot",
    "Sensitivity",
    "DetectMode",
    "ParsedFrame",
    "build_command",
    "parse_frame",
    "decode_config",
    "decode_radar_output",
]
