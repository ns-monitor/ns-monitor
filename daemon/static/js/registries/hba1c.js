/*
 * hba1c.js — Declarative metric registry for HbA1c Graph.
 * Output: renderFamily: "canvas", xAxisUnit: "calendar_date".
 */
(function (global) {
    'use strict';

    var HBA1C_REGISTRY = [
        {
            id: 'hba1c_lab',
            label: 'HbA1c (Lab)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: 'inline_hba1c', // We inject RAW_HBA1C_DATA
                params: {}
            },
            dataKey: 'hba1c_percent',
            renderType: 'scatter',
            unit: '%',
            axis: {
                group: 'hba1c',
                side: 'left',
                label: 'HbA1c / GMI (%)',
                decimals: 1,
                defaultBounds: { min: 4.5, max: null },
                clamp: { min: 4.0, max: 20.0 }
            },
            style: {
                color: '#e74c3c', // Red to match time and range heatmap
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 1.0,
                symbol: 'circle',
                symbolSize: 8
            }
        },
        {
            id: 'gmi_90d',
            label: 'GMI (90d average)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/hba1c_overlay',
                params: {}
            },
            dataKey: 'gmi_90d',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'hba1c',
                side: 'left',
                label: 'HbA1c / GMI (%)',
                decimals: 1,
                defaultBounds: { min: 4.5, max: null },
                clamp: { min: 4.0, max: 20.0 }
            },
            style: {
                color: '#f39c12', // Orange/Yellow to contrast
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 0.8
            }
        },
        {
            id: 'ehba1c_90d',
            label: 'eHbA1c (90d average)',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/hba1c_overlay',
                params: {}
            },
            dataKey: 'ehba1c_90d',
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'hba1c',
                side: 'left',
                label: 'HbA1c / GMI (%)',
                decimals: 1,
                defaultBounds: { min: 4.5, max: null },
                clamp: { min: 4.0, max: 20.0 }
            },
            style: {
                color: '#27ae60', // Green
                lineType: 'solid',
                defaultWeight: 2,
                opacity: 0.8
            }
        },
        {
            id: 'custom_gmi',
            label: 'Custom GMI',
            renderFamily: 'canvas',
            xAxisUnit: 'calendar_date',
            group: null,
            provider: {
                endpoint: '/api/v1/hba1c_overlay',
                params: {}
            },
            dataKey: 'custom_gmi_val', // We map this locally in JS
            renderType: 'line',
            unit: '%',
            axis: {
                group: 'hba1c',
                side: 'left',
                label: 'HbA1c / GMI (%)',
                decimals: 1,
                defaultBounds: { min: 4.5, max: null },
                clamp: { min: 4.0, max: 20.0 }
            },
            style: {
                color: '#9b59b6', // Purple
                lineType: 'dashed',
                defaultWeight: 2,
                opacity: 0.8
            }
        }
    ];

    var PAGE_DEFAULTS = {
        primaryId: 'hba1c_lab',
        defaultActive: ['hba1c_lab', 'gmi_90d', 'ehba1c_90d']
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { REGISTRY: HBA1C_REGISTRY, DEFAULTS: PAGE_DEFAULTS };
    } else {
        global.NS_HBA1C_REGISTRY = HBA1C_REGISTRY;
        global.NS_HBA1C_DEFAULTS = PAGE_DEFAULTS;
    }

    // --- Custom interceptor for orchestrator ---
    // The orchestrator calls `fetch()` for each endpoint. We need to handle `inline_hba1c` by resolving immediately with window.RAW_HBA1C_DATA, 
    // and for `/api/v1/hba1c_overlay` we calculate the `custom_gmi_val` on the fly.
    global.nsInterceptFetch = function(endpoint, urlParams, rawData) {
        if (endpoint === 'inline_hba1c') {
            // Re-format inline hba1c records into the orchestrator expected array format (date: 'YYYY-MM-DD')
            var arr = (window.RAW_HBA1C_DATA || []).map(function(row) {
                return {
                    date: row.date,
                    hba1c_percent: parseFloat(row.hba1c_percent)
                };
            });
            return Promise.resolve({ data: arr });
        }
        
        // Let the default orchestrator fetch happen, but post-process the overlay to inject Custom GMI
        return fetch(endpoint + '?' + urlParams)
            .then(res => res.json())
            .then(json => {
                if (json.data && endpoint === '/api/v1/hba1c_overlay') {
                    var constA = parseFloat(document.getElementById('gmi-const-a').value);
                    var constB = parseFloat(document.getElementById('gmi-const-b').value);
                    if (!Number.isFinite(constA)) constA = 3.31;
                    if (!Number.isFinite(constB)) constB = 0.4306;
                    json.data.forEach(function(row) {
                        if (row.mean_mmol_90d) {
                            row.custom_gmi_val = Math.round((constA + (constB * row.mean_mmol_90d)) * 10) / 10;
                        } else {
                            row.custom_gmi_val = null;
                        }
                    });
                }
                return json;
            });
    };

})(typeof window !== 'undefined' ? window : this);
