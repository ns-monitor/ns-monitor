"""
daemon/basal_eval.py — 24-Hour Modal Basal Evaluation Data Service

Extracts continuous 5-minute buckets across an arbitrary [start_date, end_date]
window with a 6-hour warmup prefix, runs AAPS stateful deviation classification
(_classify_deviations), merges treatment bolus events (meal vs SMB), and packages
the raw bucket array for client-side non-parametric modal evaluation.
"""

import logging
import psycopg2.extras
from datetime import datetime, date

logger = logging.getLogger("basal_eval")

DEV_WARMUP_HOURS = 6


def get_basal_eval_dataset(conn, start_date_str: str, end_date_str: str) -> dict:
    """
    Fetches 5-minute aggregate buckets and treatment boluses for [start_date, end_date].
    Applies a 6-hour warm-up prefix to bring stateful deviation classification up to date,
    then discards warm-up rows before returning.
    """
    try:
        # Import lazily to avoid circular dependencies
        try:
            from daemon.database import _classify_deviations
        except ImportError:
            from database import _classify_deviations

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH win AS (
                    SELECT ((%s::date)::timestamp
                            AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1)
                           ) AS start_ts,
                           (((%s::date + INTERVAL '1 day')::timestamp)
                            AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1)
                           ) AS end_ts
                ),
                treat AS (
                    SELECT
                        date_trunc('minute', ts) - (EXTRACT(MINUTE FROM ts)::int %% 5) * INTERVAL '1 minute' AS bucket,
                        COALESCE(SUM(CASE WHEN smb_flag = true OR (insulin > 0 AND event_type = 'Correction Bolus') THEN insulin ELSE 0 END), 0) AS smb_bolus,
                        COALESCE(SUM(CASE WHEN event_type IN ('Meal Bolus', 'Bolus Wizard') OR (carbs > 0 AND insulin > 0) THEN insulin ELSE 0 END), 0) AS meal_bolus
                    FROM treatments
                    WHERE ts >= (SELECT start_ts FROM win) - (%s * INTERVAL '1 hour')
                      AND ts <= (SELECT end_ts FROM win)
                    GROUP BY 1
                )
                SELECT
                    a.ts AT TIME ZONE 'UTC' as ts,
                    to_char(a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'YYYY-MM-DD HH24:MI:SS') as local_ts,
                    to_char(a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1), 'YYYY-MM-DD') as local_day,
                    (EXTRACT(HOUR FROM a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1))::int * 12 +
                     (EXTRACT(MINUTE FROM a.ts AT TIME ZONE (SELECT value FROM system_config WHERE key = 'TIMEZONE' LIMIT 1))::int / 5)) as time_bin,
                    a.bg, a.iob, a.cob, a.isf, a.deviation, a.bgi,
                    a.basal_rate, a.carbs,
                    COALESCE(t.meal_bolus, 0) as meal_bolus,
                    COALESCE(t.smb_bolus, 0) as smb_bolus,
                    a.scheduled_basal,
                    (a.bg_roc - a.bgi) * 18.0182 AS dev_mgdl,
                    -a.bgi * 18.0182            AS neg_bgi_mgdl,
                    (a.ts >= (SELECT start_ts FROM win)) AS in_window
                FROM layer2_five_minute_aggregate a
                LEFT JOIN treat t ON a.ts = t.bucket
                WHERE a.ts >= (SELECT start_ts FROM win) - (%s * INTERVAL '1 hour')
                  AND a.ts <= (SELECT end_ts FROM win)
                ORDER BY a.ts ASC
            """, (start_date_str, end_date_str, DEV_WARMUP_HOURS, DEV_WARMUP_HOURS))

            rows = cur.fetchall()

            # Run stateful AAPS classification across warmup + window
            _classify_deviations(rows)

            # Keep only rows inside the requested window
            clean_rows = []
            for r in rows:
                if not r.pop('in_window', True):
                    continue
                # Serialize Decimals and Datetimes for JSON emission
                clean_rows.append({
                    "ts": r['ts'].isoformat() if hasattr(r['ts'], 'isoformat') else str(r['ts']),
                    "local_ts": r['local_ts'],
                    "day": r['local_day'],
                    "time_bin": int(r['time_bin']),
                    "bg": float(r['bg']) if r['bg'] is not None else None,
                    "iob": float(r['iob']) if r['iob'] is not None else 0.0,
                    "cob": float(r['cob']) if r['cob'] is not None else 0.0,
                    "basal_rate": float(r['basal_rate']) if r['basal_rate'] is not None else 0.0,
                    "scheduled_basal": float(r['scheduled_basal']) if r['scheduled_basal'] is not None else 0.0,
                    "meal_bolus": float(r['meal_bolus']),
                    "smb_bolus": float(r['smb_bolus']),
                    "carbs": float(r['carbs']) if r['carbs'] is not None else 0.0,
                    "dev_mgdl": float(r['dev_mgdl']) if r['dev_mgdl'] is not None else None,
                    "dev_class": r.get('dev_class')
                })

            start_d = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            total_days = max(1, (end_d - start_d).days + 1)

            return {
                "start_date": start_date_str,
                "end_date": end_date_str,
                "total_days": total_days,
                "row_count": len(clean_rows),
                "rows": clean_rows
            }
    except Exception as e:
        logger.error(f"get_basal_eval_dataset failed: {e}", exc_info=True)
        conn.rollback()
        return {
            "start_date": start_date_str,
            "end_date": end_date_str,
            "total_days": 0,
            "row_count": 0,
            "rows": [],
            "error": str(e)
        }
