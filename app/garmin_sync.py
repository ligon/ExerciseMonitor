"""Garmin Connect API synchronization (24/7 Heart Rate & Original FIT Activity Files)."""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.db import get_conn, insert_or_update_meal, upsert_glucose_batch, upsert_heart_rate_batch, upsert_workout
from app.importers import get_zoneinfo, parse_garmin_fit_bytes, parse_iso_or_flexible_ts

TOKEN_DIR = os.environ.get(
    "GARMIN_TOKEN_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "exercise_monitor_garmin_tokens"),
)


def _extract_garmin_meals_for_day(
    api: Any,
    d_str: str,
    tz_name: Optional[str] = "America/Los_Angeles",
) -> List[Dict[str, Any]]:
    """
    Query Garmin Connect's nutrition endpoints (`get_nutrition_daily_meals` / `get_nutrition_daily_food_log`)
    for `d_str` (YYYY-MM-DD) and convert any logged meals into dashboard meal records.
    """
    tz_obj = get_zoneinfo(tz_name)
    day_dt = datetime.strptime(d_str, "%Y-%m-%d").replace(tzinfo=tz_obj)
    default_hours = {"breakfast": 8, "lunch": 12, "dinner": 19, "snack": 15}
    extracted: List[Dict[str, Any]] = []

    raw_items: List[Dict[str, Any]] = []
    for method_name in ("get_nutrition_daily_meals", "get_nutrition_daily_food_log"):
        fn = getattr(api, method_name, None)
        if not callable(fn):
            continue
        try:
            resp = fn(d_str)
            if isinstance(resp, list):
                raw_items.extend(x for x in resp if isinstance(x, dict))
            elif isinstance(resp, dict):
                for key in ("meals", "foodLogs", "dailyMeals", "entries", "items"):
                    if isinstance(resp.get(key), list):
                        raw_items.extend(x for x in resp[key] if isinstance(x, dict))
        except Exception:
            continue

    for item in raw_items:
        cals = item.get("calories") or item.get("totalCalories") or item.get("kilocalories")
        carbs = item.get("carbohydrates") or item.get("carbs") or item.get("totalCarbohydrates")
        protein = item.get("protein") or item.get("totalProtein")
        fat = item.get("fat") or item.get("totalFat")
        if not any(v for v in (cals, carbs, protein, fat)):
            continue

        meal_name = (
            item.get("mealName")
            or item.get("name")
            or item.get("title")
            or item.get("mealType")
            or "Garmin Logged Meal"
        )
        tag = str(item.get("mealType") or meal_name).lower()
        matched_tag = "meal"
        for k in ("breakfast", "lunch", "dinner", "snack"):
            if k in tag:
                matched_tag = k
                break

        ts_ms = None
        for ts_key in ("timestamp", "mealTime", "loggedAt", "startTimeGmt", "startTimeLocal", "createDate"):
            if item.get(ts_key):
                ts_ms = parse_iso_or_flexible_ts(str(item[ts_key]), default_tz=tz_name)
                if ts_ms:
                    break
        if not ts_ms:
            hour = default_hours.get(matched_tag, 13)
            ts_ms = int(day_dt.replace(hour=hour, minute=0).timestamp() * 1000)

        extracted.append(
            {
                "timestamp_ms": ts_ms,
                "title": str(meal_name).strip().title(),
                "notes": "Synced from Garmin Connect Nutrition",
                "carbs_g": float(carbs) if carbs is not None else None,
                "protein_g": float(protein) if protein is not None else None,
                "fat_g": float(fat) if fat is not None else None,
                "calories": float(cals) if cals is not None else None,
                "tag": matched_tag,
                "source": "garmin_nutrition",
            }
        )
    return extracted


def sync_garmin_connect(
    email: Optional[str],
    password: Optional[str],
    start_date_str: str,
    end_date_str: Optional[str] = None,
    tz_name: Optional[str] = "America/Los_Angeles",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Authenticate with Garmin Connect (using saved tokens or email/password),
    fetch 24/7 all-day heart rate for each date in [start_date_str, end_date_str],
    and download + parse original .fit files for all workouts in that date range.
    """
    from garminconnect import Garmin

    os.makedirs(TOKEN_DIR, exist_ok=True)
    end_date_str = end_date_str or start_date_str

    api = Garmin(email=email or None, password=password or None)
    try:
        api.login(TOKEN_DIR)
    except Exception as exc:
        raise RuntimeError(f"Garmin Connect authentication failed: {exc}") from exc

    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    total_hr_count = 0
    meals_synced = 0
    latest_hr_ts_ms: Optional[int] = None
    last_watch_upload_gmt: Optional[str] = None
    cur_dt = start_dt
    while cur_dt <= end_dt:
        d_str = cur_dt.strftime("%Y-%m-%d")
        try:
            hr_data = api.get_heart_rates(d_str)
            if (hr_data or {}).get("lastSyncTimestampGMT"):
                last_watch_upload_gmt = hr_data.get("lastSyncTimestampGMT")
            hr_values = (hr_data or {}).get("heartRateValues") or []
            batch: List[Dict[str, Any]] = []
            for pair in hr_values:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2 and pair[0] and pair[1]:
                    bpm = int(pair[1])
                    if 25 <= bpm <= 240:
                        ts_val = int(pair[0])
                        batch.append(
                            {
                                "timestamp_ms": ts_val,
                                "bpm": bpm,
                                "source": "garmin_daily",
                            }
                        )
                        if latest_hr_ts_ms is None or ts_val > latest_hr_ts_ms:
                            latest_hr_ts_ms = ts_val
            total_hr_count += upsert_heart_rate_batch(batch, db_path=db_path)
        except Exception:
            pass

        for g_meal in _extract_garmin_meals_for_day(api, d_str, tz_name=tz_name):
            insert_or_update_meal(g_meal, db_path=db_path)
            meals_synced += 1

        cur_dt += timedelta(days=1)

    workouts_synced = 0
    with get_conn(db_path) as conn:
        existing_ext_ids = {
            r["external_id"]
            for r in conn.execute("SELECT external_id FROM workouts WHERE external_id IS NOT NULL").fetchall()
        }

    try:
        activities = api.get_activities_by_date(
            start_dt.strftime("%Y-%m-%d"),
            end_dt.strftime("%Y-%m-%d"),
        )
        for act in activities or []:
            act_id = str(act.get("activityId"))
            act_name = act.get("activityName") or "Garmin Workout"
            if not act_id:
                continue

            # Check if this Garmin activity is a quick wrist-stamped symptom/event marker
            # (either a short <3 min 'other'/'health_snapshot'/'breathwork' activity, or named with '!' / symptom keyword)
            act_type_key = str((act.get("activityType") or {}).get("typeKey") or "").lower()
            act_dur_s = float(act.get("duration") or act.get("elapsedDuration") or 0.0)
            act_desc = str(act.get("description") or "").strip()
            name_lower = (act_name + " " + act_desc).lower()
            is_watch_event_marker = (
                act_name.strip().startswith("!")
                or any(
                    w in name_lower
                    for w in ("dizzy", "dizziness", "symptom", "lightheaded", "event", "marker", "unusual")
                )
                or (
                    0 < act_dur_s <= 180
                    and act_type_key in ("other", "generic", "health_snapshot", "breathwork", "unclassified")
                )
            )
            if is_watch_event_marker:
                ev_ts_ms = act.get("beginTimestamp")
                if not ev_ts_ms and act.get("startTimeGMT"):
                    ev_ts_ms = parse_iso_or_flexible_ts(str(act["startTimeGMT"]) + "Z")
                if ev_ts_ms:
                    clean_title = act_name.strip().lstrip("!").strip() or "Watch Event Marker"
                    insert_or_update_meal(
                        {
                            "timestamp_ms": int(ev_ts_ms),
                            "title": clean_title,
                            "notes": act_desc or f"Marked from Garmin watch ({act_type_key or 'activity'}, {int(round(act_dur_s))}s)",
                            "carbs_g": None,
                            "protein_g": None,
                            "fat_g": None,
                            "calories": None,
                            "tag": "symptom",
                            "source": "garmin_watch_marker",
                        },
                        db_path=db_path,
                    )
                    meals_synced += 1

            ext_id = f"garmin_{act_id}"
            if ext_id in existing_ext_ids:
                workouts_synced += 1
                continue
            raw_zip = api.download_activity(act_id, dl_fmt=api.ActivityDownloadFormat.ORIGINAL)
            parsed = parse_garmin_fit_bytes(
                raw_zip,
                external_id=ext_id,
                title_override=act_name,
            )
            upsert_workout(parsed["workout"], parsed["points"], db_path=db_path)
            if parsed["heart_rate_readings"]:
                total_hr_count += upsert_heart_rate_batch(parsed["heart_rate_readings"], db_path=db_path)
            if parsed["glucose_readings"]:
                upsert_glucose_batch(parsed["glucose_readings"], db_path=db_path)
            for lap_ev in parsed.get("manual_lap_events") or []:
                insert_or_update_meal(lap_ev, db_path=db_path)
                meals_synced += 1
            workouts_synced += 1
    except Exception:
        pass

    return {
        "heart_rate_points_synced": total_hr_count,
        "workouts_synced": workouts_synced,
        "meals_synced": meals_synced,
        "latest_hr_ts_ms": latest_hr_ts_ms,
        "last_watch_upload_gmt": last_watch_upload_gmt,
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date": end_dt.strftime("%Y-%m-%d"),
    }
