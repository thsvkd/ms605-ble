"""ms605 gui wire schema: web/openapi.json is not stale, ServerMessage is a
`type`-discriminated union of the 7 messages, and every response model's
fields are all required on the wire (docs/GUI_API.md sections 5, 6.7)."""

from __future__ import annotations

import json
from pathlib import Path

import ms605.gui.schemas as schemas
from ms605.gui.schemas import GatherMessage, Out, ServerMessage, openapi_document

OPENAPI_JSON = Path(__file__).resolve().parent.parent / "web" / "openapi.json"
MESSAGE_TYPES = {
    "snapshot": "SnapshotMessage",
    "sensor": "SensorMessage",
    "sensor_removed": "SensorRemovedMessage",
    "sites": "SitesMessage",
    "pending": "PendingMessage",
    "gather": "GatherMessage",
    "notice": "NoticeMessage",
}


def test_openapi_json_is_up_to_date():
    expected = json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert OPENAPI_JSON.read_text(encoding="utf-8") == expected, "run: python -m ms605.gui.schemas web/openapi.json"


def test_server_message_is_discriminated_by_type():
    schema = openapi_document()["components"]["schemas"]["ServerMessage"]
    assert schema["discriminator"]["propertyName"] == "type"
    assert schema["discriminator"]["mapping"] == {k: f"#/components/schemas/{v}" for k, v in MESSAGE_TYPES.items()}
    assert len(schema["oneOf"]) == 7
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
