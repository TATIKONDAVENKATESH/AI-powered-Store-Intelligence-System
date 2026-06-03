from __future__ import annotations
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
import uuid

# All 8 event types from the challenge spec
EventType = Literal[
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
]

SeverityLevel = Literal["INFO", "WARN", "CRITICAL"]


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None   # integer for BILLING_QUEUE_JOIN, else null
    sku_zone: Optional[str] = None      # zone label from store_layout.json
    session_seq: int = 0                # ordinal position in visitor session


class StoreEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str                      # ISO-8601 UTC string
    zone_id: Optional[str] = None       # null for ENTRY/EXIT events
    dwell_ms: int = 0                   # duration; 0 for instantaneous events
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        """Reject events with unparseable timestamps."""
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        """Reject events with non-UUID event_ids."""
        uuid.UUID(v)
        return v


class IngestRequest(BaseModel):
    events: List[StoreEvent]

    @field_validator("events")
    @classmethod
    def max_500_events(cls, v: list) -> list:
        """Enforce the 500-event batch limit from the challenge spec."""
        if len(v) > 500:
            raise ValueError(f"Batch size {len(v)} exceeds maximum of 500 events")
        return v


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicates: int
    errors: List[str] = []


class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_seconds: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: List[ZoneDwell]
    queue_depth: int
    abandonment_rate: float
    total_transactions: int
    computed_at: str


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: List[FunnelStage]
    computed_at: str


class HeatmapZone(BaseModel):
    zone_id: str
    sku_zone: Optional[str]
    visit_frequency: int
    avg_dwell_seconds: float
    normalised_score: float
    data_confidence: bool   # False if fewer than 20 sessions in window


class HeatmapResponse(BaseModel):
    store_id: str
    zones: List[HeatmapZone]
    computed_at: str


class Anomaly(BaseModel):
    anomaly_type: str
    severity: SeverityLevel
    description: str
    suggested_action: str
    detected_at: str


class AnomalyResponse(BaseModel):
    store_id: str
    anomalies: List[Anomaly]
    computed_at: str


class CameraFeedStatus(BaseModel):
    camera_id: str
    last_event_at: Optional[str]
    stale: bool   # True if >10 min since last event


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    store_feeds: List[CameraFeedStatus]
    last_event_at: Optional[str]
    stale_feed: bool
    db_connected: bool
    checked_at: str