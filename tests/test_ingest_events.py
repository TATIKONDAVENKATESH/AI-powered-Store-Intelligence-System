# PROMPT: "Write pytest tests for pipeline/ingest_events.py. Cover: main() with valid
# JSONL, missing file path, HTTP error from API, batch splitting, empty file."
# CHANGES MADE: Mock urllib.request.urlopen to avoid real HTTP calls. Used tmp_path
# for JSONL fixture. Covered all branches: missing file, empty file, successful batch,
# HTTP URLError.

from __future__ import annotations
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline.ingest_events as ingest_mod


def _make_event(i: int = 0) -> dict:
    """Build a minimal valid event dict for the JSONL file."""
    import uuid
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": f"VIS_{i:04d}",
        "event_type": "ENTRY",
        "timestamp": "2026-04-10T10:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": i},
    }


def _write_jsonl(path: str, events: list) -> None:
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _mock_response(accepted=1, rejected=0, duplicates=0):
    """Build a mock urllib response object."""
    resp_data = json.dumps({
        "accepted": accepted,
        "rejected": rejected,
        "duplicates": duplicates,
    }).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = resp_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ── Missing file ──────────────────────────────────────────────────────────────

def test_main_missing_file_prints_and_returns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ingest_mod, "JSONL", str(tmp_path / "nonexistent.jsonl"))
    ingest_mod.main()  # should not raise
    captured = capsys.readouterr()
    assert "No events file" in captured.out


# ── Empty file ────────────────────────────────────────────────────────────────

def test_main_empty_file(tmp_path, monkeypatch, capsys):
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    monkeypatch.setattr(ingest_mod, "JSONL", str(jsonl))
    with patch("urllib.request.urlopen") as mock_urlopen:
        ingest_mod.main()
    mock_urlopen.assert_not_called()  # nothing to ingest
    captured = capsys.readouterr()
    assert "0 events" in captured.out


# ── Happy path: events ingested ───────────────────────────────────────────────

def test_main_ingests_events(tmp_path, monkeypatch, capsys):
    jsonl = tmp_path / "events.jsonl"
    _write_jsonl(str(jsonl), [_make_event(i) for i in range(3)])
    monkeypatch.setattr(ingest_mod, "JSONL", str(jsonl))
    monkeypatch.setattr(ingest_mod, "API_URL", "http://localhost:8000")

    with patch("urllib.request.urlopen", return_value=_mock_response(accepted=3)) as mock_url:
        ingest_mod.main()

    mock_url.assert_called_once()
    captured = capsys.readouterr()
    assert "3 events" in captured.out
    assert "accepted=3" in captured.out


# ── Batch splitting ───────────────────────────────────────────────────────────

def test_main_splits_into_batches(tmp_path, monkeypatch, capsys):
    """600 events with BATCH=500 → 2 HTTP calls."""
    jsonl = tmp_path / "big.jsonl"
    _write_jsonl(str(jsonl), [_make_event(i) for i in range(600)])
    monkeypatch.setattr(ingest_mod, "JSONL", str(jsonl))
    monkeypatch.setattr(ingest_mod, "BATCH", 500)

    with patch("urllib.request.urlopen", return_value=_mock_response(accepted=500)) as mock_url:
        ingest_mod.main()

    assert mock_url.call_count == 2  # ceil(600/500) = 2 batches


# ── HTTP error ────────────────────────────────────────────────────────────────

def test_main_http_error_printed(tmp_path, monkeypatch, capsys):
    import urllib.error
    jsonl = tmp_path / "events.jsonl"
    _write_jsonl(str(jsonl), [_make_event()])
    monkeypatch.setattr(ingest_mod, "JSONL", str(jsonl))

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        ingest_mod.main()

    captured = capsys.readouterr()
    assert "[ERROR]" in captured.out


# ── Total accepted printed ────────────────────────────────────────────────────

def test_main_prints_total_accepted(tmp_path, monkeypatch, capsys):
    jsonl = tmp_path / "events.jsonl"
    _write_jsonl(str(jsonl), [_make_event(i) for i in range(2)])
    monkeypatch.setattr(ingest_mod, "JSONL", str(jsonl))
    monkeypatch.setattr(ingest_mod, "BATCH", 500)

    with patch("urllib.request.urlopen", return_value=_mock_response(accepted=2)):
        ingest_mod.main()

    captured = capsys.readouterr()
    assert "Total accepted: 2" in captured.out