"""
routes.py -- Flask Blueprint routes for Autotune.
Completely isolated from main.py and other reporting endpoints.
"""
from flask import Blueprint, render_template, request, jsonify, make_response
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config
import database
from autotune.models import Profile, DayClassificationStats, MMOL_TO_MGDL
from autotune.normalise import normalise_treatments
from autotune.prepare import prepare_evidence
from autotune.tune import tune_day
from autotune.report import generate_report
from autotune.data_loader import (
    get_profile_snapshot,
    get_available_profile_eras,
    get_0400_days_in_range,
    load_cgm_and_treatments_for_window
)

autotune_bp = Blueprint('autotune', __name__)

def _get_tz_name() -> str:
    return getattr(config, 'TIMEZONE', 'UTC')

@autotune_bp.route('/autotune')
@autotune_bp.route('/reports/autotune')
def autotune_page():
    tz = ZoneInfo(_get_tz_name())
    now_local = datetime.now(tz)
    today_local = now_local.strftime('%Y-%m-%d')
    yesterday_local = (now_local - timedelta(days=1)).strftime('%Y-%m-%d')

    # Default to last 5 completed days
    end_date_str = request.args.get('end_date', yesterday_local)
    start_date_str = request.args.get('start_date')

    if not start_date_str:
        end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
        start_date_str = (end_dt - timedelta(days=4)).strftime('%Y-%m-%d')

    resp = make_response(render_template(
        'autotune.html',
        today_local=today_local,
        start_date=start_date_str,
        end_date=end_date_str,
        date_state_namespace='autotune'
    ))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@autotune_bp.route('/api/v1/autotune/days', methods=['GET'])
def api_autotune_days():
    tz_name = _get_tz_name()
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not start_date or not end_date:
        now_local = datetime.now(ZoneInfo(tz_name))
        end_date = (now_local - timedelta(days=1)).strftime('%Y-%m-%d')
        start_date = (now_local - timedelta(days=5)).strftime('%Y-%m-%d')

    conn = None
    try:
        conn = database.get_conn()
        days = get_0400_days_in_range(conn, start_date, end_date, tz_name)
        return jsonify({'days': days, 'timezone': tz_name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@autotune_bp.route('/api/v1/autotune/profiles', methods=['GET'])
def api_autotune_profiles():
    tz_name = _get_tz_name()
    conn = None
    try:
        conn = database.get_conn()
        eras = get_available_profile_eras(conn, tz_name)
        return jsonify({'profiles': eras})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)


@autotune_bp.route('/api/v1/autotune/run', methods=['POST'])
def api_autotune_run():
    payload = request.get_json(silent=True) or {}
    selected_days: list = payload.get('selected_days', [])
    base_profile_name: str = payload.get('base_profile_name')
    min_factor: float = float(payload.get('autosens_min', 0.70))
    max_factor: float = float(payload.get('autosens_max', 1.20))
    min_carb_impact: float = float(payload.get('min_5m_carb_impact', 3.0))

    if not selected_days:
        return jsonify({'error': 'No input days selected for Autotune'}), 400

    # Ensure days are sorted chronologically
    selected_days_sorted = sorted(selected_days)
    tz_name = _get_tz_name()
    tz = ZoneInfo(tz_name)

    conn = None
    try:
        conn = database.get_conn()

        # 1. Derive immutable baseline pump_profile
        pump_profile = get_profile_snapshot(conn, era_name=base_profile_name, tz_name=tz_name)
        tuned_profile = pump_profile.copy()

        day_stats: list = []
        hourly_devs_all_days = {h: 0.0 for h in range(24)}
        query_archive = {
            'timezone': tz_name,
            'selected_days': selected_days_sorted,
            'windows': []
        }

        # 2. Sequential tuning for each selected day (04:00 to 04:00)
        for date_str in selected_days_sorted:
            d_dt = datetime.strptime(date_str, '%Y-%m-%d')
            day_start = d_dt.replace(hour=4, minute=0, second=0, microsecond=0, tzinfo=tz)
            day_end = day_start + timedelta(days=1)
            day_start_ms = int(day_start.timestamp() * 1000)
            day_end_ms = int(day_end.timestamp() * 1000)

            # Query CGM & Treatments
            cgm_points, all_treatments, carbs_treatments = load_cgm_and_treatments_for_window(
                conn, day_start_ms, day_end_ms, tuned_profile.dia
            )

            # Normalise treatments into discrete doses and 5-min pseudo-boluses
            doses = normalise_treatments(
                all_treatments, day_start_ms, day_end_ms, tuned_profile, pump_profile
            )

            # Prepare 5-min evidence, deviation, and 4-way classification
            prepared, cr_episodes, prep_diag = prepare_evidence(
                cgm_points, carbs_treatments, doses, tuned_profile,
                day_start_ms, day_end_ms, min_carb_impact
            )

            # Record hourly deviations for basal evidence
            for p in prepared:
                if p.classification == 'BASAL':
                    dt = datetime.fromtimestamp(p.ts_ms / 1000.0, tz=tz)
                    hourly_devs_all_days[dt.hour] += p.deviation

            # Tune this day's profile (Basal -> CR -> ISF)
            tuned_profile, day_metrics = tune_day(
                prepared, cr_episodes, tuned_profile, pump_profile,
                min_factor=min_factor, max_factor=max_factor
            )

            expected_pts = 288
            coverage_pct = round(min(100.0, (len(cgm_points) / expected_pts) * 100.0), 1)

            day_stats.append(DayClassificationStats(
                date_str=date_str,
                total_points=len(cgm_points),
                cgm_coverage_pct=coverage_pct,
                meal_points=prep_diag['meal_count'],
                uam_points=prep_diag['uam_count'],
                basal_points=prep_diag['basal_count'],
                isf_points=prep_diag['isf_count'],
                excluded_points=prep_diag['excluded_low'],
                status='Valid Day' if coverage_pct >= 80 else 'Partial Data',
                tuned_basal_total=day_metrics['tuned_total_basal'],
                tuned_isf_mmol=day_metrics['tuned_isf_mmol'],
                tuned_cr=day_metrics['tuned_cr']
            ))

            query_archive['windows'].append({
                'date': date_str,
                'cgm_query': [day_start.isoformat(), day_end.isoformat()],
                'treatment_query_start': (day_start - timedelta(hours=tuned_profile.dia + 2)).isoformat(),
                'cgm_count': len(cgm_points),
                'treatments_count': len(all_treatments)
            })

        # 3. Generate final report
        report = generate_report(
            pump_profile, tuned_profile, day_stats, hourly_devs_all_days,
            query_archive, min_factor=min_factor, max_factor=max_factor
        )

        return jsonify(report)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        if conn is not None:
            database.return_conn(conn)
