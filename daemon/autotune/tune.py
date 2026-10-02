"""
tune.py -- Core daily sequential tuning algorithm.
Executes in strict order: Basal -> Carb Ratio -> ISF.
Enforces immutable pump_profile caps and damping factors matching AutotuneCore.kt.
"""
from typing import List, Dict, Any, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
import statistics
from autotune.models import Profile, PreparedPoint

def tune_day(
    prepared_points: List[PreparedPoint],
    cr_episodes: List[Dict[str, Any]],
    current_profile: Profile,
    pump_profile: Profile,
    min_factor: float = 0.70,
    max_factor: float = 1.20
) -> Tuple[Profile, Dict[str, Any]]:
    """
    Tune one 04:00-to-04:00 day. Returns (tuned_profile, day_metrics).
    """
    tuned = current_profile.copy()
    tz = ZoneInfo(current_profile.timezone_name)

    # -------------------------------------------------------------
    # 1. BASAL TUNING (Per clock hour)
    # -------------------------------------------------------------
    # Group basal points by clock hour (0..23)
    hourly_devs: Dict[int, List[float]] = {h: [] for h in range(24)}
    for p in prepared_points:
        if p.classification == "BASAL":
            dt = datetime.fromtimestamp(p.ts_ms / 1000.0, tz=tz)
            hourly_devs[dt.hour].append(p.deviation)

    new_basal = list(current_profile.basal)
    hours_adjusted = set()

    for h in range(24):
        devs = hourly_devs[h]
        if not devs:
            continue

        dev_sum = sum(devs)
        # basal_needed in U/h
        basal_needed = round(0.2 * dev_sum / current_profile.isf_mgdl, 2)
        if abs(basal_needed) < 0.01:
            continue

        # Target 3 preceding hours: (h-3)%24, (h-2)%24, (h-1)%24
        preceding = [(h - 3) % 24, (h - 2) % 24, (h - 1) % 24]
        for ph in preceding:
            hours_adjusted.add(ph)

        if basal_needed > 0:
            add_per_hour = basal_needed / 3.0
            for ph in preceding:
                new_basal[ph] += add_per_hour
        else:
            three_hour_sum = sum(new_basal[ph] for ph in preceding)
            if three_hour_sum > 0:
                scale = 1.0 + (basal_needed / three_hour_sum)
                scale = max(0.2, scale) # defensive lower scale bound
                for ph in preceding:
                    new_basal[ph] *= scale

    # Apply safety caps relative to immutable pump_profile
    for h in range(24):
        min_cap = pump_profile.basal[h] * min_factor
        max_cap = pump_profile.basal[h] * max_factor
        new_basal[h] = max(min_cap, min(max_cap, new_basal[h]))

    # Smooth unchanged hours
    final_basal = list(new_basal)
    for h in range(24):
        if h not in hours_adjusted:
            prev_h = (h - 1) % 24
            next_h = (h + 1) % 24
            smoothed = 0.8 * pump_profile.basal[h] + 0.1 * new_basal[prev_h] + 0.1 * new_basal[next_h]
            min_cap = pump_profile.basal[h] * min_factor
            max_cap = pump_profile.basal[h] * max_factor
            final_basal[h] = max(min_cap, min(max_cap, smoothed))

    tuned.basal = [round(b, 3) for b in final_basal]

    # -------------------------------------------------------------
    # 2. CARB RATIO (CR) & CSF TUNING
    # -------------------------------------------------------------
    total_carbs = 0.0
    total_episode_insulin = 0.0

    for ep in cr_episodes:
        start_bg = ep.get("start_bg", 100.0)
        end_bg = ep.get("end_bg", 100.0)
        start_iob = ep.get("start_iob", 0.0)
        dosed = ep.get("dosed_insulin", 0.0)
        carbs = ep.get("carbs", 0.0)

        insulin_needed_for_bg = (end_bg - start_bg) / current_profile.isf_mgdl
        ep_insulin = start_iob + dosed + insulin_needed_for_bg
        if ep_insulin > 0 and carbs > 0:
            total_carbs += carbs
            total_episode_insulin += ep_insulin

    if total_episode_insulin > 0 and total_carbs > 0:
        full_new_cr = total_carbs / total_episode_insulin
        min_cr = max(3.0, pump_profile.carb_ratio * min_factor)
        max_cr = min(150.0, pump_profile.carb_ratio * max_factor)
        capped_full_new_cr = max(min_cr, min(max_cr, full_new_cr))
        # Damping: 0.8 current + 0.2 new
        tuned.carb_ratio = round(0.8 * current_profile.carb_ratio + 0.2 * capped_full_new_cr, 2)
    else:
        tuned.carb_ratio = current_profile.carb_ratio

    # Diagnostic CSF calculation
    meal_points = [p for p in prepared_points if p.classification == "CSF_MEAL"]
    meal_dev_sum = sum(p.deviation for p in meal_points)
    all_carbs = sum(ep.get("carbs", 0.0) for ep in cr_episodes)
    if all_carbs > 0:
        full_new_csf = meal_dev_sum / all_carbs
        damped_csf = 0.8 * current_profile.csf_mgdl + 0.2 * full_new_csf
        min_csf = pump_profile.csf_mgdl * min_factor
        max_csf = pump_profile.csf_mgdl * max_factor
        diagnostic_csf = max(min_csf, min(max_csf, damped_csf))
    else:
        diagnostic_csf = current_profile.csf_mgdl

    # -------------------------------------------------------------
    # 3. ISF TUNING
    # -------------------------------------------------------------
    isf_points = [p for p in prepared_points if p.classification == "ISF" and abs(p.bgi) > 0.001]

    if len(isf_points) >= 10:
        ratios = [1.0 + (p.deviation / p.bgi) for p in isf_points]
        med_ratio = statistics.median(ratios)
        full_new_isf = current_profile.isf_mgdl * med_ratio

        # Capping bounds against pump_profile
        # Note inverted bounds: min_ISF = pump_ISF / max_factor, max_ISF = pump_ISF / min_factor
        min_isf = pump_profile.isf_mgdl / max_factor
        max_isf = pump_profile.isf_mgdl / min_factor
        capped_full_new_isf = max(min_isf, min(max_isf, full_new_isf))

        # Damping: 0.8 current + 0.2 new
        damped_isf = 0.8 * current_profile.isf_mgdl + 0.2 * capped_full_new_isf
        tuned.isf_mgdl = round(max(min_isf, min(max_isf, damped_isf)), 1)
    else:
        tuned.isf_mgdl = current_profile.isf_mgdl

    day_metrics = {
        "hours_adjusted_count": len(hours_adjusted),
        "cr_episodes_counted": len([ep for ep in cr_episodes if ep.get("carbs", 0) > 0]),
        "isf_points_count": len(isf_points),
        "diagnostic_csf_mgdl": round(diagnostic_csf, 2),
        "diagnostic_csf_mmol": round(diagnostic_csf / 18.0182, 3),
        "tuned_total_basal": tuned.total_daily_basal,
        "tuned_isf_mmol": tuned.isf_mmol,
        "tuned_cr": tuned.carb_ratio
    }

    return tuned, day_metrics
