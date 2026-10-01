"""ms605 gui wire schema: web/openapi.json is not stale, ServerMessage is a
`type`-discriminated union of the 11 messages (`live` and `countdown` with
`seq: null`), ClientMessage of the 2 client messages, and every response
model's fields are all required on the wire (docs/GUI_API.md 5, 14.4)."""

from __future__ import annotations

import json
from pathlib import Path

import ms605.gui.schemas as schemas
from ms605.gui.schemas import (
    ClientMessage,
    GatherMessage,
    LiveMessage,
    LiveUnsubscribeMessage,
    Out,
    ServerMessage,
    openapi_document,
)

OPENAPI_JSON = Path(__file__).resolve().parent.parent / "web" / "openapi.json"
MESSAGE_TYPES = {
    "snapshot": "SnapshotMessage",
    "sensor": "SensorMessage",
    "sensor_removed": "SensorRemovedMessage",
    "sites": "SitesMessage",
    "pending": "PendingMessage",
    "gather": "GatherMessage",
    "notice": "NoticeMessage",
    "batch": "BatchMessage",
    "calibration_job": "CalibrationJobMessage",
    "live": "LiveMessage",
    "countdown": "CountdownMessage",
}
CLIENT_TYPES = {"live_subscribe": "LiveSubscribeMessage", "live_unsubscribe": "LiveUnsubscribeMessage"}


def test_openapi_json_is_up_to_date():
    expected = json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert OPENAPI_JSON.read_text(encoding="utf-8") == expected, "run: python -m ms605.gui.schemas web/openapi.json"


def test_server_message_is_discriminated_by_type():
    schema = openapi_document()["components"]["schemas"]["ServerMessage"]
    assert schema["discriminator"]["propertyName"] == "type"
    assert schema["discriminator"]["mapping"] == {k: f"#/components/schemas/{v}" for k, v in MESSAGE_TYPES.items()}
    assert len(schema["oneOf"]) == 11
    message = ServerMessage.model_validate(
        {"type": "gather", "seq": 3, "ts": 1.5, "data": {"gathering": True, "connecting": []}}
    )
    assert isinstance(message.root, GatherMessage)
    assert message.root.data.gathering is True


def test_response_fields_are_all_required():
    components = openapi_document()["components"]["schemas"]
    out_models = {
        name: model
        for name, model in vars(schemas).items()
        if isinstance(model, type) and issubclass(model, Out) and model is not Out
    }
    assert set(MESSAGE_TYPES.values()) <= set(out_models)
    for name in out_models:
        schema = components[name]
        assert set(schema.get("required", [])) == set(schema["properties"]), name


def test_transient_messages_have_a_required_null_seq():
    components = openapi_document()["components"]["schemas"]
    for name in ("LiveMessage", "CountdownMessage"):
        assert components[name]["properties"]["seq"]["type"] == "null", name
        assert "seq" in components[name]["required"], name
    live = ServerMessage.model_validate(
        {
            "type": "live", "seq": None, "ts": 1.0,
            "data": {"device_id": "01", "at": 1.0, "pir": None, "sub_sensor_presence": [], "zones": []},
        }
    )  # fmt: skip
    assert isinstance(live.root, LiveMessage) and live.root.seq is None


def test_client_message_is_discriminated_by_type():
    schema = openapi_document()["components"]["schemas"]["ClientMessage"]
    assert schema["discriminator"]["propertyName"] == "type"
    assert schema["discriminator"]["mapping"] == {k: f"#/components/schemas/{v}" for k, v in CLIENT_TYPES.items()}
    message = ClientMessage.model_validate_json('{"type": "live_unsubscribe", "data": {"device_ids": ["0a"]}}')
    assert isinstance(message.root, LiveUnsubscribeMessage) and message.root.data.device_ids == ["0a"]
