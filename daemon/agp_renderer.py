from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import pandas as pd
import numpy as np
import io
from datetime import datetime, time, timedelta

def fetch_agp_data(conn, start_date_str, end_date_str, cohort_days=None):
    """
    Fetches raw AGP data and returns DataFrames for percentiles, basal, and overlays.
    Calculates percentiles, target ranges, AUCs, and CV% directly in PostgreSQL.
    """
    if not end_date_str:
        end_date_str = datetime.now().strftime('%Y-%m-%d')
    if not start_date_str:
        start_date_str = (datetime.strptime(end_date_str, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
        
    if cohort_days is not None and len(cohort_days) == 0:
        return None, None, None

    cohort_clause = " AND day = ANY(%s::date[]) " if cohort_days else ""
    raw_params = [end_date_str, start_date_str]
    if cohort_days:
        raw_params.append(cohort_days)

    query = f"""
        SELECT 
            minute_bucket::int,
            ROUND((PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY sg_mmol))::numeric, 2) AS p10,
            ROUND((PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY sg_mmol))::numeric, 2) AS p25,
            ROUND((PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY sg_mmol))::numeric, 2) AS p50,
            ROUND((PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY sg_mmol))::numeric, 2) AS p75,
            ROUND((PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY sg_mmol))::numeric, 2) AS p90,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY bgi) AS bgi_p50,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY deviation) AS dev_p50,
            100.0 * COUNT(*) FILTER (WHERE sg_mmol >= 3.9 AND sg_mmol <= 10.0) / NULLIF(COUNT(*), 0) AS tir_pct,
            100.0 * COUNT(*) FILTER (WHERE sg_mmol >= 3.9 AND sg_mmol <= 7.8) / NULLIF(COUNT(*), 0) AS titr_pct,
            -- TIR Heat: full 6-band distribution per 15-min bucket. Five new
            -- columns only -- the TITR band reuses titr_pct above rather than
            -- re-selecting an identically-defined column (a duplicate name here
            -- makes percentiles['titr_pct'] a DataFrame, not a Series, and the
            -- to_numeric/smoothing loops below then blow up on it).
            -- Boundaries are mmol/L and match this page's existing tir_pct /
            -- titr_pct definitions (titr_pct + tirtitr_pct == tir_pct), NOT the
            -- integer mg/dL edges layer2_daily_band_stats uses -- 70 mg/dL is
            -- 3.8849 and 54 mg/dL is 2.9970, so those two boundaries differ by
            -- one mg/dL bucket. Internally consistent here by design.
            100.0 * COUNT(*) FILTER (WHERE sg_mmol < 3.0) / NULLIF(COUNT(*), 0) AS vlow_pct,
            100.0 * COUNT(*) FILTER (WHERE sg_mmol >= 3.0 AND sg_mmol < 3.9) / NULLIF(COUNT(*), 0) AS low_pct,
            100.0 * COUNT(*) FILTER (WHERE sg_mmol > 7.8 AND sg_mmol <= 10.0) / NULLIF(COUNT(*), 0) AS tirtitr_pct,
            100.0 * COUNT(*) FILTER (WHERE sg_mmol > 10.0 AND sg_mmol <= 13.9) / NULLIF(COUNT(*), 0) AS high_pct,
            100.0 * COUNT(*) FILTER (WHERE sg_mmol > 13.9) / NULLIF(COUNT(*), 0) AS vhigh_pct,
            SUM(GREATEST(0.0, 3.9 - sg_mmol)) * (5.0 / 60.0) / NULLIF(COUNT(DISTINCT day), 0) AS hypo_auc,
            SUM(GREATEST(0.0, sg_mmol - 7.8)) * (5.0 / 60.0) / NULLIF(COUNT(DISTINCT day), 0) AS hyper_auc,
            100.0 * (STDDEV(sg_mmol) / NULLIF(AVG(sg_mmol), 0)) AS cv_pct,
            COUNT(DISTINCT day) FILTER (WHERE sg_mmol >= 3.9 AND sg_mmol <= 4.5) AS warning_count,
            AVG(CASE WHEN 1.509 * (power(ln((sg_mmol * 18.0182)::float8), 1.084) - 5.381) < 0 
                     THEN 10.0 * power(1.509 * (power(ln((sg_mmol * 18.0182)::float8), 1.084) - 5.381), 2) 
                     ELSE 0 END) AS lbgi,
            AVG(CASE WHEN 1.509 * (power(ln((sg_mmol * 18.0182)::float8), 1.084) - 5.381) > 0 
                     THEN 10.0 * power(1.509 * (power(ln((sg_mmol * 18.0182)::float8), 1.084) - 5.381), 2) 
                     ELSE 0 END) AS hbgi
        FROM layer2_agp_raw 
        WHERE day <= %s::date
          AND day >= %s::date
          {cohort_clause}
          AND sg_mmol > 0
        GROUP BY minute_bucket
        ORDER BY minute_bucket;
    """
    
    percentiles = pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(raw_params))
            colnames = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            percentiles = pd.DataFrame(rows, columns=colnames)
    except Exception as e:
        print(f"[agp] Failed to fetch glucose data: {e}")

    if percentiles.empty:
        return None, None, None

    # Ensure all 96 buckets are present for a smooth categorical x-axis
    all_buckets = pd.DataFrame({'minute_bucket': range(0, 1440, 15)})
    percentiles = pd.merge(all_buckets, percentiles, on='minute_bucket', how='left')
    for col in ['p10', 'p25', 'p50', 'p75', 'p90', 'bgi_p50', 'dev_p50', 'tir_pct', 'titr_pct', 'hypo_auc', 'hyper_auc', 'cv_pct', 'warning_count', 'lbgi', 'hbgi',
                'vlow_pct', 'low_pct', 'tirtitr_pct', 'high_pct', 'vhigh_pct']:
        percentiles[col] = pd.to_numeric(percentiles[col], errors='coerce')

    # Apply circular/periodic centered 1-hour smoothing (4 buckets of 15m) for clean modal curves
    tir_window = 4
    tir_pad = tir_window // 2
    for col in ['titr_pct', 'tir_pct', 'hypo_auc', 'hyper_auc', 'cv_pct', 'lbgi', 'hbgi']:
        padded = pd.concat([
            percentiles[col].iloc[-tir_pad:],
            percentiles[col],
            percentiles[col].iloc[:tir_pad]
        ])
        smoothed = padded.rolling(window=tir_window, center=True, min_periods=1).mean()
        percentiles[f"{col}_smooth"] = smoothed.iloc[tir_pad:-tir_pad].values

    percentiles['hour_of_day'] = percentiles['minute_bucket'] // 60
    percentiles['minute'] = percentiles['minute_bucket'] % 60
    percentiles['time_obj'] = percentiles.apply(
        lambda r: datetime(2000, 1, 1, int(r['hour_of_day']), int(r['minute'])), axis=1
    )
    percentiles = percentiles.sort_values('time_obj')

    # Basal Data (Bucketed to 15-min intervals to match other series)
    basal_query = """
        SELECT start_seconds, end_seconds, rate 
        FROM layer2_profile_schedule 
        WHERE start_time <= %s::timestamp + interval '23 hours 59 minutes'
          AND end_time > %s::timestamp + interval '23 hours 59 minutes'
        ORDER BY start_seconds
    """
    basal_df = pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(basal_query, (end_date_str, end_date_str))
            colnames = [desc[0] for desc in cur.description]
            raw_basal_rows = cur.fetchall()
            if raw_basal_rows:
                # Create a lookup for every 15-minute bucket
                buckets = range(0, 1440, 15)
                bucketed_basal = []
                for b in buckets:
                    b_sec = b * 60
                    # Find the rate that covers this bucket start
                    rate = 0.0
                    for row in raw_basal_rows:
                        if b_sec >= row[0] and b_sec < row[1]:
                            rate = float(row[2])
                            break
                    bucketed_basal.append({'minute_bucket': b, 'rate': rate})
                basal_df = pd.DataFrame(bucketed_basal)
                
                basal_df['hour_of_day'] = basal_df['minute_bucket'] // 60
                basal_df['minute'] = basal_df['minute_bucket'] % 60
                basal_df['time_obj'] = basal_df.apply(
                    lambda r: datetime(2000, 1, 1, int(r['hour_of_day']), int(r['minute'])), axis=1
                )
    except Exception as e:
        print(f"[agp] Failed to fetch basal data: {e}")

    # IOB / Carb Load Data
    overlay_cohort = " AND day = ANY(%s::date[]) " if cohort_days else ""
    overlay_params = [end_date_str, start_date_str]
    if cohort_days:
        overlay_params.append(cohort_days)

    overlay_query = f"""
        SELECT minute_bucket, 
               AVG(median_iob) as avg_iob,
               AVG(median_cob) as avg_cob
        FROM layer2_agp_iob_cob_raw
        WHERE day <= %s::date
          AND day >= %s::date
          {overlay_cohort}
        GROUP BY minute_bucket
    """
    overlay_df = pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(overlay_query, tuple(overlay_params))
            colnames = [desc[0] for desc in cur.description]
            overlay_df = pd.DataFrame(cur.fetchall(), columns=colnames)
            if not overlay_df.empty:
                overlay_df['minute_bucket'] = overlay_df['minute_bucket'].astype(int)
    except Exception as e:
        print(f"[agp] Failed to fetch overlay data: {e}")

    if not overlay_df.empty:
        all_buckets = pd.DataFrame({'minute_bucket': range(0, 1440, 15)})
        overlay_df = pd.merge(all_buckets, overlay_df, on='minute_bucket', how='left').fillna(0)
        cob_window = 4
        cob_pad = cob_window // 2
        padded_cob = pd.concat([
            overlay_df['avg_cob'].iloc[-cob_pad:],
            overlay_df['avg_cob'],
            overlay_df['avg_cob'].iloc[:cob_pad],
        ])
        smoothed_cob = padded_cob.rolling(window=cob_window, center=True, min_periods=1).mean()
        overlay_df['avg_cob_smooth'] = smoothed_cob.iloc[cob_pad:-cob_pad].values

        overlay_df['hour_of_day'] = overlay_df['minute_bucket'] // 60
        overlay_df['minute'] = overlay_df['minute_bucket'] % 60
        overlay_df['time_obj'] = overlay_df.apply(
            lambda r: datetime(2000, 1, 1, int(r['hour_of_day']), int(r['minute'])), axis=1
        )
        overlay_df = overlay_df.sort_values('time_obj')

    # Low Episodes Count
    low_cohort = " AND (h.start_time AT TIME ZONE tz.tz_name)::date = ANY(%s::date[]) " if cohort_days else ""
    low_params = [start_date_str, end_date_str]
    if cohort_days:
        low_params.append(cohort_days)

    low_counts = pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                WITH tz AS (SELECT value AS tz_name FROM system_config WHERE key = 'TIMEZONE' LIMIT 1)
                SELECT
                    floor((EXTRACT(hour FROM (h.start_time AT TIME ZONE tz.tz_name)) * 60
                         + EXTRACT(minute FROM (h.start_time AT TIME ZONE tz.tz_name))) / 15) * 15 AS minute_bucket,
                    count(*) AS low_count
                FROM layer2_hypo_episodes h, tz
                WHERE (h.start_time AT TIME ZONE tz.tz_name)::date >= %s::date
                  AND (h.start_time AT TIME ZONE tz.tz_name)::date <= %s::date
                  {low_cohort}
                GROUP BY minute_bucket
            """, tuple(low_params))
            colnames = [desc[0] for desc in cur.description]
            low_counts = pd.DataFrame(cur.fetchall(), columns=colnames)
    except Exception:
        low_counts = pd.DataFrame(columns=['minute_bucket', 'low_count'])

    # Dynamic ISF (from layer2_five_minute_aggregate pre-computed 5-minute spine)
    isf_cohort = " AND a.day = ANY(%s::date[]) " if cohort_days else ""
    isf_params = [start_date_str, end_date_str]
    if cohort_days:
        isf_params.append(cohort_days)

    isf_query = f"""
        WITH tz AS (
            SELECT value AS tz_name FROM system_config WHERE key = 'TIMEZONE' LIMIT 1
        )
        SELECT 
            (EXTRACT(hour FROM (a.ts AT TIME ZONE tz.tz_name)) * 60 + floor(EXTRACT(minute FROM (a.ts AT TIME ZONE tz.tz_name)) / 15) * 15)::int AS minute_bucket,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY a.isf) AS median_isf
        FROM layer2_five_minute_aggregate a, tz
        WHERE a.day >= %s::date
          AND a.day <= %s::date
          {isf_cohort}
          AND a.isf IS NOT NULL 
          AND a.isf > 0
        GROUP BY 1
    """
    isf_df = pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(isf_query, tuple(isf_params))
            colnames = [desc[0] for desc in cur.description]
            isf_df = pd.DataFrame(cur.fetchall(), columns=colnames)
    except Exception as e:
        print(f"[agp] Failed to fetch ISF data: {e}")

    all_buckets = pd.DataFrame({'minute_bucket': range(0, 1440, 15)})
    if not isf_df.empty:
        isf_df['minute_bucket'] = isf_df['minute_bucket'].astype(int)
        isf_df = pd.merge(all_buckets, isf_df, on='minute_bucket', how='left')
        isf_df['median_isf'] = isf_df['median_isf'].interpolate(method='linear').bfill().ffill()
    else:
        isf_df = all_buckets.copy()
        isf_df['median_isf'] = np.nan

    isf_window = 4
    isf_pad = isf_window // 2
    padded_isf = pd.concat([
        isf_df['median_isf'].iloc[-isf_pad:],
        isf_df['median_isf'],
        isf_df['median_isf'].iloc[:isf_pad]
    ])
    smoothed_isf = padded_isf.rolling(window=isf_window, center=True, min_periods=1).mean()
    isf_df['isf_smooth'] = smoothed_isf.iloc[isf_pad:-isf_pad].values

    # Merge low_counts and ISF into percentiles
    percentiles = pd.merge(percentiles, low_counts, on='minute_bucket', how='left').fillna({'low_count': 0, 'warning_count': 0})
    percentiles = pd.merge(percentiles, isf_df[['minute_bucket', 'median_isf', 'isf_smooth']], on='minute_bucket', how='left')

    return percentiles, basal_df, overlay_df

def fetch_ce_data(conn, start_date_str, end_date_str, cohort_days=None):
    """
    Controller Effort (CE): ratio of algorithm-delivered insulin (actual
    basal incl. temp basal + SMB microboluses) to scheduled/programmed
    basal, bucketed onto the same 15-min/day grid layer2_agp_raw uses for
    glucose.
    """
    if cohort_days is not None and len(cohort_days) == 0:
        return pd.DataFrame()

    basal_cohort = " AND b.day = ANY(%s::date[]) " if cohort_days else ""
    smb_cohort = " AND (t.ts AT TIME ZONE bnds.tz_name)::date = ANY(%s::date[]) " if cohort_days else ""
    ce_params = [start_date_str, end_date_str, start_date_str, end_date_str]
    if cohort_days:
        ce_params.extend([cohort_days, cohort_days])

    query = f"""
        WITH tz AS (
            SELECT value AS tz_name FROM system_config WHERE key = 'TIMEZONE' LIMIT 1
        ),
        bounds AS (
            SELECT 
                (%s::date::text || ' 00:00:00')::timestamp AT TIME ZONE tz.tz_name AS start_ts,
                (%s::date::text || ' 23:59:59')::timestamp AT TIME ZONE tz.tz_name AS end_ts,
                tz.tz_name
            FROM tz
        ),
        basal_bucketed AS (
            SELECT
                floor((EXTRACT(hour FROM (b.minute_ts AT TIME ZONE bnds.tz_name)) * 60
                     + EXTRACT(minute FROM (b.minute_ts AT TIME ZONE bnds.tz_name))) / 15) * 15 AS minute_bucket,
                SUM(b.actual_rate) / 12.0 AS delivered_basal,
                SUM(b.base_rate) / 12.0 AS scheduled_basal
            FROM layer2_basal_5min b, bounds bnds
            WHERE b.day >= %s AND b.day <= %s
              {basal_cohort}
            GROUP BY 1
        ),
        smb_bucketed AS (
            SELECT
                floor((EXTRACT(hour FROM (t.ts AT TIME ZONE bnds.tz_name)) * 60
                     + EXTRACT(minute FROM (t.ts AT TIME ZONE bnds.tz_name))) / 15) * 15 AS minute_bucket,
                SUM(t.insulin) AS smb_insulin
            FROM treatments t, bounds bnds
            WHERE t.smb_flag = true
              AND t.ts >= bnds.start_ts
              AND t.ts <= bnds.end_ts
              {smb_cohort}
            GROUP BY 1
        )
        SELECT
            bb.minute_bucket::int AS minute_bucket,
            bb.delivered_basal,
            bb.scheduled_basal,
            COALESCE(sb.smb_insulin, 0) AS smb_insulin
        FROM basal_bucketed bb
        LEFT JOIN smb_bucketed sb ON bb.minute_bucket = sb.minute_bucket
        ORDER BY bb.minute_bucket
    """
    ce_df = pd.DataFrame()
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(ce_params))
            colnames = [desc[0] for desc in cur.description]
            ce_df = pd.DataFrame(cur.fetchall(), columns=colnames)
    except Exception as e:
        print(f"[agp] Failed to fetch CE data: {e}")
        return pd.DataFrame()

    if ce_df.empty:
        return ce_df

    for col in ('delivered_basal', 'scheduled_basal', 'smb_insulin'):
        ce_df[col] = ce_df[col].astype(float)

    # Ensure all 96 buckets are present, matching the glucose percentile grid
    all_buckets = pd.DataFrame({'minute_bucket': range(0, 1440, 15)})
    ce_df = pd.merge(all_buckets, ce_df, on='minute_bucket', how='left')
    fill_cols = ['delivered_basal', 'scheduled_basal', 'smb_insulin']
    ce_df[fill_cols] = ce_df[fill_cols].fillna(0)

    ce_df['ce_pct'] = np.where(
        ce_df['scheduled_basal'] > 0,
        100.0 * (ce_df['delivered_basal'] + ce_df['smb_insulin']) / ce_df['scheduled_basal'],
        np.nan
    )

    ce_df['hour_of_day'] = ce_df['minute_bucket'] // 60
    ce_df['minute'] = ce_df['minute_bucket'] % 60
    ce_df['time_obj'] = ce_df.apply(
        lambda r: datetime(2000, 1, 1, int(r['hour_of_day']), int(r['minute'])), axis=1
    )
    ce_df = ce_df.sort_values('time_obj')

    # Centered simple moving average, +/-1hr (8 x 15min buckets)
    window = 8
    pad = window // 2
    padded = pd.concat([
        ce_df['ce_pct'].iloc[-pad:],
        ce_df['ce_pct'],
        ce_df['ce_pct'].iloc[:pad],
    ])
    smoothed = padded.rolling(window=window, center=True, min_periods=1).mean()
    ce_df['ce_pct_smooth_2h'] = smoothed.iloc[pad:-pad].values

    return ce_df


def get_agp_json_data(conn, start_date_str, end_date_str, cohort_days=None):
    """
    Returns AGP data as a dictionary for JSON serialization.
    """
    percentiles, basal_df, overlay_df = fetch_agp_data(conn, start_date_str, end_date_str, cohort_days=cohort_days)
    
    if percentiles is None:
        return None
        
    # Format time_obj back to HH:mm for JSON
    percentiles_list = percentiles.copy()
    percentiles_list['time'] = percentiles_list['time_obj'].dt.strftime('%H:%M')
    
    # One list drives both the NaN -> None cleanup and the projection below, so
    # the two can't drift apart (this was 16 near-identical .replace() lines).
    # p10..p90 are included deliberately: they previously skipped the cleanup
    # and would serialize as bare NaN -- invalid JSON -- for any 15-min bucket
    # with no readings. No bucket is empty in practice today; this closes it.
    pct_cols = [
        'p10', 'p25', 'p50', 'p75', 'p90', 'bgi_p50', 'dev_p50',
        'low_count', 'warning_count',
        'tir_pct', 'titr_pct', 'tir_pct_smooth', 'titr_pct_smooth',
        'hypo_auc', 'hyper_auc', 'hypo_auc_smooth', 'hyper_auc_smooth',
        'cv_pct', 'cv_pct_smooth',
        'median_isf', 'isf_smooth',
        'lbgi', 'hbgi', 'lbgi_smooth', 'hbgi_smooth',
        # TIR Heat distribution bands (unsmoothed by design -- the raw 15-min
        # distribution is the point of the display). The TITR band is served by
        # titr_pct above, not a sixth column.
        'vlow_pct', 'low_pct', 'tirtitr_pct', 'high_pct', 'vhigh_pct',
    ]
    for col in pct_cols:
        percentiles_list[col] = percentiles_list[col].replace({np.nan: None})

    result = {
        "percentiles": percentiles_list[['time'] + pct_cols].to_dict(orient='records'),
        "basal": [],
        "overlays": [],
        "ce": []
    }
    
    if not basal_df.empty:
        basal_list = basal_df.copy()
        basal_list['time'] = basal_list['time_obj'].dt.strftime('%H:%M')
        result["basal"] = basal_list[['time', 'rate']].to_dict(orient='records')
        
    if not overlay_df.empty:
        overlay_list = overlay_df.copy()
        overlay_list['time'] = overlay_list['time_obj'].dt.strftime('%H:%M')
        result["overlays"] = overlay_list[['time', 'avg_iob', 'avg_cob_smooth']].to_dict(orient='records')

    ce_df = fetch_ce_data(conn, start_date_str, end_date_str, cohort_days=cohort_days)
    if not ce_df.empty:
        ce_list = ce_df.copy()
        ce_list['time'] = ce_list['time_obj'].dt.strftime('%H:%M')
        ce_list['ce_pct'] = ce_list['ce_pct'].replace({np.nan: None})
        ce_list['ce_pct_smooth_2h'] = ce_list['ce_pct_smooth_2h'].replace({np.nan: None})
        result["ce"] = ce_list[['time', 'ce_pct', 'ce_pct_smooth_2h', 'delivered_basal', 'scheduled_basal', 'smb_insulin']].to_dict(orient='records')

    return result

def generate_agp_image(conn, start_date_str=None, end_date_str=None, show_basal=True, show_iob=False, show_carbs=False):
    """
    Generates a professional AGP image by querying layer2_agp_raw.
    Uses object-oriented Matplotlib API for thread safety.
    """
    percentiles, basal_df, overlay_df = fetch_agp_data(conn, start_date_str, end_date_str)
    
    if percentiles is None:
        return None

    # Setup Plot (Object-Oriented API)
    fig = Figure(figsize=(10, 6), dpi=100)
    canvas = FigureCanvas(fig)
    fig.patch.set_facecolor('white')
    ax = fig.add_subplot(111)
    ax.set_facecolor('#fdfdfd')
    
    ax.axhspan(3.9, 7.8, color='#dcfce7', alpha=0.9, lw=0, zorder=0)
    ax.fill_between(percentiles['time_obj'], percentiles['p10'], percentiles['p90'], color='#A6CEE3', alpha=0.3, label='10th-90th', lw=0, zorder=1)
    ax.fill_between(percentiles['time_obj'], percentiles['p25'], percentiles['p75'], color='#1F78B4', alpha=0.6, label='25th-75th', lw=0, zorder=2)
    ax.plot(percentiles['time_obj'], percentiles['p50'], color='#000000', linewidth=3.0, label='Glucose Median', zorder=10)

    ax.axhline(7.8, color='#888', linestyle='-', linewidth=0.8, alpha=0.5, zorder=3)
    ax.axhline(3.9, color='#888', linestyle='-', linewidth=0.8, alpha=0.5, zorder=3)
    
    gl_max = percentiles['p90'].max()
    y_max = max(16.0, np.ceil((gl_max + 2) / 2) * 2) if not pd.isna(gl_max) else 16.0
    ax.set_ylim(0, y_max)
    ax.yaxis.set_major_locator(mticker.MultipleLocator(2))
    ax.set_xlim(datetime(2000, 1, 1, 0, 0), datetime(2000, 1, 1, 23, 59))
    
    legend_handles = []
    ax2 = ax.twinx()
    ax2.set_ylim(0, 10)
    ax2.set_ylabel("Insulin (U) / Carbs (g \u00f7 10)", fontsize=9, color='grey', labelpad=10)
    ax2.tick_params(axis='y', colors='grey', labelsize=8)
    ax2.grid(False)

    if show_basal and not basal_df.empty:
        ax2.plot(basal_df['time_obj'], basal_df['rate'], drawstyle='steps-post', color='#666', alpha=1.0, linewidth=2.0, label='Basal Profile', zorder=5)
        ax2.fill_between(basal_df['time_obj'], 0, basal_df['rate'], step='post', color='grey', alpha=0.15, lw=0, zorder=4)
        # For legend consistency
        legend_handles.append(ax2.get_legend_handles_labels()[0][-1])
        
    if not overlay_df.empty:
        if show_iob:
            v_iob = overlay_df['avg_iob'].astype(float)
            line_iob, = ax2.plot(overlay_df['time_obj'], v_iob, color='#8E44AD', alpha=1.0, linewidth=2.0, linestyle='--', label='IOB (U)', zorder=6)
            legend_handles.append(line_iob)
        if show_carbs:
            v_cob = overlay_df['avg_cob_smooth'].astype(float) * 0.1
            line_carbs, = ax2.plot(overlay_df['time_obj'], v_cob, color='#D35400', alpha=1.0, linewidth=2.5, linestyle='--', label='COB (gms / 10)', zorder=7)
            legend_handles.append(line_carbs)
    
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=1))
    ax.grid(True, which='both', axis='x', color='#888', linestyle='-', linewidth=0.5, alpha=0.3)
    ax.grid(True, which='major', axis='y', color='#888', linestyle='-', linewidth=0.5, alpha=0.3)
    ax.set_ylabel("Glucose (mmol/L)", fontsize=10)
    ax.set_xlabel("Time of Day", fontsize=10)
    
    h_gl, l_gl = ax.get_legend_handles_labels()
    ax.legend(handles=h_gl + legend_handles, loc='lower center', bbox_to_anchor=(0.5, -0.32), ncol=2, frameon=False, fontsize=9)
    
    fig.tight_layout()
    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf

