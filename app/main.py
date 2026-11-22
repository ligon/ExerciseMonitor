"""FastAPI backend for the 24/7 Stelo Blood Glucose + Garmin Watch + Meal Monitor."""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.analytics import analyze_meals, compute_glucose_summary, detect_unlogged_meals, enrich_workout_detail
from app.clarity_sync import sync_dexcom_clarity_share_code
from app.db import (
    clear_demo_data,
    delete_meal,
    get_conn,
    get_sync_settings,
    init_db,
    insert_or_update_meal,
    save_sync_settings,
    update_sync_status,
    upsert_glucose_batch,
    upsert_heart_rate_batch,
    upsert_workout,
)
from app.demo_data import seed_demo_day
from app.garmin_sync import sync_garmin_connect
from app.importers import (
    get_zoneinfo,
    parse_dexcom_clarity_csv,
    parse_garmin_fit_bytes,
    parse_iso_or_flexible_ts,
    parse_meals_csv,
    parse_nightscout_or_xdrip_json,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Auto-seed demo day if DB has zero glucose and zero workout records
    with get_conn() as conn:
        g_cnt = conn.execute("SELECT COUNT(*) AS c FROM glucose_readings").fetchone()["c"]
        w_cnt = conn.execute("SELECT COUNT(*) AS c FROM workouts").fetchone()["c"]
    if g_cnt == 0 and w_cnt == 0:
        seed_demo_day()
    yield


app = FastAPI(
    title="Stelo + Garmin 24/7 Metabolic & Exercise Monitor",
    version="1.0.0",
    lifespan=lifespan,
)

os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class MealCreateRequest(BaseModel):
    timestamp_ms: Optional[int] = None
    datetime_iso: Optional[str] = None
    tz: Optional[str] = "America/Los_Angeles"
    title: Optional[str] = ""
    notes: Optional[str] = ""
    carbs_g: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    calories: Optional[float] = None
    tag: Optional[str] = "meal"


class GarminSyncRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    start_date: str
    end_date: Optional[str] = None


class ClaritySyncRequest(BaseModel):
    share_code: str
    start_date: str
    end_date: Optional[str] = None
    tz: Optional[str] = "America/Los_Angeles"


class SyncSettingsRequest(BaseModel):
    clarity_share_code: Optional[str] = None
    garmin_email: Optional[str] = None
    garmin_password: Optional[str] = None
    meals_csv_url: Optional[str] = None
    auto_poll_minutes: Optional[int] = None
    lookback_days: Optional[int] = None


class UnifiedSyncRequest(BaseModel):
    clarity_share_code: Optional[str] = None
    garmin_email: Optional[str] = None
    garmin_password: Optional[str] = None
    meals_csv_url: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    tz: Optional[str] = "America/Los_Angeles"
    auto_poll_minutes: Optional[int] = None
    lookback_days: Optional[int] = None


@app.get("/")
def serve_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/dates")
def get_available_dates(tz: str = Query(default="America/Los_Angeles")):
    """Return available dates (YYYY-MM-DD in selected timezone `tz`) that contain BG, HR, meal, or workout data."""
    tz_obj = get_zoneinfo(tz)
    with get_conn() as conn:
        ts_rows = conn.execute(
            """
            SELECT timestamp_ms AS ts FROM glucose_readings
            UNION ALL
            SELECT timestamp_ms AS ts FROM meals
            UNION ALL
            SELECT start_time_ms AS ts FROM workouts
            """
        ).fetchall()
        date_set = {
            datetime.fromtimestamp(int(r["ts"]) / 1000.0, tz=tz_obj).strftime("%Y-%m-%d")
            for r in ts_rows
            if r["ts"] is not None
        }
        dates = sorted(date_set, reverse=True)
        counts = {
            "glucose_total": conn.execute("SELECT COUNT(*) AS c FROM glucose_readings").fetchone()["c"],
            "hr_total": conn.execute("SELECT COUNT(*) AS c FROM heart_rate_readings").fetchone()["c"],
            "meals_total": conn.execute("SELECT COUNT(*) AS c FROM meals").fetchone()["c"],
            "workouts_total": conn.execute("SELECT COUNT(*) AS c FROM workouts").fetchone()["c"],
            "has_demo": conn.execute("SELECT COUNT(*) AS c FROM glucose_readings WHERE source='demo'").fetchone()["c"]
            > 0,
        }
        bg_bounds = conn.execute(
            "SELECT MIN(timestamp_ms) AS min_ts, MAX(timestamp_ms) AS max_ts FROM glucose_readings"
        ).fetchone()
        hr_bounds = conn.execute(
            "SELECT MIN(timestamp_ms) AS min_ts, MAX(timestamp_ms) AS max_ts FROM heart_rate_readings"
        ).fetchone()

    now_ms = int(datetime.now(tz_obj).timestamp() * 1000)
    max_candidates = [ts for ts in (bg_bounds["max_ts"], hr_bounds["max_ts"]) if ts is not None]
    min_candidates = [ts for ts in (bg_bounds["min_ts"], hr_bounds["min_ts"]) if ts is not None]
    latest_ts_ms = max(max_candidates) if max_candidates else now_ms
    earliest_ts_ms = min(min_candidates) if min_candidates else (now_ms - 86_400_000)
    earliest_bg_ts_ms = bg_bounds["min_ts"] or earliest_ts_ms

    today_str = datetime.now(tz_obj).strftime("%Y-%m-%d")
    if not dates:
        dates = [today_str]
    return {
        "dates": dates,
        "latest_date": dates[0],
        "latest_ts_ms": latest_ts_ms,
        "earliest_ts_ms": earliest_ts_ms,
        "earliest_bg_ts_ms": earliest_bg_ts_ms,
        "tz": str(tz_obj),
        "counts": counts,
    }


@app.get("/api/timeline")
def get_24h_timeline(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD date in selected timezone"),
    tz: str = Query(default="America/Los_Angeles", description="IANA timezone name"),
    start_ms: Optional[int] = Query(default=None),
    end_ms: Optional[int] = Query(default=None),
):
    """Fetch synchronized 24/7 timeline data (BG, HR, Meals with 2h spike analytics, Workouts) in `tz`."""
    tz_obj = get_zoneinfo(tz)
    if start_ms is None or end_ms is None:
        if not date:
            info = get_available_dates(tz=tz)
            date = info["latest_date"]
        try:
            day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=tz_obj)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD") from exc
        next_day = (day_start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(day_start.timestamp() * 1000)
        end_ms = int(next_day.timestamp() * 1000) - 1
    else:
        date = datetime.fromtimestamp(start_ms / 1000.0, tz=tz_obj).strftime("%Y-%m-%d")

    # Fetch slightly wider BG window (+2.5h) so late-evening meal 2h postprandial spikes are still computed
    bg_query_end = end_ms + 150 * 60 * 1000

    with get_conn() as conn:
        bg_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, timestamp_ms, value_mgdl, trend, source, is_clamped
                FROM glucose_readings
                WHERE timestamp_ms BETWEEN ? AND ?
                ORDER BY timestamp_ms ASC
                """,
                (start_ms, bg_query_end),
            ).fetchall()
        ]
        # If querying a multi-day range (> 48h), bucket heart rate to 2-minute intervals so scrolling remains 60fps
        span_ms = max(1, end_ms - start_ms)
        if span_ms > 2 * 86_400_000:
            bucket_ms = 120_000 if span_ms <= 7 * 86_400_000 else 300_000
            hr_rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT MIN(id) AS id,
                           (timestamp_ms / ?) * ? AS timestamp_ms,
                           CAST(ROUND(AVG(bpm)) AS INTEGER) AS bpm,
                           MIN(source) AS source
                    FROM heart_rate_readings
                    WHERE timestamp_ms BETWEEN ? AND ?
                    GROUP BY timestamp_ms / ?
                    ORDER BY timestamp_ms ASC
                    """,
                    (bucket_ms, bucket_ms, start_ms, end_ms, bucket_ms),
                ).fetchall()
            ]
        else:
            hr_rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, timestamp_ms, bpm, source
                    FROM heart_rate_readings
                    WHERE timestamp_ms BETWEEN ? AND ?
                    ORDER BY timestamp_ms ASC
                    """,
                    (start_ms, end_ms),
                ).fetchall()
            ]
        meal_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, timestamp_ms, title, notes, carbs_g, protein_g, fat_g, calories, tag, source
                FROM meals
                WHERE timestamp_ms BETWEEN ? AND ?
                ORDER BY timestamp_ms ASC
                """,
                (start_ms, end_ms),
            ).fetchall()
        ]
        workout_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT *
                FROM workouts
                WHERE end_time_ms >= ? AND start_time_ms <= ?
                ORDER BY start_time_ms ASC
                """,
                (start_ms, end_ms),
            ).fetchall()
        ]

        day_bg = [r for r in bg_rows if r["timestamp_ms"] <= end_ms]
        bg_summary = compute_glucose_summary(day_bg)
        analyzed_meals = analyze_meals(meal_rows, bg_rows, workout_rows)
        unlogged_candidates = detect_unlogged_meals(day_bg, meal_rows, workout_rows)

        enriched_workouts: List[Dict[str, Any]] = []
        for w in workout_rows:
            pts = [
                dict(p)
                for p in conn.execute(
                    "SELECT * FROM workout_points WHERE workout_id = ? ORDER BY timestamp_ms ASC",
                    (w["id"],),
                ).fetchall()
            ]
            detail = enrich_workout_detail(w, pts, bg_rows)
            enriched_workouts.append(detail["workout"])

    bpms = [int(r["bpm"]) for r in hr_rows]
    # Estimate resting HR as 10th percentile of 24h HR
    sorted_bpms = sorted(bpms)
    resting_hr = sorted_bpms[max(0, int(len(sorted_bpms) * 0.1))] if sorted_bpms else None
    hr_summary = {
        "count": len(bpms),
        "resting_hr": resting_hr,
        "avg_hr": int(round(sum(bpms) / len(bpms))) if bpms else None,
        "max_hr": max(bpms) if bpms else None,
        "min_hr": min(bpms) if bpms else None,
        "total_workout_min": int(round(sum(float(w.get("duration_s") or 0) for w in enriched_workouts) / 60.0)),
        "total_climb_m": round(sum(float(w.get("total_ascent_m") or 0) for w in enriched_workouts), 1),
        "total_distance_km": round(sum(float(w.get("distance_m") or 0) for w in enriched_workouts) / 1000.0, 2),
    }

    return {
        "date": date,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "glucose": day_bg,
        "heart_rate": hr_rows,
        "meals": analyzed_meals,
        "unlogged_meal_candidates": unlogged_candidates,
        "workouts": enriched_workouts,
        "glucose_summary": bg_summary,
        "hr_summary": hr_summary,
    }


@app.get("/api/workouts/{workout_id}")
def get_workout_detail(workout_id: int):
    """Return high-frequency workout track points (HR, speed, climb/altitude, grade, cadence, power, BG)."""
    with get_conn() as conn:
        w_row = conn.execute("SELECT * FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        if not w_row:
            raise HTTPException(status_code=404, detail="Workout not found")
        w = dict(w_row)
        pts = [
            dict(p)
            for p in conn.execute(
                "SELECT * FROM workout_points WHERE workout_id = ? ORDER BY timestamp_ms ASC",
                (workout_id,),
            ).fetchall()
        ]
        bg_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT timestamp_ms, value_mgdl
                FROM glucose_readings
                WHERE timestamp_ms BETWEEN ? AND ?
                ORDER BY timestamp_ms ASC
                """,
                (w["start_time_ms"] - 30 * 60 * 1000, w["end_time_ms"] + 90 * 60 * 1000),
            ).fetchall()
        ]
    return enrich_workout_detail(w, pts, bg_rows)


@app.post("/api/meals")
def create_or_update_meal_endpoint(req: MealCreateRequest):
    tz_obj = get_zoneinfo(req.tz)
    ts_ms = req.timestamp_ms
    if ts_ms is None and req.datetime_iso:
        ts_ms = parse_iso_or_flexible_ts(req.datetime_iso, default_tz=req.tz)
    if ts_ms is None:
        ts_ms = int(datetime.now(tz_obj).timestamp() * 1000)

    tag_val = (req.tag or "meal").strip()
    title_val = (req.title or "").strip()
    if not title_val:
        title_val = "Unusual Event / Symptom" if tag_val == "symptom" else "Logged Meal"

    meal_id = insert_or_update_meal(
        {
            "timestamp_ms": ts_ms,
            "title": title_val,
            "notes": req.notes or "",
            "carbs_g": req.carbs_g,
            "protein_g": req.protein_g,
            "fat_g": req.fat_g,
            "calories": req.calories,
            "tag": tag_val,
            "source": "manual",
        }
    )
    date_str = datetime.fromtimestamp(ts_ms / 1000.0, tz=tz_obj).strftime("%Y-%m-%d")
    return {"id": meal_id, "timestamp_ms": ts_ms, "date": date_str}


@app.delete("/api/meals/{meal_id}")
def delete_meal_endpoint(meal_id: int):
    deleted = delete_meal(meal_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Meal not found")
    return {"deleted": True, "id": meal_id}


@app.post("/api/import/clarity-csv")
async def import_clarity_csv(
    file: UploadFile = File(...),
    tz: str = Form(default="America/Los_Angeles"),
):
    """Import an official Dexcom Clarity (Stelo) CSV export or generic glucose CSV."""
    tz_obj = get_zoneinfo(tz)
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    parsed = parse_dexcom_clarity_csv(text, tz_name=tz)
    g_cnt = upsert_glucose_batch(parsed["glucose"])
    m_cnt = 0
    for m in parsed["meals"]:
        insert_or_update_meal(m)
        m_cnt += 1

    latest_date = None
    if parsed["glucose"]:
        max_ts = max(r["timestamp_ms"] for r in parsed["glucose"])
        latest_date = datetime.fromtimestamp(max_ts / 1000.0, tz=tz_obj).strftime("%Y-%m-%d")

    return {
        "glucose_imported": g_cnt,
        "meals_imported": m_cnt,
        "latest_date": latest_date,
    }


@app.post("/api/import/fit")
async def import_garmin_fit(
    file: UploadFile = File(...),
    tz: str = Form(default="America/Los_Angeles"),
):
    """Import a Garmin .fit activity file (or .zip archive containing a .fit file)."""
    tz_obj = get_zoneinfo(tz)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    title_hint = os.path.splitext(file.filename or "Garmin Workout")[0]
    try:
        parsed = parse_garmin_fit_bytes(raw, title_override=title_hint)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to decode FIT file: {exc}") from exc

    workout_id = upsert_workout(parsed["workout"], parsed["points"])
    hr_cnt = upsert_heart_rate_batch(parsed["heart_rate_readings"])
    bg_cnt = upsert_glucose_batch(parsed["glucose_readings"])
    for lap_ev in parsed.get("manual_lap_events") or []:
        insert_or_update_meal(lap_ev)
    w_date = datetime.fromtimestamp(parsed["workout"]["start_time_ms"] / 1000.0, tz=tz_obj).strftime("%Y-%m-%d")

    return {
        "workout_id": workout_id,
        "title": parsed["workout"]["title"],
        "sport": parsed["workout"]["sport"],
        "points_imported": len(parsed["points"]),
        "hr_imported": hr_cnt,
        "bg_imported": bg_cnt,
        "date": w_date,
    }


@app.post("/api/import/nightscout-json")
async def import_nightscout_json(file: UploadFile = File(...)):
    """Import xDrip+ / Zukka / Nightscout JSON export."""
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {exc}") from exc

    parsed = parse_nightscout_or_xdrip_json(payload, source="xdrip_zukka")
    g_cnt = upsert_glucose_batch(parsed["glucose"])
    m_cnt = 0
    for m in parsed["meals"]:
        insert_or_update_meal(m)
        m_cnt += 1

    latest_date = None
    if parsed["glucose"]:
        max_ts = max(r["timestamp_ms"] for r in parsed["glucose"])
        latest_date = datetime.fromtimestamp(max_ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "glucose_imported": g_cnt,
        "meals_imported": m_cnt,
        "latest_date": latest_date,
    }


@app.get("/api/v1/experiments/test")
@app.post("/api/v1/experiments/test")
@app.get("/api/v1/status")
@app.get("/api/v1/status.json")
async def nightscout_auth_test():
    """Nightscout credential test endpoint required by Zukka / xDrip4iOS (`NightscoutSyncManager`)."""
    return JSONResponse({"status": "ok", "name": "ExerciseMonitor", "apiEnabled": True})


@app.get("/api/v1/profile")
@app.post("/api/v1/profile")
@app.get("/api/v1/devicestatus")
@app.post("/api/v1/devicestatus")
@app.post("/api/v1/devicestatus.json")
async def nightscout_noop_endpoints():
    """Nightscout profile & devicestatus compatibility endpoints for Zukka / xDrip+."""
    return JSONResponse([])


@app.get("/api/v1/entries")
@app.get("/api/v1/entries.json")
async def nightscout_get_entries():
    return JSONResponse([])


@app.post("/api/v1/entries")
@app.post("/api/v1/entries.json")
async def nightscout_entries_webhook(request: Request):
    """
    Nightscout REST API v1 compatible webhook endpoint for Zukka (iPhone) and xDrip+ (Android).
    Point Nightscout URL in Zukka/xDrip+ to `http://<host>:8000`.
    """
    payload = await request.json()
    parsed = parse_nightscout_or_xdrip_json(payload, source="live_webhook")
    g_cnt = upsert_glucose_batch(parsed["glucose"])
    return JSONResponse([{"status": "ok", "imported": g_cnt}])


@app.get("/api/v1/treatments")
@app.get("/api/v1/treatments.json")
async def nightscout_get_treatments():
    return JSONResponse([])


@app.post("/api/v1/treatments")
@app.put("/api/v1/treatments")
@app.post("/api/v1/treatments.json")
async def nightscout_treatments_webhook(request: Request):
    """Nightscout REST API v1 compatible treatments (meals/carbs/notes) webhook for Zukka and xDrip+."""
    payload = await request.json()
    parsed = parse_nightscout_or_xdrip_json(payload, source="live_webhook")
    m_cnt = 0
    for m in parsed["meals"]:
        insert_or_update_meal(m)
        m_cnt += 1
    return JSONResponse([{"status": "ok", "imported": m_cnt}])


@app.post("/api/sync/garmin")
def garmin_sync_endpoint(req: GarminSyncRequest):
    if req.email or req.password:
        save_sync_settings({"garmin_email": req.email, "garmin_password": req.password})
    stored = get_sync_settings(include_secret=True)
    try:
        result = sync_garmin_connect(
            email=req.email or stored.get("garmin_email"),
            password=req.password or stored.get("garmin_password"),
            start_date_str=req.start_date,
            end_date_str=req.end_date,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sync/clarity")
def clarity_sync_endpoint(req: ClaritySyncRequest):
    if req.share_code:
        save_sync_settings({"clarity_share_code": req.share_code})
    try:
        return sync_dexcom_clarity_share_code(
            share_code=req.share_code,
            start_date_str=req.start_date,
            end_date_str=req.end_date,
            tz_name=req.tz,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sync/settings")
def get_sync_settings_endpoint():
    return get_sync_settings(include_secret=False)


@app.post("/api/sync/settings")
def save_sync_settings_endpoint(req: SyncSettingsRequest):
    return save_sync_settings(req.model_dump(exclude_none=True))


@app.post("/api/sync/all")
def unified_sync_all_endpoint(req: UnifiedSyncRequest):
    """
    One-button & background polling unified sync for both Dexcom Clarity (Stelo) and Garmin Connect.
    Uses saved credentials/share code from SQLite (updating them if new values are passed in `req`).
    """
    updates = {
        k: v
        for k, v in {
            "clarity_share_code": req.clarity_share_code,
            "garmin_email": req.garmin_email,
            "garmin_password": req.garmin_password,
            "meals_csv_url": req.meals_csv_url,
            "auto_poll_minutes": req.auto_poll_minutes,
            "lookback_days": req.lookback_days,
        }.items()
        if v is not None
    }
    if updates:
        save_sync_settings(updates)

    cfg = get_sync_settings(include_secret=True)
    tz_obj = get_zoneinfo(req.tz)
    now_dt = datetime.now(tz_obj)
    lookback = max(1, int(cfg.get("lookback_days") or 2))

    end_date = req.end_date or now_dt.strftime("%Y-%m-%d")
    start_date = req.start_date or (now_dt - timedelta(days=lookback - 1)).strftime("%Y-%m-%d")

    share_code = (cfg.get("clarity_share_code") or "").strip()
    garmin_email = (cfg.get("garmin_email") or "").strip()
    garmin_password = cfg.get("garmin_password") or ""
    meals_csv_url = (cfg.get("meals_csv_url") or "").strip()

    if not share_code and not garmin_email and not meals_csv_url:
        return {
            "status": "unconfigured",
            "configured": False,
            "detail": "Add your Dexcom Clarity Share Code and/or Garmin Connect login to enable 1-click & auto-polling sync.",
            "clarity": None,
            "garmin": None,
            "errors": [],
        }

    clarity_res = None
    garmin_res = None
    errors: List[str] = []
    parts: List[str] = []

    if share_code:
        try:
            clarity_res = sync_dexcom_clarity_share_code(
                share_code=share_code,
                start_date_str=start_date,
                end_date_str=end_date,
                tz_name=req.tz,
            )
            parts.append(f"Stelo: {clarity_res['glucose_imported']} BG, {clarity_res['meals_imported']} meals")
        except Exception as exc:
            errors.append(f"Stelo/Clarity: {exc}")

    if garmin_email:
        try:
            garmin_res = sync_garmin_connect(
                email=garmin_email,
                password=garmin_password,
                start_date_str=start_date,
                end_date_str=end_date,
                tz_name=req.tz,
            )
            g_meal_str = f", {garmin_res['meals_synced']} meals" if garmin_res.get("meals_synced") else ""
            latest_hr_str = ""
            if garmin_res.get("latest_hr_ts_ms"):
                hr_dt = datetime.fromtimestamp(garmin_res["latest_hr_ts_ms"] / 1000.0, tz=tz_obj)
                latest_hr_str = f" [watch synced thru {hr_dt.strftime('%H:%M')}]"
            parts.append(
                f"Garmin: {garmin_res['heart_rate_points_synced']} HR{latest_hr_str}, {garmin_res['workouts_synced']} workouts{g_meal_str}"
            )
        except Exception as exc:
            errors.append(f"Garmin: {exc}")

    if meals_csv_url:
        try:
            import requests as _req

            m_resp = _req.get(meals_csv_url, timeout=15)
            m_resp.raise_for_status()
            sheet_meals = parse_meals_csv(m_resp.text, tz_name=req.tz)
            for sm in sheet_meals:
                insert_or_update_meal(sm)
            parts.append(f"Meals Sheet: {len(sheet_meals)}")
        except Exception as exc:
            errors.append(f"Meals Sheet: {exc}")

    now_ms = int(now_dt.timestamp() * 1000)
    status = "ok" if not errors else ("partial" if parts else "error")
    detail = " • ".join(parts + errors) if (parts or errors) else "No changes"
    update_sync_status(now_ms, status, detail)

    return {
        "status": status,
        "configured": True,
        "last_sync_ms": now_ms,
        "detail": detail,
        "start_date": start_date,
        "end_date": end_date,
        "clarity": clarity_res,
        "garmin": garmin_res,
        "errors": errors,
    }


@app.post("/api/demo/seed")
def seed_demo_endpoint(
    date: Optional[str] = Query(default=None),
    tz: str = Query(default="America/Los_Angeles"),
):
    return seed_demo_day(date_str=date, tz_name=tz)


@app.delete("/api/demo")
def clear_demo_endpoint():
    clear_demo_data()
    return {"cleared": True}
