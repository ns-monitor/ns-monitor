import os
import math
import shutil
import time
import json
import html
import re
import logging
import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any

import config
try:
    import weight_math
except ImportError:
    from daemon import weight_math

logger = logging.getLogger("database")

# ---------------------------------------------------------------------------
# MOD-1: Threaded connection pool (singleton).  Prevents "Postgres gone away"
# outages and avoids the overhead of creating a new TCP socket on every poll.
# ---------------------------------------------------------------------------
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

def _build_pool() -> psycopg2.pool.ThreadedConnectionPool:
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        host=config.DB_HOST,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        # TCP keepalives prevent silent socket drops during long idle periods
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )

def get_conn():
    """Acquire a connection from the pool with 3-attempt exponential backoff."""
    global _pool
    last_exc = None
    for attempt, delay in enumerate([0, 1, 2], start=1):
        try:
            if delay:
                time.sleep(delay)
            if _pool is None:
                with _pool_lock:
                    if _pool is None:
                        print("[db] Initializing new connection pool...")
                        _pool = _build_pool()
            
            # Check if pool is exhausted or broken before getting conn
            return _pool.getconn()
        except Exception as exc:
            last_exc = exc
            print(f"[db] Connection attempt {attempt}/3 failed: {exc}")
            
            # Only reset the pool on the last attempt or on specific critical failures
            # to avoid transient "unkeyed" errors in other threads
            if attempt == 3:
                print("[db] Critical pool failure. Resetting singleton.")
                try:
                    if _pool is not None: _pool.closeall()
                except: pass
                _pool = None
    raise last_exc

def return_conn(conn):
    """Return a connection to the pool. Call this instead of conn.close()."""
    global _pool
    if conn is None:
        return
        
    try:
        # If pool exists, try to return it
        if _pool is not None:
            _pool.putconn(conn)
        else:
            # Pool was destroyed while this thread was working
            conn.close()
    except Exception as exc:
        # Case: "trying to put unkeyed connection" usually means the pool was reset
        if "unkeyed connection" in str(exc).lower():
            try: conn.close()
            except: pass
        else:
            print(f"[db] Could not return conn to pool: {exc}")
            try: conn.close()
            except: pass

def upsert_ingestion_state(conn, source, ts):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingestion_state(source, last_ts)
            VALUES (%s, %s)
            ON CONFLICT (source)
            DO UPDATE SET last_ts = EXCLUDED.last_ts
            """,
            (source, ts)
        )

def get_last_ts(conn, source):
    with conn.cursor() as cur:
        cur.execute("SELECT last_ts FROM ingestion_state WHERE source = %s", (source,))
        row = cur.fetchone()
        return row[0] if row else None

def sync_system_config(conn, force_overwrite=False):
    """
    Syncs runtime settings from environment variables into system_config.
    Called on startup. If force_overwrite=True, env values overwrite the DB.
    """
    settings = {
        'TIMEZONE':                          os.environ.get("TIMEZONE", "UTC"),
        'POLL_PERIOD_SECONDS':               os.environ.get("POLL_PERIOD_SECONDS", "300"),
        'POLL_OFFSET_SECONDS':               os.environ.get("POLL_OFFSET_SECONDS", "30"),
        'max_history_days':                  os.environ.get("MAX_HISTORY_DAYS", "120"),
        'HEALTHCHECK_URL':                   os.environ.get("HEALTHCHECK_URL", ""),
        'BACKFILL_DELAY_SECONDS':            os.environ.get("BACKFILL_DELAY_SECONDS", "0"),
        'BACKFILL_CHUNK_DAYS_ENTRIES':       os.environ.get("BACKFILL_CHUNK_DAYS_ENTRIES", "1"),
        'BACKFILL_CHUNK_DAYS_TREATMENTS':    os.environ.get("BACKFILL_CHUNK_DAYS_TREATMENTS", "1"),
        'BACKFILL_CHUNK_DAYS_DEVICESTATUS':  os.environ.get("BACKFILL_CHUNK_DAYS_DEVICESTATUS", "7"),
        'BACKFILL_PAGE_SIZE':                os.environ.get("BACKFILL_PAGE_SIZE", "1000"),
        'BACKFILL_SLEEP_BETWEEN_PAGES_SEC':  os.environ.get("BACKFILL_SLEEP_BETWEEN_PAGES_SEC", "0.6"),
        'BACKFILL_SLEEP_BETWEEN_WINDOWS_SEC': os.environ.get("BACKFILL_SLEEP_BETWEEN_WINDOWS_SEC", "0.2"),
        'NIGHTSCOUT_URL':                    os.environ.get("NIGHTSCOUT_URL", ""),  # read-only display
        'NS_SSL_VERIFY':                     os.environ.get("NS_SSL_VERIFY", "true"),
        # 5-minute spine: controls how far back the generate_series goes
        'RETENTION_DAYS_RAW':                os.environ.get("RETENTION_DAYS_RAW", "120"),
        'RETENTION_DAYS_CLINICAL':           os.environ.get("RETENTION_DAYS_CLINICAL", "365"),
        'RETENTION_ENABLED':                 os.environ.get("RETENTION_ENABLED", "false"),
        'POLLING_ENABLED':                   os.environ.get("POLLING_ENABLED", "true"),
    }
    with conn.cursor() as cur:
        for key, value in settings.items():
            if force_overwrite:
                # Emergency Override
                sql = """
                    INSERT INTO system_config(key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """
            else:
                # Standard Fallback (Seeding missing keys)
                sql = """
                    INSERT INTO system_config(key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                """
            cur.execute(sql, (key, value))
        # Prune decommissioned external tool keys
        cur.execute("DELETE FROM system_config WHERE key IN ('GRAFANA_EXTERNAL_URL', 'METABASE_EXTERNAL_URL')")
    conn.commit()
    msg = "overwritten from environment (Emergency Override)" if force_overwrite else "seeded from environment (DB Precedence)"
    print(f"[config] system_config {msg} ({len(settings)} keys).")

def get_system_config(conn):
    """Returns all system_config keys as a dictionary."""
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM system_config")
        return {row[0]: row[1] for row in cur.fetchall()}

def set_system_config(conn, key, value):
    """Upserts a key-value pair into system_config."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO system_config (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, str(value)))
    conn.commit()

def delete_system_config(conn, key):
    """Deletes a key from system_config."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM system_config WHERE key = %s", (key,))
    conn.commit()

# ---------------- COMPARISON PERIOD (Advanced Analytics) ----------------
# 20 Aug 2026: single source of truth for "what is the previous/comparison
# period" -- pulled out after finding the logic had already drifted between
# call sites (spider dataset used a correct span-matched previous period;
# the kinematics J-Index calc had independently hardcoded a fixed 14-day
# lookback instead of matching the selected span, which is a bug fixed by
# this consolidation). Every endpoint with a "previous period" concept
# should call this instead of hand-deriving it.
def resolve_comparison_period(start_date_str, end_date_str, prev_start_date_str=None, prev_end_date_str=None):
    """
    Returns (prev_start_str, prev_end_str) as 'YYYY-MM-DD' strings.

    If both prev_start_date_str/prev_end_date_str are given (an explicit
    override, e.g. from the Advanced Analytics page's Last Period bar),
    they're used as-is. Otherwise the comparison period defaults to the
    immediately preceding, non-overlapping period of the same length as
    [start_date_str, end_date_str] (e.g. selecting Aug 10-19 compares
    against Aug 1-9).
    """
    if prev_start_date_str and prev_end_date_str:
        return prev_start_date_str, prev_end_date_str

    s_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
    e_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
    span = (e_dt - s_dt).days + 1

    prev_end_dt = s_dt - timedelta(days=1)
    prev_start_dt = prev_end_dt - timedelta(days=span - 1)
    return prev_start_dt.strftime('%Y-%m-%d'), prev_end_dt.strftime('%Y-%m-%d')


# ---------------- SPIDER CHART (Advanced Analytics) ----------------
# 19 Aug 2026: 10-metric live computation for an arbitrary [start,end]
# range, driven by the Advanced Analytics page's own date selector (not
# the fixed 7/14/30/60/90-day windows used on the Metrics page). All 10
# metrics compute from existing daily-grain (layer2_daily_band_stats) or
# near-raw (layer2_five_minute_aggregate) data -- no new pre-calc needed,
# except LBGI/HBGI which were exposed at daily grain specifically to keep
# this fast (see the 19 Aug commit exposing them on layer2_daily_band_stats).
def get_spider_raw_metrics(conn, start_date_str, end_date_str):
    """
    Returns a dict of the 10 spider-chart metrics computed live for the
    given [start_date, end_date] (inclusive), as raw (un-normalized) values.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH band AS (
                SELECT
                    SUM(bg_readings) AS readings,
                    SUM(n_tight) AS n_titr,       -- n_tight is misleadingly named; it's actually the TITR count
                    SUM(n_mod_high) AS n_mod_high,
                    SUM(n_vlow) AS n_vlow,
                    SUM(n_low) AS n_low,
                    SUM(n_high) AS n_high,
                    SUM(n_vhigh) AS n_vhigh,
                    SUM(bg_sum) AS bg_sum,
                    SUM(bg_sum2) AS bg_sum2,
                    SUM(lbgi * bg_readings) AS lbgi_weighted,
                    SUM(hbgi * bg_readings) AS hbgi_weighted
                FROM layer2_daily_band_stats
                WHERE date BETWEEN %(start)s AND %(end)s
            ),
            mag_calc AS (
                SELECT SUM(ABS(bg_roc)) AS mag_abs_sum, COUNT(bg_roc) AS mag_n
                FROM layer2_five_minute_aggregate
                WHERE ts >= %(start)s AND ts < (%(end)s::date + 1) AND bg_roc IS NOT NULL
            )
            SELECT
                -- TIR (3.9-10.0 mmol/L) = TITR sub-band + mod_high sub-band combined
                100.0 * (band.n_titr + band.n_mod_high) / NULLIF(band.readings, 0) AS tir,
                100.0 * band.n_titr / NULLIF(band.readings, 0) AS titr,
                100.0 * (band.n_vlow + band.n_low) / NULLIF(band.readings, 0) AS tbr,
                100.0 * (band.n_high + band.n_vhigh) / NULLIF(band.readings, 0) AS tar,
                LEAST(100,
                    3.0 * LEAST(100, 100.0 * (band.n_vlow + 0.8 * band.n_low) / NULLIF(band.readings, 0))
                    + 1.6 * LEAST(100, 100.0 * (band.n_vhigh + 0.5 * band.n_high) / NULLIF(band.readings, 0))
                ) AS gri,
                -- GRI grid axes (19 Aug 2026 addition): exposed separately from the
                -- combined GRI score so the GRI Risk Grid panel can plot (x, y) points.
                LEAST(100, 100.0 * (band.n_vlow + 0.8 * band.n_low) / NULLIF(band.readings, 0)) AS gri_x,
                LEAST(100, 100.0 * (band.n_vhigh + 0.5 * band.n_high) / NULLIF(band.readings, 0)) AS gri_y,
                3.31 + 0.02392 * (band.bg_sum / NULLIF(band.readings, 0)) AS gmi,
                100.0 * sqrt(GREATEST(0, band.bg_sum2 / NULLIF(band.readings, 0) - power(band.bg_sum / NULLIF(band.readings, 0), 2)))
                    / NULLIF(band.bg_sum / NULLIF(band.readings, 0), 0) AS cv,
                -- Mean BG in mg/dL (20 Aug 2026 addition): exposed so J-Index
                -- (0.324*(Mean+SD)^2) can be computed for the page's actual arbitrary
                -- [start,end] selection instead of snapping to the nearest fixed
                -- 7/14/30/60/90-day column. NOTE units: bg_sum/readings is mg/dL here
                -- (same basis the GMI formula above already relies on) -- callers that
                -- need mmol/L (the app-wide display convention) must divide by 18.0182.
                band.bg_sum / NULLIF(band.readings, 0) AS mean_bg_mgdl,
                band.lbgi_weighted / NULLIF(band.readings, 0) AS lbgi,
                band.hbgi_weighted / NULLIF(band.readings, 0) AS hbgi,
                mag_calc.mag_abs_sum / NULLIF(mag_calc.mag_n, 0) * 12.0 AS mag
            FROM band, mag_calc;
        """, {"start": start_date_str, "end": end_date_str})
        row = cur.fetchone()
        return {k: (float(v) if v is not None else None) for k, v in row.items()}


def _pod_time_filter_clause(start_time_str, end_time_str, params):
    """
    Builds SQL condition for pod start time of day in system timezone.
    Returns '' if no time filter or full day (00:00 to 24:00/23:59).
    """
    if not start_time_str and not end_time_str:
        return ""
    st = (start_time_str or "00:00").strip()
    et = (end_time_str or "24:00").strip()
    if st == "00:00" and et in ("24:00", "23:59", "23:59:59"):
        return ""

    if et in ("24:00", "24:00:00"):
        et = "24:00:00"
    if st in ("24:00", "24:00:00"):
        st = "00:00:00"

    params["start_time"] = st
    params["end_time"] = et

    local_time_expr = "(start_ts AT TIME ZONE COALESCE((SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'UTC'))::time"
    if st <= et:
        return f" AND {local_time_expr} >= %(start_time)s::time AND {local_time_expr} <= %(end_time)s::time"
    else:
        return f" AND ({local_time_expr} >= %(start_time)s::time OR {local_time_expr} <= %(end_time)s::time)"


def get_pod_summary_stats(conn, start_date_str, end_date_str, start_time_str=None, end_time_str=None):
    """
    Pod count and average (capped) lifespan for pods whose start_ts falls
    in [start_date, end_date]. Excludes status='active' -- see
    pod_sessions_capped -- the currently-running pod's real duration isn't
    knowable yet, so it's left out of every pod stat/histogram/graph on
    this page, not just this one.
    """
    params = {"start": start_date_str, "end": end_date_str}
    time_clause = _pod_time_filter_clause(start_time_str, end_time_str, params)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT COUNT(*) AS pod_count,
                   AVG(duration_hours) AS avg_duration_hours
            FROM pod_sessions_capped
            WHERE start_ts >= %(start)s AND start_ts < (%(end)s::date + 1)
              AND status != 'active'
              {time_clause}
        """, params)
        row = cur.fetchone()
        return {
            "pod_count": row["pod_count"] or 0,
            "avg_duration_hours": float(row["avg_duration_hours"]) if row["avg_duration_hours"] is not None else None,
        }


def get_pod_start_hour_histogram(conn, start_date_str, end_date_str, start_time_str=None, end_time_str=None):
    """
    Pod-start counts by hour-of-day (0-23), local time (TIMEZONE from
    system_config, same convention used elsewhere for local-time grouping),
    pooled across every day in the range -- a 'modal day' view of when
    pods tend to get changed. Excludes status='active'.
    """
    params = {"start": start_date_str, "end": end_date_str}
    time_clause = _pod_time_filter_clause(start_time_str, end_time_str, params)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT EXTRACT(HOUR FROM start_ts AT TIME ZONE
                       (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1))::int AS hour_of_day,
                   COUNT(*) AS starts
            FROM pod_sessions_capped
            WHERE start_ts >= %(start)s AND start_ts < (%(end)s::date + 1)
              AND status != 'active'
              {time_clause}
            GROUP BY hour_of_day
            ORDER BY hour_of_day
        """, params)
        return cur.fetchall()


def get_pod_lifespan_histogram(conn, start_date_str, end_date_str, start_time_str=None, end_time_str=None):
    """
    Pod lifespan distribution, (capped) duration_hours bucketed into 5-hour
    blocks: 0=[0,5), 1=[5,10), ... 15=[75,80]. Excludes status='active'.
    """
    params = {"start": start_date_str, "end": end_date_str}
    time_clause = _pod_time_filter_clause(start_time_str, end_time_str, params)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT LEAST(FLOOR(duration_hours / 5.0)::int, 15) AS bucket_index,
                   COUNT(*) AS pod_count
            FROM pod_sessions_capped
            WHERE start_ts >= %(start)s AND start_ts < (%(end)s::date + 1)
              AND status != 'active'
              {time_clause}
            GROUP BY bucket_index
            ORDER BY bucket_index
        """, params)
        return cur.fetchall()


def get_pod_bg_relative_curve(conn, start_date_str, end_date_str, start_time_str=None, end_time_str=None):
    """
    For every ELIGIBLE, CLEAN pod session starting in [start_date, end_date],
    computes each pod's own BG delta from its own start-time reading,
    bucketed into 5-minute offsets from -288 to +288 (i.e. -24h to +24h
    around pod start), then returns the population mean plus 10th/90th
    percentile of that delta at each offset bucket. See
    get_pod_bg_window_counts() for the eligible/clean/excluded tallies used
    to document this filtering in the UI.

    Per-pod baseline (bucket 0) = that pod's own first BG reading with
    ts >= start_ts (not an interpolation -- layer2_five_minute_aggregate is
    on its own fixed wall-clock 5-min grid, not aligned to any pod's
    individual start time). Every pod's own delta series is 0 at its own
    start by construction -- the point is the *shape* of drift around each
    pod's own start, independent of where its BG happened to sit
    absolutely.

    ELIGIBLE: status != 'active' (real duration known) AND at least 24h of
    real elapsed time since start_ts (so a full +24h forward window could
    exist at all).

    Two separate contamination guards, both about a *different* pod's own
    start-transition bleeding into this pod's window:
      - FORWARD: readings are never pulled past this pod's own next_start_ts
        (if it started less than 24h in) -- otherwise hours between the real
        changeover and +24h would actually be the *next* pod's onset
        transition, mislabeled as this pod's tail.
      - BACKWARD ("clean" filter): a pod is excluded entirely from this
        query if the *immediately preceding* pod started less than 24h
        before this one. Reason: this pod's own -24h lookback would then
        reach into that previous pod's own 0-12h onset-transition window
        (per this same analysis, that transition takes ~12h to resolve),
        contaminating what should be the previous pod's settled tail with
        a different pod's start effect. Checking only the immediately-
        preceding pod is sufficient -- pod starts are strictly sequential,
        so if that one gap is >=24h, every earlier pod is even further back
        and can't reach into the window either.

    Units: layer2_five_minute_aggregate.bg is mmol/L (matches app-wide
    display convention) -- no unit conversion needed here, unlike the
    mg/dL-basis layer2_daily_band_stats table used elsewhere.
    """
    params = {"start": start_date_str, "end": end_date_str}
    time_clause = _pod_time_filter_clause(start_time_str, end_time_str, params)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            WITH pod_seq AS (
                SELECT start_ts, next_start_ts, status,
                       LAG(start_ts) OVER (ORDER BY start_ts) AS prev_start_ts
                FROM pod_sessions_capped
            ),
            clean_pods AS (
                SELECT start_ts, next_start_ts
                FROM pod_seq
                WHERE start_ts >= %(start)s AND start_ts < (%(end)s::date + 1)
                  AND status != 'active'
                  AND start_ts + interval '24 hours' <= now()
                  AND (prev_start_ts IS NULL OR start_ts - prev_start_ts >= interval '24 hours')
                  {time_clause}
            ),
            readings AS (
                SELECT
                    p.start_ts,
                    a.bg,
                    FLOOR(EXTRACT(EPOCH FROM (a.ts - p.start_ts)) / 300.0)::int AS bucket
                FROM clean_pods p
                JOIN layer2_five_minute_aggregate a
                  ON a.ts >= p.start_ts - interval '24 hours'
                 AND a.ts < LEAST(p.start_ts + interval '24 hours 5 minutes', COALESCE(p.next_start_ts, 'infinity'::timestamptz))
                WHERE a.bg IS NOT NULL
            ),
            baseline AS (
                SELECT start_ts, bg AS baseline_bg
                FROM (
                    SELECT start_ts, bg, bucket,
                           ROW_NUMBER() OVER (PARTITION BY start_ts ORDER BY bucket) AS rn
                    FROM readings
                    WHERE bucket >= 0
                ) ranked
                WHERE rn = 1
            ),
            deltas AS (
                SELECT r.start_ts, r.bucket, (r.bg - b.baseline_bg) AS delta
                FROM readings r
                JOIN baseline b ON b.start_ts = r.start_ts
                WHERE r.bucket BETWEEN -288 AND 288
            )
            SELECT
                bucket,
                COUNT(DISTINCT start_ts) AS pod_n,
                AVG(delta) AS mean_delta,
                STDDEV(delta) AS sd_delta,
                PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY delta) AS p10_delta,
                PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY delta) AS p90_delta
            FROM deltas
            GROUP BY bucket
            ORDER BY bucket
        """, params)
        return cur.fetchall()


def get_pod_bg_roc_curve(conn, start_date_str, end_date_str, start_time_str=None, end_time_str=None):
    """
    Companion to get_pod_bg_relative_curve(): same eligible/clean pod
    population and same -24h..+24h/5-min bucket grid, but instead of BG
    delta from the pod's own start reading, computes the *rate of change*
    of BG (first derivative, mmol/L per hour) at each bucket.

    RoC at bucket i = (bg[i] - bg[i-1]) / (5/60 hours), computed only
    between ADJACENT 5-minute buckets (bucket - prev_bucket = 1, via LAG
    ordered by bucket within each pod). If layer2_five_minute_aggregate has
    a gap (missing reading) at a point, the derivative across that gap is
    intentionally dropped rather than computed over the wider interval --
    a slope averaged across a 20-minute hole isn't the same statistic as a
    true adjacent-sample rate of change, even though it would carry the
    same mmol/L/hour units. This means RoC can have more missing points
    than the delta chart in places with sensor gaps; that's expected, not
    a bug.

    Units: mmol/L/hour, from layer2_five_minute_aggregate.bg (mmol/L,
    matches app-wide display convention).
    """
    params = {"start": start_date_str, "end": end_date_str}
    time_clause = _pod_time_filter_clause(start_time_str, end_time_str, params)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            WITH pod_seq AS (
                SELECT start_ts, next_start_ts, status,
                       LAG(start_ts) OVER (ORDER BY start_ts) AS prev_start_ts
                FROM pod_sessions_capped
            ),
            clean_pods AS (
                SELECT start_ts, next_start_ts
                FROM pod_seq
                WHERE start_ts >= %(start)s AND start_ts < (%(end)s::date + 1)
                  AND status != 'active'
                  AND start_ts + interval '24 hours' <= now()
                  AND (prev_start_ts IS NULL OR start_ts - prev_start_ts >= interval '24 hours')
                  {time_clause}
            ),
            readings AS (
                SELECT
                    p.start_ts,
                    a.bg,
                    FLOOR(EXTRACT(EPOCH FROM (a.ts - p.start_ts)) / 300.0)::int AS bucket
                FROM clean_pods p
                JOIN layer2_five_minute_aggregate a
                  ON a.ts >= p.start_ts - interval '24 hours'
                 AND a.ts < LEAST(p.start_ts + interval '24 hours 5 minutes', COALESCE(p.next_start_ts, 'infinity'::timestamptz))
                WHERE a.bg IS NOT NULL
            ),
            with_lag AS (
                SELECT
                    start_ts, bucket, bg,
                    LAG(bg) OVER (PARTITION BY start_ts ORDER BY bucket) AS prev_bg,
                    LAG(bucket) OVER (PARTITION BY start_ts ORDER BY bucket) AS prev_bucket
                FROM readings
            ),
            roc AS (
                SELECT start_ts, bucket, (bg - prev_bg) / (5.0 / 60.0) AS roc
                FROM with_lag
                WHERE prev_bucket IS NOT NULL
                  AND bucket - prev_bucket = 1
                  AND bucket BETWEEN -288 AND 288
            )
            SELECT
                bucket,
                COUNT(DISTINCT start_ts) AS pod_n,
                AVG(roc) AS mean_roc,
                STDDEV(roc) AS sd_roc,
                PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY roc) AS p10_roc,
                PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY roc) AS p90_roc
            FROM roc
            GROUP BY bucket
            ORDER BY bucket
        """, params)
        return cur.fetchall()


def get_pod_bg_window_counts(conn, start_date_str, end_date_str, start_time_str=None, end_time_str=None):
    """
    Documents the filtering behind get_pod_bg_relative_curve() for display
    in the UI: how many pods in the selected range were eligible at all
    (status resolved, 24h elapsed), and of those, how many were 'clean'
    (no adjacent-pod contamination in the -24h lookback) vs excluded.
    """
    params = {"start": start_date_str, "end": end_date_str}
    time_clause = _pod_time_filter_clause(start_time_str, end_time_str, params)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            WITH pod_seq AS (
                SELECT start_ts, status,
                       LAG(start_ts) OVER (ORDER BY start_ts) AS prev_start_ts
                FROM pod_sessions_capped
            ),
            eligible AS (
                SELECT start_ts, prev_start_ts
                FROM pod_seq
                WHERE start_ts >= %(start)s AND start_ts < (%(end)s::date + 1)
                  AND status != 'active'
                  AND start_ts + interval '24 hours' <= now()
                  {time_clause}
            )
            SELECT
                COUNT(*) AS eligible_count,
                COUNT(*) FILTER (
                    WHERE prev_start_ts IS NULL OR start_ts - prev_start_ts >= interval '24 hours'
                ) AS clean_count
            FROM eligible
        """, params)
        row = cur.fetchone()
        eligible = row["eligible_count"] or 0
        clean = row["clean_count"] or 0
        return {"eligible_count": eligible, "clean_count": clean, "excluded_count": eligible - clean}




def get_spider_thresholds(conn):
    """Returns the 10 spider_* metric_thresholds rows as {metric: {t_ideal, t_critical, formula, zone}}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT setting_key, params, label FROM metric_thresholds WHERE setting_key LIKE 'spider_%'")
        rows = cur.fetchall()
    result = {}
    for r in rows:
        metric = r['setting_key'].replace('spider_', '')
        p = r['params'] or {}
        result[metric] = {
            "t_ideal": float(p.get("t_ideal")),
            "t_critical": float(p.get("t_critical")),
            "formula": p.get("formula"),
            "zone": p.get("zone"),
            "label": r['label'],
        }
    return result


def update_spider_thresholds(conn, updates):
    """
    Updates t_ideal/t_critical for given spider_* metrics.
    updates: dict of {metric: {"t_ideal": x, "t_critical": y}}, e.g. {"tir": {"t_ideal": 85, "t_critical": 40}}
    value_low/value_high are kept as plain min/max of the two, matching the table's existing convention.
    """
    with conn.cursor() as cur:
        for metric, vals in updates.items():
            t_ideal = float(vals["t_ideal"])
            t_critical = float(vals["t_critical"])
            value_low = min(t_ideal, t_critical)
            value_high = max(t_ideal, t_critical)
            cur.execute("""
                UPDATE metric_thresholds
                SET value_low = %s,
                    value_high = %s,
                    params = jsonb_set(jsonb_set(params, '{t_ideal}', to_jsonb(%s::numeric)), '{t_critical}', to_jsonb(%s::numeric)),
                    updated_at = now()
                WHERE setting_key = %s
            """, (value_low, value_high, t_ideal, t_critical, f"spider_{metric}"))
    conn.commit()

def refresh_analysis_views(conn, specific_views=None):
    """
    Dynamically discovers and refreshes materialized views in the public schema.
    If specific_views is provided (list of names), only those are refreshed.
    Otherwise, all discovered views are refreshed.
    """
    try:
        with conn.cursor() as cur:
            if specific_views:
                views_unordered = [v for v in specific_views]
            else:
                # 1. Discover all materialized views in public schema
                cur.execute("""
                    SELECT matviewname 
                    FROM pg_matviews 
                    WHERE schemaname = 'public'
                    ORDER BY matviewname;
                """)
                views_unordered = [row[0] for row in cur.fetchall()]
            
            # Hardcoded dependency order for core views
            core_order = [
                'layer2_profile_schedule',
                'layer2_temp_basal_impact',
                'layer2_hourly_stats',
                'layer2_agp_raw',
                'layer4_agp_periods',
                'layer2_daily_band_stats',
                'layer2_hypo_episodes',
                'layer2_daily_period_stats',
                'layer2_daily_risk_stats'
            ]
            
            views = []
            for v in core_order:
                if v in views_unordered:
                    views.append(v)
            for v in views_unordered:
                if v not in views:
                    views.append(v)
            
            if not views:
                print("[db] No materialized views found to refresh.")
                return

            print(f"[db] Found {len(views)} materialized views to refresh: {', '.join(views)}")
            
            # 2. Refresh each one
            # Note: We do this sequentially. If dependencies exist, Postgres might error 
            # if we don't refresh in order, but REFRESH MATERIALIZED VIEW doesn't cascade 
            # in the same way. Ideally, views should be refreshed in dependency order.
            # For now, we rely on the fact that our current views typically don't depend 
            # on other *unrefreshed* materialized views in a way that blocks the refresh 
            # (unless using CONCURRENTLY which we aren't). 
            # If sophisticated dependency ordering is needed later, we can query pg_depend.
            failed_views = []
            for view in views:
                print(f"[db] Refreshing {view}...")
                # Use CONCURRENTLY to avoid locking the view for concurrent reads
                # This requires a UNIQUE INDEX on the view (which we must ensure exists)
                try:
                    cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view};")
                    conn.commit()
                except (psycopg2.errors.ObjectNotInPrerequisiteState, psycopg2.errors.FeatureNotSupported):
                    # Fallback if no unique index exists, OR if the view has never been
                    # populated (fresh install) -- CONCURRENTLY requires a prior plain
                    # REFRESH before it can be used on a given matview.
                    conn.rollback() # Rollback the failed statement
                    print(f"[db] WARN: Could not refresh {view} concurrently (missing index, or not yet populated). Falling back to locking refresh.")
                    try:
                        cur.execute(f"REFRESH MATERIALIZED VIEW {view};")
                        conn.commit()
                    except Exception as e_inner:
                        conn.rollback()
                        print(f"[db] View locking refresh failed for {view}: {e_inner}")
                        failed_views.append(view)
                except Exception as e:
                    conn.rollback()
                    print(f"[db] View refresh failed for {view}: {e}")
                    failed_views.append(view)
                
        if failed_views:
            print(f"[db] View refresh completed WITH FAILURES ({len(failed_views)}/{len(views)}): {', '.join(failed_views)}")
        else:
            print("[db] All analysis views refreshed successfully.")
    except Exception as e:
        print(f"[db] Master refresh fetch failed: {e}")
        conn.rollback()

def compute_basal_for_date_range(conn, start_date, end_date):
    """Compute and permanently store basal data for a specific date range.
    Calls the SQL function compute_basal_for_range() which UPSERTs into layer2_basal_5min.
    start_date and end_date should be date objects or YYYY-MM-DD strings.
    Returns the number of rows inserted.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT compute_basal_for_range(%s::date, %s::date);", (str(start_date), str(end_date)))
            result = cur.fetchone()
            conn.commit()
            rows = result[0] if result else 0
            print(f"[db] Computed basal for {start_date} to {end_date}: {rows} rows")
            return rows
    except Exception as e:
        conn.rollback()
        print(f"[db] Basal computation failed for {start_date} to {end_date}: {e}")
        return 0


def _get_decay_kernel(dia_hours=3.0, peak=65, step_min=5):
    end = dia_hours * 60.0
    steps = int(end / step_min)
    tau = peak * (1.0 - peak / end) / (1.0 - 2.0 * peak / end)
    a = 2.0 * tau / end
    S = 1.0 / (1.0 - a + (1.0 + a) * math.exp(-end / tau))
    kernel = []
    for i in range(steps):
        minsAgo = i * step_min
        if minsAgo >= end:
            kernel.append(0.0)
        else:
            fraction = 1.0 - S * (1.0 - a) * (((minsAgo**2) / (tau * end * (1.0 - a)) - minsAgo / tau - 1.0) * math.exp(-minsAgo / tau) + 1.0)
            kernel.append(fraction)
    return kernel

def compute_and_update_screen_iob(conn, start_ts=None, end_ts=None):
    """
    Computes recent temp basal deviation IOB and updates devicestatus
    so that devicestatus.iob reflects the screen-matched IOB:
      iob = bolus_iob + basal_iob
    """
    try:
        now = datetime.now(timezone.utc)
        if end_ts is None:
            end_ts = now + timedelta(minutes=5)
        if start_ts is None:
            start_ts = now - timedelta(hours=6)

        kernel = _get_decay_kernel(3.0, 65, 5)
        lookback_delta = timedelta(hours=3.5)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                UPDATE devicestatus
                SET loop_iob = (raw_json->'openaps'->'iob'->>'iob')::numeric,
                    loop_basal_iob = (raw_json->'openaps'->'iob'->>'basaliob')::numeric,
                    bolus_iob = CASE 
                        WHEN (raw_json->'openaps'->'iob'->>'iob') IS NOT NULL 
                             AND (raw_json->'openaps'->'iob'->>'basaliob') IS NOT NULL
                        THEN ROUND(((raw_json->'openaps'->'iob'->>'iob')::numeric - (raw_json->'openaps'->'iob'->>'basaliob')::numeric), 3)
                        WHEN (raw_json->'openaps'->'iob'->>'iob') IS NOT NULL
                        THEN (raw_json->'openaps'->'iob'->>'iob')::numeric
                        ELSE bolus_iob
                    END
                WHERE ts >= %s AND ts <= %s
                  AND raw_json->'openaps'->'iob' IS NOT NULL
                  AND (bolus_iob IS NULL OR loop_iob IS NULL);
            """, (start_ts, end_ts))

            cur.execute("""
                SELECT ts, bolus_iob, loop_iob
                FROM devicestatus
                WHERE ts >= %s AND ts <= %s
                ORDER BY ts ASC
            """, (start_ts, end_ts))
            dev_rows = cur.fetchall()
            if not dev_rows:
                return 0

            fetch_start = start_ts - lookback_delta
            cur.execute("""
                SELECT minute_ts, COALESCE((actual_rate - base_rate) * (5.0/60.0), 0.0) as delta_units
                FROM layer2_basal_5min
                WHERE minute_ts >= %s AND minute_ts <= %s
                ORDER BY minute_ts ASC
            """, (fetch_start, end_ts))
            basal_rows = cur.fetchall()

            if not basal_rows:
                cur.execute("""
                    UPDATE devicestatus
                    SET basal_iob = 0.0,
                        iob = COALESCE(bolus_iob, loop_iob, iob)
                    WHERE ts >= %s AND ts <= %s AND (basal_iob IS NULL);
                """, (start_ts, end_ts))
                conn.commit()
                return len(dev_rows)

            basal_map = {b["minute_ts"]: float(b["delta_units"]) for b in basal_rows}
            update_tuples = []
            for dr in dev_rows:
                d_ts = dr["ts"]
                b_iob = 0.0
                for i, k_val in enumerate(kernel):
                    target_m = d_ts - timedelta(minutes=i*5)
                    snapped_m = target_m.replace(second=0, microsecond=0)
                    snapped_m = snapped_m - timedelta(minutes=snapped_m.minute % 5)
                    delta_u = basal_map.get(snapped_m, 0.0)
                    b_iob += delta_u * k_val

                b_iob_rounded = round(b_iob, 2)
                b_bolus = float(dr["bolus_iob"]) if dr["bolus_iob"] is not None else (float(dr["loop_iob"]) if dr["loop_iob"] is not None else 0.0)
                screen_iob = round(b_bolus + b_iob_rounded, 2)
                update_tuples.append((b_iob_rounded, screen_iob, d_ts))

            if update_tuples:
                psycopg2.extras.execute_batch(
                    cur,
                    "UPDATE devicestatus SET basal_iob = %s, iob = %s WHERE ts = %s",
                    update_tuples,
                    page_size=500
                )
            conn.commit()
            return len(update_tuples)
    except Exception as e:
        conn.rollback()
        print(f"[db] compute_and_update_screen_iob error: {e}")
        return 0


def populate_5min_aggregate(conn, start_ts, end_ts):
    """
    Snaps all raw metrics to the 5-minute aggregate table for the given range.
    Calls the SQL function populate_5min_aggregate().
    """
    try:
        compute_and_update_screen_iob(conn, start_ts, end_ts)
        with conn.cursor() as cur:
            cur.execute("SELECT populate_5min_aggregate(%s, %s);", (start_ts, end_ts))
            result = cur.fetchone()
            conn.commit()
            rows = result[0] if result else 0
            print(f"[db] Populated 5min aggregate for {start_ts} to {end_ts}: {rows} rows")
            return rows
    except Exception as e:
        conn.rollback()
        print(f"[db] 5min aggregate population failed: {e}")
        return 0

# Warm-up fetched before the requested window so the deviation state machine
# below enters it with the same carried state AAPS would have. Sized from 60
# days of live data: positive-deviation runs (what keeps `absorbing`/`uam`
# alive) have a p99 of 307 min and a 60-day maximum of 490 min. 6h covers
# ~99%; a longer run than this shows a short stretch of off-colour bars at the
# left edge, which is accepted rather than engineered around.
DEV_WARMUP_HOURS = 6

# Constants.DEVIATION_TO_BE_EQUAL, mg/dL per 5 min, from AAPS core Constants.kt
DEVIATION_TO_BE_EQUAL = 2.0


def _classify_deviations(rows):
    """
    Port of AAPS's per-bucket deviation classification, from
    IobCobOref1Worker.kt (the autosens loop) plus the colour precedence in
    PrepareIobAutosensGraphDataWorker.kt. Sets row['dev_class'] in place.

    This is deliberately a left-to-right stateful pass rather than a row-wise
    SQL CASE, because AAPS's classification genuinely carries state between
    buckets and cannot be expressed per-row:

      * `absorbing`  -- once a meal starts, stays csf while deviations remain
                        positive, even after COB decays to zero. This is what
                        keeps the post-meal tail grey instead of green.
      * `uam`        -- self-sustaining: the branch condition includes `|| uam`
                        and re-arms it whenever deviation > 0, so a UAM run
                        continues until a negative deviation breaks it.
      * `mealStartCounter` -- forces uam for 9 cycles (45 min) after a meal
                        absorption boundary.

    `rows` must be in ascending ts order and include the warm-up prefix.

    Known divergence from AAPS: AAPS recomputes COB itself during this loop,
    whereas our `cob` is sampled per bucket from devicestatus. Where devicestatus
    is missing, `absorbing`/`mealCarbs` will drift from what the phone shows.
    """
    absorbing = False
    uam = False
    meal_carbs = 0.0
    # In AAPS AutosensDataObject.kt, mealStartCounter is initialized to 999 so that
    # cold-start does not spuriously trigger the `< 9` (45 min post-meal) guard.
    meal_start_counter = 999
    dev_type = ''

    for row in rows:
        dev = row.get('dev_mgdl')
        if dev is None:
            # No BG or no BGI for this bucket. AAPS skips these entirely rather
            # than folding them into the state, so leave the carried state alone.
            row['dev_class'] = None
            continue
        dev = float(dev)

        cob = float(row.get('cob') or 0.0)
        iob = float(row.get('iob') or 0.0)
        carbs = float(row.get('carbs') or 0.0)
        # profile.getBasal(bgTime) -- the PROFILE rate at that time, NOT the
        # delivered rate. Confirmed from IobCobOref1Worker.kt. Using delivered
        # basal would degenerate: it is 0 for ~57% of buckets on this setup, so
        # `iob > 2 * 0` would mark nearly every zero-temp bucket as UAM.
        current_basal = float(row.get('scheduled_basal') or 0.0)

        if carbs > 0:
            meal_carbs += carbs

        if cob > 0 or absorbing or meal_carbs > 0:
            absorbing = dev > 0
            # stop excluding positive deviations once a meal has been absorbing
            # for >5h (60 cycles)
            if meal_start_counter > 60 and cob < 0.5:
                absorbing = False
            if not absorbing and cob < 0.5:
                meal_carbs = 0.0
            if dev_type != 'csf':
                meal_start_counter = 0
            meal_start_counter += 1
            dev_type = 'csf'
        else:
            # UAM threshold: In AAPS, iob.iob > 2 * currentBasal.
            uam_threshold = 2.0 * current_basal
            if iob > uam_threshold or uam or meal_start_counter < 9:
                meal_start_counter += 1
                uam = dev > 0
                dev_type = 'uam'
            else:
                dev_type = 'non-meal'

        # Colour precedence:
        # 1. |dev| < DEVIATION_TO_BE_EQUAL (2.0 mg/dL): noise floor (EQUAL / black).
        # 2. Meal carbs absorbing (csf): active meal absorption (COB / grey).
        # 3. dev < 0: dropping faster than expected (SENS / red).
        # 4. dev_type == 'uam': genuine unannounced rise >= 2.0 mg/dL (UAM / yellow).
        # 5. Fallback positive deviation: un-modeled rise / resistance (RES / green).
        if abs(dev) < DEVIATION_TO_BE_EQUAL:
            row['dev_class'] = 'EQUAL'    # black -- the noise floor
        elif dev_type == 'csf':
            row['dev_class'] = 'COB'      # grey -- active carb absorption
        elif dev < 0:
            row['dev_class'] = 'SENS'     # red -- sensitivity / dropping faster than expected
        elif dev_type == 'uam':
            row['dev_class'] = 'UAM'      # yellow -- genuine unannounced rise >= 2.0 mg/dL
        else:
            row['dev_class'] = 'RES'      # green -- resistance

    return rows


def get_daily_chart_data_range(conn, start_date_str, end_date_str):
    """
    Returns 5-minute aggregate data for every local day in
    [start_date_str, end_date_str] inclusive. Backs the Daily page's
    continuous-scroll chart (promoted from the Sandbox prototype, 23 Aug
    2026) -- the frontend holds a multi-day buffer in memory and pans
    across it rather than re-fetching a single day at a time. Replaces the
    old single-day get_daily_chart_data(), which had no other callers.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                -- AAPS-style DEV/-BGI series, derived at read time (see
                -- docs/analysis/trace-dev-bgi-deviation-chart-plan.md).
                --
                -- Stored a.deviation/a.bgi are mmol/L as scraped from AAPS's
                -- `reason` string, which AAPS unit-converts to profile units
                -- before upload. a.deviation is additionally 30-min scaled
                -- (oref's `30/5 * (minDelta - bgi)`) because it exists to feed
                -- the prediction curves, so it is NOT the per-bucket quantity
                -- AAPS draws.
                --
                -- We therefore rebuild deviation from AAPS's own definition.
                -- From IobCobOref1Worker.kt, verbatim:
                --     delta = bg - bucketedData[i + 1].recalculated
                --     val bgi = -iob.activity * sens * 5
                --     val deviation = delta - bgi
                -- i.e. exactly (5-min dBG) - BGI, in mg/dL. Our bg_roc/bgi are
                -- mmol/L, hence the 18.0182. This is not an approximation of
                -- AAPS's quantity, it is the same quantity.
                --
                -- neg_bgi negates because AAPS's graph plots -BGI against the
                -- bars: PrepareIobAutosensGraphDataWorker builds the series as
                -- `iob.activity * sens * 5.0`, i.e. the algorithm's bgi without
                -- its leading minus.
                --
                -- dev_class is NOT computed here -- AAPS's classification
                -- carries state between buckets, so it runs as a left-to-right
                -- pass in _classify_deviations() below.
                WITH win AS (
                    SELECT ((%s::date)::timestamp
                            AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1)
                           ) AS start_ts
                )
                SELECT 
                    a.ts AT TIME ZONE 'UTC' as ts,
                    to_char(a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'YYYY-MM-DD HH24:MI:SS') as local_ts,
                    a.bg, a.iob, a.cob, a.isf, a.deviation, a.bgi,
                    a.basal_rate, a.bolus_insulin, a.carbs,
                    a.scheduled_basal, a.deviation_source,
                    (a.bg_roc - a.bgi) * 18.0182 AS dev_mgdl,
                    -a.bgi * 18.0182            AS neg_bgi_mgdl,
                    (a.ts >= (SELECT start_ts FROM win)) AS in_window
                FROM layer2_five_minute_aggregate a
                WHERE a.ts >= (SELECT start_ts FROM win) - (%s * INTERVAL '1 hour')
                  AND a.day <= %s::date
                ORDER BY a.ts ASC
            """, (start_date_str, DEV_WARMUP_HOURS, end_date_str))
            rows = cur.fetchall()

            # Classify across the warm-up too, then drop it -- the warm-up
            # exists only to bring the carried state up to date before the
            # first visible bucket.
            _classify_deviations(rows)
            return [r for r in rows if r.pop('in_window', True)]
    except Exception as e:
        print(f"[db] get_daily_chart_data_range failed: {e}")
        conn.rollback()
        return []


def get_exact_basal_events_range(conn, start_date_str, end_date_str):
    """
    Returns exact Temp Basal treatments in [start_date_str - 1d, end_date_str + 1d]
    for high-fidelity exact-timestamp stepped plotting on Traces.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    t.ts AT TIME ZONE 'UTC' as ts,
                    to_char(t.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'YYYY-MM-DD HH24:MI:SS') as local_ts,
                    t.duration,
                    COALESCE(t.absolute, t.rate) as rate
                FROM treatments t
                WHERE t.event_type = 'Temp Basal'
                  AND (t.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1))::date BETWEEN %s::date - INTERVAL '1 day' AND %s::date + INTERVAL '1 day'
                  AND COALESCE(t.absolute, t.rate) IS NOT NULL
                ORDER BY t.ts ASC
            """, (start_date_str, end_date_str))
            return cur.fetchall()
    except Exception as e:
        print(f"[db] get_exact_basal_events_range failed: {e}")
        conn.rollback()
        return []


def get_sandbox_chart_data(conn, target_date_str):
    """
    Sandbox's own copy of get_daily_chart_data - deliberately independent so
    experimentation here can freely change the query without touching the
    real Daily page. Starts identical; expected to diverge over time.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    a.ts AT TIME ZONE 'UTC' as ts,
                    to_char(a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'YYYY-MM-DD HH24:MI:SS') as local_ts,
                    a.bg, a.iob, a.cob, a.isf, a.deviation, a.bgi,
                    a.basal_rate, a.bolus_insulin, a.carbs,
                    a.scheduled_basal, a.deviation_source
                FROM layer2_five_minute_aggregate a
                WHERE a.day = %s::date
                ORDER BY a.ts ASC
            """, (target_date_str,))
            return cur.fetchall()
    except Exception as e:
        print(f"[db] get_sandbox_chart_data failed: {e}")
        conn.rollback()
        return []


def get_sandbox_chart_data_range(conn, start_date_str, end_date_str):
    """
    Range variant for the sandbox continuous-scroll prototype (23 Aug 2026,
    since promoted to the Daily page -- see get_daily_chart_data_range()).
    Sandbox itself is blank again, ready for whatever's built here next; this
    stays in place as the workspace's own independent copy rather than being
    deleted, per the project's established Sandbox convention.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    a.ts AT TIME ZONE 'UTC' as ts,
                    to_char(a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'YYYY-MM-DD HH24:MI:SS') as local_ts,
                    a.bg, a.iob, a.cob, a.isf, a.deviation, a.bgi,
                    a.basal_rate, a.bolus_insulin, a.carbs,
                    a.scheduled_basal, a.deviation_source
                FROM layer2_five_minute_aggregate a
                WHERE a.day BETWEEN %s::date AND %s::date
                ORDER BY a.ts ASC
            """, (start_date_str, end_date_str))
            return cur.fetchall()
    except Exception as e:
        print(f"[db] get_sandbox_chart_data_range failed: {e}")
        conn.rollback()
        return []


def get_calendar_sparkline_data(conn, start_date_str, end_date_str):
    """Lightweight per-day BG series for Calendar's sparkline overlay (31
    Aug 2026) -- ts+bg only, not the full multi-column payload
    get_daily_chart_data_range() returns (iob/cob/isf/basal/etc aren't
    needed for a sparkline and would be wasted bandwidth across dozens of
    buffered days x ~288 points/day each).

    Returns {day_str: [[minute_of_day, bg], ...], ...}. minute_of_day (a
    float, 0-1440) lets the frontend place each point correctly along a
    24h x-axis even across data gaps (sensor warmup/disconnection are
    common), rather than assuming even 5-minute spacing from array index
    alone. bg is mmol/L, matching layer2_five_minute_aggregate's own
    convention (confirmed via a live row check, not assumed) and the rest
    of the app's display convention.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    a.day::text as day,
                    EXTRACT(EPOCH FROM (
                        (a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1))
                        - date_trunc('day', a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1))
                    )) / 60 as minute_of_day,
                    a.bg
                FROM layer2_five_minute_aggregate a
                WHERE a.day BETWEEN %s::date AND %s::date AND a.bg IS NOT NULL
                ORDER BY a.ts ASC
            """, (start_date_str, end_date_str))
            rows = cur.fetchall()
    except Exception as e:
        print(f"[db] get_calendar_sparkline_data failed: {e}")
        conn.rollback()
        return {}

    by_day = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append([
            round(float(r["minute_of_day"]), 1),
            float(r["bg"])
        ])
    return by_day


def get_dbsize_stats(conn):
    stats = []
    with conn.cursor() as cur:
        # Read the live public schema, including materialized views used by
        # Layer 2.  Keep the byte size separate from its human-readable label
        # so callers can sort accurately without parsing PostgreSQL text.
        cur.execute("""
            SELECT
                c.relname AS relation_name,
                CASE c.relkind
                    WHEN 'm' THEN 'Materialized view'
                    WHEN 'p' THEN 'Partitioned table'
                    ELSE 'Table'
                END AS relation_type,
                COALESCE(s.n_live_tup, c.reltuples, 0)::bigint AS row_count,
                pg_total_relation_size(c.oid) AS size_bytes,
                pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p', 'm')
            ORDER BY pg_total_relation_size(c.oid) DESC, c.relname;
        """)
        basic_stats = cur.fetchall()
        
        # 2. Add time ranges for known tables
        # Heuristic: try to find a time column
        time_cols = ["ts", "created_at", "day", "date", "last_ts", "Date", "minute_ts", "start_time", "minute_bucket_start"]
        
        for row in basic_stats:
            table = row[0]
            relation_type = row[1]
            count = row[2]
            size_bytes = row[3]
            size = row[4]
            min_ts = None
            max_ts = None

            # Only query time ranges if there are rows
            if count > 0:
                try:
                    # Find a valid time column for this table
                    # This is a bit expensive (N queries), but for <20 tables it's fine for a dashboard
                    # We prioritize 'ts' > 'created_at' > 'day' > 'date' > 'last_ts' > 'Date' ...
                    col_to_use = None
                    # Use lower() to match case-insensitively due to Postgres folding
                    cur.execute("""
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = %s
                          AND column_name = ANY(%s)
                    """, (table, time_cols))
                    cols = [r[0] for r in cur.fetchall()]
                    
                    # Also check pg_attribute for materialized views (which info_schema might miss)
                    if not cols:
                         cur.execute("""
                             SELECT a.attname
                             FROM pg_attribute a
                             JOIN pg_class c ON c.oid = a.attrelid
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE n.nspname = 'public'
                               AND c.relname = %s
                               AND a.attnum > 0
                               AND NOT a.attisdropped
                         """, (table,))
                         cols = [r[0] for r in cur.fetchall()]
                    
                    for candidate in time_cols:
                        if candidate in cols:
                            col_to_use = candidate
                            break
                    
                    if col_to_use:
                        cur.execute(sql.SQL("SELECT MIN({column}), MAX({column}) FROM {table}").format(
                            column=sql.Identifier(col_to_use),
                            table=sql.Identifier('public', table),
                        ))
                        times = cur.fetchone()
                        if times:
                            min_ts = times[0]
                            max_ts = times[1]
                            
                            # Convert to ISO string if it's a datetime/date object
                            if isinstance(min_ts, (datetime, )):
                                min_ts = min_ts.isoformat()
                            if isinstance(max_ts, (datetime, )):
                                max_ts = max_ts.isoformat()
                                
                            # Handle date objects
                            if hasattr(min_ts, 'strftime') and not isinstance(min_ts, str):
                                min_ts = min_ts.strftime('%Y-%m-%d')
                            if hasattr(max_ts, 'strftime') and not isinstance(max_ts, str):
                                max_ts = max_ts.strftime('%Y-%m-%d')

                except Exception as e:
                    print(f"Error getting time for {table}: {e}")
                    conn.rollback() # Vital! If a query fails, the transaction is aborted. Reset it.
                    pass
            
            stats.append({
                "table": table,
                "relation_type": relation_type,
                "rows": count, 
                "size": size,
                "size_bytes": size_bytes,
                "first_record": str(min_ts) if min_ts else "-",
                "last_record": str(max_ts) if max_ts else "-"
            })
            
    return stats

def get_retention_stats(conn):
    """
    Returns storage metrics and growth rates for Tier 1 and Tier 2.
    Tier 1 (Raw Nightscout Logs): devicestatus
    Tier 2 (Clinical History): cgm_readings, treatments
    """
    stats = {
        "tier1": {"size_bytes": 0, "rows": 0, "monthly_growth_bytes": 0, "monthly_growth_rows": 0},
        "tier2": {"size_bytes": 0, "rows": 0, "monthly_growth_bytes": 0, "monthly_growth_rows": 0},
        "disk": {"total": 0, "used": 0, "free": 0},
        "config": {"t1": 120, "t2": 365, "enabled": False}
    }
    
    # Get current config
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM system_config WHERE key IN ('retention_days_raw', 'retention_days_clinical', 'retention_enabled')")
        conf_rows = cur.fetchall()
        for k, v in conf_rows:
            if k == 'retention_days_raw': stats["config"]["t1"] = int(v)
            if k == 'retention_days_clinical': stats["config"]["t2"] = int(v)
            if k == 'retention_enabled': stats["config"]["enabled"] = (v.lower() == 'true')

    # Get real disk usage for the DB volume
    try:
        du = shutil.disk_usage("/var/lib/postgresql/data") # Path inside container
        stats["disk"]["total"] = du.total
        stats["disk"]["used"] = du.used
        stats["disk"]["free"] = du.free
    except Exception:
        # Fallback to root if specific mount isn't reachable
        try:
            du = shutil.disk_usage("/")
            stats["disk"]["total"] = du.total
            stats["disk"]["used"] = du.used
            stats["disk"]["free"] = du.free
        except Exception: pass

    with conn.cursor() as cur:
        # 1. Get current sizes and counts
        try:
            cur.execute("SELECT pg_total_relation_size('devicestatus'), (SELECT count(*) FROM devicestatus)")
            t1 = cur.fetchone()
            stats["tier1"]["size_bytes"] = t1[0] or 0
            stats["tier1"]["rows"] = t1[1] or 0
            
            cur.execute("SELECT (pg_total_relation_size('cgm_readings') + pg_total_relation_size('treatments')), (SELECT count(*) FROM cgm_readings) + (SELECT count(*) FROM treatments)")
            t2 = cur.fetchone()
            stats["tier2"]["size_bytes"] = t2[0] or 0
            stats["tier2"]["rows"] = t2[1] or 0
        except Exception as e:
            print(f"[db] Error getting relation sizes: {e}")
            conn.rollback()

        # 2. Calculate monthly growth (average over last 3 full months)
        def get_growth(table):
            try:
                cur.execute(f"""
                    SELECT AVG(cnt) FROM (
                        SELECT date_trunc('month', ts), count(*) as cnt 
                        FROM {table} 
                        WHERE ts < date_trunc('month', now())
                          AND ts >= date_trunc('month', now()) - interval '3 months'
                        GROUP BY 1
                    ) sub
                """)
                res = cur.fetchone()
                return float(res[0]) if res and res[0] else 0
            except Exception as e:
                print(f"[db] Error getting growth for {table}: {e}")
                conn.rollback()
                return 0

        t1_growth_rows = get_growth("devicestatus")
        cgm_growth_rows = get_growth("cgm_readings")
        treat_growth_rows = get_growth("treatments")
        
        stats["tier1"]["monthly_growth_rows"] = int(t1_growth_rows)
        # Disk per row estimate
        if stats["tier1"]["rows"] > 0:
            avg_row_size = stats["tier1"]["size_bytes"] / stats["tier1"]["rows"]
            stats["tier1"]["monthly_growth_bytes"] = int(avg_row_size * t1_growth_rows)
        
        stats["tier2"]["monthly_growth_rows"] = int(cgm_growth_rows + treat_growth_rows)
        if stats["tier2"]["rows"] > 0:
            avg_row_size = stats["tier2"]["size_bytes"] / stats["tier2"]["rows"]
            stats["tier2"]["monthly_growth_bytes"] = int(avg_row_size * (cgm_growth_rows + treat_growth_rows))

    return stats

def get_all_config(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM system_config")
        return {row[0]: row[1] for row in cur.fetchall()}

def upsert_ingestion_state(conn, key, val):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingestion_state (source, last_ts) VALUES (%s, %s)
            ON CONFLICT (source) DO UPDATE SET last_ts = EXCLUDED.last_ts
        """, (key, val))

def record_coverage(conn, stream: str, start_ts: datetime, end_ts: datetime, method: str):
    """
    Records a successful ingestion window in the coverage map.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingestion_coverage (stream, start_ts, end_ts, method)
            VALUES (%s, %s, %s, %s)
        """, (stream, start_ts, end_ts, method))

def get_missing_windows(conn, date_str: str, stream: str):
    """
    Identifies 15-minute windows in a given day that have no ingestion coverage.
    Returns a list of (start_ts, end_ts) tuples.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Generate 15-min slots for the day
        # Optimization: Use fixed timestamps for the range check to ensure index hits
        day_start = f"{date_str} 00:00:00"
        day_end = f"{date_str} 23:59:59"
        
        cur.execute("""
            WITH time_slots AS (
                SELECT generate_series(
                    %s::timestamptz, 
                    %s::timestamptz, 
                    '15 minutes'::interval
                ) AS slot
            )
            SELECT slot 
            FROM time_slots ts
            LEFT JOIN ingestion_coverage ic 
              ON ts.slot >= ic.start_ts AND ts.slot < ic.end_ts
              AND ic.stream = %s
            WHERE ic.id IS NULL
            ORDER BY slot ASC;
        """, (day_start, day_end, stream))
        
        missing_slots = [row['slot'] for row in cur.fetchall()]
        if not missing_slots:
            return []
            
        # Group consecutive slots into windows
        windows = []
        if missing_slots:
            group_start = missing_slots[0]
            prev = missing_slots[0]
            for slot in missing_slots[1:]:
                if slot > prev + timedelta(minutes=15):
                    windows.append((group_start, prev + timedelta(minutes=15)))
                    group_start = slot
                prev = slot
            windows.append((group_start, prev + timedelta(minutes=15)))
            
        return windows

def has_recent_gaps(conn):
    """
    Checks if any stream has gaps in the last 24 hours.
    Used by the watchdog.
    """
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    
    # Check for gaps in entries, treatments, and devicestatus
    for stream in ['entries', 'treatments', 'devicestatus']:
        # We check the date of today and yesterday
        for d_dt in [yesterday, now]:
            d_str = d_dt.strftime("%Y-%m-%d")
            gaps = get_missing_windows(conn, d_str, stream)
            if gaps:
                # If the gap is in the future (relative to 'now'), ignore it
                # get_missing_windows generates slots for the FULL day
                real_gaps = [g for g in gaps if g[0] < now - timedelta(minutes=15)]
                if real_gaps:
                    return True
    return False
    conn.commit()

def get_daily_counts(conn, start_date, end_date):
    """
    Aggregates daily record counts across main clinical tables.
    Updated to include Sync Integrity (Gap Detection).
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # 1. Base counts using index-friendly range comparisons
            # We use generate_series to create the list of dates
            query = """
            WITH config AS (SELECT value as tz FROM system_config WHERE key = 'TIMEZONE' LIMIT 1),
            days AS (
                SELECT d::date as day_dt
                FROM generate_series(%s::date, %s::date, '1 day'::interval) d
            )
            SELECT 
                day_dt as date,
                (SELECT COUNT(*) FROM cgm_readings WHERE ts >= (day_dt::timestamp AT TIME ZONE (SELECT tz FROM config)) AND ts < (day_dt + interval '1 day')::timestamp AT TIME ZONE (SELECT tz FROM config)) as cgm,
                (SELECT COUNT(*) FROM treatments WHERE ts >= (day_dt::timestamp AT TIME ZONE (SELECT tz FROM config)) AND ts < (day_dt + interval '1 day')::timestamp AT TIME ZONE (SELECT tz FROM config)) as treatments,
                (SELECT COUNT(*) FROM devicestatus WHERE ts >= (day_dt::timestamp AT TIME ZONE (SELECT tz FROM config)) AND ts < (day_dt + interval '1 day')::timestamp AT TIME ZONE (SELECT tz FROM config)) as device
            FROM days
            ORDER BY day_dt DESC;
            """
            cur.execute(query, (start_date, end_date))
            rows = cur.fetchall()
            
            # 2. Integrity Check (Timeline Coverage)
            cur.execute("""
                SELECT stream, start_ts, end_ts, method 
                FROM ingestion_coverage 
                WHERE start_ts::date >= %s AND end_ts::date <= %s
            """, (start_date, end_date))
            coverage = cur.fetchall()

            # 3. Legacy Status (Batch fetch to avoid N+1)
            # Legacy keys were 'backfill_YYYY-MM-DD'
            cur.execute("""
                SELECT source, last_ts 
                FROM ingestion_state 
                WHERE source LIKE 'backfill_%%'
            """)
            legacy_map = {row[0]: row[1] for row in cur.fetchall()}

            results = []
            for r in rows:
                d = r['date']
                d_str = d.strftime("%Y-%m-%d")
                
                # Check coverage windows
                day_cov = [c for c in coverage if c['start_ts'].date() <= d <= c['end_ts'].date()]
                
                is_live = any(c['method'] == 'polling' for c in day_cov)
                is_backfill = any(c['method'] == 'backfill' for c in day_cov)
                
                # Legacy keys in this system use underscores: backfill_YYYY_MM_DD
                legacy_key = f"backfill_{d_str.replace('-', '_')}"
                has_legacy = legacy_key in legacy_map

                status = "Gaps"
                has_data = (r['cgm'] > 0 or r['treatments'] > 0)
                if day_cov:
                    if not has_data:
                        status = "Coverage/Empty"  # coverage recorded but no records — indicates a stale/reset DB
                    elif is_live and not is_backfill:
                        status = "Live"
                    else:
                        status = "Synced"
                elif has_legacy:
                    status = "Synced" if has_data else "Coverage/Empty"

                results.append({
                    "date": d_str,
                    "cgm": r['cgm'],
                    "treatments": r['treatments'],
                    "device": r['device'],
                    "backfilled": status != "-",
                    "sync_status": status
                })
                
            return results
    except Exception as e:
        print(f"[db] Error in get_daily_counts: {e}")
        conn.rollback()
        return []

def get_latest_metrics(conn):
    """
    Returns a dictionary with the latest BGL, delta, IOB, and COB.
    Used by the ESP32 display API.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1. Get latest 2 CGM readings to calculate delta
        cur.execute("""
            SELECT ts, sg, direction
            FROM cgm_readings
            ORDER BY ts DESC
            LIMIT 2
        """)
        rows = cur.fetchall()
        
        latest_sg = None
        delta = None
        direction = None
        ts_utc = None
        mins_ago = None
        is_stale = False
        
        if rows:
            latest_sg = float(rows[0]['sg'])
            direction = rows[0]['direction']
            ts_utc = rows[0]['ts'].isoformat()
            
            # Calculate staleness
            now = datetime.now(timezone.utc)
            reading_ts = rows[0]['ts']
            if reading_ts.tzinfo is None:
                reading_ts = reading_ts.replace(tzinfo=timezone.utc)
            
            mins_ago = int((now - reading_ts).total_seconds() / 60)
            stale_thresh = getattr(config, 'STALE_THRESHOLD_SECONDS', 600)
            is_stale = (now - reading_ts).total_seconds() > stale_thresh
            
            if len(rows) > 1:
                delta = float(rows[0]['sg']) - float(rows[1]['sg'])

        # 2. Get latest IOB/COB from devicestatus
        cur.execute("""
            SELECT iob, cob
            FROM devicestatus
            WHERE iob IS NOT NULL OR cob IS NOT NULL
            ORDER BY ts DESC
            LIMIT 1
        """)
        dev = cur.fetchone()
        
        iob = float(dev['iob']) if dev and dev['iob'] is not None else 0.0
        cob = float(dev['cob']) if dev and dev['cob'] is not None else 0.0
        
        # 3. Get extra metrics for /api/latest
        extra = {
            "tir_today": 0, "tir_7day": 0, "tir_30day": 0, "tir_90day": 0,
            "titr_today": 0, "titr_7day": 0, "titr_30day": 0, "titr_90day": 0,
            "tdd_today": 0, "tdd_7day": 0, "tdd_30day": 0, "tdd_90day": 0
        }
        try:
            from zoneinfo import ZoneInfo
            
            # Fetch timezone from DB
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1")
                res = cur.fetchone()
                db_tz = res[0] if res else 'UTC'
            
            _tz = ZoneInfo(db_tz)
            today_str = datetime.now(_tz).strftime("%Y-%m-%d")
            
            # Using RealDictCursor for consistent dictionary access
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as rcur:
                rcur.execute("SELECT * FROM public.get_metrics_comparison(%s::date)", (today_str,))
                comp_rows = rcur.fetchall()
            
            row_dict = {r['Metric']: r for r in comp_rows}
            
            def parse_pct(val):
                if not val: return 0
                return int(round(float(str(val).replace('%', '').strip())))
                
            def parse_num(val):
                if not val: return 0
                return int(round(float(str(val).strip())))
            
            tir_r = row_dict.get('TIR (3.9-10) (%)', {})
            titr_r = row_dict.get('TITR (3.9-7.8) (%)', {})
            tdd_r = row_dict.get('TDD', {})
            carb_r = row_dict.get('Carbs', {})
            cv_r = row_dict.get('CV (%) (not MAVG)', {})
            
            extra["tir_today"] = parse_pct(tir_r.get('Selected Day', 0))
            extra["tir_7day"] = parse_pct(tir_r.get('7 Day Avg', 0))
            extra["tir_14day"] = parse_pct(tir_r.get('14 Day Avg', 0))
            extra["tir_30day"] = parse_pct(tir_r.get('30 Day Avg', 0))
            extra["tir_90day"] = parse_pct(tir_r.get('90 Day Avg', 0))
            
            extra["titr_today"] = parse_pct(titr_r.get('Selected Day', 0))
            extra["titr_7day"] = parse_pct(titr_r.get('7 Day Avg', 0))
            extra["titr_14day"] = parse_pct(titr_r.get('14 Day Avg', 0))
            extra["titr_30day"] = parse_pct(titr_r.get('30 Day Avg', 0))
            extra["titr_90day"] = parse_pct(titr_r.get('90 Day Avg', 0))
            
            extra["tdd_today"] = parse_num(tdd_r.get('Selected Day', 0))
            extra["tdd_7day"] = parse_num(tdd_r.get('7 Day Avg', 0))
            extra["tdd_14day"] = parse_num(tdd_r.get('14 Day Avg', 0))
            extra["tdd_30day"] = parse_num(tdd_r.get('30 Day Avg', 0))
            extra["tdd_90day"] = parse_num(tdd_r.get('90 Day Avg', 0))
            
            extra["carb_today"] = parse_num(carb_r.get('Selected Day', 0))
            extra["carb_7day"] = parse_num(carb_r.get('7 Day Avg', 0))
            extra["carb_14day"] = parse_num(carb_r.get('14 Day Avg', 0))
            extra["carb_30day"] = parse_num(carb_r.get('30 Day Avg', 0))
            extra["carb_90day"] = parse_num(carb_r.get('90 Day Avg', 0))
            
            extra["cv_today"] = parse_pct(cv_r.get('Selected Day', 0))
            extra["cv_7day"] = parse_pct(cv_r.get('7 Day Avg', 0))
            extra["cv_14day"] = parse_pct(cv_r.get('14 Day Avg', 0))
            extra["cv_30day"] = parse_pct(cv_r.get('30 Day Avg', 0))
            extra["cv_90day"] = parse_pct(cv_r.get('90 Day Avg', 0))
        except Exception as e:
            print(f"[db] Error loading extra metrics for latest api: {e}")
            conn.rollback()
        
        return {
            "sg": latest_sg,
            "delta": delta,
            "direction": direction,
            "iob": iob,
            "cob": cob,
            "ts_utc": ts_utc,
            "mins_ago": mins_ago,
            "is_stale": is_stale,
            **extra
        }

def get_earliest_cgm_date(conn) -> str:
    """
    Returns the earliest recorded CGM result date (YYYY-MM-DD) in the system's local timezone.
    If no CGM data exists, returns today's local date.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT to_char(
                    MIN(ts) AT TIME ZONE COALESCE((SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'UTC'),
                    'YYYY-MM-DD'
                )
                FROM cgm_readings
                WHERE sg IS NOT NULL
            """)
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
            # Fallback to today in local timezone
            cur.execute("""
                SELECT to_char(
                    CURRENT_TIMESTAMP AT TIME ZONE COALESCE((SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'UTC'),
                    'YYYY-MM-DD'
                )
            """)
            r = cur.fetchone()
            if r and r[0]:
                return r[0]
    except Exception as e:
        print(f"[db] Error getting earliest cgm date: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    return datetime.utcnow().strftime('%Y-%m-%d')

def get_dashboard_metrics(conn, anchor_date_str):
    """
    Fetches the 90/30/7/1 day metrics for the comprehensive dashboard.
    Calls the highly optimized get_metrics_comparison() SQL function.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.get_metrics_comparison(%s::date)", (anchor_date_str,))
            rows = cur.fetchall()
            
        metrics_list = []
        # Desired order and filtering
        desired_metrics = [
            "TIR (3.9-10) (%)", 
            "TITR (3.9-7.8) (%)", 
            "Avg BG", 
            "GMI (%)", 
            "CV (%) (not MAVG)", 
            "GVI",
            "HBGI (High Risk)",
            "LBGI (Low Risk)",
            "TDD",
            "Carbs",
            "V.Low (<3) (%)", 
            "Low (3-3.8) (%)", 
            "High (10.1-13.9) (%)",
            "V.High (>13.9) (%)"
        ]
        
        # Create lookup dict and map SQL column names to Template names
        # Function returns: Metric, 90 Day Avg, 30 Day Avg, 7 Day Avg, Selected Day, Description
        row_dict = {}
        for row in rows:
            mapped_row = {
                'Metric': row['Metric'],
                'Selected Day': row['Selected Day'],
                '7 Day Avg': row['7 Day Avg'],
                '14 Day Avg': row['14 Day Avg'],
                '30 Day Avg': row['30 Day Avg'],
                '60 Day Avg': row['60 Day Avg'],
                '90 Day Avg': row['90 Day Avg'],
                'Description': row['Description']
            }
            row_dict[row['Metric']] = mapped_row
        
        # print(f"[debug dashboard] Available metrics keys: {list(row_dict.keys())}")
        
        for m in desired_metrics:
            if m in row_dict:
                metrics_list.append(row_dict[m])
                
        # print(f"[debug dashboard] Final metrics output count: {len(metrics_list)}")
        return metrics_list
    except Exception as e:
        print(f"[db] get_dashboard_metrics failed: {e}")
        conn.rollback()
        return []

def cv_sql_expr(sum_col, sum2_col, readings_col):
    """Return SQL expression computing Coefficient of Variation (CV) percentage.
    Formula: 100 * (SD / Mean), where SD = SQRT(GREATEST(0, (sum2/n) - (sum/n)^2))
    and Mean = sum/n.
    """
    return (
        f"(100.0 * SQRT(GREATEST(0, ({sum2_col} / NULLIF({readings_col}, 0)) - "
        f"POWER({sum_col} / NULLIF({readings_col}, 0), 2))) / "
        f"NULLIF({sum_col} / NULLIF({readings_col}, 0), 0))"
    )

def get_trends_data(conn, start_date_str, end_date_str, grain='daily'):
    """
    Fetches trends for metabolic metrics over a date range.
    Supports 'daily' (raw days + moving averages) or 'monthly' (calendar month grouping).

    REWRITTEN to read from the consolidated schema (layer2_daily_band_stats /
    layer2_daily_risk_stats). All windows including 60d are now uniformly
    precalculated in layer2_daily_risk_stats — this eliminates the old
    bespoke 60-day live-window-function block entirely (previously the only
    window not covered by experimental_advanced_metrics_daily's rolled
    columns). Also fixes a pre-existing bug found while porting: the old
    query's cv_90d used e.sd_90d_mean (a mean-of-daily-SDs, a different
    statistic entirely) instead of a proper pooled SD — flagged, not
    silently carried forward; the new schema computes cv_90d correctly and
    uniformly for every window, so this class of bug can't recur.
    """
    if grain == 'monthly':
        query = """
            SELECT
                TO_CHAR(DATE_TRUNC('month', d."date"), 'YYYY-MM') as "Date",
                ROUND((SUM(d.pct_tir * d.bg_readings) / NULLIF(SUM(d.bg_readings), 0))::numeric, 1) as "TIR",
                ROUND((SUM(d.pct_titr * d.bg_readings) / NULLIF(SUM(d.bg_readings), 0))::numeric, 1) as "TITR",
                ROUND((SUM(d.bg_sum) / NULLIF(SUM(d.bg_readings), 0) / 18.0182)::numeric, 1) as mean_val,
                ROUND(SQRT(GREATEST(0, (SUM(d.bg_sum2) / NULLIF(SUM(d.bg_readings), 0)) - POWER(SUM(d.bg_sum) / NULLIF(SUM(d.bg_readings), 0), 2))) / 18.0182::numeric, 1) as sd_val,
                ROUND((SUM(COALESCE(r.curve_length,0)) / NULLIF(SUM(COALESCE(r.time_length,0)),0))::numeric, 2) as gvi,
                ROUND(AVG(r.hbgi)::numeric, 2) as hbgi,
                ROUND(AVG(r.lbgi)::numeric, 2) as lbgi,
                ROUND((3.31 + 0.02392 * (SUM(d.bg_sum) / NULLIF(SUM(d.bg_readings), 0)))::numeric, 1) as gmi,
                ROUND((""" + cv_sql_expr('SUM(d.bg_sum)', 'SUM(d.bg_sum2)', 'SUM(d.bg_readings)') + """::numeric), 1) as cv,
                ROUND(AVG(d.tdd)::numeric, 1) as tdd,
                ROUND((SUM(d.bg_sum) / NULLIF(SUM(d.bg_readings), 0) / 18.0182)::numeric, 1) as avg_bg,
                ROUND(SUM(d.carbs)::numeric, 1) as carbs,
                -- Return NULL for moving averages in monthly grouped view to keep keys consistent
                NULL as tir_7d, NULL as titr_7d, NULL as gvi_7d, NULL as hbgi_7d, NULL as lbgi_7d, NULL as gmi_7d, NULL as cv_7d, NULL as tdd_7d, NULL as avg_bg_7d, NULL as carbs_7d,
                NULL as tir_14d, NULL as titr_14d, NULL as gvi_14d, NULL as hbgi_14d, NULL as lbgi_14d, NULL as gmi_14d, NULL as cv_14d, NULL as tdd_14d, NULL as avg_bg_14d, NULL as carbs_14d,
                NULL as tir_30d, NULL as titr_30d, NULL as gvi_30d, NULL as hbgi_30d, NULL as lbgi_30d, NULL as gmi_30d, NULL as cv_30d, NULL as tdd_30d, NULL as avg_bg_30d, NULL as carbs_30d,
                NULL as tir_60d, NULL as titr_60d, NULL as gvi_60d, NULL as hbgi_60d, NULL as lbgi_60d, NULL as gmi_60d, NULL as cv_60d, NULL as tdd_60d, NULL as avg_bg_60d, NULL as carbs_60d,
                NULL as tir_90d, NULL as titr_90d, NULL as gvi_90d, NULL as hbgi_90d, NULL as lbgi_90d, NULL as gmi_90d, NULL as cv_90d, NULL as tdd_90d, NULL as avg_bg_90d, NULL as carbs_90d
            FROM layer2_daily_band_stats d
            LEFT JOIN layer2_daily_risk_stats r ON d."date" = r."date"
            WHERE d."date" BETWEEN %s AND %s
            GROUP BY 1
            ORDER BY 1 ASC
        """
    else:
        query = """
            SELECT
                TO_CHAR(d."date", 'YYYY-MM-DD') as "Date",
                d.pct_tir as "TIR",
                d.pct_titr as "TITR",
                ROUND((d.bg_sum / NULLIF(d.bg_readings,0) / 18.0182)::numeric, 1) as mean_val,
                ROUND((SQRT(GREATEST(0, d.bg_sum2/NULLIF(d.bg_readings,0) - POWER(d.bg_sum/NULLIF(d.bg_readings,0),2))) / 18.0182)::numeric, 1) as sd_val,
                r.gvi,
                r.hbgi,
                r.lbgi,
                ROUND(d.day_gmi_percent::numeric, 1) as gmi,
                ROUND((""" + cv_sql_expr('d.bg_sum', 'd.bg_sum2', 'd.bg_readings') + """::numeric), 1) as cv,
                d.carbs as carbs,
                -- 7/14/30/60/90 Day: every window now reads directly from the
                -- precalculated columns in layer2_daily_risk_stats — no live
                -- recomputation, no special-cased 60-day block.
                r.tir_7d, r.titr_7d, r.gvi_7d, r.hbgi_7d, r.lbgi_7d, r.gmi_7d, r.cv_7d, r.tdd_7d, r.mean_mmol_7d as avg_bg_7d, r.carbs_7d,
                r.tir_14d, r.titr_14d, r.gvi_14d, r.hbgi_14d, r.lbgi_14d, r.gmi_14d, r.cv_14d, r.tdd_14d, r.mean_mmol_14d as avg_bg_14d, r.carbs_14d,
                r.tir_30d, r.titr_30d, r.gvi_30d, r.hbgi_30d, r.lbgi_30d, r.gmi_30d, r.cv_30d, r.tdd_30d, r.mean_mmol_30d as avg_bg_30d, r.carbs_30d,
                r.tir_60d, r.titr_60d, r.gvi_60d, r.hbgi_60d, r.lbgi_60d, r.gmi_60d, r.cv_60d, r.tdd_60d, r.mean_mmol_60d as avg_bg_60d, r.carbs_60d,
                r.tir_90d, r.titr_90d, r.gvi_90d, r.hbgi_90d, r.lbgi_90d, r.gmi_90d, r.cv_90d, r.tdd_90d, r.mean_mmol_90d as avg_bg_90d, r.carbs_90d,
                -- Totals for Day 1
                d.tdd as tdd,
                ROUND((d.bg_sum / NULLIF(d.bg_readings,0) / 18.0182)::numeric, 1) as avg_bg
            FROM layer2_daily_band_stats d
            LEFT JOIN layer2_daily_risk_stats r ON d."date" = r."date"
            WHERE d."date" BETWEEN %s AND %s
            ORDER BY d."date" ASC
        """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (start_date_str, end_date_str))
            rows = cur.fetchall()

            # Query weight anchors from clinical_notes
            cur.execute("""
                SELECT TO_CHAR("date", 'YYYY-MM-DD') as date_str, AVG(weight_kg)::numeric(6,3) as weight_kg
                FROM clinical_notes
                WHERE weight_kg IS NOT NULL AND weight_kg > 0
                GROUP BY "date"
                ORDER BY "date" ASC
            """)
            anchors_raw = cur.fetchall()
            anchors = [
                (r['date_str'], float(r['weight_kg']))
                for r in anchors_raw
                if r.get('date_str') and r.get('weight_kg') is not None
            ]

            if grain == 'monthly':
                cur.execute("""
                    SELECT TO_CHAR("date", 'YYYY-MM-DD') as date_str, tdd
                    FROM layer2_daily_band_stats
                    WHERE "date" BETWEEN %s AND %s AND tdd IS NOT NULL
                """, (start_date_str, end_date_str))
                daily_samples = [
                    (r['date_str'], float(r['tdd']))
                    for r in cur.fetchall()
                    if r.get('date_str') and r.get('tdd') is not None
                ]
                weight_math.attach_tdd_weight_monthly(rows, daily_samples, anchors)
            else:
                weight_math.attach_tdd_weight_daily(rows, anchors)

            return rows
    except Exception as e:
        print(f"[db] get_trends_data failed: {e}")
        conn.rollback()
        return []


def run_maintenance(conn):
    """
    Core maintenance task:
    1. Optional: Prunes Tier 1 & Tier 2 data based on retention policy.
    2. Runs VACUUM ANALYZE to reclaim space and update stats.
    """
    with conn.cursor() as cur:
        # --- OPTIONAL RETENTION POLICY (Raw & Clinical) ---
        cur.execute("SELECT key, value FROM system_config WHERE key IN ('retention_days_raw', 'retention_days_clinical', 'retention_enabled')")
        conf = {row[0]: row[1] for row in cur.fetchall()}
        
        enabled = conf.get('retention_enabled', 'false').lower() == 'true'
        if not enabled:
            print("[maintenance] Skipping optional Tier 1/2 pruning: Disabled in settings.")
        else:
            try:
                t1_days = int(conf.get('retention_days_raw', '120'))
                t2_days = int(conf.get('retention_days_clinical', '365'))
            except (ValueError, TypeError):
                print("[maintenance] Error: Invalid retention period in config.")
                t1_days = t2_days = 9999

            print(f"[maintenance] Starting optional pruning... Tier1: {t1_days} days, Tier2: {t2_days} days")

            # Prune Tier 1 (Raw Logs)
            if t1_days < 9999:
                cur.execute("DELETE FROM devicestatus WHERE ts < NOW() - %s * INTERVAL '1 day'", (t1_days,))
                count = cur.rowcount
                print(f"[maintenance] Pruned {count} rows from devicestatus (Tier 1).")

            # Prune Tier 2 (Clinical History)
            if t2_days < 9999:
                cur.execute("DELETE FROM cgm_readings WHERE ts < NOW() - %s * INTERVAL '1 day'", (t2_days,))
                c1 = cur.rowcount
                cur.execute("DELETE FROM treatments WHERE ts < NOW() - %s * INTERVAL '1 day'", (t2_days,))
                c2 = cur.rowcount
                print(f"[maintenance] Pruned {c1 + c2} rows from clinical tables (Tier 2).")

        conn.commit()

        # Housekeeping: Reclaim space and update stats
        print("[maintenance] Running VACUUM ANALYZE...")
        old_isolation = conn.isolation_level
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        try:
            if enabled:
                cur.execute("VACUUM ANALYZE devicestatus;")
                cur.execute("VACUUM ANALYZE cgm_readings;")
                cur.execute("VACUUM ANALYZE treatments;")
        except Exception as ve:
            print(f"[maintenance] Vacuum failed: {ve}")
        finally:
            conn.set_isolation_level(old_isolation)
        
        print("[maintenance] Maintenance complete.")


# ---------------------------------------------------------------------------
# Sandbox: best/worst period search  (EXPERIMENTAL -- added 23 Aug 2026,
# stat set expanded 23 Aug 2026 session 2: GMI, TDD, Carbs, TAR, TBR added)
# ---------------------------------------------------------------------------
# Self-contained. Reads only from layer2_daily_risk_stats; adds no schema, no
# columns, no views, and is not called by anything outside the Sandbox page.
# Safe to delete outright along with its route + template block if the feature
# doesn't earn its place.
#
# Semantics (agreed with the project before building):
#   - A "candidate" is a window of `period_days` ending on a given date and
#     looking BACKWARDS. The [start_date, end_date] range bounds which END
#     DATES are tested, NOT which raw data may be read -- so a candidate ending
#     on start_date legitimately reaches back before it.
#   - For periods 7/14/30/60/90 every stat already exists as a precalculated
#     trailing-window column (tir_Xd, gmi_Xd, tdd_Xd, etc). Those are used
#     verbatim, so a result here matches the number the rest of the site shows
#     for that day -- confirmed 23 Aug against tdd_7d/carbs_7d specifically,
#     which are trailing PER-DAY AVERAGES, not period totals (values sit in
#     the same range as a single day's tdd/carbs, not 7x it).
#   - Periods 1/2/3 have no precalc equivalent, so they're rolled on the fly
#     using the SAME conventions the precalc uses:
#       * TIR/TITR/TAR/TBR -- reading-count-weighted (SUM(stat*bg_readings)
#         / SUM(bg_readings)), matching how the trailing-window stats pool
#         across days rather than naively averaging one number per day.
#       * Avg BG / GMI -- derived from pooled bg_sum/bg_readings, same
#         formula as get_trends_data() uses elsewhere (GMI = 3.31 + 0.02392 *
#         mean_mgdl).
#       * TDD / Carbs -- plain per-day average (AVG(tdd)/AVG(carbs) over the
#         window), matching tdd_Xd/carbs_Xd's own averaging, confirmed above.
#       * Sensor coverage -- plain per-day average, divided by the period
#         length (not by rows present) so a genuinely missing day counts as
#         0%, not a shrunk denominator.
#   - CV is ALWAYS the mean of the daily trailing-14-day CV (cv_14d) across
#     the window, for every period length -- the project's standing convention, so
#     CVs from different period lengths stay comparable. Daily CV is
#     deliberately never used.
#   - "Best" direction: TIR/TITR high; Avg BG/GMI/CV/TAR/TBR low (standard
#     clinical sense -- less time out of range, less variability, lower
#     average, is better). TDD/Carbs are a DELIBERATE EXCEPTION -- there's no
#     clinical "better" direction for insulin dose or carb intake in
#     isolation, so per the project's explicit instruction, "best" = higher for
#     these two. This isn't a clinical claim, just the ranking convention
#     for this tool -- flagged in the UI notes since it's the one direction
#     that isn't self-evident from the metric's own meaning.
BEST_WORST_STATS = {
    "tir":    {"label": "TIR",    "better": "high", "unit": "%",      "decimals": 1},
    "titr":   {"label": "TITR",   "better": "high", "unit": "%",      "decimals": 1},
    "avg_bg": {"label": "Avg BG", "better": "low",  "unit": "mmol/L", "decimals": 2},
    "gmi":    {"label": "GMI",    "better": "low",  "unit": "%",      "decimals": 2},
    "cv":     {"label": "CV",     "better": "low",  "unit": "%",      "decimals": 1},
    "tdd":    {"label": "TDD",    "better": "high", "unit": "U",      "decimals": 1},
    "carbs":  {"label": "Carbs",  "better": "high", "unit": "g",      "decimals": 1},
    "tar":    {"label": "TAR",    "better": "low",  "unit": "%",      "decimals": 1},
    "tbr":    {"label": "TBR",    "better": "low",  "unit": "%",      "decimals": 1},
}
# Display order for the table's stat columns -- independent of dict iteration
# order so it can't silently change if the dict above is ever reordered.
# NOTE: "cv" is deliberately excluded from this list -- unlike every other
# stat, it never reads a precalc column keyed by period; it is ALWAYS the
# mean of cv_14d (see module docstring), computed once via cv_raw in the SQL
# below and merged into each row's dict separately. Including it here would
# make the precalc-column lookup try (and fail) to find a "cv" prefix.
BEST_WORST_STAT_ORDER = ["tir", "titr", "avg_bg", "gmi", "tdd", "carbs", "tar", "tbr"]
BEST_WORST_PERIODS = [1, 2, 3, 7, 14, 30, 60, 90]
_BW_PRECALC_PERIODS = {7, 14, 30, 60, 90}
# statistic -> precalc column prefix (i.e. "{prefix}_{p}d"). cv is handled
# separately (always cv_14d, never a per-period precalc column).
_BW_PRECALC_PREFIX = {
    "tir": "tir", "titr": "titr", "avg_bg": "mean_mmol", "gmi": "gmi",
    "tdd": "tdd", "carbs": "carbs", "tar": "tar", "tbr": "tbr",
}
# Reading-count-weighted when rolling 1/2/3 days live (pooled across readings,
# not naively averaged one-value-per-day) -- these have a raw per-day column
# on layer2_daily_risk_stats (r.tir, r.titr, r.tar, r.tbr) to weight.
_BW_WEIGHTED_STATS = {"tir", "titr", "tar", "tbr"}
# Plain per-day average when rolling live -- not a per-reading quantity.
_BW_PLAIN_AVG_STATS = {"tdd", "carbs"}


def _bw_rolled_exprs(stat, p):
    """SQL expression for `stat`, rolled live over the current window `w`,
    for the 1/2/3-day case (no precalc column exists). See module docstring
    above for why each stat uses the method it does."""
    if stat in _BW_WEIGHTED_STATS:
        return (f"(SUM(r.{stat} * r.bg_readings) OVER w) "
                f"/ NULLIF(SUM(r.bg_readings) OVER w, 0)")
    if stat == "avg_bg":
        # bg_sum is mg/dL (see Standing Gotchas) -- convert for display.
        return ("(SUM(r.bg_sum) OVER w) "
                "/ NULLIF(SUM(r.bg_readings) OVER w, 0) / 18.0182")
    if stat == "gmi":
        # Same formula as get_trends_data() -- 3.31 + 0.02392 * mean_mgdl.
        return ("3.31 + 0.02392 * ((SUM(r.bg_sum) OVER w) "
                "/ NULLIF(SUM(r.bg_readings) OVER w, 0))")
    if stat in _BW_PLAIN_AVG_STATS:
        return f"(AVG(r.{stat}) OVER w)"
    raise AssertionError("unhandled stat in _bw_rolled_exprs: %s" % stat)


def find_best_worst_period(conn, statistic, period_days, start_date, end_date,
                           direction="highest", min_coverage=80.0, limit=10,
                           max_overlap_pct=None):
    """Rank every backward-looking `period_days` window whose END DATE falls in
    [start_date, end_date], by `statistic`, and return the top `limit`.

    Returns {"results": [...], "meta": {...}} or raises ValueError on bad input.
    Every returned row carries ALL nine stats, not just the ranked one -- a
    good window for one metric is often a poor one for another, and that
    contrast is the point of the table.
    """
    if statistic not in BEST_WORST_STATS:
        raise ValueError("Unknown statistic: %s" % statistic)
    if period_days not in BEST_WORST_PERIODS:
        raise ValueError("Unsupported period: %s" % period_days)
    if direction not in ("best", "worst", "highest", "lowest"):
        raise ValueError("direction must be highest, lowest, best, or worst")

    is_highest = direction in ("best", "highest")
    dir_label = "highest" if is_highest else "lowest"

    p = int(period_days)
    precalc = p in _BW_PRECALC_PERIODS

    start_dt = datetime.strptime(str(start_date), "%Y-%m-%d").date() if isinstance(start_date, str) else start_date
    end_dt = datetime.strptime(str(end_date), "%Y-%m-%d").date() if isinstance(end_date, str) else end_date
    min_end_date = start_dt + timedelta(days=p - 1)

    select_lines = []
    for stat in BEST_WORST_STAT_ORDER:
        if precalc:
            # p and stat both come from fixed whitelists above, so this can
            # only ever produce a known, real column name.
            expr = f"r.{_BW_PRECALC_PREFIX[stat]}_{p}d"
        else:
            expr = _bw_rolled_exprs(stat, p)
        decimals = BEST_WORST_STATS[stat]["decimals"]
        select_lines.append(f"ROUND(({expr})::numeric, {decimals}) AS {stat}")

    cov_expr = f"r.sensor_active_pct_{p}d" if precalc else f"(SUM(r.sensor_active_pct) OVER w) / {p}.0"

    # RANGE (not ROWS) so a missing calendar day can never silently stretch a
    # window across a longer real span than it claims.
    query = f"""
        WITH roll AS (
            SELECT
                r.date                                     AS d,
                COUNT(*) OVER w                            AS n_days,
                COUNT(r.cv_14d) OVER w                     AS cv_days,
                (AVG(r.cv_14d) OVER w)::numeric            AS cv_raw,
                ({cov_expr})::numeric                      AS coverage_raw,
                {", ".join(select_lines)}
            FROM layer2_daily_risk_stats r
            WINDOW w AS (
                ORDER BY r.date
                RANGE BETWEEN INTERVAL '{p - 1} days' PRECEDING AND CURRENT ROW
            )
        )
        SELECT d, n_days, cv_days,
               (SELECT MIN(date) FROM layer2_daily_risk_stats) AS data_start,
               ROUND(cv_raw, 1)       AS cv,
               ROUND(coverage_raw, 1) AS coverage,
               {", ".join(BEST_WORST_STAT_ORDER)}
        FROM roll
        WHERE d BETWEEN %s AND %s
        ORDER BY d
    """

    with conn.cursor() as cur:
        cur.execute(query, (min_end_date, end_dt))
        cols = [c.name for c in cur.description]
        rows = cur.fetchall()

    n_tested = len(rows)
    n_incomplete = 0
    n_low_coverage = 0
    candidates = []

    for row in rows:
        r = dict(zip(cols, row))
        d, n_days, cv_days, data_start = r["d"], r["n_days"], r["cv_days"], r["data_start"]

        # A window is only a candidate if it has a full complement of real days
        # AND every one of those days has a valid trailing-14d CV -- otherwise
        # the row would show a CV computed off a shorter basis than it claims.
        if n_days != p or cv_days != p:
            n_incomplete += 1
            continue
        # Second, subtler CV guard: cv_14d is non-null but computed off a SHORT
        # basis for the first 13 days of data overall (there simply isn't 14
        # days of history behind it yet). A non-null check can't catch that, so
        # exclude any window reaching into that opening stretch.
        window_start = d - timedelta(days=p - 1)
        if data_start is not None and window_start < data_start + timedelta(days=13):
            n_incomplete += 1
            continue
        coverage = r["coverage"]
        if coverage is None or float(coverage) < float(min_coverage):
            n_low_coverage += 1
            continue
        if r[statistic] is None:
            n_incomplete += 1
            continue

        entry = {
            "period_end": d.isoformat(),
            "period_start": window_start.isoformat(),
            "coverage": float(coverage),
            "cv": None if r["cv"] is None else float(r["cv"]),
            "_sort": float(r[statistic]),
            "_start_date": window_start,
            "_end_date": d,
        }
        for stat in BEST_WORST_STAT_ORDER:
            entry[stat] = None if r[stat] is None else float(r[stat])
        candidates.append(entry)

    # For highest, sort descending. For lowest, sort ascending.
    descending = is_highest

    # Two-pass stable sort: ties always resolve to the most recent window.
    candidates.sort(key=lambda c: c["period_end"], reverse=True)
    candidates.sort(key=lambda c: c["_sort"], reverse=descending)

    results = []
    accepted_ranges = []
    for c in candidates:
        if max_overlap_pct is not None and p > 1:
            c_start = c["_start_date"]
            c_end = c["_end_date"]
            overlap = False
            for a_start, a_end in accepted_ranges:
                shared_days = max(0, (min(c_end, a_end) - max(c_start, a_start)).days + 1)
                if max_overlap_pct == 0:
                    if shared_days > 0:
                        overlap = True
                        break
                else:
                    shared_pct = (shared_days / p) * 100.0
                    if shared_pct >= max_overlap_pct:
                        overlap = True
                        break
            if overlap:
                continue
            accepted_ranges.append((c_start, c_end))

        c = dict(c)
        c.pop("_sort", None)
        c.pop("_start_date", None)
        c.pop("_end_date", None)
        c["rank"] = len(results) + 1
        results.append(c)
        if len(results) >= limit:
            break

    return {
        "mode": "fixed_period",
        "results": results,
        "meta": {
            "mode": "fixed_period",
            "statistic": statistic,
            "statistic_label": BEST_WORST_STATS[statistic]["label"],
            "direction": dir_label,
            "period_days": p,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "min_coverage": float(min_coverage),
            "max_overlap_pct": float(max_overlap_pct) if (max_overlap_pct is not None and p > 1) else None,
            "windows_tested": n_tested,
            "windows_eligible": len(candidates),
            "excluded_incomplete": n_incomplete,
            "excluded_low_coverage": n_low_coverage,
            "source": ("precalculated trailing-window columns"
                       if precalc
                       else "rolled live from daily rows"),
        },
    }


def _streak_op_match(val, op, thresh):
    if val is None:
        return False
    v = float(val)
    t = float(thresh)
    if op == ">=":
        return v >= t - 1e-6
    if op == ">":
        return v > t + 1e-6
    if op == "=":
        return abs(v - t) < 0.05
    if op == "<=":
        return v <= t + 1e-6
    if op == "<":
        return v < t - 1e-6
    return False


def find_longest_streak_period(conn, statistic, operator, threshold, start_date, end_date, limit=10):
    """Find the top `limit` longest unbroken periods meeting the given condition
    between start_date and end_date.

    If statistic == "bg":
        Evaluates contiguous 5-minute CGM readings from layer2_five_minute_aggregate.
        Duration is high-precision (days:hours:mins).
    Else:
        Evaluates contiguous calendar days from layer2_daily_risk_stats.
        Duration is in days.
    """
    valid_ops = {">=", ">", "=", "<=", "<"}
    if operator not in valid_ops:
        raise ValueError(f"Invalid operator: {operator}")

    thresh_val = float(threshold)
    start_dt = datetime.strptime(str(start_date), "%Y-%m-%d").date() if isinstance(start_date, str) else start_date
    end_dt = datetime.strptime(str(end_date), "%Y-%m-%d").date() if isinstance(end_date, str) else end_date

    if statistic == "bg":
        query = """
            SELECT ts, bg
            FROM layer2_five_minute_aggregate
            WHERE day >= %s AND day <= %s AND bg IS NOT NULL
            ORDER BY ts
        """
        with conn.cursor() as cur:
            cur.execute(query, (start_dt, end_dt))
            rows = cur.fetchall()

        streaks = []
        curr_start = None
        curr_end = None
        curr_readings = []

        def _record_bg(s_ts, e_ts, r_list):
            if not r_list:
                return
            n = len(r_list)
            dur_secs = (e_ts - s_ts).total_seconds() + 300.0
            mins = int(round(dur_secs / 60.0))
            d = mins // 1440
            h = (mins % 1440) // 60
            m = mins % 60
            parts = []
            if d > 0:
                parts.append(f"{d}d")
            if h > 0 or d > 0:
                parts.append(f"{h}h")
            parts.append(f"{m}m")
            dur_fmt = " ".join(parts)

            avg_val = round(sum(r_list) / n, 2)
            min_val = round(min(r_list), 1)
            max_val = round(max(r_list), 1)
            tir_val = round(sum(1 for b in r_list if 3.9 <= b <= 10.0) / n * 100.0, 1)
            titr_val = round(sum(1 for b in r_list if 3.9 <= b <= 7.8) / n * 100.0, 1)
            streaks.append({
                "period_start": s_ts.strftime("%Y-%m-%d %H:%M"),
                "period_end": e_ts.strftime("%Y-%m-%d %H:%M"),
                "start_time": s_ts.strftime("%Y-%m-%d %H:%M"),
                "end_time": e_ts.strftime("%Y-%m-%d %H:%M"),
                "duration_seconds": dur_secs,
                "duration_str": dur_fmt,
                "duration_formatted": dur_fmt,
                "duration_days": round(dur_secs / 86400.0, 2),
                "reading_count": n,
                "readings_count": n,
                "avg_bg": avg_val,
                "min_bg": min_val,
                "max_bg": max_val,
                "tir": tir_val,
                "titr": titr_val,
                "tir_pct": tir_val,
                "titr_pct": titr_val,
            })

        for ts, bg in rows:
            v = float(bg)
            if _streak_op_match(v, operator, thresh_val):
                if curr_start is None:
                    curr_start = ts
                    curr_end = ts
                    curr_readings = [v]
                else:
                    if (ts - curr_end) <= timedelta(minutes=15):
                        curr_end = ts
                        curr_readings.append(v)
                    else:
                        _record_bg(curr_start, curr_end, curr_readings)
                        curr_start = ts
                        curr_end = ts
                        curr_readings = [v]
            else:
                if curr_start is not None:
                    _record_bg(curr_start, curr_end, curr_readings)
                    curr_start = None
                    curr_end = None
                    curr_readings = []

        if curr_start is not None:
            _record_bg(curr_start, curr_end, curr_readings)

        streaks.sort(key=lambda s: s["period_end"], reverse=True)
        streaks.sort(key=lambda s: s["duration_seconds"], reverse=True)

        results = []
        for i, s in enumerate(streaks[:limit], start=1):
            s = dict(s)
            s["rank"] = i
            results.append(s)

        return {
            "mode": "longest",
            "type": "bg_continuous",
            "results": results,
            "meta": {
                "mode": "longest",
                "type": "bg_continuous",
                "statistic": "bg",
                "statistic_label": "Continuous BG",
                "operator": operator,
                "threshold": thresh_val,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "streaks_found": len(streaks),
            },
        }

    # Daily metric streak finder
    col_map = {
        "titr": "titr",
        "tir": "tir",
        "tar": "tar",
        "tbr": "tbr",
        "cv": "cv_14d",
        "avg_bg": "avg_bg",
        "gmi": "gmi",
        "tdd": "tdd",
        "carbs": "carbs",
    }
    if statistic not in col_map:
        raise ValueError(f"Unsupported statistic for longest search: {statistic}")

    col_name = col_map[statistic]
    query = """
        SELECT date,
               COALESCE(sensor_active_pct, (bg_readings::float / 288.0 * 100.0)) AS coverage,
               cv_14d,
               tir, titr, tar, tbr,
               (bg_sum / NULLIF(bg_readings, 0) / 18.0182)::numeric AS avg_bg,
               (3.31 + 0.02392 * (bg_sum / NULLIF(bg_readings, 0)))::numeric AS gmi,
               tdd, carbs
        FROM layer2_daily_risk_stats
        WHERE date >= %s AND date <= %s
        ORDER BY date
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (start_dt, end_dt))
        rows = cur.fetchall()

    streaks = []
    curr_streak = []

    def _record_daily(s_rows):
        if not s_rows:
            return
        n = len(s_rows)
        s_date = s_rows[0]["date"]
        e_date = s_rows[-1]["date"]

        avg_bg_vals = [float(r["avg_bg"]) for r in s_rows if r["avg_bg"] is not None]
        titr_vals = [float(r["titr"]) for r in s_rows if r["titr"] is not None]
        tir_vals = [float(r["tir"]) for r in s_rows if r["tir"] is not None]
        tar_vals = [float(r["tar"]) for r in s_rows if r["tar"] is not None]
        tbr_vals = [float(r["tbr"]) for r in s_rows if r["tbr"] is not None]
        cv_vals = [float(r["cv_14d"]) for r in s_rows if r["cv_14d"] is not None]
        gmi_vals = [float(r["gmi"]) for r in s_rows if r["gmi"] is not None]
        tdd_vals = [float(r["tdd"]) for r in s_rows if r["tdd"] is not None]
        carbs_vals = [float(r["carbs"]) for r in s_rows if r["carbs"] is not None]
        cov_vals = [float(r["coverage"]) for r in s_rows if r["coverage"] is not None]

        dur_fmt = f"{n} day" if n == 1 else f"{n} days"
        streaks.append({
            "period_start": s_date.isoformat(),
            "period_end": e_date.isoformat(),
            "duration_days": n,
            "duration_str": dur_fmt,
            "duration_formatted": dur_fmt,
            "avg_bg": round(sum(avg_bg_vals) / len(avg_bg_vals), 2) if avg_bg_vals else None,
            "titr": round(sum(titr_vals) / len(titr_vals), 1) if titr_vals else None,
            "tir": round(sum(tir_vals) / len(tir_vals), 1) if tir_vals else None,
            "tar": round(sum(tar_vals) / len(tar_vals), 1) if tar_vals else None,
            "tbr": round(sum(tbr_vals) / len(tbr_vals), 1) if tbr_vals else None,
            "cv": round(sum(cv_vals) / len(cv_vals), 1) if cv_vals else None,
            "gmi": round(sum(gmi_vals) / len(gmi_vals), 2) if gmi_vals else None,
            "tdd": round(sum(tdd_vals) / len(tdd_vals), 1) if tdd_vals else None,
            "carbs": round(sum(carbs_vals) / len(carbs_vals), 1) if carbs_vals else None,
            "coverage": round(sum(cov_vals) / len(cov_vals), 1) if cov_vals else None,
        })

    prev_date = None
    for r in rows:
        d = r["date"]
        val = r[col_name]
        match = _streak_op_match(val, operator, thresh_val)
        if match:
            if not curr_streak:
                curr_streak = [r]
                prev_date = d
            else:
                if d == prev_date + timedelta(days=1):
                    curr_streak.append(r)
                    prev_date = d
                else:
                    _record_daily(curr_streak)
                    curr_streak = [r]
                    prev_date = d
        else:
            if curr_streak:
                _record_daily(curr_streak)
                curr_streak = []
                prev_date = None

    if curr_streak:
        _record_daily(curr_streak)

    streaks.sort(key=lambda s: s["period_end"], reverse=True)
    streaks.sort(key=lambda s: s["duration_days"], reverse=True)

    results = []
    for i, s in enumerate(streaks[:limit], start=1):
        s = dict(s)
        s["rank"] = i
        results.append(s)

    stat_label = BEST_WORST_STATS[statistic]["label"] if statistic in BEST_WORST_STATS else statistic.upper()
    return {
        "mode": "longest",
        "type": "daily_metric",
        "results": results,
        "meta": {
            "mode": "longest",
            "type": "daily_metric",
            "statistic": statistic,
            "statistic_label": stat_label,
            "operator": operator,
            "threshold": thresh_val,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "streaks_found": len(streaks),
        },
    }


# ---------------------------------------------------------------------------
# Phase 8: Targets & Saved Views CRUD
# ---------------------------------------------------------------------------
def list_targets(conn) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, rule, target_kind, display_defaults, created_at, updated_at
            FROM targets
            ORDER BY id ASC
        """)
        rows = cur.fetchall()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return rows


def get_target(conn, target_id: int) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, rule, target_kind, display_defaults, created_at, updated_at
            FROM targets
            WHERE id = %s
        """, (target_id,))
        r = cur.fetchone()
        if r:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return r


def create_target(conn, name: str, rule: dict, target_kind: str = "evaluative", display_defaults: dict | None = None) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO targets (name, rule, target_kind, display_defaults, created_at, updated_at)
            VALUES (%s, %s, %s, %s, now(), now())
            RETURNING id, name, rule, target_kind, display_defaults, created_at, updated_at
        """, (name, json.dumps(rule), target_kind, json.dumps(display_defaults) if display_defaults is not None else None))
        r = cur.fetchone()
        conn.commit()
        if r:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return r


def update_target(conn, target_id: int, name: str | None = None, rule: dict | None = None,
                  target_kind: str | None = None, display_defaults: dict | None = None) -> dict | None:
    fields = []
    vals = []
    if name is not None:
        fields.append("name = %s")
        vals.append(name)
    if rule is not None:
        fields.append("rule = %s")
        vals.append(json.dumps(rule))
    if target_kind is not None:
        fields.append("target_kind = %s")
        vals.append(target_kind)
    if display_defaults is not None:
        fields.append("display_defaults = %s")
        vals.append(json.dumps(display_defaults))

    if not fields:
        return get_target(conn, target_id)

    fields.append("updated_at = now()")
    vals.append(target_id)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            UPDATE targets
            SET {', '.join(fields)}
            WHERE id = %s
            RETURNING id, name, rule, target_kind, display_defaults, created_at, updated_at
        """, tuple(vals))
        r = cur.fetchone()
        conn.commit()
        if r:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return r


def delete_target(conn, target_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM targets WHERE id = %s RETURNING id", (target_id,))
        deleted = cur.fetchone() is not None
        conn.commit()
        return deleted


def list_saved_views(conn, page: str) -> list:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, page, name, payload, is_builtin, source_default_id, is_deleted, created_at, updated_at
            FROM saved_views
            WHERE page = %s
            ORDER BY id ASC
        """, (page,))
        rows = cur.fetchall()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return rows


def get_saved_view(conn, view_id: int) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, page, name, payload, is_builtin, source_default_id, is_deleted, created_at, updated_at
            FROM saved_views
            WHERE id = %s
        """, (view_id,))
        r = cur.fetchone()
        if r:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return r


def create_saved_view(conn, page: str, name: str, payload: dict, is_builtin: bool = False,
                      source_default_id: str | None = None, is_deleted: bool = False) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO saved_views (page, name, payload, is_builtin, source_default_id, is_deleted, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now(), now())
            RETURNING id, page, name, payload, is_builtin, source_default_id, is_deleted, created_at, updated_at
        """, (page, name, json.dumps(payload), is_builtin, source_default_id, is_deleted))
        r = cur.fetchone()
        conn.commit()
        if r:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return r


def update_saved_view(conn, view_id: int, name: str | None = None, payload: dict | None = None,
                      is_deleted: bool | None = None) -> dict | None:
    fields = []
    vals = []
    if name is not None:
        fields.append("name = %s")
        vals.append(name)
    if payload is not None:
        fields.append("payload = %s")
        vals.append(json.dumps(payload))
    if is_deleted is not None:
        fields.append("is_deleted = %s")
        vals.append(is_deleted)

    if not fields:
        return get_saved_view(conn, view_id)

    fields.append("updated_at = now()")
    vals.append(view_id)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            UPDATE saved_views
            SET {', '.join(fields)}
            WHERE id = %s
            RETURNING id, page, name, payload, is_builtin, source_default_id, is_deleted, created_at, updated_at
        """, tuple(vals))
        r = cur.fetchone()
        conn.commit()
        if r:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return r


def delete_saved_view(conn, view_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM saved_views WHERE id = %s RETURNING id", (view_id,))
        deleted = cur.fetchone() is not None
        conn.commit()
        return deleted


# ---------------------------------------------------------------------------
# Filter CRUD & Deletion Guard Helpers (Phase 7b)
# ---------------------------------------------------------------------------
def get_filters(conn) -> List[Dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, notes, filter_tree, display_defaults, applies_to, created_at, updated_at
            FROM filters
            ORDER BY name ASC, id ASC
        """)
        rows = cur.fetchall()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at"):
                r["updated_at"] = r["updated_at"].isoformat()
        return [dict(r) for r in rows]


def get_filter(conn, filter_id: int) -> Optional[Dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, name, notes, filter_tree, display_defaults, applies_to, created_at, updated_at
            FROM filters
            WHERE id = %s
        """, (filter_id,))
        r = cur.fetchone()
        if not r:
            return None
        r = dict(r)
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("updated_at"):
            r["updated_at"] = r["updated_at"].isoformat()
        return r


def create_filter(conn, name: str, filter_tree: Dict, display_defaults: Optional[Dict] = None, notes: Optional[str] = None, applies_to: Optional[List[str]] = None) -> Dict:
    # applies_to is required at the API layer (_validate_applies_to) and
    # backstopped by filters_applies_to_chk at the DB layer -- the ["calendar"]
    # fallback here only covers a caller that bypasses the API route entirely
    # (e.g. a script against database.py directly), matching the DB column's
    # own DEFAULT for the same reason.
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO filters (name, notes, filter_tree, display_defaults, applies_to, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, now(), now())
            RETURNING id, name, notes, filter_tree, display_defaults, applies_to, created_at, updated_at
        """, (name, notes, json.dumps(filter_tree), json.dumps(display_defaults) if display_defaults is not None else None, json.dumps(applies_to if applies_to else ["calendar"])))
        r = dict(cur.fetchone())
        conn.commit()
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("updated_at"):
            r["updated_at"] = r["updated_at"].isoformat()
        return r


def update_filter(conn, filter_id: int, name: Optional[str] = None, filter_tree: Optional[Dict] = None, display_defaults: Optional[Dict] = None, notes: Optional[str] = None, applies_to: Optional[List[str]] = None) -> Optional[Dict]:
    fields = []
    vals = []
    if name is not None:
        fields.append("name = %s")
        vals.append(name)
    if filter_tree is not None:
        fields.append("filter_tree = %s")
        vals.append(json.dumps(filter_tree))
    if display_defaults is not None:
        fields.append("display_defaults = %s")
        vals.append(json.dumps(display_defaults))
    if notes is not None:
        fields.append("notes = %s")
        vals.append(notes)
    if applies_to is not None:
        fields.append("applies_to = %s")
        vals.append(json.dumps(applies_to))
    if not fields:
        return get_filter(conn, filter_id)

    fields.append("updated_at = now()")
    vals.append(filter_id)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            UPDATE filters
            SET {', '.join(fields)}
            WHERE id = %s
            RETURNING id, name, notes, filter_tree, display_defaults, applies_to, created_at, updated_at
        """, tuple(vals))
        r = cur.fetchone()
        conn.commit()
        if not r:
            return None
        r = dict(r)
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("updated_at"):
            r["updated_at"] = r["updated_at"].isoformat()
        return r


def delete_filter(conn, filter_id: int) -> bool:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("DELETE FROM filters WHERE id = %s RETURNING id", (filter_id,))
        deleted = cur.fetchone() is not None
        conn.commit()
        return deleted


def check_target_deletion_guard(conn, target_id: int) -> List[Dict[str, str]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT page, name FROM saved_views
            WHERE is_deleted = false
              AND payload -> 'targetRefs' @> to_jsonb(ARRAY[%s]::int[])
            ORDER BY page, name
        """, (target_id,))
        return [dict(r) for r in cur.fetchall()]


def check_filter_deletion_guard(conn, filter_id: int) -> List[Dict[str, str]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT page, name FROM saved_views
            WHERE is_deleted = false
              AND payload -> 'filterRefs' @> to_jsonb(ARRAY[%s]::int[])
            ORDER BY page, name
        """, (filter_id,))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Visual Chart Display Bands (Phase 7b)
# ---------------------------------------------------------------------------
def get_chart_bands(conn) -> Dict[str, Dict[str, float]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT setting_key, value_low, value_high
            FROM metric_thresholds
            WHERE setting_key IN ('chart_band_titr', 'chart_band_tir')
        """)
        rows = cur.fetchall()
        bands = {
            "titr": {"low": 3.9, "high": 7.8},
            "tir": {"low": 3.9, "high": 10.0}
        }
        for r in rows:
            k = r["setting_key"]
            if k == "chart_band_titr":
                bands["titr"]["low"] = float(r["value_low"]) if r["value_low"] is not None else 3.9
                bands["titr"]["high"] = float(r["value_high"]) if r["value_high"] is not None else 7.8
            elif k == "chart_band_tir":
                bands["tir"]["low"] = float(r["value_low"]) if r["value_low"] is not None else 3.9
                bands["tir"]["high"] = float(r["value_high"]) if r["value_high"] is not None else 10.0
        return bands


def update_chart_bands(conn, bands: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if "titr" in bands and isinstance(bands["titr"], dict):
            low = float(bands["titr"]["low"])
            high = float(bands["titr"]["high"])
            cur.execute("""
                UPDATE metric_thresholds
                SET value_low = %s, value_high = %s, updated_at = now()
                WHERE setting_key = 'chart_band_titr'
            """, (low, high))
        if "tir" in bands and isinstance(bands["tir"], dict):
            low = float(bands["tir"]["low"])
            high = float(bands["tir"]["high"])
            cur.execute("""
                UPDATE metric_thresholds
                SET value_low = %s, value_high = %s, updated_at = now()
                WHERE setting_key = 'chart_band_tir'
            """, (low, high))
        conn.commit()
    return get_chart_bands(conn)


def get_chart_metric_settings(conn, page: str = None) -> dict:
    """
    Returns custom chart metric settings.
    If page is specified, returns { metric_id: settings_dict }.
    If page is None, returns { page: { metric_id: settings_dict } }.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if page:
            cur.execute("""
                SELECT metric_id, settings
                FROM chart_metric_settings
                WHERE page = %s
            """, (page,))
            rows = cur.fetchall()
            return {r["metric_id"]: r["settings"] for r in rows}
        else:
            cur.execute("""
                SELECT page, metric_id, settings
                FROM chart_metric_settings
            """)
            rows = cur.fetchall()
            result = {}
            for r in rows:
                p = r["page"]
                if p not in result:
                    result[p] = {}
                result[p][r["metric_id"]] = r["settings"]
            return result


def upsert_chart_metric_settings(conn, page: str, metric_id: str, settings: dict) -> dict:
    """
    Upserts a metric's settings for a page and returns the updated settings.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO chart_metric_settings (page, metric_id, settings, created_at, updated_at)
            VALUES (%s, %s, %s, now(), now())
            ON CONFLICT (page, metric_id) DO UPDATE
            SET settings = EXCLUDED.settings, updated_at = now()
            RETURNING page, metric_id, settings, updated_at
        """, (page, metric_id, json.dumps(settings)))
        r = cur.fetchone()
        conn.commit()
        return r


def delete_chart_metric_settings(conn, page: str, metric_id: str) -> bool:
    """
    Removes a metric's global default override for a page, so it falls back
    to the registry's baked-in factory default (registryItem.style in the
    JS registries). Used by the style panel's "Restore default" action.
    Returns True if a row was actually deleted.
    """
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM chart_metric_settings
            WHERE page = %s AND metric_id = %s
        """, (page, metric_id))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def get_views_for_metric_cascade(conn, page: str, metric_id: str = None) -> list:
    """
    Returns active (non-deleted) saved views for a page with id, name, and current style overrides.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, page, name, payload
            FROM saved_views
            WHERE page = %s AND is_deleted = false
            ORDER BY id ASC
        """, (page,))
        rows = cur.fetchall()
        views = []
        for r in rows:
            payload = r.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            styles = payload.get("styles", {})
            has_override = metric_id in styles if metric_id else bool(styles)
            views.append({
                "id": r["id"],
                "name": r["name"],
                "page": r["page"],
                "has_override": has_override
            })
        return views


def cascade_metric_style_to_views(conn, page: str, metric_id: str, style_updates: dict, view_ids: list) -> int:
    """
    Updates the metric style override inside payload['styles'][metric_id] for selected view IDs.
    Wrapped in a single transaction; if any update fails, rolls back completely.
    Returns the count of updated views.
    """
    if not view_ids:
        return 0

    updated_count = 0
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        try:
            for vid in view_ids:
                cur.execute("""
                    SELECT id, payload
                    FROM saved_views
                    WHERE id = %s AND page = %s AND is_deleted = false
                    FOR UPDATE
                """, (vid, page))
                row = cur.fetchone()
                if not row:
                    continue

                payload = row.get("payload") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}

                # When a view is cascaded to adopt the global default, remove the
                # view-specific override so the view cleanly inherits the global default
                # and clears its 'has override' flag.
                if "styles" in payload and isinstance(payload["styles"], dict):
                    payload["styles"].pop(metric_id, None)
                    if not payload["styles"]:
                        payload.pop("styles", None)

                cur.execute("""
                    UPDATE saved_views
                    SET payload = %s, updated_at = now()
                    WHERE id = %s
                """, (json.dumps(payload), vid))
                updated_count += 1

            conn.commit()
            return updated_count
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Clinical Notes: free-text notes (user + synced AAPS), Weight, HbA1c.
# See docs/analysis/notes-weight-hba1c-implementation-plan.md (v3).
# ---------------------------------------------------------------------------

def _tz_sql() -> str:
    """SQL fragment to fetch the configured local timezone from
    system_config. Duplicated (not imported) from filter_engine._tz_sql --
    filter_engine imports database, so importing back would be circular.
    Same value either way."""
    return "(SELECT COALESCE(NULLIF(value, ''), 'UTC') FROM system_config WHERE key = 'TIMEZONE' LIMIT 1)"


def hba1c_percent_to_mmol_mol(percent: float) -> int:
    """Standard NGSP (%) -> IFCC (mmol/mol) conversion."""
    return round((float(percent) - 2.15) * 10.929)


def hba1c_mmol_mol_to_percent(mmol_mol: float) -> float:
    """Standard IFCC (mmol/mol) -> NGSP (%) conversion, inverse of the above."""
    return round((float(mmol_mol) / 10.929) + 2.15, 1)


def get_notes_for_date(conn, date_str: str) -> list:
    """All clinical_notes rows for one calendar day, oldest first -- the
    Day Note Popup's single query. time_created is converted to local
    wall-clock time here (not left as the stored UTC instant) -- otherwise
    a note created at a local wall-clock time must retain that time when
    converted for display."""
    tz_sql = _tz_sql()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT id, date, (time_created AT TIME ZONE {tz_sql}) AS time_created,
                   note_type, text_content,
                   weight_kg, hba1c_percent, hba1c_mmol_mol, is_edited
            FROM clinical_notes
            WHERE date = %s
            ORDER BY time_created ASC
        """, (date_str,))
        return [_serialize_note_row(dict(r)) for r in cur.fetchall()]


def _serialize_note_row(row: dict) -> dict:
    """psycopg2 returns real date/datetime objects; Flask's default JSON
    encoder renders those as RFC-822 strings ('Tue, 08 Jul 2025...'), not
    ISO -- harmless for display text but breaks round-tripping into an
    <input type="date"> (which requires exactly 'YYYY-MM-DD'). Normalize
    to ISO here, once, rather than relying on every caller to know this."""
    if row.get("date") is not None:
        row["date"] = row["date"].isoformat()
    if row.get("time_created") is not None:
        row["time_created"] = row["time_created"].isoformat()
    return row


def get_note_dates_range(conn, start_date: str, end_date: str) -> list:
    """Distinct dates with at least one note/record in range, for the
    Calendar's pill rendering. Cheap: indexed on date already."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT date FROM clinical_notes
            WHERE date BETWEEN %s AND %s
        """, (start_date, end_date))
        return [r[0].isoformat() for r in cur.fetchall()]


def save_note(conn, note_data: dict) -> dict:
    """Create or update a note/metric entry. Handles the aaps->user type
    transition (any edit to an AAPS-sourced note becomes a user note and
    is flagged is_edited so the poller never overwrites it again).

    note_data keys: id (omit/None for create), date, time_created (HH:MM,
    interpreted as LOCAL wall-clock time -- see the AT TIME ZONE casts
    below, not a naive string handed straight to timestamptz, which would
    silently be interpreted as UTC by Postgres' own session timezone),
    note_type, text_content, weight_kg, hba1c_percent, hba1c_mmol_mol.
    """
    tz_sql = _tz_sql()
    note_id = note_data.get("id")
    date_str = note_data["date"]
    time_str = note_data.get("time_created") or "00:00"
    time_created_local = f"{date_str} {time_str}:00"
    note_type = note_data["note_type"]
    text_content = note_data.get("text_content")
    weight_kg = note_data.get("weight_kg")
    hba1c_percent = note_data.get("hba1c_percent")
    hba1c_mmol_mol = note_data.get("hba1c_mmol_mol")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if note_id:
            # Editing an AAPS note converts it to a user note (per spec) --
            # only flip note_type if it was 'aaps' to begin with; editing an
            # existing user/weight/hba1c entry keeps its type as-is.
            cur.execute(f"""
                UPDATE clinical_notes
                SET date = %s, time_created = (%s::timestamp AT TIME ZONE {tz_sql}),
                    note_type = CASE WHEN note_type = 'aaps' THEN 'user' ELSE note_type END,
                    text_content = %s, weight_kg = %s,
                    hba1c_percent = %s, hba1c_mmol_mol = %s,
                    is_edited = TRUE, updated_at = now()
                WHERE id = %s
                RETURNING id, date, (time_created AT TIME ZONE {tz_sql}) AS time_created,
                          note_type, text_content,
                          weight_kg, hba1c_percent, hba1c_mmol_mol, is_edited
            """, (date_str, time_created_local, text_content, weight_kg,
                  hba1c_percent, hba1c_mmol_mol, note_id))
        else:
            cur.execute(f"""
                INSERT INTO clinical_notes
                    (date, time_created, note_type, text_content, weight_kg,
                     hba1c_percent, hba1c_mmol_mol)
                VALUES (%s, (%s::timestamp AT TIME ZONE {tz_sql}), %s, %s, %s, %s, %s)
                RETURNING id, date, (time_created AT TIME ZONE {tz_sql}) AS time_created,
                          note_type, text_content,
                          weight_kg, hba1c_percent, hba1c_mmol_mol, is_edited
            """, (date_str, time_created_local, note_type, text_content, weight_kg,
                  hba1c_percent, hba1c_mmol_mol))
        row = cur.fetchone()
        conn.commit()
        return _serialize_note_row(dict(row)) if row else None


def delete_note(conn, note_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM clinical_notes WHERE id = %s", (note_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def get_metric_records(conn, metric_type: str, start_date: str, end_date: str) -> list:
    """Rows for the dedicated Weight/HbA1c table screens. metric_type is
    'weight' or 'hba1c'."""
    tz_sql = _tz_sql()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT id, date, (time_created AT TIME ZONE {tz_sql}) AS time_created,
                   weight_kg, hba1c_percent, hba1c_mmol_mol
            FROM clinical_notes
            WHERE note_type = %s AND date BETWEEN %s AND %s
            ORDER BY date DESC, time_created DESC
        """, (metric_type, start_date, end_date))
        return [_serialize_note_row(dict(r)) for r in cur.fetchall()]


def get_all_hba1c_records(conn) -> list:
    """Fetch all historical HbA1c entries in chronological order for graphing."""
    tz_sql = _tz_sql()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT id, date, (time_created AT TIME ZONE {tz_sql}) AS time_created,
                   hba1c_percent, hba1c_mmol_mol
            FROM clinical_notes
            WHERE hba1c_percent IS NOT NULL
            ORDER BY date ASC, time_created ASC NULLS LAST, id ASC
        """)
        return [_serialize_note_row(dict(r)) for r in cur.fetchall()]


def get_hba1c_custom_gmi_pairs(conn, start_date, end_date) -> list:
    """Exact-date lab / trailing-90-day glucose pairs in a displayed range.

    The fit deliberately uses the daily risk row for the lab draw's calendar
    date, rather than the chart's weekly display samples or a nearby date.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                TO_CHAR(n.date, 'YYYY-MM-DD') AS date,
                n.hba1c_percent,
                r.mean_mmol_90d,
                r.sensor_active_pct_90d
            FROM clinical_notes n
            INNER JOIN layer2_daily_risk_stats r ON r.date = n.date
            WHERE n.hba1c_percent IS NOT NULL
              AND r.mean_mmol_90d IS NOT NULL
              AND r.sensor_active_pct_90d >= 90
              AND n.date BETWEEN %s AND %s
            ORDER BY n.date ASC, n.time_created ASC NULLS LAST, n.id ASC
        """, (start_date, end_date))
        rows = cur.fetchall()
        return [
            {
                "date": row["date"],
                "hba1c_percent": float(row["hba1c_percent"]),
                "mean_mmol_90d": float(row["mean_mmol_90d"]),
                "sensor_active_pct_90d": float(row["sensor_active_pct_90d"]),
            }
            for row in rows
        ]


# ---------------------------------------------------------------------------
# CLINICAL DIARY & NOTES SYSTEM (3-Pane Studio)
# ---------------------------------------------------------------------------

def get_diary_categories(conn) -> dict:
    """Returns categories with active note counts, plus counts for smart views."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT c.id, c.name, c.color, c.is_builtin,
                   COUNT(m.note_id)::int AS note_count
            FROM note_categories c
            LEFT JOIN note_category_map m ON c.id = m.category_id
            GROUP BY c.id, c.name, c.color, c.is_builtin
            ORDER BY c.is_builtin DESC, c.id ASC
        """)
        categories = [dict(r) for r in cur.fetchall()]

        # Smart view counts
        cur.execute("SELECT COUNT(*)::int AS count FROM clinical_notes")
        count_all = cur.fetchone()["count"]

        cur.execute("""
            SELECT COUNT(*)::int AS count FROM clinical_notes n
            WHERE NOT EXISTS (SELECT 1 FROM note_category_map m WHERE m.note_id = n.id)
        """)
        count_uncategorized = cur.fetchone()["count"]

        return {
            "categories": categories,
            "count_all": count_all,
            "count_uncategorized": count_uncategorized
        }


def save_diary_category(conn, cat_data: dict) -> dict:
    """Create or update a category."""
    cat_id = cat_data.get("id")
    name = (cat_data.get("name") or "").strip()
    color = (cat_data.get("color") or "#3498db").strip()
    if not name:
        raise ValueError("Category name is required")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if cat_id:
            cur.execute("""
                UPDATE note_categories
                SET name = %s, color = %s
                WHERE id = %s
                RETURNING id, name, color, is_builtin, created_at
            """, (name, color, cat_id))
        else:
            cur.execute("""
                INSERT INTO note_categories (name, color, is_builtin)
                VALUES (%s, %s, FALSE)
                RETURNING id, name, color, is_builtin, created_at
            """, (name, color))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def delete_diary_category(conn, cat_id: int) -> bool:
    """Delete a user category. Built-in categories (Weight, HbA1c) cannot be deleted.
    Unlinks notes safely (note_category_map rows cascade on delete)."""
    with conn.cursor() as cur:
        cur.execute("SELECT is_builtin FROM note_categories WHERE id = %s", (cat_id,))
        r = cur.fetchone()
        if not r:
            return False
        if r[0]:
            raise ValueError("Built-in categories cannot be deleted")

        cur.execute("DELETE FROM note_categories WHERE id = %s", (cat_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def get_diary_notes(conn, category_id: Optional[str] = None, search: Optional[str] = None,
                    sort: str = "date_desc", limit: int = 300) -> list:
    """Fetch note summaries for Pane 2 feed and reading canvas."""
    tz_sql = _tz_sql()
    params = []
    where_clauses = []

    # Category filtering
    if category_id:
        cat_str = str(category_id).strip().lower()
        if cat_str in ("all", ""):
            pass
        elif cat_str == "uncategorized":
            where_clauses.append("""
                NOT EXISTS (SELECT 1 FROM note_category_map m WHERE m.note_id = n.id)
            """)
        elif cat_str == "recent":
            sort = "recent_edited"
        elif cat_str.isdigit():
            where_clauses.append("""
                EXISTS (SELECT 1 FROM note_category_map m WHERE m.note_id = n.id AND m.category_id = %s)
            """)
            params.append(int(cat_str))
        else:
            # Match built-in biomarkers or custom category names (case-insensitive) or cat- prefix
            lookup_name = cat_str[4:] if cat_str.startswith("cat-") else cat_str
            if lookup_name in ("weight",):
                where_clauses.append("""
                    (EXISTS (SELECT 1 FROM note_category_map m 
                             JOIN note_categories c ON m.category_id = c.id 
                             WHERE m.note_id = n.id AND LOWER(c.name) = 'weight')
                     OR n.weight_kg IS NOT NULL)
                """)
            elif lookup_name in ("hba1c", "hba1c_percent", "hba1c_mmol_mol"):
                where_clauses.append("""
                    (EXISTS (SELECT 1 FROM note_category_map m 
                             JOIN note_categories c ON m.category_id = c.id 
                             WHERE m.note_id = n.id AND LOWER(c.name) = 'hba1c')
                     OR n.hba1c_percent IS NOT NULL OR n.hba1c_mmol_mol IS NOT NULL)
                """)
            else:
                where_clauses.append("""
                    EXISTS (SELECT 1 FROM note_category_map m 
                            JOIN note_categories c ON m.category_id = c.id 
                            WHERE m.note_id = n.id AND (LOWER(c.name) = LOWER(%s) OR c.id::text = %s))
                """)
                params.extend([lookup_name, lookup_name])

    # Text search
    if search and search.strip():
        q = f"%{search.strip()}%"
        where_clauses.append("(n.title ILIKE %s OR n.text_content ILIKE %s)")
        params.extend([q, q])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if sort == "recent_edited":
        order_sql = "ORDER BY n.updated_at DESC, n.date DESC, n.id DESC"
    else:
        order_sql = "ORDER BY n.date DESC, n.is_timeless DESC, n.time_created DESC NULLS LAST, n.id DESC"

    params.append(limit)

    query = f"""
        SELECT n.id, n.date, (n.time_created AT TIME ZONE {tz_sql}) AS time_created,
               n.is_timeless, n.title, n.note_type, n.text_content,
               n.weight_kg, n.hba1c_percent, n.hba1c_mmol_mol, n.is_edited,
               n.created_at, n.updated_at,
               COALESCE(
                   (SELECT json_agg(json_build_object('id', c.id, 'name', c.name, 'color', c.color, 'is_builtin', c.is_builtin))
                    FROM note_category_map m
                    JOIN note_categories c ON m.category_id = c.id
                    WHERE m.note_id = n.id),
                   '[]'::json
               ) AS categories
        FROM clinical_notes n
        {where_sql}
        {order_sql}
        LIMIT %s
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return [_serialize_diary_note_row(dict(r)) for r in rows]


def get_diary_note(conn, note_id: int) -> Optional[dict]:
    """Fetch single detailed note with full categories."""
    tz_sql = _tz_sql()
    query = f"""
        SELECT n.id, n.date, (n.time_created AT TIME ZONE {tz_sql}) AS time_created,
               n.is_timeless, n.title, n.note_type, n.text_content,
               n.weight_kg, n.hba1c_percent, n.hba1c_mmol_mol, n.is_edited,
               n.created_at, n.updated_at,
               COALESCE(
                   (SELECT json_agg(json_build_object('id', c.id, 'name', c.name, 'color', c.color, 'is_builtin', c.is_builtin))
                    FROM note_category_map m
                    JOIN note_categories c ON m.category_id = c.id
                    WHERE m.note_id = n.id),
                   '[]'::json
               ) AS categories
        FROM clinical_notes n
        WHERE n.id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (note_id,))
        row = cur.fetchone()
        return _serialize_diary_note_row(dict(row)) if row else None


def save_diary_note(conn, note_data: dict) -> dict:
    """Create or update note, including timeless flag, title, biomarkers, and category associations."""
    tz_sql = _tz_sql()
    note_id = note_data.get("id")
    date_str = note_data["date"]
    is_timeless = bool(note_data.get("is_timeless", False))
    time_str = note_data.get("time_created")

    if is_timeless or not time_str:
        time_created_val = None
    else:
        if len(time_str) == 5:
            time_created_val = f"{date_str} {time_str}:00"
        else:
            time_created_val = f"{date_str} {time_str}"

    title = (note_data.get("title") or "").strip() or None
    text_content = note_data.get("text_content") or ""
    weight_kg = note_data.get("weight_kg")
    hba1c_percent = note_data.get("hba1c_percent")
    hba1c_mmol_mol = note_data.get("hba1c_mmol_mol")
    note_type = note_data.get("note_type") or "user"
    if note_type not in ("user", "aaps", "weight", "hba1c"):
        note_type = "user"

    category_ids = note_data.get("category_ids") or []
    cat_id_set = set()
    for cid in category_ids:
        try:
            cat_id_set.add(int(cid))
        except (ValueError, TypeError):
            pass

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Auto-link built-in categories if biomarker entered
        if weight_kg is not None and str(weight_kg).strip() != "":
            cur.execute("SELECT id FROM note_categories WHERE name = 'Weight'")
            w_cat = cur.fetchone()
            if w_cat:
                cat_id_set.add(w_cat["id"])

        if (hba1c_percent is not None and str(hba1c_percent).strip() != "") or \
           (hba1c_mmol_mol is not None and str(hba1c_mmol_mol).strip() != ""):
            cur.execute("SELECT id FROM note_categories WHERE name = 'HbA1c'")
            h_cat = cur.fetchone()
            if h_cat:
                cat_id_set.add(h_cat["id"])

        if note_id:
            if time_created_val is not None:
                cur.execute(f"""
                    UPDATE clinical_notes
                    SET date = %s,
                        time_created = (%s::timestamp AT TIME ZONE {tz_sql}),
                        is_timeless = %s,
                        title = %s,
                        note_type = CASE WHEN note_type = 'aaps' THEN 'user' ELSE note_type END,
                        text_content = %s,
                        weight_kg = %s,
                        hba1c_percent = %s,
                        hba1c_mmol_mol = %s,
                        is_edited = TRUE,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                """, (date_str, time_created_val, is_timeless, title, text_content,
                      weight_kg, hba1c_percent, hba1c_mmol_mol, note_id))
            else:
                cur.execute("""
                    UPDATE clinical_notes
                    SET date = %s,
                        time_created = NULL,
                        is_timeless = %s,
                        title = %s,
                        note_type = CASE WHEN note_type = 'aaps' THEN 'user' ELSE note_type END,
                        text_content = %s,
                        weight_kg = %s,
                        hba1c_percent = %s,
                        hba1c_mmol_mol = %s,
                        is_edited = TRUE,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                """, (date_str, is_timeless, title, text_content,
                      weight_kg, hba1c_percent, hba1c_mmol_mol, note_id))
            target_id = note_id
        else:
            if time_created_val is not None:
                cur.execute(f"""
                    INSERT INTO clinical_notes
                        (date, time_created, is_timeless, title, note_type,
                         text_content, weight_kg, hba1c_percent, hba1c_mmol_mol,
                         is_edited, created_at, updated_at)
                    VALUES (%s, (%s::timestamp AT TIME ZONE {tz_sql}), %s, %s, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
                    RETURNING id
                """, (date_str, time_created_val, is_timeless, title, note_type,
                      text_content, weight_kg, hba1c_percent, hba1c_mmol_mol))
            else:
                cur.execute("""
                    INSERT INTO clinical_notes
                        (date, time_created, is_timeless, title, note_type,
                         text_content, weight_kg, hba1c_percent, hba1c_mmol_mol,
                         is_edited, created_at, updated_at)
                    VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
                    RETURNING id
                """, (date_str, is_timeless, title, note_type,
                      text_content, weight_kg, hba1c_percent, hba1c_mmol_mol))
            row = cur.fetchone()
            target_id = row["id"]

        # Sync category map
        cur.execute("DELETE FROM note_category_map WHERE note_id = %s", (target_id,))
        for cid in cat_id_set:
            cur.execute("""
                INSERT INTO note_category_map (note_id, category_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (target_id, cid))

        conn.commit()

    return get_diary_note(conn, target_id)


def delete_diary_note(conn, note_id: int) -> bool:
    """Delete a note and cascade delete its category mappings."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM clinical_notes WHERE id = %s", (note_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def _serialize_diary_note_row(row: dict) -> dict:
    """Serialize date, time_created, created_at, updated_at, and numeric Decimals."""
    if row.get("date") is not None:
        row["date"] = row["date"].isoformat()
    if row.get("time_created") is not None:
        dt = row["time_created"]
        row["time_created"] = dt.strftime("%H:%M") if hasattr(dt, "strftime") else str(dt)[:5]
    else:
        row["time_created"] = None

    if row.get("created_at") is not None and hasattr(row["created_at"], "isoformat"):
        row["created_at"] = row["created_at"].isoformat()
    if row.get("updated_at") is not None and hasattr(row["updated_at"], "isoformat"):
        row["updated_at"] = row["updated_at"].isoformat()

    if row.get("weight_kg") is not None:
        row["weight_kg"] = float(row["weight_kg"])
    if row.get("hba1c_percent") is not None:
        row["hba1c_percent"] = float(row["hba1c_percent"])

    return row


def import_diary_notes_batch(conn, rows: list, skip_duplicates: bool = True) -> dict:
    """
    Import a batch of diary notes from parsed CSV rows in a single atomic transaction.
    - Validates row fields (date, time, category, value).
    - Enforces row count limit (<= 500 rows).
    - Case-insensitively matches or auto-creates categories in note_categories.
    - Accurately parses numeric vs text values for biomarkers (Weight & HbA1c).
    - Converts plain text values to editor-compatible escaped HTML paragraphs.
    - Optionally skips identical duplicate notes.
    - Rolls back entire transaction if any unexpected error occurs.
    """
    if not isinstance(rows, list):
        raise ValueError("Rows must be a list")
    if len(rows) == 0:
        raise ValueError("No rows provided for import")
    if len(rows) > 500:
        raise ValueError("Import exceeds maximum limit of 500 rows per batch")

    tz_sql = _tz_sql()
    palette = ["#3498db", "#e67e22", "#1abc9c", "#9b59b6", "#e74c3c", "#f1c40f", "#2ecc71", "#34495e"]

    # Query today local date for future-date guard
    with conn.cursor() as cur:
        cur.execute(f"SELECT (NOW() AT TIME ZONE {tz_sql})::date")
        today_local = cur.fetchone()[0]

    imported_count = 0
    skipped_count = 0
    new_categories_created = []
    dates_processed = []

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Load existing categories into case-insensitive map
            cur.execute("SELECT id, name, color, is_builtin FROM note_categories")
            cat_rows = cur.fetchall()
            cat_map = {r["name"].strip().lower(): dict(r) for r in cat_rows}

            # 2. Identify missing categories and insert them safely
            palette_idx = len(cat_rows)
            for row in rows:
                raw_cat = (row.get("category") or "").strip()
                if not raw_cat:
                    raw_cat = "General"
                cat_key = raw_cat.lower()
                if cat_key not in cat_map:
                    color = palette[palette_idx % len(palette)]
                    palette_idx += 1
                    cur.execute("""
                        INSERT INTO note_categories (name, color, is_builtin)
                        VALUES (%s, %s, FALSE)
                        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                        RETURNING id, name, color, is_builtin
                    """, (raw_cat, color))
                    new_cat = dict(cur.fetchone())
                    cat_map[cat_key] = new_cat
                    new_categories_created.append(new_cat["name"])

            # 3. Process each row
            for idx, r in enumerate(rows, start=1):
                raw_date = (r.get("date") or "").strip()
                if not raw_date:
                    raise ValueError(f"Row {idx}: Date is required")

                try:
                    parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                except ValueError:
                    raise ValueError(f"Row {idx}: Invalid date '{raw_date}'. Must be ISO YYYY-MM-DD")

                if parsed_date > today_local:
                    raise ValueError(f"Row {idx}: Date '{raw_date}' is in the future")

                date_str = parsed_date.isoformat()
                dates_processed.append(date_str)

                # Time parsing
                raw_time = (r.get("time") or "").strip().lower()
                is_timeless = raw_time in ("", "all-day", "allday", "all day", "-", "none", "null")
                time_created_val = None

                if not is_timeless:
                    parts = raw_time.split(":")
                    if len(parts) in (2, 3):
                        try:
                            h, m = int(parts[0]), int(parts[1])
                            if not (0 <= h <= 23 and 0 <= m <= 59):
                                raise ValueError()
                            time_created_val = f"{date_str} {h:02d}:{m:02d}:00"
                        except ValueError:
                            raise ValueError(f"Row {idx}: Invalid time '{r.get('time')}'. Must be 24-hour HH:MM")
                    else:
                        raise ValueError(f"Row {idx}: Invalid time '{r.get('time')}'. Must be 24-hour HH:MM")

                # Category identification
                raw_cat = (r.get("category") or "").strip() or "General"
                cat_info = cat_map[raw_cat.lower()]
                cat_id = cat_info["id"]
                cat_name = cat_info["name"]

                # Value & biomarker processing
                raw_value = str(r.get("value") or "").strip()
                weight_kg = None
                hba1c_percent = None
                hba1c_mmol_mol = None
                note_type = "user"

                # Check built-in biomarker categories
                if cat_name.lower() == "weight":
                    num_match = re.search(r"^[-+]?([0-9]+(?:\.[0-9]+)?)", raw_value)
                    if num_match:
                        try:
                            weight_kg = float(num_match.group(1))
                            note_type = "weight"
                        except ValueError:
                            weight_kg = None
                elif cat_name.lower() == "hba1c":
                    num_match = re.search(r"^[-+]?([0-9]+(?:\.[0-9]+)?)", raw_value)
                    if num_match:
                        try:
                            val_num = float(num_match.group(1))
                            note_type = "hba1c"
                            if val_num <= 20.0:
                                hba1c_percent = val_num
                                hba1c_mmol_mol = hba1c_percent_to_mmol_mol(val_num)
                            else:
                                hba1c_mmol_mol = round(val_num)
                                hba1c_percent = hba1c_mmol_mol_to_percent(val_num)
                        except ValueError:
                            pass

                # Convert plain text to rich HTML editor paragraphs safely
                escaped_lines = [html.escape(line) for line in raw_value.splitlines() if line.strip()]
                if escaped_lines:
                    text_content = "".join(f"<p>{l}</p>" for l in escaped_lines)
                else:
                    if weight_kg is not None:
                        text_content = f"<p>{weight_kg} kg</p>"
                    elif hba1c_percent is not None:
                        text_content = f"<p>HbA1c: {hba1c_percent}% ({hba1c_mmol_mol} mmol/mol)</p>"
                    else:
                        text_content = "<p></p>"

                # Duplicate detection check
                if skip_duplicates:
                    if time_created_val is not None:
                        cur.execute(f"""
                            SELECT n.id FROM clinical_notes n
                            JOIN note_category_map m ON n.id = m.note_id
                            WHERE n.date = %s
                              AND n.time_created = (%s::timestamp AT TIME ZONE {tz_sql})
                              AND m.category_id = %s
                              AND n.text_content = %s
                            LIMIT 1
                        """, (date_str, time_created_val, cat_id, text_content))
                    else:
                        cur.execute("""
                            SELECT n.id FROM clinical_notes n
                            JOIN note_category_map m ON n.id = m.note_id
                            WHERE n.date = %s
                              AND n.time_created IS NULL
                              AND m.category_id = %s
                              AND n.text_content = %s
                            LIMIT 1
                        """, (date_str, cat_id, text_content))

                    if cur.fetchone():
                        skipped_count += 1
                        continue

                # Insert note
                if time_created_val is not None:
                    cur.execute(f"""
                        INSERT INTO clinical_notes
                            (date, time_created, is_timeless, title, note_type,
                             text_content, weight_kg, hba1c_percent, hba1c_mmol_mol,
                             is_edited, created_at, updated_at)
                        VALUES (%s, (%s::timestamp AT TIME ZONE {tz_sql}), %s, NULL, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
                        RETURNING id
                    """, (date_str, time_created_val, is_timeless, note_type,
                          text_content, weight_kg, hba1c_percent, hba1c_mmol_mol))
                else:
                    cur.execute("""
                        INSERT INTO clinical_notes
                            (date, time_created, is_timeless, title, note_type,
                             text_content, weight_kg, hba1c_percent, hba1c_mmol_mol,
                             is_edited, created_at, updated_at)
                        VALUES (%s, NULL, %s, NULL, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
                        RETURNING id
                    """, (date_str, is_timeless, note_type,
                          text_content, weight_kg, hba1c_percent, hba1c_mmol_mol))

                new_note_id = cur.fetchone()["id"]

                # Link category in note_category_map
                cur.execute("""
                    INSERT INTO note_category_map (note_id, category_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (new_note_id, cat_id))

                imported_count += 1

        # Commit single atomic batch
        conn.commit()

        min_d = min(dates_processed) if dates_processed else None
        max_d = max(dates_processed) if dates_processed else None

        return {
            "success": True,
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "new_categories": list(set(new_categories_created)),
            "min_date": min_d,
            "max_date": max_d
        }
    except Exception:
        conn.rollback()
        raise



# ---------------------------------------------------------------------------
# Dashboard Layouts & Widgets (Phase 1)
# ---------------------------------------------------------------------------
DEVICE_PROFILES = {
    "computer": 24,
    "phone_portrait": 2,
    "phone_landscape": 6,
    "tablet_portrait": 8,
    "tablet_landscape": 12,
}

# This is the server-side counterpart of dashboard_home.html's widget catalogue.
# Keeping the allow-list here prevents malformed or unknown widget payloads from
# becoming persistent layouts that the client cannot reliably render.
DASHBOARD_WIDGET_TYPES = {
    "autosens_ratio_stat", "avg_bg_scorecard", "cgm_sensor_timer",
    "current_bg_hero", "custom_text_header", "cv_scorecard",
    "device_battery_stat", "ghost_curve_overlay", "gmi_scorecard",
    "gri_scorecard", "gvi_scorecard", "hbgi_scorecard", "hypo_free_streak",
    "iob_cob_stat", "lbgi_scorecard", "mag_scorecard", "meal_analysis_placeholder",
    "pod_countdown_timer", "range_scorecard", "recent_notes_feed",
    "reservoir_monitor", "sd_scorecard", "smb_activity_stat", "stacked_range_bar",
    "tar_scorecard", "tatr_scorecard", "tbr_scorecard", "tdd_dosing_stat",
    "tir_scorecard", "titr_scorecard", "triage_focus_table",
}


def _dashboard_profile_coordinates(value: Any, columns: int) -> Dict[str, int]:
    """Validate one persisted GridStack coordinate set for a device profile."""
    if not isinstance(value, dict):
        raise ValueError("Profile coordinates must be an object.")
    try:
        x, y, w, h = (int(value[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Profile coordinates require integer x, y, w and h values.") from exc
    if x < 0 or y < 0 or w < 1 or h < 1 or x + w > columns:
        raise ValueError("Profile coordinates are outside the selected device grid.")
    return {"x": x, "y": y, "w": w, "h": h}


def _dashboard_widget_key(value: Any) -> str:
    """Return a validated stable widget UUID, generating one for a new widget."""
    import uuid
    if value in (None, ""):
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("widget_key must be a UUID.") from exc


def get_dashboard_slots(conn) -> List[Dict[str, Any]]:
    """Return all dashboard layout slots ordered by display_order."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, slot_number, display_order, name, target_device, is_active, created_at, updated_at
            FROM dashboard_layouts
            ORDER BY display_order ASC, slot_number ASC
        """)
        rows = cur.fetchall()
        for r in rows:
            if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                r["created_at"] = r["created_at"].isoformat()
            if r.get("updated_at") and hasattr(r["updated_at"], "isoformat"):
                r["updated_at"] = r["updated_at"].isoformat()
        return rows


def reorder_dashboard_slots(conn, slots_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Update display_order and optionally names of dashboard slots in an atomic transaction."""
    with conn.cursor() as cur:
        for item in slots_data:
            slot_num = item.get("slot_number")
            order = item.get("display_order")
            name = item.get("name")
            target_device = item.get("target_device")
            if slot_num is not None:
                if int(slot_num) == 1 and target_device not in (None, "computer"):
                    raise ValueError("Dashboard slot 1 must use the computer profile.")
                if target_device is not None and target_device not in DEVICE_PROFILES:
                    raise ValueError("Unknown dashboard device profile.")
                if name is not None and str(name).strip():
                    cur.execute("""
                        UPDATE dashboard_layouts
                        SET display_order = COALESCE(%s, display_order),
                            name = %s,
                            target_device = COALESCE(%s, target_device),
                            updated_at = NOW()
                        WHERE slot_number = %s
                    """, (order, str(name).strip(), target_device, int(slot_num)))
                else:
                    cur.execute("""
                        UPDATE dashboard_layouts
                        SET display_order = COALESCE(%s, display_order),
                            target_device = COALESCE(%s, target_device),
                            updated_at = NOW()
                        WHERE slot_number = %s
                    """, (order, target_device, int(slot_num)))
        conn.commit()
    return get_dashboard_slots(conn)


def set_active_dashboard_slot(conn, slot_number: int) -> bool:
    """Set which slot number (1 through 6) is currently active."""
    with conn.cursor() as cur:
        cur.execute("UPDATE dashboard_layouts SET is_active = (slot_number = %s)", (slot_number,))
        updated = cur.rowcount > 0
        conn.commit()
        return updated


def get_dashboard_layout(conn, slot_number: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Get active layout (or specific slot_number) and all its widgets."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if slot_number is not None:
            cur.execute("""
                SELECT id, slot_number, name, target_device, is_active
                FROM dashboard_layouts
                WHERE slot_number = %s
            """, (slot_number,))
        else:
            cur.execute("""
                SELECT id, slot_number, name, target_device, is_active
                FROM dashboard_layouts
                WHERE is_active = TRUE
                LIMIT 1
            """)
        layout = cur.fetchone()
        if not layout:
            return None

        cur.execute("""
            SELECT id, widget_key, widget_type, title, x, y, w, h, config, profile_overrides
            FROM dashboard_widgets
            WHERE layout_id = %s
            ORDER BY y ASC, x ASC, id ASC
        """, (layout["id"],))
        widgets = cur.fetchall()
        layout["widgets"] = widgets
        return layout


def save_dashboard_layout(
    conn, slot_number: int, name: Optional[str], widgets: List[Dict[str, Any]], edited_profile: str
) -> Dict[str, Any]:
    """Save/replace all widgets and optionally rename a dashboard slot in an atomic transaction."""
    if edited_profile not in DEVICE_PROFILES:
        raise ValueError("Unknown dashboard device profile.")
    if slot_number == 1 and edited_profile != "computer":
        raise ValueError("Dashboard slot 1 must use the computer profile.")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 1. Update slot name if provided
        if name:
            cur.execute("""
                UPDATE dashboard_layouts
                SET name = %s, updated_at = NOW()
                WHERE slot_number = %s
                RETURNING id
            """, (name.strip(), slot_number))
        else:
            cur.execute("""
                UPDATE dashboard_layouts
                SET updated_at = NOW()
                WHERE slot_number = %s
                RETURNING id
            """, (slot_number,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Dashboard slot {slot_number} does not exist.")
        layout_id = row["id"]

        cur.execute("SELECT target_device FROM dashboard_layouts WHERE id = %s", (layout_id,))
        layout = cur.fetchone()
        if layout and layout["target_device"] != edited_profile:
            raise ValueError("The saved profile does not match this dashboard slot.")

        cur.execute("SELECT widget_key, x, y, w, h, profile_overrides FROM dashboard_widgets WHERE layout_id = %s", (layout_id,))
        existing_widgets = {str(row["widget_key"]): row for row in cur.fetchall()}
        widget_keys = set()
        prepared_widgets = []
        for widget in widgets:
            if not isinstance(widget, dict):
                raise ValueError("Each dashboard widget must be an object.")
            widget_key = _dashboard_widget_key(widget.get("widget_key"))
            if widget_key in widget_keys:
                raise ValueError("Each dashboard widget must have a unique widget_key.")
            widget_keys.add(widget_key)

            widget_type = widget.get("widget_type")
            if widget_type not in DASHBOARD_WIDGET_TYPES:
                raise ValueError("Unknown dashboard widget type.")
            if not isinstance(widget.get("config", {}), dict):
                raise ValueError("Dashboard widget config must be an object.")

            existing = existing_widgets.get(widget_key)
            stored_overrides = dict(existing.get("profile_overrides") or {}) if existing else {}
            incoming_overrides = widget.get("profile_overrides") or {}
            if edited_profile == "computer":
                desktop = _dashboard_profile_coordinates(widget, DEVICE_PROFILES["computer"])
            else:
                edited_coordinates = _dashboard_profile_coordinates(
                    incoming_overrides.get(edited_profile), DEVICE_PROFILES[edited_profile]
                )
                stored_overrides[edited_profile] = edited_coordinates
                desktop = (
                    _dashboard_profile_coordinates(existing, DEVICE_PROFILES["computer"])
                    if existing else _dashboard_profile_coordinates(widget, DEVICE_PROFILES["computer"])
                )

            prepared_widgets.append({
                "widget_key": widget_key,
                "widget_type": widget_type,
                "title": widget.get("title", ""),
                "x": desktop["x"], "y": desktop["y"], "w": desktop["w"], "h": desktop["h"],
                "config": widget.get("config", {}),
                "profile_overrides": stored_overrides,
            })

        # 2. Replace widgets
        cur.execute("DELETE FROM dashboard_widgets WHERE layout_id = %s", (layout_id,))
        for w in prepared_widgets:
            cur.execute("""
                INSERT INTO dashboard_widgets (layout_id, widget_key, widget_type, title, x, y, w, h, config, profile_overrides)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                layout_id,
                w["widget_key"],
                w.get("widget_type", "unnamed_widget"),
                w.get("title", ""),
                int(w.get("x", 0)),
                int(w.get("y", 0)),
                int(w.get("w", 4)),
                int(w.get("h", 3)),
                psycopg2.extras.Json(w.get("config", {})),
                psycopg2.extras.Json(w.get("profile_overrides", {})),
            ))
        conn.commit()

    return get_dashboard_layout(conn, slot_number)


def _calc_gri(vlow_v, low_v, vhigh_v, high_v):
    vl = float(vlow_v or 0)
    l = float(low_v or 0)
    vh = float(vhigh_v or 0)
    h = float(high_v or 0)
    hypo_comp = min(100.0, vl + 0.8 * l)
    hyper_comp = min(100.0, vh + 0.5 * h)
    return round(min(100.0, (3.0 * hypo_comp) + (1.6 * hyper_comp)), 1)


def _fetch_glycemic_stats(cur, res: Dict[str, Any]) -> None:
    # 1. Latest 2 daily records (today / latest available, and yesterday)
    cur.execute("""
        SELECT 
            date,
            tir, titr, tar, tbr, vlow, low, high, vhigh,
            mean_mmol, sd_mmol,
            round((3.31 + (0.430995 * mean_mmol)), 1) AS gmi,
            round(((100.0 * sd_mmol) / NULLIF(mean_mmol, 0)), 1) AS cv,
            gri, lbgi, hbgi, gvi, mag, tdd, carbs, episode_count,
            
            tir_7d, titr_7d, tar_7d, tbr_7d, vlow_7d, low_7d, high_7d, vhigh_7d,
            mean_mmol_7d, sd_mmol_7d, gmi_7d, cv_7d,
            lbgi_7d, hbgi_7d, gvi_7d, mag_7d, tdd_7d, carbs_7d, episode_count_7d,
            
            tir_14d, titr_14d, tar_14d, tbr_14d, vlow_14d, low_14d, high_14d, vhigh_14d,
            mean_mmol_14d, sd_mmol_14d, gmi_14d, cv_14d,
            lbgi_14d, hbgi_14d, gvi_14d, mag_14d, tdd_14d, carbs_14d,
            
            tir_30d, titr_30d, tar_30d, tbr_30d, vlow_30d, high_30d, vhigh_30d,
            mean_mmol_30d, sd_mmol_30d, gmi_30d, cv_30d,
            lbgi_30d, hbgi_30d, gvi_30d, mag_30d,
            
            tir_60d, titr_60d, tar_60d, tbr_60d, vlow_60d, high_60d, vhigh_60d,
            mean_mmol_60d, sd_mmol_60d, cv_60d, gmi_60d,
            lbgi_60d, hbgi_60d, gvi_60d, mag_60d,
            
            tir_90d, titr_90d, tar_90d, tbr_90d, vlow_90d, high_90d, vhigh_90d,
            mean_mmol_90d, sd_mmol_90d, cv_90d, gmi_90d,
            lbgi_90d, hbgi_90d, gvi_90d, mag_90d
        FROM layer2_daily_risk_stats
        ORDER BY date DESC
        LIMIT 2
    """)
    rows = cur.fetchall()

    if rows:
        t = dict(rows[0])
        if t.get("date"):
            t["date"] = str(t["date"])

        t["gri_7d"] = _calc_gri(t.get("vlow_7d"), t.get("low_7d"), t.get("vhigh_7d"), t.get("high_7d"))
        t["gri_14d"] = _calc_gri(t.get("vlow_14d"), t.get("low_14d"), t.get("vhigh_14d"), t.get("high_14d"))
        t["gri_30d"] = _calc_gri(t.get("vlow_30d"), t.get("low_30d"), t.get("vhigh_30d"), t.get("high_30d"))
        t["gri_90d"] = _calc_gri(t.get("vlow_90d"), t.get("low_90d"), t.get("vhigh_90d"), t.get("high_90d"))

        # TATR (Time Above Tight Range: > 7.8 mmol/L = 100 - TITR - TBR)
        if t.get("titr") is not None and t.get("tbr") is not None:
            t["tatr"] = round(max(0.0, 100.0 - float(t["titr"] or 0) - float(t["tbr"] or 0)), 1)
        t["tatr_7d"] = round(max(0.0, 100.0 - float(t.get("titr_7d") or 0) - float(t.get("tbr_7d") or 0)), 1) if t.get("titr_7d") is not None else None
        t["tatr_14d"] = round(max(0.0, 100.0 - float(t.get("titr_14d") or 0) - float(t.get("tbr_14d") or 0)), 1) if t.get("titr_14d") is not None else None
        t["tatr_30d"] = round(max(0.0, 100.0 - float(t.get("titr_30d") or 0) - float(t.get("tbr_30d") or 0)), 1) if t.get("titr_30d") is not None else None
        t["tatr_60d"] = round(max(0.0, 100.0 - float(t.get("titr_60d") or 0) - float(t.get("tbr_60d") or 0)), 1) if t.get("titr_60d") is not None else None
        t["tatr_90d"] = round(max(0.0, 100.0 - float(t.get("titr_90d") or 0) - float(t.get("tbr_90d") or 0)), 1) if t.get("titr_90d") is not None else None

        res["today"] = t

        # Rolling shortcuts for 7d, 14d, 30d, 90d
        res["rolling"] = {
            "tir_7d": t.get("tir_7d"), "tir_14d": t.get("tir_14d"), "tir_30d": t.get("tir_30d"), "tir_90d": t.get("tir_90d"),
            "tir_60d": t.get("tir_60d"),
            "titr_7d": t.get("titr_7d"), "titr_14d": t.get("titr_14d"), "titr_30d": t.get("titr_30d"), "titr_60d": t.get("titr_60d"), "titr_90d": t.get("titr_90d"),
            "tatr_7d": t.get("tatr_7d"), "tatr_14d": t.get("tatr_14d"), "tatr_30d": t.get("tatr_30d"), "tatr_60d": t.get("tatr_60d"), "tatr_90d": t.get("tatr_90d"),
            "tar_7d": t.get("tar_7d"), "tar_14d": t.get("tar_14d"), "tar_30d": t.get("tar_30d"), "tar_60d": t.get("tar_60d"), "tar_90d": t.get("tar_90d"),
            "tbr_7d": t.get("tbr_7d"), "tbr_14d": t.get("tbr_14d"), "tbr_30d": t.get("tbr_30d"), "tbr_60d": t.get("tbr_60d"), "tbr_90d": t.get("tbr_90d"),
            "vlow_7d": t.get("vlow_7d"), "vlow_14d": t.get("vlow_14d"), "vlow_30d": t.get("vlow_30d"), "vlow_60d": t.get("vlow_60d"), "vlow_90d": t.get("vlow_90d"),
            "vhigh_7d": t.get("vhigh_7d"), "vhigh_14d": t.get("vhigh_14d"), "vhigh_30d": t.get("vhigh_30d"), "vhigh_60d": t.get("vhigh_60d"), "vhigh_90d": t.get("vhigh_90d"),
            "mean_7d": t.get("mean_mmol_7d"), "mean_14d": t.get("mean_mmol_14d"), "mean_30d": t.get("mean_mmol_30d"), "mean_90d": t.get("mean_mmol_90d"),
            "mean_mmol_7d": t.get("mean_mmol_7d"), "mean_mmol_14d": t.get("mean_mmol_14d"), "mean_mmol_30d": t.get("mean_mmol_30d"), "mean_mmol_90d": t.get("mean_mmol_90d"),
            "sd_7d": t.get("sd_mmol_7d"), "sd_14d": t.get("sd_mmol_14d"), "sd_30d": t.get("sd_mmol_30d"), "sd_90d": t.get("sd_mmol_90d"),
            "sd_mmol_7d": t.get("sd_mmol_7d"), "sd_mmol_14d": t.get("sd_mmol_14d"), "sd_mmol_30d": t.get("sd_mmol_30d"), "sd_mmol_90d": t.get("sd_mmol_90d"),
            "gmi_7d": t.get("gmi_7d"), "gmi_14d": t.get("gmi_14d"), "gmi_30d": t.get("gmi_30d"), "gmi_60d": t.get("gmi_60d"), "gmi_90d": t.get("gmi_90d"),
            "cv_7d": t.get("cv_7d"), "cv_14d": t.get("cv_14d"), "cv_30d": t.get("cv_30d"), "cv_90d": t.get("cv_90d"),
            "gri_7d": t.get("gri_7d"), "gri_14d": t.get("gri_14d"), "gri_30d": t.get("gri_30d"), "gri_90d": t.get("gri_90d"),
            "lbgi_7d": t.get("lbgi_7d"), "lbgi_14d": t.get("lbgi_14d"), "lbgi_30d": t.get("lbgi_30d"), "lbgi_90d": t.get("lbgi_90d"),
            "hbgi_7d": t.get("hbgi_7d"), "hbgi_14d": t.get("hbgi_14d"), "hbgi_30d": t.get("hbgi_30d"), "hbgi_90d": t.get("hbgi_90d"),
            "gvi_7d": t.get("gvi_7d"), "gvi_14d": t.get("gvi_14d"), "gvi_30d": t.get("gvi_30d"), "gvi_90d": t.get("gvi_90d"),
            "mag_7d": t.get("mag_7d"), "mag_14d": t.get("mag_14d"), "mag_30d": t.get("mag_30d"), "mag_90d": t.get("mag_90d"),
            "tdd_7d": t.get("tdd_7d"), "tdd_14d": t.get("tdd_14d"), "carbs_7d": t.get("carbs_7d"), "carbs_14d": t.get("carbs_14d"),
            "episode_count_7d": t.get("episode_count_7d")
        }

    if len(rows) > 1:
        y = dict(rows[1])
        if y.get("date"):
            y["date"] = str(y["date"])
        if y.get("titr") is not None and y.get("tbr") is not None:
            y["tatr"] = round(max(0.0, 100.0 - float(y["titr"] or 0) - float(y["tbr"] or 0)), 1)
        res["yesterday"] = y

    # 1b. Trailing 365-Day Aggregate Averages
    cur.execute("""
        SELECT 
            round(avg(tir), 1) as tir_365d,
            round(avg(titr), 1) as titr_365d,
            round(avg(tar), 1) as tar_365d,
            round(avg(tbr), 1) as tbr_365d,
            round(avg(vlow), 1) as vlow_365d,
            round(avg(vhigh), 1) as vhigh_365d,
            round(avg(mean_mmol), 2) as mean_mmol_365d,
            round(avg(sd_mmol), 2) as sd_mmol_365d,
            round(avg(100.0 * sd_mmol / NULLIF(mean_mmol, 0)), 1) as cv_365d,
            round(3.31 + (0.431 * avg(mean_mmol)), 2) as gmi_365d,
            round(avg(gri), 1) as gri_365d,
            round(avg(lbgi), 2) as lbgi_365d,
            round(avg(hbgi), 2) as hbgi_365d,
            round(avg(gvi), 2) as gvi_365d,
            round(avg(mag), 2) as mag_365d
        FROM layer2_daily_risk_stats 
        WHERE date >= CURRENT_DATE - INTERVAL '365 days';
    """)
    row_365 = cur.fetchone()
    if row_365:
        titr_365 = float(row_365["titr_365d"] or 0)
        tbr_365 = float(row_365["tbr_365d"] or 0)
        tatr_365 = round(max(0.0, 100.0 - titr_365 - tbr_365), 1)
        res["rolling"].update({
            "tir_365d": row_365["tir_365d"],
            "titr_365d": row_365["titr_365d"],
            "tatr_365d": tatr_365,
            "tar_365d": row_365["tar_365d"],
            "tbr_365d": row_365["tbr_365d"],
            "vlow_365d": row_365["vlow_365d"],
            "vhigh_365d": row_365["vhigh_365d"],
            "mean_365d": row_365["mean_mmol_365d"],
            "mean_mmol_365d": row_365["mean_mmol_365d"],
            "sd_365d": row_365["sd_mmol_365d"],
            "sd_mmol_365d": row_365["sd_mmol_365d"],
            "cv_365d": row_365["cv_365d"],
            "gmi_365d": row_365["gmi_365d"],
            "gri_365d": row_365["gri_365d"],
            "lbgi_365d": row_365["lbgi_365d"],
            "hbgi_365d": row_365["hbgi_365d"],
            "gvi_365d": row_365["gvi_365d"],
            "mag_365d": row_365["mag_365d"]
        })


def _fetch_best_records(cur, res: Dict[str, Any]) -> None:
    # 2. All-Time & Last-365d Best Records
    cur.execute("""
        SELECT
            -- All-Time Best
            (SELECT json_build_object('val', tir_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE tir_14d IS NOT NULL ORDER BY tir_14d DESC LIMIT 1) AS best_tir,
             
            (SELECT json_build_object('val', titr_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE titr_14d IS NOT NULL ORDER BY titr_14d DESC LIMIT 1) AS best_titr,

            (SELECT json_build_object('val', round(100.0 - titr_14d - tbr_14d, 1), 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE titr_14d IS NOT NULL AND tbr_14d IS NOT NULL ORDER BY (100.0 - titr_14d - tbr_14d) ASC LIMIT 1) AS best_tatr,

            (SELECT json_build_object('val', tar_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE tar_14d IS NOT NULL ORDER BY tar_14d ASC LIMIT 1) AS best_tar,

            (SELECT json_build_object('val', tbr_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE tbr_14d IS NOT NULL ORDER BY tbr_14d ASC LIMIT 1) AS best_tbr,

            (SELECT json_build_object('val', vlow_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE vlow_14d IS NOT NULL ORDER BY vlow_14d ASC LIMIT 1) AS best_vlow,

            (SELECT json_build_object('val', vhigh_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE vhigh_14d IS NOT NULL ORDER BY vhigh_14d ASC LIMIT 1) AS best_vhigh,
             
            (SELECT json_build_object('val', mean_mmol_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE mean_mmol_14d IS NOT NULL ORDER BY mean_mmol_14d ASC LIMIT 1) AS best_mean,

            (SELECT json_build_object('val', sd_mmol_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE sd_mmol_14d IS NOT NULL ORDER BY sd_mmol_14d ASC LIMIT 1) AS best_sd,
             
            (SELECT json_build_object('val', gmi_90d, 'date', to_char(date, 'Mon YYYY'))
             FROM layer2_daily_risk_stats WHERE gmi_90d IS NOT NULL ORDER BY gmi_90d ASC LIMIT 1) AS best_gmi,
             
            (SELECT json_build_object('val', cv_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE cv_14d IS NOT NULL ORDER BY cv_14d ASC LIMIT 1) AS best_cv,

            (SELECT json_build_object('val', round(LEAST(100.0, (3.0 * LEAST(100.0, vlow_14d + 0.8 * low_14d)) + (1.6 * LEAST(100.0, vhigh_14d + 0.5 * high_14d))), 1), 'date', to_char(date, 'Mon YYYY'))
             FROM layer2_daily_risk_stats WHERE vlow_14d IS NOT NULL ORDER BY LEAST(100.0, (3.0 * LEAST(100.0, vlow_14d + 0.8 * low_14d)) + (1.6 * LEAST(100.0, vhigh_14d + 0.5 * high_14d))) ASC LIMIT 1) AS best_gri,

            (SELECT json_build_object('val', lbgi_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE lbgi_14d IS NOT NULL ORDER BY lbgi_14d ASC LIMIT 1) AS best_lbgi,

            (SELECT json_build_object('val', hbgi_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE hbgi_14d IS NOT NULL ORDER BY hbgi_14d ASC LIMIT 1) AS best_hbgi,

            (SELECT json_build_object('val', gvi_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE gvi_14d IS NOT NULL ORDER BY gvi_14d ASC LIMIT 1) AS best_gvi,

            (SELECT json_build_object('val', mag_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE mag_14d IS NOT NULL ORDER BY mag_14d ASC LIMIT 1) AS best_mag,

            -- Last 365 Days Best
            (SELECT json_build_object('val', tir_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE tir_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY tir_14d DESC LIMIT 1) AS best_tir_365d,

            (SELECT json_build_object('val', titr_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE titr_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY titr_14d DESC LIMIT 1) AS best_titr_365d,

            (SELECT json_build_object('val', round(100.0 - titr_14d - tbr_14d, 1), 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE titr_14d IS NOT NULL AND tbr_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY (100.0 - titr_14d - tbr_14d) ASC LIMIT 1) AS best_tatr_365d,

            (SELECT json_build_object('val', tar_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE tar_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY tar_14d ASC LIMIT 1) AS best_tar_365d,

            (SELECT json_build_object('val', tbr_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE tbr_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY tbr_14d ASC LIMIT 1) AS best_tbr_365d,

            (SELECT json_build_object('val', vlow_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE vlow_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY vlow_14d ASC LIMIT 1) AS best_vlow_365d,

            (SELECT json_build_object('val', vhigh_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE vhigh_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY vhigh_14d ASC LIMIT 1) AS best_vhigh_365d,

            (SELECT json_build_object('val', mean_mmol_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE mean_mmol_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY mean_mmol_14d ASC LIMIT 1) AS best_mean_365d,

            (SELECT json_build_object('val', sd_mmol_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE sd_mmol_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY sd_mmol_14d ASC LIMIT 1) AS best_sd_365d,

            (SELECT json_build_object('val', gmi_90d, 'date', to_char(date, 'Mon YYYY'))
             FROM layer2_daily_risk_stats WHERE gmi_90d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY gmi_90d ASC LIMIT 1) AS best_gmi_365d,

            (SELECT json_build_object('val', cv_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE cv_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY cv_14d ASC LIMIT 1) AS best_cv_365d,

            (SELECT json_build_object('val', round(LEAST(100.0, (3.0 * LEAST(100.0, vlow_14d + 0.8 * low_14d)) + (1.6 * LEAST(100.0, vhigh_14d + 0.5 * high_14d))), 1), 'date', to_char(date, 'Mon YYYY'))
             FROM layer2_daily_risk_stats WHERE vlow_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY LEAST(100.0, (3.0 * LEAST(100.0, vlow_14d + 0.8 * low_14d)) + (1.6 * LEAST(100.0, vhigh_14d + 0.5 * high_14d))) ASC LIMIT 1) AS best_gri_365d,

            (SELECT json_build_object('val', lbgi_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE lbgi_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY lbgi_14d ASC LIMIT 1) AS best_lbgi_365d,

            (SELECT json_build_object('val', hbgi_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE hbgi_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY hbgi_14d ASC LIMIT 1) AS best_hbgi_365d,

            (SELECT json_build_object('val', gvi_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE gvi_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY gvi_14d ASC LIMIT 1) AS best_gvi_365d,

            (SELECT json_build_object('val', mag_14d, 'date', to_char(date, 'Mon YYYY')) 
             FROM layer2_daily_risk_stats WHERE mag_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY mag_14d ASC LIMIT 1) AS best_mag_365d
    """)
    rec = cur.fetchone()
    if rec:
        res["records"] = {
            "tir": rec["best_tir"] or {"val": None, "date": "-"},
            "titr": rec["best_titr"] or {"val": None, "date": "-"},
            "tatr": rec["best_tatr"] or {"val": None, "date": "-"},
            "tar": rec["best_tar"] or {"val": None, "date": "-"},
            "tbr": rec["best_tbr"] or {"val": None, "date": "-"},
            "vlow": rec["best_vlow"] or {"val": None, "date": "-"},
            "vhigh": rec["best_vhigh"] or {"val": None, "date": "-"},
            "mean_mmol": rec["best_mean"] or {"val": None, "date": "-"},
            "avg_bg": rec["best_mean"] or {"val": None, "date": "-"},
            "sd_mmol": rec["best_sd"] or {"val": None, "date": "-"},
            "sd": rec["best_sd"] or {"val": None, "date": "-"},
            "cv": rec["best_cv"] or {"val": None, "date": "-"},
            "gmi": rec["best_gmi"] or {"val": None, "date": "-"},
            "gri": rec["best_gri"] or {"val": None, "date": "-"},
            "lbgi": rec["best_lbgi"] or {"val": None, "date": "-"},
            "hbgi": rec["best_hbgi"] or {"val": None, "date": "-"},
            "gvi": rec["best_gvi"] or {"val": None, "date": "-"},
            "mag": rec["best_mag"] or {"val": None, "date": "-"}
        }
        res["records_365d"] = {
            "tir": rec["best_tir_365d"] or {"val": None, "date": "-"},
            "titr": rec["best_titr_365d"] or {"val": None, "date": "-"},
            "tatr": rec["best_tatr_365d"] or {"val": None, "date": "-"},
            "tar": rec["best_tar_365d"] or {"val": None, "date": "-"},
            "tbr": rec["best_tbr_365d"] or {"val": None, "date": "-"},
            "vlow": rec["best_vlow_365d"] or {"val": None, "date": "-"},
            "vhigh": rec["best_vhigh_365d"] or {"val": None, "date": "-"},
            "mean_mmol": rec["best_mean_365d"] or {"val": None, "date": "-"},
            "avg_bg": rec["best_mean_365d"] or {"val": None, "date": "-"},
            "sd_mmol": rec["best_sd_365d"] or {"val": None, "date": "-"},
            "sd": rec["best_sd_365d"] or {"val": None, "date": "-"},
            "cv": rec["best_cv_365d"] or {"val": None, "date": "-"},
            "gmi": rec["best_gmi_365d"] or {"val": None, "date": "-"},
            "gri": rec["best_gri_365d"] or {"val": None, "date": "-"},
            "lbgi": rec["best_lbgi_365d"] or {"val": None, "date": "-"},
            "hbgi": rec["best_hbgi_365d"] or {"val": None, "date": "-"},
            "gvi": rec["best_gvi_365d"] or {"val": None, "date": "-"},
            "mag": rec["best_mag_365d"] or {"val": None, "date": "-"}
        }


def _fetch_hypo_streaks(cur, res: Dict[str, Any]) -> None:
    # 3. Hypo Streaks: Severe (< 3.0 mmol/L) and Any Hypo (< 3.9 mmol/L)
    cur.execute("""
        WITH numbered AS (
            SELECT date, vlow,
                   SUM(CASE WHEN vlow > 0 THEN 1 ELSE 0 END) OVER (ORDER BY date DESC) as grp
            FROM layer2_daily_risk_stats
        )
        SELECT COUNT(*) AS streak_days
        FROM numbered
        WHERE grp = 0
    """)
    streak_severe_row = cur.fetchone()

    cur.execute("""
        SELECT to_char(date, 'DD Mon YYYY') AS last_event
        FROM layer2_daily_risk_stats
        WHERE vlow > 0
        ORDER BY date DESC
        LIMIT 1
    """)
    last_severe_row = cur.fetchone()

    cur.execute("""
        WITH numbered AS (
            SELECT date, tbr,
                   SUM(CASE WHEN tbr > 0 THEN 1 ELSE 0 END) OVER (ORDER BY date DESC) as grp
            FROM layer2_daily_risk_stats
        )
        SELECT COUNT(*) AS streak_days
        FROM numbered
        WHERE grp = 0
    """)
    streak_any_row = cur.fetchone()

    cur.execute("""
        SELECT to_char(date, 'DD Mon YYYY') AS last_event
        FROM layer2_daily_risk_stats
        WHERE tbr > 0
        ORDER BY date DESC
        LIMIT 1
    """)
    last_any_row = cur.fetchone()

    res["streak"] = {
        "days": streak_severe_row["streak_days"] if streak_severe_row else 0,
        "last_date": last_severe_row["last_event"] if last_severe_row else "None recorded"
    }
    res["streak_severe"] = res["streak"]
    res["streak_any"] = {
        "days": streak_any_row["streak_days"] if streak_any_row else 0,
        "last_date": last_any_row["last_event"] if last_any_row else "None recorded"
    }


def _fetch_recent_notes(cur, res: Dict[str, Any]) -> None:
    # 4. Recent Clinical Notes (last 10 from Diary Studio clinical_notes)
    cur.execute("""
        SELECT id, date, time_created, created_at,
               COALESCE(NULLIF(trim(title), ''), NULLIF(trim(regexp_replace(text_content, '<[^>]*>', '', 'g')), ''), 'Clinical Note') AS note_title
        FROM clinical_notes
        ORDER BY date DESC, id DESC
        LIMIT 10
    """)
    notes_rows = cur.fetchall()
    if notes_rows:
        for nr in notes_rows:
            ts_val = nr["time_created"] or nr["created_at"] or nr["date"]
            date_str = str(nr["date"]) if nr.get("date") else ""
            res["recent_notes"].append({
                "id": nr["id"],
                "ts": ts_val.isoformat() if hasattr(ts_val, 'isoformat') else str(ts_val),
                "date_str": date_str,
                "notes": nr["note_title"]
            })
    else:
        # Fallback to device treatments notes
        cur.execute("""
            SELECT ts, notes, event_type
            FROM treatments
            WHERE (notes IS NOT NULL AND trim(notes) != '')
               OR event_type = 'Note'
            ORDER BY ts DESC
            LIMIT 10
        """)
        for nr in cur.fetchall():
            ts_val = nr["ts"]
            res["recent_notes"].append({
                "id": None,
                "ts": ts_val.isoformat() if hasattr(ts_val, 'isoformat') else str(ts_val),
                "date_str": "",
                "notes": nr["notes"] or nr["event_type"] or "Note"
            })


def _fetch_device_health(cur, res: Dict[str, Any], tz_name: str, tz_obj: Any, now_local: datetime) -> None:
    # Pod change
    cur.execute("""
        SELECT ts FROM treatments 
        WHERE event_type LIKE '%Site%' OR event_type LIKE '%Pod%' 
        ORDER BY ts DESC LIMIT 1
    """)
    pod_row = cur.fetchone()
    pod_data = {
        "last_ts": None,
        "age_hours": None,
        "remaining_80h_hours": None,
        "remaining_72h_hours": None,
        "expiry_80h_label": None,
        "is_red": False
    }
    if pod_row and pod_row[0]:
        pod_ts = pod_row[0]
        if pod_ts.tzinfo is None:
            pod_ts = pod_ts.replace(tzinfo=timezone.utc)
        pod_ts_local = pod_ts.astimezone(tz_obj)
        pod_data["last_ts"] = pod_ts.isoformat()
        
        age_sec = (now_local - pod_ts_local).total_seconds()
        age_hours = round(max(0.0, age_sec / 3600.0), 1)
        pod_data["age_hours"] = age_hours
        
        cutoff_80h = pod_ts_local + timedelta(hours=80)
        cutoff_72h = pod_ts_local + timedelta(hours=72)
        rem_80h = (cutoff_80h - now_local).total_seconds() / 3600.0
        rem_72h = (cutoff_72h - now_local).total_seconds() / 3600.0
        pod_data["remaining_80h_hours"] = round(rem_80h, 1)
        pod_data["remaining_72h_hours"] = round(rem_72h, 1)

        if cutoff_80h.date() == now_local.date():
            time_str = cutoff_80h.strftime("%H:%M")
            pod_data["expiry_80h_label"] = f"Today {time_str}"
        elif cutoff_80h.date() == (now_local + timedelta(days=1)).date():
            time_str = cutoff_80h.strftime("%H:%M")
            pod_data["expiry_80h_label"] = f"Tomorrow {time_str}"
        else:
            pod_data["expiry_80h_label"] = cutoff_80h.strftime("%a %d %b %H:%M")

        # Red warning logic: Quiet hours 21:00 to 09:00
        if rem_80h <= 0:
            pod_data["is_red"] = True
        elif cutoff_80h.date() == now_local.date() and cutoff_80h.hour < 21:
            pod_data["is_red"] = True
        else:
            quiet_start = datetime(now_local.year, now_local.month, now_local.day, 21, 0, tzinfo=tz_obj)
            quiet_end = quiet_start + timedelta(hours=12)
            if quiet_start <= cutoff_80h <= quiet_end:
                pod_data["is_red"] = (now_local.hour >= 9)
            else:
                pod_data["is_red"] = False

    # Sensor change
    cur.execute("""
        SELECT ts FROM treatments 
        WHERE event_type LIKE '%Sensor%' 
        ORDER BY ts DESC LIMIT 1
    """)
    sensor_row = cur.fetchone()
    sensor_data = {
        "last_ts": None,
        "age_days": None,
        "remaining_10d_days": None,
        "expiry_label": None
    }
    if sensor_row and sensor_row[0]:
        sensor_ts = sensor_row[0]
        if sensor_ts.tzinfo is None:
            sensor_ts = sensor_ts.replace(tzinfo=timezone.utc)
        sensor_ts_local = sensor_ts.astimezone(tz_obj)
        sensor_data["last_ts"] = sensor_ts.isoformat()
        
        s_age_sec = (now_local - sensor_ts_local).total_seconds()
        s_age_days = round(max(0.0, s_age_sec / 86400.0), 1)
        sensor_data["age_days"] = s_age_days
        sensor_data["remaining_10d_days"] = round(10.0 - s_age_days, 1)

        cutoff_10d = sensor_ts_local + timedelta(days=10)
        if cutoff_10d.date() == now_local.date():
            s_time_str = cutoff_10d.strftime("%H:%M")
            sensor_data["expiry_label"] = f"Today {s_time_str}"
        elif cutoff_10d.date() == (now_local + timedelta(days=1)).date():
            s_time_str = cutoff_10d.strftime("%H:%M")
            sensor_data["expiry_label"] = f"Tomorrow {s_time_str}"
        else:
            sensor_data["expiry_label"] = cutoff_10d.strftime("%a %d %b %H:%M")

    # Hardware pump status & telemetry (PHASE B FIX)
    # devicestatus has no status_json/pump_battery/uploader_battery/reservoir columns.
    # Hardware data lives in raw_json (jsonb). autosens_ratio is a dedicated column.
    cur.execute("""
        SELECT ts, autosens_ratio, raw_json
        FROM devicestatus
        ORDER BY ts DESC LIMIT 1
    """)
    ds_row = cur.fetchone()
    hardware_data = {
        "pump_status": "Unknown",
        "pump_battery": {},
        "uploader_battery": None,
        "reservoir": None,
        "is_reservoir_floor": False,
        "autosens_ratio": None
    }
    if ds_row:
        rj = ds_row["raw_json"] or {}
        if not isinstance(rj, dict):
            rj = {}

        # Pump data from raw_json -> pump
        pump = rj.get("pump", {}) or {}
        if isinstance(pump, dict):
            # Reservoir
            reservoir_val = pump.get("reservoir")
            if reservoir_val is not None:
                try:
                    hardware_data["reservoir"] = float(reservoir_val)
                    hardware_data["is_reservoir_floor"] = (hardware_data["reservoir"] <= 50.0)
                except (ValueError, TypeError):
                    pass

            # Pump battery (jsonb object — may be empty {} for Omnipod)
            pump_bat = pump.get("battery")
            if pump_bat and isinstance(pump_bat, dict):
                hardware_data["pump_battery"] = pump_bat

            # Pump status
            pump_status = pump.get("status", {})
            if isinstance(pump_status, dict):
                hardware_data["pump_status"] = pump_status.get("status", "Unknown")
            elif isinstance(pump_status, str):
                hardware_data["pump_status"] = pump_status

        # Uploader battery from raw_json -> uploader -> battery
        uploader = rj.get("uploader", {}) or {}
        if isinstance(uploader, dict):
            up_bat = uploader.get("battery")
            if up_bat is not None:
                try:
                    hardware_data["uploader_battery"] = int(up_bat)
                except (ValueError, TypeError):
                    pass

        # Autosens ratio — use dedicated column
        if ds_row["autosens_ratio"] is not None:
            try:
                hardware_data["autosens_ratio"] = round(float(ds_row["autosens_ratio"]), 4)
            except (ValueError, TypeError):
                pass

    # SMB Micro-bolus activity today
    cur.execute("""
        SELECT COUNT(*) as smb_count, COALESCE(SUM(carbs), 0) as total_carbs, COALESCE(SUM(insulin), 0) as total_insulin
        FROM treatments
        WHERE (ts AT TIME ZONE %s)::date = %s
          AND (event_type = 'SMB' OR smb_flag = true OR notes LIKE '%%SMB%%')
    """, (tz_name, now_local.date()))
    smb_row = cur.fetchone()
    smb_data = {
        "count": int(smb_row["smb_count"]) if smb_row else 0,
        "units": round(float(smb_row["total_insulin"]), 2) if smb_row and smb_row["total_insulin"] else 0.0
    }

    res["device_health"] = {
        "pod": pod_data,
        "sensor": sensor_data,
        "hardware": hardware_data,
        "smb": smb_data
    }


def _fetch_dosing_data(cur, res: Dict[str, Any], tz_name: str, now_local: datetime) -> None:
    # 6. Dosing Breakdown Today (Basal vs Bolus U & %)
    cur.execute("""
        SELECT 
            COALESCE(SUM(CASE WHEN event_type IN ('Correction Bolus', 'Meal Bolus', 'Bolus', 'SMB') OR insulin > 0 THEN insulin ELSE 0 END), 0) as bolus_units
        FROM treatments
        WHERE (ts AT TIME ZONE %s)::date = %s
    """, (tz_name, now_local.date()))
    bolus_row = cur.fetchone()
    bolus_today = round(float(bolus_row["bolus_units"]), 1) if bolus_row and bolus_row["bolus_units"] else 0.0

    # Basal units estimate from layer2 or calculated
    cur.execute("""
        SELECT tdd
        FROM layer2_daily_risk_stats
        WHERE date = %s
    """, (now_local.date(),))
    daily_dosing_row = cur.fetchone()
    tdd_today = float(daily_dosing_row["tdd"]) if (daily_dosing_row and daily_dosing_row["tdd"]) else None
    if tdd_today is not None:
        basal_today = round(max(0.0, tdd_today - bolus_today), 1)
    else:
        # Fallback estimate from layer2 14d basal average
        cur.execute("SELECT round(avg(tdd), 1) as tdd_14d FROM layer2_daily_risk_stats WHERE date >= %s - INTERVAL '14 days'", (now_local.date(),))
        t14 = cur.fetchone()
        tdd_today = float(t14["tdd_14d"]) if (t14 and t14["tdd_14d"]) else 50.0
        basal_today = round(max(0.0, tdd_today - bolus_today), 1)

    total_dosing = basal_today + bolus_today
    basal_pct = round((basal_today / total_dosing) * 100) if total_dosing > 0 else 50
    bolus_pct = 100 - basal_pct if total_dosing > 0 else 50

    smb_info = res.get("device_health", {}).get("smb", {})
    hardware_info = res.get("device_health", {}).get("hardware", {})

    res["dosing"] = {
        "tdd_today": tdd_today,
        "tdd_yesterday": float(res["yesterday"]["tdd"]) if res.get("yesterday") and res["yesterday"].get("tdd") else None,
        "tdd_14d": float(res["rolling"]["tdd_14d"]) if res.get("rolling") and res["rolling"].get("tdd_14d") else None,
        "basal_today": basal_today,
        "bolus_today": bolus_today,
        "basal_pct": basal_pct,
        "bolus_pct": bolus_pct,
        "smb_count": smb_info.get("count", 0),
        "smb_units": smb_info.get("units", 0.0),
        "autosens_ratio": hardware_info.get("autosens_ratio")
    }


def _fetch_ghost_curve(cur, res: Dict[str, Any], tz_name: str, now_local: datetime) -> None:
    # 7. Today vs Baseline Ghost Curve Points
    cur.execute("""
        SELECT 
            EXTRACT(epoch FROM (ts - (date_trunc('day', ts AT TIME ZONE %s) AT TIME ZONE %s))) / 60.0 AS min_of_day,
            round(sg / 18.0182, 1) AS sg_mmol
        FROM cgm_readings
        WHERE ts >= (date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s)
        ORDER BY ts ASC
    """, (tz_name, tz_name, tz_name, tz_name))
    today_cgm_rows = cur.fetchall()
    ghost_today_points = [
        {"min": round(float(r["min_of_day"])), "sg": float(r["sg_mmol"])}
        for r in today_cgm_rows if r["sg_mmol"] is not None
    ]

    def _fetch_hourly_medians(start_d, end_d):
        if not start_d or not end_d:
            return []
        cur.execute("""
            SELECT 
                EXTRACT(hour FROM (ts AT TIME ZONE %s))::int AS hr,
                round(percentile_cont(0.5) WITHIN GROUP (ORDER BY (sg / 18.0182))::numeric, 1) AS median_sg
            FROM cgm_readings
            WHERE (ts AT TIME ZONE %s)::date BETWEEN %s AND %s
            GROUP BY hr
            ORDER BY hr ASC
        """, (tz_name, tz_name, start_d, end_d))
        return [{"hour": int(b_row["hr"]), "median_sg": float(b_row["median_sg"])} for b_row in cur.fetchall()]

    # All-Time Best 14d 24-hour hourly medians
    cur.execute("SELECT date FROM layer2_daily_risk_stats WHERE tir_14d IS NOT NULL ORDER BY tir_14d DESC LIMIT 1")
    best_row = cur.fetchone()
    best_14d_medians = []
    if best_row and best_row["date"]:
        best_14d_medians = _fetch_hourly_medians(best_row["date"] - timedelta(days=13), best_row["date"])

    # Last 365d Best 14d 24-hour hourly medians
    cur.execute("SELECT date FROM layer2_daily_risk_stats WHERE tir_14d IS NOT NULL AND date >= CURRENT_DATE - INTERVAL '365 days' ORDER BY tir_14d DESC LIMIT 1")
    best_365_row = cur.fetchone()
    best_365d_medians = []
    if best_365_row and best_365_row["date"]:
        best_365d_medians = _fetch_hourly_medians(best_365_row["date"] - timedelta(days=13), best_365_row["date"])

    # Trailing 7d, 14d, 30d hourly medians
    today_date = now_local.date()
    trailing_7d_medians = _fetch_hourly_medians(today_date - timedelta(days=7), today_date - timedelta(days=1))
    trailing_14d_medians = _fetch_hourly_medians(today_date - timedelta(days=14), today_date - timedelta(days=1))
    trailing_30d_medians = _fetch_hourly_medians(today_date - timedelta(days=30), today_date - timedelta(days=1))

    res["ghost_curve"] = {
        "today_points": ghost_today_points,
        "best_14d": best_14d_medians,
        "best_365d": best_365d_medians,
        "trailing_7d": trailing_7d_medians,
        "trailing_14d": trailing_14d_medians,
        "trailing_30d": trailing_30d_medians,
        "benchmark_medians": best_14d_medians,
        "target_low": 3.9,
        "target_high": 10.0
    }


def get_dashboard_scorecard_data(conn) -> Dict[str, Any]:
    """
    Unified, fault-tolerant aggregator for dashboard scorecards and triage tables.
    Each domain is independently error-isolated so a single query failure
    (e.g. missing column, NULL edge case) does not crash the entire endpoint.
    """
    res: Dict[str, Any] = {
        "today": {},
        "yesterday": {},
        "rolling": {},
        "records": {},
        "records_365d": {},
        "streak": {"days": 0, "last_date": "None recorded"},
        "streak_severe": {"days": 0, "last_date": "None recorded"},
        "streak_any": {"days": 0, "last_date": "None recorded"},
        "telemetry": {},
        "recent_notes": [],
        "device_health": {
            "pod": {
                "last_ts": None,
                "age_hours": None,
                "remaining_80h_hours": None,
                "remaining_72h_hours": None,
                "expiry_80h_label": None,
                "is_red": False
            },
            "sensor": {
                "last_ts": None,
                "age_days": None,
                "remaining_10d_days": None,
                "expiry_label": None
            },
            "hardware": {
                "pump_status": "Unknown",
                "pump_battery": {},
                "uploader_battery": None,
                "reservoir": None,
                "is_reservoir_floor": False,
                "autosens_ratio": None
            },
            "smb": {
                "count": 0,
                "units": 0.0
            }
        },
        "dosing": {
            "tdd_today": None,
            "tdd_yesterday": None,
            "tdd_14d": None,
            "basal_today": 0,
            "bolus_today": 0,
            "basal_pct": 50,
            "bolus_pct": 50,
            "smb_count": 0,
            "smb_units": 0.0,
            "autosens_ratio": None
        },
        "ghost_curve": {
            "today_points": [],
            "best_14d": [],
            "best_365d": [],
            "trailing_7d": [],
            "trailing_14d": [],
            "trailing_30d": [],
            "benchmark_medians": [],
            "target_low": 3.9,
            "target_high": 10.0
        }
    }

    tz_name = "UTC"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1")
            tz_row = cur.fetchone()
            if tz_row and tz_row[0]:
                tz_name = tz_row[0]
    except Exception as e:
        logger.warning("Dashboard aggregator: timezone lookup failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    try:
        from zoneinfo import ZoneInfo
        tz_obj = ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone as dt_tz
        tz_obj = dt_tz.utc

    now_local = datetime.now(tz_obj)

    # Domain 1: Glycemic stats (today, yesterday, rolling, 365d aggregates)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_glycemic_stats(cur, res)
    except Exception as e:
        logger.warning("Dashboard aggregator: glycemic_stats failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 2: All-time & 365d best records
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_best_records(cur, res)
    except Exception as e:
        logger.warning("Dashboard aggregator: best_records failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 3: Hypo streaks
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_hypo_streaks(cur, res)
    except Exception as e:
        logger.warning("Dashboard aggregator: hypo_streaks failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 4: Recent diary notes
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_recent_notes(cur, res)
    except Exception as e:
        logger.warning("Dashboard aggregator: recent_notes failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 5: Device health & hardware (pod, sensor, hardware telemetry)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_device_health(cur, res, tz_name, tz_obj, now_local)
    except Exception as e:
        logger.warning("Dashboard aggregator: device_health failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 6: Dosing breakdown (TDD, basal/bolus split, SMB)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_dosing_data(cur, res, tz_name, now_local)
    except Exception as e:
        logger.warning("Dashboard aggregator: dosing failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 7: Ghost curve points (today CGM + baseline medians)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _fetch_ghost_curve(cur, res, tz_name, now_local)
    except Exception as e:
        logger.warning("Dashboard aggregator: ghost_curve failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # Domain 8: Live telemetry (already has its own try/except)
    try:
        tel = get_latest_metrics(conn)
        if tel.get("sg") is not None:
            tel["sg_mmol"] = round(float(tel["sg"]) / 18.0182, 1)
        if tel.get("delta") is not None:
            tel["delta_mmol"] = round(float(tel["delta"]) / 18.0182, 1)
        res["telemetry"] = tel
    except Exception as te:
        logger.warning("Dashboard aggregator: live telemetry failed: %s", te)
        res["telemetry"] = {"error": str(te)}

    return res



def get_periodicity_daily_records(conn, start_date_str, end_date_str):
    """Calendar-complete daily data for the periodicity explorer."""
    query = """
        SELECT TO_CHAR(days.day, 'YYYY-MM-DD') AS date,
               r.mean_mmol AS mean, r.sd_mmol AS sd, r.tir, r.titr, r.tdd
        FROM generate_series(%s::date, %s::date, interval '1 day') AS days(day)
        LEFT JOIN layer2_daily_risk_stats r ON r.date = days.day::date
        ORDER BY days.day ASC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (start_date_str, end_date_str))
        rows = []
        for row in cur.fetchall():
            item = {k: (v if k == 'date' else (float(v) if v is not None else None)) for k, v in row.items()}
            mean, sd = item['mean'], item['sd']
            item['cv'] = round(100 * sd / mean, 2) if mean and mean > 0 and sd is not None else None
            rows.append(item)
        return rows


def get_hba1c_overlay_data(conn, start_date_str, end_date_str):
    query = """
        SELECT 
            TO_CHAR(d."date", 'YYYY-MM-DD') as date,
            r.gmi_90d as gmi_90d,
            r.mean_mmol_90d as mean_mmol_90d,
            ROUND(((r.mean_mmol_90d + 2.59) / 1.59)::numeric, 1) as ehba1c_90d
        FROM layer2_daily_band_stats d
        LEFT JOIN layer2_daily_risk_stats r ON d."date" = r."date"
        WHERE d."date" BETWEEN %s AND %s
          AND MOD('2000-01-01'::DATE - d."date", 7) = 0
        ORDER BY d."date" ASC
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (start_date_str, end_date_str))
            rows = cur.fetchall()
            for r in rows:
                if r['gmi_90d'] is not None: r['gmi_90d'] = float(r['gmi_90d'])
                if r['mean_mmol_90d'] is not None: r['mean_mmol_90d'] = float(r['mean_mmol_90d'])
                if r['ehba1c_90d'] is not None: r['ehba1c_90d'] = float(r['ehba1c_90d'])
            return rows
    except Exception as e:
        print(f"[db] get_hba1c_overlay_data failed: {e}")
        conn.rollback()
        return []
