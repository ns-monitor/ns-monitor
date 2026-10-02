"""
iob.py -- Bilinear exponential insulin activity and IOB curves.
Direct parity with AndroidAPS LocalInsulin.kt and upstream dev ICfg safety floors.
"""
import math
from typing import List, Tuple
from autotune.models import Dose

def calculate_iob_and_activity(dose_u: float, t_min: float, dia_hours: float, peak_min: float) -> Tuple[float, float]:
    """
    Compute IOB (Units) and activity (U/min) for a dose at elapsed time t_min.
    """
    if dose_u <= 0 or t_min < 0:
        return 0.0, 0.0

    # Defensive floors from AndroidAPS dev
    dia = max(5.0, float(dia_hours))
    td = dia * 60.0
    tp = max(1.0, min(float(peak_min), td / 2.0 - 1.0))

    if t_min >= td:
        return 0.0, 0.0

    tau = tp * (1.0 - tp / td) / (1.0 - 2.0 * tp / td)
    a = 2.0 * tau / td
    s = 1.0 / (1.0 - a + (1.0 + a) * math.exp(-td / tau))

    activity = dose_u * (s / (tau ** 2)) * t_min * (1.0 - t_min / td) * math.exp(-t_min / tau)
    iob = dose_u * (1.0 - s * (1.0 - a) * (
        ((t_min ** 2) / (tau * td * (1.0 - a)) - t_min / tau - 1.0) * math.exp(-t_min / tau) + 1.0
    ))

    return max(0.0, float(iob)), max(0.0, float(activity))


def calculate_total_iob_and_activity(
    doses: List[Dose],
    current_time_ms: int,
    dia_hours: float,
    peak_min: float
) -> Tuple[float, float]:
    """
    Sum active IOB and activity over all doses within the DIA lookback window.
    """
    total_iob = 0.0
    total_activity = 0.0
    cutoff_ms = current_time_ms - int(dia_hours * 3600 * 1000)

    for d in doses:
        if d.ts_ms > current_time_ms or d.ts_ms < cutoff_ms:
            continue
        t_min = (current_time_ms - d.ts_ms) / 60000.0
        iob, act = calculate_iob_and_activity(d.amount, t_min, dia_hours, peak_min)
        total_iob += iob
        total_activity += act

    return total_iob, total_activity
