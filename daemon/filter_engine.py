"""
filter_engine.py — Dedicated Filter & Target Evaluation Engine for nightscout-monitor.

Implements Phase 5 specification (docs/analysis/phase5-filter-target-engine-spec.md).
Evaluates single Rules and 1-level AND/OR composite Filters across Daily, Continuous,
and Event metric kinds, returning portable calendar date sets (matchingDates).
"""

import time
import math
from datetime import datetime, date, timedelta
from typing import Dict, Any, Set, List, Tuple, Optional
import psycopg2
import psycopg2.extras

import database

# ---------------------------------------------------------------------------
# Declarative Metric Catalog (§5)
# ---------------------------------------------------------------------------
FILTER_METRICS: Dict[str, Dict[str, Any]] = {
    "tir": {
        "kind": "daily",
        "label": "TIR (3.9-10.0)",
        "unit": "%",
        "table": "layer2_daily_risk_stats",
        "column": "tir",
        "decimals": 1,
        "default_comparator": "gte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 70.0,
        "min": 0.0,
        "max": 100.0,
        "step": 1.0,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "titr": {
        "kind": "daily",
        "label": "TITR (3.9-7.8)",
        "unit": "%",
        "table": "layer2_daily_risk_stats",
        "column": "titr",
        "decimals": 1,
        "default_comparator": "gte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 50.0,
        "min": 0.0,
        "max": 100.0,
        "step": 1.0,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "tar": {
        "kind": "daily",
        "label": "TAR (>10.0)",
        "unit": "%",
        "table": "layer2_daily_risk_stats",
        "column": "tar",
        "decimals": 1,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 25.0,
        "min": 0.0,
        "max": 100.0,
        "step": 1.0,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "tbr": {
        "kind": "daily",
        "label": "TBR (<3.9)",
        "unit": "%",
        "table": "layer2_daily_risk_stats",
        "column": "tbr",
        "decimals": 1,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 4.0,
        "min": 0.0,
        "max": 100.0,
        "step": 0.5,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "avg_bg": {
        "kind": "daily",
        "label": "Avg BG",
        "unit": "mmol/L",
        "table": "layer2_daily_risk_stats",
        "column": "mean_mmol",
        "decimals": 1,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 7.0,
        "min": 2.0,
        "max": 25.0,
        "step": 0.1,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "cv": {
        "kind": "daily",
        "label": "CV",
        "unit": "%",
        "table": "layer2_daily_band_stats",
        "column": "cv",
        "decimals": 1,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 36.0,
        "min": 0.0,
        "max": 100.0,
        "step": 0.5,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "gmi": {
        "kind": "daily",
        "label": "GMI",
        "unit": "%",
        "table": "layer2_daily_band_stats",
        "column": "day_gmi_percent",
        "decimals": 1,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 6.5,
        "min": 4.0,
        "max": 15.0,
        "step": 0.1,
        "duration_units": ["days", "weeks"],
        "supports_timescope": True,
    },
    "tdd": {
        "kind": "daily",
        "label": "TDD",
        "unit": "U",
        "table": "layer2_daily_band_stats",
        "column": "tdd",
        "decimals": 1,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 50.0,
        "min": 0.0,
        "max": 200.0,
        "step": 0.5,
        "duration_units": ["days", "weeks"],
        "supports_timescope": False,
    },
    "carbs": {
        "kind": "daily",
        "label": "Carbs",
        "unit": "g",
        "table": "layer2_daily_band_stats",
        "column": "carbs",
        "decimals": 0,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 150.0,
        "min": 0.0,
        "max": 500.0,
        "step": 5.0,
        "duration_units": ["days", "weeks"],
        "supports_timescope": False,
    },
    "lbgi": {
        "kind": "daily",
        "label": "LBGI",
        "unit": "index",
        "table": "layer2_daily_risk_stats",
        "column": "lbgi",
        "decimals": 2,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 2.5,
        "min": 0.0,
        "max": 20.0,
        "step": 0.1,
        "duration_units": ["days", "weeks"],
        "supports_timescope": False,
    },
    "hbgi": {
        "kind": "daily",
        "label": "HBGI",
        "unit": "index",
        "table": "layer2_daily_risk_stats",
        "column": "hbgi",
        "decimals": 2,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 4.5,
        "min": 0.0,
        "max": 40.0,
        "step": 0.1,
        "duration_units": ["days", "weeks"],
        "supports_timescope": False,
    },
    "gvi": {
        "kind": "daily",
        "label": "GVI",
        "unit": "index",
        "table": "layer2_daily_risk_stats",
        "column": "gvi",
        "decimals": 2,
        "default_comparator": "lte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 1.2,
        "min": 1.0,
        "max": 5.0,
        "step": 0.05,
        "duration_units": ["days", "weeks"],
        "supports_timescope": False,
    },
    "bg": {
        "kind": "continuous",
        "label": "BG",
        "unit": "mmol/L",
        "table": "layer2_five_minute_aggregate",
        "column": "bg",
        "decimals": 1,
        "default_comparator": "lt",
        "allowed_comparators": ["gt", "gte", "lt", "lte"],
        "default_threshold": 3.9,
        "min": 2.0,
        "max": 25.0,
        "step": 0.1,
        "duration_units": ["minutes", "hours"],
        "supports_timescope": True,
    },
    "iob": {
        "kind": "continuous",
        "label": "IOB",
        "unit": "U",
        "table": "layer2_five_minute_aggregate",
        "column": "iob",
        "decimals": 1,
        "default_comparator": "gt",
        "allowed_comparators": ["gt", "gte", "lt", "lte"],
        "default_threshold": 3.0,
        "min": 0.0,
        "max": 30.0,
        "step": 0.2,
        "duration_units": ["minutes", "hours"],
        "supports_timescope": True,
    },
    "cob": {
        "kind": "continuous",
        "label": "COB",
        "unit": "g",
        "table": "layer2_five_minute_aggregate",
        "column": "cob",
        "decimals": 0,
        "default_comparator": "gt",
        "allowed_comparators": ["gt", "gte", "lt", "lte"],
        "default_threshold": 30.0,
        "min": 0.0,
        "max": 150.0,
        "step": 5.0,
        "duration_units": ["minutes", "hours"],
        "supports_timescope": True,
    },
    "basal_rate": {
        "kind": "continuous",
        "label": "Basal",
        "unit": "U/hr",
        "table": "layer2_five_minute_aggregate",
        "column": "basal_rate",
        "decimals": 2,
        "default_comparator": "gt",
        "allowed_comparators": ["gt", "gte", "lt", "lte"],
        "default_threshold": 1.0,
        "min": 0.0,
        "max": 5.0,
        "step": 0.05,
        "duration_units": ["minutes", "hours"],
        "supports_timescope": True,
    },
    "deviation": {
        "kind": "continuous",
        "label": "Deviation",
        "unit": "mmol/L",
        "table": "layer2_five_minute_aggregate",
        "column": "deviation",
        "decimals": 1,
        "default_comparator": "lt",
        "allowed_comparators": ["gt", "gte", "lt", "lte"],
        "default_threshold": -1.0,
        "min": -10.0,
        "max": 10.0,
        "step": 0.1,
        "duration_units": ["minutes", "hours"],
        "supports_timescope": True,
    },
    "pod_change": {
        "kind": "event",
        "label": "Pod Change",
        "unit": "events",
        "table": "site_changes",
        "column": "ts",
        "decimals": 0,
        "default_comparator": "occurred",
        "allowed_comparators": ["occurred"],
        "default_threshold": 1,
        "min": 1,
        "max": 1,
        "step": 1,
        "duration_units": [],
        "supports_timescope": True,
    },
    "low_count": {
        "kind": "event",
        "label": "Hypo Events",
        "unit": "events",
        "table": "layer2_daily_risk_stats",
        "column": "episode_count",
        "decimals": 0,
        "default_comparator": "gte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 1,
        "min": 0,
        "max": 20,
        "step": 1,
        "duration_units": [],
        "supports_timescope": True,
    },
    "warning_count": {
        "kind": "event",
        "label": "Warning Events",
        "unit": "readings",
        "table": "layer2_five_minute_aggregate",
        "column": "bg",
        "decimals": 0,
        "default_comparator": "gte",
        "allowed_comparators": ["gt", "gte", "lt", "lte", "eq"],
        "default_threshold": 1,
        "min": 0,
        "max": 288,
        "step": 1,
        "duration_units": [],
        "supports_timescope": True,
    },
}

COMPARATOR_OPERATORS: Dict[str, str] = {
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "eq": "=",
}

# ---------------------------------------------------------------------------
# Whitelist Validation & Gating (§5)
# ---------------------------------------------------------------------------
def validate_filter_payload(payload: Dict[str, Any]) -> None:
    """Validate incoming filter and date range payload against strict whitelists.
    Raises ValueError on any invalid or malformed field.
    """
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")

    date_range = payload.get("dateRange")
    if not date_range or not isinstance(date_range, dict):
        raise ValueError("dateRange object required with start and end")

    start = date_range.get("start")
    end = date_range.get("end")
    if not start or not end:
        raise ValueError("dateRange must specify start and end dates (YYYY-MM-DD)")

    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
        if start_d > end_d:
            raise ValueError("start date cannot be after end date")
    except ValueError as e:
        raise ValueError(f"Invalid date in dateRange: {e}")

    filter_tree = payload.get("filter")
    if not filter_tree or not isinstance(filter_tree, dict):
        raise ValueError("filter object required")

    _validate_filter_node(filter_tree)


def _validate_filter_node(node: Dict[str, Any], depth: int = 1) -> None:
    """Recursively validate a composite group node or leaf rule node.

    `depth` counts levels of AND/OR group nesting -- the top-level filter,
    if itself a group, is depth 1. Per spec §3/§4: one level of nesting
    only (a group's clauses may contain plain Rules or exactly one more
    nested group, never a group nested inside a group nested inside a
    group). Enforced here, not just documented -- added 29 Aug 2026 after
    grounded review found the recursion had no depth limit.
    """
    if "clauses" in node:
        if depth > 2:
            raise ValueError(
                "Filter groups may nest at most one level deep (a "
                "top-level AND/OR group containing plain Rules or exactly "
                "one more nested AND/OR group) -- this payload nests deeper."
            )
        op = node.get("op", "AND").upper()
        if op not in ("AND", "OR"):
            raise ValueError(f"Unsupported group operator: {op}. Must be AND or OR.")
        clauses = node.get("clauses")
        if not isinstance(clauses, list) or len(clauses) == 0:
            raise ValueError("Group node must contain a non-empty clauses list")
        for clause in clauses:
            _validate_filter_node(clause, depth=depth + 1)
    else:
        # Value Overlay or Leaf rule
        if node.get("kind") == "value":
            metric_id = node.get("metric")
            if metric_id not in FILTER_METRICS:
                raise ValueError(f"Unknown metric: {metric_id}. Whitelisted keys: {list(FILTER_METRICS.keys())}")
            cfg = FILTER_METRICS[metric_id]
            if cfg.get("kind") == "event":
                raise ValueError(f"Event metric '{metric_id}' cannot be a Value overlay")
            window_type = node.get("window_type", "single_day")
            if window_type not in ("single_day", "rolling"):
                raise ValueError(f"Invalid window_type '{window_type}'. Must be 'single_day' or 'rolling'")
            if window_type == "rolling":
                rolling_days = node.get("rolling_days")
                if not isinstance(rolling_days, int) or rolling_days <= 0:
                    raise ValueError(f"rolling_days must be a positive integer, got: {rolling_days}")
            ts = node.get("timeScope")
            if ts:
                if not isinstance(ts, dict):
                    raise ValueError("timeScope must be an object with start and end ('HH:MM')")
                s = ts.get("start", "")
                e = ts.get("end", "")
                if not s or not e:
                    raise ValueError("timeScope requires both start and end strings")
                try:
                    datetime.strptime(s, "%H:%M")
                    datetime.strptime(e, "%H:%M")
                except ValueError:
                    raise ValueError(f"Invalid time format in timeScope: {s} - {e}. Expected HH:MM")
            return

        metric_id = node.get("metric")
        if metric_id not in FILTER_METRICS:
            raise ValueError(f"Unknown metric: {metric_id}. Whitelisted keys: {list(FILTER_METRICS.keys())}")

        cfg = FILTER_METRICS[metric_id]
        comp = node.get("comparator", cfg["default_comparator"])
        if comp not in cfg["allowed_comparators"]:
            raise ValueError(
                f"Comparator '{comp}' not permitted for metric '{metric_id}'. "
                f"Allowed: {cfg['allowed_comparators']}"
            )

        threshold = node.get("threshold", cfg["default_threshold"])
        if not isinstance(threshold, (int, float)):
            raise ValueError(f"Threshold for metric '{metric_id}' must be numeric")

        # TimeScope validation
        ts = node.get("timeScope")
        if ts:
            if not isinstance(ts, dict):
                raise ValueError("timeScope must be an object with start and end ('HH:MM')")
            s = ts.get("start", "")
            e = ts.get("end", "")
            if not s or not e:
                raise ValueError("timeScope requires both start and end strings")
            try:
                datetime.strptime(s, "%H:%M")
                datetime.strptime(e, "%H:%M")
            except ValueError:
                raise ValueError(f"Invalid time format in timeScope: {s} - {e}. Expected HH:MM")

        # Duration validation
        dur = node.get("duration")
        if dur:
            if not isinstance(dur, dict):
                raise ValueError("duration must be an object with value and unit")
            val = dur.get("value")
            unit = dur.get("unit")
            if not isinstance(val, (int, float)) or val <= 0:
                raise ValueError(f"duration value must be positive number: {val}")
            if unit not in cfg["duration_units"]:
                raise ValueError(
                    f"duration unit '{unit}' not allowed for metric '{metric_id}'. "
                    f"Allowed: {cfg['duration_units']}"
                )

        # Preceding lookback validation
        prec = node.get("preceding")
        if prec:
            if not isinstance(prec, dict):
                raise ValueError("preceding must be an object with value and unit")
            pval = prec.get("value")
            punit = prec.get("unit")
            if not isinstance(pval, (int, float)) or pval <= 0:
                raise ValueError(f"preceding value must be positive number: {pval}")
            if punit not in ("minutes", "hours"):
                raise ValueError(f"preceding unit must be 'minutes' or 'hours', got '{punit}'")


def validate_target_rule(rule: Dict[str, Any]) -> None:
    """Validate a single leaf rule intended for a Target.
    Targets are single-rule only; composite groups ('clauses') are rejected.
    Reuses _validate_filter_node() directly for leaf validation.
    """
    if not isinstance(rule, dict):
        raise ValueError("Target rule must be a JSON object")
    if "clauses" in rule:
        raise ValueError("Target rule must be a single leaf rule, not a composite group")
    _validate_filter_node(rule)


def validate_filter_tree(tree: Dict[str, Any]) -> None:
    """Validate a filter tree (leaf rule or 1-level AND/OR group) against
    strict whitelists and depth limits without requiring dateRange.
    Raises ValueError on invalid metric, comparator, threshold, or depth > 2.
    """
    if not isinstance(tree, dict):
        raise ValueError("Filter tree must be a JSON object")
    _validate_filter_node(tree, depth=1)



# ---------------------------------------------------------------------------
# SQL Clause Builders
# ---------------------------------------------------------------------------
def _tz_sql() -> str:
    """Return SQL fragment to fetch local timezone from system_config."""
    return "(SELECT COALESCE(NULLIF(value, ''), 'UTC') FROM system_config WHERE key = 'TIMEZONE' LIMIT 1)"


def build_timescope_clause(time_scope: Optional[Dict[str, str]],
                           time_expr: str) -> Tuple[str, List[Any]]:
    """Return SQL condition and parameter list for time-of-day filtering (§Invariant 2).
    Supports intraday (start <= end) and overnight wraparound (start > end).
    """
    if not time_scope:
        return "", []

    start = time_scope.get("start")
    end = time_scope.get("end")
    if not start or not end:
        return "", []

    if start <= end:
        clause = f"AND ({time_expr})::time >= %s::time AND ({time_expr})::time <= %s::time"
        return clause, [start, end]
    else:
        clause = f"AND (({time_expr})::time >= %s::time OR ({time_expr})::time <= %s::time)"
        return clause, [start, end]


# ---------------------------------------------------------------------------
# Leaf Clause Evaluators
# ---------------------------------------------------------------------------
def eval_continuous_clause(cur: Any, rule: Dict[str, Any],
                           start_date: str, end_date: str) -> Set[str]:
    """Evaluate a continuous metric (bg, iob, cob, basal_rate, deviation) on
    layer2_five_minute_aggregate (§Invariant 3).
    """
    metric_id = rule["metric"]
    cfg = FILTER_METRICS[metric_id]
    col = cfg["column"]
    comp = rule.get("comparator", cfg["default_comparator"])
    op = COMPARATOR_OPERATORS[comp]
    threshold = float(rule.get("threshold", cfg["default_threshold"]))
    time_scope = rule.get("timeScope")
    duration = rule.get("duration")

    tz_sub = _tz_sql()
    time_expr = f"a.ts AT TIME ZONE {tz_sub}"
    ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)

    if not duration:
        # Single reading suffices
        query = f"""
            SELECT DISTINCT a.day::text
            FROM layer2_five_minute_aggregate a
            WHERE a.day BETWEEN %s AND %s
              AND a.{col} IS NOT NULL
              AND a.{col} {op} %s
              {ts_clause}
        """
        params = [start_date, end_date, threshold] + ts_params
        cur.execute(query, params)
        return {str(row[0]) for row in cur.fetchall()}

    # Sustained run duration using LAG() + cumulative-SUM() episode pattern (§Invariant 3)
    val = float(duration["value"])
    unit = duration.get("unit", "minutes")
    minutes = val * 60.0 if unit == "hours" else val
    k_readings = max(1, math.ceil(minutes / 5.0))

    query = f"""
        WITH flagged AS (
            SELECT a.ts, a.day,
                   CASE WHEN a.{col} IS NOT NULL AND a.{col} {op} %s THEN 1 ELSE 0 END AS m,
                   LAG(CASE WHEN a.{col} IS NOT NULL AND a.{col} {op} %s THEN 1 ELSE 0 END)
                     OVER (ORDER BY a.ts) AS prev_m
            FROM layer2_five_minute_aggregate a
            WHERE a.day BETWEEN %s AND %s
              {ts_clause}
        ),
        episode_start AS (
            SELECT ts, day, m,
                   SUM(CASE WHEN m = 1 AND COALESCE(prev_m, 0) = 0 THEN 1 ELSE 0 END)
                     OVER (ORDER BY ts) AS episode_id
            FROM flagged
            WHERE m = 1
        )
        SELECT DISTINCT day::text
        FROM episode_start
        GROUP BY day, episode_id
        HAVING COUNT(*) >= %s
    """
    # Note: %s appears twice for threshold in flagged CTE
    params = [threshold, threshold, start_date, end_date] + ts_params + [k_readings]
    cur.execute(query, params)
    return {str(row[0]) for row in cur.fetchall()}


def eval_event_clause(cur: Any, rule: Dict[str, Any],
                      start_date: str, end_date: str) -> Set[str]:
    """Evaluate an event metric (pod_change, low_count, warning_count)."""
    metric_id = rule["metric"]
    time_scope = rule.get("timeScope")

    tz_sub = _tz_sql()
    time_expr = f"e.ts AT TIME ZONE {tz_sub}"
    ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)

    if metric_id == "pod_change":
        query = f"""
            SELECT DISTINCT ({time_expr})::date::text AS day
            FROM site_changes e
            WHERE ({time_expr})::date BETWEEN %s AND %s
              {ts_clause}
        """
        params = [start_date, end_date] + ts_params
        cur.execute(query, params)
        return {str(row[0]) for row in cur.fetchall()}
    elif metric_id == "low_count":
        cfg = FILTER_METRICS[metric_id]
        comp = rule.get("comparator", cfg["default_comparator"])
        op = COMPARATOR_OPERATORS[comp]
        threshold = float(rule.get("threshold", cfg["default_threshold"]))
        if time_scope:
            h_time_expr = f"h.start_time AT TIME ZONE {tz_sub}"
            h_ts_clause, h_ts_params = build_timescope_clause(time_scope, h_time_expr)
            query = f"""
                WITH days AS (
                    SELECT generate_series(%s::date, %s::date, '1 day'::interval)::date AS day
                ),
                matched_episodes AS (
                    SELECT ({h_time_expr})::date AS day, count(*) AS cnt
                    FROM layer2_hypo_episodes h
                    WHERE ({h_time_expr})::date BETWEEN %s AND %s
                      {h_ts_clause}
                    GROUP BY 1
                )
                SELECT d.day::text
                FROM days d
                LEFT JOIN matched_episodes m ON d.day = m.day
                WHERE COALESCE(m.cnt, 0) {op} %s
            """
            params = [start_date, end_date, start_date, end_date] + h_ts_params + [threshold]
        else:
            query = f"""
                SELECT d.date::text
                FROM layer2_daily_risk_stats d
                WHERE d.date BETWEEN %s AND %s
                  AND d.episode_count {op} %s
            """
            params = [start_date, end_date, threshold]
        cur.execute(query, params)
        return {str(row[0]) for row in cur.fetchall()}
    elif metric_id == "warning_count":
        cfg = FILTER_METRICS[metric_id]
        comp = rule.get("comparator", cfg["default_comparator"])
        op = COMPARATOR_OPERATORS[comp]
        threshold = float(rule.get("threshold", cfg["default_threshold"]))
        a_time_expr = f"a.ts AT TIME ZONE {tz_sub}"
        a_ts_clause, a_ts_params = build_timescope_clause(time_scope, a_time_expr)
        query = f"""
            SELECT a.day::text
            FROM layer2_five_minute_aggregate a
            WHERE a.day BETWEEN %s AND %s
              {a_ts_clause}
            GROUP BY a.day
            HAVING COUNT(*) FILTER (WHERE a.bg >= 3.9 AND a.bg <= 4.5) {op} %s
        """
        params = [start_date, end_date] + a_ts_params + [threshold]
        cur.execute(query, params)
        return {str(row[0]) for row in cur.fetchall()}

    return set()


def _get_timescoped_daily_expr(metric_id: str, table_alias: str = "a") -> Optional[str]:
    """Return SQL expression computing a daily metric from 5-minute readings
    when timeScope is active (§Invariant 2).
    """
    if metric_id == "tir":
        return f"ROUND(100.0 * COUNT(*) FILTER (WHERE {table_alias}.bg >= 3.9 AND {table_alias}.bg <= 10.0) / NULLIF(COUNT({table_alias}.bg), 0), 1)"
    elif metric_id == "titr":
        return f"ROUND(100.0 * COUNT(*) FILTER (WHERE {table_alias}.bg >= 3.9 AND {table_alias}.bg <= 7.8) / NULLIF(COUNT({table_alias}.bg), 0), 1)"
    elif metric_id == "tar":
        return f"ROUND(100.0 * COUNT(*) FILTER (WHERE {table_alias}.bg > 10.0) / NULLIF(COUNT({table_alias}.bg), 0), 1)"
    elif metric_id == "tbr":
        return f"ROUND(100.0 * COUNT(*) FILTER (WHERE {table_alias}.bg < 3.9) / NULLIF(COUNT({table_alias}.bg), 0), 1)"
    elif metric_id == "avg_bg":
        return f"ROUND(AVG({table_alias}.bg), 1)"
    elif metric_id == "cv":
        return f"ROUND((100.0 * STDDEV({table_alias}.bg) / NULLIF(AVG({table_alias}.bg), 0))::numeric, 1)"
    elif metric_id == "gmi":
        return f"ROUND((3.31 + 0.02392 * (AVG({table_alias}.bg) * 18.0182))::numeric, 1)"
    elif metric_id == "low_count":
        return f"COUNT(*) FILTER (WHERE {table_alias}.bg < 3.9)"
    elif metric_id == "warning_count":
        return f"COUNT(*) FILTER (WHERE {table_alias}.bg >= 3.9 AND {table_alias}.bg <= 4.5)"
    return None


def resolve_metric_source(metric_id: str,
                          time_scope: Optional[Dict[str, str]] = None,
                          table_alias: str = "d") -> Tuple[str, str, str, bool]:
    """Resolve backing table, column/calculation SQL expression, date column name,
    and whether the source is time-scoped to 5-minute aggregates (§3.5).

    Returns:
        (table_name, col_expr, date_col, is_timescoped)
    """
    if metric_id not in FILTER_METRICS:
        raise ValueError(f"Unknown metric: {metric_id}. Whitelisted keys: {list(FILTER_METRICS.keys())}")

    if metric_id == "low_count":
        if time_scope:
            return ("layer2_hypo_episodes", "episode_count", "day", True)
        return ("layer2_daily_risk_stats", f"{table_alias}.episode_count", "date", False)

    if metric_id == "warning_count":
        expr = _get_timescoped_daily_expr(metric_id, table_alias="a")
        return ("layer2_five_minute_aggregate", expr, "day", True)

    cfg = FILTER_METRICS[metric_id]
    if cfg.get("kind") != "daily":
        raise ValueError(f"Metric '{metric_id}' is of kind '{cfg.get('kind')}', not daily.")

    if time_scope and cfg.get("supports_timescope"):
        expr = _get_timescoped_daily_expr(metric_id, table_alias="a")
        if expr:
            return ("layer2_five_minute_aggregate", expr, "day", True)

    table = cfg["table"]
    if metric_id == "cv":
        col_expr = database.cv_sql_expr(f"{table_alias}.bg_sum", f"{table_alias}.bg_sum2", f"{table_alias}.bg_readings")
    else:
        col_expr = f"{table_alias}.{cfg['column']}"

    return (table, col_expr, "date", False)


def eval_daily_clause(cur: Any, rule: Dict[str, Any],
                      start_date: str, end_date: str) -> Set[str]:
    """Evaluate a daily metric with optional streak duration or timeScope."""
    metric_id = rule["metric"]
    comp = rule.get("comparator", FILTER_METRICS[metric_id]["default_comparator"])
    op = COMPARATOR_OPERATORS[comp]
    threshold = float(rule.get("threshold", FILTER_METRICS[metric_id]["default_threshold"]))
    time_scope = rule.get("timeScope")
    duration = rule.get("duration")

    # Streak duration calculation (§Invariant 4)
    streak_days = None
    if duration:
        val = int(duration["value"])
        unit = duration.get("unit", "days")
        streak_days = val * 7 if unit == "weeks" else val

    table, col_expr, date_col, is_timescoped = resolve_metric_source(metric_id, time_scope, table_alias="d")

    # Case A: timeScope active on daily metric -> Fall back to layer2_five_minute_aggregate
    if is_timescoped:
        tz_sub = _tz_sql()
        time_expr = f"a.ts AT TIME ZONE {tz_sub}"
        ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)

        if not streak_days or streak_days <= 1:
            query = f"""
                SELECT a.day::text
                FROM layer2_five_minute_aggregate a
                WHERE a.day BETWEEN %s AND %s
                  {ts_clause}
                GROUP BY a.day
                HAVING COUNT(a.bg) >= 12
                   AND ({col_expr}) {op} %s
            """
            params = [start_date, end_date] + ts_params + [threshold]
            cur.execute(query, params)
            return {str(row[0]) for row in cur.fetchall()}
        else:
            # Streak with timeScope
            query = f"""
                WITH daily_calc AS (
                    SELECT a.day AS date,
                           CASE WHEN ({col_expr}) {op} %s THEN 1 ELSE 0 END AS m
                    FROM layer2_five_minute_aggregate a
                    WHERE a.day BETWEEN (%s::date - INTERVAL '{streak_days} days')::date AND %s::date
                      {ts_clause}
                    GROUP BY a.day
                    HAVING COUNT(a.bg) >= 12
                ),
                daily_groups AS (
                    SELECT date, m,
                           date - (ROW_NUMBER() OVER (ORDER BY date) * INTERVAL '1 day') AS grp
                    FROM daily_calc
                    WHERE m = 1
                ),
                streaks AS (
                    SELECT grp, MIN(date) AS streak_start, MAX(date) AS streak_end,
                           COUNT(*) AS len
                    FROM daily_groups
                    GROUP BY grp
                    HAVING COUNT(*) >= %s
                )
                SELECT d::date::text
                FROM streaks, generate_series(streak_start, streak_end, '1 day') AS d
                WHERE d::date BETWEEN %s::date AND %s::date
            """
            params = [threshold, start_date, end_date] + ts_params + [streak_days, start_date, end_date]
            cur.execute(query, params)
            return {str(row[0]) for row in cur.fetchall()}

    # Case B: Standard daily metrics from layer2_daily_risk_stats or layer2_daily_band_stats
    if not streak_days or streak_days <= 1:
        query = f"""
            SELECT d.{date_col}::text
            FROM {table} d
            WHERE d.{date_col} BETWEEN %s AND %s
              AND {col_expr} IS NOT NULL
              AND ({col_expr}) {op} %s
        """
        params = [start_date, end_date, threshold]
        cur.execute(query, params)
        return {str(row[0]) for row in cur.fetchall()}

    # Streak mode: flag every day in the qualifying streak (§Invariant 4)
    query = f"""
        WITH daily_match AS (
            SELECT d.{date_col} AS date,
                   CASE WHEN ({col_expr}) {op} %s THEN 1 ELSE 0 END AS m
            FROM {table} d
            WHERE d.{date_col} BETWEEN (%s::date - INTERVAL '{streak_days} days')::date AND %s::date
              AND {col_expr} IS NOT NULL
        ),
        daily_groups AS (
            SELECT date, m,
                   date - (ROW_NUMBER() OVER (ORDER BY date) * INTERVAL '1 day') AS grp
            FROM daily_match
            WHERE m = 1
        ),
        streaks AS (
            SELECT grp, MIN(date) AS streak_start, MAX(date) AS streak_end,
                   COUNT(*) AS len
            FROM daily_groups
            GROUP BY grp
            HAVING COUNT(*) >= %s
        )
        SELECT d::date::text
        FROM streaks, generate_series(streak_start, streak_end, '1 day') AS d
        WHERE d::date BETWEEN %s::date AND %s::date
    """
    params = [threshold, start_date, end_date, streak_days, start_date, end_date]
    cur.execute(query, params)
    return {str(row[0]) for row in cur.fetchall()}


def eval_correlated_preceding_clauses(cur: Any, anchor_clause: Dict[str, Any],
                                      preceding_clauses: List[Dict[str, Any]],
                                      start_date: str, end_date: str) -> Set[str]:
    """Evaluates an anchor clause (continuous or event) correlated with one or more
    antecedent clauses that occurred 'in the preceding X hours/minutes'.
    Seamlessly crosses midnight boundaries via timestamp arithmetic on layer2_five_minute_aggregate.
    """
    anchor_metric = anchor_clause["metric"]
    anchor_cfg = FILTER_METRICS[anchor_metric]
    anchor_kind = anchor_cfg["kind"]
    time_scope = anchor_clause.get("timeScope")
    tz_sub = _tz_sql()

    # 1. Build Anchor CTE
    if anchor_kind == "continuous":
        col = anchor_cfg["column"]
        comp = anchor_clause.get("comparator", anchor_cfg["default_comparator"])
        op = COMPARATOR_OPERATORS[comp]
        threshold = float(anchor_clause.get("threshold", anchor_cfg["default_threshold"]))
        time_expr = f"a.ts AT TIME ZONE {tz_sub}"
        ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)
        duration = anchor_clause.get("duration")

        if not duration:
            anchor_sql = f"""
                SELECT a.ts, a.day
                FROM layer2_five_minute_aggregate a
                WHERE a.day BETWEEN %s AND %s
                  AND a.{col} IS NOT NULL AND a.{col} {op} %s
                  {ts_clause}
            """
            anchor_params = [start_date, end_date, threshold] + ts_params
        else:
            val = float(duration["value"])
            unit = duration.get("unit", "minutes")
            minutes = val * 60.0 if unit == "hours" else val
            k_readings = max(1, math.ceil(minutes / 5.0))
            anchor_sql = f"""
                WITH flagged AS (
                    SELECT a.ts, a.day,
                           CASE WHEN a.{col} IS NOT NULL AND a.{col} {op} %s THEN 1 ELSE 0 END AS m,
                           LAG(CASE WHEN a.{col} IS NOT NULL AND a.{col} {op} %s THEN 1 ELSE 0 END)
                             OVER (ORDER BY a.ts) AS prev_m
                    FROM layer2_five_minute_aggregate a
                    WHERE a.day BETWEEN %s AND %s
                      {ts_clause}
                ),
                episode_start AS (
                    SELECT ts, day, m,
                           SUM(CASE WHEN m = 1 AND COALESCE(prev_m, 0) = 0 THEN 1 ELSE 0 END)
                             OVER (ORDER BY ts) AS episode_id
                    FROM flagged
                    WHERE m = 1
                )
                SELECT MIN(ts) AS ts, day
                FROM episode_start
                GROUP BY day, episode_id
                HAVING COUNT(*) >= %s
            """
            anchor_params = [threshold, threshold, start_date, end_date] + ts_params + [k_readings]
    elif anchor_metric == "pod_change":
        time_expr = f"e.ts AT TIME ZONE {tz_sub}"
        ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)
        anchor_sql = f"""
            SELECT e.ts, ({time_expr})::date AS day
            FROM site_changes e
            WHERE ({time_expr})::date BETWEEN %s AND %s
              {ts_clause}
        """
        anchor_params = [start_date, end_date] + ts_params
    elif anchor_metric == "low_count":
        h_time_expr = f"h.start_time AT TIME ZONE {tz_sub}"
        h_ts_clause, h_ts_params = build_timescope_clause(time_scope, h_time_expr)
        anchor_sql = f"""
            SELECT h.start_time AS ts, ({h_time_expr})::date AS day
            FROM layer2_hypo_episodes h
            WHERE ({h_time_expr})::date BETWEEN %s AND %s
              {h_ts_clause}
        """
        anchor_params = [start_date, end_date] + h_ts_params
    elif anchor_metric == "warning_count":
        time_expr = f"a.ts AT TIME ZONE {tz_sub}"
        ts_clause, ts_params = build_timescope_clause(time_scope, time_expr)
        anchor_sql = f"""
            SELECT a.ts, a.day
            FROM layer2_five_minute_aggregate a
            WHERE a.day BETWEEN %s AND %s
              AND a.bg >= 3.9 AND a.bg <= 4.5
              {ts_clause}
        """
        anchor_params = [start_date, end_date] + ts_params
    else:
        return eval_leaf_clause(cur, anchor_clause, start_date, end_date)

    # 2. Build correlated conditions for each preceding clause
    exists_clauses = []
    exists_params = []

    for i, pc in enumerate(preceding_clauses):
        p_metric = pc["metric"]
        p_cfg = FILTER_METRICS[p_metric]
        p_comp = pc.get("comparator", p_cfg["default_comparator"])
        p_op = COMPARATOR_OPERATORS[p_comp]
        p_thresh = float(pc.get("threshold", p_cfg["default_threshold"]))
        prec = pc["preceding"]
        pval = float(prec["value"])
        punit = prec.get("unit", "hours")
        interval_str = f"{pval} {punit}"
        alias = f"p{i}"

        if p_metric == "pod_change":
            exists_clauses.append(f"""
                EXISTS (
                    SELECT 1 FROM site_changes {alias}
                    WHERE {alias}.ts >= anc.ts - INTERVAL '{interval_str}'
                      AND {alias}.ts <= anc.ts
                )
            """)
        else:
            p_col = p_cfg["column"]
            exists_clauses.append(f"""
                EXISTS (
                    SELECT 1 FROM layer2_five_minute_aggregate {alias}
                    WHERE {alias}.ts >= anc.ts - INTERVAL '{interval_str}'
                      AND {alias}.ts <= anc.ts
                      AND {alias}.{p_col} IS NOT NULL
                      AND {alias}.{p_col} {p_op} %s
                )
            """)
            exists_params.append(p_thresh)

    if not exists_clauses:
        combined_sql = f"""
            WITH anc AS (
                {anchor_sql}
            )
            SELECT DISTINCT anc.day::text
            FROM anc
        """
        total_params = anchor_params
    else:
        combined_sql = f"""
            WITH anc AS (
                {anchor_sql}
            )
            SELECT DISTINCT anc.day::text
            FROM anc
            WHERE {" AND ".join(exists_clauses)}
        """
        total_params = anchor_params + exists_params

    cur.execute(combined_sql, total_params)
    return {str(row[0]) for row in cur.fetchall()}


def eval_leaf_clause(cur: Any, clause: Dict[str, Any],
                     start_date: str, end_date: str) -> Set[str]:
    """Dispatch evaluation of a single leaf clause based on its metric kind."""
    metric_id = clause["metric"]
    kind = FILTER_METRICS[metric_id]["kind"]

    if clause.get("preceding") and kind == "continuous":
        cfg = FILTER_METRICS[metric_id]
        col = cfg["column"]
        comp = clause.get("comparator", cfg["default_comparator"])
        op = COMPARATOR_OPERATORS[comp]
        threshold = float(clause.get("threshold", cfg["default_threshold"]))
        prec = clause["preceding"]
        pval = float(prec["value"])
        punit = prec.get("unit", "hours")
        query = f"""
            SELECT DISTINCT d.date::text
            FROM generate_series(%s::date, %s::date, '1 day'::interval) d(date)
            JOIN layer2_five_minute_aggregate p
              ON p.ts >= (d.date::timestamp) - INTERVAL '{pval} {punit}'
             AND p.ts <= (d.date::timestamp)
            WHERE p.{col} IS NOT NULL AND p.{col} {op} %s
        """
        cur.execute(query, [start_date, end_date, threshold])
        return {str(row[0]) for row in cur.fetchall()}

    if kind == "continuous":
        return eval_continuous_clause(cur, clause, start_date, end_date)
    elif kind == "event":
        return eval_event_clause(cur, clause, start_date, end_date)
    elif kind == "daily":
        return eval_daily_clause(cur, clause, start_date, end_date)

    return set()


# ---------------------------------------------------------------------------
# Filter Tree Evaluator (§Invariant 1)
# ---------------------------------------------------------------------------
def _eval_node(cur: Any, node: Dict[str, Any],
               start_date: str, end_date: str) -> Set[str]:
    """Recursively evaluate a filter node and compose child sets."""
    if "clauses" in node:
        op = node.get("op", "AND").upper()
        clauses = node.get("clauses", [])
        if not clauses:
            return set()

        if op == "AND":
            preceding_clauses = [c for c in clauses if c.get("preceding")]
            base_clauses = [c for c in clauses if not c.get("preceding")]
            if preceding_clauses and base_clauses:
                # Correlated lookback: find anchor clause from base_clauses
                anchor_clause = next((c for c in base_clauses if FILTER_METRICS.get(c.get("metric", ""), {}).get("kind") in ("continuous", "event")), None)
                if anchor_clause:
                    corr_days = eval_correlated_preceding_clauses(cur, anchor_clause, preceding_clauses, start_date, end_date)
                    other_base = [c for c in base_clauses if c is not anchor_clause]
                    other_sets = [_eval_node(cur, c, start_date, end_date) for c in other_base]
                    all_sets = [corr_days] + other_sets
                    return set.intersection(*all_sets)

        child_sets = [_eval_node(cur, c, start_date, end_date) for c in clauses]

        if op == "AND":
            return set.intersection(*child_sets) if child_sets else set()
        elif op == "OR":
            return set.union(*child_sets) if child_sets else set()
        else:
            raise ValueError(f"Unknown operator: {op}")
    else:
        return eval_leaf_clause(cur, node, start_date, end_date)


def evaluate_filter_tree(conn: Any, filter_def: Dict[str, Any],
                         date_range: Dict[str, str]) -> Dict[str, Any]:
    """Evaluate a Filter AST over a dateRange.
    Returns the standardized output payload with matchingDates and meta (§6).
    """
    t0 = time.perf_counter()

    start_date = date_range["start"]
    end_date = date_range["end"]

    start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
    total_days = (end_d - start_d).days + 1

    with conn.cursor() as cur:
        matching_dates_set = _eval_node(cur, filter_def, start_date, end_date)

    sorted_dates = sorted(list(matching_dates_set))
    execution_time_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    return {
        "filter": filter_def,
        "dateRange": date_range,
        "matchingDates": sorted_dates,
        "meta": {
            "totalDaysEvaluated": total_days,
            "matchCount": len(sorted_dates),
            "executionTimeMs": execution_time_ms,
        },
    }
