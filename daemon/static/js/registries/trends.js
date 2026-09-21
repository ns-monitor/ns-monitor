/*
 * trends.js — Declarative metric registry for Trends (timeline.html).
 *
 * Conforms to the registry entry schema in docs/analysis/data-model-reference.md.
 * Output: renderFamily: "canvas", xAxisUnit: "calendar_date".
 */
(function (global) {
    'use strict';

    var TRENDS_REGISTRY = [
        {
            id: 'gmi',
            label: 'GMI (%)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'gmi',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'gmi',
                side: 'left',
                label: 'GMI (%)',
                decimals: 1,
                defaultBounds: { min: 5, max: null },
                clamp: { min: 4.0, max: 20.0 }
            },
            style: {
                color: '#3498db',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'tir',
            label: 'TIR (%)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'tir',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'pct',
                side: 'left',
                label: 'TIR / TITR (%)',
                decimals: 0,
                defaultBounds: { min: 0, max: 100 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#2ecc71',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'titr',
            label: 'TITR (%)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'titr',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'pct',
                side: 'left',
                label: 'TIR / TITR (%)',
                decimals: 0,
                defaultBounds: { min: 0, max: 100 },
                clamp: { min: 0, max: 100 }
            },
            style: {
                color: '#27ae60',
                lineType: 'dashed',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'avg_bg',
            label: 'Avg BG',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'avg_bg',
            renderType: 'line',
            unit: 'mmol/L',
            axis: {
                group: 'avg_bg',
                side: 'left',
                label: 'Avg BG (mmol/L)',
                decimals: 1,
                defaultBounds: { min: null, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#9b59b6',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'cv',
            label: 'CV (%)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'cv',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'cv',
                side: 'left',
                label: 'CV (%)',
                decimals: 1,
                defaultBounds: { min: null, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#8e44ad',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'gvi',
            label: 'GVI',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'gvi',
            renderType: 'line',
            unit: '',
            axis: {
                group: 'gvi',
                side: 'right',
                label: 'GVI',
                decimals: 2,
                defaultBounds: { min: null, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#f39c12',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'lbgi',
            label: 'LBGI',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'lbgi',
            renderType: 'line',
            unit: '',
            axis: {
                group: 'risk',
                side: 'right',
                label: 'LBGI / HBGI',
                decimals: 2,
                defaultBounds: { min: null, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#ff5252',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'hbgi',
            label: 'HBGI',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'hbgi',
            renderType: 'line',
            unit: '',
            axis: {
                group: 'risk',
                side: 'right',
                label: 'LBGI / HBGI',
                decimals: 2,
                defaultBounds: { min: null, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#800000',
                lineType: 'dashed',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'tdd',
            label: 'TDD',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'tdd',
            renderType: 'bar',
            unit: 'U',
            axis: {
                group: 'tdd',
                side: 'right',
                label: 'TDD (U)',
                decimals: 1,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#7f8c8d',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'tdd_weight',
            label: 'TDD / Weight',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'tdd_weight',
            renderType: 'line',
            unit: 'U/kg',
            axis: {
                group: 'tdd_weight',
                side: 'right',
                label: 'TDD / Weight (U/kg)',
                decimals: 2,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#16a085',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        },
        {
            id: 'carbs',
            label: 'Carbs (g)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/trends_data',
                params: { grain: 'daily' }
            },
            dataKey: 'carbs',
            renderType: 'bar',
            unit: 'g',
            axis: {
                group: 'carbs',
                side: 'right',
                label: 'Carbs (g)',
                decimals: 0,
                defaultBounds: { min: 0, max: null },
                clamp: { min: 0 }
            },
            style: {
                color: '#f1c40f',
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0
            }
        }
    ];

    global.TRENDS_REGISTRY = TRENDS_REGISTRY;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { TRENDS_REGISTRY: TRENDS_REGISTRY };
    }
})(typeof window !== 'undefined' ? window : this);
