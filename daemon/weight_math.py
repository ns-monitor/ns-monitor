import bisect
import datetime
import math
from typing import Dict, List, Optional, Tuple, Union


def sanitize_anchors(raw_anchors: List[Tuple[Union[datetime.date, str], Union[float, int, str]]]) -> List[Tuple[datetime.date, float]]:
    """
    Cleans and sorts weight anchors:
    - Parses date if given as string
    - Filters out null, non-numeric, non-finite, and non-positive (<= 0) weights
    - Averages same-day duplicate notes
    - Sorts chronologically by date
    """
    by_date: Dict[datetime.date, List[float]] = {}
    for item in raw_anchors:
        if isinstance(item, dict):
            d = item.get("date") or item.get("date_str") or item.get("Date")
            w = item.get("weight_kg") or item.get("weight")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            d, w = item[0], item[1]
        else:
            continue

        if d is None or w is None:
            continue
        # Convert date if string
        if isinstance(d, str):
            try:
                d = datetime.date.fromisoformat(d.strip())
            except Exception:
                continue
        elif isinstance(d, datetime.datetime):
            d = d.date()
        elif not isinstance(d, datetime.date):
            continue

        # Parse weight float
        try:
            val = float(w)
            if not math.isfinite(val) or val <= 0:
                continue
        except (ValueError, TypeError):
            continue

        if d not in by_date:
            by_date[d] = []
        by_date[d].append(val)

    # Average same-day duplicates and sort
    cleaned = []
    for d in sorted(by_date.keys()):
        avg_w = sum(by_date[d]) / len(by_date[d])
        cleaned.append((d, avg_w))
    return cleaned


def interpolate_weight(target_date: Union[datetime.date, datetime.datetime, str],
                       anchors: List[Tuple[datetime.date, float]]) -> Optional[float]:
    """
    Straight-line interpolation of weight between known anchor points:
    1. Before first anchor: returns None (no historical extrapolation before first entry).
    2. Exact anchor: returns exact weight.
    3. Between anchors: straight line interpolation (unrounded float).
    4. After latest anchor: flat forward-fill of latest known weight.
    5. Single anchor: returns that weight for dates >= anchor, None for dates < anchor.
    6. Zero anchors: returns None.
    """
    if not anchors:
        return None

    if isinstance(target_date, str):
        try:
            target_date = datetime.date.fromisoformat(target_date.strip())
        except Exception:
            return None
    elif isinstance(target_date, datetime.datetime):
        target_date = target_date.date()

    if target_date < anchors[0][0]:
        return None

    if target_date >= anchors[-1][0]:
        return anchors[-1][1]

    dates = [a[0] for a in anchors]
    idx = bisect.bisect_right(dates, target_date)
    d0, w0 = anchors[idx - 1]
    d1, w1 = anchors[idx]

    total_days = (d1 - d0).days
    if total_days <= 0:
        return w0

    elapsed_days = (target_date - d0).days
    ratio = elapsed_days / total_days
    return w0 + ratio * (w1 - w0)


def attach_tdd_weight_daily(rows: List[Dict], anchors: List[Tuple[datetime.date, float]]) -> List[Dict]:
    """
    Attaches tdd_weight and all moving average variants (tdd_weight_7d .. 90d)
    to daily trends rows. Guarantees all six keys are present on every row.
    """
    cleaned_anchors = sanitize_anchors(anchors)
    mavg_keys = ["7d", "14d", "30d", "60d", "90d"]

    for row in rows:
        raw_date = row.get("Date") or row.get("date")
        row_date = None
        if raw_date:
            try:
                row_date = datetime.date.fromisoformat(str(raw_date)[:10])
            except Exception:
                row_date = None

        w = interpolate_weight(row_date, cleaned_anchors) if row_date else None

        # Daily ratio
        tdd_val = row.get("tdd")
        if w is not None and w > 0 and tdd_val is not None:
            try:
                row["tdd_weight"] = round(float(tdd_val) / w, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                row["tdd_weight"] = None
        else:
            row["tdd_weight"] = None

        # MAVG ratios
        for mk in mavg_keys:
            in_key = f"tdd_{mk}"
            out_key = f"tdd_weight_{mk}"
            m_val = row.get(in_key)
            if w is not None and w > 0 and m_val is not None:
                try:
                    row[out_key] = round(float(m_val) / w, 2)
                except (ValueError, TypeError, ZeroDivisionError):
                    row[out_key] = None
            else:
                row[out_key] = None

    return rows


def attach_tdd_weight_monthly(monthly_rows: List[Dict],
                             daily_tdd_samples: List[Tuple[datetime.date, float]],
                             anchors: List[Tuple[datetime.date, float]]) -> List[Dict]:
    """
    Attaches tdd_weight to monthly aggregated rows using the mean interpolated weight
    sampled exclusively across the days that contributed non-null TDD to that month.
    All moving average keys (tdd_weight_7d .. 90d) are guaranteed to be present as None.
    """
    cleaned_anchors = sanitize_anchors(anchors)
    mavg_keys = ["7d", "14d", "30d", "60d", "90d"]

    # Group valid daily weights by month bucket YYYY-MM
    month_weights: Dict[str, List[float]] = {}
    for item in daily_tdd_samples:
        if isinstance(item, dict):
            raw_d = item.get("date") or item.get("date_str") or item.get("Date")
            tdd_val = item.get("tdd")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            raw_d, tdd_val = item[0], item[1]
        else:
            continue

        if raw_d is None or tdd_val is None:
            continue
        d_obj = raw_d if isinstance(raw_d, datetime.date) else datetime.date.fromisoformat(str(raw_d)[:10])
        w = interpolate_weight(d_obj, cleaned_anchors)
        if w is not None and w > 0:
            m_str = d_obj.strftime("%Y-%m")
            if m_str not in month_weights:
                month_weights[m_str] = []
            month_weights[m_str].append(w)

    mean_month_weights = {
        m: (sum(w_list) / len(w_list)) for m, w_list in month_weights.items() if w_list
    }

    for row in monthly_rows:
        month_key = str(row.get("Date") or row.get("date") or "")[:7]
        monthly_tdd = row.get("tdd")
        mean_w = mean_month_weights.get(month_key)

        if monthly_tdd is not None and mean_w is not None and mean_w > 0:
            try:
                row["tdd_weight"] = round(float(monthly_tdd) / mean_w, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                row["tdd_weight"] = None
        else:
            row["tdd_weight"] = None

        # In monthly mode, all moving average keys are explicitly present with value None
        for mk in mavg_keys:
            row[f"tdd_weight_{mk}"] = None

    return monthly_rows
