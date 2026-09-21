-- migrate:up

--
-- PostgreSQL database dump
--


-- Dumped from database version 15.17 (Debian 15.17-1.pgdg13+1)
-- Dumped by pg_dump version 15.17 (Debian 15.17-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET search_path = public, pg_catalog;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: compute_basal_for_range(date, date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.compute_basal_for_range(p_start date, p_end date) RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    rows_affected INTEGER;
    tz_name TEXT;
BEGIN
    -- Get timezone
    SELECT value INTO tz_name FROM system_config WHERE key = 'TIMEZONE' LIMIT 1;
    IF tz_name IS NULL THEN tz_name := 'UTC'; END IF;

    -- Insert computed basal for the range
    -- We use ON CONFLICT (minute_ts) DO UPDATE to make this idempotent
    -- even if the 'day' mapping has changed due to timezone shifts.
    WITH spine AS (
        SELECT
            m AS minute_ts,
            ((m AT TIME ZONE tz_name))::date AS day,
            (EXTRACT(HOUR FROM (m AT TIME ZONE tz_name)) * 3600 +
             EXTRACT(MINUTE FROM (m AT TIME ZONE tz_name)) * 60 +
             EXTRACT(SECOND FROM (m AT TIME ZONE tz_name)))::int AS seconds_of_day
        FROM generate_series(
            (p_start::timestamp AT TIME ZONE tz_name),
            ((p_end + 1)::timestamp AT TIME ZONE tz_name) - INTERVAL '1 second',
            INTERVAL '5 minutes'
        ) AS m
        WHERE (m AT TIME ZONE tz_name)::date >= p_start
          AND (m AT TIME ZONE tz_name)::date <= p_end
    ),

    base AS (
        SELECT
            s.minute_ts,
            s.day,
            ps.rate AS base_rate
        FROM spine s
        JOIN layer2_profile_schedule ps ON
            s.minute_ts >= ps.start_time
            AND s.minute_ts < ps.end_time
            AND s.seconds_of_day >= ps.start_seconds
            AND s.seconds_of_day < ps.end_seconds
    ),
    temp AS (
        SELECT
            s.minute_ts,
            tb.absolute AS temp_rate
        FROM spine s
        LEFT JOIN LATERAL (
            SELECT absolute
            FROM treatments
            WHERE event_type = 'Temp Basal'
              AND ts <= s.minute_ts
              AND ts >= s.minute_ts - INTERVAL '120 minutes'
              AND ts + (COALESCE(duration, 0) || ' minutes')::interval > s.minute_ts
              AND NOT EXISTS (
                  SELECT 1 FROM treatments t2
                  WHERE t2.ts > treatments.ts AND t2.ts <= s.minute_ts
                    AND (t2.event_type = 'Temp Basal' OR t2.event_type = 'Profile Switch')
              )
            ORDER BY ts DESC
            LIMIT 1
        ) tb ON true
        WHERE tb.absolute IS NOT NULL
    )
    INSERT INTO layer2_basal_5min (minute_ts, day, base_rate, temp_rate, actual_rate, computed_at)
    SELECT
        b.minute_ts,
        b.day,
        b.base_rate,
        t.temp_rate,
        COALESCE(t.temp_rate, b.base_rate) AS actual_rate,
        NOW()
    FROM base b
    LEFT JOIN temp t ON b.minute_ts = t.minute_ts
    ON CONFLICT (minute_ts) DO UPDATE SET
        day = EXCLUDED.day,
        base_rate = EXCLUDED.base_rate,
        temp_rate = EXCLUDED.temp_rate,
        actual_rate = EXCLUDED.actual_rate,
        computed_at = EXCLUDED.computed_at;

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected;
END;
$$;


--
-- Name: get_metrics_comparison(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_metrics_comparison(anchor_date date) RETURNS TABLE("Metric" text, "90 Day Avg" text, "60 Day Avg" text, "30 Day Avg" text, "14 Day Avg" text, "7 Day Avg" text, "Selected Day" text, "Description" text)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH sel AS (
        SELECT * FROM layer2_daily_band_stats WHERE "date" = anchor_date
    ),
    win AS (
        SELECT * FROM layer2_daily_risk_stats WHERE "date" = anchor_date
    ),
    calculated AS (
        SELECT 'TIR (3.9-10) (%)'::text as m, win.tir_90d as v90, win.tir_60d as v60, win.tir_30d as v30, win.tir_14d as v14, win.tir_7d as v7, sel.pct_tir as vS, 1 as p, 1 as u, 'Time in Range (70-180 mg/dL).'::text as d FROM sel, win
        UNION ALL SELECT 'TITR (3.9-7.8) (%)', win.titr_90d, win.titr_60d, win.titr_30d, win.titr_14d, win.titr_7d, sel.pct_titr, 1, 1, 'Time in Tight Range (70-140 mg/dL).' FROM sel, win
        UNION ALL SELECT 'Avg BG', win.mean_mmol_90d, win.mean_mmol_60d, win.mean_mmol_30d, win.mean_mmol_14d, win.mean_mmol_7d, sel.day_mean_mmol, 1, 0, 'Average blood glucose level.' FROM sel, win
        UNION ALL SELECT 'GMI (%)', win.gmi_90d, win.gmi_60d, win.gmi_30d, win.gmi_14d, win.gmi_7d, sel.day_gmi_percent, 1, 1, 'Glucose Management Indicator.' FROM sel, win
        UNION ALL SELECT 'CV (%) (not MAVG)', win.cv_90d, win.cv_60d, win.cv_30d, win.cv_14d, win.cv_7d,
            CAST(100.0*SQRT(GREATEST(0, sel.bg_sum2/NULLIF(sel.bg_readings,0) - POWER(sel.bg_sum/NULLIF(sel.bg_readings,0),2)))/NULLIF(sel.bg_sum/NULLIF(sel.bg_readings,0),0) AS double precision),
            1, 1, 'Coefficient of Variation.' FROM sel, win
        UNION ALL SELECT 'GVI', win.gvi_90d, win.gvi_60d, win.gvi_30d, win.gvi_14d, win.gvi_7d, win.gvi, 2, 0, 'Glycemic Variability Index.' FROM win
        UNION ALL SELECT 'HBGI (High Risk)', win.hbgi_90d, win.hbgi_60d, win.hbgi_30d, win.hbgi_14d, win.hbgi_7d, win.hbgi, 2, 0, 'High Blood Glucose Risk Index.' FROM win
        UNION ALL SELECT 'LBGI (Low Risk)', win.lbgi_90d, win.lbgi_60d, win.lbgi_30d, win.lbgi_14d, win.lbgi_7d, win.lbgi, 2, 0, 'Low Blood Glucose Risk Index.' FROM win
        UNION ALL SELECT 'TDD', win.tdd_90d, win.tdd_60d, win.tdd_30d, win.tdd_14d, win.tdd_7d, sel.tdd, 1, 0, 'Total Daily Dose (Bolus + Basal).' FROM sel, win
        UNION ALL SELECT 'Carbs', win.carbs_90d, win.carbs_60d, win.carbs_30d, win.carbs_14d, win.carbs_7d, sel.carbs, 0, 0, 'Total daily carbohydrates (g).' FROM sel, win
        UNION ALL SELECT 'V.Low (<3) (%)', win.vlow_90d, win.vlow_60d, win.vlow_30d, win.vlow_14d, win.vlow_7d, sel.pct_vlow, 1, 1, 'Very Low (<54 mg/dL).' FROM sel, win
        UNION ALL SELECT 'Low (3-3.8) (%)', win.low_90d, win.low_60d, win.low_30d, win.low_14d, win.low_7d, sel.pct_low, 1, 1, 'Low (54-69 mg/dL).' FROM sel, win
        UNION ALL SELECT 'High (10.1-13.9) (%)', win.high_90d, win.high_60d, win.high_30d, win.high_14d, win.high_7d, sel.pct_high, 1, 1, 'High (181-250 mg/dL).' FROM sel, win
        UNION ALL SELECT 'V.High (>13.9) (%)', win.vhigh_90d, win.vhigh_60d, win.vhigh_30d, win.vhigh_14d, win.vhigh_7d, sel.pct_vhigh, 1, 1, 'Very High (>250 mg/dL).' FROM sel, win
    ),
    formatter AS (
        -- FIXED 23 Aug 2026: the old CAST(...AS double precision) round-trip
        -- silently dropped trailing zeros -- ROUND(0.0::numeric, 1) formatted
        -- via double precision renders as "0" not "0.0", so V.Low/V.High (and
        -- any other metric landing on an exact round number) displayed with
        -- fewer decimals than their configured precision `p` intended.
        -- NUMERIC retains scale information that DOUBLE PRECISION doesn't;
        -- casting numeric straight to text preserves it correctly. Every
        -- metric now consistently shows exactly `p` decimals, always.
        SELECT m,
            COALESCE(CAST(ROUND(CAST(v90 AS numeric), p) AS text) || CASE WHEN u=1 AND v90 IS NOT NULL THEN '%' ELSE '' END, '') as f90,
            COALESCE(CAST(ROUND(CAST(v60 AS numeric), p) AS text) || CASE WHEN u=1 AND v60 IS NOT NULL THEN '%' ELSE '' END, '') as f60,
            COALESCE(CAST(ROUND(CAST(v30 AS numeric), p) AS text) || CASE WHEN u=1 AND v30 IS NOT NULL THEN '%' ELSE '' END, '') as f30,
            COALESCE(CAST(ROUND(CAST(v14 AS numeric), p) AS text) || CASE WHEN u=1 AND v14 IS NOT NULL THEN '%' ELSE '' END, '') as f14,
            COALESCE(CAST(ROUND(CAST(v7 AS numeric) , p) AS text) || CASE WHEN u=1 AND v7 IS NOT NULL THEN '%' ELSE '' END, '') as f7,
            COALESCE(CAST(ROUND(CAST(vS AS numeric) , p) AS text) || CASE WHEN u=1 AND vS IS NOT NULL THEN '%' ELSE '' END, '') as fS,
            d as descr
        FROM calculated
    )
    SELECT m::text, f90::text, f60::text, f30::text, f14::text, f7::text, fS::text, descr::text FROM formatter;
END;
$$;


--
-- Name: populate_5min_aggregate(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.populate_5min_aggregate(p_start timestamp with time zone, p_end timestamp with time zone) RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    rows_affected INTEGER;
    tz_name TEXT;
BEGIN
    -- Get timezone
    SELECT value INTO tz_name FROM system_config WHERE key = 'TIMEZONE' LIMIT 1;
    IF tz_name IS NULL THEN tz_name := 'UTC'; END IF;

    -- Truncate range to 5-minute boundaries
    p_start := date_trunc('minute', p_start) - (EXTRACT(MINUTE FROM p_start)::int % 5) * INTERVAL '1 minute';
    p_end := date_trunc('minute', p_end) - (EXTRACT(MINUTE FROM p_end)::int % 5) * INTERVAL '1 minute';

    -- Insert range using ON CONFLICT for idempotency
    INSERT INTO layer2_five_minute_aggregate (
        ts, day, bg, iob, cob, isf, deviation, bgi, 
        sensitivity, variable_sens, basal_rate, bolus_insulin, 
        carbs, deviation_source, scheduled_basal, computed_at,
        bg_diff_1h, bg_diff_2h, bg_diff_4h, bg_roc,
        bolus_iob, basal_iob, loop_iob
    )
    WITH spine AS (
        SELECT m AS bucket
        FROM generate_series(p_start, p_end, INTERVAL '5 minutes') AS m
    ),
    cgm AS (
        SELECT date_trunc('minute', ts) - (EXTRACT(MINUTE FROM ts)::int % 5) * INTERVAL '1 minute' AS bucket,
               AVG(sg) / 18.0182 AS bg
        FROM cgm_readings
        WHERE ts >= p_start - INTERVAL '5 minutes' AND ts <= p_end + INTERVAL '5 minutes'
        GROUP BY 1
    ),
    device AS (
        SELECT DISTINCT ON (bucket)
               date_trunc('minute', ts) - (EXTRACT(MINUTE FROM ts)::int % 5) * INTERVAL '1 minute' AS bucket,
               iob,
               cob,
               isf,
               deviation,
               bgi,
               autosens_ratio AS sensitivity,
               variable_sens,
               bolus_iob,
               basal_iob,
               loop_iob,
               CASE 
                    WHEN cob > 0 THEN 'COB'
                    WHEN EXISTS (
                        SELECT 1 
                        FROM jsonb_array_elements(raw_json->'openaps'->'suggested'->'consoleError') AS elem 
                        WHERE elem::text ILIKE '%UAM Impact:%'
                          AND (substring(elem::text from 'UAM Impact: ([0-9.-]+)')::numeric) > 0.1
                    ) THEN 'UAM'
                    WHEN deviation < 0 THEN 'SENS'
                    WHEN deviation > 0 THEN 'RES'
                    ELSE 'NONE'
               END AS deviation_source
        FROM devicestatus
        WHERE ts >= p_start - INTERVAL '5 minutes' AND ts <= p_end + INTERVAL '5 minutes'
        ORDER BY bucket, ts DESC
    ),
    basal AS (
        SELECT minute_ts AS bucket, 
               actual_rate AS basal_rate,
               base_rate AS scheduled_basal
        FROM layer2_basal_5min
        WHERE minute_ts >= p_start AND minute_ts <= p_end
    ),
    bolus AS (
        SELECT date_trunc('minute', ts) - (EXTRACT(MINUTE FROM ts)::int % 5) * INTERVAL '1 minute' AS bucket,
               SUM(insulin) AS bolus_insulin,
               SUM(carbs) AS carbs
        FROM treatments
        WHERE ts >= p_start AND ts <= p_end
          AND (insulin > 0 OR carbs > 0)
        GROUP BY 1
    )
    SELECT
        s.bucket,
        (s.bucket AT TIME ZONE tz_name)::date AS day,
        c.bg,
        d.iob,
        d.cob,
        d.isf,
        d.deviation,
        d.bgi,
        d.sensitivity,
        d.variable_sens,
        COALESCE(b.basal_rate, 0),
        COALESCE(bol.bolus_insulin, 0),
        COALESCE(bol.carbs, 0),
        d.deviation_source,
        b.scheduled_basal,
        NOW(),
        c.bg - (SELECT bg FROM layer2_five_minute_aggregate WHERE ts = s.bucket - INTERVAL '1 hour'),
        c.bg - (SELECT bg FROM layer2_five_minute_aggregate WHERE ts = s.bucket - INTERVAL '2 hours'),
        c.bg - (SELECT bg FROM layer2_five_minute_aggregate WHERE ts = s.bucket - INTERVAL '4 hours'),
        c.bg - (SELECT bg FROM layer2_five_minute_aggregate WHERE ts = s.bucket - INTERVAL '5 minutes'),
        d.bolus_iob,
        d.basal_iob,
        d.loop_iob
    FROM spine s
    LEFT JOIN cgm c ON s.bucket = c.bucket
    LEFT JOIN device d ON s.bucket = d.bucket
    LEFT JOIN basal b ON s.bucket = b.bucket
    LEFT JOIN bolus bol ON s.bucket = bol.bucket
    ON CONFLICT (ts) DO UPDATE SET
        day = EXCLUDED.day,
        bg = EXCLUDED.bg,
        iob = EXCLUDED.iob,
        cob = EXCLUDED.cob,
        isf = EXCLUDED.isf,
        deviation = EXCLUDED.deviation,
        bgi = EXCLUDED.bgi,
        sensitivity = EXCLUDED.sensitivity,
        variable_sens = EXCLUDED.variable_sens,
        basal_rate = EXCLUDED.basal_rate,
        bolus_insulin = EXCLUDED.bolus_insulin,
        carbs = EXCLUDED.carbs,
        deviation_source = EXCLUDED.deviation_source,
        scheduled_basal = EXCLUDED.scheduled_basal,
        computed_at = EXCLUDED.computed_at,
        bg_diff_1h = EXCLUDED.bg_diff_1h,
        bg_diff_2h = EXCLUDED.bg_diff_2h,
        bg_diff_4h = EXCLUDED.bg_diff_4h,
        bg_roc = EXCLUDED.bg_roc,
        bolus_iob = EXCLUDED.bolus_iob,
        basal_iob = EXCLUDED.basal_iob,
        loop_iob = EXCLUDED.loop_iob;

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: cgm_readings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cgm_readings (
    ts timestamp with time zone NOT NULL,
    sg numeric,
    direction text,
    device text,
    raw jsonb
);


--
-- Name: chart_metric_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chart_metric_settings (
    id integer NOT NULL,
    page character varying(32) NOT NULL,
    metric_id character varying(64) NOT NULL,
    settings jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: chart_metric_settings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.chart_metric_settings ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.chart_metric_settings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: clinical_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_notes (
    id integer NOT NULL,
    date date NOT NULL,
    time_created timestamp with time zone,
    note_type text NOT NULL,
    text_content text,
    weight_kg numeric(5,2),
    hba1c_percent numeric(4,2),
    hba1c_mmol_mol integer,
    raw_treatment_id text,
    raw_treatment_ts timestamp with time zone,
    is_edited boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    is_timeless boolean DEFAULT false NOT NULL,
    title text,
    CONSTRAINT clinical_notes_note_type_check CHECK ((note_type = ANY (ARRAY['user'::text, 'aaps'::text, 'weight'::text, 'hba1c'::text])))
);


--
-- Name: clinical_notes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.clinical_notes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: clinical_notes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.clinical_notes_id_seq OWNED BY public.clinical_notes.id;


--
-- Name: system_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.system_config (
    key text PRIMARY KEY,
    value text
);


--
-- Name: daily_circadian; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.daily_circadian AS
 WITH hourly_stats AS (
         SELECT ((cgm_readings.ts AT TIME ZONE config.tz_name))::date AS day,
            EXTRACT(hour FROM (cgm_readings.ts AT TIME ZONE config.tz_name)) AS hour,
            avg(cgm_readings.sg) AS avg_sg
           FROM (public.cgm_readings
             CROSS JOIN ( SELECT system_config.value AS tz_name
                   FROM public.system_config
                  WHERE (system_config.key = 'TIMEZONE'::text)
                 LIMIT 1) config)
          GROUP BY (((cgm_readings.ts AT TIME ZONE config.tz_name))::date), (EXTRACT(hour FROM (cgm_readings.ts AT TIME ZONE config.tz_name)))
        )
 SELECT hourly_stats.day,
    stddev_pop(hourly_stats.avg_sg) AS sd_hourly
   FROM hourly_stats
  GROUP BY hourly_stats.day;


--
-- Name: dashboard_layouts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_layouts (
    id integer NOT NULL,
    slot_number integer NOT NULL,
    name text NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    display_order integer DEFAULT 1 NOT NULL,
    CONSTRAINT dashboard_layouts_slot_number_check CHECK (((slot_number >= 1) AND (slot_number <= 6)))
);


--
-- Name: dashboard_layouts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.dashboard_layouts ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.dashboard_layouts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: dashboard_widgets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_widgets (
    id integer NOT NULL,
    layout_id integer NOT NULL,
    widget_type text NOT NULL,
    title text,
    x integer DEFAULT 0 NOT NULL,
    y integer DEFAULT 0 NOT NULL,
    w integer DEFAULT 4 NOT NULL,
    h integer DEFAULT 3 NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: dashboard_widgets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.dashboard_widgets ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.dashboard_widgets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: devicestatus; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.devicestatus (
    ts timestamp with time zone NOT NULL,
    iob numeric,
    cob numeric,
    bgi numeric,
    deviation numeric,
    autosens_ratio numeric,
    loop_enacted boolean,
    predictions jsonb,
    raw_json jsonb,
    isf numeric,
    cr numeric,
    target_bg numeric,
    insulin_req numeric,
    variable_sens numeric,
    bolus_iob numeric,
    basal_iob numeric,
    loop_iob numeric,
    loop_basal_iob numeric
);


--
-- Name: filters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.filters (
    id integer NOT NULL,
    name text NOT NULL,
    filter_tree jsonb NOT NULL,
    display_defaults jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    notes text,
    applies_to jsonb DEFAULT '["calendar"]'::jsonb NOT NULL,
    CONSTRAINT filters_applies_to_chk CHECK (((jsonb_array_length(applies_to) >= 1) AND (applies_to <@ '["calendar", "patterns", "trends", "trace"]'::jsonb))),
    CONSTRAINT filters_name_length_chk CHECK ((char_length(name) <= 30))
);


--
-- Name: filters_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.filters ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.filters_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: ingestion_coverage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ingestion_coverage (
    id integer NOT NULL,
    stream text NOT NULL,
    start_ts timestamp with time zone NOT NULL,
    end_ts timestamp with time zone NOT NULL,
    method text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT ts_valid CHECK ((end_ts >= start_ts))
);


--
-- Name: ingestion_coverage_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ingestion_coverage_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ingestion_coverage_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ingestion_coverage_id_seq OWNED BY public.ingestion_coverage.id;


--
-- Name: ingestion_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ingestion_state (
    source text NOT NULL,
    last_ts timestamp with time zone
);


--
-- Name: layer2_agp_iob_cob_raw; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_agp_iob_cob_raw AS
 WITH device AS (
         SELECT ((devicestatus.ts AT TIME ZONE config.tz_name))::date AS day,
            (((((EXTRACT(hour FROM (devicestatus.ts AT TIME ZONE config.tz_name)) * (60)::numeric) + EXTRACT(minute FROM (devicestatus.ts AT TIME ZONE config.tz_name))))::integer / 15) * 15) AS minute_bucket,
            max(devicestatus.iob) AS max_iob,
            max(devicestatus.cob) AS max_cob
           FROM (public.devicestatus
             CROSS JOIN ( SELECT system_config.value AS tz_name
                   FROM public.system_config
                  WHERE (system_config.key = 'TIMEZONE'::text)
                 LIMIT 1) config)
          GROUP BY (((devicestatus.ts AT TIME ZONE config.tz_name))::date), (((((EXTRACT(hour FROM (devicestatus.ts AT TIME ZONE config.tz_name)) * (60)::numeric) + EXTRACT(minute FROM (devicestatus.ts AT TIME ZONE config.tz_name))))::integer / 15) * 15)
        )
 SELECT device.day,
    device.minute_bucket,
    percentile_cont((0.5)::double precision) WITHIN GROUP (ORDER BY ((device.max_iob)::double precision)) AS median_iob,
    percentile_cont((0.5)::double precision) WITHIN GROUP (ORDER BY ((device.max_cob)::double precision)) AS median_cob
   FROM device
  GROUP BY device.day, device.minute_bucket
  WITH NO DATA;


--
-- Name: layer2_five_minute_aggregate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.layer2_five_minute_aggregate (
    ts timestamp with time zone NOT NULL,
    day date NOT NULL,
    bg numeric(5,2),
    iob numeric(6,3),
    cob numeric(6,2),
    isf numeric(6,2),
    deviation numeric(6,2),
    bgi numeric(6,2),
    sensitivity numeric(5,2),
    variable_sens numeric(6,2),
    basal_rate numeric(8,4),
    bolus_insulin numeric(8,4),
    carbs numeric(6,1),
    is_uam boolean DEFAULT false,
    computed_at timestamp with time zone DEFAULT now(),
    scheduled_basal numeric(8,4),
    deviation_source text,
    bg_diff_1h numeric(6,2),
    bg_diff_2h numeric(6,2),
    bg_diff_4h numeric(6,2),
    bg_roc numeric(6,4),
    bolus_iob numeric,
    basal_iob numeric,
    loop_iob numeric
);


--
-- Name: layer2_agp_raw; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_agp_raw AS
 SELECT r.ts,
    ((r.ts AT TIME ZONE config.tz_name))::date AS day,
    (floor((((EXTRACT(hour FROM (r.ts AT TIME ZONE config.tz_name)) * (60)::numeric) + EXTRACT(minute FROM (r.ts AT TIME ZONE config.tz_name))) / (15)::numeric)) * (15)::numeric) AS minute_bucket,
    (('2000-01-01'::date + ((EXTRACT(hour FROM (r.ts AT TIME ZONE config.tz_name)) || ' hours'::text))::interval) + (((floor((EXTRACT(minute FROM (r.ts AT TIME ZONE config.tz_name)) / (15)::numeric)) * (15)::numeric) || ' minutes'::text))::interval) AS plot_time,
    (r.sg / 18.0182) AS sg_mmol,
    agg.bgi,
    agg.deviation
   FROM ((public.cgm_readings r
     LEFT JOIN public.layer2_five_minute_aggregate agg ON ((agg.ts = (date_trunc('hour'::text, r.ts) + (((floor((EXTRACT(minute FROM r.ts) / (5)::numeric)) * (5)::numeric))::double precision * '00:01:00'::interval)))))
     CROSS JOIN ( SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)
         LIMIT 1) config)
  WHERE (r.sg IS NOT NULL)
  ORDER BY r.ts, (((r.ts AT TIME ZONE config.tz_name))::date)
  WITH NO DATA;


--
-- Name: layer2_basal_5min; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.layer2_basal_5min (
    minute_ts timestamp with time zone NOT NULL,
    day date NOT NULL,
    base_rate numeric(8,4),
    temp_rate numeric(8,4),
    actual_rate numeric(8,4),
    computed_at timestamp with time zone DEFAULT now()
);


--
-- Name: treatments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.treatments (
    ts timestamp with time zone NOT NULL,
    event_type text,
    insulin numeric,
    carbs numeric,
    smb_flag boolean,
    notes text,
    raw_id text NOT NULL,
    duration numeric,
    rate numeric,
    absolute numeric,
    raw_json jsonb
);


--
-- Name: layer2_five_minute_timeline; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.layer2_five_minute_timeline AS
 WITH config AS (
         SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)
         LIMIT 1
        ), hist_config AS (
         SELECT (COALESCE(( SELECT system_config.value
                   FROM public.system_config
                  WHERE (system_config.key = 'max_history_days'::text)), '120'::text))::integer AS max_days
        ), range AS (
         SELECT GREATEST((date_trunc('day'::text, min(sub.ts)) - '1 day'::interval), (now() - ((( SELECT hist_config.max_days
                   FROM hist_config))::double precision * '1 day'::interval))) AS start_ts,
            date_trunc('minute'::text, now()) AS end_ts
           FROM ( SELECT min(treatments.ts) AS ts
                   FROM public.treatments
                UNION ALL
                 SELECT min(cgm_readings.ts) AS ts
                   FROM public.cgm_readings) sub
        )
 SELECT m.m AS minute_ts,
    ((m.m AT TIME ZONE config.tz_name))::date AS day,
    ((((EXTRACT(hour FROM (m.m AT TIME ZONE config.tz_name)) * (3600)::numeric) + (EXTRACT(minute FROM (m.m AT TIME ZONE config.tz_name)) * (60)::numeric)) + EXTRACT(second FROM (m.m AT TIME ZONE config.tz_name))))::integer AS seconds_of_day
   FROM ((range
     CROSS JOIN LATERAL generate_series(range.start_ts, range.end_ts, '00:05:00'::interval) m(m))
     CROSS JOIN config);


--
-- Name: layer2_profile_schedule; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_profile_schedule AS
 WITH profile_events AS (
         SELECT treatments.ts AS start_time,
            lead(treatments.ts, 1, 'infinity'::timestamp with time zone) OVER (ORDER BY treatments.ts) AS end_time,
            (((treatments.raw_json ->> 'profileJson'::text))::jsonb -> 'basal'::text) AS basal_schedule_json,
            COALESCE(((treatments.raw_json ->> 'percentage'::text))::numeric, (100)::numeric) AS scaling_pct,
            ((treatments.raw_json ->> 'duration'::text))::numeric AS duration_mins
           FROM public.treatments
          WHERE ((treatments.event_type = 'Profile Switch'::text) OR ((treatments.event_type = 'Note'::text) AND ((treatments.raw_json ->> 'profileJson'::text) IS NOT NULL)))
        ), profile_intervals AS (
         SELECT profile_events.start_time,
            lead(profile_events.start_time, 1, 'infinity'::timestamp with time zone) OVER (ORDER BY profile_events.start_time) AS next_profile_start,
                CASE
                    WHEN (profile_events.duration_mins > (0)::numeric) THEN (profile_events.start_time + ((profile_events.duration_mins || ' minutes'::text))::interval)
                    ELSE 'infinity'::timestamp with time zone
                END AS override_end_time,
            profile_events.basal_schedule_json,
            profile_events.scaling_pct
           FROM profile_events
        ), split_profiles AS (
         SELECT profile_intervals.start_time,
            LEAST(profile_intervals.override_end_time, profile_intervals.next_profile_start) AS end_time,
            profile_intervals.basal_schedule_json,
            profile_intervals.scaling_pct
           FROM profile_intervals
        UNION ALL
         SELECT profile_intervals.override_end_time AS start_time,
            profile_intervals.next_profile_start AS end_time,
            profile_intervals.basal_schedule_json,
            100 AS scaling_pct
           FROM profile_intervals
          WHERE (profile_intervals.override_end_time < profile_intervals.next_profile_start)
        ), expanded AS (
         SELECT split_profiles.start_time,
            split_profiles.end_time,
            ((((jsonb_array_elements(split_profiles.basal_schedule_json) ->> 'value'::text))::numeric * split_profiles.scaling_pct) / 100.0) AS rate,
            ((jsonb_array_elements(split_profiles.basal_schedule_json) ->> 'timeAsSeconds'::text))::integer AS start_seconds
           FROM split_profiles
        )
 SELECT expanded.start_time,
    expanded.end_time,
    expanded.rate,
    expanded.start_seconds,
    lead(expanded.start_seconds, 1, 86400) OVER (PARTITION BY expanded.start_time ORDER BY expanded.start_seconds) AS end_seconds
   FROM expanded
  WHERE (expanded.start_time < expanded.end_time)
  WITH NO DATA;


--
-- Name: layer2_base_basal_per_minute; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.layer2_base_basal_per_minute AS
 SELECT t.minute_ts,
    t.day,
    s.rate AS base_rate
   FROM (public.layer2_five_minute_timeline t
     JOIN public.layer2_profile_schedule s ON (((t.minute_ts >= s.start_time) AND (t.minute_ts < s.end_time) AND (t.seconds_of_day >= s.start_seconds) AND (t.seconds_of_day < s.end_seconds))));


--
-- Name: metric_thresholds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.metric_thresholds (
    setting_key text NOT NULL,
    value_low numeric,
    value_high numeric,
    time_low time without time zone,
    time_high time without time zone,
    params jsonb,
    label text,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: layer2_daily_band_stats; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_daily_band_stats AS
 WITH config AS (
         SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)
         LIMIT 1
        ), thresholds AS (
         SELECT ( SELECT metric_thresholds.value_low
                   FROM public.metric_thresholds
                  WHERE (metric_thresholds.setting_key = 'auc_hypo'::text)) AS auc_hypo,
            ( SELECT metric_thresholds.value_low
                   FROM public.metric_thresholds
                  WHERE (metric_thresholds.setting_key = 'auc_hyper'::text)) AS auc_hyper
        ), daily_basal AS (
         SELECT layer2_basal_5min.day,
            (sum(layer2_basal_5min.base_rate) / 12.0) AS scheduled_basal,
            (sum((layer2_basal_5min.actual_rate - layer2_basal_5min.base_rate)) / 12.0) AS temp_basal_impact,
            (sum(layer2_basal_5min.actual_rate) / 12.0) AS total_basal
           FROM public.layer2_basal_5min
          WHERE (layer2_basal_5min.minute_ts <= now())
          GROUP BY layer2_basal_5min.day
        ), daily_bolus AS (
         SELECT ((t.ts AT TIME ZONE config.tz_name))::date AS day,
            sum(
                CASE
                    WHEN (t.smb_flag = true) THEN t.insulin
                    ELSE (0)::numeric
                END) AS smb,
            sum(
                CASE
                    WHEN ((t.smb_flag IS NOT TRUE) AND (t.event_type ~~* '%Correction%'::text)) THEN t.insulin
                    ELSE (0)::numeric
                END) AS correction_bolus,
            sum(
                CASE
                    WHEN ((t.smb_flag IS NOT TRUE) AND (t.event_type !~~* '%Correction%'::text)) THEN t.insulin
                    ELSE (0)::numeric
                END) AS meal_bolus,
            sum(COALESCE(t.carbs, (0)::numeric)) AS carbs
           FROM (public.treatments t
             CROSS JOIN config)
          WHERE (((t.event_type = ANY (ARRAY['Meal Bolus'::text, 'Correction Bolus'::text, 'Bolus Wizard'::text, 'Meal'::text, 'Carb Correction'::text])) OR (t.smb_flag = true) OR (t.carbs > (0)::numeric)) AND ((t.insulin IS NOT NULL) OR (t.carbs > (0)::numeric)))
          GROUP BY (((t.ts AT TIME ZONE config.tz_name))::date)
        ), daily_cgm AS (
         SELECT ((r.ts AT TIME ZONE config.tz_name))::date AS day,
            count(*) AS n_readings,
            round((((count(*))::numeric / 288.0) * (100)::numeric), 1) AS sg_coverage_pct,
            sum(r.sg) AS bg_sum,
            sum((r.sg * r.sg)) AS bg_sum2,
            count(*) FILTER (WHERE (r.sg < (54)::numeric)) AS n_vlow,
            count(*) FILTER (WHERE ((r.sg >= (54)::numeric) AND (r.sg < (70)::numeric))) AS n_low,
            count(*) FILTER (WHERE ((r.sg >= (70)::numeric) AND (r.sg <= (140)::numeric))) AS n_tight,
            count(*) FILTER (WHERE ((r.sg >= (70)::numeric) AND (r.sg <= (135)::numeric))) AS n_tikr,
            count(*) FILTER (WHERE ((r.sg >= (141)::numeric) AND (r.sg <= (180)::numeric))) AS n_mod_high,
            count(*) FILTER (WHERE ((r.sg >= (181)::numeric) AND (r.sg <= (250)::numeric))) AS n_high,
            count(*) FILTER (WHERE (r.sg > (250)::numeric)) AS n_vhigh,
            sum((GREATEST((0)::numeric, (thr.auc_hypo - (r.sg / 18.0182))) * (5.0 / 60.0))) AS hypo_auc_sum,
            sum((GREATEST((0)::numeric, ((r.sg / 18.0182) - thr.auc_hyper)) * (5.0 / 60.0))) AS hyper_auc_sum,
            avg(
                CASE
                    WHEN ((1.509 * (power(NULLIF(ln(NULLIF(r.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)) < (0)::numeric) THEN ((10)::numeric * power((1.509 * (power(NULLIF(ln(NULLIF(r.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)), (2)::numeric))
                    ELSE (0)::numeric
                END) AS lbgi,
            avg(
                CASE
                    WHEN ((1.509 * (power(NULLIF(ln(NULLIF(r.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)) > (0)::numeric) THEN ((10)::numeric * power((1.509 * (power(NULLIF(ln(NULLIF(r.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)), (2)::numeric))
                    ELSE (0)::numeric
                END) AS hbgi
           FROM ((public.cgm_readings r
             CROSS JOIN config)
             CROSS JOIN thresholds thr)
          WHERE (r.sg IS NOT NULL)
          GROUP BY (((r.ts AT TIME ZONE config.tz_name))::date)
        )
 SELECT COALESCE(cgm.day, db.day, bol.day) AS date,
    round((((COALESCE(db.total_basal, (0)::numeric) + COALESCE(bol.smb, (0)::numeric)) + COALESCE(bol.meal_bolus, (0)::numeric)) + COALESCE(bol.correction_bolus, (0)::numeric)), 1) AS tdd,
    round(COALESCE(db.total_basal, (0)::numeric), 1) AS total_basal,
    round(COALESCE(db.scheduled_basal, (0)::numeric), 1) AS scheduled_basal,
    round(COALESCE(db.temp_basal_impact, (0)::numeric), 1) AS temp_basal_impact,
    round(COALESCE(bol.smb, (0)::numeric), 1) AS smb,
    round(COALESCE(bol.meal_bolus, (0)::numeric), 1) AS meal_bolus,
    round(COALESCE(bol.correction_bolus, (0)::numeric), 1) AS correction_bolus,
    round(COALESCE(bol.carbs, (0)::numeric), 1) AS carbs,
    cgm.n_readings AS bg_readings,
    cgm.sg_coverage_pct,
    cgm.bg_sum,
    cgm.bg_sum2,
    cgm.n_vlow,
    cgm.n_low,
    cgm.n_tight,
    cgm.n_tikr,
    cgm.n_mod_high,
    cgm.n_high,
    cgm.n_vhigh,
    round(((100.0 * (cgm.n_vlow)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_vlow,
    round(((100.0 * (cgm.n_low)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_low,
    round(((100.0 * ((cgm.n_vlow + cgm.n_low))::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_tbr,
    round(((100.0 * (cgm.n_tight)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_titr,
    round(((100.0 * (cgm.n_tikr)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_tikr,
    round(((100.0 * ((cgm.n_tight + cgm.n_mod_high))::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_tir,
    round(((100.0 * (cgm.n_mod_high)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_mod_high,
    round(((100.0 * (cgm.n_high)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_high,
    round(((100.0 * (cgm.n_vhigh)::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_vhigh,
    round(((100.0 * ((cgm.n_high + cgm.n_vhigh))::numeric) / (NULLIF(cgm.n_readings, 0))::numeric), 1) AS pct_tar,
    round(((cgm.bg_sum / (NULLIF(cgm.n_readings, 0))::numeric) / 18.0182), 2) AS day_mean_mmol,
    round((3.31 + (0.02392 * (cgm.bg_sum / (NULLIF(cgm.n_readings, 0))::numeric))), 3) AS day_gmi_percent,
    round(cgm.hypo_auc_sum, 3) AS hypo_auc_sum,
    round(cgm.hyper_auc_sum, 3) AS hyper_auc_sum,
    round(cgm.lbgi, 2) AS lbgi,
    round(cgm.hbgi, 2) AS hbgi
   FROM ((daily_cgm cgm
     FULL JOIN daily_basal db ON ((db.day = cgm.day)))
     FULL JOIN daily_bolus bol ON ((bol.day = cgm.day)))
  ORDER BY COALESCE(cgm.day, db.day, bol.day) DESC
  WITH NO DATA;


--
-- Name: layer2_daily_period_stats; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_daily_period_stats AS
 WITH config AS (
         SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)
         LIMIT 1
        ), window_cfg AS (
         SELECT metric_thresholds.time_low AS ov_start,
            metric_thresholds.time_high AS ov_end
           FROM public.metric_thresholds
          WHERE (metric_thresholds.setting_key = 'overnight_window'::text)
        ), classified AS (
         SELECT r.sg,
            ((r.ts AT TIME ZONE config.tz_name))::date AS local_date,
            ((r.ts AT TIME ZONE config.tz_name))::time without time zone AS local_time,
            w.ov_start,
            w.ov_end
           FROM ((public.cgm_readings r
             CROSS JOIN config)
             CROSS JOIN window_cfg w)
          WHERE (r.sg IS NOT NULL)
        ), bucketed AS (
         SELECT classified.sg,
                CASE
                    WHEN (classified.ov_start > classified.ov_end) THEN
                    CASE
                        WHEN (classified.local_time >= classified.ov_start) THEN classified.local_date
                        WHEN (classified.local_time < classified.ov_end) THEN (classified.local_date - 1)
                        ELSE classified.local_date
                    END
                    ELSE classified.local_date
                END AS bucket_date,
                CASE
                    WHEN (classified.ov_start > classified.ov_end) THEN
                    CASE
                        WHEN ((classified.local_time >= classified.ov_start) OR (classified.local_time < classified.ov_end)) THEN 'overnight'::text
                        ELSE 'daytime'::text
                    END
                    ELSE
                    CASE
                        WHEN ((classified.local_time >= classified.ov_start) AND (classified.local_time < classified.ov_end)) THEN 'overnight'::text
                        ELSE 'daytime'::text
                    END
                END AS period
           FROM classified
        )
 SELECT bucketed.bucket_date AS date,
    bucketed.period,
    count(*) AS bg_readings,
    sum(bucketed.sg) AS bg_sum,
    sum((bucketed.sg * bucketed.sg)) AS bg_sum2,
    count(*) FILTER (WHERE ((bucketed.sg >= (70)::numeric) AND (bucketed.sg <= (140)::numeric))) AS n_tight,
    count(*) FILTER (WHERE ((bucketed.sg >= (141)::numeric) AND (bucketed.sg <= (180)::numeric))) AS n_mod_high,
    round(((100.0 * (count(*) FILTER (WHERE ((bucketed.sg >= (70)::numeric) AND (bucketed.sg <= (180)::numeric))))::numeric) / (NULLIF(count(*), 0))::numeric), 1) AS tir,
    round(((100.0 * sqrt(GREATEST((0)::numeric, ((sum((bucketed.sg * bucketed.sg)) / (NULLIF(count(*), 0))::numeric) - power((sum(bucketed.sg) / (NULLIF(count(*), 0))::numeric), (2)::numeric))))) / NULLIF((sum(bucketed.sg) / (NULLIF(count(*), 0))::numeric), (0)::numeric)), 1) AS cv
   FROM bucketed
  GROUP BY bucketed.bucket_date, bucketed.period
  ORDER BY bucketed.bucket_date DESC, bucketed.period
  WITH NO DATA;


--
-- Name: layer2_hypo_episodes; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_hypo_episodes AS
 WITH lag_data AS (
         SELECT cgm_readings.ts,
            cgm_readings.sg,
                CASE
                    WHEN (cgm_readings.sg < (70)::numeric) THEN 1
                    ELSE 0
                END AS is_low,
            lag(
                CASE
                    WHEN (cgm_readings.sg < (70)::numeric) THEN 1
                    ELSE 0
                END) OVER (ORDER BY cgm_readings.ts) AS prev_low
           FROM public.cgm_readings
          WHERE (cgm_readings.sg IS NOT NULL)
        ), episode_start AS (
         SELECT lag_data.ts,
            lag_data.sg,
            lag_data.is_low,
            sum(
                CASE
                    WHEN ((lag_data.is_low = 1) AND (lag_data.prev_low = 0)) THEN 1
                    ELSE 0
                END) OVER (ORDER BY lag_data.ts) AS episode_id
           FROM lag_data
          WHERE (lag_data.is_low = 1)
        ), episodes AS (
         SELECT episode_start.episode_id,
            min(episode_start.ts) AS start_time,
            max(episode_start.ts) AS end_time,
            (count(*) * 5) AS duration_minutes,
            (min(episode_start.sg) / 18.0182) AS min_bg_mmol,
            round(sum(((3.9 - (episode_start.sg / 18.0182)) * (5.0 / 60.0))), 2) AS auc_mmol_hours
           FROM episode_start
          GROUP BY episode_start.episode_id
         HAVING (count(*) >= 3)
        )
 SELECT e.episode_id,
    e.start_time,
    e.end_time,
    e.duration_minutes,
    e.min_bg_mmol,
    e.auc_mmol_hours,
    rec.recovery_time,
    round((EXTRACT(epoch FROM (rec.recovery_time - e.start_time)) / 60.0), 1) AS ttr_minutes
   FROM (episodes e
     LEFT JOIN LATERAL ( SELECT r.ts AS recovery_time
           FROM public.cgm_readings r
          WHERE ((r.ts > e.end_time) AND (r.sg >= (70)::numeric) AND (r.sg IS NOT NULL))
          ORDER BY r.ts
         LIMIT 1) rec ON (true))
  ORDER BY e.start_time DESC
  WITH NO DATA;


--
-- Name: layer2_daily_risk_stats; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_daily_risk_stats AS
 WITH config AS (
         SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)
         LIMIT 1
        ), daily_cgm_moments AS (
         SELECT ((sub.ts AT TIME ZONE config.tz_name))::date AS day,
            avg(
                CASE
                    WHEN ((1.509 * (power(NULLIF(ln(NULLIF(sub.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)) < (0)::numeric) THEN ((10)::numeric * power((1.509 * (power(NULLIF(ln(NULLIF(sub.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)), (2)::numeric))
                    ELSE (0)::numeric
                END) AS lbgi,
            avg(
                CASE
                    WHEN ((1.509 * (power(NULLIF(ln(NULLIF(sub.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)) > (0)::numeric) THEN ((10)::numeric * power((1.509 * (power(NULLIF(ln(NULLIF(sub.sg, (0)::numeric)), (0)::numeric), 1.084) - 5.381)), (2)::numeric))
                    ELSE (0)::numeric
                END) AS hbgi,
            count(*) FILTER (WHERE ((sub.sg > (250)::numeric) AND ((sub.prev_sg <= (250)::numeric) OR (sub.prev_sg IS NULL)))) AS events_high,
            count(*) FILTER (WHERE ((sub.sg < (54)::numeric) AND ((sub.prev_sg >= (54)::numeric) OR (sub.prev_sg IS NULL)))) AS events_low,
            sum(sqrt((power(COALESCE((sub.sg - sub.prev_sg), (0)::numeric), (2)::numeric) + power((EXTRACT(epoch FROM (sub.ts - sub.prev_ts)) / 60.0), (2)::numeric)))) AS curve_length,
            sum((EXTRACT(epoch FROM (sub.ts - sub.prev_ts)) / 60.0)) AS time_length,
            sum(abs(agg.bg_roc)) FILTER (WHERE (agg.bg_roc IS NOT NULL)) AS mag_abs_sum,
            count(agg.bg_roc) AS mag_n
           FROM ((( SELECT cgm_readings.ts,
                    cgm_readings.sg,
                    lag(cgm_readings.sg) OVER (PARTITION BY (((cgm_readings.ts AT TIME ZONE cfg.tz_name))::date) ORDER BY cgm_readings.ts) AS prev_sg,
                    lag(cgm_readings.ts) OVER (PARTITION BY (((cgm_readings.ts AT TIME ZONE cfg.tz_name))::date) ORDER BY cgm_readings.ts) AS prev_ts
                   FROM (public.cgm_readings
                     CROSS JOIN ( SELECT system_config.value AS tz_name
                           FROM public.system_config
                          WHERE (system_config.key = 'TIMEZONE'::text)
                         LIMIT 1) cfg)) sub
             CROSS JOIN config)
             LEFT JOIN public.layer2_five_minute_aggregate agg ON ((agg.ts = (date_trunc('minute'::text, sub.ts) - ((((EXTRACT(minute FROM sub.ts))::integer % 5))::double precision * '00:01:00'::interval)))))
          GROUP BY (((sub.ts AT TIME ZONE config.tz_name))::date)
        ), daily_episodes AS (
         SELECT ((layer2_hypo_episodes.start_time AT TIME ZONE ( SELECT config.tz_name
                   FROM config)))::date AS day,
            count(*) AS episode_count,
            avg(layer2_hypo_episodes.duration_minutes) AS avg_duration,
            avg(layer2_hypo_episodes.ttr_minutes) AS avg_ttr
           FROM public.layer2_hypo_episodes
          GROUP BY (((layer2_hypo_episodes.start_time AT TIME ZONE ( SELECT config.tz_name
                   FROM config)))::date)
        ), base AS (
         SELECT d.date,
            COALESCE(m.hbgi, (0)::numeric) AS hbgi,
            COALESCE(m.lbgi, (0)::numeric) AS lbgi,
            COALESCE(m.events_high, (0)::bigint) AS events_high,
            COALESCE(m.events_low, (0)::bigint) AS events_low,
            COALESCE(m.curve_length, (0)::numeric) AS curve_length,
            COALESCE(m.time_length, (0)::numeric) AS time_length,
            (COALESCE(m.curve_length, (0)::numeric) / NULLIF(m.time_length, (0)::numeric)) AS gvi,
            COALESCE(m.mag_abs_sum, (0)::numeric) AS mag_abs_sum,
            COALESCE(m.mag_n, (0)::bigint) AS mag_n,
            ((12.0 * COALESCE(m.mag_abs_sum, (0)::numeric)) / (NULLIF(m.mag_n, 0))::numeric) AS mag,
            c.sd_hourly,
            d.tdd,
            d.carbs,
            d.bg_readings,
            d.bg_sum,
            d.bg_sum2,
            d.n_vlow,
            d.n_low,
            d.n_tight,
            d.n_mod_high,
            d.n_high,
            d.n_vhigh,
            d.pct_tir,
            d.pct_titr,
            d.pct_tbr,
            d.pct_tar,
            d.pct_vlow,
            d.pct_low,
            d.pct_high,
            d.pct_vhigh,
            d.hypo_auc_sum,
            d.hyper_auc_sum,
            d.sg_coverage_pct,
            LEAST((100)::numeric, (d.pct_vlow + (0.8 * d.pct_low))) AS gri_hypo_component,
            LEAST((100)::numeric, (d.pct_vhigh + (0.5 * d.pct_high))) AS gri_hyper_component,
            COALESCE(ep.episode_count, (0)::bigint) AS episode_count,
            ep.avg_duration,
            ep.avg_ttr
           FROM (((public.layer2_daily_band_stats d
             LEFT JOIN daily_cgm_moments m ON ((d.date = m.day)))
             LEFT JOIN public.daily_circadian c ON ((d.date = c.day)))
             LEFT JOIN daily_episodes ep ON ((d.date = ep.day)))
        ), windowed AS (
         SELECT b.date,
            b.hbgi,
            b.lbgi,
            b.events_high,
            b.events_low,
            b.curve_length,
            b.time_length,
            b.gvi,
            b.mag_abs_sum,
            b.mag_n,
            b.mag,
            b.sd_hourly,
            b.tdd,
            b.carbs,
            b.bg_readings,
            b.bg_sum,
            b.bg_sum2,
            b.n_vlow,
            b.n_low,
            b.n_tight,
            b.n_mod_high,
            b.n_high,
            b.n_vhigh,
            b.pct_tir,
            b.pct_titr,
            b.pct_tbr,
            b.pct_tar,
            b.pct_vlow,
            b.pct_low,
            b.pct_high,
            b.pct_vhigh,
            b.hypo_auc_sum,
            b.hyper_auc_sum,
            b.sg_coverage_pct,
            b.gri_hypo_component,
            b.gri_hyper_component,
            b.episode_count,
            b.avg_duration,
            b.avg_ttr,
            sum(b.n_vlow) OVER w7 AS n_vlow_7d,
            sum(b.n_low) OVER w7 AS n_low_7d,
            sum(b.n_tight) OVER w7 AS n_tight_7d,
            sum(b.n_mod_high) OVER w7 AS n_mod_high_7d,
            sum(b.n_high) OVER w7 AS n_high_7d,
            sum(b.n_vhigh) OVER w7 AS n_vhigh_7d,
            sum(b.bg_readings) OVER w7 AS readings_7d,
            sum(b.bg_sum) OVER w7 AS bg_sum_7d,
            sum(b.bg_sum2) OVER w7 AS bg_sum2_7d,
            (sum((b.hbgi * (b.bg_readings)::numeric)) OVER w7 / NULLIF(sum(b.bg_readings) OVER w7, (0)::numeric)) AS hbgi_7d,
            (sum((b.lbgi * (b.bg_readings)::numeric)) OVER w7 / NULLIF(sum(b.bg_readings) OVER w7, (0)::numeric)) AS lbgi_7d,
            ((sum(b.mag_abs_sum) OVER w7 / NULLIF(sum(b.mag_n) OVER w7, (0)::numeric)) * 12.0) AS mag_7d,
            (sum(b.curve_length) OVER w7 / NULLIF(sum(b.time_length) OVER w7, (0)::numeric)) AS gvi_7d,
            avg(b.tdd) OVER w7 AS tdd_7d,
            avg(b.carbs) OVER w7 AS carbs_7d,
            avg(b.events_high) OVER w7 AS events_high_7d,
            avg(b.events_low) OVER w7 AS events_low_7d,
            avg(b.sd_hourly) OVER w7 AS sd_hourly_7d,
            sum(b.hypo_auc_sum) OVER w7 AS auc_hypo_sum_7d,
            sum(b.hyper_auc_sum) OVER w7 AS auc_hyper_sum_7d,
            sum(b.episode_count) OVER w7 AS episode_count_7d,
            avg(b.avg_duration) OVER w7 AS avg_episode_duration_7d,
            avg(b.avg_ttr) OVER w7 AS avg_ttr_7d,
            sum(b.n_vlow) OVER w14 AS n_vlow_14d,
            sum(b.n_low) OVER w14 AS n_low_14d,
            sum(b.n_tight) OVER w14 AS n_tight_14d,
            sum(b.n_mod_high) OVER w14 AS n_mod_high_14d,
            sum(b.n_high) OVER w14 AS n_high_14d,
            sum(b.n_vhigh) OVER w14 AS n_vhigh_14d,
            sum(b.bg_readings) OVER w14 AS readings_14d,
            sum(b.bg_sum) OVER w14 AS bg_sum_14d,
            sum(b.bg_sum2) OVER w14 AS bg_sum2_14d,
            (sum((b.hbgi * (b.bg_readings)::numeric)) OVER w14 / NULLIF(sum(b.bg_readings) OVER w14, (0)::numeric)) AS hbgi_14d,
            (sum((b.lbgi * (b.bg_readings)::numeric)) OVER w14 / NULLIF(sum(b.bg_readings) OVER w14, (0)::numeric)) AS lbgi_14d,
            ((sum(b.mag_abs_sum) OVER w14 / NULLIF(sum(b.mag_n) OVER w14, (0)::numeric)) * 12.0) AS mag_14d,
            (sum(b.curve_length) OVER w14 / NULLIF(sum(b.time_length) OVER w14, (0)::numeric)) AS gvi_14d,
            avg(b.tdd) OVER w14 AS tdd_14d,
            avg(b.carbs) OVER w14 AS carbs_14d,
            avg(b.events_high) OVER w14 AS events_high_14d,
            avg(b.events_low) OVER w14 AS events_low_14d,
            avg(b.sd_hourly) OVER w14 AS sd_hourly_14d,
            sum(b.hypo_auc_sum) OVER w14 AS auc_hypo_sum_14d,
            sum(b.hyper_auc_sum) OVER w14 AS auc_hyper_sum_14d,
            sum(b.bg_readings) OVER w14 AS sensor_readings_14d,
            sum(b.n_vlow) OVER w30 AS n_vlow_30d,
            sum(b.n_low) OVER w30 AS n_low_30d,
            sum(b.n_tight) OVER w30 AS n_tight_30d,
            sum(b.n_mod_high) OVER w30 AS n_mod_high_30d,
            sum(b.n_high) OVER w30 AS n_high_30d,
            sum(b.n_vhigh) OVER w30 AS n_vhigh_30d,
            sum(b.bg_readings) OVER w30 AS readings_30d,
            sum(b.bg_sum) OVER w30 AS bg_sum_30d,
            sum(b.bg_sum2) OVER w30 AS bg_sum2_30d,
            (sum((b.hbgi * (b.bg_readings)::numeric)) OVER w30 / NULLIF(sum(b.bg_readings) OVER w30, (0)::numeric)) AS hbgi_30d,
            (sum((b.lbgi * (b.bg_readings)::numeric)) OVER w30 / NULLIF(sum(b.bg_readings) OVER w30, (0)::numeric)) AS lbgi_30d,
            ((sum(b.mag_abs_sum) OVER w30 / NULLIF(sum(b.mag_n) OVER w30, (0)::numeric)) * 12.0) AS mag_30d,
            (sum(b.curve_length) OVER w30 / NULLIF(sum(b.time_length) OVER w30, (0)::numeric)) AS gvi_30d,
            avg(b.tdd) OVER w30 AS tdd_30d,
            avg(b.carbs) OVER w30 AS carbs_30d,
            avg(b.events_high) OVER w30 AS events_high_30d,
            avg(b.events_low) OVER w30 AS events_low_30d,
            avg(b.sd_hourly) OVER w30 AS sd_hourly_30d,
            sum(b.hypo_auc_sum) OVER w30 AS auc_hypo_sum_30d,
            sum(b.hyper_auc_sum) OVER w30 AS auc_hyper_sum_30d,
            sum(b.episode_count) OVER w30 AS episode_count_30d,
            avg(b.avg_duration) OVER w30 AS avg_episode_duration_30d,
            avg(b.avg_ttr) OVER w30 AS avg_ttr_30d,
            sum(b.n_vlow) OVER w60 AS n_vlow_60d,
            sum(b.n_low) OVER w60 AS n_low_60d,
            sum(b.n_tight) OVER w60 AS n_tight_60d,
            sum(b.n_mod_high) OVER w60 AS n_mod_high_60d,
            sum(b.n_high) OVER w60 AS n_high_60d,
            sum(b.n_vhigh) OVER w60 AS n_vhigh_60d,
            sum(b.bg_readings) OVER w60 AS readings_60d,
            sum(b.bg_sum) OVER w60 AS bg_sum_60d,
            sum(b.bg_sum2) OVER w60 AS bg_sum2_60d,
            (sum((b.hbgi * (b.bg_readings)::numeric)) OVER w60 / NULLIF(sum(b.bg_readings) OVER w60, (0)::numeric)) AS hbgi_60d,
            (sum((b.lbgi * (b.bg_readings)::numeric)) OVER w60 / NULLIF(sum(b.bg_readings) OVER w60, (0)::numeric)) AS lbgi_60d,
            ((sum(b.mag_abs_sum) OVER w60 / NULLIF(sum(b.mag_n) OVER w60, (0)::numeric)) * 12.0) AS mag_60d,
            (sum(b.curve_length) OVER w60 / NULLIF(sum(b.time_length) OVER w60, (0)::numeric)) AS gvi_60d,
            avg(b.tdd) OVER w60 AS tdd_60d,
            avg(b.carbs) OVER w60 AS carbs_60d,
            avg(b.events_high) OVER w60 AS events_high_60d,
            avg(b.events_low) OVER w60 AS events_low_60d,
            avg(b.sd_hourly) OVER w60 AS sd_hourly_60d,
            sum(b.hypo_auc_sum) OVER w60 AS auc_hypo_sum_60d,
            sum(b.hyper_auc_sum) OVER w60 AS auc_hyper_sum_60d,
            sum(b.episode_count) OVER w60 AS episode_count_60d,
            avg(b.avg_duration) OVER w60 AS avg_episode_duration_60d,
            avg(b.avg_ttr) OVER w60 AS avg_ttr_60d,
            sum(b.n_vlow) OVER w90 AS n_vlow_90d,
            sum(b.n_low) OVER w90 AS n_low_90d,
            sum(b.n_tight) OVER w90 AS n_tight_90d,
            sum(b.n_mod_high) OVER w90 AS n_mod_high_90d,
            sum(b.n_high) OVER w90 AS n_high_90d,
            sum(b.n_vhigh) OVER w90 AS n_vhigh_90d,
            sum(b.bg_readings) OVER w90 AS readings_90d,
            sum(b.bg_sum) OVER w90 AS bg_sum_90d,
            sum(b.bg_sum2) OVER w90 AS bg_sum2_90d,
            (sum((b.hbgi * (b.bg_readings)::numeric)) OVER w90 / NULLIF(sum(b.bg_readings) OVER w90, (0)::numeric)) AS hbgi_90d,
            (sum((b.lbgi * (b.bg_readings)::numeric)) OVER w90 / NULLIF(sum(b.bg_readings) OVER w90, (0)::numeric)) AS lbgi_90d,
            ((sum(b.mag_abs_sum) OVER w90 / NULLIF(sum(b.mag_n) OVER w90, (0)::numeric)) * 12.0) AS mag_90d,
            (sum(b.curve_length) OVER w90 / NULLIF(sum(b.time_length) OVER w90, (0)::numeric)) AS gvi_90d,
            avg(b.tdd) OVER w90 AS tdd_90d,
            avg(b.carbs) OVER w90 AS carbs_90d,
            avg(b.events_high) OVER w90 AS events_high_90d,
            avg(b.events_low) OVER w90 AS events_low_90d,
            avg(b.sd_hourly) OVER w90 AS sd_hourly_90d,
            sum(b.hypo_auc_sum) OVER w90 AS auc_hypo_sum_90d,
            sum(b.hyper_auc_sum) OVER w90 AS auc_hyper_sum_90d,
            sum(b.episode_count) OVER w90 AS episode_count_90d,
            avg(b.avg_duration) OVER w90 AS avg_episode_duration_90d,
            avg(b.avg_ttr) OVER w90 AS avg_ttr_90d
           FROM base b
          WINDOW w7 AS (ORDER BY b.date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), w14 AS (ORDER BY b.date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW), w30 AS (ORDER BY b.date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW), w60 AS (ORDER BY b.date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW), w90 AS (ORDER BY b.date ROWS BETWEEN 89 PRECEDING AND CURRENT ROW)
        )
 SELECT windowed.date,
    windowed.hbgi,
    windowed.lbgi,
    windowed.gvi,
    windowed.mag,
    windowed.curve_length,
    windowed.time_length,
    windowed.sd_hourly,
    windowed.tdd,
    windowed.carbs,
    windowed.events_high,
    windowed.events_low,
    windowed.bg_readings,
    windowed.bg_sum,
    windowed.bg_sum2,
    round(((windowed.bg_sum / (NULLIF(windowed.bg_readings, 0))::numeric) / 18.0182), 2) AS mean_mmol,
    round((sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2 / (NULLIF(windowed.bg_readings, 0))::numeric) - power((windowed.bg_sum / (NULLIF(windowed.bg_readings, 0))::numeric), (2)::numeric)))) / 18.0182), 2) AS sd_mmol,
    windowed.pct_tir AS tir,
    windowed.pct_titr AS titr,
    windowed.pct_tbr AS tbr,
    windowed.pct_tar AS tar,
    windowed.pct_vlow AS vlow,
    windowed.pct_low AS low,
    windowed.pct_high AS high,
    windowed.pct_vhigh AS vhigh,
    windowed.hypo_auc_sum,
    windowed.hyper_auc_sum,
    round(windowed.gri_hypo_component, 2) AS gri_hypo_component,
    round(windowed.gri_hyper_component, 2) AS gri_hyper_component,
    round(LEAST((100)::numeric, ((3.0 * windowed.gri_hypo_component) + (1.6 * windowed.gri_hyper_component))), 1) AS gri,
    windowed.sg_coverage_pct AS sensor_active_pct,
    windowed.episode_count,
    windowed.avg_duration AS avg_episode_duration,
    windowed.avg_ttr,
    round(((100.0 * (windowed.n_tight_7d + windowed.n_mod_high_7d)) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS tir_7d,
    round(((100.0 * windowed.n_tight_7d) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS titr_7d,
    round(((100.0 * (windowed.n_vlow_7d + windowed.n_low_7d)) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS tbr_7d,
    round(((100.0 * (windowed.n_high_7d + windowed.n_vhigh_7d)) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS tar_7d,
    round(((100.0 * windowed.n_vlow_7d) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS vlow_7d,
    round(((100.0 * windowed.n_low_7d) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS low_7d,
    round(((100.0 * windowed.n_high_7d) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS high_7d,
    round(((100.0 * windowed.n_vhigh_7d) / NULLIF(windowed.readings_7d, (0)::numeric)), 1) AS vhigh_7d,
    round(((windowed.bg_sum_7d / NULLIF(windowed.readings_7d, (0)::numeric)) / 18.0182), 2) AS mean_mmol_7d,
    round((sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_7d / NULLIF(windowed.readings_7d, (0)::numeric)) - power((windowed.bg_sum_7d / NULLIF(windowed.readings_7d, (0)::numeric)), (2)::numeric)))) / 18.0182), 2) AS sd_mmol_7d,
    round(((100.0 * sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_7d / NULLIF(windowed.readings_7d, (0)::numeric)) - power((windowed.bg_sum_7d / NULLIF(windowed.readings_7d, (0)::numeric)), (2)::numeric))))) / NULLIF((windowed.bg_sum_7d / NULLIF(windowed.readings_7d, (0)::numeric)), (0)::numeric)), 1) AS cv_7d,
    round((3.31 + (0.430995 * ((windowed.bg_sum_7d / NULLIF(windowed.readings_7d, (0)::numeric)) / 18.0182))), 2) AS gmi_7d,
    round(windowed.hbgi_7d, 2) AS hbgi_7d,
    round(windowed.lbgi_7d, 2) AS lbgi_7d,
    round(windowed.mag_7d, 2) AS mag_7d,
    round(windowed.gvi_7d, 3) AS gvi_7d,
    round(windowed.tdd_7d, 1) AS tdd_7d,
    round(windowed.carbs_7d, 1) AS carbs_7d,
    round(windowed.auc_hypo_sum_7d, 2) AS auc_hypo_sum_7d,
    round(windowed.auc_hyper_sum_7d, 2) AS auc_hyper_sum_7d,
    round(((100.0 * windowed.readings_7d) / (288.0 * (7)::numeric)), 1) AS sensor_active_pct_7d,
    windowed.episode_count_7d,
    round(windowed.avg_episode_duration_7d, 1) AS avg_episode_duration_7d,
    round(windowed.avg_ttr_7d, 1) AS avg_ttr_7d,
    round(((100.0 * (windowed.n_tight_14d + windowed.n_mod_high_14d)) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS tir_14d,
    round(((100.0 * windowed.n_tight_14d) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS titr_14d,
    round(((100.0 * (windowed.n_vlow_14d + windowed.n_low_14d)) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS tbr_14d,
    round(((100.0 * (windowed.n_high_14d + windowed.n_vhigh_14d)) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS tar_14d,
    round(((100.0 * windowed.n_vlow_14d) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS vlow_14d,
    round(((100.0 * windowed.n_low_14d) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS low_14d,
    round(((100.0 * windowed.n_high_14d) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS high_14d,
    round(((100.0 * windowed.n_vhigh_14d) / NULLIF(windowed.readings_14d, (0)::numeric)), 1) AS vhigh_14d,
    round(((windowed.bg_sum_14d / NULLIF(windowed.readings_14d, (0)::numeric)) / 18.0182), 2) AS mean_mmol_14d,
    round((sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_14d / NULLIF(windowed.readings_14d, (0)::numeric)) - power((windowed.bg_sum_14d / NULLIF(windowed.readings_14d, (0)::numeric)), (2)::numeric)))) / 18.0182), 2) AS sd_mmol_14d,
    round(((100.0 * sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_14d / NULLIF(windowed.readings_14d, (0)::numeric)) - power((windowed.bg_sum_14d / NULLIF(windowed.readings_14d, (0)::numeric)), (2)::numeric))))) / NULLIF((windowed.bg_sum_14d / NULLIF(windowed.readings_14d, (0)::numeric)), (0)::numeric)), 1) AS cv_14d,
    round((3.31 + (0.430995 * ((windowed.bg_sum_14d / NULLIF(windowed.readings_14d, (0)::numeric)) / 18.0182))), 2) AS gmi_14d,
    round(windowed.hbgi_14d, 2) AS hbgi_14d,
    round(windowed.lbgi_14d, 2) AS lbgi_14d,
    round(windowed.mag_14d, 2) AS mag_14d,
    round(windowed.gvi_14d, 3) AS gvi_14d,
    round(windowed.tdd_14d, 1) AS tdd_14d,
    round(windowed.carbs_14d, 1) AS carbs_14d,
    round(windowed.auc_hypo_sum_14d, 2) AS auc_hypo_sum_14d,
    round(windowed.auc_hyper_sum_14d, 2) AS auc_hyper_sum_14d,
    round(((100.0 * windowed.sensor_readings_14d) / (288.0 * (14)::numeric)), 1) AS sensor_active_pct_14d,
    round(((100.0 * (windowed.n_tight_30d + windowed.n_mod_high_30d)) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS tir_30d,
    round(((100.0 * windowed.n_tight_30d) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS titr_30d,
    round(((100.0 * (windowed.n_vlow_30d + windowed.n_low_30d)) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS tbr_30d,
    round(((100.0 * (windowed.n_high_30d + windowed.n_vhigh_30d)) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS tar_30d,
    round(((100.0 * windowed.n_vlow_30d) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS vlow_30d,
    round(((100.0 * windowed.n_low_30d) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS low_30d,
    round(((100.0 * windowed.n_high_30d) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS high_30d,
    round(((100.0 * windowed.n_vhigh_30d) / NULLIF(windowed.readings_30d, (0)::numeric)), 1) AS vhigh_30d,
    round(((windowed.bg_sum_30d / NULLIF(windowed.readings_30d, (0)::numeric)) / 18.0182), 2) AS mean_mmol_30d,
    round((sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_30d / NULLIF(windowed.readings_30d, (0)::numeric)) - power((windowed.bg_sum_30d / NULLIF(windowed.readings_30d, (0)::numeric)), (2)::numeric)))) / 18.0182), 2) AS sd_mmol_30d,
    round(((100.0 * sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_30d / NULLIF(windowed.readings_30d, (0)::numeric)) - power((windowed.bg_sum_30d / NULLIF(windowed.readings_30d, (0)::numeric)), (2)::numeric))))) / NULLIF((windowed.bg_sum_30d / NULLIF(windowed.readings_30d, (0)::numeric)), (0)::numeric)), 1) AS cv_30d,
    round((3.31 + (0.430995 * ((windowed.bg_sum_30d / NULLIF(windowed.readings_30d, (0)::numeric)) / 18.0182))), 2) AS gmi_30d,
    round(windowed.hbgi_30d, 2) AS hbgi_30d,
    round(windowed.lbgi_30d, 2) AS lbgi_30d,
    round(windowed.mag_30d, 2) AS mag_30d,
    round(windowed.gvi_30d, 3) AS gvi_30d,
    round(windowed.tdd_30d, 1) AS tdd_30d,
    round(windowed.carbs_30d, 1) AS carbs_30d,
    round(windowed.auc_hypo_sum_30d, 2) AS auc_hypo_sum_30d,
    round(windowed.auc_hyper_sum_30d, 2) AS auc_hyper_sum_30d,
    round(((100.0 * windowed.readings_30d) / (288.0 * (30)::numeric)), 1) AS sensor_active_pct_30d,
    windowed.episode_count_30d,
    round(windowed.avg_episode_duration_30d, 1) AS avg_episode_duration_30d,
    round(windowed.avg_ttr_30d, 1) AS avg_ttr_30d,
    round(((100.0 * (windowed.n_tight_60d + windowed.n_mod_high_60d)) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS tir_60d,
    round(((100.0 * windowed.n_tight_60d) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS titr_60d,
    round(((100.0 * (windowed.n_vlow_60d + windowed.n_low_60d)) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS tbr_60d,
    round(((100.0 * (windowed.n_high_60d + windowed.n_vhigh_60d)) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS tar_60d,
    round(((100.0 * windowed.n_vlow_60d) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS vlow_60d,
    round(((100.0 * windowed.n_low_60d) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS low_60d,
    round(((100.0 * windowed.n_high_60d) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS high_60d,
    round(((100.0 * windowed.n_vhigh_60d) / NULLIF(windowed.readings_60d, (0)::numeric)), 1) AS vhigh_60d,
    round(((windowed.bg_sum_60d / NULLIF(windowed.readings_60d, (0)::numeric)) / 18.0182), 2) AS mean_mmol_60d,
    round((sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_60d / NULLIF(windowed.readings_60d, (0)::numeric)) - power((windowed.bg_sum_60d / NULLIF(windowed.readings_60d, (0)::numeric)), (2)::numeric)))) / 18.0182), 2) AS sd_mmol_60d,
    round(((100.0 * sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_60d / NULLIF(windowed.readings_60d, (0)::numeric)) - power((windowed.bg_sum_60d / NULLIF(windowed.readings_60d, (0)::numeric)), (2)::numeric))))) / NULLIF((windowed.bg_sum_60d / NULLIF(windowed.readings_60d, (0)::numeric)), (0)::numeric)), 1) AS cv_60d,
    round((3.31 + (0.430995 * ((windowed.bg_sum_60d / NULLIF(windowed.readings_60d, (0)::numeric)) / 18.0182))), 2) AS gmi_60d,
    round(windowed.hbgi_60d, 2) AS hbgi_60d,
    round(windowed.lbgi_60d, 2) AS lbgi_60d,
    round(windowed.mag_60d, 2) AS mag_60d,
    round(windowed.gvi_60d, 3) AS gvi_60d,
    round(windowed.tdd_60d, 1) AS tdd_60d,
    round(windowed.carbs_60d, 1) AS carbs_60d,
    round(windowed.auc_hypo_sum_60d, 2) AS auc_hypo_sum_60d,
    round(windowed.auc_hyper_sum_60d, 2) AS auc_hyper_sum_60d,
    round(((100.0 * windowed.readings_60d) / (288.0 * (60)::numeric)), 1) AS sensor_active_pct_60d,
    windowed.episode_count_60d,
    round(windowed.avg_episode_duration_60d, 1) AS avg_episode_duration_60d,
    round(windowed.avg_ttr_60d, 1) AS avg_ttr_60d,
    round(((100.0 * (windowed.n_tight_90d + windowed.n_mod_high_90d)) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS tir_90d,
    round(((100.0 * windowed.n_tight_90d) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS titr_90d,
    round(((100.0 * (windowed.n_vlow_90d + windowed.n_low_90d)) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS tbr_90d,
    round(((100.0 * (windowed.n_high_90d + windowed.n_vhigh_90d)) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS tar_90d,
    round(((100.0 * windowed.n_vlow_90d) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS vlow_90d,
    round(((100.0 * windowed.n_low_90d) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS low_90d,
    round(((100.0 * windowed.n_high_90d) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS high_90d,
    round(((100.0 * windowed.n_vhigh_90d) / NULLIF(windowed.readings_90d, (0)::numeric)), 1) AS vhigh_90d,
    round(((windowed.bg_sum_90d / NULLIF(windowed.readings_90d, (0)::numeric)) / 18.0182), 2) AS mean_mmol_90d,
    round((sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_90d / NULLIF(windowed.readings_90d, (0)::numeric)) - power((windowed.bg_sum_90d / NULLIF(windowed.readings_90d, (0)::numeric)), (2)::numeric)))) / 18.0182), 2) AS sd_mmol_90d,
    round(((100.0 * sqrt(GREATEST((0)::numeric, ((windowed.bg_sum2_90d / NULLIF(windowed.readings_90d, (0)::numeric)) - power((windowed.bg_sum_90d / NULLIF(windowed.readings_90d, (0)::numeric)), (2)::numeric))))) / NULLIF((windowed.bg_sum_90d / NULLIF(windowed.readings_90d, (0)::numeric)), (0)::numeric)), 1) AS cv_90d,
    round((3.31 + (0.430995 * ((windowed.bg_sum_90d / NULLIF(windowed.readings_90d, (0)::numeric)) / 18.0182))), 2) AS gmi_90d,
    round(windowed.hbgi_90d, 2) AS hbgi_90d,
    round(windowed.lbgi_90d, 2) AS lbgi_90d,
    round(windowed.mag_90d, 2) AS mag_90d,
    round(windowed.gvi_90d, 3) AS gvi_90d,
    round(windowed.tdd_90d, 1) AS tdd_90d,
    round(windowed.carbs_90d, 1) AS carbs_90d,
    round(windowed.auc_hypo_sum_90d, 2) AS auc_hypo_sum_90d,
    round(windowed.auc_hyper_sum_90d, 2) AS auc_hyper_sum_90d,
    round(((100.0 * windowed.readings_90d) / (288.0 * (90)::numeric)), 1) AS sensor_active_pct_90d,
    windowed.episode_count_90d,
    round(windowed.avg_episode_duration_90d, 1) AS avg_episode_duration_90d,
    round(windowed.avg_ttr_90d, 1) AS avg_ttr_90d
   FROM windowed
  ORDER BY windowed.date DESC
  WITH NO DATA;


--
-- Name: layer2_hourly_stats; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_hourly_stats AS
 WITH hourly_spine AS (
         SELECT date_trunc('hour'::text, layer2_basal_5min.minute_ts) AS hour_ts,
            layer2_basal_5min.day
           FROM public.layer2_basal_5min
          GROUP BY (date_trunc('hour'::text, layer2_basal_5min.minute_ts)), layer2_basal_5min.day
        ), hourly_basal AS (
         SELECT date_trunc('hour'::text, layer2_basal_5min.minute_ts) AS hour_ts,
            (sum(layer2_basal_5min.actual_rate) / 12.0) AS basal_sum
           FROM public.layer2_basal_5min
          GROUP BY (date_trunc('hour'::text, layer2_basal_5min.minute_ts))
        ), hourly_treatments AS (
         SELECT date_trunc('hour'::text, (treatments.ts AT TIME ZONE config.tz_name)) AS hour_ts,
            sum(
                CASE
                    WHEN ((treatments.event_type = ANY (ARRAY['Meal Bolus'::text, 'Correction Bolus'::text, 'Bolus Wizard'::text, 'Meal'::text])) OR (treatments.smb_flag = true)) THEN treatments.insulin
                    ELSE (0)::numeric
                END) AS bolus_sum,
            sum(treatments.carbs) AS carbs_sum
           FROM (public.treatments
             CROSS JOIN ( SELECT system_config.value AS tz_name
                   FROM public.system_config
                  WHERE (system_config.key = 'TIMEZONE'::text)) config)
          GROUP BY (date_trunc('hour'::text, (treatments.ts AT TIME ZONE config.tz_name)))
        )
 SELECT h.hour_ts AS "Hour",
    h.day AS "Date",
    round(COALESCE(b.basal_sum, (0)::numeric), 3) AS "Basal",
    round(COALESCE(t.bolus_sum, (0)::numeric), 3) AS "Bolus",
    round(COALESCE(t.carbs_sum, (0)::numeric), 1) AS "Carbs"
   FROM ((hourly_spine h
     LEFT JOIN hourly_basal b ON ((h.hour_ts = b.hour_ts)))
     LEFT JOIN hourly_treatments t ON ((h.hour_ts = t.hour_ts)))
  ORDER BY h.hour_ts DESC
  WITH NO DATA;


--
-- Name: layer2_temp_basal_impact; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer2_temp_basal_impact AS
 SELECT t.minute_ts,
    tb.absolute AS acts_temp_absolute
   FROM (public.layer2_five_minute_timeline t
     LEFT JOIN LATERAL ( SELECT treatments.absolute
           FROM public.treatments
          WHERE ((treatments.event_type = 'Temp Basal'::text) AND (treatments.ts <= t.minute_ts) AND (treatments.ts >= (t.minute_ts - '02:00:00'::interval)) AND ((treatments.ts + ((COALESCE(treatments.duration, (0)::numeric) || ' minutes'::text))::interval) > t.minute_ts) AND (NOT (EXISTS ( SELECT 1
                   FROM public.treatments t2
                  WHERE ((t2.ts > treatments.ts) AND (t2.ts <= t.minute_ts) AND ((t2.event_type = 'Temp Basal'::text) OR (t2.event_type = 'Profile Switch'::text)))))))
          ORDER BY treatments.ts DESC
         LIMIT 1) tb ON (true))
  WHERE (tb.absolute IS NOT NULL)
  WITH NO DATA;


--
-- Name: layer2_insulin_timeline; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.layer2_insulin_timeline AS
 SELECT b.minute_ts,
    b.day,
    COALESCE(b.base_rate, (0)::numeric) AS base_rate,
        CASE
            WHEN (tmp.acts_temp_absolute IS NOT NULL) THEN tmp.acts_temp_absolute
            ELSE COALESCE(b.base_rate, (0)::numeric)
        END AS actual_rate
   FROM (public.layer2_base_basal_per_minute b
     LEFT JOIN public.layer2_temp_basal_impact tmp ON ((b.minute_ts = tmp.minute_ts)));


--
-- Name: layer2_meal_starts; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.layer2_meal_starts AS
 WITH ordered_carbs AS (
         SELECT treatments.ts,
            treatments.carbs,
                CASE
                    WHEN ((treatments.ts - lag(treatments.ts) OVER (ORDER BY treatments.ts)) > '01:00:00'::interval) THEN 1
                    ELSE 0
                END AS is_new_group
           FROM public.treatments
          WHERE (treatments.carbs > (0)::numeric)
        ), grouped_carbs AS (
         SELECT ordered_carbs.ts,
            ordered_carbs.carbs,
            sum(ordered_carbs.is_new_group) OVER (ORDER BY ordered_carbs.ts) AS group_id
           FROM ordered_carbs
        )
 SELECT min(grouped_carbs.ts) AS meal_start_time,
    sum(grouped_carbs.carbs) AS total_carbs
   FROM grouped_carbs
  GROUP BY grouped_carbs.group_id
 HAVING (sum(grouped_carbs.carbs) >= (15)::numeric);


--
-- Name: layer2_meal_analysis; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.layer2_meal_analysis AS
 WITH meal_windows AS (
         SELECT m.meal_start_time,
            m.total_carbs,
            lead(m.meal_start_time) OVER (ORDER BY m.meal_start_time) AS next_meal_start
           FROM public.layer2_meal_starts m
        ), calculated_windows AS (
         SELECT meal_windows.meal_start_time,
            meal_windows.total_carbs,
            meal_windows.next_meal_start,
            LEAST((meal_windows.meal_start_time + '04:00:00'::interval), COALESCE(meal_windows.next_meal_start, (meal_windows.meal_start_time + '04:00:00'::interval))) AS monitoring_end_time,
                CASE
                    WHEN ((meal_windows.next_meal_start IS NOT NULL) AND (meal_windows.next_meal_start < (meal_windows.meal_start_time + '04:00:00'::interval))) THEN true
                    ELSE false
                END AS is_curtailed
           FROM meal_windows
        )
 SELECT w.meal_start_time,
    w.total_carbs,
    ((w.meal_start_time AT TIME ZONE config.tz_name))::date AS day,
    w.is_curtailed,
    (start_bg.sg / 18.0182) AS start_bg_mmol,
    (max_bg.max_sg / 18.0182) AS max_bg_mmol,
    ((max_bg.max_sg - start_bg.sg) / 18.0182) AS delta_mmol,
    (EXTRACT(epoch FROM (max_bg.peak_time - w.meal_start_time)) / (60)::numeric) AS time_to_peak_minutes
   FROM (((calculated_windows w
     CROSS JOIN ( SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)) config)
     LEFT JOIN LATERAL ( SELECT cgm_readings.sg
           FROM public.cgm_readings
          WHERE ((cgm_readings.ts >= (w.meal_start_time - '00:15:00'::interval)) AND (cgm_readings.ts <= (w.meal_start_time + '00:15:00'::interval)))
          ORDER BY (abs(EXTRACT(epoch FROM (cgm_readings.ts - w.meal_start_time))))
         LIMIT 1) start_bg ON (true))
     LEFT JOIN LATERAL ( SELECT cgm_readings.sg AS max_sg,
            cgm_readings.ts AS peak_time
           FROM public.cgm_readings
          WHERE ((cgm_readings.ts > w.meal_start_time) AND (cgm_readings.ts <= w.monitoring_end_time))
          ORDER BY cgm_readings.sg DESC, cgm_readings.ts
         LIMIT 1) max_bg ON (true))
  WHERE (start_bg.sg IS NOT NULL);


--
-- Name: layer2_minute_timeline; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.layer2_minute_timeline AS
 SELECT layer2_five_minute_timeline.minute_ts,
    layer2_five_minute_timeline.day,
    layer2_five_minute_timeline.seconds_of_day
   FROM public.layer2_five_minute_timeline;


--
-- Name: layer4_agp_periods; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.layer4_agp_periods AS
 WITH config AS (
         SELECT system_config.value AS tz_name
           FROM public.system_config
          WHERE (system_config.key = 'TIMEZONE'::text)
        ), periods AS (
         SELECT '7 Days'::text AS period,
            '7 days'::interval AS duration,
            1 AS sort_order
        UNION ALL
         SELECT '30 Days'::text AS period,
            '30 days'::interval AS duration,
            2 AS sort_order
        UNION ALL
         SELECT '90 Days'::text AS period,
            '90 days'::interval AS duration,
            3 AS sort_order
        )
 SELECT p.period,
    (floor((EXTRACT(minute FROM (r.ts AT TIME ZONE c.tz_name)) / (15)::numeric)) * (15)::numeric) AS minute_bucket_start,
    EXTRACT(hour FROM (r.ts AT TIME ZONE c.tz_name)) AS hour_of_day,
    (('2000-01-01'::date + ((EXTRACT(hour FROM (r.ts AT TIME ZONE c.tz_name)) || ' hours'::text))::interval) + (((floor((EXTRACT(minute FROM (r.ts AT TIME ZONE c.tz_name)) / (15)::numeric)) * (15)::numeric) || ' minutes'::text))::interval) AS plot_time,
    round(((percentile_cont((0.10)::double precision) WITHIN GROUP (ORDER BY ((r.sg)::double precision)) / (18.0182)::double precision))::numeric, 1) AS p10,
    round(((percentile_cont((0.25)::double precision) WITHIN GROUP (ORDER BY ((r.sg)::double precision)) / (18.0182)::double precision))::numeric, 1) AS p25,
    round(((percentile_cont((0.50)::double precision) WITHIN GROUP (ORDER BY ((r.sg)::double precision)) / (18.0182)::double precision))::numeric, 1) AS p50,
    round(((percentile_cont((0.75)::double precision) WITHIN GROUP (ORDER BY ((r.sg)::double precision)) / (18.0182)::double precision))::numeric, 1) AS p75,
    round(((percentile_cont((0.90)::double precision) WITHIN GROUP (ORDER BY ((r.sg)::double precision)) / (18.0182)::double precision))::numeric, 1) AS p90,
    count(*) AS readings_count,
    p.sort_order
   FROM ((public.cgm_readings r
     CROSS JOIN config c)
     JOIN periods p ON ((r.ts >= (now() - p.duration))))
  WHERE (r.sg IS NOT NULL)
  GROUP BY p.period, (floor((EXTRACT(minute FROM (r.ts AT TIME ZONE c.tz_name)) / (15)::numeric)) * (15)::numeric), (EXTRACT(hour FROM (r.ts AT TIME ZONE c.tz_name))), (('2000-01-01'::date + ((EXTRACT(hour FROM (r.ts AT TIME ZONE c.tz_name)) || ' hours'::text))::interval) + (((floor((EXTRACT(minute FROM (r.ts AT TIME ZONE c.tz_name)) / (15)::numeric)) * (15)::numeric) || ' minutes'::text))::interval), p.sort_order
  ORDER BY p.sort_order, (EXTRACT(hour FROM (r.ts AT TIME ZONE c.tz_name))), (floor((EXTRACT(minute FROM (r.ts AT TIME ZONE c.tz_name)) / (15)::numeric)) * (15)::numeric)
  WITH NO DATA;


--
-- Name: note_categories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_categories (
    id integer NOT NULL,
    name text NOT NULL,
    color text DEFAULT '#3498db'::text NOT NULL,
    is_builtin boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: note_categories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.note_categories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: note_categories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.note_categories_id_seq OWNED BY public.note_categories.id;


--
-- Name: note_category_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_category_map (
    note_id integer NOT NULL,
    category_id integer NOT NULL
);


--
-- Name: site_changes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.site_changes (
    ts timestamp with time zone NOT NULL
);


--
-- Name: pod_sessions_raw; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.pod_sessions_raw AS
 WITH sc AS (
         SELECT site_changes.ts AS start_ts,
            lead(site_changes.ts) OVER (ORDER BY site_changes.ts) AS next_start_ts
           FROM public.site_changes
        )
 SELECT sc.start_ts,
    sc.next_start_ts,
        CASE
            WHEN (sc.next_start_ts IS NOT NULL) THEN sc.next_start_ts
            WHEN ((now() - sc.start_ts) >= '80:00:00'::interval) THEN (sc.start_ts + '80:00:00'::interval)
            ELSE NULL::timestamp with time zone
        END AS end_ts
   FROM sc
  ORDER BY sc.start_ts;


--
-- Name: pod_sessions_capped; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.pod_sessions_capped AS
 SELECT pod_sessions_raw.start_ts,
    pod_sessions_raw.next_start_ts,
    pod_sessions_raw.end_ts,
        CASE
            WHEN (pod_sessions_raw.end_ts IS NULL) THEN NULL::timestamp with time zone
            ELSE LEAST(pod_sessions_raw.end_ts, (pod_sessions_raw.start_ts + '80:00:00'::interval))
        END AS capped_end_ts,
        CASE
            WHEN (pod_sessions_raw.end_ts IS NULL) THEN NULL::numeric
            ELSE (EXTRACT(epoch FROM (LEAST(pod_sessions_raw.end_ts, (pod_sessions_raw.start_ts + '80:00:00'::interval)) - pod_sessions_raw.start_ts)) / 3600.0)
        END AS duration_hours,
        CASE
            WHEN (pod_sessions_raw.end_ts IS NULL) THEN 'active'::text
            WHEN (pod_sessions_raw.next_start_ts IS NULL) THEN 'capped'::text
            WHEN (pod_sessions_raw.end_ts > (pod_sessions_raw.start_ts + '80:00:00'::interval)) THEN 'capped'::text
            ELSE 'completed'::text
        END AS status
   FROM public.pod_sessions_raw
  ORDER BY pod_sessions_raw.start_ts;


--
-- Name: saved_views; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_views (
    id integer NOT NULL,
    page text NOT NULL,
    name text NOT NULL,
    payload jsonb NOT NULL,
    is_builtin boolean DEFAULT false NOT NULL,
    source_default_id text,
    is_deleted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: saved_views_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.saved_views ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.saved_views_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.targets (
    id integer NOT NULL,
    name text NOT NULL,
    rule jsonb NOT NULL,
    target_kind text DEFAULT 'evaluative'::text NOT NULL,
    display_defaults jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: targets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.targets ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.targets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: clinical_notes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_notes ALTER COLUMN id SET DEFAULT nextval('public.clinical_notes_id_seq'::regclass);


--
-- Name: ingestion_coverage id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingestion_coverage ALTER COLUMN id SET DEFAULT nextval('public.ingestion_coverage_id_seq'::regclass);


--
-- Name: note_categories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_categories ALTER COLUMN id SET DEFAULT nextval('public.note_categories_id_seq'::regclass);


--
-- Name: cgm_readings cgm_readings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cgm_readings
    ADD CONSTRAINT cgm_readings_pkey PRIMARY KEY (ts);


--
-- Name: chart_metric_settings chart_metric_settings_page_metric_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chart_metric_settings
    ADD CONSTRAINT chart_metric_settings_page_metric_id_key UNIQUE (page, metric_id);


--
-- Name: chart_metric_settings chart_metric_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chart_metric_settings
    ADD CONSTRAINT chart_metric_settings_pkey PRIMARY KEY (id);


--
-- Name: clinical_notes clinical_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_notes
    ADD CONSTRAINT clinical_notes_pkey PRIMARY KEY (id);


--
-- Name: dashboard_layouts dashboard_layouts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_layouts
    ADD CONSTRAINT dashboard_layouts_pkey PRIMARY KEY (id);


--
-- Name: dashboard_layouts dashboard_layouts_slot_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_layouts
    ADD CONSTRAINT dashboard_layouts_slot_number_key UNIQUE (slot_number);


--
-- Name: dashboard_widgets dashboard_widgets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_widgets
    ADD CONSTRAINT dashboard_widgets_pkey PRIMARY KEY (id);


--
-- Name: devicestatus devicestatus_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.devicestatus
    ADD CONSTRAINT devicestatus_pkey PRIMARY KEY (ts);


--
-- Name: filters filters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.filters
    ADD CONSTRAINT filters_pkey PRIMARY KEY (id);


--
-- Name: ingestion_coverage ingestion_coverage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingestion_coverage
    ADD CONSTRAINT ingestion_coverage_pkey PRIMARY KEY (id);


--
-- Name: ingestion_state ingestion_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ingestion_state
    ADD CONSTRAINT ingestion_state_pkey PRIMARY KEY (source);


--
-- Name: layer2_basal_5min layer2_basal_5min_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.layer2_basal_5min
    ADD CONSTRAINT layer2_basal_5min_pkey PRIMARY KEY (minute_ts);


--
-- Name: layer2_five_minute_aggregate layer2_five_minute_aggregate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.layer2_five_minute_aggregate
    ADD CONSTRAINT layer2_five_minute_aggregate_pkey PRIMARY KEY (ts);


--
-- Name: metric_thresholds metric_thresholds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.metric_thresholds
    ADD CONSTRAINT metric_thresholds_pkey PRIMARY KEY (setting_key);


--
-- Name: note_categories note_categories_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_categories
    ADD CONSTRAINT note_categories_name_key UNIQUE (name);


--
-- Name: note_categories note_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_categories
    ADD CONSTRAINT note_categories_pkey PRIMARY KEY (id);


--
-- Name: note_category_map note_category_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_category_map
    ADD CONSTRAINT note_category_map_pkey PRIMARY KEY (note_id, category_id);


--
-- Name: saved_views saved_views_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_pkey PRIMARY KEY (id);


--
-- Name: site_changes site_changes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.site_changes
    ADD CONSTRAINT site_changes_pkey PRIMARY KEY (ts);


--
-- Name: system_config system_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

-- system_config_pkey included in table definition


--
-- Name: targets targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.targets
    ADD CONSTRAINT targets_pkey PRIMARY KEY (id);


--
-- Name: treatments treatments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.treatments
    ADD CONSTRAINT treatments_pkey PRIMARY KEY (ts, raw_id);


--
-- Name: chart_metric_settings_page_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chart_metric_settings_page_idx ON public.chart_metric_settings USING btree (page);


--
-- Name: filters_name_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filters_name_idx ON public.filters USING btree (name);


--
-- Name: idx_5min_agg_day; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_5min_agg_day ON public.layer2_five_minute_aggregate USING btree (day);


--
-- Name: idx_agp_raw_day; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agp_raw_day ON public.layer2_agp_raw USING btree (day);


--
-- Name: idx_agp_raw_plot_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agp_raw_plot_time ON public.layer2_agp_raw USING btree (plot_time);


--
-- Name: idx_basal_5min_day; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_basal_5min_day ON public.layer2_basal_5min USING btree (day);


--
-- Name: idx_cgm_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cgm_ts ON public.cgm_readings USING btree (ts);


--
-- Name: idx_cgm_ts_sg; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cgm_ts_sg ON public.cgm_readings USING btree (ts, sg);


--
-- Name: idx_clinical_notes_aaps_raw_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_clinical_notes_aaps_raw_id ON public.clinical_notes USING btree (raw_treatment_id, raw_treatment_ts) WHERE (raw_treatment_id IS NOT NULL);


--
-- Name: idx_clinical_notes_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_notes_date ON public.clinical_notes USING btree (date);


--
-- Name: idx_clinical_notes_date_desc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_notes_date_desc ON public.clinical_notes USING btree (date DESC, is_timeless DESC, time_created DESC NULLS LAST);


--
-- Name: idx_clinical_notes_type_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_notes_type_date ON public.clinical_notes USING btree (note_type, date);


--
-- Name: idx_clinical_notes_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clinical_notes_updated ON public.clinical_notes USING btree (updated_at DESC);


--
-- Name: idx_coverage_stream_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coverage_stream_ts ON public.ingestion_coverage USING btree (stream, start_ts, end_ts);


--
-- Name: idx_dashboard_widgets_layout; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_widgets_layout ON public.dashboard_widgets USING btree (layout_id);


--
-- Name: idx_dev_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dev_ts ON public.devicestatus USING btree (ts);


--
-- Name: idx_l2_agp_iob_cob_time; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_l2_agp_iob_cob_time ON public.layer2_agp_iob_cob_raw USING btree (day, minute_bucket);


--
-- Name: idx_l2_hourly_stats_time; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_l2_hourly_stats_time ON public.layer2_hourly_stats USING btree ("Hour");


--
-- Name: idx_l2_hypo_episodes_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_l2_hypo_episodes_start ON public.layer2_hypo_episodes USING btree (start_time);


--
-- Name: idx_l2_profile_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_l2_profile_time ON public.layer2_profile_schedule USING btree (start_time, end_time);


--
-- Name: idx_l2_temp_basal_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_l2_temp_basal_ts ON public.layer2_temp_basal_impact USING btree (minute_ts);


--
-- Name: idx_l4_agp_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_l4_agp_period ON public.layer4_agp_periods USING btree (period);


--
-- Name: idx_note_cat_map_cat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_note_cat_map_cat ON public.note_category_map USING btree (category_id);


--
-- Name: idx_treat_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_treat_ts ON public.treatments USING btree (ts);


--
-- Name: idx_treat_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_treat_type ON public.treatments USING btree (event_type);


--
-- Name: idx_treat_type_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_treat_type_ts ON public.treatments USING btree (event_type, ts DESC);


--
-- Name: idx_uq_agp_raw_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_agp_raw_ts ON public.layer2_agp_raw USING btree (ts);


--
-- Name: idx_uq_l2_daily_band_stats_date; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l2_daily_band_stats_date ON public.layer2_daily_band_stats USING btree (date);


--
-- Name: idx_uq_l2_daily_period_stats; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l2_daily_period_stats ON public.layer2_daily_period_stats USING btree (date, period);


--
-- Name: idx_uq_l2_daily_risk_stats_date; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l2_daily_risk_stats_date ON public.layer2_daily_risk_stats USING btree (date);


--
-- Name: idx_uq_l2_hypo_episodes_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l2_hypo_episodes_id ON public.layer2_hypo_episodes USING btree (episode_id);


--
-- Name: idx_uq_l2_profile_schedule; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l2_profile_schedule ON public.layer2_profile_schedule USING btree (start_time, start_seconds);


--
-- Name: idx_uq_l2_temp_basal_impact; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l2_temp_basal_impact ON public.layer2_temp_basal_impact USING btree (minute_ts);


--
-- Name: idx_uq_l4_agp_periods; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_uq_l4_agp_periods ON public.layer4_agp_periods USING btree (period, hour_of_day, minute_bucket_start);


--
-- Name: saved_views_page_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX saved_views_page_idx ON public.saved_views USING btree (page);


--
-- Name: saved_views_source_default_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX saved_views_source_default_idx ON public.saved_views USING btree (page, source_default_id) WHERE (source_default_id IS NOT NULL);


--
-- Name: targets_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX targets_kind_idx ON public.targets USING btree (target_kind);


--
-- Name: dashboard_widgets dashboard_widgets_layout_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_widgets
    ADD CONSTRAINT dashboard_widgets_layout_id_fkey FOREIGN KEY (layout_id) REFERENCES public.dashboard_layouts(id) ON DELETE CASCADE;


--
-- Name: note_category_map note_category_map_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_category_map
    ADD CONSTRAINT note_category_map_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.note_categories(id) ON DELETE CASCADE;


--
-- Name: note_category_map note_category_map_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_category_map
    ADD CONSTRAINT note_category_map_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.clinical_notes(id) ON DELETE CASCADE;


--
-- Seed Built-in System Config, Categories, Layouts, Widgets & Metric Thresholds
--

-- 1. System Config Default Timezone

-- 2. Built-in Clinical Note Categories (Weight & HbA1c)
INSERT INTO public.note_categories (name, color, is_builtin)
VALUES 
    ('Weight', '#2ecc71', TRUE),
    ('HbA1c', '#9b59b6', TRUE)
ON CONFLICT (name) DO NOTHING;

-- 3. Metric Thresholds (Spider Chart Analytics)
INSERT INTO public.metric_thresholds (setting_key, value_low, value_high, params, label) VALUES
('spider_tir', 40, 85, '{"t_ideal": 85, "t_critical": 40, "formula": "A", "zone": 1}'::jsonb, 'Spider Chart: TIR (Time in Range 3.9-10.0 mmol/L)'),
('spider_titr', 20, 60, '{"t_ideal": 60, "t_critical": 20, "formula": "A", "zone": 1}'::jsonb, 'Spider Chart: TITR (Time in Tight Range 3.9-7.8 mmol/L)'),
('spider_tbr', 0, 8, '{"t_ideal": 0, "t_critical": 8, "formula": "B", "zone": 1}'::jsonb, 'Spider Chart: TBR (Time Below Range <3.9 mmol/L)'),
('spider_tar', 5, 50, '{"t_ideal": 5, "t_critical": 50, "formula": "B", "zone": 1}'::jsonb, 'Spider Chart: TAR (Time Above Range >10.0 mmol/L)'),
('spider_gmi', 5.5, 8.5, '{"t_ideal": 5.5, "t_critical": 8.5, "formula": "C", "zone": 2}'::jsonb, 'Spider Chart: GMI (Glucose Management Indicator, %)'),
('spider_cv', 28, 45, '{"t_ideal": 28, "t_critical": 45, "formula": "C", "zone": 2}'::jsonb, 'Spider Chart: CV (Coefficient of Variation, %)'),
('spider_mag', 1.0, 3.5, '{"t_ideal": 1.0, "t_critical": 3.5, "formula": "B", "zone": 2}'::jsonb, 'Spider Chart: MAG (Mean Absolute Glucose change, mmol/L/hr)'),
('spider_lbgi', 0.0, 5.0, '{"t_ideal": 0.0, "t_critical": 5.0, "formula": "B", "zone": 3}'::jsonb, 'Spider Chart: LBGI (Low Blood Glucose Index)'),
('spider_hbgi', 0.0, 12.0, '{"t_ideal": 0.0, "t_critical": 12.0, "formula": "B", "zone": 3}'::jsonb, 'Spider Chart: HBGI (High Blood Glucose Index)'),
('spider_gri', 20, 80, '{"t_ideal": 20, "t_critical": 80, "formula": "B", "zone": 3}'::jsonb, 'Spider Chart: GRI (Glycemia Risk Index, Zone A top to Zone E bottom)')
ON CONFLICT (setting_key) DO NOTHING;

-- 4. Dashboard Layout Slots (1-6)
INSERT INTO public.dashboard_layouts (slot_number, display_order, name, is_active)
VALUES 
    (1, 1, 'Daily Overview', true),
    (2, 2, 'Clinical Deep Dive', false),
    (3, 3, 'Device & Sensor Health', false),
    (4, 4, 'Weekly Trends', false),
    (5, 5, 'Exercise & Activity', false),
    (6, 6, 'Custom Sandbox', false)
ON CONFLICT (slot_number) DO NOTHING;

-- 5. Dashboard Widgets for Slot 1 (Daily Overview)
INSERT INTO public.dashboard_widgets (layout_id, widget_type, title, x, y, w, h, config)
SELECT l.id, w.widget_type, w.title, w.x, w.y, w.w, w.h, w.config::jsonb
FROM public.dashboard_layouts l
CROSS JOIN (
    VALUES
        ('tir_scorecard', 'Time in Range (TIR)', 0, 0, 4, 3, '{"range": "standard"}'),
        ('avg_bg_scorecard', 'Average Glucose', 4, 0, 4, 3, '{"scope": "all_day"}'),
        ('gmi_scorecard', 'GMI (Est. HbA1c)', 8, 0, 4, 3, '{"unit": "percent"}'),
        ('current_bg_hero', 'Current Glucose & Trend', 0, 3, 4, 3, '{}'),
        ('iob_cob_stat', 'Active IOB & COB', 4, 3, 4, 3, '{"sparkline": true}'),
        ('hypo_free_streak', 'Days Free of Severe Hypo', 8, 3, 4, 3, '{"threshold": 3.0}')
) AS w(widget_type, title, x, y, w, h, config)
WHERE l.slot_number = 1
  AND NOT EXISTS (SELECT 1 FROM public.dashboard_widgets WHERE layout_id = l.id);

-- 6. Dashboard Widgets for Slot 2 (Clinical Deep Dive)
INSERT INTO public.dashboard_widgets (layout_id, widget_type, title, x, y, w, h, config)
SELECT l.id, w.widget_type, w.title, w.x, w.y, w.w, w.h, w.config::jsonb
FROM public.dashboard_layouts l
CROSS JOIN (
    VALUES
        ('triage_focus_table', 'Rolling-Week Triage Focus Table', 0, 0, 8, 5, '{}'),
        ('gri_scorecard', 'Glycemia Risk Index (GRI)', 8, 0, 4, 3, '{}'),
        ('cv_scorecard', 'Coefficient of Variation (CV)', 8, 3, 4, 2, '{}')
) AS w(widget_type, title, x, y, w, h, config)
WHERE l.slot_number = 2
  AND NOT EXISTS (SELECT 1 FROM public.dashboard_widgets WHERE layout_id = l.id);

-- 7. Dashboard Widgets for Slot 3 (Device & Sensor Health)
INSERT INTO public.dashboard_widgets (layout_id, widget_type, title, x, y, w, h, config)
SELECT l.id, w.widget_type, w.title, w.x, w.y, w.w, w.h, w.config::jsonb
FROM public.dashboard_layouts l
CROSS JOIN (
    VALUES
        ('pod_countdown_timer', 'Pod Lifetime Countdown (80h)', 0, 0, 4, 3, '{}'),
        ('reservoir_monitor', 'Reservoir Remaining', 4, 0, 4, 3, '{}'),
        ('hypo_free_streak', 'Days Free of Severe Hypo', 8, 0, 4, 3, '{}')
) AS w(widget_type, title, x, y, w, h, config)
WHERE l.slot_number = 3
  AND NOT EXISTS (SELECT 1 FROM public.dashboard_widgets WHERE layout_id = l.id);


-- migrate:down
-- Baseline down-migration is a no-op to prevent catastrophic accidental data drops
SELECT 1;
