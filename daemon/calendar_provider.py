"""
calendar_provider.py — Dedicated Calendar Date Value Provider for nightscout-monitor.

Implements Phase 6 specification (docs/analysis/phase6-calendar-date-provider-spec.md).
Computes and returns one numeric metric value per calendar day over a requested date range
with support for single_day (with optional timeScope fallback) and rolling windows.
Reuses filter_engine.FILTER_METRICS catalog and shared source-resolver helper.
"""

import time
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, List, Tuple

from filter_engine import (
    FILTER_METRICS,
    resolve_metric_source,
    build_timescope_clause,
    _tz_sql,
)

# Rolling window day whitelists (§3.4)
ALLOWED_ROLLING_DAYS: List[int] = [7, 14, 30, 60, 90]

# Mapping from daily metric key to rolling column template in layer2_daily_risk_stats
ROLLING_COLUMNS: Dict[str, str] = {
    "tir": "tir_{N}d",
    "titr": "titr_{N}d",
    "tar": "tar_{N}d",
    "tbr": "tbr_{N}d",
    "avg_bg": "mean_mmol_{N}d",
    "cv": "cv_{N}d",
    "gmi": "gmi_{N}d",
    "hbgi": "hbgi_{N}d",
    "lbgi": "lbgi_{N}d",
    "gvi": "gvi_{N}d",
    "tdd": "tdd_{N}d",
    "carbs": "carbs_{N}d",
    "low_count": "episode_count_{N}d",
}


def validate_calendar_request(
    metric_id: str,
    window_type: str,
    rolling_days: Optional[int],
    start_date: str,
    end_date: str,
    time_scope: Optional[Dict[str, str]] = None,
) -> Tuple[date, date]:
    """Validate calendar metric value request parameters against strict whitelists.
    Raises ValueError on any invalid or malformed field.
    Returns parsed (start_date, end_date) as date objects.
    """
    if not metric_id or metric_id not in FILTER_METRICS:
        raise ValueError(
            f"Unknown metric: '{metric_id}'. Whitelisted keys: {list(FILTER_METRICS.keys())}"
        )

    cfg = FILTER_METRICS[metric_id]
    kind = cfg.get("kind")

    # Event metrics have no value (§3.3)
    if kind == "event" and metric_id not in ("low_count", "warning_count", "pod_change"):
        raise ValueError(
            f"{metric_id} is an event metric and has no per-day value -- "
            f"use the flag provider (POST /api/v1/filters/evaluate) instead"
        )

    # Continuous metrics are not discrete per-day summary values
    if kind == "continuous":
        raise ValueError(
            f"{metric_id} is a continuous metric -- calendar metric values only "
            f"support daily metrics. Use daily metrics (e.g. avg_bg) or the flag "
            f"provider (POST /api/v1/filters/evaluate) instead"
        )

    if not start_date or not end_date:
        raise ValueError("Both start and end dates (YYYY-MM-DD) are required")

    try:
        start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
        if start_d > end_d:
            raise ValueError("start date cannot be after end date")
    except ValueError as e:
        raise ValueError(f"Invalid date format: {e}")

    if window_type not in ("single_day", "rolling"):
        raise ValueError(
            f"Invalid window type '{window_type}'. Must be 'single_day' or 'rolling'"
        )

    if window_type == "rolling":
        if rolling_days is None or rolling_days not in ALLOWED_ROLLING_DAYS:
            raise ValueError(
                f"Invalid rollingDays '{rolling_days}'. Must be one of {ALLOWED_ROLLING_DAYS}"
            )
        if metric_id not in ROLLING_COLUMNS and metric_id not in ("low_count", "warning_count", "pod_change"):
            raise ValueError(
                f"Rolling window not supported for metric '{metric_id}'"
            )
        if time_scope:
            raise ValueError(
                "timeScope is not supported with window=rolling (rolling windows use precalculated 24h stats)"
            )

    if time_scope:
        s = time_scope.get("start", "")
        e = time_scope.get("end", "")
        if not s or not e:
            raise ValueError("timeScope requires both start and end times ('HH:MM')")
        try:
            datetime.strptime(s, "%H:%M")
            datetime.strptime(e, "%H:%M")
        except ValueError:
            raise ValueError(f"Invalid time format in timeScope: {s} - {e}. Expected HH:MM")

    return start_d, end_d


def get_calendar_metric_values(
    conn: Any,
    metric_id: str,
    window_type: str = "single_day",
    rolling_days: Optional[int] = None,
    start_date: str = "",
    end_date: str = "",
    time_scope: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Fetch calendar metric values map for [start_date, end_date] inclusive.
    Returns standardized response with full-range calendar day keys (§3.1).
    """
    t0 = time.perf_counter()

    start_d, end_d = validate_calendar_request(
        metric_id, window_type, rolling_days, start_date, end_date, time_scope
    )

    cfg = FILTER_METRICS[metric_id]
    decimals = cfg.get("decimals", 1)
    if window_type == "rolling" and metric_id in ("low_count", "warning_count"):
        decimals = 1
    raw_values: Dict[str, Optional[float]] = {}

    with conn.cursor() as cur:
        if window_type == "rolling":
            if metric_id == "warning_count":
                query = f"""
                    WITH daily_counts AS (
                        SELECT a.day, (COUNT(*) FILTER (WHERE a.bg >= 3.9 AND a.bg <= 4.5))::numeric AS cnt
                        FROM layer2_five_minute_aggregate a
                        WHERE a.day BETWEEN (%s::date - INTERVAL '{rolling_days} days')::date AND %s::date
                        GROUP BY a.day
                    )
                    SELECT d.day::text,
                           ROUND(AVG(d.cnt) OVER (ORDER BY d.day ROWS BETWEEN {rolling_days - 1} PRECEDING AND CURRENT ROW), 1) AS val
                    FROM daily_counts d
                    WHERE d.day BETWEEN %s::date AND %s::date
                    ORDER BY d.day ASC
                """
                cur.execute(query, [start_date, end_date, start_date, end_date])
                for row in cur.fetchall():
                    if row[0]:
                        raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None
            elif metric_id == "pod_change":
                tz_sub = _tz_sql()
                e_time_expr = f"e.ts AT TIME ZONE {tz_sub}"
                query = f"""
                    WITH days AS (
                        SELECT generate_series((%s::date - INTERVAL '{rolling_days} days')::date, %s::date, '1 day'::interval)::date AS day
                    ),
                    daily_counts AS (
                        SELECT ({e_time_expr})::date AS day, count(*) AS cnt
                        FROM site_changes e
                        WHERE ({e_time_expr})::date BETWEEN (%s::date - INTERVAL '{rolling_days} days')::date AND %s::date
                        GROUP BY 1
                    ),
                    merged AS (
                        SELECT d.day, COALESCE(c.cnt, 0)::numeric AS cnt
                        FROM days d
                        LEFT JOIN daily_counts c ON d.day = c.day
                    )
                    SELECT m.day::text,
                           ROUND(AVG(m.cnt) OVER (ORDER BY m.day ROWS BETWEEN {rolling_days - 1} PRECEDING AND CURRENT ROW), 1) AS val
                    FROM merged m
                    WHERE m.day BETWEEN %s::date AND %s::date
                    ORDER BY m.day ASC
                """
                cur.execute(query, [start_date, end_date, start_date, end_date, start_date, end_date])
                for row in cur.fetchall():
                    if row[0]:
                        raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None
            else:
                col_template = ROLLING_COLUMNS[metric_id]
                col_name = col_template.format(N=rolling_days)
                query = f"""
                    SELECT r.date::text, r.{col_name} AS val
                    FROM layer2_daily_risk_stats r
                    WHERE r.date BETWEEN %s AND %s
                    ORDER BY r.date ASC
                """
                cur.execute(query, [start_date, end_date])
                for row in cur.fetchall():
                    if row[0]:
                        raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None

        else:
            if metric_id == "pod_change":
                tz_sub = _tz_sql()
                e_time_expr = f"e.ts AT TIME ZONE {tz_sub}"
                ts_clause, ts_params = build_timescope_clause(time_scope, e_time_expr)
                query = f"""
                    WITH days AS (
                        SELECT generate_series(%s::date, %s::date, '1 day'::interval)::date AS day
                    ),
                    matched AS (
                        SELECT ({e_time_expr})::date AS day, count(*) AS cnt
                        FROM site_changes e
                        WHERE ({e_time_expr})::date BETWEEN %s AND %s
                          {ts_clause}
                        GROUP BY 1
                    )
                    SELECT d.day::text, COALESCE(m.cnt, 0)::numeric AS val
                    FROM days d
                    LEFT JOIN matched m ON d.day = m.day
                    ORDER BY d.day ASC
                """
                cur.execute(query, [start_date, end_date, start_date, end_date] + ts_params)
                for row in cur.fetchall():
                    if row[0]:
                        raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None
            else:
                # Single day window — resolve backing table via shared helper (§3.5)
                table, col_expr, date_col, is_timescoped = resolve_metric_source(
                    metric_id, time_scope, table_alias="d"
                )

                if metric_id == "low_count" and is_timescoped:
                    tz_sub = _tz_sql()
                    h_time_expr = f"h.start_time AT TIME ZONE {tz_sub}"
                    ts_clause, ts_params = build_timescope_clause(time_scope, h_time_expr)
                    query = f"""
                        WITH days AS (
                            SELECT generate_series(%s::date, %s::date, '1 day'::interval)::date AS day
                        ),
                        matched_episodes AS (
                            SELECT ({h_time_expr})::date AS day, count(*) AS cnt
                            FROM layer2_hypo_episodes h
                            WHERE ({h_time_expr})::date BETWEEN %s AND %s
                              {ts_clause}
                            GROUP BY 1
                        )
                        SELECT d.day::text, COALESCE(m.cnt, 0)::numeric AS val
                        FROM days d
                        LEFT JOIN matched_episodes m ON d.day = m.day
                        ORDER BY d.day ASC
                    """
                    cur.execute(query, [start_date, end_date, start_date, end_date] + ts_params)
                    for row in cur.fetchall():
                        if row[0]:
                            raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None
                elif is_timescoped:
                    tz_sub = _tz_sql()
                    time_expr = f"a.ts AT TIME ZONE {tz_sub}"
                    ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)

                    query = f"""
                        SELECT a.day::text, ({col_expr}) AS val
                        FROM layer2_five_minute_aggregate a
                        WHERE a.day BETWEEN %s AND %s
                          {ts_clause}
                        GROUP BY a.day
                        HAVING COUNT(a.bg) >= 12
                        ORDER BY a.day ASC
                    """
                    cur.execute(query, [start_date, end_date] + ts_params)
                    for row in cur.fetchall():
                        if row[0]:
                            raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None
                else:
                    query = f"""
                        SELECT d.{date_col}::text, ({col_expr}) AS val
                        FROM {table} d
                        WHERE d.{date_col} BETWEEN %s AND %s
                        ORDER BY d.{date_col} ASC
                    """
                    cur.execute(query, [start_date, end_date])
                    for row in cur.fetchall():
                        if row[0]:
                            raw_values[str(row[0])] = float(row[1]) if row[1] is not None else None

    # Assemble full-range calendar day map (§3.1)
    values: Dict[str, Optional[float]] = {}
    curr = start_d
    days_with_data = 0
    total_days = (end_d - start_d).days + 1

    while curr <= end_d:
        d_str = curr.strftime("%Y-%m-%d")
        if d_str in raw_values and raw_values[d_str] is not None:
            val = raw_values[d_str]
            values[d_str] = round(val, decimals) if decimals > 0 else int(round(val))
            days_with_data += 1
        else:
            values[d_str] = None
        curr += timedelta(days=1)

    execution_time_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    window_resp: Dict[str, Any] = {"type": window_type}
    if window_type == "rolling":
        window_resp["days"] = rolling_days

    return {
        "metric": metric_id,
        "window": window_resp,
        "dateRange": {
            "start": start_date,
            "end": end_date,
        },
        "values": values,
        "meta": {
            "totalDaysEvaluated": total_days,
            "daysWithData": days_with_data,
            "executionTimeMs": execution_time_ms,
        },
    }
