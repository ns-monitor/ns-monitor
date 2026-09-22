-- migrate:up
-- A chained FULL JOIN could emit two rows for one calendar date when basal
-- and bolus data existed without CGM data. Rebuild around a canonical spine.

CREATE TEMP TABLE _daily_band_stats_risk_definition AS
SELECT pg_get_viewdef('public.layer2_daily_risk_stats'::regclass, true) AS definition;

DROP MATERIALIZED VIEW public.layer2_daily_risk_stats;
DROP MATERIALIZED VIEW public.layer2_daily_band_stats;

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
        ), all_days AS (
         SELECT day
           FROM daily_cgm
        UNION
         SELECT day
           FROM daily_basal
        UNION
         SELECT day
           FROM daily_bolus
        )
 SELECT d.day AS date,
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
   FROM (((all_days d
     LEFT JOIN daily_cgm cgm ON ((cgm.day = d.day)))
     LEFT JOIN daily_basal db ON ((db.day = d.day)))
     LEFT JOIN daily_bolus bol ON ((bol.day = d.day)))
  ORDER BY d.day DESC
  WITH NO DATA;
CREATE UNIQUE INDEX idx_uq_l2_daily_band_stats_date
    ON public.layer2_daily_band_stats USING btree (date);


DO $$
DECLARE
    risk_definition text;
BEGIN
    SELECT definition INTO risk_definition
    FROM _daily_band_stats_risk_definition;

    EXECUTE format(
        'CREATE MATERIALIZED VIEW public.layer2_daily_risk_stats AS %s WITH NO DATA',
        regexp_replace(risk_definition, ';[[:space:]]*$', '')
    );
END $$;

CREATE UNIQUE INDEX idx_uq_l2_daily_risk_stats_date
    ON public.layer2_daily_risk_stats USING btree (date);

DROP TABLE _daily_band_stats_risk_definition;

-- migrate:down
-- Reverting would reintroduce duplicate daily rows; this derived-data-only
-- migration therefore has a deliberately safe no-op down migration.
SELECT 1;
