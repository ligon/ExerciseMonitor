"""SQLite database layer for 24/7 Blood Glucose, Garmin HR, Meals, and FIT Workouts."""

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = os.environ.get(
    "EXERCISE_MONITOR_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "monitor.db"),
)


def get_db_path() -> str:
    return os.environ.get("EXERCISE_MONITOR_DB", DEFAULT_DB_PATH)


@contextmanager
def get_conn(db_path: Optional[str] = None):
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS glucose_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms INTEGER NOT NULL,
                value_mgdl REAL NOT NULL,
                trend TEXT,
                source TEXT NOT NULL DEFAULT 'stelo_clarity',
                is_clamped INTEGER NOT NULL DEFAULT 0,
                UNIQUE(timestamp_ms, source)
            );
            CREATE INDEX IF NOT EXISTS idx_glucose_ts ON glucose_readings(timestamp_ms);

            CREATE TABLE IF NOT EXISTS heart_rate_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms INTEGER NOT NULL,
                bpm INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'garmin_daily',
                UNIQUE(timestamp_ms, source)
            );
            CREATE INDEX IF NOT EXISTS idx_hr_ts ON heart_rate_readings(timestamp_ms);

            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms INTEGER NOT NULL,
                title TEXT NOT NULL,
                notes TEXT DEFAULT '',
                carbs_g REAL,
                protein_g REAL,
                fat_g REAL,
                calories REAL,
                tag TEXT DEFAULT 'meal',
                source TEXT NOT NULL DEFAULT 'manual',
                UNIQUE(timestamp_ms, title)
            );
            CREATE INDEX IF NOT EXISTS idx_meals_ts ON meals(timestamp_ms);

            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE,
                start_time_ms INTEGER NOT NULL,
                end_time_ms INTEGER NOT NULL,
                sport TEXT NOT NULL DEFAULT 'running',
                title TEXT NOT NULL,
                distance_m REAL DEFAULT 0.0,
                duration_s REAL DEFAULT 0.0,
                total_ascent_m REAL DEFAULT 0.0,
                total_descent_m REAL DEFAULT 0.0,
                avg_hr INTEGER,
                max_hr INTEGER,
                avg_speed_mps REAL,
                max_speed_mps REAL,
                calories REAL,
                source TEXT NOT NULL DEFAULT 'garmin_fit'
            );
            CREATE INDEX IF NOT EXISTS idx_workouts_time ON workouts(start_time_ms, end_time_ms);

            CREATE TABLE IF NOT EXISTS workout_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
                timestamp_ms INTEGER NOT NULL,
                elapsed_s REAL NOT NULL,
                heart_rate INTEGER,
                speed_mps REAL,
                altitude_m REAL,
                distance_m REAL,
                cadence INTEGER,
                power_w INTEGER,
                grade_pct REAL,
                lat REAL,
                lon REAL,
                glucose_mgdl REAL
            );
            CREATE INDEX IF NOT EXISTS idx_wpoints_workout ON workout_points(workout_id, timestamp_ms);

            CREATE TABLE IF NOT EXISTS sync_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                clarity_share_code TEXT DEFAULT '',
                garmin_email TEXT DEFAULT '',
                garmin_password TEXT DEFAULT '',
                meals_csv_url TEXT DEFAULT '',
                auto_poll_minutes INTEGER NOT NULL DEFAULT 15,
                lookback_days INTEGER NOT NULL DEFAULT 2,
                last_sync_ms INTEGER,
                last_sync_status TEXT DEFAULT '',
                last_sync_detail TEXT DEFAULT ''
            );
            INSERT OR IGNORE INTO sync_settings (id, auto_poll_minutes, lookback_days)
            VALUES (1, 15, 2);
            """
        )
        # Ensure meals_csv_url column exists on older databases
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(sync_settings)").fetchall()]
        if "meals_csv_url" not in cols:
            conn.execute("ALTER TABLE sync_settings ADD COLUMN meals_csv_url TEXT DEFAULT ''")
        # Pause-aware workouts: moving (timer) time + list of [pause_start_ms, pause_end_ms]
        w_cols = [r["name"] for r in conn.execute("PRAGMA table_info(workouts)").fetchall()]
        if "timer_s" not in w_cols:
            conn.execute("ALTER TABLE workouts ADD COLUMN timer_s REAL")
        if "pauses_json" not in w_cols:
            conn.execute("ALTER TABLE workouts ADD COLUMN pauses_json TEXT DEFAULT '[]'")


def upsert_glucose_batch(readings: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    if not readings:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO glucose_readings (timestamp_ms, value_mgdl, trend, source, is_clamped)
            VALUES (:timestamp_ms, :value_mgdl, :trend, :source, :is_clamped)
            ON CONFLICT(timestamp_ms, source) DO UPDATE SET
                value_mgdl=excluded.value_mgdl,
                trend=COALESCE(excluded.trend, glucose_readings.trend),
                is_clamped=excluded.is_clamped
            """,
            [
                {
                    "timestamp_ms": int(r["timestamp_ms"]),
                    "value_mgdl": float(r["value_mgdl"]),
                    "trend": r.get("trend"),
                    "source": r.get("source", "stelo_clarity"),
                    "is_clamped": int(r.get("is_clamped", 0)),
                }
                for r in readings
            ],
        )
        return len(readings)


def upsert_heart_rate_batch(readings: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    if not readings:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO heart_rate_readings (timestamp_ms, bpm, source)
            VALUES (:timestamp_ms, :bpm, :source)
            ON CONFLICT(timestamp_ms, source) DO UPDATE SET
                bpm=excluded.bpm
            """,
            [
                {
                    "timestamp_ms": int(r["timestamp_ms"]),
                    "bpm": int(r["bpm"]),
                    "source": r.get("source", "garmin_daily"),
                }
                for r in readings
            ],
        )
        return len(readings)


def insert_or_update_meal(meal: Dict[str, Any], db_path: Optional[str] = None) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO meals (timestamp_ms, title, notes, carbs_g, protein_g, fat_g, calories, tag, source)
            VALUES (:timestamp_ms, :title, :notes, :carbs_g, :protein_g, :fat_g, :calories, :tag, :source)
            ON CONFLICT(timestamp_ms, title) DO UPDATE SET
                notes=excluded.notes,
                carbs_g=COALESCE(excluded.carbs_g, meals.carbs_g),
                protein_g=COALESCE(excluded.protein_g, meals.protein_g),
                fat_g=COALESCE(excluded.fat_g, meals.fat_g),
                calories=COALESCE(excluded.calories, meals.calories),
                tag=excluded.tag
            RETURNING id
            """,
            {
                "timestamp_ms": int(meal["timestamp_ms"]),
                "title": str(meal["title"]).strip(),
                "notes": meal.get("notes", "") or "",
                "carbs_g": float(meal["carbs_g"]) if meal.get("carbs_g") is not None else None,
                "protein_g": float(meal["protein_g"]) if meal.get("protein_g") is not None else None,
                "fat_g": float(meal["fat_g"]) if meal.get("fat_g") is not None else None,
                "calories": float(meal["calories"]) if meal.get("calories") is not None else None,
                "tag": meal.get("tag", "meal") or "meal",
                "source": meal.get("source", "manual") or "manual",
            },
        )
        row = cur.fetchone()
        return int(row["id"]) if row else 0


def delete_meal(meal_id: int, db_path: Optional[str] = None) -> bool:
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        return cur.rowcount > 0


def upsert_workout(
    workout: Dict[str, Any],
    points: List[Dict[str, Any]],
    db_path: Optional[str] = None,
) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO workouts (
                external_id, start_time_ms, end_time_ms, sport, title,
                distance_m, duration_s, total_ascent_m, total_descent_m,
                avg_hr, max_hr, avg_speed_mps, max_speed_mps, calories, source,
                timer_s, pauses_json
            )
            VALUES (
                :external_id, :start_time_ms, :end_time_ms, :sport, :title,
                :distance_m, :duration_s, :total_ascent_m, :total_descent_m,
                :avg_hr, :max_hr, :avg_speed_mps, :max_speed_mps, :calories, :source,
                :timer_s, :pauses_json
            )
            ON CONFLICT(external_id) DO UPDATE SET
                start_time_ms=excluded.start_time_ms,
                end_time_ms=excluded.end_time_ms,
                sport=excluded.sport,
                title=excluded.title,
                distance_m=excluded.distance_m,
                duration_s=excluded.duration_s,
                total_ascent_m=excluded.total_ascent_m,
                total_descent_m=excluded.total_descent_m,
                avg_hr=excluded.avg_hr,
                max_hr=excluded.max_hr,
                avg_speed_mps=excluded.avg_speed_mps,
                max_speed_mps=excluded.max_speed_mps,
                calories=excluded.calories,
                source=excluded.source,
                timer_s=excluded.timer_s,
                pauses_json=excluded.pauses_json
            RETURNING id
            """,
            {
                "timer_s": float(workout["timer_s"]) if workout.get("timer_s") is not None else None,
                "pauses_json": json.dumps(workout.get("pauses") or []),
                "external_id": workout["external_id"],
                "start_time_ms": int(workout["start_time_ms"]),
                "end_time_ms": int(workout["end_time_ms"]),
                "sport": workout.get("sport", "running"),
                "title": workout.get("title", "Workout"),
                "distance_m": float(workout.get("distance_m") or 0.0),
                "duration_s": float(workout.get("duration_s") or 0.0),
                "total_ascent_m": float(workout.get("total_ascent_m") or 0.0),
                "total_descent_m": float(workout.get("total_descent_m") or 0.0),
                "avg_hr": int(workout["avg_hr"]) if workout.get("avg_hr") is not None else None,
                "max_hr": int(workout["max_hr"]) if workout.get("max_hr") is not None else None,
                "avg_speed_mps": float(workout["avg_speed_mps"]) if workout.get("avg_speed_mps") is not None else None,
                "max_speed_mps": float(workout["max_speed_mps"]) if workout.get("max_speed_mps") is not None else None,
                "calories": float(workout["calories"]) if workout.get("calories") is not None else None,
                "source": workout.get("source", "garmin_fit"),
            },
        )
        workout_id = int(cur.fetchone()["id"])
        if points:
            conn.execute("DELETE FROM workout_points WHERE workout_id = ?", (workout_id,))
            conn.executemany(
                """
                INSERT INTO workout_points (
                    workout_id, timestamp_ms, elapsed_s, heart_rate, speed_mps,
                    altitude_m, distance_m, cadence, power_w, grade_pct, lat, lon, glucose_mgdl
                )
                VALUES (
                    :workout_id, :timestamp_ms, :elapsed_s, :heart_rate, :speed_mps,
                    :altitude_m, :distance_m, :cadence, :power_w, :grade_pct, :lat, :lon, :glucose_mgdl
                )
                """,
                [
                    {
                        "workout_id": workout_id,
                        "timestamp_ms": int(p["timestamp_ms"]),
                        "elapsed_s": float(p.get("elapsed_s", 0.0)),
                        "heart_rate": int(p["heart_rate"]) if p.get("heart_rate") is not None else None,
                        "speed_mps": float(p["speed_mps"]) if p.get("speed_mps") is not None else None,
                        "altitude_m": float(p["altitude_m"]) if p.get("altitude_m") is not None else None,
                        "distance_m": float(p["distance_m"]) if p.get("distance_m") is not None else None,
                        "cadence": int(p["cadence"]) if p.get("cadence") is not None else None,
                        "power_w": int(p["power_w"]) if p.get("power_w") is not None else None,
                        "grade_pct": float(p["grade_pct"]) if p.get("grade_pct") is not None else None,
                        "lat": float(p["lat"]) if p.get("lat") is not None else None,
                        "lon": float(p["lon"]) if p.get("lon") is not None else None,
                        "glucose_mgdl": float(p["glucose_mgdl"]) if p.get("glucose_mgdl") is not None else None,
                    }
                    for p in points
                ],
            )
        return workout_id


def clear_demo_data(db_path: Optional[str] = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM glucose_readings WHERE source = 'demo'")
        conn.execute("DELETE FROM heart_rate_readings WHERE source = 'demo'")
        conn.execute("DELETE FROM meals WHERE source = 'demo'")
        conn.execute("DELETE FROM workouts WHERE source = 'demo'")


def get_sync_settings(db_path: Optional[str] = None, include_secret: bool = False) -> Dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM sync_settings WHERE id = 1").fetchone()
        if not row:
            return {
                "clarity_share_code": "",
                "garmin_email": "",
                "has_garmin_password": False,
                "auto_poll_minutes": 15,
                "lookback_days": 2,
                "last_sync_ms": None,
                "last_sync_status": "",
                "last_sync_detail": "",
            }
        d = dict(row)
        has_pw = bool(d.get("garmin_password"))
        if not include_secret:
            d.pop("garmin_password", None)
        d["has_garmin_password"] = has_pw
        return d


def save_sync_settings(settings: Dict[str, Any], db_path: Optional[str] = None) -> Dict[str, Any]:
    with get_conn(db_path) as conn:
        cur_row = conn.execute("SELECT * FROM sync_settings WHERE id = 1").fetchone()
        cur = dict(cur_row) if cur_row else {}

        share_code = settings.get("clarity_share_code")
        if share_code is None:
            share_code = cur.get("clarity_share_code", "")

        garmin_email = settings.get("garmin_email")
        if garmin_email is None:
            garmin_email = cur.get("garmin_email", "")

        garmin_password = settings.get("garmin_password")
        if garmin_password is None or garmin_password == "":
            garmin_password = cur.get("garmin_password", "")

        meals_csv_url = settings.get("meals_csv_url")
        if meals_csv_url is None:
            meals_csv_url = cur.get("meals_csv_url", "")

        auto_poll_minutes = settings.get("auto_poll_minutes")
        if auto_poll_minutes is None:
            auto_poll_minutes = cur.get("auto_poll_minutes", 15)

        lookback_days = settings.get("lookback_days")
        if lookback_days is None:
            lookback_days = cur.get("lookback_days", 2)

        conn.execute(
            """
            UPDATE sync_settings
            SET clarity_share_code = ?,
                garmin_email = ?,
                garmin_password = ?,
                meals_csv_url = ?,
                auto_poll_minutes = ?,
                lookback_days = ?
            WHERE id = 1
            """,
            (
                str(share_code).strip(),
                str(garmin_email).strip(),
                str(garmin_password),
                str(meals_csv_url).strip(),
                int(auto_poll_minutes),
                max(1, min(30, int(lookback_days))),
            ),
        )
    return get_sync_settings(db_path=db_path, include_secret=False)


def update_sync_status(
    timestamp_ms: int,
    status: str,
    detail: str,
    db_path: Optional[str] = None,
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE sync_settings
            SET last_sync_ms = ?, last_sync_status = ?, last_sync_detail = ?
            WHERE id = 1
            """,
            (int(timestamp_ms), status, detail),
        )

