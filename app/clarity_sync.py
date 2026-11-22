"""Automated Dexcom Clarity CSV fetcher using a Clarity Share Code (sugarpull protocol)."""

import base64
from datetime import datetime, timedelta
import json
from typing import Any, Dict, List, Optional
import requests

from app.db import get_conn, insert_or_update_meal, upsert_glucose_batch
from app.importers import get_zoneinfo, parse_dexcom_clarity_csv


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


def _prune_stale_clarity_rows(
    new_ts: List[int],
    win_start_ms: Optional[int] = None,
    win_end_ms: Optional[int] = None,
    db_path: Optional[str] = None,
) -> int:
    """
    Remove `stelo_clarity` rows inside the freshly exported window whose exact timestamp is not
    in the new export (e.g. duplicate/shifted rows left over after changing the dashboard timezone).
    When `win_end_ms` is None (syncing through today), prune everything >= `win_start_ms` not in
    `new_ts`, which also removes any rows previously shifted past the latest real Clarity reading.
    """
    if not new_ts:
        return 0
    new_set = {int(t) for t in new_ts}
    lo = min(new_set) if win_start_ms is None else min(int(win_start_ms), min(new_set))
    with get_conn(db_path) as conn:
        if win_end_ms is None:
            rows = conn.execute(
                "SELECT id, timestamp_ms FROM glucose_readings WHERE source='stelo_clarity' AND timestamp_ms >= ?",
                (lo,),
            ).fetchall()
        else:
            hi = max(int(win_end_ms), max(new_set))
            rows = conn.execute(
                "SELECT id, timestamp_ms FROM glucose_readings WHERE source='stelo_clarity' AND timestamp_ms BETWEEN ? AND ?",
                (lo, hi),
            ).fetchall()
        stale = [(r["id"],) for r in rows if int(r["timestamp_ms"]) not in new_set]
        if stale:
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
    end_date_str = end_date_str or start_date_str
    csv_text = fetch_clarity_csv_via_share_code(
        share_code=share_code,
        start_date_str=start_date_str,
        end_date_str=end_date_str,
    )
    # Dexcom Clarity CSV timestamps are naive local wall-clock times matching the phone's timezone.
    # NOTE: Clarity holds back live Stelo readings by ~60 minutes (up to 3h per Dexcom docs), so
    # never infer the phone's timezone from (max_ts - utc_now) — always trust the selected `tz_name`.
    parsed = parse_dexcom_clarity_csv(csv_text, tz_name=tz_name)
    new_ts = [int(g["timestamp_ms"]) for g in parsed["glucose"]]

    tz_obj = get_zoneinfo(tz_name)
    win_start_ms: Optional[int] = None
    win_end_ms: Optional[int] = None
    try:
        s_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=tz_obj)
        e_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=tz_obj)
        win_start_ms = int(s_dt.timestamp() * 1000)
        today_local = datetime.now(tz_obj).date()
        # If syncing through today, leave upper bound open so any stale future-shifted rows are pruned
        if e_dt.date() < today_local:
            win_end_ms = int((e_dt + timedelta(days=1)).timestamp() * 1000) - 1
    except ValueError:
        pass

    pruned = _prune_stale_clarity_rows(
        new_ts,
        win_start_ms=win_start_ms,
        win_end_ms=win_end_ms,
        db_path=db_path,
    )
    g_cnt = upsert_glucose_batch(parsed["glucose"], db_path=db_path)
    m_cnt = 0
    for m in parsed["meals"]:
        insert_or_update_meal(m, db_path=db_path)
        m_cnt += 1

    latest_bg_ts_ms = max(new_ts) if new_ts else None
    return {
        "glucose_imported": g_cnt,
        "meals_imported": m_cnt,
        "stale_rows_pruned": pruned,
        "latest_bg_ts_ms": latest_bg_ts_ms,
        "start_date": start_date_str,
        "end_date": end_date_str,
    }
