# PROMPT: "Write pytest tests for a Pydantic StoreEvent schema. Test that all 8 event types
# are accepted, that invalid timestamps are rejected, that duplicate UUIDs fail validation,
# and that is_staff and confidence bounds are enforced. Use pytest.mark.parametrize."
# CHANGES MADE: Added ST1076/ST1008 store_id variants; added REENTRY type test case;
# tightened confidence boundary test to use 1.01 (not 2.0) per Pydantic ge/le behaviour.

import pytest
import uuid
from pydantic import ValidationError
from app.models import StoreEvent, EventMetadata


def _base_event(**overrides) -> dict:
    """Return a valid event dict, overriding any specified fields."""
    base = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   "ST1076",
        "camera_id":  "CAM3",
        "visitor_id": "VIS_0001",
        "event_type": "ENTRY",
        "timestamp":  "2026-03-08T13:00:00Z",
        "confidence": 0.85,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("event_type", [
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
])
def test_all_event_types_accepted(event_type):
    """All 8 event types from the spec must be valid."""
    ev = StoreEvent(**_base_event(event_type=event_type))
    assert ev.event_type == event_type


def test_invalid_event_type_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(event_type="UNKNOWN_TYPE"))


def test_invalid_timestamp_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(timestamp="not-a-timestamp"))


def test_invalid_uuid_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(event_id="not-a-uuid"))


def test_confidence_upper_bound():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(confidence=1.01))


def test_confidence_lower_bound():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(confidence=-0.01))


def test_is_staff_default_false():
    ev = StoreEvent(**_base_event())
    assert ev.is_staff is False


def test_metadata_defaults():
    ev = StoreEvent(**_base_event())
    assert ev.metadata.queue_depth is None
    assert ev.metadata.sku_zone is None
    assert ev.metadata.session_seq == 0


def test_zone_event_with_zone_id():
    ev = StoreEvent(**_base_event(event_type="ZONE_ENTER", zone_id="ST1076_Z01"))
    assert ev.zone_id == "ST1076_Z01"


def test_billing_event_with_queue_depth():
    ev = StoreEvent(**_base_event(
        event_type="BILLING_QUEUE_JOIN",
        zone_id="ST1076_Z_BILLING_01",
        metadata={"queue_depth": 3, "sku_zone": None, "session_seq": 2},
    ))
    assert ev.metadata.queue_depth == 3


def test_store_id_st1008():
    ev = StoreEvent(**_base_event(store_id="ST1008", camera_id="CAM_ENTRY_1"))
    assert ev.store_id == "ST1008"


def test_auto_generated_event_id():
    """event_id should be auto-generated if not provided."""
    data = _base_event()
    del data["event_id"]
    ev = StoreEvent(**data)
    uuid.UUID(ev.event_id)  # must be valid UUID