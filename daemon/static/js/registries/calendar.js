/*
 * calendar.js — Panel registry entries for the Calendar page (Phase 7a).
 *
 * Conforms to the registry entry schema in docs/analysis/data-model-reference.md.
 * Output: renderFamily: "panel", xAxisUnit: "calendar_date", renderType: "calendar-cell".
 */
(function (global) {
    'use strict';

    var CALENDAR_REGISTRY = [
        {
            // Front-layer content type per docs/analysis/calendar-cell-content-model.md
            // -- draws on top of fill/badge/icon as an opaque stroke, only
            // claims the pixels it passes through. Linear axis, 2-15 mmol/L range.
            id: 'bg_sparkline',
            label: 'BG Sparkline',
            renderFamily: 'panel',
            xAxisUnit: 'calendar_date',
            provider: {
                endpoint: '/api/v1/calendar/sparkline_data',
                method: 'GET',
                params: {}
            },
            dataKey: 'data',
            renderType: 'calendar-cell',
            encoding: 'sparkline',
            color: 'var(--accent)',
            yAxisMin: 2,
            yAxisMax: 15
        }
    ];

    global.CALENDAR_REGISTRY = CALENDAR_REGISTRY;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { CALENDAR_REGISTRY: CALENDAR_REGISTRY };
    }
})(typeof window !== 'undefined' ? window : this);
