/*
 * agp.js — Declarative metric registry for Patterns (agp.html).
 *
 * Conforms to the canonical registry schema in docs/analysis/data-model-reference.md.
 * Output: renderFamily: "canvas", xAxisUnit: "minute_bucket".
 */
(function (global) {
    'use strict';

    var AGP_REGISTRY = [
        {
            id: 'agp',
            label: 'AGP',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'percentiles',
            renderType: 'band',
            unit: 'mmol/L',
            axis: {
                group: 'bg',
                side: 'left',
                label: 'Glucose (mmol/L)',
                decimals: 1,
                defaultBounds: { min: 2.0, max: 16.0 },
                clamp: { min: 1.0, max: 25.0 }
            },
            style: {
                color: '#2278b5',
                lineType: 'solid',
                defaultWeight: 1,
                opacity: 0.60
            }
        },
        {
            id: 'median_bg',
            label: 'Median BG',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'p50',
            renderType: 'line',
            unit: 'mmol/L',
            axis: {
                group: 'bg',
                side: 'left',
                label: 'Glucose (mmol/L)',
                decimals: 1,
                defaultBounds: { min: 2.0, max: 16.0 },
                clamp: { min: 1.0, max: 25.0 }
            },
            style: {
                color: '#1a1a1a',
                lineType: 'solid',
                defaultWeight: 3,
                opacity: 1.0
            }
        },
        {
            id: 'tir',
            label: 'TIR (3.9-10)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'tir_pct_smooth',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'pct',
                side: 'right',
                label: 'Percentage (%)',
                decimals: 0,
                defaultBounds: { min: 0, max: 100 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#27ae60',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'titr',
            label: 'TITR (3.9-7.8)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'titr_pct_smooth',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'pct',
                side: 'right',
                label: 'Percentage (%)',
                decimals: 0,
                defaultBounds: { min: 0, max: 100 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#16a085',
                lineType: 'dashed',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'tir_heat',
            label: 'TIR Heat',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'heat_bands',
            renderType: 'area',
            unit: '%',
            axis: {
                group: 'pct',
                side: 'right',
                label: 'Percentage (%)',
                decimals: 0,
                defaultBounds: { min: 0, max: 100 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#16a085',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 0.8
            }
        },
        {
            id: 'cv',
            label: 'CV (%)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'cv_pct_smooth',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'cv',
                side: 'right',
                label: 'CV (%)',
                decimals: 1,
                defaultBounds: { min: 0, max: 60 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#9b59b6',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'hypo_auc',
            label: 'Hypo AUC (<3.9)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'hypo_auc_smooth',
            renderType: 'line',
            unit: 'mmol*min',
            axis: {
                group: 'auc',
                side: 'right',
                label: 'AUC',
                decimals: 1,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#e74c3c',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'hyper_auc',
            label: 'Hyper AUC (>7.8)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'hyper_auc_smooth',
            renderType: 'line',
            unit: 'mmol*min',
            axis: {
                group: 'auc',
                side: 'right',
                label: 'AUC',
                decimals: 1,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#e67e22',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'lbgi',
            label: 'LBGI',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'lbgi_smooth',
            renderType: 'line',
            unit: '',
            axis: {
                group: 'risk',
                side: 'right',
                label: 'LBGI / HBGI',
                decimals: 2,
                defaultBounds: { min: 0, max: 10 },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#c0392b',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'hbgi',
            label: 'HBGI',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'hbgi_smooth',
            renderType: 'line',
            unit: '',
            axis: {
                group: 'risk',
                side: 'right',
                label: 'LBGI / HBGI',
                decimals: 2,
                defaultBounds: { min: 0, max: 10 },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#d35400',
                lineType: 'dashed',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'basal_profile',
            label: 'Basal Profile',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'basal',
            renderType: 'step',
            unit: 'U/hr',
            axis: {
                group: 'basal',
                side: 'left',
                label: 'Basal (U/hr)',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#607d8b',
                lineType: 'solid',
                defaultWeight: 1.5,
                opacity: 1.0
            }
        },
        {
            id: 'cob',
            label: 'COB (gms/10)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'avg_cob_smooth',
            renderType: 'line',
            unit: 'gms/10',
            axis: {
                group: 'carbs',
                side: 'left',
                label: 'Carbs / IOB',
                decimals: 1,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#d35400',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'iob',
            label: 'IOB (U)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'avg_iob',
            renderType: 'line',
            unit: 'U',
            axis: {
                group: 'carbs',
                side: 'left',
                label: 'Carbs / IOB',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: null, max: null }
            },
            style: {
                color: '#2980b9',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'dynamic_isf',
            label: 'Dynamic ISF',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'isf_smooth',
            renderType: 'line',
            unit: 'mmol/L/U',
            axis: {
                group: 'isf',
                side: 'left',
                label: 'ISF (mmol/L/U)',
                decimals: 1,
                defaultBounds: { min: 0, max: 10 },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#3498db',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'median_bgi',
            label: 'Median BGI',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'bgi_p50',
            renderType: 'line',
            unit: 'mmol/L',
            axis: {
                group: 'bgidev',
                side: 'right',
                label: 'BGI / Dev',
                decimals: 2,
                defaultBounds: { min: null, max: null },
                clamp: { min: null, max: null }
            },
            style: {
                color: '#8e44ad',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'median_dev',
            label: 'Median Deviation',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'dev_p50',
            renderType: 'line',
            unit: 'mmol/L',
            axis: {
                group: 'bgidev',
                side: 'right',
                label: 'BGI / Dev',
                decimals: 2,
                defaultBounds: { min: null, max: null },
                clamp: { min: null, max: null }
            },
            style: {
                color: '#16a085',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'low_events',
            label: 'Low Events',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'low_count',
            renderType: 'scatter',
            unit: 'events',
            axis: {
                group: 'events',
                side: 'right',
                label: 'Events',
                decimals: 0,
                defaultBounds: { min: 0, max: 10 },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#e74c3c',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'warning_events',
            label: 'Warning Events',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'warning_count',
            renderType: 'scatter',
            unit: 'events',
            axis: {
                group: 'events',
                side: 'right',
                label: 'Events',
                decimals: 0,
                defaultBounds: { min: 0, max: 10 },
                clamp: { min: 0, max: null }
            },
            style: {
                color: '#f39c12',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'controller_effort',
            label: 'Controller Effort (%)',
            renderFamily: 'canvas',
            xAxisUnit: 'minute_bucket',
            group: null,
            provider: {
                endpoint: '/api/v1/agp_data',
                params: {}
            },
            dataKey: 'ce_pct_smooth_2h',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'pct',
                side: 'right',
                label: 'Percentage (%)',
                decimals: 0,
                defaultBounds: { min: 0, max: 100 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#00838f',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        }
    ];

    global.AGP_REGISTRY = AGP_REGISTRY;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { AGP_REGISTRY: AGP_REGISTRY };
    }
})(typeof window !== 'undefined' ? window : this);
