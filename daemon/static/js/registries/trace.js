/*
 * trace.js — Declarative metric registry for Trace (dashboard_daily.html).
 *
 * Conforms to the registry entry schema in docs/analysis/data-model-reference.md.
 * Output: renderFamily: "canvas", xAxisUnit: "calendar_date".
 */
(function (global) {
    'use strict';

    var TRACE_REGISTRY = [
        {
            id: 'bg',
            label: 'BG',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'bg',
            renderType: 'line',
            unit: 'mmol/L',
            axis: {
                group: 'bg',
                side: 'left',
                label: 'BG (mmol/L)',
                decimals: 1,
                defaultBounds: { min: 0, max: 18 },
                clamp: { min: 0.0, max: 30.0 }
            },
            style: {
                color: '#111111',
                lineType: 'solid',
                defaultWeight: 2.5,
                opacity: 1.0
            }
        },
        {
            id: 'carbs',
            label: 'Carbs',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'carbs',
            renderType: 'bar',
            unit: 'g',
            axis: {
                group: 'carbs',
                side: 'left',
                label: 'Carbs / COB',
                decimals: 0,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: null }
            },
            style: {
                color: '#e9a800',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'cob',
            label: 'COB',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'cob',
            renderType: 'line',
            unit: 'g',
            axis: {
                group: 'carbs',
                side: 'left',
                label: 'Carbs / COB',
                decimals: 0,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: null }
            },
            style: {
                color: '#e67e22',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'bolus',
            label: 'Bolus',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'bolus_insulin',
            renderType: 'bar',
            unit: 'U',
            axis: {
                group: 'bolus',
                side: 'left',
                label: 'Bolus / IOB',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: null }
            },
            style: {
                color: '#c084fc',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'iob',
            label: 'IOB',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'iob',
            renderType: 'line',
            unit: 'U',
            axis: {
                group: 'bolus',
                side: 'left',
                label: 'Bolus / IOB',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: 30.0 }
            },
            style: {
                color: '#8e44ad',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'scheduled_basal',
            label: 'Basal Profile',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'scheduled_basal',
            renderType: 'step',
            unit: 'U/hr',
            axis: {
                group: 'basal',
                side: 'right',
                label: 'Basal (U/hr)',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: null }
            },
            style: {
                color: '#a0aec0',
                lineType: 'dashed',
                defaultWeight: 1.5,
                opacity: 1.0
            }
        },
        {
            id: 'basal_rate',
            label: 'Basal Rate',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'basal_rate',
            renderType: 'step',
            unit: 'U/hr',
            axis: {
                group: 'basal',
                side: 'right',
                label: 'Basal (U/hr)',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: null }
            },
            style: {
                color: '#6366f1',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        /*
         * DEV / BGI (AAPS "Variable sensitivity" style) -- 4 Sep 2026 split.
         *
         * Three separately-toggleable items, all reading the same
         * server-derived fields (dev_mgdl, neg_bgi_mgdl -- see
         * get_daily_chart_data_range()), so no backend change was needed to
         * add this split:
         *
         *  - `dev_bgi`   built-in composite: the classifier-coloured bars +
         *                the BGI line together on one axis, one toggle. Not
         *                per-metric style-editable -- see
         *                BUILTIN_METRIC_IDS in ns-chart-customizer.js. This
         *                is the original AAPS-style diagnostic chart.
         *  - `deviation` plain single-colour line/bar version of Deviation,
         *                fully style-editable. Shares an axis with `bgi`.
         *  - `bgi`       plain single-colour BGI line, fully style-editable
         *                (unchanged from before the split). Shares an axis
         *                with `deviation`.
         *
         * Deliberately mg/dL, not Trace's usual mmol/L: AAPS's own deviation
         * graph is always drawn in mg/dL regardless of the user's display
         * units, and Constants.DEVIATION_TO_BE_EQUAL = 2.0 is an mg/dL
         * threshold.
         */
        {
            id: 'dev_bgi',
            label: 'DEV-BGI',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            // Composite entries have no single dataKey -- the rendering loop
            // expands `parts` into one series per part instead. Each part is
            // otherwise shaped like a normal registry entry (dataKey,
            // renderType, optional colorBy, style).
            composite: true,
            parts: [
                {
                    id: 'dev_bgi_bars',
                    label: 'Deviation',
                    dataKey: 'dev_mgdl',
                    renderType: 'bar',
                    colorBy: {
                        key: 'dev_class',
                        palette: {
                            RES:   'rgba(46,160,46,0.85)',   // resistance  -- BG above expectation
                            SENS:  'rgba(214,45,45,0.85)',   // sensitivity -- BG below expectation
                            UAM:   'rgba(190,170,60,0.90)',  // unannounced meal
                            COB:   'rgba(120,120,120,0.80)', // carb absorption
                            EQUAL: 'rgba(40,40,40,0.75)'     // within the +/-2.0 mg/dL noise floor
                        },
                        fallback: 'rgba(40,40,40,0.75)'
                    },
                    style: { color: '#555555', lineType: 'solid', defaultWeight: 1, opacity: 1.0 }
                },
                {
                    id: 'dev_bgi_line',
                    label: 'BGI',
                    dataKey: 'neg_bgi_mgdl',
                    renderType: 'line',
                    style: { color: '#00A0A0', lineType: 'solid', defaultWeight: 1.5, opacity: 1.0 }
                }
            ],
            // Composite has no single editable colour -- locked, same as the
            // old per-bar colorBy entry was.
            lockColor: true,
            unit: 'mg/dL',
            axis: {
                group: 'dev_bgi_composite',
                side: 'right',
                label: 'DEV-BGI (mg/dL per 5min)',
                decimals: 0,
                defaultBounds: { min: -25, max: 25 },
                clamp: { min: -200, max: 200 }
            },
            style: {
                color: '#555555',
                lineType: 'solid',
                defaultWeight: 1,
                opacity: 1.0
            }
        },
        {
            id: 'deviation',
            label: 'Deviation',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'dev_mgdl',
            renderType: 'line',
            unit: 'mg/dL',
            axis: {
                group: 'devbgi_raw',
                side: 'right',
                label: 'Deviation / BGI (mg/dL per 5min)',
                decimals: 0,
                defaultBounds: { min: -25, max: 25 },
                clamp: { min: -200, max: 200 }
            },
            style: {
                color: '#555555',
                lineType: 'solid',
                defaultWeight: 1.5,
                opacity: 1.0
            }
        },
        {
            id: 'bgi',
            label: 'BGI',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'neg_bgi_mgdl',
            renderType: 'line',
            unit: 'mg/dL',
            axis: {
                group: 'devbgi_raw',
                side: 'right',
                label: 'Deviation / BGI (mg/dL per 5min)',
                decimals: 0,
                defaultBounds: { min: -25, max: 25 },
                clamp: { min: -200, max: 200 }
            },
            style: {
                color: '#00A0A0',
                lineType: 'solid',
                defaultWeight: 1.5,
                opacity: 1.0
            }
        },
        {
            id: 'isf',
            label: 'ISF',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/daily_chart_data_range',
                params: {}
            },
            dataKey: 'isf',
            renderType: 'line',
            unit: 'mmol/L/U',
            axis: {
                group: 'isf',
                side: 'right',
                label: 'ISF',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0.0, max: null }
            },
            style: {
                color: '#3498db',
                lineType: 'solid',
                defaultWeight: 1.5,
                opacity: 1.0
            }
        }
    ];

    global.TRACE_REGISTRY = TRACE_REGISTRY;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { TRACE_REGISTRY: TRACE_REGISTRY };
    }
})(typeof window !== 'undefined' ? window : this);
