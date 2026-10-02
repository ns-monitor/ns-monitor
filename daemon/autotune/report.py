"""
report.py -- Formats comparison scorecards, 24h hourly profiles,
progression tables, JSON diffs, and Markdown reports.
"""
from typing import List, Dict, Any
from autotune.models import Profile, HourlyBasalComparison, DayClassificationStats, MMOL_TO_MGDL

def generate_report(
    pump_profile: Profile,
    tuned_profile: Profile,
    day_stats: List[DayClassificationStats],
    hourly_devs_all_days: Dict[int, float],
    query_archive: Dict[str, Any],
    min_factor: float = 0.70,
    max_factor: float = 1.20
) -> Dict[str, Any]:
    """Compile complete Autotune report payload for the UI and exports."""
    # 1. Summary comparison
    basal_delta = round(tuned_profile.total_daily_basal - pump_profile.total_daily_basal, 2)
    basal_pct = round((basal_delta / pump_profile.total_daily_basal) * 100.0, 1) if pump_profile.total_daily_basal > 0 else 0.0

    isf_delta_mmol = round(tuned_profile.isf_mmol - pump_profile.isf_mmol, 2)
    isf_pct = round((isf_delta_mmol / pump_profile.isf_mmol) * 100.0, 1) if pump_profile.isf_mmol > 0 else 0.0

    cr_delta = round(tuned_profile.carb_ratio - pump_profile.carb_ratio, 2)
    cr_pct = round((cr_delta / pump_profile.carb_ratio) * 100.0, 1) if pump_profile.carb_ratio > 0 else 0.0

    summary = {
        "basal": {
            "baseline": pump_profile.total_daily_basal,
            "tuned": tuned_profile.total_daily_basal,
            "delta": basal_delta,
            "pct_change": basal_pct,
            "unit": "U/day"
        },
        "isf": {
            "baseline_mmol": pump_profile.isf_mmol,
            "tuned_mmol": tuned_profile.isf_mmol,
            "delta_mmol": isf_delta_mmol,
            "pct_change": isf_pct,
            "baseline_mgdl": round(pump_profile.isf_mgdl, 1),
            "tuned_mgdl": round(tuned_profile.isf_mgdl, 1),
            "unit": "mmol/L/U"
        },
        "carb_ratio": {
            "baseline": pump_profile.carb_ratio,
            "tuned": tuned_profile.carb_ratio,
            "delta": cr_delta,
            "pct_change": cr_pct,
            "unit": "g/U"
        },
        "csf": {
            "baseline_mmol": pump_profile.csf_mmol,
            "tuned_mmol": tuned_profile.csf_mmol,
            "baseline_mgdl": pump_profile.csf_mgdl,
            "tuned_mgdl": tuned_profile.csf_mgdl,
            "unit": "mmol/L/g"
        }
    }

    # 2. 24 Hourly comparisons
    hourly: List[Dict[str, Any]] = []
    for h in range(24):
        p_rate = pump_profile.basal[h]
        t_rate = tuned_profile.basal[h]
        delta = round(t_rate - p_rate, 3)
        pct = round((delta / p_rate) * 100.0, 1) if p_rate > 0 else 0.0
        min_cap = round(p_rate * min_factor, 3)
        max_cap = round(p_rate * max_factor, 3)
        capped = (t_rate <= min_cap + 0.001) or (t_rate >= max_cap - 0.001)

        dev_mgdl = round(hourly_devs_all_days.get(h, 0.0), 1)
        dev_mmol = round(dev_mgdl / MMOL_TO_MGDL, 2)

        hourly.append({
            "hour": h,
            "time_str": f"{h:02d}:00",
            "pump_rate": p_rate,
            "tuned_rate": t_rate,
            "delta_rate": delta,
            "pct_change": pct,
            "min_cap": min_cap,
            "max_cap": max_cap,
            "capped": capped,
            "deviation_mgdl": dev_mgdl,
            "deviation_mmol": dev_mmol
        })

    # 3. Formatted Markdown report
    md_lines = [
        "# Autotune Parameter Recommendation Report",
        "",
        f"**Base Profile:** {pump_profile.name} (DIA {pump_profile.dia}h, Peak {pump_profile.peak}m)",
        f"**Evaluation Windows:** {len(day_stats)} discrete 04:00-to-04:00 days",
        f"**Autosens Bounds:** {min_factor:.2f}x to {max_factor:.2f}x",
        "",
        "### Parameter Summary",
        f"- **Daily Basal Total:** {pump_profile.total_daily_basal:.2f} U/d → **{tuned_profile.total_daily_basal:.2f} U/d** ({basal_delta:+.2f} U, {basal_pct:+.1f}%)",
        f"- **ISF:** {pump_profile.isf_mmol:.2f} mmol/L/U ({pump_profile.isf_mgdl:.0f} mg/dL) → **{tuned_profile.isf_mmol:.2f} mmol/L/U** ({tuned_profile.isf_mgdl:.0f} mg/dL) ({isf_pct:+.1f}%)",
        f"- **Carb Ratio:** {pump_profile.carb_ratio:.2f} g/U → **{tuned_profile.carb_ratio:.2f} g/U** ({cr_delta:+.2f} g/U, {cr_pct:+.1f}%)",
        f"- **Diagnostic CSF:** {pump_profile.csf_mmol:.3f} mmol/g → {tuned_profile.csf_mmol:.3f} mmol/g (reporting only)",
        "",
        "### Hourly Basal Rates (U/h)",
        "| Hour | Baseline | Tuned | Delta | % | Cap Applied | Net Dev (mmol/L) |",
        "|---|---|---|---|---|---|---|"
    ]
    for h in hourly:
        cap_str = "YES" if h["capped"] else "-"
        md_lines.append(f"| {h['time_str']} | {h['pump_rate']:.2f} | {h['tuned_rate']:.2f} | {h['delta_rate']:+.2f} | {h['pct_change']:+.1f}% | {cap_str} | {h['deviation_mmol']:+.2f} |")

    markdown_report = "\n".join(md_lines)

    return {
        "summary": summary,
        "hourly": hourly,
        "progression": [s.__dict__ for s in day_stats],
        "baseline_profile": pump_profile.to_dict(),
        "tuned_profile": tuned_profile.to_dict(),
        "markdown_report": markdown_report,
        "query_archive": query_archive
    }
