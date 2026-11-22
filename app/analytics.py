"""24/7 Metabolic, Meal Excursion, and Workout Performance + Glucose Analytics."""

import bisect
import math
from typing import Any, Dict, List, Optional


def interpolate_glucose_at(
    ts_ms: int,
    bg_ts: List[int],
    bg_vals: List[float],
    max_gap_ms: int = 25 * 60 * 1000,
) -> Optional[float]:
    """Linearly interpolate blood glucose at ts_ms from sorted (bg_ts, bg_vals) within max_gap_ms."""
    if not bg_ts:
        return None
    idx = bisect.bisect_left(bg_ts, ts_ms)
    if idx < len(bg_ts) and bg_ts[idx] == ts_ms:
        return round(bg_vals[idx], 1)
    if idx == 0:
        if bg_ts[0] - ts_ms <= 10 * 60 * 1000:
            return round(bg_vals[0], 1)
        return None
    if idx == len(bg_ts):
        if ts_ms - bg_ts[-1] <= 10 * 60 * 1000:
            return round(bg_vals[-1], 1)
        return None

    t0, t1 = bg_ts[idx - 1], bg_ts[idx]
    if (t1 - t0) > max_gap_ms:
        # Fallback to nearest if within 10 mins
        if (ts_ms - t0) <= 10 * 60 * 1000:
            return round(bg_vals[idx - 1], 1)
        if (t1 - ts_ms) <= 10 * 60 * 1000:
            return round(bg_vals[idx], 1)
        return None

    frac = (ts_ms - t0) / max(1, (t1 - t0))
    val = bg_vals[idx - 1] + frac * (bg_vals[idx] - bg_vals[idx - 1])
    return round(val, 1)


def compute_glucose_summary(glucose_readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute Stelo / CGM summary statistics (Mean, SD, CV%, GMI, Tight TIR 70-140, Standard TIR 70-180)."""
    if not glucose_readings:
        return {
            "count": 0,
            "mean_mgdl": None,
            "sd_mgdl": None,
            "cv_pct": None,
            "gmi_pct": None,
            "min_mgdl": None,
            "max_mgdl": None,
            "tir_70_140_pct": None,
            "tir_70_180_pct": None,
            "below_70_pct": None,
            "above_140_pct": None,
            "above_180_pct": None,
        }

    vals = [float(r["value_mgdl"]) for r in glucose_readings]
    n = len(vals)
    mean_val = sum(vals) / n
    variance = sum((v - mean_val) ** 2 for v in vals) / n if n > 1 else 0.0
    sd_val = math.sqrt(variance)
    cv_pct = (sd_val / mean_val * 100.0) if mean_val > 0 else 0.0
    # Jaeb Center GMI formula: 3.31 + 0.02392 * mean_mgdl
    gmi_pct = 3.31 + 0.02392 * mean_val

    in_70_140 = sum(1 for v in vals if 70.0 <= v <= 140.0)
    in_70_180 = sum(1 for v in vals if 70.0 <= v <= 180.0)
    below_70 = sum(1 for v in vals if v < 70.0)
    above_140 = sum(1 for v in vals if v > 140.0)
    above_180 = sum(1 for v in vals if v > 180.0)

    return {
        "count": n,
        "mean_mgdl": round(mean_val, 1),
        "sd_mgdl": round(sd_val, 1),
        "cv_pct": round(cv_pct, 1),
        "gmi_pct": round(gmi_pct, 2),
        "min_mgdl": round(min(vals), 1),
        "max_mgdl": round(max(vals), 1),
        "tir_70_140_pct": round(in_70_140 / n * 100.0, 1),
        "tir_70_180_pct": round(in_70_180 / n * 100.0, 1),
        "below_70_pct": round(below_70 / n * 100.0, 1),
        "above_140_pct": round(above_140 / n * 100.0, 1),
        "above_180_pct": round(above_180 / n * 100.0, 1),
    }


def analyze_meals(
    meals: List[Dict[str, Any]],
    glucose_readings: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Calculate postprandial (2-hour post-meal) glucose response for each meal:
    baseline BG, 2h peak BG, delta spike (+mg/dL), time to peak (min), 2h recovery BG,
    incremental AUC, and nearby exercise context.
    """
    sorted_bg = sorted(glucose_readings, key=lambda r: r["timestamp_ms"])
    bg_ts = [int(r["timestamp_ms"]) for r in sorted_bg]
    bg_vals = [float(r["value_mgdl"]) for r in sorted_bg]

    results: List[Dict[str, Any]] = []
    for meal in meals:
        m_ts = int(meal["timestamp_ms"])
        baseline_bg = interpolate_glucose_at(m_ts, bg_ts, bg_vals)

        # Find readings inside [m_ts, m_ts + 2h]
        window_end = m_ts + 120 * 60 * 1000
        i0 = bisect.bisect_left(bg_ts, m_ts)
        i1 = bisect.bisect_right(bg_ts, window_end)
        window_pts = [(bg_ts[i], bg_vals[i]) for i in range(i0, i1)]

        if baseline_bg is None and window_pts:
            if (window_pts[0][0] - m_ts) <= 20 * 60 * 1000:
                baseline_bg = round(window_pts[0][1], 1)

        peak_bg: Optional[float] = None
        peak_ts: Optional[int] = None
        time_to_peak_min: Optional[int] = None
        delta_bg: Optional[float] = None
        two_hour_bg = interpolate_glucose_at(window_end, bg_ts, bg_vals)
        iauc: Optional[float] = None

        if window_pts:
            peak_ts, peak_val = max(window_pts, key=lambda x: x[1])
            peak_bg = round(peak_val, 1)
            time_to_peak_min = int(round((peak_ts - m_ts) / 60000.0))
            if baseline_bg is not None:
                delta_bg = round(max(0.0, peak_bg - baseline_bg), 1)
                # Trapezoidal incremental AUC above baseline (mg/dL * min)
                pts = [(m_ts, baseline_bg)] + window_pts
                area = 0.0
                for k in range(1, len(pts)):
                    dt_min = (pts[k][0] - pts[k - 1][0]) / 60000.0
                    y0 = max(0.0, pts[k - 1][1] - baseline_bg)
                    y1 = max(0.0, pts[k][1] - baseline_bg)
                    area += 0.5 * (y0 + y1) * dt_min
                iauc = round(area, 1)

        classification = "no_data"
        if delta_bg is not None and peak_bg is not None:
            if delta_bg <= 30.0 and peak_bg <= 140.0:
                classification = "optimal"
            elif delta_bg <= 50.0 and peak_bg <= 160.0:
                classification = "moderate"
            else:
                classification = "spike"

        # Check workout proximity (within 2.5h before or 2h after meal)
        workout_context: Optional[str] = None
        for w in workouts:
            w_start = int(w["start_time_ms"])
            w_end = int(w["end_time_ms"])
            if w_start <= m_ts <= w_end:
                workout_context = f"During {w['title']}"
                break
            elif 0 < (m_ts - w_end) <= 150 * 60 * 1000:
                mins_after = int(round((m_ts - w_end) / 60000.0))
                workout_context = f"{mins_after}m after {w['title']}"
                break
            elif 0 < (w_start - m_ts) <= 120 * 60 * 1000:
                mins_before = int(round((w_start - m_ts) / 60000.0))
                workout_context = f"{mins_before}m before {w['title']}"
                break

        enriched = dict(meal)
        enriched.update(
            {
                "baseline_bg": baseline_bg,
                "peak_bg": peak_bg,
                "delta_bg": delta_bg,
                "time_to_peak_min": time_to_peak_min,
                "two_hour_bg": two_hour_bg,
                "iauc": iauc,
                "classification": classification,
                "workout_context": workout_context,
            }
        )
        results.append(enriched)

    return results


def detect_unlogged_meals(
    glucose_readings: List[Dict[str, Any]],
    meals: List[Dict[str, Any]],
    workouts: List[Dict[str, Any]],
    min_rise_mgdl: float = 20.0,
) -> List[Dict[str, Any]]:
    """
    Automatically detect probable unlogged meals from 5-minute Stelo BG excursions
    (rises >= `min_rise_mgdl` within 65 minutes that are not already covered by a logged meal
    within [-75m, +60m] or an active high-intensity workout hepatic rise).
    """
    sorted_bg = sorted(glucose_readings, key=lambda r: r["timestamp_ms"])
    n = len(sorted_bg)
    if n < 5:
        return []

    meal_times = [int(m["timestamp_ms"]) for m in meals]
    candidates: List[Dict[str, Any]] = []
    last_candidate_ts = -1

    for i in range(n - 3):
        t0 = int(sorted_bg[i]["timestamp_ms"])
        v0 = float(sorted_bg[i]["value_mgdl"])

        # Must be a local trough/inflection point (not already halfway up a spike)
        if i > 0 and float(sorted_bg[i - 1]["value_mgdl"]) < v0 - 2.0:
            continue

        # Skip if within 2 hours of a previously detected candidate
        if last_candidate_ts > 0 and (t0 - last_candidate_ts) < 110 * 60 * 1000:
            continue

        # Skip if an existing logged meal is already near t0 ([-60m, +75m])
        if any(-60 * 60 * 1000 <= (t0 - mt) <= 75 * 60 * 1000 for mt in meal_times):
            continue

        # Skip if t0 falls inside an active workout
        if any(int(w["start_time_ms"]) - 15 * 60 * 1000 <= t0 <= int(w["end_time_ms"]) for w in workouts):
            continue

        # Look ahead up to 75 minutes for peak
        peak_val = v0
        peak_ts = t0
        for j in range(i + 1, n):
            tj = int(sorted_bg[j]["timestamp_ms"])
            if (tj - t0) > 75 * 60 * 1000:
                break
            vj = float(sorted_bg[j]["value_mgdl"])
            if vj > peak_val:
                peak_val = vj
                peak_ts = tj

        delta = round(peak_val - v0, 1)
        if delta >= min_rise_mgdl and (peak_ts - t0) >= 15 * 60 * 1000:
            candidates.append(
                {
                    "estimated_meal_ts_ms": t0,
                    "baseline_bg": round(v0, 1),
                    "peak_bg": round(peak_val, 1),
                    "delta_bg": delta,
                    "time_to_peak_min": int(round((peak_ts - t0) / 60000.0)),
                }
            )
            last_candidate_ts = t0

    return candidates


def enrich_workout_detail(
    workout: Dict[str, Any],
    points: List[Dict[str, Any]],
    glucose_readings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aligns 5-minute Stelo BG readings with high-frequency Garmin FIT workout points
    (HR, speed, climb/altitude, cadence, power, grade) and computes workout metabolic summary.
    """
    sorted_bg = sorted(glucose_readings, key=lambda r: r["timestamp_ms"])
    bg_ts = [int(r["timestamp_ms"]) for r in sorted_bg]
    bg_vals = [float(r["value_mgdl"]) for r in sorted_bg]

    enriched_pts: List[Dict[str, Any]] = []
    workout_bgs: List[float] = []

    for pt in points:
        p = dict(pt)
        if p.get("glucose_mgdl") is None:
            p["glucose_mgdl"] = interpolate_glucose_at(int(p["timestamp_ms"]), bg_ts, bg_vals)
        if p.get("glucose_mgdl") is not None:
            workout_bgs.append(float(p["glucose_mgdl"]))
        enriched_pts.append(p)

    w_start = int(workout["start_time_ms"])
    w_end = int(workout["end_time_ms"])
    start_bg = interpolate_glucose_at(w_start, bg_ts, bg_vals)
    end_bg = interpolate_glucose_at(w_end, bg_ts, bg_vals)
    post_1h_bg = interpolate_glucose_at(w_end + 60 * 60 * 1000, bg_ts, bg_vals)

    if not workout_bgs:
        i0 = bisect.bisect_left(bg_ts, w_start)
        i1 = bisect.bisect_right(bg_ts, w_end)
        workout_bgs = [bg_vals[i] for i in range(i0, i1)]

    duration_h = max(0.01, float(workout.get("duration_s") or 0.0) / 3600.0)
    ascent_m = float(workout.get("total_ascent_m") or 0.0)
    vam_m_per_h = round(ascent_m / duration_h, 0) if ascent_m > 0 else 0.0

    summary = dict(workout)
    summary.update(
        {
            "start_bg": start_bg,
            "end_bg": end_bg,
            "delta_bg": round(end_bg - start_bg, 1) if (start_bg is not None and end_bg is not None) else None,
            "min_bg": round(min(workout_bgs), 1) if workout_bgs else None,
            "max_bg": round(max(workout_bgs), 1) if workout_bgs else None,
            "avg_bg": round(sum(workout_bgs) / len(workout_bgs), 1) if workout_bgs else None,
            "post_1h_bg": post_1h_bg,
            "vam_m_per_h": vam_m_per_h,
        }
    )

    return {"workout": summary, "points": enriched_pts}
