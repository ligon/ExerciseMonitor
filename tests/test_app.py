"""Unit and API integration tests for the Stelo + Garmin 24/7 Monitor."""

import os
import tempfile
import pytest
from fastapi.testclient import TestClient

from app.clarity_sync import _add_dashes, _decode_subject_id
from app.db import init_db
from app.importers import parse_dexcom_clarity_csv, parse_nightscout_or_xdrip_json
from app.main import app


@pytest.fixture()
def client():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = os.path.join(tmpdir, "test_monitor.db")
        os.environ["EXERCISE_MONITOR_DB"] = db_file
        init_db(db_file)
        with TestClient(app) as c:
            yield c


def test_parse_dexcom_clarity_csv():
    sample_csv = """Index,Timestamp (YYYY-MM-DDThh:mm:ss),Event Type,Event Subtype,Patient Info,Device Info,Source Device ID,Glucose Value (mg/dL),Insulin Value (u),Carb Value (grams),Duration (hh:mm:ss),Glucose Rate of Change (mg/dL/min),Transmitter Time (Long Integer)
1,2026-11-21T08:00:00,EGV,,,,Stelo,94,,,,,
2,2026-11-21T08:05:00,EGV,,,,Stelo,102,,,,,
3,2026-11-21T08:05:00,Carbs,Oatmeal & Berries,,,Stelo,,,52,,,
4,2026-11-21T08:35:00,EGV,,,,Stelo,Above 250,,,,,
5,2026-11-21T09:00:00,EGV,,,,Stelo,Below 70,,,,,
"""
    parsed = parse_dexcom_clarity_csv(sample_csv)
    assert len(parsed["glucose"]) == 4
    assert parsed["glucose"][0]["value_mgdl"] == 94.0
    assert parsed["glucose"][0]["is_clamped"] == 0
    assert parsed["glucose"][2]["value_mgdl"] == 251.0
    assert parsed["glucose"][2]["is_clamped"] == 1
    assert parsed["glucose"][3]["value_mgdl"] == 69.0
    assert parsed["glucose"][3]["is_clamped"] == 1

    assert len(parsed["meals"]) == 1
    assert parsed["meals"][0]["title"] == "Oatmeal & Berries"
    assert parsed["meals"][0]["carbs_g"] == 52.0


def test_parse_zukka_xdrip_nightscout_json():
    payload = {
        "entries": [
            {"sgv": 108, "date": 1795252800000, "direction": "Flat"},
            {"sgv": 134, "dateString": "2026-11-21T09:25:00Z", "direction": "FortyFiveUp"},
        ],
        "treatments": [
            {
                "eventType": "Meal Bolus",
                "created_at": "2026-11-21T09:00:00Z",
                "carbs": 40,
                "notes": "Sourdough Toast & Eggs",
            }
        ],
    }
    parsed = parse_nightscout_or_xdrip_json(payload)
    assert len(parsed["glucose"]) == 2
    assert parsed["glucose"][0]["value_mgdl"] == 108.0
    assert len(parsed["meals"]) == 1
    assert parsed["meals"][0]["title"] == "Sourdough Toast & Eggs"
    assert parsed["meals"][0]["carbs_g"] == 40.0


def test_timeline_and_demo_seeding_and_workout_detail(client: TestClient):
    dates_res = client.get("/api/dates")
    assert dates_res.status_code == 200
    d_info = dates_res.json()
    assert d_info["counts"]["glucose_total"] == 288
    assert d_info["counts"]["workouts_total"] == 1

    tl_res = client.get(f"/api/timeline?date={d_info['latest_date']}")
    assert tl_res.status_code == 200
    tl = tl_res.json()
    assert len(tl["glucose"]) == 288
    assert len(tl["heart_rate"]) == 720
    assert len(tl["meals"]) == 4
    assert len(tl["workouts"]) == 1

    # Verify postprandial meal spike calculation works
    breakfast = tl["meals"][0]
    assert breakfast["baseline_bg"] is not None
    assert breakfast["peak_bg"] > breakfast["baseline_bg"]
    assert breakfast["delta_bg"] > 30.0
    assert breakfast["time_to_peak_min"] > 0

    # Verify workout detail endpoint has speed, climb/altitude, HR, grade, and interpolated BG
    w_id = tl["workouts"][0]["id"]
    w_res = client.get(f"/api/workouts/{w_id}")
    assert w_res.status_code == 200
    w_detail = w_res.json()
    assert w_detail["workout"]["total_ascent_m"] > 200
    assert len(w_detail["points"]) > 100
    first_pt = w_detail["points"][5]
    assert first_pt["heart_rate"] is not None
    assert first_pt["speed_mps"] is not None
    assert first_pt["altitude_m"] is not None
    assert first_pt["glucose_mgdl"] is not None


def test_meal_crud_and_nightscout_webhook(client: TestClient):
    dates_info = client.get("/api/dates").json()
    day = dates_info["latest_date"]

    # Create a custom meal
    create_res = client.post(
        "/api/meals",
        json={
            "datetime_iso": f"{day}T16:00:00Z",
            "title": "Afternoon Trail Mix",
            "carbs_g": 24,
            "protein_g": 9,
            "fat_g": 14,
            "calories": 258,
            "tag": "snack",
        },
    )
    assert create_res.status_code == 200
    meal_id = create_res.json()["id"]

    # Push a live reading via the Zukka / xDrip+ Nightscout webhook
    wh_res = client.post(
        "/api/v1/entries",
        json=[{"sgv": 119, "dateString": f"{day}T16:15:00Z", "direction": "Flat"}],
    )
    assert wh_res.status_code == 200
    assert wh_res.json()[0]["imported"] == 1

    # Delete the meal
    del_res = client.delete(f"/api/meals/{meal_id}")
    assert del_res.status_code == 200


def test_clarity_share_code_helpers():
    assert _add_dashes("ABCD1234EFGH") == "ABCD-1234-EFGH"
    # Base64url of {"subjectId": "123456789"} is eyJzdWJqZWN0SWQiOiAiMTIzNDU2Nzg5In0
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWJqZWN0SWQiOiAiMTIzNDU2Nzg5In0.sig"
    assert _decode_subject_id(fake_jwt) == "123456789"


def test_sync_settings_and_unified_sync_unconfigured(client: TestClient):
    res = client.get("/api/sync/settings")
    assert res.status_code == 200
    cfg = res.json()
    assert cfg["auto_poll_minutes"] == 15

    # Unconfigured unified sync returns status="unconfigured"
    sync_res = client.post("/api/sync/all", json={"tz": "America/Los_Angeles"})
    assert sync_res.status_code == 200
    assert sync_res.json()["status"] == "unconfigured"

    # Save settings and verify password is not exposed in GET /api/sync/settings
    save_res = client.post(
        "/api/sync/settings",
        json={
            "clarity_share_code": "ABCD-1234-EFGH",
            "garmin_email": "runner@example.com",
            "garmin_password": "secret-password",
            "auto_poll_minutes": 5,
            "lookback_days": 7,
        },
    )
    assert save_res.status_code == 200
    updated = save_res.json()
    assert updated["clarity_share_code"] == "ABCD-1234-EFGH"
    assert updated["garmin_email"] == "runner@example.com"
    assert updated["has_garmin_password"] is True
    assert "garmin_password" not in updated
    assert updated["auto_poll_minutes"] == 5


def test_unlogged_meal_spike_detection(client: TestClient):
    day = client.get("/api/dates").json()["latest_date"]
    tl = client.get(f"/api/timeline?date={day}").json()
    # Delete the first logged meal (breakfast) and verify the unlogged spike detector flags it!
    breakfast_id = tl["meals"][0]["id"]
    client.delete(f"/api/meals/{breakfast_id}")

    tl_after = client.get(f"/api/timeline?date={day}").json()
    assert len(tl_after["unlogged_meal_candidates"]) >= 1
    assert tl_after["unlogged_meal_candidates"][0]["delta_bg"] >= 20.0



def test_clarity_phone_timezone_offset_detection():
    from app.clarity_sync import HOUR_MS, MIN_MS, _resolve_phone_utc_offset_ms

    now_ms = 1_795_368_000_000
    # Phone on Pacific (UTC-8), newest reading 4 min old, dashboard set to America/Denver (UTC-7 in Nov)
    pacific_latest = now_ms - 8 * HOUR_MS - 4 * MIN_MS
    assert _resolve_phone_utc_offset_ms([pacific_latest], "America/Denver", now_ms) == -8 * HOUR_MS
    # Dashboard timezone matches the phone -> no correction (keep DST-aware IANA parsing)
    assert _resolve_phone_utc_offset_ms([pacific_latest], "America/Los_Angeles", now_ms) is None
    # Historical export (newest reading days old) -> cannot infer
    assert _resolve_phone_utc_offset_ms([now_ms - 72 * HOUR_MS], "America/Denver", now_ms) is None


def test_workout_pauses_decoded_and_moving_time_used():
    from app.analytics import enrich_workout_detail

    w = {
        "start_time_ms": 0, "end_time_ms": 3_600_000, "duration_s": 3600.0, "timer_s": 1800.0,
        "total_ascent_m": 300.0, "pauses_json": "[[600000, 2400000]]",
    }
    out = enrich_workout_detail(w, [], [])["workout"]
    assert out["pauses"] == [[600000, 2400000]]
    assert "pauses_json" not in out
    assert out["vam_m_per_h"] == 600  # 300 m over 0.5 h moving, not 1 h elapsed
