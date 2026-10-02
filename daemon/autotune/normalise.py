"""
normalise.py -- Insulin delivery normalisation.
Converts boluses, SMBs, extended boluses, and temp basals into discrete doses
and 5-minute pseudo-boluses matching AutotuneIob.kt.
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from autotune.models import Dose, Profile, TempBasalSegment

def get_basal_rate_at_time(profile: Profile, ts_ms: int) -> float:
    """Return the profile basal rate for the given timestamp in local time."""
    tz = ZoneInfo(profile.timezone_name)
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=tz)
    hour = dt.hour
    return profile.basal[hour]

def normalise_treatments(
    treatments: List[Dict[str, Any]],
    start_ms: int,
    end_ms: int,
    tuned_profile: Profile,
    pump_profile: Profile
) -> List[Dose]:
    """
    Process all treatments inside [start_ms, end_ms) into discrete Doses.
    start_ms includes the (DIA + 2h) history lookback.
    """
    doses: List[Dose] = []
    temp_basals: List[TempBasalSegment] = []

    for t in treatments:
        ts_ms = t.get("ts_ms")
        if ts_ms is None:
            continue

        evt = (t.get("event_type") or "").lower()
        insulin = float(t.get("insulin") or 0.0)

        # Standard Boluses and SMBs
        if insulin > 0:
            if t.get("smb_flag") or "smb" in evt:
                doses.append(Dose(ts_ms=ts_ms, amount=insulin, dose_type="smb"))
            else:
                doses.append(Dose(ts_ms=ts_ms, amount=insulin, dose_type="bolus"))

        # Extended Bolus / Combo Bolus
        duration = float(t.get("duration") or 0.0)
        if duration > 0 and "extended" in evt and insulin > 0:
            # Split extended bolus into ~5-min pseudo doses
            interval_min = 5.0
            num_steps = max(1, int(round(duration / interval_min)))
            step_u = insulin / num_steps
            for step in range(num_steps):
                step_ts = ts_ms + int((step + 0.5) * interval_min * 60000)
                if start_ms <= step_ts < end_ms:
                    doses.append(Dose(ts_ms=step_ts, amount=step_u, dose_type="pseudo_bolus"))

        # Temp Basal records
        rate = t.get("rate")
        if rate is None and t.get("absolute") is not None:
            rate = float(t.get("absolute"))

        if rate is not None and duration > 0 and ("temp basal" in evt or "temporary" in evt):
            temp_basals.append(TempBasalSegment(
                ts_ms=ts_ms,
                duration_min=duration,
                rate=float(rate)
            ))

    # Sort temp basals chronologically
    temp_basals.sort(key=lambda x: x.ts_ms)

    # 5-minute bucket grid for basal delivery from start_ms to end_ms
    step_ms = 5 * 60 * 1000
    current_bucket = (start_ms // step_ms) * step_ms

    tb_idx = 0
    num_tbs = len(temp_basals)

    while current_bucket < end_ms:
        bucket_mid_ms = current_bucket + step_ms // 2

        # Find active temp basal covering bucket_mid_ms
        actual_rate = None
        while tb_idx < num_tbs:
            tb = temp_basals[tb_idx]
            tb_end = tb.ts_ms + int(tb.duration_min * 60000)
            if tb.ts_ms <= bucket_mid_ms < tb_end:
                actual_rate = tb.rate
                break
            elif bucket_mid_ms >= tb_end:
                tb_idx += 1
            else:
                break

        # If no active temp basal, fallback to synthetic 100% scheduled pump basal
        if actual_rate is None:
            actual_rate = get_basal_rate_at_time(pump_profile, bucket_mid_ms)

        # Baseline comparison rate is tuned_profile at this time
        tuned_rate = get_basal_rate_at_time(tuned_profile, bucket_mid_ms)
        net_rate = actual_rate - tuned_rate

        # Pseudo-bolus for 5 minutes
        pseudo_u = net_rate * (5.0 / 60.0)
        if abs(pseudo_u) >= 0.001:
            doses.append(Dose(
                ts_ms=bucket_mid_ms,
                amount=pseudo_u,
                dose_type="pseudo_bolus",
                duration_min=5.0
            ))

        current_bucket += step_ms

    doses.sort(key=lambda d: d.ts_ms)
    return doses
