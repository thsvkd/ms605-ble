"""ms605.errors -- exception hierarchy for the MS605 BLE driver."""

from __future__ import annotations


class MS605Error(Exception):
    """Base class for all MS605 driver errors."""


class FrameError(MS605Error, ValueError):
    """A frame is structurally malformed (bad magic, truncated, bad TLV
    length, or a field out of its byte range). Note: a CRC mismatch is
    reported via `ParsedFrame.crc_ok`, not by raising this."""


class MS605TimeoutError(MS605Error, TimeoutError):
    """A command got no response, or a push event didn't arrive in time."""


class MS605DeviceError(MS605Error):
    """The device replied with a non-zero status (tag 3) for a command."""

    def __init__(self, status: int, msg_id: int) -> None:
        self.status = status
        self.msg_id = msg_id
        super().__init__(f"device returned error status {status} for msgId={msg_id}")


class MS605ConnectionError(MS605Error):
    """A BLE/adapter-level failure, decorated with an actionable hint."""
