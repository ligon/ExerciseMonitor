"""Importers for Dexcom Clarity (Stelo) CSV, xDrip+/Zukka/Nightscout JSON, and Garmin FIT files."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_zoneinfo(tz_name: Optional[str] = None) -> ZoneInfo:
    """Resolve an IANA timezone name (defaults to America/Los_Angeles or UTC fallback)."""
    name = (tz_name or "America/Los_Angeles").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return ZoneInfo("UTC")


def parse_iso_or_flexible_ts(ts_str: str, default_tz: Optional[str] = None) -> Optional[int]:
    """
    Parse ISO or common timestamp strings into epoch milliseconds.
    If the timestamp has no explicit timezone offset (naive, as in Dexcom Clarity CSV exports),
    interpret it in `default_tz` (or UTC if not specified).
    """
    if not ts_str:
        return None
    s = str(ts_str).strip()
    if not s:
        return None
    # Numeric epoch seconds or milliseconds
    if s.isdigit():
        val = int(s)
        return val * 1000 if val < 10_000_000_000 else val

    tz_obj = get_zoneinfo(default_tz) if default_tz else timezone.utc

    # Normalize trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz_obj)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=tz_obj)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def parse_glucose_value(raw_val: Any) -> Tuple[Optional[float], int]:
    """
    Parse a glucose value from Dexcom Clarity / Stelo CSV.
    Handles numeric strings as well as 'Low', '<70', 'Below 70', 'High', '>250', 'Above 250'.
    Returns (value_mgdl, is_clamped).
    """
    if raw_val is None:
        return None, 0
    s = str(raw_val).strip()
    if not s:
        return None, 0
    lower = s.lower()
    if lower in ("low", "<70", "below 70", "< 70"):
        return 69.0, 1
    if lower in ("high", ">250", "above 250", "> 250", ">400", "above 400"):
        return 251.0, 1
    try:
        val = float(s)
        if 20.0 <= val <= 600.0:
            return val, 0
    except ValueError:
        pass
    return None, 0


def parse_dexcom_clarity_csv(
    csv_text: str,
    tz_name: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse official Dexcom Clarity CSV exports (Stelo / G7) as well as generic BG CSV files.
    Naive timestamps in Clarity CSVs are interpreted in `tz_name` (if provided) so they align with UTC Garmin FIT files.
    Returns {"glucose": [...], "meals": [...]}.
    """
    # Strip BOM if present
    cleaned = csv_text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(cleaned))
    rows = list(reader)
    if not rows:
        return {"glucose": [], "meals": []}

    # Find header row (Dexcom Clarity usually has header on row 0, or after metadata lines)
    header_idx = 0
    header: List[str] = []
    for idx, row in enumerate(rows[:20]):
        joined = " ".join(c.lower() for c in row)
        if "timestamp" in joined or ("glucose" in joined and ("date" in joined or "time" in joined)):
            header_idx = idx
            header = [c.strip() for c in row]
            break

    if not header:
        header = [c.strip() for c in rows[0]]
        header_idx = 0

    col_map: Dict[str, int] = {}
    for i, col in enumerate(header):
        cl = col.lower()
        if "timestamp" in cl or cl in ("time", "date", "datetime", "date_time"):
            col_map.setdefault("timestamp", i)
        elif "event type" in cl or cl == "type":
            col_map["event_type"] = i
        elif "event subtype" in cl or "subtype" in cl or "notes" in cl or "description" in cl:
            col_map["event_subtype"] = i
        elif "glucose value" in cl or cl in ("glucose", "bg", "sgv", "value", "blood_glucose", "glucose_mgdl"):
            col_map["glucose"] = i
        elif "carb value" in cl or cl in ("carbs", "carbs_g", "carbohydrates"):
            col_map["carbs"] = i
        elif "rate of change" in cl or cl == "trend":
            col_map["trend"] = i

    glucose_list: List[Dict[str, Any]] = []
    meals_list: List[Dict[str, Any]] = []

    for row in rows[header_idx + 1 :]:
        if not row or all(not c.strip() for c in row):
            continue

        ts_idx = col_map.get("timestamp")
        if ts_idx is None or ts_idx >= len(row):
            continue
        ts_ms = parse_iso_or_flexible_ts(row[ts_idx], default_tz=tz_name)
        if ts_ms is None:
            continue

        ev_type = ""
        if "event_type" in col_map and col_map["event_type"] < len(row):
            ev_type = row[col_map["event_type"]].strip()

        ev_subtype = ""
        if "event_subtype" in col_map and col_map["event_subtype"] < len(row):
            ev_subtype = row[col_map["event_subtype"]].strip()

        bg_raw = row[col_map["glucose"]].strip() if "glucose" in col_map and col_map["glucose"] < len(row) else ""
        carbs_raw = row[col_map["carbs"]].strip() if "carbs" in col_map and col_map["carbs"] < len(row) else ""
        trend_raw = row[col_map["trend"]].strip() if "trend" in col_map and col_map["trend"] < len(row) else None

        ev_lower = ev_type.lower()

        # Check if this row is a glucose reading (EGV or generic CSV with glucose value)
        if ev_lower in ("egv", "glucose", "bg", "") and bg_raw:
            val, is_clamped = parse_glucose_value(bg_raw)
            if val is not None:
                glucose_list.append(
                    {
                        "timestamp_ms": ts_ms,
                        "value_mgdl": val,
                        "trend": trend_raw or None,
                        "source": "stelo_clarity",
                        "is_clamped": is_clamped,
                    }
                )

        # Check if this row is a Meal / Carb / Note event logged in the Stelo or Clarity app
        is_meal_event = ev_lower in ("carbs", "meal", "food", "nutrition", "note", "notes") or bool(carbs_raw)
        if is_meal_event:
            carbs_val: Optional[float] = None
            if carbs_raw:
                try:
                    carbs_val = float(carbs_raw)
                except ValueError:
                    carbs_val = None
            title = ev_subtype or (f"Meal ({int(carbs_val)}g carbs)" if carbs_val else "Stelo Logged Meal")
            meals_list.append(
                {
                    "timestamp_ms": ts_ms,
                    "title": title,
                    "notes": f"Imported from Dexcom Clarity ({ev_type})" if ev_type else "Imported from CSV",
                    "carbs_g": carbs_val,
                    "protein_g": None,
                    "fat_g": None,
                    "calories": None,
                    "tag": "meal",
                    "source": "stelo_clarity",
                }
            )

    return {"glucose": glucose_list, "meals": meals_list}


def parse_nightscout_or_xdrip_json(payload: Any, source: str = "xdrip_zukka") -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse xDrip+ / Zukka / Nightscout JSON payloads.
    Supports:
    - List of SGV entry dicts (`sgv`, `date` or `dateString`, `direction`)
    - List of treatment dicts (`carbs`, `notes`, `created_at` or `timestamp`, `eventType`)
    - Combined wrapper object `{"entries": [...], "treatments": [...]}`
    """
    items: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        if "entries" in payload or "treatments" in payload or "sgvs" in payload:
            items.extend(payload.get("entries", []))
            items.extend(payload.get("sgvs", []))
            items.extend(payload.get("treatments", []))
        else:
            items.append(payload)
    elif isinstance(payload, list):
        items.extend(payload)

    glucose_list: List[Dict[str, Any]] = []
    meals_list: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        ts_ms: Optional[int] = None
        for key in ("date", "mills", "timestamp_ms", "timestamp", " date"):
            if key in item and item[key] is not None:
                ts_ms = parse_iso_or_flexible_ts(str(item[key]))
                if ts_ms:
                    break
        if not ts_ms:
            for key in ("dateString", "created_at", "sysTime"):
                if key in item and item[key]:
                    ts_ms = parse_iso_or_flexible_ts(str(item[key]))
                    if ts_ms:
                        break
        if not ts_ms:
            continue

        # Glucose reading
        sgv_raw = item.get("sgv") or item.get("glucose") or item.get("value_mgdl") or item.get("bg")
        if sgv_raw is not None:
            val, is_clamped = parse_glucose_value(sgv_raw)
            if val is not None:
                glucose_list.append(
                    {
                        "timestamp_ms": ts_ms,
                        "value_mgdl": val,
                        "trend": item.get("direction") or item.get("trend"),
                        "source": source,
                        "is_clamped": is_clamped,
                    }
                )

        # Meal / Treatment entry
        carbs = item.get("carbs")
        notes = item.get("notes") or item.get("enteredBy") or ""
        ev_type = str(item.get("eventType", "")).lower()
        if (carbs is not None and float(carbs or 0) > 0) or "meal" in ev_type or "carb" in ev_type or (
            notes and sgv_raw is None
        ):
            carbs_f = float(carbs) if carbs is not None and str(carbs).strip() != "" else None
            title = str(item.get("notes") or "").strip() or (
                f"Meal ({int(carbs_f)}g carbs)" if carbs_f else "Logged Meal"
            )
            meals_list.append(
                {
                    "timestamp_ms": ts_ms,
                    "title": title,
                    "notes": str(notes),
                    "carbs_g": carbs_f,
                    "protein_g": float(item["protein"]) if item.get("protein") is not None else None,
                    "fat_g": float(item["fat"]) if item.get("fat") is not None else None,
                    "calories": None,
                    "tag": "meal",
                    "source": source,
                }
            )

    return {"glucose": glucose_list, "meals": meals_list}


def _semicircles_to_degrees(val: Optional[int]) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val) * (180.0 / (2**31))
    except (TypeError, ValueError):
        return None


def _dt_to_ms(dt_or_val: Any) -> Optional[int]:
    if dt_or_val is None:
        return None
    if isinstance(dt_or_val, datetime):
        if dt_or_val.tzinfo is None:
            dt_or_val = dt_or_val.replace(tzinfo=timezone.utc)
        return int(dt_or_val.timestamp() * 1000)
    return parse_iso_or_flexible_ts(str(dt_or_val))


def extract_fit_bytes(raw_bytes: bytes) -> bytes:
    """If raw_bytes is a ZIP archive (as returned by Garmin Connect ORIGINAL download), extract the .fit file."""
    if len(raw_bytes) >= 4 and raw_bytes[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".fit"):
                    return zf.read(name)
            # Fallback to first file in zip
            if zf.namelist():
                return zf.read(zf.namelist()[0])
    return raw_bytes


def parse_garmin_fit_bytes(
    raw_bytes: bytes,
    external_id: Optional[str] = None,
    title_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Decode a Garmin .fit file (or .zip containing a .fit file) using garmin-fit-sdk.
    Extracts workout summary, per-second track points (HR, speed, climb/altitude, cadence, power, grade, developer BG),
    and heart rate stream.
    """
    from garmin_fit_sdk import Decoder, Stream

    fit_bytes = extract_fit_bytes(raw_bytes)
    digest = hashlib.sha1(fit_bytes).hexdigest()[:12]
    ext_id = external_id or f"fit_{digest}"

    stream = Stream.from_byte_array(bytearray(fit_bytes))
    decoder = Decoder(stream)
    messages, errors = decoder.read()

    session_mesgs = messages.get("session_mesgs", [])
    record_mesgs = messages.get("record_mesgs", [])

    sport = "running"
    start_time_ms: Optional[int] = None
    duration_s = 0.0
    distance_m = 0.0
    total_ascent_m = 0.0
    total_descent_m = 0.0
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    avg_speed_mps: Optional[float] = None
    max_speed_mps: Optional[float] = None
    calories: Optional[float] = None

    if session_mesgs:
        sess = session_mesgs[0]
        sport = str(sess.get("sport", "running")).lower()
        start_time_ms = _dt_to_ms(sess.get("start_time") or sess.get("timestamp"))
        duration_s = float(sess.get("total_elapsed_time") or sess.get("total_timer_time") or 0.0)
        distance_m = float(sess.get("total_distance") or 0.0)
        total_ascent_m = float(sess.get("total_ascent") or 0.0)
        total_descent_m = float(sess.get("total_descent") or 0.0)
        if sess.get("avg_heart_rate") is not None:
            avg_hr = int(sess["avg_heart_rate"])
        if sess.get("max_heart_rate") is not None:
            max_hr = int(sess["max_heart_rate"])
        spd = sess.get("enhanced_avg_speed") if sess.get("enhanced_avg_speed") is not None else sess.get("avg_speed")
        if spd is not None:
            avg_speed_mps = float(spd)
        mspd = sess.get("enhanced_max_speed") if sess.get("enhanced_max_speed") is not None else sess.get("max_speed")
        if mspd is not None:
            max_speed_mps = float(mspd)
        if sess.get("total_calories") is not None:
            calories = float(sess["total_calories"])

    points: List[Dict[str, Any]] = []
    hr_readings: List[Dict[str, Any]] = []
    bg_readings: List[Dict[str, Any]] = []

    prev_alt: Optional[float] = None
    prev_dist: Optional[float] = None
    computed_ascent = 0.0
    computed_descent = 0.0

    for rec in record_mesgs:
        ts_ms = _dt_to_ms(rec.get("timestamp"))
        if ts_ms is None:
            continue
        if start_time_ms is None:
            start_time_ms = ts_ms

        hr = int(rec["heart_rate"]) if rec.get("heart_rate") is not None else None
        spd = rec.get("enhanced_speed") if rec.get("enhanced_speed") is not None else rec.get("speed")
        speed_mps = float(spd) if spd is not None else None
        alt = rec.get("enhanced_altitude") if rec.get("enhanced_altitude") is not None else rec.get("altitude")
        altitude_m = float(alt) if alt is not None else None
        dist = float(rec["distance"]) if rec.get("distance") is not None else None
        cad = int(rec["cadence"]) if rec.get("cadence") is not None else None
        pwr = int(rec["power"]) if rec.get("power") is not None else None
        lat = _semicircles_to_degrees(rec.get("position_lat"))
        lon = _semicircles_to_degrees(rec.get("position_long"))

        # Check developer fields (Connect IQ FitContributor blood glucose)
        dev_bg: Optional[float] = None
        for k, v in rec.items():
            kl = str(k).lower()
            if "glucose" in kl or kl in ("bg", "sgv", "cgm"):
                val, _ = parse_glucose_value(v)
                if val is not None:
                    dev_bg = val
                    break
        if dev_bg is None and isinstance(rec.get("developer_fields"), dict):
            for k, v in rec["developer_fields"].items():
                kl = str(k).lower()
                if "glucose" in kl or kl in ("bg", "sgv", "cgm"):
                    val, _ = parse_glucose_value(v)
                    if val is not None:
                        dev_bg = val
                        break

        grade_pct: Optional[float] = None
        if altitude_m is not None and prev_alt is not None:
            d_alt = altitude_m - prev_alt
            if d_alt > 0.15:
                computed_ascent += d_alt
            elif d_alt < -0.15:
                computed_descent += abs(d_alt)
            if dist is not None and prev_dist is not None and (dist - prev_dist) >= 2.0:
                grade_pct = round((d_alt / (dist - prev_dist)) * 100.0, 1)
                grade_pct = max(-45.0, min(45.0, grade_pct))
        if altitude_m is not None:
            prev_alt = altitude_m
        if dist is not None:
            prev_dist = dist

        elapsed_s = max(0.0, (ts_ms - start_time_ms) / 1000.0)
        points.append(
            {
                "timestamp_ms": ts_ms,
                "elapsed_s": elapsed_s,
                "heart_rate": hr,
                "speed_mps": speed_mps,
                "altitude_m": altitude_m,
                "distance_m": dist,
                "cadence": cad,
                "power_w": pwr,
                "grade_pct": grade_pct,
                "lat": lat,
                "lon": lon,
                "glucose_mgdl": dev_bg,
            }
        )
        if hr is not None:
            hr_readings.append({"timestamp_ms": ts_ms, "bpm": hr, "source": "garmin_fit"})
        if dev_bg is not None:
            bg_readings.append(
                {
                    "timestamp_ms": ts_ms,
                    "value_mgdl": dev_bg,
                    "trend": None,
                    "source": "fit_activity",
                    "is_clamped": 0,
                }
            )

    if start_time_ms is None:
        start_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if points:
        end_time_ms = points[-1]["timestamp_ms"]
        if duration_s <= 0:
            duration_s = max(1.0, (end_time_ms - start_time_ms) / 1000.0)
        if distance_m <= 0 and points[-1].get("distance_m"):
            distance_m = float(points[-1]["distance_m"])
        if total_ascent_m <= 0 and computed_ascent > 0:
            total_ascent_m = round(computed_ascent, 1)
        if total_descent_m <= 0 and computed_descent > 0:
            total_descent_m = round(computed_descent, 1)
        hrs = [p["heart_rate"] for p in points if p.get("heart_rate") is not None]
        if hrs and avg_hr is None:
            avg_hr = int(round(sum(hrs) / len(hrs)))
        if hrs and max_hr is None:
            max_hr = max(hrs)
        spds = [p["speed_mps"] for p in points if p.get("speed_mps") is not None]
        if spds and avg_speed_mps is None:
            avg_speed_mps = round(sum(spds) / len(spds), 3)
        if spds and max_speed_mps is None:
            max_speed_mps = round(max(spds), 3)
    else:
        end_time_ms = start_time_ms + int(duration_s * 1000)

    sport_label = sport.replace("_", " ").title()
    workout_dict = {
        "external_id": ext_id,
        "start_time_ms": start_time_ms,
        "end_time_ms": end_time_ms,
        "sport": sport,
        "title": title_override or f"Garmin {sport_label}",
        "distance_m": round(distance_m, 1),
        "duration_s": round(duration_s, 1),
        "total_ascent_m": round(total_ascent_m, 1),
        "total_descent_m": round(total_descent_m, 1),
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "avg_speed_mps": avg_speed_mps,
        "max_speed_mps": max_speed_mps,
        "calories": calories,
        "source": "garmin_fit",
    }

    return {
        "workout": workout_dict,
        "points": points,
        "heart_rate_readings": hr_readings,
        "glucose_readings": bg_readings,
        "errors": [str(e) for e in errors] if errors else [],
    }
