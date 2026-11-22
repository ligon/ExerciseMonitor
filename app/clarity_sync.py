"""Automated Dexcom Clarity CSV fetcher using a Clarity Share Code (sugarpull protocol)."""

import base64
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any, Dict, Optional
import requests

from app.db import get_conn, insert_or_update_meal, upsert_glucose_batch
from app.importers import get_zoneinfo, parse_dexcom_clarity_csv

MIN_MS = 60_000
HOUR_MS = 3_600_000


def _add_dashes(share_code: str) -> str:
    clean = share_code.replace("-", "").strip()
    return "-".join(clean[i : i + 4] for i in range(0, len(clean), 4))


def _decode_subject_id(access_token: str) -> str:
    parts = access_token.split(".")
    if len(parts) < 2:
        raise ValueError("Invalid Clarity JWT access token")
    payload_b64 = parts[1]
    # Add base64 padding
    payload_b64 += "=" * (-len(payload_b64) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    subject_id = data.get("subjectId")
    if not subject_id:
        raise ValueError("subjectId missing from Clarity access token")
    return str(subject_id)


def fetch_clarity_csv_via_share_code(
    share_code: str,
    start_date_str: str,
    end_date_str: Optional[str] = None,
    base_url: str = "https://clarity.dexcom.com",
) -> str:
    """
    Redeem a 12-character Dexcom Clarity Share Code (generated in the Dexcom Clarity mobile app
    under Profile -> Authorize Sharing -> Generate Code) and download the raw Clarity CSV
    for `start_date_str/end_date_str` (YYYY-MM-DD/YYYY-MM-DD).
    """
    clean_code = share_code.replace("-", "").strip()
    if not clean_code:
        raise ValueError("Dexcom Clarity Share Code is required")

    end_date_str = end_date_str or start_date_str
    # Dexcom Clarity's /export endpoint treats the end date in `YYYY-MM-DD/YYYY-MM-DD` as an exclusive
    # midnight upper bound (< end_date 00:00:00). Advance end_date by +1 day so today's readings are included.
    try:
        end_dt_exclusive = (datetime.strptime(end_date_str, "%Y-%m-%d").date() + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        end_dt_exclusive = end_date_str
    date_interval = f"{start_date_str}/{end_dt_exclusive}"

    session = requests.Session()

    # 1. Redeem share code for JWT accessToken
    redeem_resp = session.get(
        f"{base_url}/api/access_code/redeem",
        params={"accesscode": clean_code},
        timeout=20,
    )
    if redeem_resp.status_code != 200:
        raise RuntimeError(
            f"Clarity Share Code redemption failed (HTTP {redeem_resp.status_code}): {redeem_resp.text[:200]}"
        )
    access_token = (redeem_resp.json() or {}).get("accessToken")
    if not access_token:
        raise RuntimeError("No accessToken returned for Clarity Share Code (check if code is valid/unexpired)")

    subject_id = _decode_subject_id(access_token)

    # 2. Obtain _rogue_material_session cookie via /user/sharing
    dashed_code = _add_dashes(clean_code)
    session.post(
        f"{base_url}/user/sharing",
        data={"sharing_code": dashed_code, "commit": ""},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": base_url,
            "Referer": f"{base_url}/professional/",
        },
        allow_redirects=False,
        timeout=20,
    )

    auth_headers = {"Access-Token": access_token}

    # 3. Fetch subject info (first_name, last_name, locale, glucose_unit)
    subj_resp = session.get(f"{base_url}/subject_info", headers=auth_headers, timeout=20)
    if subj_resp.status_code == 200 and isinstance(subj_resp.json(), dict):
        s_info = subj_resp.json()
    else:
        s_info = {}

    export_form = {
        "dateInterval": date_interval,
        "accessToken": access_token,
        "firstName": s_info.get("first_name") or "Stelo",
        "lastName": s_info.get("last_name") or "User",
        "locale": s_info.get("locale") or "en",
        "units": s_info.get("glucose_unit") or "mgdl",
    }

    # 4. Download raw Clarity CSV
    export_resp = session.post(
        f"{base_url}/api/subject/{subject_id}/export",
        data=export_form,
        headers={
            **auth_headers,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    if export_resp.status_code != 200:
        raise RuntimeError(
            f"Clarity CSV export failed (HTTP {export_resp.status_code}): {export_resp.text[:200]}"
        )

    return export_resp.text


def _resolve_phone_utc_offset_ms(
    naive_as_utc_ms: list, tz_name: Optional[str], now_ms: Optional[int] = None
) -> Optional[int]:
    """
    Clarity CSV timestamps are naive *phone-local* times. If the phone's clock is in a different
    timezone from the dashboard (e.g. watch/dashboard on Mountain time while traveling, phone still
    on Pacific), interpreting them in the dashboard timezone shifts every BG reading by the difference.

    The newest EGV in a live export is at most a few minutes old, so (newest_naive - utc_now) reveals
    the phone's real UTC offset. Returns None when the configured `tz_name` is already consistent
    (or the export isn't live so nothing can be inferred); otherwise the detected offset in ms.
    """
    if not naive_as_utc_ms:
        return None
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    raw = max(naive_as_utc_ms) - now_ms  # = phone_offset - staleness
    if abs(raw) > 15 * HOUR_MS:
        return None  # historical export; can't infer the phone offset

    def _fits(offset_ms: int) -> bool:
        residual = raw - offset_ms  # = -staleness
        return -58 * MIN_MS < residual <= 2 * MIN_MS

    cfg = get_zoneinfo(tz_name).utcoffset(datetime.now(timezone.utc))
    if cfg is not None and _fits(int(cfg.total_seconds() * 1000)):
        return None
    detected = math.ceil((raw - 2 * MIN_MS) / HOUR_MS) * HOUR_MS
    return detected if _fits(detected) else None


def _prune_stale_clarity_rows(new_ts: list, db_path: Optional[str] = None) -> int:
    """Remove stelo_clarity rows inside the freshly exported window that no longer match a reading
    (e.g. rows previously stored with the wrong timezone offset)."""
    if not new_ts:
        return 0
    # The Clarity export is authoritative for its window and its timestamps are stable, so any row
    # whose exact timestamp isn't in the new export is stale. (A ±tolerance match would NOT work:
    # an exact whole-hour shift lands stale rows on the same 5-minute grid, ±1 s.)
    new_set = set(int(t) for t in new_ts)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, timestamp_ms FROM glucose_readings WHERE source='stelo_clarity' AND timestamp_ms BETWEEN ? AND ?",
            (min(new_set), max(new_set)),
        ).fetchall()
        stale = [(r["id"],) for r in rows if int(r["timestamp_ms"]) not in new_set]
        conn.executemany("DELETE FROM glucose_readings WHERE id=?", stale)
        return len(stale)


def sync_dexcom_clarity_share_code(
    share_code: str,
    start_date_str: str,
    end_date_str: Optional[str] = None,
    tz_name: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Download Clarity CSV using Share Code and import 5-minute BG + meal events into SQLite."""
    csv_text = fetch_clarity_csv_via_share_code(
        share_code=share_code,
        start_date_str=start_date_str,
        end_date_str=end_date_str,
    )
    # Detect the phone's real UTC offset (it may differ from the dashboard timezone while traveling).
    as_utc = parse_dexcom_clarity_csv(csv_text, tz_name="UTC")
    phone_offset_ms = _resolve_phone_utc_offset_ms(
        [g["timestamp_ms"] for g in as_utc["glucose"]], tz_name
    )
    if phone_offset_ms is None:
        parsed = parse_dexcom_clarity_csv(csv_text, tz_name=tz_name)
    else:
        parsed = as_utc
        for rec in parsed["glucose"] + parsed["meals"]:
            for key in ("timestamp_ms", "start_time_ms", "end_time_ms"):
                if isinstance(rec.get(key), (int, float)):
                    rec[key] = int(rec[key]) - phone_offset_ms

    pruned = _prune_stale_clarity_rows([g["timestamp_ms"] for g in parsed["glucose"]], db_path=db_path)
    g_cnt = upsert_glucose_batch(parsed["glucose"], db_path=db_path)
    m_cnt = 0
    for m in parsed["meals"]:
        insert_or_update_meal(m, db_path=db_path)
        m_cnt += 1

    return {
        "glucose_imported": g_cnt,
        "meals_imported": m_cnt,
        "stale_rows_pruned": pruned,
        "phone_utc_offset_h": (phone_offset_ms / HOUR_MS) if phone_offset_ms is not None else None,
        "start_date": start_date_str,
        "end_date": end_date_str or start_date_str,
    }
