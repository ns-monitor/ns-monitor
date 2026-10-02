"""
prepare.py -- 5-minute bucketing, deviation calculation, COB absorption,
and 4-way evidence classification matching AutotunePrep.kt.
"""
from typing import List, Dict, Any, Tuple
from autotune.models import GlucosePoint, Dose, PreparedPoint, Profile
from autotune.iob import calculate_total_iob_and_activity

def prepare_evidence(
    cgm_points: List[GlucosePoint],
    carbs_treatments: List[Dict[str, Any]],
    doses: List[Dose],
    tuned_profile: Profile,
    day_start_ms: int,
    day_end_ms: int,
    min_5m_carb_impact: float = 3.0
) -> Tuple[List[PreparedPoint], List[Dict[str, Any]], Dict[str, Any]]:
    """
    Process chronological 5-minute buckets for [day_start_ms, day_end_ms).
    Returns (prepared_points, cr_episodes, diagnostics).
    """
    # 1. Filter and sort CGM
    valid_cgm: List[GlucosePoint] = []
    cgm_points_sorted = sorted(cgm_points, key=lambda p: p.ts_ms)

    last_ts = -1
    excluded_low = 0
    for p in cgm_points_sorted:
        if p.sgv <= 39.0:
            excluded_low += 1
            continue
        if last_ts != -1 and (p.ts_ms - last_ts) < 120000: # < 2 min duplicate
            continue
        valid_cgm.append(p)
        last_ts = p.ts_ms

    # 2. Build 5-min bucket map
    step_ms = 5 * 60 * 1000
    bucket_map: Dict[int, float] = {}
    for p in valid_cgm:
        b_key = (p.ts_ms // step_ms) * step_ms
        bucket_map[b_key] = p.sgv

    # Carbs map: sum carbs by bucket
    carbs_map: Dict[int, float] = {}
    for c in carbs_treatments:
        ts = c.get("ts_ms")
        carbs = float(c.get("carbs") or 0.0)
        if ts and carbs > 0:
            b_key = (ts // step_ms) * step_ms
            carbs_map[b_key] = carbs_map.get(b_key, 0.0) + carbs

    prepared: List[PreparedPoint] = []
    cr_episodes: List[Dict[str, Any]] = []

    current_cob = 0.0
    uam_active = False
    meal_tail_active = False

    # CR Episode tracking
    active_episode: Dict[str, Any] = None

    # Step through every 5-min bucket in [day_start_ms, day_end_ms)
    t = day_start_ms
    while t < day_end_ms:
        if t in bucket_map:
            bg_now = bucket_map[t]

            # avg_delta = (bg_now - bg_20_min_later) / 4 (oref0 convention)
            # Find BG reading around 20 min later (15-25 min)
            bg_20 = None
            for offset_m in [20, 25, 15]:
                chk = t + offset_m * 60000
                if chk in bucket_map:
                    bg_20 = bucket_map[chk]
                    break

            if bg_20 is not None:
                # AndroidAPS convention: avg_delta = (bg_now - bg_20) / 4
                # Note: if BG is rising, bg_20 > bg_now, delta is negative?
                # AndroidAPS AutotunePrep.kt: avg_delta = (bg_now - bg_20m_ago) / 4 or (bg_now - bg_20m_later) / 4
                # In oref0: avg_delta is (BG_now - BG_about_20_min_ago) / 4.
                # However spec says: avg_delta = (BG_now - BG_about_20_min_later) / 4 or delta per 5m.
                # Let's check: if bg rises by 20 mg/dL over 20 min, avg_delta = +5 mg/dL.
                avg_delta = (bg_now - bg_20) / 4.0
                # If bg_20 was later, then (bg_20 - bg_now) is the forward slope.
                # Let's align forward rate: (bg_20 - bg_now) / 4.0
                avg_delta = (bg_20 - bg_now) / 4.0
            else:
                # Fallback to previous 5m delta if forward reading is missing
                prev_bg = bucket_map.get(t - step_ms)
                avg_delta = (bg_now - prev_bg) if prev_bg is not None else 0.0

            # Insulin activity and IOB
            iob, act = calculate_total_iob_and_activity(
                doses, t, tuned_profile.dia, tuned_profile.peak
            )

            # BGI = - insulin_activity * ISF * 5 min
            bgi = - act * tuned_profile.isf_mgdl * 5.0
            deviation = avg_delta - bgi

            # Positive deviations forced to 0 below 80 mg/dL
            if bg_now < 80.0 and deviation > 0.0:
                deviation = 0.0

            # Carbs on board & absorption
            new_carbs = carbs_map.get(t, 0.0)
            if new_carbs > 0:
                current_cob += new_carbs
                if active_episode is None:
                    active_episode = {
                        "start_ts": t,
                        "start_bg": bg_now,
                        "start_iob": iob,
                        "carbs": new_carbs,
                        "dosed_insulin": 0.0
                    }
                else:
                    active_episode["carbs"] += new_carbs

            if current_cob > 0:
                carb_impact = max(deviation, min_5m_carb_impact)
                absorbed_g = carb_impact * tuned_profile.carb_ratio / tuned_profile.isf_mgdl
                absorbed_g = max(0.0, absorbed_g)
                current_cob = max(0.0, current_cob - absorbed_g)

            # Hour basal rate
            from autotune.normalise import get_basal_rate_at_time
            current_basal = get_basal_rate_at_time(tuned_profile, t)
            basal_bgi = current_basal * tuned_profile.isf_mgdl / 60.0 * 5.0

            # Classification
            classification = "ISF"
            if current_cob > 0:
                classification = "CSF_MEAL"
                meal_tail_active = True
            elif meal_tail_active and deviation > 0.0:
                classification = "CSF_MEAL"
            else:
                meal_tail_active = False

                # UAM check
                is_uam = (iob > 2.0 * current_basal) or (deviation > 6.0) or uam_active
                if is_uam:
                    classification = "UAM"
                    uam_active = (deviation > 0.0)
                else:
                    uam_active = False
                    # Basal check
                    is_basal = (basal_bgi > -4.0 * bgi) or (avg_delta > 0.0 and avg_delta > -2.0 * bgi)
                    if is_basal:
                        classification = "BASAL"
                    else:
                        classification = "ISF"

            # Accumulate dosed insulin for CR episode
            if active_episode is not None:
                # Add discrete boluses delivered in this 5m bucket
                bucket_doses = [d.amount for d in doses if d.dose_type in ("bolus", "smb") and t <= d.ts_ms < t + step_ms]
                active_episode["dosed_insulin"] += sum(bucket_doses)

                # Check episode close condition: COB == 0 and IOB <= current_basal / 2
                if current_cob <= 0 and iob <= current_basal / 2.0:
                    active_episode["end_ts"] = t
                    active_episode["end_bg"] = bg_now
                    active_episode["end_iob"] = iob
                    duration_min = (t - active_episode["start_ts"]) / 60000.0
                    if duration_min >= 60.0:
                        cr_episodes.append(active_episode)
                    active_episode = None

            prepared.append(PreparedPoint(
                ts_ms=t,
                sgv=bg_now,
                avg_delta=avg_delta,
                bgi=bgi,
                deviation=deviation,
                cob=current_cob,
                iob=iob,
                activity=act,
                classification=classification
            ))

        t += step_ms

    # UAM policy: if >= 12 meal points exist, move UAM to Basal
    meal_count = sum(1 for p in prepared if p.classification == "CSF_MEAL")
    uam_count = sum(1 for p in prepared if p.classification == "UAM")
    if meal_count >= 12:
        for p in prepared:
            if p.classification == "UAM":
                p.classification = "BASAL"
    elif uam_count > 0:
        # Keep lowest half of deviations when UAM dominates
        uam_points = [p for p in prepared if p.classification == "UAM"]
        sorted_devs = sorted(p.deviation for p in uam_points)
        median_dev = sorted_devs[len(sorted_devs) // 2]
        for p in prepared:
            if p.classification == "UAM" and p.deviation > median_dev:
                p.classification = "ISF"

    diagnostics = {
        "total_bucket_points": len(prepared),
        "excluded_low": excluded_low,
        "meal_count": sum(1 for p in prepared if p.classification == "CSF_MEAL"),
        "uam_count": sum(1 for p in prepared if p.classification == "UAM"),
        "basal_count": sum(1 for p in prepared if p.classification == "BASAL"),
        "isf_count": sum(1 for p in prepared if p.classification == "ISF"),
        "cr_episodes_found": len(cr_episodes)
    }

    return prepared, cr_episodes, diagnostics
