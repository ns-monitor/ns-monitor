"""
cgm_health.py -- CGM data-quality intelligence: how much of the expected
CGM stream actually arrived, how fresh it was when the loop needed it, and
what (of the things Harry can actually influence -- phone battery, being
away from the rig) explains the gaps.

Deliberately excludes sensor lifespan/warm-up tracking: Harry runs a hacked
G6 transmitter (~50min warm-up, not the standard ~2h), sometimes G7, and
dual overlapping xDrip/AAPS setups, so `Sensor Change` treatments don't
reliably correspond to when a sensor actually started feeding AAPS. That
data isn't trustworthy enough to build on, and it's not something Harry is
interested in besides.

Coverage/staleness numbers here are also intended as the source of truth
for a possible future footnote on Patterns/Trace/Trends ("X% CGM coverage
this period") -- kept as small, cheap, self-contained queries for that
reason rather than folded into a bigger combined query.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import config
from filter_engine import _tz_sql

# A gap shorter than this is routine (a missed BLE beacon, self-resolving)
# and not worth surfacing individually -- only counted toward coverage%.
GAP_THRESHOLD_MINUTES = 15

# AAPS's own approximate cutoff for refusing to act on stale BG. Not read
# from AAPS source here -- treated as a labelled reference line on the
# staleness chart, not asserted as exact.
LOOP_STALENESS_REFERENCE_MINUTES = 15

# A reading arriving this much later than it was actually taken is treated
# as backfilled/batch-uploaded rather than real-time.
BACKFILL_LAG_THRESHOLD_MINUTES = 20

# Last known uploader battery level below this, in the run-up to a gap, is
# treated as a plausible "phone died" explanation. Deliberately close to
# true dead-battery territory (Harry's correction, 1 Sep 2026) rather than
# "getting low" -- 20% was catching gaps that had nothing to do with the
# battery actually running out.
LOW_BATTERY_THRESHOLD_PCT = 3


def get_coverage_stats(conn, start_date: str, end_date: str) -> Dict[str, Any]:
    """Coverage% (actual 5-min readings vs theoretical slots in range) and
    the real-time vs backfilled split, from created_at/dateString lag.

    start_date/end_date are local calendar dates as picked in the UI. The
    WHERE boundaries below convert them to the actual UTC instants of local
    midnight via _tz_sql() -- the same pattern get_loop_staleness() already
    uses below. Comparing bare ::date literals directly against ts
    (timestamptz) without this conversion resolves against Postgres' own
    session timezone (UTC in this container), not Harry's configured
    Australia/Perth -- silently shifting the window 8 hours later than the
    calendar days actually selected. Caught 3 Sep 2026: "1-2 September"
    showed ~88% coverage from this bug; the true local-day figure is ~99.5%
    -- the window was dropping a fully-populated 8h block off the start and
    counting an as-yet-unelapsed, necessarily-empty 8h block at the end.
    """
    tz_sub = _tz_sql()
    query = f"""
        WITH readings AS (
            SELECT
                ts,
                raw ? 'created_at' AND raw ? 'dateString' AS has_lag_fields,
                EXTRACT(EPOCH FROM (
                    (raw->>'created_at')::timestamptz - (raw->>'dateString')::timestamptz
                )) / 60.0 AS lag_min
            FROM cgm_readings
            WHERE ts >= (%(start)s::date::timestamp AT TIME ZONE {tz_sub})
              AND ts < ((%(end)s::date + INTERVAL '1 day')::timestamp AT TIME ZONE {tz_sub})
        )
        SELECT
            count(*) AS actual_readings,
            count(*) FILTER (WHERE has_lag_fields) AS readings_with_lag_data,
            count(*) FILTER (WHERE has_lag_fields AND lag_min > %(backfill_threshold)s) AS backfilled_count
        FROM readings
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "start": start_date, "end": end_date,
            "backfill_threshold": BACKFILL_LAG_THRESHOLD_MINUTES,
        })
        actual_readings, readings_with_lag_data, backfilled_count = cur.fetchone()

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    theoretical_slots = int((end_dt - start_dt).total_seconds() / 300)

    coverage_pct = round(100.0 * actual_readings / theoretical_slots, 1) if theoretical_slots else None
    backfilled_pct = (
        round(100.0 * backfilled_count / readings_with_lag_data, 1)
        if readings_with_lag_data else None
    )

    return {
        "actual_readings": actual_readings,
        "theoretical_slots": theoretical_slots,
        "coverage_pct": coverage_pct,
        "readings_with_lag_data": readings_with_lag_data,
        "backfilled_count": backfilled_count,
        "backfilled_pct": backfilled_pct,
    }


def get_loop_staleness(conn, start_date: str, end_date: str) -> Dict[str, Any]:
    """For every loop devicestatus cycle, how old was the CGM reading it had
    available. The daily headline number is pct_fresh -- % of that day's
    loop cycles that had BG no older than LOOP_STALENESS_REFERENCE_MINUTES
    -- a 0-100% "how good was this day" score, which is a much more direct
    answer to "is the sensor driving loop decisions" than a raw minutes
    figure (Harry's 1 Sep 2026 feedback: the original minutes-based chart
    didn't communicate anything at a glance). max_staleness_min is kept
    alongside for tooltip detail on the worst moment of a bad day, not as
    the chart's primary series."""
    tz_sub = _tz_sql()
    daily_query = f"""
        SELECT
            (d.ts AT TIME ZONE {tz_sub})::date AS day,
            ROUND((100.0 * count(*) FILTER (WHERE EXTRACT(EPOCH FROM (d.ts - c.ts)) / 60.0 <= %(ref)s)
                / NULLIF(count(*), 0))::numeric, 1) AS pct_fresh,
            ROUND(MAX(EXTRACT(EPOCH FROM (d.ts - c.ts)) / 60.0)::numeric, 2) AS max_staleness_min,
            count(*) AS n_cycles
        FROM devicestatus d
        LEFT JOIN LATERAL (
            SELECT ts FROM cgm_readings WHERE ts <= d.ts ORDER BY ts DESC LIMIT 1
        ) c ON true
        WHERE d.ts >= %(start)s::date AND d.ts < %(end)s::date + INTERVAL '1 day'
        GROUP BY 1 ORDER BY 1
    """
    with conn.cursor() as cur:
        cur.execute(daily_query, {"start": start_date, "end": end_date, "ref": LOOP_STALENESS_REFERENCE_MINUTES})
        daily_rows = cur.fetchall()

    summary_query = """
        SELECT
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY staleness_min)::numeric, 2) AS median_min,
            ROUND(PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY staleness_min)::numeric, 2) AS p90_min,
            ROUND((100.0 * count(*) FILTER (WHERE staleness_min <= %(ref)s) / NULLIF(count(*), 0))::numeric, 1)
                AS pct_within_reference,
            count(*) AS total_cycles
        FROM (
            SELECT EXTRACT(EPOCH FROM (d.ts - c.ts)) / 60.0 AS staleness_min
            FROM devicestatus d
            LEFT JOIN LATERAL (
                SELECT ts FROM cgm_readings WHERE ts <= d.ts ORDER BY ts DESC LIMIT 1
            ) c ON true
            WHERE d.ts >= %(start)s::date AND d.ts < %(end)s::date + INTERVAL '1 day'
        ) sub
        WHERE staleness_min IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(summary_query, {
            "start": start_date, "end": end_date, "ref": LOOP_STALENESS_REFERENCE_MINUTES,
        })
        median_min, p90_min, pct_within_reference, total_cycles = cur.fetchone()

    return {
        "reference_minutes": LOOP_STALENESS_REFERENCE_MINUTES,
        "median_staleness_min": float(median_min) if median_min is not None else None,
        "p90_staleness_min": float(p90_min) if p90_min is not None else None,
        "pct_within_reference": float(pct_within_reference) if pct_within_reference is not None else None,
        "total_cycles": total_cycles,
        "daily": [
            {"day": day.isoformat(), "pct_fresh": float(pct_fresh) if pct_fresh is not None else None,
             "max_min": float(max_min) if max_min is not None else None, "n_cycles": n_cycles}
            for day, pct_fresh, max_min, n_cycles in daily_rows
        ],
    }


def _find_raw_gaps(conn, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Consecutive cgm_readings more than GAP_THRESHOLD_MINUTES apart, within
    [start_date, end_date] (local calendar dates -- see get_coverage_stats()
    above for why the AT TIME ZONE conversion below matters; same bug shape,
    fixed together 3 Sep 2026). Looks one day before start_date so a gap
    that began just before the window is still correctly captured."""
    tz_sub = _tz_sql()
    query = f"""
        WITH readings AS (
            SELECT ts, ts - LAG(ts) OVER (ORDER BY ts) AS gap
            FROM cgm_readings
            WHERE ts >= ((%(start)s::date - INTERVAL '1 day')::timestamp AT TIME ZONE {tz_sub})
              AND ts < ((%(end)s::date + INTERVAL '1 day')::timestamp AT TIME ZONE {tz_sub})
        )
        SELECT ts - gap AS gap_start, ts AS gap_end,
               EXTRACT(EPOCH FROM gap) / 60.0 AS duration_min
        FROM readings
        WHERE gap > (%(threshold)s || ' minutes')::interval
          AND ts >= (%(start)s::date::timestamp AT TIME ZONE {tz_sub})
          AND ts < ((%(end)s::date + INTERVAL '1 day')::timestamp AT TIME ZONE {tz_sub})
        ORDER BY gap_start
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "start": start_date, "end": end_date, "threshold": GAP_THRESHOLD_MINUTES,
        })
        return [
            {"gap_start": gap_start, "gap_end": gap_end, "duration_min": float(duration_min)}
            for gap_start, gap_end, duration_min in cur.fetchall()
        ]


def _classify_gap(conn, gap: Dict[str, Any]) -> str:
    """battery | absence | other. Battery takes priority over absence when
    both signals are present (it's the more specific, actionable cause --
    a dead phone is also why you were "absent"). Gap counts are low enough
    (tens per year, see module docstring's timing note) that a per-gap
    lookup query is cheap; no need to batch this into the main gap query."""
    with conn.cursor() as cur:
        # Last known uploader battery level in the hour before the gap, and
        # whether devicestatus itself went dark for the gap's duration (the
        # phone stopped reporting anything at all, not just losing the CGM).
        cur.execute("""
            SELECT
                (SELECT (raw_json->>'uploaderBattery')::numeric
                 FROM devicestatus
                 WHERE ts <= %(gap_start)s AND ts >= %(gap_start)s - INTERVAL '1 hour'
                   AND raw_json ? 'uploaderBattery'
                 ORDER BY ts DESC LIMIT 1) AS last_battery_pct,
                NOT EXISTS (
                    SELECT 1 FROM devicestatus
                    WHERE ts > %(gap_start)s AND ts < %(gap_end)s
                ) AS phone_silent_during_gap
        """, {"gap_start": gap["gap_start"], "gap_end": gap["gap_end"]})
        last_battery_pct, phone_silent_during_gap = cur.fetchone()

        if (last_battery_pct is not None and last_battery_pct <= LOW_BATTERY_THRESHOLD_PCT) \
                or phone_silent_during_gap:
            return "battery"

        # Did the reading that ends the gap arrive noticeably late? That's
        # the transmitter's own buffer being flushed on reconnect -- direct
        # evidence the phone wasn't in range in real time when it happened.
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM (
                (raw->>'created_at')::timestamptz - (raw->>'dateString')::timestamptz
            )) / 60.0
            FROM cgm_readings
            WHERE ts = %(gap_end)s AND raw ? 'created_at' AND raw ? 'dateString'
        """, {"gap_end": gap["gap_end"]})
        row = cur.fetchone()
        lag_min = row[0] if row else None

        if lag_min is not None and lag_min > BACKFILL_LAG_THRESHOLD_MINUTES:
            return "absence"

    return "other"


def get_gap_breakdown(conn, start_date: str, end_date: str) -> Dict[str, Any]:
    """Every gap over GAP_THRESHOLD_MINUTES in range, classified by the
    most likely user-influenced cause, plus per-category totals."""
    gaps = _find_raw_gaps(conn, start_date, end_date)

    tz = ZoneInfo(getattr(config, "TIMEZONE", "UTC"))
    classified = []
    totals = {"battery": {"count": 0, "minutes": 0.0}, "absence": {"count": 0, "minutes": 0.0},
              "other": {"count": 0, "minutes": 0.0}}
    for gap in gaps:
        cause = _classify_gap(conn, gap)
        totals[cause]["count"] += 1
        totals[cause]["minutes"] += gap["duration_min"]
        classified.append({
            "start": gap["gap_start"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "end": gap["gap_end"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "duration_min": round(gap["duration_min"], 1),
            "cause": cause,
        })

    classified.sort(key=lambda g: g["start"], reverse=True)
    for cat in totals:
        totals[cat]["minutes"] = round(totals[cat]["minutes"], 1)

    return {"gaps": classified, "totals": totals, "threshold_minutes": GAP_THRESHOLD_MINUTES}
