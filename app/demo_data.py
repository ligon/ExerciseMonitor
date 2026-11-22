"""Realistic 24-Hour Stelo Blood Glucose + Garmin HR + Meals + Hilly Trail Run/Ride Demo Generator."""

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import (
    clear_demo_data,
    insert_or_update_meal,
    upsert_glucose_batch,
    upsert_heart_rate_batch,
    upsert_workout,
)
from app.importers import get_zoneinfo


def seed_demo_day(
    date_str: Optional[str] = None,
    db_path: Optional[str] = None,
    tz_name: Optional[str] = "America/Los_Angeles",
) -> Dict[str, Any]:
    """
    Generate a realistic 24-hour dataset for `date_str` (YYYY-MM-DD in `tz_name`):
    - 288 5-minute Stelo blood glucose readings (with overnight stability, breakfast spike,
      afternoon hilly trail run with hepatic rise + aerobic dip, post-workout blunted lunch,
      and low-glycemic dinner).
    - 720 2-minute 24/7 Garmin heart rate readings.
    - 4 logged meals (Breakfast, Pre-Run Snack, Post-Run Recovery Lunch, Dinner).
    - 1 detailed 70-minute Garmin FIT workout ("Ridge Trail Climb & Run") with per-15s
      Speed, Altitude/Climb, Grade %, Cadence, Heart Rate, and BG.
    """
    clear_demo_data(db_path=db_path)

    tz_obj = get_zoneinfo(tz_name)
    if date_str:
        day_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz_obj)
    else:
        now = datetime.now(tz_obj)
        day_dt = datetime(now.year, now.month, now.day, tzinfo=tz_obj)

    day_start_ms = int(day_dt.timestamp() * 1000)

    def hm_to_ms(hour: int, minute: int = 0) -> int:
        return day_start_ms + (hour * 3600 + minute * 60) * 1000

    # 1. Seed 4 realistic meals
    meals = [
        {
            "timestamp_ms": hm_to_ms(7, 30),
            "title": "Oatmeal, Banana & Honey + Espresso",
            "notes": "High-glycemic carb breakfast to test morning glucose response",
            "carbs_g": 68.0,
            "protein_g": 12.0,
            "fat_g": 8.0,
            "calories": 392.0,
            "tag": "breakfast",
            "source": "demo",
        },
        {
            "timestamp_ms": hm_to_ms(11, 15),
            "title": "Half Energy Bar + Almond Butter",
            "notes": "30 mins before Ridge Trail Climb",
            "carbs_g": 22.0,
            "protein_g": 6.0,
            "fat_g": 9.0,
            "calories": 193.0,
            "tag": "pre_workout",
            "source": "demo",
        },
        {
            "timestamp_ms": hm_to_ms(13, 30),
            "title": "Grilled Chicken Rice Bowl & Avocado",
            "notes": "Post-workout lunch — muscles soak up glucose (blunted spike!)",
            "carbs_g": 62.0,
            "protein_g": 44.0,
            "fat_g": 18.0,
            "calories": 586.0,
            "tag": "post_workout",
            "source": "demo",
        },
        {
            "timestamp_ms": hm_to_ms(19, 0),
            "title": "Roasted Salmon, Quinoa & Asparagus",
            "notes": "Balanced protein/fat/fiber dinner",
            "carbs_g": 32.0,
            "protein_g": 42.0,
            "fat_g": 22.0,
            "calories": 494.0,
            "tag": "dinner",
            "source": "demo",
        },
    ]
    for m in meals:
        insert_or_update_meal(m, db_path=db_path)

    # 2. Seed 24-hour 5-minute Stelo Blood Glucose curve (288 readings)
    def bg_model_at_min(m_of_day: float) -> float:
        # Base overnight fasting glucose ~92 mg/dL with gentle dawn effect around 6:30am
        base = 91.0 + 3.5 * math.sin((m_of_day - 300) / 1440.0 * 2 * math.pi)
        dawn = 5.0 * math.exp(-((m_of_day - 405) / 60.0) ** 2)

        # Breakfast at 7:30 (450m): sharp spike +58 mg/dL peaking around 8:15 (495m)
        b_spike = 0.0
        if m_of_day >= 450:
            dt = m_of_day - 450
            b_spike = 61.0 * ((dt / 42.0) ** 1.6) * math.exp(1.6 * (1.0 - dt / 42.0)) if dt < 180 else 0.0

        # Pre-workout snack at 11:15 (675m) + Trail Run 11:45 - 12:55 (705m - 775m)
        # Mild pre-run rise, brief climb hepatic bump around 12:05, then steady aerobic uptake dip to ~83 mg/dL
        w_effect = 0.0
        if m_of_day >= 675:
            dt = m_of_day - 675
            w_effect += 16.0 * math.exp(-((dt - 30) / 22.0) ** 2)
        if m_of_day >= 705:
            dt_w = m_of_day - 705
            w_effect -= 13.0 * math.exp(-((dt_w - 58) / 30.0) ** 2)

        # Post-workout lunch at 13:30 (810m): despite 62g carbs, blunted +24 mg/dL response due to muscle GLUT4 uptake
        l_spike = 0.0
        if m_of_day >= 810:
            dt = m_of_day - 810
            l_spike = 25.0 * ((dt / 38.0) ** 1.5) * math.exp(1.5 * (1.0 - dt / 38.0)) if dt < 170 else 0.0

        # Dinner at 19:00 (1140m): gentle +19 mg/dL curve
        d_spike = 0.0
        if m_of_day >= 1140:
            dt = m_of_day - 1140
            d_spike = 19.0 * ((dt / 48.0) ** 1.5) * math.exp(1.5 * (1.0 - dt / 48.0)) if dt < 200 else 0.0

        ripple = 1.8 * math.sin(m_of_day * 0.23) + 1.1 * math.cos(m_of_day * 0.41)
        return round(max(65.0, min(240.0, base + dawn + b_spike + w_effect + l_spike + d_spike + ripple)), 1)

    glucose_batch: List[Dict[str, Any]] = []
    for step in range(288):
        m_of_day = step * 5
        ts_ms = day_start_ms + m_of_day * 60 * 1000
        val = bg_model_at_min(m_of_day)
        prev_val = bg_model_at_min(max(0, m_of_day - 5))
        rate = (val - prev_val) / 5.0
        trend = "Flat"
        if rate > 2.0:
            trend = "DoubleUp"
        elif rate > 1.0:
            trend = "SingleUp"
        elif rate > 0.4:
            trend = "FortyFiveUp"
        elif rate < -2.0:
            trend = "DoubleDown"
        elif rate < -1.0:
            trend = "SingleDown"
        elif rate < -0.4:
            trend = "FortyFiveDown"

        glucose_batch.append(
            {
                "timestamp_ms": ts_ms,
                "value_mgdl": val,
                "trend": trend,
                "source": "demo",
                "is_clamped": 0,
            }
        )
    upsert_glucose_batch(glucose_batch, db_path=db_path)

    # 3. Seed Detailed 70-minute Hilly Trail Run Workout (11:45 - 12:55, 705m to 775m)
    w_start_ms = hm_to_ms(11, 45)
    w_duration_s = 70 * 60  # 4200s
    w_end_ms = w_start_ms + w_duration_s * 1000

    workout_points: List[Dict[str, Any]] = []
    hr_batch: List[Dict[str, Any]] = []

    cum_dist = 0.0
    alt = 120.0
    total_ascent = 0.0
    total_descent = 0.0
    prev_alt = alt

    for elapsed in range(0, w_duration_s + 1, 15):
        ts_ms = w_start_ms + elapsed * 1000
        prog = elapsed / w_duration_s  # 0.0 to 1.0

        # Terrain profile:
        # 0..25 min (0.0..0.36): Sustained steep climb (+310m)
        # 25..40 min (0.36..0.57): Rolling ridge with short punchy climb (+110m)
        # 40..70 min (0.57..1.0): Fast technical descent (-420m)
        if prog < 0.36:
            grade_pct = 8.5 + 3.5 * math.sin(prog * 22.0)
        elif prog < 0.57:
            grade_pct = 2.5 + 6.0 * math.sin((prog - 0.36) * 35.0)
        else:
            grade_pct = -7.8 + 2.5 * math.cos((prog - 0.57) * 25.0)

        # Speed depends inversely on uphill grade (GAP adjustment)
        base_speed = 3.25  # m/s (~7.3 mph on flat)
        speed_mps = max(1.65, min(4.45, base_speed - 0.13 * grade_pct + 0.12 * math.sin(elapsed * 0.03)))
        if elapsed > 0:
            d_dist = speed_mps * 15.0
            cum_dist += d_dist
            d_alt = d_dist * (grade_pct / 100.0)
            alt += d_alt
            if d_alt > 0:
                total_ascent += d_alt
            else:
                total_descent += abs(d_alt)

        # Heart rate responds to speed + uphill grade + warm-up lag
        warmup_factor = 1.0 - 0.25 * math.exp(-elapsed / 240.0)
        effort_hr = 136.0 + 2.9 * max(0.0, grade_pct) + 6.5 * (speed_mps - 2.5)
        if grade_pct < -3.0:
            effort_hr -= 6.0
        hr = int(round(max(112.0, min(184.0, effort_hr * warmup_factor + 2.5 * math.sin(elapsed * 0.05)))))
        cadence = int(round(166 + 4 * (speed_mps - 2.5) - 0.6 * max(0.0, grade_pct)))
        power_w = int(round(max(130.0, 215.0 + 14.0 * grade_pct + 32.0 * (speed_mps - 2.8))))
        m_of_day = 705.0 + (elapsed / 60.0)

        workout_points.append(
            {
                "timestamp_ms": ts_ms,
                "elapsed_s": float(elapsed),
                "heart_rate": hr,
                "speed_mps": round(speed_mps, 2),
                "altitude_m": round(alt, 1),
                "distance_m": round(cum_dist, 1),
                "cadence": cadence,
                "power_w": power_w,
                "grade_pct": round(grade_pct, 1),
                "lat": round(37.7950 + 0.018 * math.sin(prog * math.pi), 6),
                "lon": round(-122.4520 + 0.022 * math.cos(prog * math.pi), 6),
                "glucose_mgdl": bg_model_at_min(m_of_day),
            }
        )
        prev_alt = alt

    hrs = [p["heart_rate"] for p in workout_points]
    spds = [p["speed_mps"] for p in workout_points]

    workout_summary = {
        "external_id": f"demo_trail_run_{day_dt.strftime('%Y%m%d')}",
        "start_time_ms": w_start_ms,
        "end_time_ms": w_end_ms,
        "sport": "trail_running",
        "title": "Ridge Trail Climb & Tempo Run",
        "distance_m": round(cum_dist, 1),
        "duration_s": float(w_duration_s),
        "total_ascent_m": round(total_ascent, 1),
        "total_descent_m": round(total_descent, 1),
        "avg_hr": int(round(sum(hrs) / len(hrs))),
        "max_hr": max(hrs),
        "avg_speed_mps": round(sum(spds) / len(spds), 2),
        "max_speed_mps": round(max(spds), 2),
        "calories": 745.0,
        "source": "demo",
    }
    upsert_workout(workout_summary, workout_points, db_path=db_path)

    # 4. Seed 24/7 All-Day Garmin Heart Rate (every 2 minutes)
    for step in range(720):
        m_of_day = step * 2
        ts_ms = day_start_ms + m_of_day * 60 * 1000
        if 705 <= m_of_day <= 775:
            # Interpolate from workout points
            idx = min(len(workout_points) - 1, int(round((m_of_day - 705) * 60 / 15)))
            bpm = workout_points[idx]["heart_rate"]
        elif m_of_day < 390 or m_of_day >= 1360:
            # Sleeping HR ~51-57 bpm
            bpm = int(round(53 + 3.0 * math.sin(m_of_day * 0.08)))
        elif 775 < m_of_day <= 840:
            # Post-exercise EPOC recovery
            dt_rec = m_of_day - 775
            bpm = int(round(68 + 38 * math.exp(-dt_rec / 24.0)))
        else:
            # Daytime resting / light activity ~63-78 bpm
            bpm = int(round(67 + 6.5 * math.sin(m_of_day * 0.14) + 3.5 * math.cos(m_of_day * 0.31)))

        hr_batch.append({"timestamp_ms": ts_ms, "bpm": bpm, "source": "demo"})

    upsert_heart_rate_batch(hr_batch, db_path=db_path)

    return {
        "date": day_dt.strftime("%Y-%m-%d"),
        "glucose_count": len(glucose_batch),
        "heart_rate_count": len(hr_batch),
        "meals_count": len(meals),
        "workout_title": workout_summary["title"],
    }
