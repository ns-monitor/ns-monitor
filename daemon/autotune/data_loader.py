"""
data_loader.py -- High-speed read-only queries against PostgreSQL
for CGM entries, treatments, and profile eras.
"""
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
import database
import profile_history
from autotune.models import Profile, GlucosePoint, MMOL_TO_MGDL

def _parse_numeric(text: str) -> Optional[float]:
    match = re.search(r'[0-9]{1,2}:[0-9]{2}\s+([0-9]+(?:\.[0-9]+)?)', text)
    if match:
        return float(match.group(1))
    # Fallback to any number
    matches = re.findall(r'([0-9]+(?:\.[0-9]+)?)', text)
    return float(matches[-1]) if matches else None

def get_profile_snapshot(conn, era_name: Optional[str] = None, tz_name: str = "UTC") -> Profile:
    """
    Derive an immutable baseline Profile from profile history eras.
    Defaults to the latest/active era.
    """
    # Look back 90 days to find recent eras
    now = datetime.now(ZoneInfo(tz_name))
    start_str = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    end_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    eras = profile_history.get_profile_history(conn, start_str, end_str)
    selected_era = None

    if eras:
        if era_name:
            for e in eras:
                if e.get("name") == era_name:
                    selected_era = e
                    break
        if not selected_era:
            # First era is latest/active in profile_history.py convention
            selected_era = eras[0]

    # Defaults in case of missing profile
    name = selected_era.get("name", "Default Profile") if selected_era else "Default Profile"
    dia = float(selected_era.get("dia", 5.0)) if selected_era and selected_era.get("dia") else 5.0
    peak = 55.0

    # Expand basal_series (seconds, rate) to 24 hourly values
    basal = [1.0] * 24
    if selected_era and selected_era.get("basal_series"):
        series = sorted(selected_era["basal_series"], key=lambda x: x.get("seconds", 0))
        for h in range(24):
            sec = h * 3600
            rate = series[0].get("rate", 1.0)
            for item in series:
                if item.get("seconds", 0) <= sec:
                    rate = float(item.get("rate", rate))
                else:
                    break
            basal[h] = round(rate, 3)

    # ISF parsing: in eras isf_lines is e.g. ['00:00  4.1 mmol/U']
    isf_mgdl = 45.0
    if selected_era and selected_era.get("isf_lines"):
        val = _parse_numeric(selected_era["isf_lines"][0])
        if val is not None:
            if "mmol" in selected_era["isf_lines"][0].lower():
                isf_mgdl = val * MMOL_TO_MGDL
            else:
                isf_mgdl = val

    # Carb ratio parsing: e.g. ['00:00  4.2 g/U']
    carb_ratio = 10.0
    if selected_era and selected_era.get("ic_lines"):
        val = _parse_numeric(selected_era["ic_lines"][0])
        if val is not None:
            carb_ratio = val

    # Target low / high
    target_low_mgdl = 100.0
    target_high_mgdl = 100.0
    if selected_era and selected_era.get("target_lines"):
        matches = re.findall(r'([0-9]+(?:\.[0-9]+)?)', selected_era["target_lines"][0])
        if len(matches) >= 2:
            t_low, t_high = float(matches[0]), float(matches[1])
            if "mmol" in selected_era["target_lines"][0].lower():
                target_low_mgdl = t_low * MMOL_TO_MGDL
                target_high_mgdl = t_high * MMOL_TO_MGDL
            else:
                target_low_mgdl = t_low
                target_high_mgdl = t_high

    return Profile(
        name=name,
        dia=dia,
        peak=peak,
        basal=basal,
        isf_mgdl=isf_mgdl,
        carb_ratio=carb_ratio,
        target_low_mgdl=target_low_mgdl,
        target_high_mgdl=target_high_mgdl,
        timezone_name=tz_name
    )


def get_available_profile_eras(conn, tz_name: str = "UTC") -> List[Dict[str, Any]]:
    """List distinct profile eras available to select as baseline."""
    now = datetime.now(ZoneInfo(tz_name))
    start_str = (now - timedelta(days=180)).strftime("%Y-%m-%d")
    end_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    eras = profile_history.get_profile_history(conn, start_str, end_str)
    result = []
    for e in eras:
        name = e.get("name", "Profile")
        from_str = e.get("from", "")
        prof = get_profile_snapshot(conn, name, tz_name)
        result.append({
            "name": name,
            "label": f"{name} (from {from_str})" if from_str else name,
            "dia": prof.dia,
            "basal_sum": round(sum(prof.basal), 2),
            "isf_mmol": prof.isf_mmol,
            "carb_ratio": round(prof.carb_ratio, 2),
            "csf_mmol": prof.csf_mmol,
            "isf_summary": e.get("isf_lines", [""])[0] if e.get("isf_lines") else "",
            "ic_summary": e.get("ic_lines", [""])[0] if e.get("ic_lines") else ""
        })
    return result


def get_0400_days_in_range(
    conn,
    start_date_str: str,
    end_date_str: str,
    tz_name: str = "UTC"
) -> List[Dict[str, Any]]:
    """
    Returns metadata for each 04:00-to-04:00 day in [start_date_str, end_date_str].
    Evaluates point counts, gap flags, and sets default=True for the last 5 completed days.
    """
    tz = ZoneInfo(tz_name)
    s_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date_str, "%Y-%m-%d")

    # Ensure s_dt <= e_dt
    if s_dt > e_dt:
        s_dt, e_dt = e_dt, s_dt

    cur_dt = s_dt
    day_windows = []

    # Reference today local
    now_local = datetime.now(tz)
    today_0400 = now_local.replace(hour=4, minute=0, second=0, microsecond=0)

    while cur_dt <= e_dt:
        d_str = cur_dt.strftime("%Y-%m-%d")
        d_start = cur_dt.replace(hour=4, minute=0, second=0, microsecond=0, tzinfo=tz)
        d_end = d_start + timedelta(days=1)

        # Do not include future or in-progress windows
        if d_end > now_local:
            cur_dt += timedelta(days=1)
            continue

        day_windows.append({
            "date_str": d_str,
            "start_dt": d_start,
            "end_dt": d_end,
            "start_ms": int(d_start.timestamp() * 1000),
            "end_ms": int(d_end.timestamp() * 1000)
        })
        cur_dt += timedelta(days=1)

    if not day_windows:
        return []

    # Batch query CGM counts and min/max ts per day window
    overall_start_ms = day_windows[0]["start_ms"]
    overall_end_ms = day_windows[-1]["end_ms"]

    query = """
        SELECT
            EXTRACT(EPOCH FROM ts) * 1000 AS ts_ms,
            sg
        FROM cgm_readings
        WHERE ts >= TO_TIMESTAMP(%s / 1000.0)
          AND ts < TO_TIMESTAMP(%s / 1000.0)
          AND sg IS NOT NULL
        ORDER BY ts ASC
    """

    with conn.cursor() as cur:
        cur.execute(query, (overall_start_ms, overall_end_ms))
        rows = cur.fetchall()

    # Partition rows by day window
    w_idx = 0
    num_windows = len(day_windows)
    window_readings: Dict[str, List[Tuple[int, float]]] = {w["date_str"]: [] for w in day_windows}

    for r in rows:
        ts_ms = int(r[0])
        sg = float(r[1])
        while w_idx < num_windows:
            w = day_windows[w_idx]
            if w["start_ms"] <= ts_ms < w["end_ms"]:
                window_readings[w["date_str"]].append((ts_ms, sg))
                break
            elif ts_ms >= w["end_ms"]:
                w_idx += 1
            else:
                break

    # Build day report
    results = []
    # Identify last 5 completed windows
    last_5_dates = set([w["date_str"] for w in day_windows[-5:]])

    for w in day_windows:
        d_str = w["date_str"]
        readings = window_readings[d_str]
        pts = len(readings)
        expected = 288 # 24h * 12 points/h
        coverage_pct = round(min(100.0, (pts / expected) * 100.0), 1)

        # Detect gaps > 15m
        has_gaps = False
        gap_count = 0
        max_gap_m = 0
        if len(readings) > 1:
            for i in range(len(readings) - 1):
                gap_m = (readings[i+1][0] - readings[i][0]) / 60000.0
                if gap_m > 15.0:
                    has_gaps = True
                    gap_count += 1
                    max_gap_m = max(max_gap_m, gap_m)

        # Hygiene check
        low_count = sum(1 for _, sg in readings if sg <= 39.0)
        is_clean = (pts >= 250) and (max_gap_m <= 25.0) and (low_count == 0)

        dt = w["start_dt"]
        day_name = dt.strftime("%a")
        formatted_date = dt.strftime("%d %b %Y")

        results.append({
            "date_str": d_str,
            "day_name": day_name,
            "display_title": f"{day_name} {dt.strftime('%d %b')}",
            "window_span": "04:00 → 04:00",
            "cgm_points": pts,
            "coverage_pct": coverage_pct,
            "has_gaps": has_gaps,
            "gap_count": gap_count,
            "max_gap_min": round(max_gap_m),
            "low_readings_count": low_count,
            "is_clean": is_clean,
            "start_ms": w["start_ms"],
            "end_ms": w["end_ms"],
            "recommended_default": (d_str in last_5_dates)
        })

    return results


def load_cgm_and_treatments_for_window(
    conn,
    day_start_ms: int,
    day_end_ms: int,
    dia_hours: float
) -> Tuple[List[GlucosePoint], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Query CGM inside [day_start_ms, day_end_ms) and
    Treatments inside [day_start_ms - (dia + 2h), day_end_ms).
    """
    lookback_ms = int((dia_hours + 2.0) * 3600 * 1000)
    treatment_start_ms = day_start_ms - lookback_ms

    # 1. CGM
    cgm_query = """
        SELECT
            EXTRACT(EPOCH FROM ts) * 1000 AS ts_ms,
            sg,
            direction
        FROM cgm_readings
        WHERE ts >= TO_TIMESTAMP(%s / 1000.0)
          AND ts < TO_TIMESTAMP(%s / 1000.0)
          AND sg IS NOT NULL
        ORDER BY ts ASC
    """
    with conn.cursor() as cur:
        cur.execute(cgm_query, (day_start_ms, day_end_ms))
        cgm_rows = cur.fetchall()

    cgm_points = [
        GlucosePoint(ts_ms=int(r[0]), sgv=float(r[1]), direction=r[2] or "")
        for r in cgm_rows
    ]

    # 2. Treatments
    treat_query = """
        SELECT
            EXTRACT(EPOCH FROM ts) * 1000 AS ts_ms,
            event_type,
            insulin,
            carbs,
            smb_flag,
            duration,
            rate,
            absolute
        FROM treatments
        WHERE ts >= TO_TIMESTAMP(%s / 1000.0)
          AND ts < TO_TIMESTAMP(%s / 1000.0)
        ORDER BY ts ASC
    """
    with conn.cursor() as cur:
        cur.execute(treat_query, (treatment_start_ms, day_end_ms))
        treat_rows = cur.fetchall()

    all_treatments: List[Dict[str, Any]] = []
    carbs_treatments: List[Dict[str, Any]] = []

    for r in treat_rows:
        t_dict = {
            "ts_ms": int(r[0]),
            "event_type": r[1] or "",
            "insulin": float(r[2]) if r[2] is not None else 0.0,
            "carbs": float(r[3]) if r[3] is not None else 0.0,
            "smb_flag": bool(r[4]) if r[4] is not None else False,
            "duration": float(r[5]) if r[5] is not None else 0.0,
            "rate": float(r[6]) if r[6] is not None else None,
            "absolute": float(r[7]) if r[7] is not None else None
        }
        all_treatments.append(t_dict)
        if t_dict["carbs"] > 0:
            carbs_treatments.append(t_dict)

    return cgm_points, all_treatments, carbs_treatments
