/*
 * ns-chart-customizer.js — Unified chart display customization engine.
 *
 * Responsibilities:
 * 1. 3-tier style resolution: Code Baseline (Registry) -> Global Default (/api/v1/chart_settings) -> View Override (payload.styles)
 * 2. In-page Metric Style Modal: full styling controls with "Save for this view" vs "Save as system-wide default".
 * 3. Popover checklist renderer: Calendar-style searchable checklist for series visibility.
 * 4. Active-only legend calculation for ECharts.
 */
(function (global) {
    'use strict';

    // Metric ids that are composite/built-in graphs (multiple sub-series or
    // a stateful classifier drives their appearance) rather than a single
    // pickable-color series. No per-metric style editing is offered for
    // these -- their legends break out the actual sub-parts instead.
    // 'dev_bgi' (Trace) is the classifier-coloured Deviation+-BGI composite,
    // split from the standalone `deviation`/`bgi` plain metrics 4 Sep 2026.
    var BUILTIN_METRIC_IDS = ['agp', 'tir_heat', 'dev_bgi'];

    // Marker types offered for Line-rendered metrics. Size is not a user
    // control -- always 3x the metric's line thickness (draft.defaultWeight),
    // computed wherever a marker is actually drawn (preview SVG here, and
    // ECharts symbolSize on Trends/Trace/Patterns' series).
    var MARKER_TYPES = [
        { value: 'none', label: 'None' },
        { value: 'circle', label: 'Circle' },
        { value: 'triangle', label: 'Triangle' },
        { value: 'cross', label: 'Cross' },
        { value: 'square', label: 'Square' }
    ];

    // Curve-smoothing tension steps, Trends/Patterns only (visual-only
    // ECharts spline interpolation -- see 'off' == smooth:false). Exposed
    // as the full 0.1 increments for now while Harry tunes what the
    // shipping Light/Med/Heavy presets should map to; not a decision on the
    // final user-facing control shape.
    var SMOOTH_LEVELS = ['off', 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0];

    // Pages where curve smoothing applies at all. Trace is deliberately
    // excluded -- Harry: "Smoothing has no place on trace" (4 Sep 2026) --
    // each raw CGM/pump reading is real, not a value MAVG or a backend
    // *_smooth column has already reduced, so curving it visually would
    // misrepresent single readings the same way unclamped overshoot does.
    var SMOOTHING_PAGES = ['trends', 'patterns', 'hba1c'];

    var _globalSettings = {
        patterns: null,
        trends: null,
        trace: null,
        hba1c: null
    };

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function getPageMenuLabel(pageKey, fallback) {
        if (typeof document !== 'undefined') {
            var link = document.querySelector('.sb-link[href="/' + pageKey + '"] span');
            if (link && link.textContent && link.textContent.trim()) {
                return link.textContent.trim();
            }
        }
        return fallback || (pageKey ? (pageKey.charAt(0).toUpperCase() + pageKey.slice(1)) : 'Chart');
    }

    function loadGlobalSettings(page, callback) {
        if (_globalSettings[page]) {
            if (callback) callback(_globalSettings[page]);
            return Promise.resolve(_globalSettings[page]);
        }
        return fetch('/api/v1/chart_settings?page=' + page)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                _globalSettings[page] = data || {};
                if (callback) callback(_globalSettings[page]);
                return _globalSettings[page];
            })
            .catch(function (err) {
                console.error('[NSChartCustomizer] Failed to load settings for ' + page + ':', err);
                _globalSettings[page] = {};
                if (callback) callback({});
                return {};
            });
    }

    function getResolvedStyle(page, metricId, baseStyle, viewOverrides) {
        var base = baseStyle || { color: '#3498db', lineType: 'solid', defaultWeight: 2, opacity: 1.0 };
        var globalPage = _globalSettings[page] || {};
        var globalMetric = globalPage[metricId] || {};
        var viewMetric = (viewOverrides && (viewOverrides[metricId] || (viewOverrides.styles && viewOverrides.styles[metricId]))) || {};
        var res = Object.assign({}, base, globalMetric, viewMetric);
        // Normalize legacy 'icicle' render type to step + invertAxis
        if (res.renderType === 'icicle') {
            res.renderType = 'step';
            if (res.invertAxis === undefined) res.invertAxis = true;
        }
        if (res.invertAxis === undefined) res.invertAxis = false;
        return res;
    }

    // Page-scoped render-type options matrix (4 Sep 2026, kitchen-sink union)
    var RENDER_TYPE_TABLE = {
        patterns: {
            median_bg: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            tir: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            titr: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            cv: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            hypo_auc: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            hyper_auc: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            lbgi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            hbgi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            basal_profile: [{ value: 'step', label: 'Step' }],
            cob: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            iob: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            dynamic_isf: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            median_bgi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            median_dev: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            controller_effort: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }]
        },
        trends: {
            gmi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }],
            tir: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            titr: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            avg_bg: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            cv: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            gvi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }],
            lbgi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            hbgi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            tdd: [{ value: 'bar', label: 'Bar' }, { value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }],
            tdd_weight: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar' }, { value: 'area', label: 'Area' }],
            carbs: [{ value: 'bar', label: 'Bar' }, { value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }]
        },
        trace: {
            bg: [{ value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }],
            carbs: [{ value: 'bar', label: 'Bar (Hist)' }, { value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }],
            cob: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            bolus: [{ value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            iob: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            scheduled_basal: [{ value: 'step', label: 'Step' }, { value: 'bar', label: 'Bar (Hist)' }],
            basal_rate: [{ value: 'step', label: 'Step' }, { value: 'bar', label: 'Bar (Hist)' }],
            deviation: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            bgi: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }, { value: 'area', label: 'Area' }],
            isf: [{ value: 'line', label: 'Line' }, { value: 'bar', label: 'Bar (Hist)' }]
        },
        hba1c: {
            hba1c_lab: [{ value: 'scatter', label: 'Scatter (Points)' }, { value: 'line', label: 'Line' }],
            gmi_90d: [{ value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }],
            ehba1c_90d: [{ value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }],
            custom_gmi: [{ value: 'line', label: 'Line' }, { value: 'area', label: 'Area' }]
        }
    };

    var INVERTIBLE_TABLE = {
        patterns: ['basal_profile', 'cob', 'iob'],
        trends: [],
        trace: ['cob', 'iob', 'scheduled_basal', 'basal_rate'],
        hba1c: []
    };

    function isMetricInvertible(page, metricId) {
        if (!page || !INVERTIBLE_TABLE[page]) return false;
        return INVERTIBLE_TABLE[page].indexOf(metricId) !== -1;
    }

    function getRenderTypeOptions(page, metricId) {
        if (arguments.length === 1) {
            metricId = page;
            page = null;
        }
        if (page && RENDER_TYPE_TABLE[page]) {
            return RENDER_TYPE_TABLE[page][metricId] || null;
        }
        for (var p in RENDER_TYPE_TABLE) {
            if (RENDER_TYPE_TABLE[p][metricId]) return RENDER_TYPE_TABLE[p][metricId];
        }
        return null;
    }

    function colorWithOpacity(color, opacity) {
        if (!color) return 'transparent';
        if (opacity === undefined || opacity === null) opacity = 1;
        var op = parseFloat(opacity);
        if (isNaN(op)) op = 1;
        if (op < 0) op = 0;
        if (op > 1) op = 1;

        var c = ('' + color).trim();
        if (c.charAt(0) === '#') {
            var hex = c.substring(1);
            if (hex.length === 3) {
                hex = hex.charAt(0) + hex.charAt(0) + hex.charAt(1) + hex.charAt(1) + hex.charAt(2) + hex.charAt(2);
            }
            if (hex.length === 6) {
                var r = parseInt(hex.substring(0, 2), 16);
                var g = parseInt(hex.substring(2, 4), 16);
                var b = parseInt(hex.substring(4, 6), 16);
                return 'rgba(' + r + ', ' + g + ', ' + b + ', ' + op + ')';
            }
        } else if (c.indexOf('rgb(') === 0) {
            return c.replace('rgb(', 'rgba(').replace(')', ', ' + op + ')');
        } else if (c.indexOf('rgba(') === 0) {
            return c.replace(/,\s*[\d\.]+\)$/, ', ' + op + ')');
        }
        return c;
    }

    // Small inline SVG preview matching a metric's actual rendered shape
    // (line/step/area/icicle stroke, or filled bar), at its real colour/opacity --
    // used in the Targets metric list and could be reused anywhere else a
    // compact "what does this look like" swatch is useful. When a Line
    // metric has a marker type set, a single representative marker is drawn
    // mid-line so the preview reflects the marker too, not just the stroke.
    function buildMetricPreviewSvg(style) {
        var type = style.renderType || 'line';
        var color = style.color || '#3498db';
        var fillColor = style.fillColor || color;
        var fillOpacity = style.fillOpacity !== undefined ? style.fillOpacity : 0;
        var lineOpacity = style.opacity !== undefined ? style.opacity : 1;
        var thickness = style.defaultWeight !== undefined ? style.defaultWeight : 2;
        var w = 40, h = 24;

        function markerSvg() {
            if (type !== 'line' || !style.markerType || style.markerType === 'none') return '';
            var mColor = style.markerColor || color;
            var mSize = thickness * 3; // fixed: marker size is always 3x line thickness
            var cx = 22, cy = 14; // mid-point of the sample polyline below
            var r = mSize / 2;
            if (style.markerType === 'circle') {
                return '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + escapeHtml(mColor) + '"/>';
            }
            if (style.markerType === 'square') {
                return '<rect x="' + (cx - r) + '" y="' + (cy - r) + '" width="' + mSize + '" height="' + mSize + '" fill="' + escapeHtml(mColor) + '"/>';
            }
            if (style.markerType === 'triangle') {
                return '<polygon points="' + cx + ',' + (cy - r) + ' ' + (cx + r) + ',' + (cy + r) + ' ' + (cx - r) + ',' + (cy + r) + '" fill="' + escapeHtml(mColor) + '"/>';
            }
            if (style.markerType === 'cross') {
                // Filled plus/cross polygon, matching the equivalent ECharts
                // custom symbol path used on the actual charts (MARKER_SYMBOL_MAP
                // in timeline.html/dashboard_daily.html/agp.html) -- not a
                // diagonal-X stroke, since ECharts symbol paths render via fill,
                // and an open two-segment path has no fillable area.
                var arm = mSize * 0.32;
                return '<path d="M ' + (cx - arm) + ' ' + (cy - r) + ' L ' + (cx + arm) + ' ' + (cy - r)
                    + ' L ' + (cx + arm) + ' ' + (cy - arm) + ' L ' + (cx + r) + ' ' + (cy - arm)
                    + ' L ' + (cx + r) + ' ' + (cy + arm) + ' L ' + (cx + arm) + ' ' + (cy + arm)
                    + ' L ' + (cx + arm) + ' ' + (cy + r) + ' L ' + (cx - arm) + ' ' + (cy + r)
                    + ' L ' + (cx - arm) + ' ' + (cy + arm) + ' L ' + (cx - r) + ' ' + (cy + arm)
                    + ' L ' + (cx - r) + ' ' + (cy - arm) + ' L ' + (cx - arm) + ' ' + (cy - arm) + ' Z" fill="' + escapeHtml(mColor) + '"/>';
            }
            return '';
        }

        if (type === 'bar') {
            var bh = h * 0.6;
            return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">'
                + '<rect x="2" y="' + (h - bh) + '" width="' + (w - 4) + '" height="' + bh + '" fill="' + escapeHtml(fillColor) + '" opacity="' + fillOpacity + '"/>'
                + '<rect x="2" y="' + (h - bh) + '" width="' + (w - 4) + '" height="' + bh + '" fill="none" stroke="' + escapeHtml(color) + '" stroke-width="1"/></svg>';
        }
        if (type === 'area') {
            var aDash = style.lineType === 'dashed' ? '4,3' : (style.lineType === 'dotted' ? '1.5,2.5' : 'none');
            return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">'
                + '<polygon points="2,22 2,18 12,8 22,14 32,4 38,10 38,22" fill="' + escapeHtml(fillColor) + '" opacity="' + fillOpacity + '"/>'
                + '<polyline points="2,18 12,8 22,14 32,4 38,10" fill="none" stroke="' + escapeHtml(color) + '" stroke-width="' + thickness + '" stroke-dasharray="' + aDash + '" stroke-linecap="round" stroke-linejoin="round" opacity="' + lineOpacity + '"/></svg>';
        }
        if (type === 'step' || type === 'icicle') {
            var path = 'M2,18 L2,10 L12,10 L12,16 L22,16 L22,6 L32,6 L32,12 L38,12';
            var area = (type === 'icicle' || fillOpacity > 0) ? '<path d="' + path + ' L38,22 L2,22 Z" fill="' + escapeHtml(fillColor) + '" opacity="' + fillOpacity + '"/>' : '';
            return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' + area
                + '<path d="' + path + '" fill="none" stroke="' + escapeHtml(color) + '" stroke-width="' + thickness + '"/></svg>';
        }
        var dash = style.lineType === 'dashed' ? '4,3' : (style.lineType === 'dotted' ? '1.5,2.5' : 'none');
        return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">'
            + '<polyline points="2,18 12,8 22,14 32,4 38,10" fill="none" stroke="' + escapeHtml(color) + '" stroke-width="' + thickness + '" stroke-dasharray="' + dash + '" stroke-linecap="round" stroke-linejoin="round" opacity="' + lineOpacity + '"/>' + markerSvg() + '</svg>';
    }

    // Shared metric style editor. Used two ways: embedded directly in a
    // page container (Targets' master-detail list) or inside a floating
    // popover (the on-graph pencil-icon trigger) -- see openStylePopover
    // below. One implementation, one set of fields, one place fill
    // colour/opacity and graph-type ever need to be added again.
    //
    // opts: {
    //   page, metricId, registryItem, viewOverrides,
    //   mode: 'view'   -- graph pages: Save for this view + Make default,
    //                      Restore default clears the VIEW override only
    //         'global' -- Targets: Make default only, Restore default
    //                      clears the GLOBAL override (registry baseline)
    //   onSaveView(metricId, style)
    //   onSaveGlobal(metricId, style, cascadeIds)
    //   onRestoreView(metricId)      -- mode 'view' only
    //   onAfterRestoreGlobal(metricId)
    //   onChange()                  -- called after any save/restore, for the
    //                                   caller to re-render its list/legend
    // }
    function renderStylePanel(container, opts) {
        if (!container) return;
        var page = opts.page;
        var metricId = opts.metricId;
        var registryItem = opts.registryItem;
        var viewOverrides = opts.viewOverrides;
        var mode = opts.mode || 'global';

        var resolved = getResolvedStyle(page, metricId, registryItem.style, viewOverrides);
        var baseStyle = registryItem.style || {};
        // renderType lives as a top-level registry field (registryItem.
        // renderType), separate from the style sub-object getResolvedStyle
        // merges -- without this fallback, resolved.renderType stays
        // undefined until an override exists, and buildMetricPreviewSvg
        // silently draws the generic line shape instead of the metric's
        // real default (e.g. Step for basal metrics), even though the
        // Graph type dropdown looked right (browsers just show the first
        // listed option when nothing actually matches 'selected').
        if (!resolved.renderType) resolved.renderType = registryItem.renderType || 'line';
        // fillColor/fillOpacity are new fields -- no registry declares them
        // yet, so give them the same de facto default the chart-rendering
        // code already hardcodes as its own fallback (0.75 opacity for bar
        // series, same colour as the stroke), rather than leaving fresh bar
        // metrics defaulting to an invisible 0% fill the first time someone
        // opens this panel.
        if (resolved.fillOpacity === undefined) {
            resolved.fillOpacity = resolved.renderType === 'bar' ? 0.75 : ((resolved.renderType === 'icicle' || resolved.renderType === 'step' || resolved.renderType === 'area') ? 0.22 : 0);
        }
        if (!resolved.fillColor) resolved.fillColor = resolved.color;
        // Marker fields -- Line-only, new 4 Sep 2026. markerColor defaults to
        // the stroke colour (not fillColor) since it's an independent field:
        // Fill colour still governs Bar/Area/Step fill, unaffected by marker
        // choice, so a metric can be flipped Line -> Bar -> Line without its
        // fill colour being clobbered by whatever the marker colour was set to.
        if (resolved.markerType === undefined) resolved.markerType = 'none';
        if (!resolved.markerColor) resolved.markerColor = resolved.color;
        // Curve smoothing -- Trends/Patterns only. smoothMonotone defaults to
        // true (clamps the curve to never overshoot past a neighbour's value
        // -- see the Trends axis-colour/overshoot investigation, 4 Sep 2026)
        // even though smooth itself defaults 'off', so turning Smooth on
        // starts from the safe/honest state rather than needing a second
        // deliberate step to avoid the overshoot bug reappearing.
        var smoothingApplies = SMOOTHING_PAGES.indexOf(page) !== -1;
        if (smoothingApplies) {
            if (resolved.smooth === undefined) resolved.smooth = 'off';
            if (resolved.smoothMonotone === undefined) resolved.smoothMonotone = true;
        }
        var hasViewOverride = Boolean(viewOverrides && (viewOverrides[metricId] || (viewOverrides.styles && viewOverrides.styles[metricId])));
        var renderTypeOptions = getRenderTypeOptions(page, metricId);
        var lastSaved = Object.assign({}, resolved);
        var draft = Object.assign({}, resolved);

        function isBarLike() {
            return draft.renderType === 'bar' || draft.renderType === 'icicle' || draft.renderType === 'step' || draft.renderType === 'area';
        }

        function paint() {
            var lineDisabled = (draft.renderType === 'bar' || draft.renderType === 'scatter');
            var fillDisabled = !isBarLike();
            var html = '';

            html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:16px;">'
                + '<div style="min-width:0;">'
                + '<div style="font-size:15px;font-weight:500;margin-bottom:2px;">' + escapeHtml(registryItem.label || metricId) + '</div>'
                + '<div style="font-size:12px;color:var(--text-secondary);">'
                + escapeHtml(getPageMenuLabel(page)) + (hasViewOverride ? ' &middot; <span class="badge badge-secondary" style="font-size:0.7em;">Override active</span>' : '')
                + '</div></div>'
                + '<div id="msp-preview-container" style="flex-shrink:0;">' + buildMetricPreviewSvg(draft) + '</div>'
                + '</div>';

            if (renderTypeOptions) {
                var showMarkerSel = draft.renderType === 'line';
                html += '<div style="display:grid;grid-template-columns:' + (showMarkerSel ? 'minmax(0,1fr) minmax(0,1fr)' : 'minmax(0,1fr)') + ';gap:12px;margin-bottom:14px;">'
                    + '<div><label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Graph type</label>'
                    + '<select class="form-select form-select-sm msp-type" style="width:100%;">'
                    + renderTypeOptions.map(function (o) {
                        return '<option value="' + o.value + '"' + (draft.renderType === o.value ? ' selected' : '') + '>' + escapeHtml(o.label) + '</option>';
                    }).join('')
                    + '</select></div>';
                if (showMarkerSel) {
                    html += '<div><label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Marker</label>'
                        + '<select class="form-select form-select-sm msp-marker-type" style="width:100%;">'
                        + MARKER_TYPES.map(function (o) {
                            return '<option value="' + o.value + '"' + (draft.markerType === o.value ? ' selected' : '') + '>' + escapeHtml(o.label) + '</option>';
                        }).join('')
                        + '</select></div>';
                }
                html += '</div>';
            }

            if (isMetricInvertible(page, metricId)) {
                html += '<div style="margin-bottom:14px;">'
                    + '<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-color);cursor:pointer;">'
                    + '<input type="checkbox" class="msp-invert"' + (draft.invertAxis ? ' checked' : '') + '>'
                    + '<span>Invert Y-axis</span>'
                    + '</label></div>';
            }

            if (!registryItem.lockColor) {
                var isLineType = draft.renderType === 'line';
                var secondColorLabel = isLineType ? 'Marker colour' : 'Fill colour';
                var secondColorDisabled = isLineType ? (!draft.markerType || draft.markerType === 'none') : fillDisabled;
                var secondColorValue = isLineType ? (draft.markerColor || draft.color || '#3498db') : (draft.fillColor || draft.color || '#3498db');
                html += '<div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:14px;">'
                    + '<div><label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Stroke colour</label>'
                    + '<div style="display:flex;gap:6px;align-items:center;min-width:0;">'
                    + '<input type="color" class="msp-color" value="' + escapeHtml(draft.color || '#3498db') + '" style="width:30px;height:30px;padding:1px;border-radius:4px;border:1px solid var(--border-color);cursor:pointer;flex-shrink:0;">'
                    + '<input type="text" class="form-input form-input-sm msp-color-text" value="' + escapeHtml(draft.color || '#3498db') + '" style="width:100%;min-width:0;box-sizing:border-box;font-family:monospace;font-size:11px;padding:4px 6px;" maxlength="7"></div></div>'
                    + '<div><label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">' + secondColorLabel + '</label>'
                    + '<div style="display:flex;gap:6px;align-items:center;min-width:0;">'
                    + '<input type="color" class="msp-second-color" value="' + escapeHtml(secondColorValue) + '" style="width:30px;height:30px;padding:1px;border-radius:4px;border:1px solid var(--border-color);cursor:pointer;flex-shrink:0;"' + (secondColorDisabled ? ' disabled' : '') + '>'
                    + '<input type="text" class="form-input form-input-sm msp-second-color-text" value="' + escapeHtml(secondColorValue) + '" style="width:100%;min-width:0;box-sizing:border-box;font-family:monospace;font-size:11px;padding:4px 6px;" maxlength="7"' + (secondColorDisabled ? ' disabled' : '') + '></div></div>'
                    + '</div>';
            } else {
                html += '<div style="font-size:12px;color:var(--text-secondary);margin-bottom:14px;">Colour is set per data point by category and is not editable.</div>';
            }

            html += '<div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:14px;">'
                + '<div><label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Line style</label>'
                + '<select class="form-select form-select-sm msp-line-style" style="width:100%;min-width:0;"' + (lineDisabled ? ' disabled' : '') + '>'
                + ['solid', 'dashed', 'dotted'].map(function (v) {
                    return '<option value="' + v + '"' + (draft.lineType === v ? ' selected' : '') + '>' + v.charAt(0).toUpperCase() + v.slice(1) + '</option>';
                }).join('')
                + '</select></div>'
                + '<div><label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Thickness</label>'
                + '<select class="form-select form-select-sm msp-weight" style="width:100%;min-width:0;">'
                + [1, 1.5, 2, 2.5, 3, 4, 5].map(function (v) {
                    return '<option value="' + v + '"' + (parseFloat(draft.defaultWeight) === v ? ' selected' : '') + '>' + v.toFixed(1) + 'px</option>';
                }).join('')
                + '</select></div></div>';

            html += '<div style="margin-bottom:14px;"><label id="msp-opacity-label" style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Line opacity &middot; ' + Math.round((draft.opacity !== undefined ? draft.opacity : 1) * 100) + '%</label>'
                + '<input type="range" class="msp-opacity" min="0.2" max="1" step="0.05" value="' + (draft.opacity !== undefined ? draft.opacity : 1) + '" style="width:100%;"></div>';

            html += '<div style="margin-bottom:16px;"><label id="msp-fill-opacity-label" style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:5px;">Fill opacity &middot; ' + Math.round((draft.fillOpacity !== undefined ? draft.fillOpacity : 0) * 100) + '%</label>'
                + '<input type="range" class="msp-fill-opacity" min="0" max="1" step="0.05" value="' + (draft.fillOpacity !== undefined ? draft.fillOpacity : 0) + '" style="width:100%;"' + (fillDisabled ? ' disabled' : '') + '></div>';

            if (smoothingApplies) {
                var smoothVal = draft.smooth === undefined ? 'off' : draft.smooth;
                var monotoneDisabled = (smoothVal === 'off');
                html += '<div style="border-top:0.5px solid var(--border-color);padding-top:12px;margin-bottom:14px;">'
                    + '<div style="font-size:11px;color:var(--text-secondary);margin-bottom:8px;">Curve smoothing (visual only -- does not change the data)</div>'
                    + '<div style="display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end;">'
                    + '<div><label style="font-size:11px;color:var(--text-secondary);display:block;margin-bottom:4px;">Smooth</label>'
                    + '<select class="form-select form-select-sm msp-smooth" style="width:100%;">'
                    + SMOOTH_LEVELS.map(function (v) {
                        var lbl = v === 'off' ? 'Off' : Number(v).toFixed(1);
                        return '<option value="' + v + '"' + (String(smoothVal) === String(v) ? ' selected' : '') + '>' + lbl + '</option>';
                    }).join('')
                    + '</select></div>'
                    + '<label style="display:flex;flex-direction:column;align-items:center;gap:5px;font-size:11px;color:var(--text-secondary);cursor:' + (monotoneDisabled ? 'default' : 'pointer') + ';padding-bottom:6px;opacity:' + (monotoneDisabled ? '0.5' : '1') + ';">'
                    + '<span>Monotone</span><input type="checkbox" class="msp-monotone"' + (draft.smoothMonotone ? ' checked' : '') + (monotoneDisabled ? ' disabled' : '') + '></label>'
                    + '</div></div>';
            }

            html += '<div id="msp-cascade-section" style="display:none;margin-bottom:14px;padding:10px;background:var(--surface-inset);border:1px solid var(--border-color);border-radius:4px;">'
                + '<div style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:6px;">Cascade to saved views:</div>'
                + '<div id="msp-cascade-list" style="max-height:120px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;"></div></div>';

            html += '<div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;">'
                + '<button type="button" class="btn btn-secondary btn-sm msp-btn-restore">Restore default</button>'
                + '<button type="button" class="btn btn-secondary btn-sm msp-btn-reset">Reset</button>'
                + (mode === 'view' ? '<button type="button" class="btn btn-secondary btn-sm msp-btn-save-view">Save for this view</button>' : '')
                + '<button type="button" class="btn btn-primary btn-sm msp-btn-save-global">Make default</button>'
                + '</div>';

            container.innerHTML = html;
            wire();
        }

        function collect() {
            return {
                color: draft.color,
                fillColor: draft.fillColor,
                markerType: draft.markerType,
                markerColor: draft.markerColor,
                lineType: draft.lineType,
                defaultWeight: draft.defaultWeight,
                opacity: draft.opacity,
                fillOpacity: draft.fillOpacity,
                renderType: draft.renderType,
                invertAxis: !!draft.invertAxis,
                smooth: draft.smooth,
                smoothMonotone: !!draft.smoothMonotone
            };
        }

        function emitPreview() {
            if (typeof opts.onPreview === 'function') {
                opts.onPreview(metricId, collect());
            }
        }

        function updatePreviewSvg() {
            var el = container.querySelector('#msp-preview-container');
            if (el) el.innerHTML = buildMetricPreviewSvg(draft);
        }

        function wire() {
            var typeSel = container.querySelector('.msp-type');
            if (typeSel) typeSel.addEventListener('change', function () {
                draft.renderType = this.value;
                paint();
                emitPreview();
            });

            var markerSel = container.querySelector('.msp-marker-type');
            if (markerSel) markerSel.addEventListener('change', function () {
                draft.markerType = this.value;
                paint();
                updatePreviewSvg();
                emitPreview();
            });

            var invertCb = container.querySelector('.msp-invert');
            if (invertCb) invertCb.addEventListener('change', function () {
                draft.invertAxis = this.checked;
                emitPreview();
            });

            var colorPicker = container.querySelector('.msp-color');
            var colorText = container.querySelector('.msp-color-text');
            if (colorPicker) colorPicker.addEventListener('input', function () {
                draft.color = this.value;
                if (colorText) colorText.value = this.value;
                updatePreviewSvg();
                emitPreview();
            });
            if (colorText) colorText.addEventListener('input', function () {
                if (/^#[0-9a-fA-F]{6}$/.test(this.value)) {
                    draft.color = this.value;
                    if (colorPicker) colorPicker.value = this.value;
                    updatePreviewSvg();
                    emitPreview();
                }
            });

            var fillPicker = container.querySelector('.msp-second-color');
            var fillText = container.querySelector('.msp-second-color-text');
            // Same input pair serves Fill colour (Bar/Area/Step) or Marker
            // colour (Line) -- whichever field is live is decided by
            // draft.renderType at the moment paint() last ran, so re-derive it
            // here rather than assuming; wire() always runs right after paint().
            var secondField = (draft.renderType === 'line') ? 'markerColor' : 'fillColor';
            if (fillPicker) fillPicker.addEventListener('input', function () {
                draft[secondField] = this.value;
                if (fillText) fillText.value = this.value;
                updatePreviewSvg();
                emitPreview();
            });
            if (fillText) fillText.addEventListener('input', function () {
                if (/^#[0-9a-fA-F]{6}$/.test(this.value)) {
                    draft[secondField] = this.value;
                    if (fillPicker) fillPicker.value = this.value;
                    updatePreviewSvg();
                    emitPreview();
                }
            });

            var lineStyleSel = container.querySelector('.msp-line-style');
            if (lineStyleSel) lineStyleSel.addEventListener('change', function () {
                draft.lineType = this.value;
                updatePreviewSvg();
                emitPreview();
            });

            var weightSel = container.querySelector('.msp-weight');
            if (weightSel) weightSel.addEventListener('change', function () {
                draft.defaultWeight = parseFloat(this.value);
                updatePreviewSvg();
                emitPreview();
            });

            var opacityRange = container.querySelector('.msp-opacity');
            if (opacityRange) opacityRange.addEventListener('input', function () {
                draft.opacity = parseFloat(this.value);
                var lbl = container.querySelector('#msp-opacity-label');
                if (lbl) lbl.textContent = 'Line opacity · ' + Math.round(draft.opacity * 100) + '%';
                updatePreviewSvg();
                emitPreview();
            });

            var fillOpacityRange = container.querySelector('.msp-fill-opacity');
            if (fillOpacityRange) fillOpacityRange.addEventListener('input', function () {
                draft.fillOpacity = parseFloat(this.value);
                var lbl = container.querySelector('#msp-fill-opacity-label');
                if (lbl) lbl.textContent = 'Fill opacity · ' + Math.round(draft.fillOpacity * 100) + '%';
                updatePreviewSvg();
                emitPreview();
            });

            var smoothSel = container.querySelector('.msp-smooth');
            if (smoothSel) smoothSel.addEventListener('change', function () {
                draft.smooth = this.value === 'off' ? 'off' : parseFloat(this.value);
                paint();
                emitPreview();
            });

            var monotoneCb = container.querySelector('.msp-monotone');
            if (monotoneCb) monotoneCb.addEventListener('change', function () {
                draft.smoothMonotone = this.checked;
                emitPreview();
            });

            var restoreBtn = container.querySelector('.msp-btn-restore');
            if (restoreBtn) restoreBtn.addEventListener('click', onRestore);

            var resetBtn = container.querySelector('.msp-btn-reset');
            if (resetBtn) resetBtn.addEventListener('click', function () {
                draft = Object.assign({}, lastSaved);
                paint();
                emitPreview();
            });

            var saveViewBtn = container.querySelector('.msp-btn-save-view');
            if (saveViewBtn) saveViewBtn.addEventListener('click', function () {
                var st = collect();
                lastSaved = Object.assign({}, st);
                if (opts.onSaveView) {
                    opts.onSaveView(metricId, st);
                }
                if (opts.onChange) opts.onChange();
            });

            var saveGlobalBtn = container.querySelector('.msp-btn-save-global');
            if (saveGlobalBtn) saveGlobalBtn.addEventListener('click', function () { onSaveGlobal(saveGlobalBtn); });

            loadCascadeSection();
        }

        function loadCascadeSection() {
            var section = container.querySelector('#msp-cascade-section');
            var list = container.querySelector('#msp-cascade-list');
            if (!section || !list) return;
            fetch('/api/v1/chart_settings/affected_views?page=' + page + '&metric_id=' + metricId)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    var views = (data && data.views) || [];
                    if (!views.length) { section.style.display = 'none'; return; }
                    section.style.display = 'block';
                    list.innerHTML = views.map(function (v) {
                        return '<label style="display:flex;align-items:center;gap:8px;font-size:0.85rem;color:var(--text-color);cursor:pointer;">'
                            + '<input type="checkbox" class="msp-cascade-cb" value="' + v.id + '"' + (!v.has_override ? ' checked' : '') + '>'
                            + '<span>' + escapeHtml(v.name) + '</span>'
                            + (v.has_override ? ' <span class="badge badge-secondary" style="font-size:0.7em;">has override</span>' : '')
                            + '</label>';
                    }).join('');
                })
                .catch(function () { section.style.display = 'none'; });
        }

        function onSaveGlobal(btn) {
            var st = collect();
            btn.disabled = true;
            btn.textContent = 'Saving...';
            var cascadeIds = [];
            var checked = container.querySelectorAll('.msp-cascade-cb:checked');
            for (var i = 0; i < checked.length; i++) cascadeIds.push(parseInt(checked[i].value, 10));

            fetch('/api/v1/chart_settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ page: page, metric_id: metricId, settings: st, cascade_view_ids: cascadeIds })
            })
            .then(function (r) { return r.json(); })
            .then(function () {
                if (!_globalSettings[page]) _globalSettings[page] = {};
                _globalSettings[page][metricId] = st;
                lastSaved = Object.assign({}, st);
                if (opts.onSaveGlobal) opts.onSaveGlobal(metricId, st, cascadeIds);
                if (opts.onChange) opts.onChange();
                btn.disabled = false;
                btn.textContent = 'Make default';
            })
            .catch(function (err) {
                btn.disabled = false;
                btn.textContent = 'Make default';
                alert('Failed to save: ' + err.message);
            });
        }

        function onRestore() {
            if (mode === 'view') {
                if (opts.onRestoreView) opts.onRestoreView(metricId);
                draft = Object.assign({}, getResolvedStyle(page, metricId, baseStyle, null));
                lastSaved = Object.assign({}, draft);
                paint();
                emitPreview();
                if (opts.onChange) opts.onChange();
                return;
            }
            fetch('/api/v1/chart_settings?page=' + page + '&metric_id=' + metricId, { method: 'DELETE' })
                .then(function (r) { return r.json(); })
                .then(function () {
                    if (_globalSettings[page]) delete _globalSettings[page][metricId];
                    draft = Object.assign({}, baseStyle);
                    lastSaved = Object.assign({}, draft);
                    paint();
                    emitPreview();
                    if (opts.onAfterRestoreGlobal) opts.onAfterRestoreGlobal(metricId);
                    if (opts.onChange) opts.onChange();
                })
                .catch(function (err) { alert('Failed to restore default: ' + err.message); });
        }

        paint();
    }

    // Floating popover wrapper around renderStylePanel, anchored as a
    // submenu to the left of the parent settings menu. Only one style
    // popover instance open at a time. Returns back to the open settings
    // menu when saved or when another metric is clicked. Closes both
    // if clicked outside of both.
    var _activeStylePopover = null;
    var _activePopoverRevertFn = null;
    var _activeParentSettingsEl = null;

    function closeStylePopover(committed) {
        if (_activeStylePopover) {
            if (!committed && typeof _activePopoverRevertFn === 'function') {
                _activePopoverRevertFn();
            }
            _activePopoverRevertFn = null;
            _activeParentSettingsEl = null;
            _activeStylePopover.remove();
            _activeStylePopover = null;
            document.removeEventListener('click', _stylePopoverOutsideClick, true);
            document.removeEventListener('keydown', _stylePopoverKeyDown, true);
        }
    }

    function _stylePopoverKeyDown(e) {
        if (e.key === 'Escape') {
            e.stopPropagation();
            closeStylePopover(false);
        }
    }

    function _stylePopoverOutsideClick(e) {
        if (!_activeStylePopover) return;
        if (_activeStylePopover.contains(e.target)) return;

        // If clicked inside the parent settings menu:
        if (_activeParentSettingsEl && _activeParentSettingsEl.contains(e.target)) {
            // Close the style popover so focus returns to the open settings menu.
            // If the click was on another pencil icon, its click listener will trigger
            // openStylePopover for the new metric.
            closeStylePopover(false);
            return;
        }

        // Click is outside BOTH the style popover AND the parent settings menu:
        var parent = _activeParentSettingsEl;
        closeStylePopover(false);
        if (parent) {
            var isSettingsBtn = e.target.closest && e.target.closest('#agp-settings-btn, #trends-settings-btn, #trace-settings-btn, .settings-btn');
            if (!isSettingsBtn) {
                parent.classList.remove('active');
            }
        }
    }

    function openStylePopover(anchorEl, opts) {
        closeStylePopover(false);
        var pop = document.createElement('div');
        pop.className = 'metric-style-popover';
        pop.style.cssText = 'position:absolute;width:310px;box-sizing:border-box;background:var(--card-bg);border:0.5px solid var(--border-color-strong);border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,0.18);padding:16px 18px;z-index:2100;max-height:85vh;overflow-y:auto;overflow-x:hidden;';
        document.body.appendChild(pop);
        _activeStylePopover = pop;

        var parentSettings = (anchorEl && anchorEl.closest) ? anchorEl.closest('.settings-popover') : null;
        _activeParentSettingsEl = parentSettings;

        // Align to the settings popover's own position (top-left), not the
        // individual pencil icon's row -- so the style popover always opens
        // in the same place regardless of which row in a scrolled checklist
        // was clicked, consistent with where the settings menu itself sits.
        var alignEl = parentSettings || anchorEl;
        var rect = alignEl.getBoundingClientRect();
        var top = window.scrollY + rect.top;
        var left = window.scrollX + rect.left - 310 - 12;
        // Not enough room to the left (narrow window) -- fall back to the
        // right of the trigger rather than running off-screen.
        if (left < 8) left = window.scrollX + rect.right + 12;
        pop.style.top = top + 'px';
        pop.style.left = left + 'px';

        var initialResolved = getResolvedStyle(opts.page, opts.metricId, (opts.registryItem && opts.registryItem.style) || {}, opts.viewOverrides);
        var originalSaved = Object.assign({}, initialResolved);
        _activePopoverRevertFn = function () {
            if (typeof opts.onPreview === 'function') {
                opts.onPreview(opts.metricId, null);
            }
        };

        renderStylePanel(pop, Object.assign({}, opts, {
            onSaveView: function (mid, st) {
                originalSaved = Object.assign({}, st);
                if (opts.onSaveView) opts.onSaveView(mid, st);
                closeStylePopover(true);
            },
            onSaveGlobal: function (mid, st, cascadeIds) {
                originalSaved = Object.assign({}, st);
                if (opts.onSaveGlobal) opts.onSaveGlobal(mid, st, cascadeIds);
                closeStylePopover(true);
            },
            onRestoreView: function (mid) {
                var restored = getResolvedStyle(opts.page, opts.metricId, (opts.registryItem && opts.registryItem.style) || {}, null);
                originalSaved = Object.assign({}, restored);
                if (opts.onRestoreView) opts.onRestoreView(mid);
            },
            onChange: function () {
                if (opts.onChange) opts.onChange();
            }
        }));

        setTimeout(function () {
            document.addEventListener('click', _stylePopoverOutsideClick, true);
            document.addEventListener('keydown', _stylePopoverKeyDown, true);
        }, 0);
    }

    function getLegendIcon(renderTypeOrStyle, lineType, weight, extraOptions) {
        var st = {};
        if (typeof renderTypeOrStyle === 'object' && renderTypeOrStyle !== null) {
            st = renderTypeOrStyle;
        } else {
            st = Object.assign({
                renderType: renderTypeOrStyle,
                lineType: lineType,
                defaultWeight: weight
            }, extraOptions || {});
        }

        var renderType = st.renderType || 'line';
        var lt = st.lineType || 'solid';
        var w = parseFloat(st.defaultWeight);
        if (isNaN(w) || w <= 0) w = 2.0;
        if (w > 6) w = 6;

        var color = st.color;
        var hasColor = !!color;
        if (!hasColor) color = '#3498db';

        var lineOpacity = (st.opacity !== undefined && st.opacity !== null) ? parseFloat(st.opacity) : 1.0;
        var fillColor = st.fillColor || color;
        var fillOpacity = (st.fillOpacity !== undefined && st.fillOpacity !== null) ? parseFloat(st.fillOpacity) : (renderType === 'bar' ? 0.75 : (renderType === 'area' ? 0.25 : 0));
        var markerType = (renderType === 'line' && st.markerType) ? st.markerType : 'none';
        var markerColor = st.markerColor || color;

        // When called with a style object or extra options, generate a rich SVG Data URI:
        if (typeof renderTypeOrStyle === 'object' || extraOptions || markerType !== 'none') {
            var svgW = 24, svgH = 12;
            var svgBody = '';

            if (renderType === 'bar') {
                svgBody = '<rect x="1" y="2" width="22" height="8" rx="2" ry="2" fill="' + escapeHtml(fillColor) + '" fill-opacity="' + fillOpacity + '" stroke="' + escapeHtml(color) + '" stroke-width="' + Math.min(w, 2) + '" stroke-opacity="' + lineOpacity + '"/>';
            } else if (renderType === 'area') {
                var aDash = lt === 'dashed' ? 'stroke-dasharray="3,2"' : (lt === 'dotted' ? 'stroke-dasharray="1.5,2"' : '');
                svgBody = '<polygon points="1,11 1,6 12,2 23,6 23,11" fill="' + escapeHtml(fillColor) + '" fill-opacity="' + fillOpacity + '"/>'
                        + '<polyline points="1,6 12,2 23,6" fill="none" stroke="' + escapeHtml(color) + '" stroke-width="' + Math.min(w, 2.5) + '" stroke-opacity="' + lineOpacity + '" ' + aDash + ' stroke-linecap="round" stroke-linejoin="round"/>';
            } else if (renderType === 'step' || renderType === 'icicle') {
                var stepArea = (renderType === 'icicle' || fillOpacity > 0)
                    ? '<polygon points="1,11 1,8 10,8 10,3 23,3 23,11" fill="' + escapeHtml(fillColor) + '" fill-opacity="' + fillOpacity + '"/>'
                    : '';
                svgBody = stepArea + '<path d="M 1 8 L 10 8 L 10 3 L 23 3" fill="none" stroke="' + escapeHtml(color) + '" stroke-width="' + Math.min(w, 2.5) + '" stroke-opacity="' + lineOpacity + '"/>';
            } else {
                // Line
                var dashAttr = lt === 'dashed' ? 'stroke-dasharray="4,2"' : (lt === 'dotted' ? 'stroke-dasharray="1.5,2"' : '');
                svgBody = '<line x1="1" y1="6" x2="23" y2="6" stroke="' + escapeHtml(color) + '" stroke-width="' + w + '" stroke-opacity="' + lineOpacity + '" ' + dashAttr + ' stroke-linecap="round"/>';
                if (markerType && markerType !== 'none') {
                    var mSize = Math.max(5, Math.min(w * 3, 9));
                    var cx = 12, cy = 6;
                    var r = mSize / 2;
                    if (markerType === 'circle') {
                        svgBody += '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + escapeHtml(markerColor) + '" stroke="' + escapeHtml(color) + '" stroke-width="0.75"/>';
                    } else if (markerType === 'square' || markerType === 'rect') {
                        svgBody += '<rect x="' + (cx - r) + '" y="' + (cy - r) + '" width="' + mSize + '" height="' + mSize + '" fill="' + escapeHtml(markerColor) + '" stroke="' + escapeHtml(color) + '" stroke-width="0.75"/>';
                    } else if (markerType === 'triangle') {
                        svgBody += '<polygon points="' + cx + ',' + (cy - r) + ' ' + (cx + r) + ',' + (cy + r) + ' ' + (cx - r) + ',' + (cy + r) + '" fill="' + escapeHtml(markerColor) + '" stroke="' + escapeHtml(color) + '" stroke-width="0.75"/>';
                    } else if (markerType === 'diamond') {
                        svgBody += '<polygon points="' + cx + ',' + (cy - r) + ' ' + (cx + r) + ',' + cy + ' ' + cx + ',' + (cy + r) + ' ' + (cx - r) + ',' + cy + '" fill="' + escapeHtml(markerColor) + '" stroke="' + escapeHtml(color) + '" stroke-width="0.75"/>';
                    } else if (markerType === 'cross') {
                        var arm = mSize * 0.32;
                        svgBody += '<path d="M ' + (cx - arm) + ' ' + (cy - r) + ' L ' + (cx + arm) + ' ' + (cy - r)
                            + ' L ' + (cx + arm) + ' ' + (cy - arm) + ' L ' + (cx + r) + ' ' + (cy - arm)
                            + ' L ' + (cx + r) + ' ' + (cy + arm) + ' L ' + (cx + arm) + ' ' + (cy + arm)
                            + ' L ' + (cx + arm) + ' ' + (cy + r) + ' L ' + (cx - arm) + ' ' + (cy + r)
                            + ' L ' + (cx - arm) + ' ' + (cy + arm) + ' L ' + (cx - r) + ' ' + (cy + arm)
                            + ' L ' + (cx - r) + ' ' + (cy - arm) + ' L ' + (cx - arm) + ' ' + (cy - arm) + ' Z" fill="' + escapeHtml(markerColor) + '" stroke="' + escapeHtml(color) + '" stroke-width="0.5"/>';
                    }
                }
            }

            var fullSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + svgW + '" height="' + svgH + '" viewBox="0 0 ' + svgW + ' ' + svgH + '">' + svgBody + '</svg>';
            return 'image://data:image/svg+xml;utf8,' + encodeURIComponent(fullSvg);
        }

        // Fallback for string-only legacy calls:
        if (renderType === 'bar') return 'roundRect';
        if (renderType === 'scatter' || renderType === 'circle') return 'circle';
        if (renderType === 'area') {
            return 'path:' + '/' + '/' + 'M 0 9 L 0 5 L 10 1 L 20 5 L 20 9 Z';
        }
        if (renderType === 'step') {
            return 'path:' + '/' + '/' + 'M 0 6.5 L 9 6.5 L 9 2.5 L 20 2.5 L 20 4.5 L 11 4.5 L 11 8.5 L 0 8.5 Z';
        }
        if (renderType === 'icicle') {
            return 'path:' + '/' + '/' + 'M 0 2 L 20 2 L 20 4.5 L 12 4.5 L 12 8.5 L 8 8.5 L 8 4.5 L 0 4.5 Z';
        }

        var half = w / 2;
        var y1 = (5 - half).toFixed(2);
        var y2 = (5 + half).toFixed(2);

        if (lt === 'dashed') {
            return 'path:' + '/' + '/' + 'M 0 ' + y1 + ' L 5 ' + y1 + ' L 5 ' + y2 + ' L 0 ' + y2 + ' Z '
                 + 'M 7.5 ' + y1 + ' L 12.5 ' + y1 + ' L 12.5 ' + y2 + ' L 7.5 ' + y2 + ' Z '
                 + 'M 15 ' + y1 + ' L 20 ' + y1 + ' L 20 ' + y2 + ' L 15 ' + y2 + ' Z';
        }
        if (lt === 'dotted') {
            return 'path:' + '/' + '/' + 'M 1 ' + y1 + ' L 3.5 ' + y1 + ' L 3.5 ' + y2 + ' L 1 ' + y2 + ' Z '
                 + 'M 5.5 ' + y1 + ' L 8 ' + y1 + ' L 8 ' + y2 + ' L 5.5 ' + y2 + ' Z '
                 + 'M 10 ' + y1 + ' L 12.5 ' + y1 + ' L 12.5 ' + y2 + ' L 10 ' + y2 + ' Z '
                 + 'M 14.5 ' + y1 + ' L 17 ' + y1 + ' L 17 ' + y2 + ' L 14.5 ' + y2 + ' Z '
                 + 'M 19 ' + y1 + ' L 21.5 ' + y1 + ' L 21.5 ' + y2 + ' L 19 ' + y2 + ' Z';
        }
        return 'path:' + '/' + '/' + 'M 0 ' + y1 + ' L 20 ' + y1 + ' L 20 ' + y2 + ' L 0 ' + y2 + ' Z';
    }

    function renderPopoverChecklist(containerId, options) {
        var container = document.getElementById(containerId);
        if (!container) return;

        var registry = options.registry || [];
        var activeIds = options.activeIds || [];
        var searchTerm = (options.searchTerm || '').trim().toLowerCase();

        var filtered = registry.filter(function (m) {
            if (!searchTerm) return true;
            return (m.label || '').toLowerCase().indexOf(searchTerm) !== -1 || (m.id || '').toLowerCase().indexOf(searchTerm) !== -1;
        });

        if (!filtered.length) {
            container.innerHTML = '<div class="overlay-list-empty">No series match &ldquo;' + escapeHtml(searchTerm) + '&rdquo;</div>';
            return;
        }

        var activeView = (global.NSViews && typeof global.NSViews.getActiveView === 'function')
            ? global.NSViews.getActiveView()
            : (global.NSViews && typeof global.NSViews.findView === 'function' ? global.NSViews.findView(global.NSViews.getActiveViewId()) : null);
        var viewOverrides = (activeView && (activeView.styles || (activeView.payload && activeView.payload.styles))) || {};

        var html = '';
        filtered.forEach(function (m) {
            var isChecked = activeIds.indexOf(m.id) !== -1;
            var resStyle = options.page ? getResolvedStyle(options.page, m.id, m.style, viewOverrides) : (m.style || {});
            var swatchColor = resStyle.color || (m.style && m.style.color) || '#3498db';
            // Registry labels sometimes already bake the unit in (e.g. 'GMI (%)')
            // -- skip the badge in that case rather than showing '(%) (%)'.
            // Only suppresses an EXACT trailing match; a label like
            // 'TIR (3.9-10)' with unit '%' still gets the '(%)' badge since
            // the parenthetical there is a range, not the unit itself.
            var isBuiltin = BUILTIN_METRIC_IDS.indexOf(m.id) !== -1;
            var typeBadge;
            if (isBuiltin) {
                typeBadge = '<span class="overlay-list-type">Built-in</span>';
            } else {
                // Registry labels sometimes already bake the unit in (e.g. 'GMI (%)')
                // -- skip the badge in that case rather than showing '(%) (%)'.
                // Only suppresses an EXACT trailing match; a label like
                // 'TIR (3.9-10)' with unit '%' still gets the '(%)' badge since
                // the parenthetical there is a range, not the unit itself.
                var unitOrType = m.unit || m.renderType || 'line';
                var escapedUnit = unitOrType.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                var labelAlreadyHasUnit = new RegExp('\\(' + escapedUnit + '\\)\\s*$', 'i').test(m.label || '');
                typeBadge = labelAlreadyHasUnit ? '' : '<span class="overlay-list-type">(' + escapeHtml(unitOrType) + ')</span>';
            }
            // Built-in/composite metrics don't get a per-metric style editor
            // -- there's no single color/style that meaningfully represents
            // them (see BUILTIN_METRIC_IDS comment above).
            var editIcon = isBuiltin
                ? ''
                : '<i class="fa-solid fa-pen overlay-list-edit-icon" data-metric-id="' + escapeHtml(m.id) + '" title="Edit style"></i>';
            html += '<div class="overlay-list-row">'
                + '<label class="overlay-list-row-main">'
                + '  <input type="checkbox" class="overlay-list-checkbox" data-metric-id="' + escapeHtml(m.id) + '"' + (isChecked ? ' checked' : '') + '>'
                + '  <span class="overlay-list-swatch" style="background-color: ' + escapeHtml(swatchColor) + ';"></span>'
                + '  <span class="overlay-list-label">' + escapeHtml(m.label) + '</span>'
                + '  ' + typeBadge
                + '</label>'
                + '<div class="overlay-list-actions-icons">'
                + '  ' + editIcon
                + '</div>'
                + '</div>';
        });
        container.innerHTML = html;

        var checkboxes = container.querySelectorAll('.overlay-list-checkbox');
        checkboxes.forEach(function (cb) {
            cb.addEventListener('change', function () {
                var mid = this.getAttribute('data-metric-id');
                if (options.onToggle) options.onToggle(mid, this.checked);
            });
        });

        var editIcons = container.querySelectorAll('.overlay-list-edit-icon');
        editIcons.forEach(function (icon) {
            icon.addEventListener('click', function (e) {
                e.stopPropagation();
                var mid = this.getAttribute('data-metric-id');
                if (options.onEdit) options.onEdit(mid, this);
            });
        });
    }

    function getActiveLegend(registry, activeIds, styleResolver) {
        var activeRegistry = registry.filter(function (m) {
            return activeIds.indexOf(m.id) !== -1;
        });

        return activeRegistry.map(function (m) {
            var st = styleResolver ? styleResolver(m.id, m.style) : (m.style || {});
            var renderType = st.renderType || m.renderType || 'line';
            var icon = getLegendIcon(renderType, st.lineType, st.defaultWeight);
            return {
                name: m.label,
                icon: icon,
                itemStyle: { color: st.color || '#3498db' }
            };
        });
    }

    global.NSChartCustomizer = {
        loadGlobalSettings: loadGlobalSettings,
        getResolvedStyle: getResolvedStyle,
        getPageMenuLabel: getPageMenuLabel,
        renderStylePanel: renderStylePanel,
        openStylePopover: openStylePopover,
        closeStylePopover: closeStylePopover,
        buildMetricPreviewSvg: buildMetricPreviewSvg,
        getRenderTypeOptions: getRenderTypeOptions,
        renderPopoverChecklist: renderPopoverChecklist,
        getActiveLegend: getActiveLegend,
        colorWithOpacity: colorWithOpacity,
        getLegendIcon: getLegendIcon,
        isMetricInvertible: isMetricInvertible,
        RENDER_TYPE_TABLE: RENDER_TYPE_TABLE,
        INVERTIBLE_TABLE: INVERTIBLE_TABLE,
        BUILTIN_METRIC_IDS: BUILTIN_METRIC_IDS
    };

})(typeof window !== 'undefined' ? window : this);
