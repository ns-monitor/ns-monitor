"""Pure numerical engine for the Sandbox empirical periodicity explorer.

Performs deterministic empirical periodicity analysis:
- Validation of observations and non-zero variance
- Mean-preserved ordinary least squares (OLS) linear detrending
- Continuous Lomb-Scargle periodogram across 450 log-spaced periods (2.0 to 400.0 days)
- Exact effective-trial False Alarm Probability (FAP) ceilings (95% and 99%)
- Phase-folding with tie-corrected Kruskal-Wallis non-parametric significance
- Scout needle telemetry (interpolated power, resonance score, gradient, nearest peak)
"""

import math
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
from scipy import stats

MIN_PERIOD: float = 2.0
MAX_PERIOD: float = 400.0
STEPS: int = 450


def _to_float(x: Any) -> Optional[float]:
    """Safely cast value to float or return None."""
    if x is None:
        return None
    try:
        val = float(x)
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None


def paired(records: List[Dict[str, Any]], metric: str) -> List[Tuple[float, float, str]]:
    """Extract (elapsed_days, value, date_str) observations from daily records.
    
    Elapsed days are calculated relative to the first date in the dataset,
    ensuring calendar gaps (e.g. missing Day 42) leave the subsequent observation
    at elapsed day 43 rather than shifting array positions.
    """
    if not records:
        return []
    origin = np.datetime64(records[0]["date"])
    out = []
    for r in records:
        v = _to_float(r.get(metric))
        if v is not None:
            day_dt = np.datetime64(r["date"])
            elapsed = float((day_dt - origin).astype("timedelta64[D]").astype(int))
            out.append((elapsed, v, str(r["date"])))
    return out


def detrend(t: np.ndarray, y: np.ndarray, enabled: bool) -> Tuple[np.ndarray, float, float]:
    """Fit OLS against elapsed days.
    
    When enabled, applies mean-preserved adjustment:
        adjusted = raw - fitted_OLS_trend + raw_mean
    This preserves the clinical units and baseline while eliminating secular drift.
    When disabled, returns a copy of raw values with zero slope.
    """
    if len(y) == 0:
        return y.copy(), 0.0, 0.0
    raw_mean = float(np.mean(y))
    if not enabled or len(y) < 2:
        return y.copy(), 0.0, raw_mean
    
    # If all values identical or time has no variance, return unchanged
    if np.all(y == y[0]) or np.all(t == t[0]):
        return y.copy(), 0.0, raw_mean

    slope, intercept = np.polyfit(t, y, 1)
    adjusted = y - (intercept + slope * t) + raw_mean
    return adjusted, float(slope), float(intercept)


def spectrum(t: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float, float, List[Dict[str, Any]]]:
    """Compute 450 log-spaced Lomb-Scargle periodogram powers from 2.0 to 400.0 days.
    
    Uses time-shift formulation:
        tau = atan2(sum(sin(2*w*t)), sum(cos(2*w*t))) / (2*w)
    and evaluates normalized power.
    FAP ceilings are computed using:
        N_eff = max(10, min(450, round(1.193 * N)))
        Z_95 = -ln(1 - (1 - 0.05)**(1 / N_eff))
        Z_99 = -ln(1 - (1 - 0.01)**(1 / N_eff))
    """
    n = len(y)
    periods = np.logspace(math.log10(MIN_PERIOD), math.log10(MAX_PERIOD), STEPS)
    if n < 2:
        return periods, np.zeros(STEPS), 0.0, 0.0, []

    mean = float(np.mean(y))
    var = float(np.var(y, ddof=1))
    if var <= 1e-12:
        return periods, np.zeros(STEPS), 0.0, 0.0, []

    yc = y - mean
    powers = []
    for p in periods:
        w = 2.0 * math.pi / p
        two_w_t = 2.0 * w * t
        s_sum = float(np.sin(two_w_t).sum())
        c_sum = float(np.cos(two_w_t).sum())
        tau = math.atan2(s_sum, c_sum) / (2.0 * w)
        
        arg = w * (t - tau)
        c = np.cos(arg)
        s = np.sin(arg)
        
        c_dot_yc = float(yc @ c)
        s_dot_yc = float(yc @ s)
        c_dot_c = float(c @ c)
        s_dot_s = float(s @ s)
        
        term_c = (c_dot_yc ** 2) / max(c_dot_c, 1e-12)
        term_s = (s_dot_yc ** 2) / max(s_dot_s, 1e-12)
        power = (term_c + term_s) / (2.0 * var)
        powers.append(power)

    powers_arr = np.asarray(powers, dtype=float)
    
    # Effective trials and FAP ceilings
    ne = max(10.0, min(float(STEPS), round(1.193 * n)))
    z95 = -math.log(1.0 - (1.0 - 0.05) ** (1.0 / ne))
    z99 = -math.log(1.0 - (1.0 - 0.01) ** (1.0 / ne))

    # Detect local maxima exceeding the 95% FAP threshold
    peaks = []
    for i in range(1, STEPS - 1):
        if powers_arr[i] > powers_arr[i - 1] and powers_arr[i] > powers_arr[i + 1]:
            if powers_arr[i] >= z95:
                peaks.append({
                    "period": round(float(periods[i]), 3),
                    "power": round(float(powers_arr[i]), 5),
                    "is_sig99": bool(powers_arr[i] >= z99),
                })
    peaks.sort(key=lambda x: x["power"], reverse=True)

    return periods, powers_arr, float(z95), float(z99), peaks


def fold(t: np.ndarray, y: np.ndarray, period: float) -> Dict[str, Any]:
    """Fold time series into phase bins for a given candidate period.
    
    Bin count: min(14, max(6, round(P))).
    Calculates phase medians, Q1, Q3, spread, and Kruskal-Wallis H and p-value.
    Empty bins are safely excluded; degenerate configurations return H=0, df=0, p=1.
    """
    k = min(14, max(6, int(round(period))))
    bins: List[List[float]] = [[] for _ in range(k)]

    for ti, yi in zip(t, y):
        # Clamped phase bin index in [0, k-1]
        phase = (ti % period) / period
        b_idx = min(k - 1, max(0, int(phase * k)))
        bins[b_idx].append(float(yi))

    bin_results = []
    populated_bins = [b for b in bins if len(b) > 0]
    medians: List[Optional[float]] = []

    for b in bins:
        if b:
            q1 = float(np.percentile(b, 25))
            med = float(np.median(b))
            q3 = float(np.percentile(b, 75))
            bin_results.append({"q1": q1, "median": med, "q3": q3, "count": len(b)})
            medians.append(med)
        else:
            bin_results.append({"q1": None, "median": None, "q3": None, "count": 0})
            medians.append(None)

    valid_meds = [m for m in medians if m is not None]
    spread = float(max(valid_meds) - min(valid_meds)) if valid_meds else 0.0

    # Non-parametric Kruskal-Wallis test across populated bins
    h, p_val = 0.0, 1.0
    is_valid_kw = False
    if len(populated_bins) >= 2:
        concatenated = np.concatenate(populated_bins)
        if len(set(concatenated)) > 1:
            try:
                stat_h, stat_p = stats.kruskal(*populated_bins)
                if math.isfinite(stat_h) and math.isfinite(stat_p):
                    h, p_val = float(stat_h), float(stat_p)
                    is_valid_kw = True
            except ValueError:
                h, p_val = 0.0, 1.0

    df = (len(populated_bins) - 1) if is_valid_kw else 0
    cycles = len(y) / period if period > 0 else 0.0

    return {
        "bins": bin_results,
        "spread": spread,
        "h": h,
        "df": df,
        "p_value": p_val,
        "cycles": cycles,
        "k": k,
    }


def analyse(
    records: List[Dict[str, Any]],
    metric: str,
    detrended: bool,
    period: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute complete server-side analysis pipeline for the active metric and period."""
    obs = paired(records, metric)
    if len(obs) < 15:
        return {
            "error": "At least 15 valid daily observations are required.",
            "valid_count": len(obs),
        }

    t = np.asarray([x[0] for x in obs], dtype=float)
    raw = np.asarray([x[1] for x in obs], dtype=float)
    
    # Detrend series if requested
    y, slope, intercept = detrend(t, raw, detrended)

    # Compute continuous periodogram
    periods, powers, z95, z99, peaks = spectrum(t, y)

    # Fallback to dominant peak or 14.0 days
    if period is None:
        period = float(peaks[0]["period"]) if peaks else 14.0
    period = float(min(MAX_PERIOD, max(MIN_PERIOD, period)))

    # Scout needle telemetry
    power_at_period = float(np.interp(period, periods, powers))
    nearest_peak = min(peaks, key=lambda p: abs(period - p["period"])) if peaks else None
    peak_dist = abs(period - nearest_peak["period"]) if nearest_peak else None

    # Coherence normalization denominator
    max_peak_power = max((p["power"] for p in peaks), default=0.0)
    denom = max(max_peak_power, 1.5 * z99, 1e-12)
    score = int(max(5, min(100, round((power_at_period / denom) * 100))))

    # Local gradient (P ± 0.2)
    grad_p = float(np.interp(min(MAX_PERIOD, period + 0.2), periods, powers))
    grad_m = float(np.interp(max(MIN_PERIOD, period - 0.2), periods, powers))
    gradient = grad_p - grad_m

    # Phase fold calculation
    fold_res = fold(t, y, period)

    # Annual raster percentile scaling
    lo, hi = float(np.percentile(y, 3)), float(np.percentile(y, 97))
    denominator = max(1e-6, hi - lo)

    # Build display series
    series = [{"date": d_str, "value": float(val)} for (elapsed, val, d_str) in obs]

    return {
        "records": records,
        "valid_count": len(obs),
        "metric": metric,
        "detrended": detrended,
        "trend": {"slope": slope, "intercept": intercept},
        "series": series,
        "spectrum": {
            "periods": periods.round(4).tolist(),
            "powers": powers.round(6).tolist(),
            "z95": z95,
            "z99": z99,
            "peaks": peaks,
        },
        "period": period,
        "fold": fold_res,
        "telemetry": {
            "power": power_at_period,
            "score": score,
            "gradient": gradient,
            "nearest_peak": nearest_peak,
            "distance": peak_dist,
            "is_sig99": bool(power_at_period >= z99),
            "is_sig95": bool(power_at_period >= z95),
        },
        "raster": {
            "p3": lo,
            "p97": hi,
            "denominator": denominator,
        },
    }
