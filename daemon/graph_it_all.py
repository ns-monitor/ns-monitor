"""
daemon/graph_it_all.py - Graph-it-All Analytics Workbench Backend

Phase 1 Core Daily Cartesian Engine:
- Page route: GET /graph-it-all
- Evidence catalogue endpoint: GET /api/v1/graph_it_all/catalogue
- Query compiler & statistical engine: POST /api/v1/graph_it_all/query
- Closed-form OLS linear & quadratic fitting with exact 95% mean confidence corridor
- Safe connection checkout, 6000ms statement timeout, and transaction rollback
- Zero silent truncation and comprehensive failure recovery
"""

import math
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
import numpy as np
from scipy import stats as scipy_stats
import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, render_template, request

import config
import database
import weight_math

logger = logging.getLogger(__name__)

graph_it_all_bp = Blueprint("graph_it_all", __name__)

# ---------------------------------------------------------------------------
# Metric Evidence Ledger (Phases 1 & 2 Catalogue)
# ---------------------------------------------------------------------------
METRIC_CATALOGUE = {
    "mean_glucose": {
        "id": "mean_glucose",
        "name": "Mean Glucose",
        "domain": "glucose",
        "unit": "mmol/L",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "day_mean_mmol",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "sd_glucose": {
        "id": "sd_glucose",
        "name": "Standard Deviation (SD)",
        "domain": "glucose",
        "unit": "mmol/L",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "sd_mmol",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "cv_glucose": {
        "id": "cv_glucose",
        "name": "Coefficient of Variation (CV)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "100.0 * sd_mmol / NULLIF(mean_mmol, 0)",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "tir_pct": {
        "id": "tir_pct",
        "name": "Time in Range (3.9–10.0)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "pct_tir",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "titr_pct": {
        "id": "titr_pct",
        "name": "Time in Tight Range (3.9–7.8)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "pct_titr",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "tbr_pct": {
        "id": "tbr_pct",
        "name": "Time Below Range (< 3.9)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "pct_tbr",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "tbr_vlow_pct": {
        "id": "tbr_vlow_pct",
        "name": "Time Very Low (< 3.0)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "pct_vlow",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "tar_pct": {
        "id": "tar_pct",
        "name": "Time Above Range (> 10.0)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "pct_tar",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "tar_vhigh_pct": {
        "id": "tar_vhigh_pct",
        "name": "Time Very High (> 13.9)",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "pct_vhigh",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "gmi": {
        "id": "gmi",
        "name": "Glucose Management Indicator",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "day_gmi_percent",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "readings_count": {
        "id": "readings_count",
        "name": "Sensor Reading Count",
        "domain": "glucose",
        "unit": "count",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "bg_readings",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "tdd": {
        "id": "tdd",
        "name": "Total Daily Dose (TDD)",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "tdd",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "total_basal": {
        "id": "total_basal",
        "name": "Basal Insulin Total",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "total_basal",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "total_bolus": {
        "id": "total_bolus",
        "name": "Total Bolus Delivered (U)",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "smb + meal_bolus + correction_bolus",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "basal_ratio": {
        "id": "basal_ratio",
        "name": "Basal / Total Ratio",
        "domain": "insulin",
        "unit": "%",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "100.0 * total_basal / NULLIF(tdd, 0)",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "total_carbs": {
        "id": "total_carbs",
        "name": "Total Carbs",
        "domain": "carbs",
        "unit": "g",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "carbs",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "meal_count": {
        "id": "meal_count",
        "name": "Meal Count (Carb Entries)",
        "domain": "events",
        "unit": "count",
        "source_table": "treatments",
        "column_expr": "carbs",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase2",
    },
    "tdd_per_kg": {
        "id": "tdd_per_kg",
        "name": "TDD per kg (Weight-Adjusted)",
        "domain": "insulin",
        "unit": "U/kg",
        "source_table": "weight_adjusted_tdd",
        "column_expr": "tdd",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase2",
    },
    "iob": {
        "id": "iob",
        "name": "Insulin on Board (IOB)",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "iob",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "cob": {
        "id": "cob",
        "name": "Carbs on Board (COB)",
        "domain": "carbs",
        "unit": "g",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "cob",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "basal_rate": {
        "id": "basal_rate",
        "name": "Basal Rate",
        "domain": "insulin",
        "unit": "U/hr",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "basal_rate",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "basal_delta": {
        "id": "basal_delta",
        "name": "Enacted Basal Delta",
        "domain": "insulin",
        "unit": "U/hr",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "(basal_rate - COALESCE(scheduled_basal, 0))",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["5min", "hourly", "daily", "weekly", "monthly"],
        "status": "verified_phase2",
    },
    "zero_temp_ratio": {
        "id": "zero_temp_ratio",
        "name": "Zero-Temp Suspension Ratio",
        "domain": "insulin",
        "unit": "%",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "(CASE WHEN COALESCE(basal_rate, 0) <= 0.001 THEN 100.0 ELSE 0.0 END)",
        "valid_aggregations": ["avg", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "daily", "weekly", "monthly"],
        "status": "verified_phase2",
    },
    "bolus_insulin": {
        "id": "bolus_insulin",
        "name": "Bolus Delivered",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "bolus_insulin",
        "valid_aggregations": ["sum", "avg", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "isf": {
        "id": "isf",
        "name": "Insulin Sensitivity Factor",
        "domain": "glucose",
        "unit": "mmol/L/U",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "isf",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "deviation": {
        "id": "deviation",
        "name": "Loop Deviation",
        "domain": "glucose",
        "unit": "mmol/L",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "deviation",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "pod_active_dur": {
        "id": "pod_active_dur",
        "name": "Capped Pod Duration",
        "domain": "hardware",
        "unit": "hours",
        "source_table": "pod_sessions_capped",
        "column_expr": "duration_hours",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily"],
        "status": "verified_phase1",
    },
    "body_weight": {
        "id": "body_weight",
        "name": "Body Weight (Weigh-ins)",
        "domain": "clinical",
        "unit": "kg",
        "source_table": "clinical_notes",
        "column_expr": "weight_kg",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily"],
        "status": "verified_phase1",
    },
    "lab_hba1c": {
        "id": "lab_hba1c",
        "name": "Laboratory HbA1c",
        "domain": "clinical",
        "unit": "%",
        "source_table": "clinical_notes",
        "column_expr": "hba1c_percent",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily"],
        "status": "verified_phase1",
    },
    "gri": {
        "id": "gri",
        "name": "Glycemic Risk Index (GRI)",
        "domain": "clinical",
        "unit": "pts",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "gri",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "gri_hypo_component": {
        "id": "gri_hypo_component",
        "name": "GRI Hypo Component",
        "domain": "clinical",
        "unit": "pts",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "gri_hypo_component",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "gri_hyper_component": {
        "id": "gri_hyper_component",
        "name": "GRI Hyper Component",
        "domain": "clinical",
        "unit": "pts",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "gri_hyper_component",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "sensor_active_pct": {
        "id": "sensor_active_pct",
        "name": "CGM Wear Time / Sensor Active",
        "domain": "glucose",
        "unit": "%",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "sensor_active_pct",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "episode_count": {
        "id": "episode_count",
        "name": "Hypo Episode Count",
        "domain": "events",
        "unit": "count",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "episode_count",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "avg_episode_duration": {
        "id": "avg_episode_duration",
        "name": "Avg Hypo Duration",
        "domain": "clinical",
        "unit": "min",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "avg_episode_duration",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "avg_ttr": {
        "id": "avg_ttr",
        "name": "Time to Recovery from Hypo (TTR)",
        "domain": "clinical",
        "unit": "min",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "avg_ttr",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "hbgi": {
        "id": "hbgi",
        "name": "High Blood Glucose Index (HBGI)",
        "domain": "clinical",
        "unit": "pts",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "hbgi",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "lbgi": {
        "id": "lbgi",
        "name": "Low Blood Glucose Index (LBGI)",
        "domain": "clinical",
        "unit": "pts",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "lbgi",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "scheduled_basal": {
        "id": "scheduled_basal",
        "name": "Scheduled Basal Total",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "scheduled_basal",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "temp_basal_impact": {
        "id": "temp_basal_impact",
        "name": "Temp Basal Delta / Impact",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "temp_basal_impact",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "smb_bolus": {
        "id": "smb_bolus",
        "name": "SMB Insulin Delivered (U)",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "smb",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "meal_bolus": {
        "id": "meal_bolus",
        "name": "Meal Bolus Delivered (U)",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "meal_bolus",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "correction_bolus": {
        "id": "correction_bolus",
        "name": "Correction Bolus Delivered (U)",
        "domain": "insulin",
        "unit": "U",
        "source_table": "layer2_daily_band_stats",
        "column_expr": "correction_bolus",
        "valid_aggregations": ["sum", "avg", "median", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase1",
    },
    "smb_count": {
        "id": "smb_count",
        "name": "Super Micro Bolus (SMB) Count",
        "domain": "events",
        "unit": "count",
        "source_table": "treatments",
        "column_expr": "smb_count",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase2",
    },
    "meal_bolus_count": {
        "id": "meal_bolus_count",
        "name": "Meal Bolus Count",
        "domain": "events",
        "unit": "count",
        "source_table": "treatments",
        "column_expr": "meal_bolus_count",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase2",
    },
    "total_bolus_count": {
        "id": "total_bolus_count",
        "name": "Total Bolus Count",
        "domain": "events",
        "unit": "count",
        "source_table": "treatments",
        "column_expr": "total_bolus_count",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase2",
    },
    "temp_basal_count": {
        "id": "temp_basal_count",
        "name": "Temp Basal Change Count",
        "domain": "events",
        "unit": "count",
        "source_table": "treatments",
        "column_expr": "temp_basal_count",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
        "status": "verified_phase2",
    },
    "hypo_count": {
        "id": "hypo_count",
        "name": "Hypo Episode Count",
        "domain": "events",
        "unit": "count",
        "source_table": "layer2_daily_risk_stats",
        "column_expr": "episode_count",
        "valid_aggregations": ["sum", "avg", "min", "max"],
        "default_aggregation": "sum",
        "valid_grains": ["daily", "weekly", "monthly"],
        "status": "verified_phase1",
    },
    "bgi": {
        "id": "bgi",
        "name": "Blood Glucose Impact (BGI)",
        "domain": "glucose",
        "unit": "mmol/L/5m",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "bgi",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
    "bg_roc": {
        "id": "bg_roc",
        "name": "Glucose Rate of Change (ROC)",
        "domain": "glucose",
        "unit": "mmol/L/m",
        "source_table": "layer2_five_minute_aggregate",
        "column_expr": "bg_roc",
        "valid_aggregations": ["avg", "median", "min", "max"],
        "default_aggregation": "avg",
        "valid_grains": ["hourly", "5min"],
        "status": "verified_phase2",
    },
}

# Special pseudo-metric for time-series abscissa
DATE_METRIC = {
    "id": "date",
    "name": "Calendar Date",
    "domain": "temporal",
    "unit": "",
    "source_table": "date_spine",
    "column_expr": "date",
    "valid_aggregations": ["none"],
    "default_aggregation": "none",
    "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
    "status": "verified_phase1",
}


def _get_tz():
    tz_name = getattr(config, "TIMEZONE", "Australia/Perth")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def sanitize_floats(obj):
    """Recursively convert NaN/Inf and numpy numbers to JSON-serializable types."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize_floats(v) for v in obj]
    elif isinstance(obj, (np.floating, np.integer)):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    elif isinstance(obj, Decimal):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    return obj


# ---------------------------------------------------------------------------
# Closed-Form OLS Statistics
# ---------------------------------------------------------------------------
def compute_ols(x_arr, y_arr, model="linear", show_corridor=True, x_labels=None):
    """
    Computes closed-form parametric (linear, quadratic, exponential, power)
    or non-parametric (LOESS) curve fitting with exact confidence corridors and ribbons.
    Supports continuous numeric abscissa or labeled discrete/date abscissa via x_labels.
    """
    n = len(x_arr)
    p = 2 if model in ("linear", "exponential", "power") else (3 if model == "quadratic" else 5)
    if n < p + 1:
        return None, f"Insufficient data points (need at least {p + 1}, got {n})"

    if np.ptp(x_arr) == 0:
        return None, "All X values are identical; no trend computable."

    y_var = float(np.var(y_arr))
    all_y_identical = (y_var == 0)

    # Initialize band structures
    bands = {
        "ci95": [],
        "ci99": [],
        "pi95": [],
        "q25_75": [],
        "q20_80": [],
        "q5_95": [],
    }

    try:
        if model == "linear":
            X = np.column_stack([np.ones(n), x_arr])
            beta, _, _, _ = np.linalg.lstsq(X, y_arr, rcond=None)
            intercept, slope = float(beta[0]), float(beta[1])
            y_fit = intercept + slope * x_arr
            sign = "+" if intercept >= 0 else "-"
            var_name = "t" if x_labels is not None else "x"
            equation = f"y = {slope:.3f}{var_name} {sign} {abs(intercept):.3f}"
            params = {"slope": slope, "intercept": intercept}

        elif model == "quadratic":
            X = np.column_stack([np.ones(n), x_arr, x_arr**2])
            beta, _, _, _ = np.linalg.lstsq(X, y_arr, rcond=None)
            c, b, a = float(beta[0]), float(beta[1]), float(beta[2])
            y_fit = a * (x_arr**2) + b * x_arr + c
            sign_b = "+" if b >= 0 else "-"
            sign_c = "+" if c >= 0 else "-"
            var_name = "t" if x_labels is not None else "x"
            equation = f"y = {a:.4f}{var_name}² {sign_b} {abs(b):.3f}{var_name} {sign_c} {abs(c):.3f}"
            params = {"a": a, "b": b, "c": c}

        elif model == "exponential":
            pos_mask = (y_arr > 0)
            if np.sum(pos_mask) < 3:
                return None, "Exponential model requires strictly positive Y values."
            x_eval, y_eval = x_arr[pos_mask], y_arr[pos_mask]
            ly = np.log(y_eval)
            X = np.column_stack([np.ones(len(x_eval)), x_eval])
            beta, _, _, _ = np.linalg.lstsq(X, ly, rcond=None)
            a = float(np.exp(beta[0]))
            b = float(beta[1])
            y_fit = a * np.exp(b * x_arr)
            var_name = "t" if x_labels is not None else "x"
            equation = f"y = {a:.3f} · e^({b:.3f}{var_name})"
            params = {"a": a, "b": b}
            ss_res_log = float(np.sum((ly - X @ beta)**2))
            se_log = float(np.sqrt(ss_res_log / max(1, len(x_eval) - 2)))

        elif model == "power":
            pos_mask = (x_arr > 0) & (y_arr > 0)
            if np.sum(pos_mask) < 3:
                return None, "Power model requires strictly positive X and Y values."
            x_eval, y_eval = x_arr[pos_mask], y_arr[pos_mask]
            lx = np.log(x_eval)
            ly = np.log(y_eval)
            X = np.column_stack([np.ones(len(x_eval)), lx])
            beta, _, _, _ = np.linalg.lstsq(X, ly, rcond=None)
            a = float(np.exp(beta[0]))
            b = float(beta[1])
            y_fit = a * (np.maximum(1e-9, x_arr) ** b)
            var_name = "t" if x_labels is not None else "x"
            equation = f"y = {a:.3f} · {var_name}^({b:.3f})"
            params = {"a": a, "b": b}
            ss_res_log = float(np.sum((ly - X @ beta)**2))
            se_log = float(np.sqrt(ss_res_log / max(1, len(x_eval) - 2)))

        elif model == "loess":
            # Locally Weighted Scatterplot Smoothing (span = 0.5)
            span = 0.5
            k = max(5, int(span * n))
            if x_labels is not None:
                grid_x = x_arr
            else:
                grid_x = np.linspace(float(np.min(x_arr)), float(np.max(x_arr)), 100)
            grid_y = []
            grid_se = []
            grid_w2 = []

            for x0 in grid_x:
                dists = np.abs(x_arr - x0)
                idx = np.argsort(dists)[:k]
                d_max = max(1e-9, dists[idx[-1]])
                u = np.minimum(1.0, dists[idx] / d_max)
                w = (1.0 - u**3)**3
                w_sum = np.sum(w)
                w_norm = (w / w_sum) if w_sum > 0 else (np.ones(k) / k)

                X_loc = np.column_stack([np.ones(k), x_arr[idx] - x0])
                W = np.diag(np.sqrt(w_norm))
                b_loc, _, _, _ = np.linalg.lstsq(W @ X_loc, W @ y_arr[idx], rcond=None)
                y0 = float(b_loc[0])
                res_loc = y_arr[idx] - (b_loc[0] + b_loc[1] * (x_arr[idx] - x0))
                se_loc = float(np.sqrt(max(1e-9, np.sum(w_norm * (res_loc**2)))))
                grid_y.append(y0)
                grid_se.append(se_loc)
                grid_w2.append(float(np.sum(w_norm**2)))

            grid_y = np.array(grid_y)
            grid_se = np.array(grid_se)
            grid_w2 = np.array(grid_w2)
            y_fit = grid_y if x_labels is not None else np.interp(x_arr, grid_x, grid_y)
            equation = "LOESS Non-Parametric (span = 0.5)"
            params = {"span": span}

            # Generate corridors for LOESS directly
            if show_corridor:
                for i, x0 in enumerate(grid_x):
                    y0 = grid_y[i]
                    se0 = grid_se[i]
                    w2 = grid_w2[i]
                    se_mean = float(se0 * np.sqrt(w2))
                    se_pred = float(se0 * np.sqrt(1.0 + w2))
                    xc = x_labels[i] if x_labels is not None else float(x0)
                    bands["ci95"].append([xc, float(y0 - 1.96 * se_mean), float(y0 + 1.96 * se_mean), float(y0)])
                    bands["ci99"].append([xc, float(y0 - 2.576 * se_mean), float(y0 + 2.576 * se_mean), float(y0)])
                    bands["pi95"].append([xc, float(y0 - 1.96 * se_pred), float(y0 + 1.96 * se_pred), float(y0)])
                    bands["q25_75"].append([xc, float(y0 - 0.674 * se_pred), float(y0 + 0.674 * se_pred), float(y0)])
                    bands["q20_80"].append([xc, float(y0 - 0.842 * se_pred), float(y0 + 0.842 * se_pred), float(y0)])
                    bands["q5_95"].append([xc, float(y0 - 1.645 * se_pred), float(y0 + 1.645 * se_pred), float(y0)])

        else:
            return None, f"Unsupported trend model: {model}"

    except Exception as e:
        logger.warning("Statistical fit failed: %s", e)
        return None, f"Matrix computation failed: {e}"

    # R2
    if all_y_identical:
        r2 = None
    else:
        ss_tot = float(np.sum((y_arr - np.mean(y_arr))**2))
        ss_res = float(np.sum((y_arr - y_fit)**2))
        r2 = max(0.0, float(1.0 - (ss_res / ss_tot))) if ss_tot > 0 else None

    # Standard error of estimate
    df = max(1, n - p)
    ss_res = float(np.sum((y_arr - y_fit)**2))
    se = float(np.sqrt(ss_res / df)) if df > 0 else 0.0

    # For parametric models (linear, quadratic, exponential, power), generate confidence corridors
    if model != "loess" and show_corridor and df > 0:
        if x_labels is not None:
            grid_x = x_arr
        else:
            x_min, x_max = float(np.min(x_arr)), float(np.max(x_arr))
            grid_x = np.linspace(x_min, x_max, 100)
        try:
            inv_XTX = np.linalg.inv(X.T @ X)
            t_95 = float(scipy_stats.t.ppf(0.975, df))
            t_99 = float(scipy_stats.t.ppf(0.995, df))
            t_q75 = float(scipy_stats.t.ppf(0.75, df))
            t_q80 = float(scipy_stats.t.ppf(0.80, df))
            t_q95 = float(scipy_stats.t.ppf(0.95, df))

            for i, x0 in enumerate(grid_x):
                xc = x_labels[i] if x_labels is not None else float(x0)
                if model == "linear":
                    X0 = np.array([1.0, x0])
                    y0 = intercept + slope * x0
                    var_mean = float(X0 @ inv_XTX @ X0)
                    se_mean = se * np.sqrt(max(0.0, var_mean))
                    se_pred = se * np.sqrt(max(0.0, 1.0 + var_mean))
                    bands["ci95"].append([xc, float(y0 - t_95 * se_mean), float(y0 + t_95 * se_mean), float(y0)])
                    bands["ci99"].append([xc, float(y0 - t_99 * se_mean), float(y0 + t_99 * se_mean), float(y0)])
                    bands["pi95"].append([xc, float(y0 - t_95 * se_pred), float(y0 + t_95 * se_pred), float(y0)])
                    bands["q25_75"].append([xc, float(y0 - t_q75 * se_pred), float(y0 + t_q75 * se_pred), float(y0)])
                    bands["q20_80"].append([xc, float(y0 - t_q80 * se_pred), float(y0 + t_q80 * se_pred), float(y0)])
                    bands["q5_95"].append([xc, float(y0 - t_q95 * se_pred), float(y0 + t_q95 * se_pred), float(y0)])

                elif model == "quadratic":
                    X0 = np.array([1.0, x0, x0**2])
                    y0 = a * (x0**2) + b * x0 + c
                    var_mean = float(X0 @ inv_XTX @ X0)
                    se_mean = se * np.sqrt(max(0.0, var_mean))
                    se_pred = se * np.sqrt(max(0.0, 1.0 + var_mean))
                    bands["ci95"].append([xc, float(y0 - t_95 * se_mean), float(y0 + t_95 * se_mean), float(y0)])
                    bands["ci99"].append([xc, float(y0 - t_99 * se_mean), float(y0 + t_99 * se_mean), float(y0)])
                    bands["pi95"].append([xc, float(y0 - t_95 * se_pred), float(y0 + t_95 * se_pred), float(y0)])
                    bands["q25_75"].append([xc, float(y0 - t_q75 * se_pred), float(y0 + t_q75 * se_pred), float(y0)])
                    bands["q20_80"].append([xc, float(y0 - t_q80 * se_pred), float(y0 + t_q80 * se_pred), float(y0)])
                    bands["q5_95"].append([xc, float(y0 - t_q95 * se_pred), float(y0 + t_q95 * se_pred), float(y0)])

                elif model == "exponential":
                    X0 = np.array([1.0, x0])
                    ly0 = beta[0] + beta[1] * x0
                    y0 = float(np.exp(ly0))
                    var_mean = float(X0 @ inv_XTX @ X0)
                    se_mean_log = se_log * np.sqrt(max(0.0, var_mean))
                    se_pred_log = se_log * np.sqrt(max(0.0, 1.0 + var_mean))
                    bands["ci95"].append([xc, float(np.exp(ly0 - t_95 * se_mean_log)), float(np.exp(ly0 + t_95 * se_mean_log)), float(y0)])
                    bands["ci99"].append([xc, float(np.exp(ly0 - t_99 * se_mean_log)), float(np.exp(ly0 + t_99 * se_mean_log)), float(y0)])
                    bands["pi95"].append([xc, float(np.exp(ly0 - t_95 * se_pred_log)), float(np.exp(ly0 + t_95 * se_pred_log)), float(y0)])
                    bands["q25_75"].append([xc, float(np.exp(ly0 - t_q75 * se_pred_log)), float(np.exp(ly0 + t_q75 * se_pred_log)), float(y0)])
                    bands["q20_80"].append([xc, float(np.exp(ly0 - t_q80 * se_pred_log)), float(np.exp(ly0 + t_q80 * se_pred_log)), float(y0)])
                    bands["q5_95"].append([xc, float(np.exp(ly0 - t_q95 * se_pred_log)), float(np.exp(ly0 + t_q95 * se_pred_log)), float(y0)])

                elif model == "power":
                    if x0 <= 0:
                        continue
                    lx0 = np.log(x0)
                    X0 = np.array([1.0, lx0])
                    ly0 = beta[0] + beta[1] * lx0
                    y0 = float(np.exp(ly0))
                    var_mean = float(X0 @ inv_XTX @ X0)
                    se_mean_log = se_log * np.sqrt(max(0.0, var_mean))
                    se_pred_log = se_log * np.sqrt(max(0.0, 1.0 + var_mean))
                    bands["ci95"].append([xc, float(np.exp(ly0 - t_95 * se_mean_log)), float(np.exp(ly0 + t_95 * se_mean_log)), float(y0)])
                    bands["ci99"].append([xc, float(np.exp(ly0 - t_99 * se_mean_log)), float(np.exp(ly0 + t_99 * se_mean_log)), float(y0)])
                    bands["pi95"].append([xc, float(np.exp(ly0 - t_95 * se_pred_log)), float(np.exp(ly0 + t_95 * se_pred_log)), float(y0)])
                    bands["q25_75"].append([xc, float(np.exp(ly0 - t_q75 * se_pred_log)), float(np.exp(ly0 + t_q75 * se_pred_log)), float(y0)])
                    bands["q20_80"].append([xc, float(np.exp(ly0 - t_q80 * se_pred_log)), float(np.exp(ly0 + t_q80 * se_pred_log)), float(y0)])
                    bands["q5_95"].append([xc, float(np.exp(ly0 - t_q95 * se_pred_log)), float(np.exp(ly0 + t_q95 * se_pred_log)), float(y0)])

        except (np.linalg.LinAlgError, ValueError):
            for k in bands:
                bands[k] = []

    # Ensure physical realism for clinical non-negative and percentage metrics
    all_y_non_neg = bool(np.all(y_arr >= 0))
    all_y_pct = bool(all_y_non_neg and np.all(y_arr <= 100))
    for k in bands:
        sanitized = []
        for row in bands[k]:
            xc, y_low, y_high, y_mid = row
            if all_y_non_neg:
                y_low = max(0.0, y_low)
            if all_y_pct and float(np.max(y_arr)) > 10.0:  # Percentage domain [0, 100]
                y_high = min(100.0, y_high)
            sanitized.append([xc, y_low, y_high, y_mid])
        bands[k] = sanitized

    # Trend line points
    points = []
    if model == "loess":
        points = [[(x_labels[i] if x_labels is not None else float(gx)), float(gy)] for i, (gx, gy) in enumerate(zip(grid_x, grid_y))]
    else:
        if x_labels is not None:
            gx_eval = x_arr
        else:
            x_min, x_max = float(np.min(x_arr)), float(np.max(x_arr))
            gx_eval = np.linspace(x_min, x_max, 100)
        for i, x0 in enumerate(gx_eval):
            xc = x_labels[i] if x_labels is not None else float(x0)
            if model == "linear":
                points.append([xc, float(intercept + slope * x0)])
            elif model == "quadratic":
                points.append([xc, float(a * (x0**2) + b * x0 + c)])
            elif model == "exponential":
                points.append([xc, float(np.exp(beta[0] + beta[1] * x0))])
            elif model == "power":
                if x0 > 0:
                    points.append([xc, float(np.exp(beta[0] + beta[1] * np.log(x0)))])

    corridor = bands["ci95"]
    prediction_corridor = bands["pi95"]

    trend_result = {
        "model": model,
        "equation": equation,
        "formula": equation,
        "r2": r2,
        "se": se,
        "params": params,
        "points": points,
        "bands": bands,
        "corridor": corridor,
        "prediction_corridor": prediction_corridor,
        "disclaimer": "Descriptive association, not causal inference."
    }
    if all_y_identical:
        trend_result["trend_note"] = "All Y values identical"

    return trend_result, None


# ---------------------------------------------------------------------------
# Query Compiler
# ---------------------------------------------------------------------------
GRAIN_LIMITS = {
    "5min": 365,       # 1 year max for high-frequency 5min
    "hourly": 3650,    # 10 years max for chronological hourly
    "daily": 36500,    # Unlimited ("All of Time" - 100 years)
    "weekly": 36500,   # Unlimited ("All of Time" - 100 years)
    "monthly": 36500,  # Unlimited ("All of Time" - 100 years)
}

FIVE_MIN_EXPR_MAP = {
    "mean_glucose": "bg",
    "total_carbs": "carbs",
    "iob": "iob",
    "cob": "cob",
    "basal_rate": "basal_rate",
    "basal_delta": "(basal_rate - COALESCE(scheduled_basal, 0))",
    "temp_basal_impact": "(basal_rate - COALESCE(scheduled_basal, 0))",
    "zero_temp_ratio": "(CASE WHEN COALESCE(basal_rate, 0) <= 0.001 THEN 100.0 ELSE 0.0 END)",
    "bolus_insulin": "bolus_insulin",
    "isf": "isf",
    "deviation": "deviation",
    "bgi": "bgi",
    "bg_roc": "bg_roc",
    "meal_count": "(CASE WHEN carbs > 0 THEN 1 ELSE 0 END)",
}

HOURLY_AGG_MAP = {
    "mean_glucose": "AVG(bg)",
    "total_carbs": "SUM(carbs)",
    "iob": "AVG(iob)",
    "cob": "AVG(cob)",
    "basal_rate": "AVG(basal_rate)",
    "basal_delta": "AVG(basal_rate - COALESCE(scheduled_basal, 0))",
    "temp_basal_impact": "AVG(basal_rate - COALESCE(scheduled_basal, 0))",
    "zero_temp_ratio": "100.0 * COUNT(CASE WHEN COALESCE(basal_rate, 0) <= 0.001 THEN 1 END) / NULLIF(COUNT(*), 0)",
    "bolus_insulin": "SUM(bolus_insulin)",
    "isf": "AVG(isf)",
    "deviation": "AVG(deviation)",
    "bgi": "AVG(bgi)",
    "bg_roc": "AVG(bg_roc)",
    "meal_count": "SUM(CASE WHEN carbs > 0 THEN 1 ELSE 0 END)",
}


def build_sql_aggregation(col, requested_agg, order_col=None):
    """
    Builds SQL expression for all supported aggregation functions:
    Common: sum, avg, median, min, max
    Spread: stddev, cv, iqr, range
    Percentiles: p10, p25, p75, p90
    Counts: count, count_nonzero
    Dynamics: delta, first, last
    """
    agg = (requested_agg or "avg").lower().strip()
    if agg == "sum":
        return f"SUM({col})"
    elif agg == "avg":
        return f"AVG({col})"
    elif agg == "median":
        return f"PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY {col})"
    elif agg == "min":
        return f"MIN({col})"
    elif agg == "max":
        return f"MAX({col})"
    elif agg == "stddev":
        return f"COALESCE(STDDEV_SAMP({col}), 0)"
    elif agg == "cv":
        return f"100.0 * COALESCE(STDDEV_SAMP({col}), 0) / NULLIF(AVG({col}), 0)"
    elif agg == "iqr":
        return f"(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {col}) - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {col}))"
    elif agg == "range":
        return f"(MAX({col}) - MIN({col}))"
    elif agg == "p10":
        return f"PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY {col})"
    elif agg == "p25":
        return f"PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {col})"
    elif agg == "p75":
        return f"PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {col})"
    elif agg == "p90":
        return f"PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY {col})"
    elif agg == "count":
        return f"COUNT({col})"
    elif agg == "count_nonzero":
        return f"COUNT(CASE WHEN {col} > 0 THEN 1 END)"
    elif agg == "first":
        ord_expr = f" ORDER BY {order_col} ASC" if order_col else ""
        return f"(ARRAY_AGG({col}{ord_expr}))[1]"
    elif agg == "last":
        ord_expr = f" ORDER BY {order_col} DESC" if order_col else ""
        return f"(ARRAY_AGG({col}{ord_expr}))[1]"
    elif agg == "delta":
        ord_asc = f" ORDER BY {order_col} ASC" if order_col else ""
        ord_desc = f" ORDER BY {order_col} DESC" if order_col else ""
        return f"((ARRAY_AGG({col}{ord_desc}))[1] - (ARRAY_AGG({col}{ord_asc}))[1])"
    else:
        return f"AVG({col})"


TREATMENT_METRIC_CONFIG = {
    "smb_count": {
        "is_count": True,
        "from_table": "treatments",
        "where": "(smb_flag = true OR event_type = 'SMB' OR notes ILIKE '%%SMB%%')",
        "val_col": None,
    },
    "meal_bolus_count": {
        "is_count": True,
        "from_table": "treatments",
        "where": "(smb_flag IS NOT TRUE AND (notes NOT ILIKE '%%SMB%%' OR notes IS NULL)) AND (event_type NOT ILIKE '%%Correction%%' OR event_type IS NULL) AND insulin > 0",
        "val_col": None,
    },
    "total_bolus_count": {
        "is_count": True,
        "from_table": "treatments",
        "where": "insulin > 0",
        "val_col": None,
    },
    "temp_basal_count": {
        "is_count": True,
        "from_table": """(SELECT ts,
                                 COALESCE(absolute, rate) AS rate,
                                 LAG(COALESCE(absolute, rate)) OVER (ORDER BY ts ASC) AS prev_rate,
                                 LAG(ts) OVER (ORDER BY ts ASC) AS prev_ts,
                                 LAG(duration) OVER (ORDER BY ts ASC) AS prev_dur
                          FROM treatments
                          WHERE event_type = 'Temp Basal'
                            AND ts >= ((%(start)s || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s - interval '1 day')
                            AND ts < (((%(end)s::date + 1) || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s + interval '1 day')
                         ) tb""",
        "where": "(prev_rate IS NULL OR rate != prev_rate OR ts > (prev_ts + (COALESCE(prev_dur, 30) || ' minutes')::interval))",
        "val_col": None,
    },
    "meal_count": {
        "is_count": True,
        "from_table": "treatments",
        "where": "carbs > 0",
        "val_col": None,
    },
    "smb_bolus": {
        "is_count": False,
        "from_table": "treatments",
        "where": "(smb_flag = true OR event_type = 'SMB' OR notes ILIKE '%%SMB%%')",
        "val_col": "insulin",
    },
    "meal_bolus": {
        "is_count": False,
        "from_table": "treatments",
        "where": "(smb_flag IS NOT TRUE AND (notes NOT ILIKE '%%SMB%%' OR notes IS NULL)) AND (event_type NOT ILIKE '%%Correction%%' OR event_type IS NULL) AND insulin > 0",
        "val_col": "insulin",
    },
    "correction_bolus": {
        "is_count": False,
        "from_table": "treatments",
        "where": "(event_type ILIKE '%%Correction%%' OR notes ILIKE '%%Correction%%') AND insulin > 0",
        "val_col": "insulin",
    },
    "total_bolus": {
        "is_count": False,
        "from_table": "treatments",
        "where": "insulin > 0",
        "val_col": "insulin",
    },
}


def query_diurnal_24h_treatment(conn, metric, agg, params, day_count, dow_clause_ts):
    """
    Computes 24-hour diurnal modal aggregation for discrete treatment events / deliveries.
    Aggregates by hour (0..23) across the date range:
    - avg: true 24-hour modal rate (total sum across period / day_count monitored days)
    - sum: total count / units across period
    - max: max count / units on any single day at that hour
    - min: min count / units on any single day at that hour (0 if count of active days < day_count)
    """
    m_id = metric["id"]
    cfg = TREATMENT_METRIC_CONFIG.get(m_id)
    if not cfg:
        return {}

    agg_name = (agg or metric.get("default_aggregation", "avg")).lower().strip()
    is_count = cfg["is_count"]
    from_table = cfg["from_table"]
    where_cond = cfg["where"]
    val_col = cfg["val_col"]

    event_agg = "COUNT(*)::numeric" if is_count else f"COALESCE(SUM({val_col}), 0)::numeric"

    if agg_name == "sum":
        outer_expr = "COALESCE(SUM(val), 0)"
    elif agg_name == "avg":
        outer_expr = f"ROUND(COALESCE(SUM(val), 0)::numeric / {day_count}, 3)"
    elif agg_name == "max":
        outer_expr = "COALESCE(MAX(val), 0)"
    elif agg_name == "min":
        outer_expr = f"CASE WHEN COUNT(DISTINCT day) < {day_count} THEN 0 ELSE COALESCE(MIN(val), 0) END"
    else:
        outer_expr = f"ROUND(COALESCE(SUM(val), 0)::numeric / {day_count}, 3)"

    sql = f"""
        WITH daily_hour_events AS (
            SELECT (ts AT TIME ZONE %(tz)s)::date AS day,
                   EXTRACT(HOUR FROM (ts AT TIME ZONE %(tz)s))::int AS cycle_bin,
                   {event_agg} AS val
            FROM {from_table}
            WHERE {where_cond}
              AND ts >= ((%(start)s || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s)
              AND ts < (((%(end)s::date + 1) || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s)
              {dow_clause_ts}
            GROUP BY 1, 2
        )
        SELECT cycle_bin,
               {outer_expr} AS metric_val
        FROM daily_hour_events
        GROUP BY cycle_bin
        ORDER BY cycle_bin ASC;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SET LOCAL statement_timeout = '6000ms';")
        cur.execute(sql, params)
        res = cur.fetchall()
        return {int(r["cycle_bin"]): float(r["metric_val"]) if r["metric_val"] is not None else 0.0 for r in res}


def get_treatment_hourly_subquery(metric, agg, alias):
    cfg = TREATMENT_METRIC_CONFIG[metric["id"]]
    is_count = cfg["is_count"]
    from_table = cfg["from_table"]
    where_cond = cfg["where"]
    val_col = cfg["val_col"]
    agg_name = (agg or metric.get("default_aggregation", "sum")).lower().strip()

    if is_count:
        val_expr = "COUNT(*)"
    else:
        if agg_name == "avg":
            val_expr = f"COALESCE(AVG({val_col}), 0)"
        elif agg_name == "max":
            val_expr = f"COALESCE(MAX({val_col}), 0)"
        elif agg_name == "min":
            val_expr = f"COALESCE(MIN({val_col}), 0)"
        else:
            val_expr = f"COALESCE(SUM({val_col}), 0)"

    subquery = f"""
        LEFT JOIN (
            SELECT (date_trunc('hour', ts AT TIME ZONE %(tz)s))::timestamp AT TIME ZONE %(tz)s AS hr,
                   {val_expr} AS val
            FROM {from_table}
            WHERE {where_cond}
              AND ts >= ((%(start)s || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s)
              AND ts < (((%(end)s::date + 1) || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s)
            GROUP BY 1
        ) {alias} ON s.hr = {alias}.hr
    """
    col_expr = f"COALESCE({alias}.val, 0)"
    return subquery, col_expr


def get_treatment_5min_subquery(metric, agg, alias):
    cfg = TREATMENT_METRIC_CONFIG[metric["id"]]
    is_count = cfg["is_count"]
    from_table = cfg["from_table"]
    where_cond = cfg["where"]
    val_col = cfg["val_col"]
    agg_name = (agg or metric.get("default_aggregation", "sum")).lower().strip()

    if is_count:
        val_expr = "COUNT(*)"
    else:
        if agg_name == "avg":
            val_expr = f"COALESCE(AVG({val_col}), 0)"
        elif agg_name == "max":
            val_expr = f"COALESCE(MAX({val_col}), 0)"
        elif agg_name == "min":
            val_expr = f"COALESCE(MIN({val_col}), 0)"
        else:
            val_expr = f"COALESCE(SUM({val_col}), 0)"

    subquery = f"""
        LEFT JOIN (
            SELECT (date_trunc('hour', ts) + (floor(extract(minute from ts) / 5) * 5 || ' minutes')::interval) AS b_ts,
                   {val_expr} AS val
            FROM {from_table}
            WHERE {where_cond}
              AND ts >= ((%(start)s || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s)
              AND ts < (((%(end)s::date + 1) || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s)
            GROUP BY 1
        ) {alias} ON layer2_five_minute_aggregate.ts = {alias}.b_ts
    """
    col_expr = f"COALESCE({alias}.val, 0)"
    return subquery, col_expr


def get_source_clause(m, alias):
    """Produces the source subquery/column for daily/temporal metrics."""
    mid = m["id"]
    tbl = m["source_table"]
    col = m["column_expr"]
    if mid == "smb_count":
        return (
            f"(SELECT b.date, COALESCE(e.cnt, 0)::int AS val "
            f"FROM layer2_daily_band_stats b "
            f"LEFT JOIN (SELECT (ts AT TIME ZONE %(tz)s)::date AS date, COUNT(*)::int AS cnt "
            f"           FROM treatments "
            f"           WHERE (smb_flag = true OR event_type = 'SMB' OR notes ILIKE '%%SMB%%') "
            f"             AND ts >= (%(start)s::date - interval '1 day') AND ts < (%(end)s::date + interval '2 days') "
            f"           GROUP BY 1) e ON b.date = e.date) {alias}",
            f"{alias}.val"
        )
    elif mid == "meal_bolus_count":
        return (
            f"(SELECT b.date, COALESCE(e.cnt, 0)::int AS val "
            f"FROM layer2_daily_band_stats b "
            f"LEFT JOIN (SELECT (ts AT TIME ZONE %(tz)s)::date AS date, COUNT(*)::int AS cnt "
            f"           FROM treatments "
            f"           WHERE (smb_flag IS NOT TRUE AND (notes NOT ILIKE '%%SMB%%' OR notes IS NULL)) "
            f"             AND (event_type NOT ILIKE '%%Correction%%' OR event_type IS NULL) "
            f"             AND insulin > 0 "
            f"             AND ts >= (%(start)s::date - interval '1 day') AND ts < (%(end)s::date + interval '2 days') "
            f"           GROUP BY 1) e ON b.date = e.date) {alias}",
            f"{alias}.val"
        )
    elif mid == "total_bolus_count":
        return (
            f"(SELECT b.date, COALESCE(e.cnt, 0)::int AS val "
            f"FROM layer2_daily_band_stats b "
            f"LEFT JOIN (SELECT (ts AT TIME ZONE %(tz)s)::date AS date, COUNT(*)::int AS cnt "
            f"           FROM treatments "
            f"           WHERE insulin > 0 "
            f"             AND ts >= (%(start)s::date - interval '1 day') AND ts < (%(end)s::date + interval '2 days') "
            f"           GROUP BY 1) e ON b.date = e.date) {alias}",
            f"{alias}.val"
        )
    elif mid == "temp_basal_count":
        return (
            f"(SELECT b.date, COALESCE(e.cnt, 0)::int AS val "
            f"FROM layer2_daily_band_stats b "
            f"LEFT JOIN (SELECT (ts AT TIME ZONE %(tz)s)::date AS date, COUNT(*)::int AS cnt "
            f"           FROM (SELECT ts, "
            f"                        COALESCE(absolute, rate) AS rate, "
            f"                        LAG(COALESCE(absolute, rate)) OVER (ORDER BY ts ASC) AS prev_rate, "
            f"                        LAG(ts) OVER (ORDER BY ts ASC) AS prev_ts, "
            f"                        LAG(duration) OVER (ORDER BY ts ASC) AS prev_dur "
            f"                 FROM treatments "
            f"                 WHERE event_type = 'Temp Basal' "
            f"                   AND ts >= (%(start)s::date - interval '1 day') AND ts < (%(end)s::date + interval '2 days') "
            f"                ) tb "
            f"           WHERE prev_rate IS NULL "
            f"              OR rate != prev_rate "
            f"              OR ts > (prev_ts + (COALESCE(prev_dur, 30) || ' minutes')::interval) "
            f"           GROUP BY 1) e ON b.date = e.date) {alias}",
            f"{alias}.val"
        )
    elif mid == "meal_count":
        return (
            f"(SELECT b.date, COALESCE(e.cnt, 0)::int AS val "
            f"FROM layer2_daily_band_stats b "
            f"LEFT JOIN (SELECT (ts AT TIME ZONE %(tz)s)::date AS date, COUNT(*)::int AS cnt "
            f"           FROM treatments "
            f"           WHERE carbs > 0 "
            f"             AND ts >= (%(start)s::date - interval '1 day') AND ts < (%(end)s::date + interval '2 days') "
            f"           GROUP BY 1) e ON b.date = e.date) {alias}",
            f"{alias}.val"
        )
    elif mid in ("episode_count", "hypo_count"):
        return (
            f"(SELECT date, episode_count AS val FROM layer2_daily_risk_stats WHERE episode_count IS NOT NULL) {alias}",
            f"{alias}.val"
        )
    elif mid == "tdd_per_kg":
        return (
            f"(SELECT date, tdd AS val FROM layer2_daily_band_stats) {alias}",
            f"{alias}.val"
        )
    elif mid == "basal_delta":
        return (
            f"(SELECT day AS date, AVG(basal_rate - COALESCE(scheduled_basal, 0)) AS val "
            f"FROM layer2_five_minute_aggregate "
            f"WHERE day >= %(start)s AND day <= %(end)s "
            f"GROUP BY day) {alias}",
            f"{alias}.val"
        )
    elif mid == "zero_temp_ratio":
        return (
            f"(SELECT day AS date, "
            f"        ROUND(100.0 * COUNT(CASE WHEN COALESCE(basal_rate, 0) <= 0.001 THEN 1 END) / NULLIF(COUNT(*), 0), 1) AS val "
            f"FROM layer2_five_minute_aggregate "
            f"WHERE day >= %(start)s AND day <= %(end)s "
            f"GROUP BY day) {alias}",
            f"{alias}.val"
        )
    elif tbl == "clinical_notes":
        note_type = "weight" if mid == "body_weight" else "hba1c"
        return (
            f"(SELECT date, {col} AS val FROM clinical_notes WHERE note_type = '{note_type}' AND {col} IS NOT NULL) {alias}",
            f"{alias}.val"
        )
    elif tbl == "pod_sessions_capped":
        return (
            f"(SELECT (start_ts AT TIME ZONE %(tz)s)::date AS date, AVG({col}) AS val FROM pod_sessions_capped WHERE status != 'active' GROUP BY (start_ts AT TIME ZONE %(tz)s)::date) {alias}",
            f"{alias}.val"
        )
    elif tbl == "layer2_five_minute_aggregate":
        return (
            f"(SELECT day AS date, {col} AS val FROM layer2_five_minute_aggregate) {alias}",
            f"{alias}.val"
        )
    else:
        return (
            f"(SELECT date, {col} AS val FROM {tbl}) {alias}",
            f"{alias}.val"
        )


def _compile_and_execute_query(
    conn, grain, start_date_str, end_date_str, x_metric, y_metric, x_agg, y_agg, tz_name,
    y2_metric=None, y2_agg="avg", cycle_mode="none", cycle_period=3.0, cycle_anchor=None, dow_filter=None
):
    """
    Compiles and executes parameterized SQL across temporal grains and cycle modes:
    Temporal Grains:
    - 5min: direct from layer2_five_minute_aggregate
    - hourly: grouped by hour from layer2_five_minute_aggregate
    - daily: single-table or cross-view JOINs with meal_count and weight interpolation
    - weekly: grouped by week from daily data
    - monthly: grouped by month from daily data

    Cycle Modes:
    - diurnal_24h: 24 clock hours (00:00 - 23:00) from 5-min aggregate
    - weekly_7d: 7 days of week (Mon - Sun)
    - annual_12m: 12 calendar months (Jan - Dec)
    - custom_x: X-day bioperiod modal with anchor date (e.g. 3.0d pod, 10d sensor, 28d hormonal)

    DOW Filter:
    - dow_filter: list of integers [1..7] where 1=Mon, ..., 7=Sun.
    """
    params = {"start": start_date_str, "end": end_date_str, "tz": tz_name}
    x_is_date = (x_metric["id"] == "date")
    t0 = datetime.now()

    dow_clause_ts = ""
    dow_clause_date = ""
    if dow_filter and len(dow_filter) < 7:
        params["dow_filter"] = dow_filter
        dow_clause_ts = " AND EXTRACT(ISODOW FROM (ts AT TIME ZONE %(tz)s)) = ANY(%(dow_filter)s)"
        dow_clause_date = " AND EXTRACT(ISODOW FROM date) = ANY(%(dow_filter)s)"

    # -----------------------------------------------------------------------
    # Cycle Modes (Periodicity Fold)
    # -----------------------------------------------------------------------
    if cycle_mode == "diurnal_24h":
        y_is_treat = y_metric["id"] in TREATMENT_METRIC_CONFIG
        y2_is_treat = bool(y2_metric and y2_metric["id"] in TREATMENT_METRIC_CONFIG)

        if not y_is_treat and not y2_is_treat:
            base_x = FIVE_MIN_EXPR_MAP.get(x_metric["id"], "NULL")
            base_y = FIVE_MIN_EXPR_MAP.get(y_metric["id"], "NULL")

            col_x = "LPAD(EXTRACT(HOUR FROM (ts AT TIME ZONE %(tz)s))::text, 2, '0') || ':00'" if x_is_date else build_sql_aggregation(base_x, x_agg or x_metric.get("default_aggregation", "avg"), order_col="ts")
            col_y = build_sql_aggregation(base_y, y_agg or y_metric.get("default_aggregation", "avg"), order_col="ts")

            col_y2_select = ""
            if y2_metric:
                base_y2 = FIVE_MIN_EXPR_MAP.get(y2_metric["id"], "NULL")
                agg_y2 = build_sql_aggregation(base_y2, y2_agg or y2_metric.get("default_aggregation", "avg"), order_col="ts")
                col_y2_select = f", {agg_y2} AS y2_val"

            sql = f"""
                SELECT EXTRACT(HOUR FROM (ts AT TIME ZONE %(tz)s))::int AS cycle_bin,
                       LPAD(EXTRACT(HOUR FROM (ts AT TIME ZONE %(tz)s))::text, 2, '0') || ':00' AS obs_date,
                       {col_x} AS x_val,
                       {col_y} AS y_val{col_y2_select}
                FROM layer2_five_minute_aggregate
                WHERE day >= %(start)s AND day <= %(end)s{dow_clause_ts}
                GROUP BY 1, 2
                ORDER BY 1 ASC;
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '6000ms';")
                cur.execute(sql, params)
                sql_rows = cur.fetchall()

            # Build full 24-hour canonical grid (00:00 - 23:00)
            row_map = {int(r["cycle_bin"]): r for r in sql_rows if r.get("cycle_bin") is not None}
            rows = []
            for h in range(24):
                lbl = f"{h:02d}:00"
                if h in row_map:
                    rows.append(row_map[h])
                else:
                    rows.append({
                        "cycle_bin": h,
                        "obs_date": lbl,
                        "x_val": lbl if x_is_date else None,
                        "y_val": None,
                        "y2_val": None if y2_metric else None
                    })
            ms = int((datetime.now() - t0).total_seconds() * 1000)
            return rows, ms
        else:
            # At least one metric is treatment-based
            # 1. Determine day_count denominator (monitored days in the selected range)
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT GREATEST(COUNT(DISTINCT date), 1)
                    FROM layer2_daily_band_stats
                    WHERE date >= %(start)s AND date <= %(end)s {dow_clause_date};
                """, params)
                cnt_row = cur.fetchone()
                day_count = cnt_row[0] if (cnt_row and cnt_row[0]) else 1

            # 2. Query treatment metric(s)
            y_treat_map = {}
            if y_is_treat:
                y_treat_map = query_diurnal_24h_treatment(conn, y_metric, y_agg, params, day_count, dow_clause_ts)

            y2_treat_map = {}
            if y2_is_treat and y2_metric:
                y2_treat_map = query_diurnal_24h_treatment(conn, y2_metric, y2_agg, params, day_count, dow_clause_ts)

            # 3. Query 5m metrics if y or y2 is from 5m table
            five_min_map = {}
            if not y_is_treat or (y2_metric and not y2_is_treat):
                col_y_5m = "NULL AS y_val"
                if not y_is_treat:
                    base_y = FIVE_MIN_EXPR_MAP.get(y_metric["id"], "NULL")
                    col_y_5m = f"{build_sql_aggregation(base_y, y_agg or y_metric.get('default_aggregation', 'avg'), order_col='ts')} AS y_val"

                col_y2_5m = ""
                if y2_metric and not y2_is_treat:
                    base_y2 = FIVE_MIN_EXPR_MAP.get(y2_metric["id"], "NULL")
                    col_y2_5m = f", {build_sql_aggregation(base_y2, y2_agg or y2_metric.get('default_aggregation', 'avg'), order_col='ts')} AS y2_val"

                sql_5m = f"""
                    SELECT EXTRACT(HOUR FROM (ts AT TIME ZONE %(tz)s))::int AS cycle_bin,
                           {col_y_5m}{col_y2_5m}
                    FROM layer2_five_minute_aggregate
                    WHERE day >= %(start)s AND day <= %(end)s{dow_clause_ts}
                    GROUP BY 1
                    ORDER BY 1 ASC;
                """
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SET LOCAL statement_timeout = '6000ms';")
                    cur.execute(sql_5m, params)
                    five_min_rows = cur.fetchall()
                    five_min_map = {int(r["cycle_bin"]): r for r in five_min_rows if r.get("cycle_bin") is not None}

            # 4. Construct canonical 24-hour grid (00:00 to 23:00)
            rows = []
            for h in range(24):
                lbl = f"{h:02d}:00"
                if y_is_treat:
                    y_val = y_treat_map.get(h, 0.0)
                else:
                    y_val = five_min_map.get(h, {}).get("y_val")

                y2_val = None
                if y2_metric:
                    if y2_is_treat:
                        y2_val = y2_treat_map.get(h, 0.0)
                    else:
                        y2_val = five_min_map.get(h, {}).get("y2_val")

                rows.append({
                    "cycle_bin": h,
                    "obs_date": lbl,
                    "x_val": lbl if x_is_date else None,
                    "y_val": y_val,
                    "y2_val": y2_val
                })

            ms = int((datetime.now() - t0).total_seconds() * 1000)
            return rows, ms

    elif cycle_mode in ("weekly_7d", "annual_12m", "custom_x"):
        if cycle_mode == "weekly_7d":
            bin_expr = "EXTRACT(ISODOW FROM tbl_y.date)::int"
            label_expr = """CASE EXTRACT(ISODOW FROM tbl_y.date)::int
                WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue' WHEN 3 THEN 'Wed'
                WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri' WHEN 6 THEN 'Sat'
                WHEN 7 THEN 'Sun'
            END"""
            canonical_bins = [(1, 'Mon'), (2, 'Tue'), (3, 'Wed'), (4, 'Thu'), (5, 'Fri'), (6, 'Sat'), (7, 'Sun')]
        elif cycle_mode == "annual_12m":
            bin_expr = "EXTRACT(MONTH FROM tbl_y.date)::int"
            label_expr = """CASE EXTRACT(MONTH FROM tbl_y.date)::int
                WHEN 1 THEN 'Jan' WHEN 2 THEN 'Feb' WHEN 3 THEN 'Mar'
                WHEN 4 THEN 'Apr' WHEN 5 THEN 'May' WHEN 6 THEN 'Jun'
                WHEN 7 THEN 'Jul' WHEN 8 THEN 'Aug' WHEN 9 THEN 'Sep'
                WHEN 10 THEN 'Oct' WHEN 11 THEN 'Nov' WHEN 12 THEN 'Dec'
            END"""
            canonical_bins = [(1, 'Jan'), (2, 'Feb'), (3, 'Mar'), (4, 'Apr'), (5, 'May'), (6, 'Jun'), (7, 'Jul'), (8, 'Aug'), (9, 'Sep'), (10, 'Oct'), (11, 'Nov'), (12, 'Dec')]
        else: # custom_x
            period_int = max(1, int(round(cycle_period or 3.0)))
            params["period"] = period_int
            params["anchor"] = cycle_anchor or start_date_str
            bin_expr = "MOD(MOD(tbl_y.date - %(anchor)s::date, %(period)s) + %(period)s, %(period)s)::int + 1"
            label_expr = f"'Day ' || ({bin_expr})::text"
            canonical_bins = [(d, f"Day {d}") for d in range(1, period_int + 1)]

        dow_clause_y = " AND EXTRACT(ISODOW FROM tbl_y.date) = ANY(%(dow_filter)s)" if (dow_filter and len(dow_filter) < 7) else ""

        from_y, col_y = get_source_clause(y_metric, "tbl_y")
        agg_y_expr = build_sql_aggregation(col_y, y_agg or y_metric.get("default_aggregation", "avg"), order_col="tbl_y.date")

        join_x = ""
        if x_is_date:
            col_x_expr = label_expr
        else:
            from_x, col_x = get_source_clause(x_metric, "tbl_x")
            join_x = f"INNER JOIN {from_x} ON tbl_y.date = tbl_x.date"
            col_x_expr = build_sql_aggregation(col_x, x_agg or x_metric.get("default_aggregation", "avg"), order_col="tbl_x.date")

        col_y2_select = ""
        join_y2 = ""
        if y2_metric:
            from_y2, col_y2 = get_source_clause(y2_metric, "tbl_y2")
            agg_y2_expr = build_sql_aggregation(col_y2, y2_agg or y2_metric.get("default_aggregation", "avg"), order_col="tbl_y2.date")
            col_y2_select = f", {agg_y2_expr} AS y2_val"
            join_y2 = f"INNER JOIN {from_y2} ON tbl_y.date = tbl_y2.date"

        sql = f"""
            SELECT {bin_expr} AS cycle_bin,
                   {label_expr} AS obs_date,
                   {col_x_expr} AS x_val,
                   {agg_y_expr} AS y_val{col_y2_select}
            FROM {from_y}
            {join_x}
            {join_y2}
            WHERE tbl_y.date >= %(start)s AND tbl_y.date <= %(end)s{dow_clause_y}
            GROUP BY 1, 2
            ORDER BY 1 ASC;
        """
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '6000ms';")
            cur.execute(sql, params)
            sql_rows = cur.fetchall()

        # Build full canonical bins
        row_map = {int(r["cycle_bin"]): r for r in sql_rows if r.get("cycle_bin") is not None}
        rows = []
        for b_id, b_label in canonical_bins:
            if b_id in row_map:
                rows.append(row_map[b_id])
            else:
                rows.append({
                    "cycle_bin": b_id,
                    "obs_date": b_label,
                    "x_val": b_label if x_is_date else None,
                    "y_val": None,
                    "y2_val": None if y2_metric else None
                })
        ms = int((datetime.now() - t0).total_seconds() * 1000)
        return rows, ms

    # -----------------------------------------------------------------------
    # Chronological Temporal Grains
    # -----------------------------------------------------------------------
    if grain == "5min":
        y_is_treat = y_metric["id"] in TREATMENT_METRIC_CONFIG
        y2_is_treat = bool(y2_metric and y2_metric["id"] in TREATMENT_METRIC_CONFIG)

        if not y_is_treat and not y2_is_treat:
            col_x = "to_char(ts AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI')" if x_is_date else FIVE_MIN_EXPR_MAP.get(x_metric["id"], "NULL")
            col_y = FIVE_MIN_EXPR_MAP.get(y_metric["id"], "NULL")
            col_y2_select = f", {FIVE_MIN_EXPR_MAP.get(y2_metric['id'], 'NULL')} AS y2_val" if y2_metric else ""
            sql = f"""
                SELECT to_char(ts AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI') AS obs_date,
                       {col_x} AS x_val,
                       {col_y} AS y_val{col_y2_select}
                FROM layer2_five_minute_aggregate
                WHERE day >= %(start)s AND day <= %(end)s{dow_clause_ts}
                ORDER BY ts ASC;
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '6000ms';")
                cur.execute(sql, params)
                rows = cur.fetchall()
            ms = int((datetime.now() - t0).total_seconds() * 1000)
            return rows, ms
        else:
            col_x = "to_char(layer2_five_minute_aggregate.ts AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI')" if x_is_date else FIVE_MIN_EXPR_MAP.get(x_metric["id"], "NULL")

            joins = []
            if y_is_treat:
                sub_y, col_y = get_treatment_5min_subquery(y_metric, y_agg, "t5_y")
                joins.append(sub_y)
            else:
                col_y = FIVE_MIN_EXPR_MAP.get(y_metric["id"], "NULL")

            col_y2_select = ""
            if y2_metric:
                if y2_is_treat:
                    sub_y2, col_y2 = get_treatment_5min_subquery(y2_metric, y2_agg, "t5_y2")
                    joins.append(sub_y2)
                else:
                    col_y2 = FIVE_MIN_EXPR_MAP.get(y2_metric["id"], "NULL")
                col_y2_select = f", {col_y2} AS y2_val"

            join_sql = "\n".join(joins)
            sql = f"""
                SELECT to_char(layer2_five_minute_aggregate.ts AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI') AS obs_date,
                       {col_x} AS x_val,
                       {col_y} AS y_val{col_y2_select}
                FROM layer2_five_minute_aggregate
                {join_sql}
                WHERE day >= %(start)s AND day <= %(end)s{dow_clause_ts}
                ORDER BY layer2_five_minute_aggregate.ts ASC;
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '6000ms';")
                cur.execute(sql, params)
                rows = cur.fetchall()
            ms = int((datetime.now() - t0).total_seconds() * 1000)
            return rows, ms

    elif grain == "hourly":
        y_is_treat = y_metric["id"] in TREATMENT_METRIC_CONFIG
        y2_is_treat = bool(y2_metric and y2_metric["id"] in TREATMENT_METRIC_CONFIG)

        if not y_is_treat and not y2_is_treat:
            if x_is_date:
                col_x = "to_char(date_trunc('hour', ts AT TIME ZONE %(tz)s), 'YYYY-MM-DD HH24:00')"
            else:
                base_col_x = FIVE_MIN_EXPR_MAP.get(x_metric["id"], "NULL")
                col_x = build_sql_aggregation(base_col_x, x_agg or x_metric.get("default_aggregation", "avg"), order_col="ts")

            base_col_y = FIVE_MIN_EXPR_MAP.get(y_metric["id"], "NULL")
            col_y = build_sql_aggregation(base_col_y, y_agg or y_metric.get("default_aggregation", "avg"), order_col="ts")

            col_y2_select = ""
            if y2_metric:
                base_col_y2 = FIVE_MIN_EXPR_MAP.get(y2_metric["id"], "NULL")
                agg_y2 = build_sql_aggregation(base_col_y2, y2_agg or y2_metric.get("default_aggregation", "avg"), order_col="ts")
                col_y2_select = f", {agg_y2} AS y2_val"

            sql = f"""
                SELECT to_char(date_trunc('hour', ts AT TIME ZONE %(tz)s), 'YYYY-MM-DD HH24:00') AS obs_date,
                       {col_x} AS x_val,
                       {col_y} AS y_val{col_y2_select}
                FROM layer2_five_minute_aggregate
                WHERE day >= %(start)s AND day <= %(end)s{dow_clause_ts}
                GROUP BY date_trunc('hour', ts AT TIME ZONE %(tz)s)
                ORDER BY 1 ASC;
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '6000ms';")
                cur.execute(sql, params)
                rows = cur.fetchall()
            ms = int((datetime.now() - t0).total_seconds() * 1000)
            return rows, ms
        else:
            # Hourly grain with treatment metric(s)
            dow_spine = " AND EXTRACT(ISODOW FROM (s.hr AT TIME ZONE %(tz)s)) = ANY(%(dow_filter)s)" if (dow_filter and len(dow_filter) < 7) else ""

            joins = []
            if y_is_treat:
                sub_y, col_y = get_treatment_hourly_subquery(y_metric, y_agg, "th_y")
                joins.append(sub_y)
            else:
                base_col_y = FIVE_MIN_EXPR_MAP.get(y_metric["id"], "NULL")
                agg_y = build_sql_aggregation(base_col_y, y_agg or y_metric.get("default_aggregation", "avg"), order_col="ts")
                joins.append(f"""
                    LEFT JOIN (
                        SELECT (date_trunc('hour', ts AT TIME ZONE %(tz)s))::timestamp AT TIME ZONE %(tz)s AS hr,
                               {agg_y} AS val
                        FROM layer2_five_minute_aggregate
                        WHERE day >= %(start)s AND day <= %(end)s
                        GROUP BY 1
                    ) f_y ON s.hr = f_y.hr
                """)
                col_y = "f_y.val"

            col_y2_select = ""
            if y2_metric:
                if y2_is_treat:
                    sub_y2, col_y2 = get_treatment_hourly_subquery(y2_metric, y2_agg, "th_y2")
                    joins.append(sub_y2)
                else:
                    base_col_y2 = FIVE_MIN_EXPR_MAP.get(y2_metric["id"], "NULL")
                    agg_y2 = build_sql_aggregation(base_col_y2, y2_agg or y2_metric.get("default_aggregation", "avg"), order_col="ts")
                    joins.append(f"""
                        LEFT JOIN (
                            SELECT (date_trunc('hour', ts AT TIME ZONE %(tz)s))::timestamp AT TIME ZONE %(tz)s AS hr,
                                   {agg_y2} AS val
                            FROM layer2_five_minute_aggregate
                            WHERE day >= %(start)s AND day <= %(end)s
                            GROUP BY 1
                        ) f_y2 ON s.hr = f_y2.hr
                    """)
                    col_y2 = "f_y2.val"
                col_y2_select = f", {col_y2} AS y2_val"

            join_sql = "\n".join(joins)
            sql = f"""
                WITH spine AS (
                    SELECT generate_series(
                        (%(start)s || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s,
                        ((%(end)s::date + 1) || ' 00:00:00')::timestamp AT TIME ZONE %(tz)s - interval '1 hour',
                        interval '1 hour'
                    ) AS hr
                )
                SELECT to_char(s.hr AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:00') AS obs_date,
                       to_char(s.hr AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:00') AS x_val,
                       {col_y} AS y_val{col_y2_select}
                FROM spine s
                {join_sql}
                WHERE 1=1{dow_spine}
                ORDER BY s.hr ASC;
            """
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL statement_timeout = '6000ms';")
                cur.execute(sql, params)
                rows = cur.fetchall()
            ms = int((datetime.now() - t0).total_seconds() * 1000)
            return rows, ms

    elif grain in ("daily", "weekly", "monthly"):
        needs_weight_interpolation = (
            x_metric["id"] == "tdd_per_kg" or
            y_metric["id"] == "tdd_per_kg" or
            (y2_metric is not None and y2_metric["id"] == "tdd_per_kg")
        )

        dow_clause_simple = " AND EXTRACT(ISODOW FROM date) = ANY(%(dow_filter)s)" if (dow_filter and len(dow_filter) < 7) else ""
        dow_clause_tbl_y = " AND EXTRACT(ISODOW FROM tbl_y.date) = ANY(%(dow_filter)s)" if (dow_filter and len(dow_filter) < 7) else ""
        dow_clause_tbl_x = " AND EXTRACT(ISODOW FROM tbl_x.date) = ANY(%(dow_filter)s)" if (dow_filter and len(dow_filter) < 7) else ""

        NON_DIRECT_DAILY_TABLES = ("clinical_notes", "pod_sessions_capped", "layer2_five_minute_aggregate", "treatments")
        NON_DIRECT_DAILY_METRIC_IDS = ("meal_count", "smb_count", "meal_bolus_count", "total_bolus_count", "temp_basal_count", "tdd_per_kg", "basal_delta", "zero_temp_ratio")

        if grain == "daily":
            if not y2_metric:
                if x_is_date:
                    from_y, col_y = get_source_clause(y_metric, "tbl_y")
                    sql = f"""
                        SELECT tbl_y.date::text AS obs_date,
                               tbl_y.date::text AS x_val,
                               {col_y} AS y_val
                        FROM {from_y}
                        WHERE tbl_y.date >= %(start)s AND tbl_y.date <= %(end)s{dow_clause_tbl_y}
                        ORDER BY tbl_y.date ASC;
                    """
                elif x_metric["source_table"] == y_metric["source_table"] and x_metric["id"] not in NON_DIRECT_DAILY_METRIC_IDS and y_metric["id"] not in NON_DIRECT_DAILY_METRIC_IDS and x_metric["source_table"] not in NON_DIRECT_DAILY_TABLES:
                    t_x = x_metric["source_table"]
                    sql = f"""
                        SELECT date::text AS obs_date,
                               {x_metric['column_expr']} AS x_val,
                               {y_metric['column_expr']} AS y_val
                        FROM {t_x}
                        WHERE date >= %(start)s AND date <= %(end)s{dow_clause_simple}
                        ORDER BY date ASC;
                    """
                else:
                    from_x, col_x = get_source_clause(x_metric, "tbl_x")
                    from_y, col_y = get_source_clause(y_metric, "tbl_y")
                    sql = f"""
                        SELECT tbl_x.date::text AS obs_date,
                               {col_x} AS x_val,
                               {col_y} AS y_val
                        FROM {from_x}
                        INNER JOIN {from_y} ON tbl_x.date = tbl_y.date
                        WHERE tbl_x.date >= %(start)s AND tbl_x.date <= %(end)s{dow_clause_tbl_x}
                        ORDER BY tbl_x.date ASC;
                    """
            else:
                # y2_metric is active
                if x_is_date:
                    if y_metric["source_table"] == y2_metric["source_table"] and y_metric["id"] not in NON_DIRECT_DAILY_METRIC_IDS and y2_metric["id"] not in NON_DIRECT_DAILY_METRIC_IDS and y_metric["source_table"] not in NON_DIRECT_DAILY_TABLES:
                        sql = f"""
                            SELECT date::text AS obs_date,
                                   date::text AS x_val,
                                   {y_metric['column_expr']} AS y_val,
                                   {y2_metric['column_expr']} AS y2_val
                            FROM {y_metric['source_table']}
                            WHERE date >= %(start)s AND date <= %(end)s{dow_clause_simple}
                            ORDER BY date ASC;
                        """
                    else:
                        from_y, col_y = get_source_clause(y_metric, "tbl_y")
                        from_y2, col_y2 = get_source_clause(y2_metric, "tbl_y2")
                        sql = f"""
                            SELECT tbl_y.date::text AS obs_date,
                                   tbl_y.date::text AS x_val,
                                   {col_y} AS y_val,
                                   {col_y2} AS y2_val
                            FROM {from_y}
                            INNER JOIN {from_y2} ON tbl_y.date = tbl_y2.date
                            WHERE tbl_y.date >= %(start)s AND tbl_y.date <= %(end)s{dow_clause_tbl_y}
                            ORDER BY tbl_y.date ASC;
                        """
                else:
                    if x_metric["source_table"] == y_metric["source_table"] == y2_metric["source_table"] and all(m["id"] not in NON_DIRECT_DAILY_METRIC_IDS for m in (x_metric, y_metric, y2_metric)) and x_metric["source_table"] not in NON_DIRECT_DAILY_TABLES:
                        sql = f"""
                            SELECT date::text AS obs_date,
                                   {x_metric['column_expr']} AS x_val,
                                   {y_metric['column_expr']} AS y_val,
                                   {y2_metric['column_expr']} AS y2_val
                            FROM {x_metric['source_table']}
                            WHERE date >= %(start)s AND date <= %(end)s{dow_clause_simple}
                            ORDER BY date ASC;
                        """
                    else:
                        from_x, col_x = get_source_clause(x_metric, "tbl_x")
                        from_y, col_y = get_source_clause(y_metric, "tbl_y")
                        from_y2, col_y2 = get_source_clause(y2_metric, "tbl_y2")
                        sql = f"""
                            SELECT tbl_x.date::text AS obs_date,
                                   {col_x} AS x_val,
                                   {col_y} AS y_val,
                                   {col_y2} AS y2_val
                            FROM {from_x}
                            INNER JOIN {from_y} ON tbl_x.date = tbl_y.date
                            INNER JOIN {from_y2} ON tbl_x.date = tbl_y2.date
                            WHERE tbl_x.date >= %(start)s AND tbl_x.date <= %(end)s{dow_clause_tbl_x}
                            ORDER BY tbl_x.date ASC;
                        """
        else:
            # weekly or monthly
            trunc_part = "week" if grain == "weekly" else "month"
            from_y, col_y = get_source_clause(y_metric, "tbl_y")
            agg_y_expr = build_sql_aggregation(col_y, y_agg or y_metric.get("default_aggregation", "avg"), order_col="tbl_y.date")

            col_y2_select = ""
            join_y2 = ""
            if y2_metric:
                from_y2, col_y2 = get_source_clause(y2_metric, "tbl_y2")
                agg_y2_expr = build_sql_aggregation(col_y2, y2_agg or y2_metric.get("default_aggregation", "avg"), order_col="tbl_y2.date")
                col_y2_select = f", {agg_y2_expr} AS y2_val"
                target_join = "tbl_y" if x_is_date else "tbl_x"
                join_y2 = f"INNER JOIN {from_y2} ON {target_join}.date = tbl_y2.date"

            if x_is_date:
                sql = f"""
                    SELECT date_trunc('{trunc_part}', tbl_y.date)::date::text AS obs_date,
                           date_trunc('{trunc_part}', tbl_y.date)::date::text AS x_val,
                           {agg_y_expr} AS y_val{col_y2_select}
                    FROM {from_y}
                    {join_y2}
                    WHERE tbl_y.date >= %(start)s AND tbl_y.date <= %(end)s{dow_clause_tbl_y}
                    GROUP BY date_trunc('{trunc_part}', tbl_y.date)::date
                    ORDER BY 1 ASC;
                """
            else:
                from_x, col_x = get_source_clause(x_metric, "tbl_x")
                agg_x_expr = build_sql_aggregation(col_x, x_agg or x_metric.get("default_aggregation", "avg"), order_col="tbl_x.date")
                sql = f"""
                    SELECT date_trunc('{trunc_part}', tbl_x.date)::date::text AS obs_date,
                           {agg_x_expr} AS x_val,
                           {agg_y_expr} AS y_val{col_y2_select}
                    FROM {from_x}
                    INNER JOIN {from_y} ON tbl_x.date = tbl_y.date
                    {join_y2}
                    WHERE tbl_x.date >= %(start)s AND tbl_x.date <= %(end)s{dow_clause_tbl_x}
                    GROUP BY date_trunc('{trunc_part}', tbl_x.date)::date
                    ORDER BY 1 ASC;
                """

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '6000ms';")
            cur.execute(sql, params)
            rows = cur.fetchall()

        # Handle weight-adjustment for tdd_per_kg if requested
        if needs_weight_interpolation:
            with conn.cursor() as cur:
                cur.execute("SELECT date, weight_kg FROM clinical_notes WHERE note_type = 'weight' AND weight_kg IS NOT NULL ORDER BY date ASC;")
                raw_weights = cur.fetchall()
            anchors = weight_math.sanitize_anchors(raw_weights)

            for r in rows:
                row_d = None
                try:
                    row_d = datetime.strptime(r["obs_date"][:10], "%Y-%m-%d").date()
                except Exception:
                    pass
                w = weight_math.interpolate_weight(row_d, anchors) if (row_d and anchors) else None

                if x_metric["id"] == "tdd_per_kg":
                    if w and w > 0 and r["x_val"] is not None:
                        r["x_val"] = round(float(r["x_val"]) / float(w), 4)
                    else:
                        r["x_val"] = None

                if y_metric["id"] == "tdd_per_kg":
                    if w and w > 0 and r["y_val"] is not None:
                        r["y_val"] = round(float(r["y_val"]) / float(w), 4)
                    else:
                        r["y_val"] = None

                if y2_metric and y2_metric["id"] == "tdd_per_kg":
                    if w and w > 0 and r.get("y2_val") is not None:
                        r["y2_val"] = round(float(r["y2_val"]) / float(w), 4)
                    else:
                        r["y2_val"] = None

        ms = int((datetime.now() - t0).total_seconds() * 1000)
        return rows, ms


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@graph_it_all_bp.route("/graph-it-all", methods=["GET"])
def graph_it_all_page():
    """Render the Graph-it-All exploration workbench."""
    tz = _get_tz()
    today = datetime.now(tz).date()

    end_date = request.args.get("end_date", "").strip() or today.strftime("%Y-%m-%d")
    start_date = request.args.get("start_date", "").strip() or (today - timedelta(days=90)).strftime("%Y-%m-%d")

    conn = None
    earliest_cgm = None
    try:
        conn = database.get_conn()
        if conn is not None:
            earliest_cgm = database.get_earliest_cgm_date(conn)
    except Exception as e:
        logger.warning("Could not fetch earliest cgm date: %s", e)
    finally:
        if conn is not None:
            database.return_conn(conn)

    return render_template(
        "graph_it_all.html",
        start_date=start_date,
        end_date=end_date,
        today_local=today.strftime("%Y-%m-%d"),
        earliest_cgm_date=earliest_cgm,
    )


@graph_it_all_bp.route("/api/v1/graph_it_all/catalogue", methods=["GET"])
def api_get_catalogue():
    """Return the evidence ledger metadata for client consumption."""
    items = []
    # Add calendar date option for line/bar charts
    items.append({
        "id": DATE_METRIC["id"],
        "name": DATE_METRIC["name"],
        "domain": DATE_METRIC["domain"],
        "unit": DATE_METRIC["unit"],
        "valid_aggregations": DATE_METRIC["valid_aggregations"],
        "default_aggregation": DATE_METRIC["default_aggregation"],
        "valid_grains": DATE_METRIC["valid_grains"],
        "status": DATE_METRIC["status"]
    })
    for m in METRIC_CATALOGUE.values():
        items.append({
            "id": m["id"],
            "name": m["name"],
            "domain": m["domain"],
            "unit": m["unit"],
            "valid_aggregations": m["valid_aggregations"],
            "default_aggregation": m["default_aggregation"],
            "valid_grains": m["valid_grains"],
            "status": m["status"]
        })
    return jsonify({"metrics": items})


@graph_it_all_bp.route("/api/v1/graph_it_all/query", methods=["POST"])
def api_query_graph_it_all():
    """
    Execute exploratory query and compute statistical overlays.
    Enforces failure recovery protocols A1-A6, B1-B5, and Zero Silent Truncation.
    """
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a valid JSON object."}), 400

    grain = data.get("grain", "daily")
    if grain not in GRAIN_LIMITS:
        return jsonify({"error": f"Invalid grain '{grain}'. Valid grains are: {list(GRAIN_LIMITS.keys())}."}), 400

    start_date = data.get("start_date", "").strip()
    end_date = data.get("end_date", "").strip()

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required."}), 400

    try:
        d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        d_end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Dates must be in YYYY-MM-DD format."}), 400

    if d_start > d_end:
        return jsonify({"error": "start_date cannot be after end_date."}), 400

    # Parse Periodicity / Cycle Fold & DOW Filter
    cycle_mode = data.get("cycle_mode", "none")
    if cycle_mode not in ("none", "diurnal_24h", "weekly_7d", "annual_12m", "custom_x"):
        return jsonify({"error": f"Invalid cycle_mode '{cycle_mode}'. Valid modes: ['none', 'diurnal_24h', 'weekly_7d', 'annual_12m', 'custom_x']."}), 400

    cycle_period = float(data.get("cycle_period") or 3.0)
    cycle_period = max(1.0, min(365.0, cycle_period))
    cycle_anchor = data.get("cycle_anchor", "").strip() or start_date

    dow_filter = data.get("dow_filter")
    if dow_filter and isinstance(dow_filter, list):
        clean_dow = [int(d) for d in dow_filter if str(d).isdigit() and 1 <= int(d) <= 7]
        if 0 < len(clean_dow) < 7:
            dow_filter = sorted(list(set(clean_dow)))
        else:
            dow_filter = None
    else:
        dow_filter = None

    # Pre-execution check: ceiling per grain (§8.1 & §5.2)
    # Relaxed constraints: allow all of time (36,500d) for daily/weekly/monthly and cycle folds
    span_days = (d_end - d_start).days + 1
    max_days = 36500 if (cycle_mode != "none" or grain in ("daily", "weekly", "monthly")) else GRAIN_LIMITS[grain]
    if span_days > max_days:
        time_desc = f"{max_days // 365} years ({max_days} days)" if max_days >= 365 else f"{max_days} days"
        return jsonify({
            "error": f"Date range of {span_days} days exceeds maximum permitted span of {time_desc} for {grain} analysis. Please shorten date range."
        }), 400

    x_spec = data.get("x") or {}
    y_spec = data.get("y") or {}

    x_metric_id = x_spec.get("metricId", "").strip()
    y_metric_id = y_spec.get("metricId", "").strip()
    x_agg = x_spec.get("aggregation", "avg")
    y_agg = y_spec.get("aggregation", "avg")

    if not x_metric_id or not y_metric_id:
        return jsonify({"error": "Both x and y metric specifications are required."}), 400

    if cycle_mode != "none":
        cycle_name = (
            "Hour of Day" if cycle_mode == "diurnal_24h" else (
                "Day of Week" if cycle_mode == "weekly_7d" else (
                    "Calendar Month" if cycle_mode == "annual_12m" else "Cycle Day"
                )
            )
        )
        x_metric = {
            "id": "date",
            "name": cycle_name,
            "domain": "temporal",
            "unit": "",
            "source_table": "date_spine",
            "column_expr": "date",
            "valid_aggregations": ["none"],
            "default_aggregation": "none",
            "valid_grains": ["daily", "weekly", "monthly", "hourly", "5min"],
            "status": "verified_phase1",
        }
        x_agg = "none"
    else:
        x_metric = DATE_METRIC if x_metric_id == "date" else METRIC_CATALOGUE.get(x_metric_id)
        if not x_metric:
            return jsonify({"error": f"Unknown or unverified X metric ID '{x_metric_id}'."}), 400

    y_metric = METRIC_CATALOGUE.get(y_metric_id)
    if not y_metric:
        return jsonify({"error": f"Unknown or unverified Y metric ID '{y_metric_id}'."}), 400

    # Diurnal 24h mode requires high-frequency/hourly metrics
    effective_grain = "hourly" if (cycle_mode == "diurnal_24h" and grain in ("daily", "weekly", "monthly")) else grain

    if cycle_mode == "diurnal_24h":
        if "hourly" not in y_metric["valid_grains"] and "5min" not in y_metric["valid_grains"]:
            return jsonify({"error": f"Metric '{y_metric['name']}' does not support 24-hour diurnal analysis (daily aggregate only)."}), 400
    else:
        if effective_grain not in x_metric["valid_grains"]:
            return jsonify({"error": f"Metric '{x_metric['name']}' is not available for {effective_grain} grain (requires {', '.join(x_metric['valid_grains'])})."}), 400
        if effective_grain not in y_metric["valid_grains"]:
            return jsonify({"error": f"Metric '{y_metric['name']}' is not available for {effective_grain} grain (requires {', '.join(y_metric['valid_grains'])})."}), 400

    # Optional Secondary Y2 Metric
    y2_spec = data.get("y2")
    y2_metric = None
    y2_agg = "avg"
    if y2_spec and isinstance(y2_spec, dict) and y2_spec.get("metricId"):
        y2_metric_id = y2_spec.get("metricId", "").strip()
        y2_agg = y2_spec.get("aggregation", "avg")
        y2_metric = METRIC_CATALOGUE.get(y2_metric_id)
        if not y2_metric:
            return jsonify({"error": f"Unknown or unverified Y2 metric ID '{y2_metric_id}'."}), 400
        if cycle_mode == "diurnal_24h":
            if "hourly" not in y2_metric["valid_grains"] and "5min" not in y2_metric["valid_grains"]:
                return jsonify({"error": f"Metric '{y2_metric['name']}' does not support 24-hour diurnal analysis."}), 400
        elif effective_grain not in y2_metric["valid_grains"]:
            return jsonify({"error": f"Metric '{y2_metric['name']}' is not available for {effective_grain} grain (requires {', '.join(y2_metric['valid_grains'])})."}), 400

    # Database checkout and execution
    conn = None
    try:
        conn = database.get_conn()
        if conn is None:
            return jsonify({"error": "Database temporarily unavailable. Please retry."}), 503

        tz_name = getattr(config, "TIMEZONE", "Australia/Perth")

        with conn:
            rows, exec_ms = _compile_and_execute_query(
                conn, effective_grain, start_date, end_date, x_metric, y_metric, x_agg, y_agg, tz_name,
                y2_metric=y2_metric, y2_agg=y2_agg, cycle_mode=cycle_mode,
                cycle_period=cycle_period, cycle_anchor=cycle_anchor, dow_filter=dow_filter
            )

            # Zero silent truncation ceiling (§8.3)
            if len(rows) > 50000:
                return jsonify({"error": f"Query returned {len(rows)} points, exceeding 50,000 point ceiling. Please shorten date range or aggregate."}), 400

    except psycopg2.errors.QueryCanceled:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.warning("Graph-it-All query exceeded 6000ms statement timeout")
        return jsonify({"error": "Query exceeded time limit (6s). Shorten date range or use coarser grain."}), 504
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception("Graph-it-All query failed")
        return jsonify({"error": f"Query execution failed: {e}"}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)

    # Empty rows (A4 / A5)
    if not rows:
        return jsonify({
            "data": [],
            "trend": None,
            "meta": {
                "points": 0,
                "nulls_excluded": 0,
                "execution_ms": exec_ms,
                "message": "No data available for this date range and metric."
            }
        })

    # Filter null pairs without replacing with zero (A6)
    valid_points = []
    is_cycle = (cycle_mode != "none")
    x_is_date = (x_metric["id"] == "date") or is_cycle
    nulls_excluded = 0

    for r in rows:
        xv = r["x_val"]
        yv = r["y_val"]
        y2v = r.get("y2_val")
        obs_d = r["obs_date"]

        if yv is None:
            nulls_excluded += 1
            if is_cycle:
                # Retain empty cycle buckets so canonical 12 months / 24 hours axis remains fully populated
                valid_points.append({
                    "x": obs_d,
                    "y": None,
                    "date": obs_d,
                    "y2": None
                })
            continue

        if not x_is_date and xv is None:
            nulls_excluded += 1
            continue

        pt = {
            "x": obs_d if is_cycle else (str(xv) if x_is_date else float(xv)),
            "y": float(yv),
            "date": obs_d
        }
        if y2_metric:
            pt["y2"] = float(y2v) if y2v is not None else None

        valid_points.append(pt)

    # Statistical computation (computed on primary X-Y)
    trend_spec = data.get("trend") or {}
    model = trend_spec.get("model")
    selected_band = trend_spec.get("band", "ci95" if trend_spec.get("showConfidenceBand", True) else "none")
    show_band = (selected_band != "none")

    trend_result = None
    trend_skipped_reason = None

    fit_points = [p for p in valid_points if p["y"] is not None and (x_is_date or p["x"] is not None)]

    if model in ("linear", "quadratic", "exponential", "power", "loess"):
        if len(fit_points) < 3:
            trend_skipped_reason = "Insufficient data points for trend fitting."
        else:
            if x_is_date:
                if is_cycle:
                    x_arr = np.array([valid_points.index(p) + (1.0 if model == "power" else 0.0) for p in fit_points], dtype=np.float64)
                    x_labels = [p["x"] for p in valid_points]
                else:
                    if model == "power":
                        x_arr = np.arange(1, len(fit_points) + 1, dtype=np.float64)
                    else:
                        x_arr = np.arange(len(fit_points), dtype=np.float64)
                    x_labels = [p["x"] for p in fit_points]
            else:
                x_arr = np.array([p["x"] for p in fit_points], dtype=np.float64)
                x_labels = None

            y_arr = np.array([p["y"] for p in fit_points], dtype=np.float64)
            trend_result, trend_skipped_reason = compute_ols(
                x_arr, y_arr, model=model, show_corridor=show_band, x_labels=x_labels
            )
            if trend_result:
                trend_result["selected_band"] = selected_band
                trend_result["showConfidenceBand"] = (selected_band in ("ci95", "ci99", "both_95"))
                trend_result["showPredictionBand"] = (selected_band in ("pi95", "both_95"))

    # Prepare response payload
    if y2_metric:
        serialized_data = [[p["x"], p["y"], p["date"], p.get("y2")] for p in valid_points]
    else:
        serialized_data = [[p["x"], p["y"], p["date"]] for p in valid_points]

    response_data = {
        "data": serialized_data,
        "trend": trend_result,
        "meta": {
            "points": len(fit_points),
            "nulls_excluded": nulls_excluded,
            "execution_ms": exec_ms,
            "x_metric": x_metric["name"] if not is_cycle else ("Hour of Day" if cycle_mode == "diurnal_24h" else ("Day of Week" if cycle_mode == "weekly_7d" else ("Calendar Month" if cycle_mode == "annual_12m" else "Cycle Day"))),
            "y_metric": y_metric["name"],
            "x_unit": x_metric["unit"] if not is_cycle else "",
            "y_unit": y_metric["unit"],
            "cycle_mode": cycle_mode,
            "dow_filter": dow_filter,
            "is_cycle": is_cycle,
        }
    }
    if y2_metric:
        response_data["meta"]["y2_metric"] = y2_metric["name"]
        response_data["meta"]["y2_unit"] = y2_metric["unit"]
        response_data["meta"]["has_y2"] = True

    if trend_skipped_reason:
        response_data["meta"]["trend_skipped_reason"] = trend_skipped_reason

    # Sanitize NaN/Inf (B4)
    sanitized = sanitize_floats(response_data)
    return jsonify(sanitized)
