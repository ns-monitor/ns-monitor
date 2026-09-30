/**
 * daemon/static/js/graph-it-all-builder.js
 * Reactive state manager, axis role mapping, query compiler, CSV exporter,
 * and NSViews/NSAxisPillars integration.
 */
(function(global) {
    'use strict';

    const DEFAULT_GRAPH_VIEWS = [
        {
            id: 'view_carbs_vs_tdd',
            name: 'Carbs vs Total Daily Dose',
            isDefault: true,
            grain: 'daily',
            chartType: 'scatter',
            x: { metricId: 'total_carbs', aggregation: 'sum' },
            y: { metricId: 'tdd', aggregation: 'sum' },
            y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
            trend: { model: 'linear', band: 'ci95' },
            payload: {
                specVersion: 1,
                grain: 'daily',
                chartType: 'scatter',
                x: { metricId: 'total_carbs', aggregation: 'sum' },
                y: { metricId: 'tdd', aggregation: 'sum' },
                y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
                trend: { model: 'linear', band: 'ci95' }
            }
        },
        {
            id: 'view_glucose_vs_cv',
            name: 'Mean Glucose vs CV%',
            isDefault: true,
            grain: 'daily',
            chartType: 'scatter',
            x: { metricId: 'mean_glucose', aggregation: 'avg' },
            y: { metricId: 'cv_glucose', aggregation: 'avg' },
            y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
            trend: { model: 'linear', band: 'ci95' },
            payload: {
                specVersion: 1,
                grain: 'daily',
                chartType: 'scatter',
                x: { metricId: 'mean_glucose', aggregation: 'avg' },
                y: { metricId: 'cv_glucose', aggregation: 'avg' },
                y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
                trend: { model: 'linear', band: 'ci95' }
            }
        },
        {
            id: 'view_tir_over_time',
            name: 'TIR % Over Time',
            isDefault: true,
            grain: 'daily',
            chartType: 'line',
            x: { metricId: 'date', aggregation: 'none' },
            y: { metricId: 'tir_pct', aggregation: 'avg' },
            y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
            trend: { model: 'linear', band: 'ci95' },
            payload: {
                specVersion: 1,
                grain: 'daily',
                chartType: 'line',
                x: { metricId: 'date', aggregation: 'none' },
                y: { metricId: 'tir_pct', aggregation: 'avg' },
                y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
                trend: { model: 'linear', band: 'ci95' }
            }
        }
    ];

    function getInitialDefaultState() {
        try {
            const activeId = localStorage.getItem('ns_graph_it_all_active_view');
            const matched = activeId ? DEFAULT_GRAPH_VIEWS.find(v => String(v.id) === String(activeId)) : null;
            const initial = matched || DEFAULT_GRAPH_VIEWS[0];
            const p = initial.payload || initial;
            return {
                grain: p.grain || 'daily',
                chartType: p.chartType || 'scatter',
                cycleMode: p.cycleMode || 'none',
                cyclePeriod: p.cyclePeriod || 3.0,
                cycleAnchor: p.cycleAnchor || '',
                dowFilter: (Array.isArray(p.dowFilter) && p.dowFilter.length > 0) ? p.dowFilter : [1, 2, 3, 4, 5, 6, 7],
                x: Object.assign({}, p.x),
                y: Object.assign({}, p.y),
                y2: Object.assign({ enabled: false, metricId: 'tdd', aggregation: 'sum' }, p.y2 || {}),
                trend: Object.assign({ model: 'linear', band: 'ci95' }, p.trend || {}),
                isLoading: false
            };
        } catch (e) {
            return {
                grain: 'daily',
                chartType: 'scatter',
                cycleMode: 'none',
                cyclePeriod: 3.0,
                cycleAnchor: '',
                dowFilter: [1, 2, 3, 4, 5, 6, 7],
                x: { metricId: 'total_carbs', aggregation: 'sum' },
                y: { metricId: 'tdd', aggregation: 'sum' },
                y2: { enabled: false, metricId: 'tdd', aggregation: 'sum' },
                trend: { model: 'linear', band: 'ci95' },
                isLoading: false
            };
        }
    }

    const state = getInitialDefaultState();
    let activeViewSpec = null;

    function getState() {
        return JSON.parse(JSON.stringify(state));
    }

    function captureGraphDefinition() {
        return {
            specVersion: 1,
            grain: state.grain,
            chartType: state.chartType,
            cycleMode: state.cycleMode || 'none',
            cyclePeriod: state.cyclePeriod || 3.0,
            cycleAnchor: state.cycleAnchor || '',
            dowFilter: state.dowFilter || [1, 2, 3, 4, 5, 6, 7],
            x: state.x,
            y: state.y,
            y2: state.y2,
            trend: state.trend,
            axisBounds: (window.NSAxisPillars ? NSAxisPillars.getAllBounds() : {})
        };
    }

    function applyGraphDefinition(view, options) {
        if (!view) return;
        const p = view.payload || view;
        if (!p.x || !p.y) return;

        showAlert('', false);

        state.grain = p.grain || 'daily';
        state.chartType = p.chartType || 'scatter';
        state.cycleMode = p.cycleMode || 'none';
        state.cyclePeriod = p.cyclePeriod || 3.0;
        state.cycleAnchor = p.cycleAnchor || '';
        state.dowFilter = (Array.isArray(p.dowFilter) && p.dowFilter.length > 0) ? p.dowFilter : [1, 2, 3, 4, 5, 6, 7];

        // Validate metrics in catalogue (C1: degraded fallback)
        let degraded = false;
        let warningMsg = '';

        if (p.x && p.x.metricId) {
            const mX = (p.x.metricId === 'date') ? { id: 'date' } : GIACatalog.getMetric(p.x.metricId);
            if (mX) {
                state.x = { metricId: p.x.metricId, aggregation: p.x.aggregation || 'avg' };
            } else {
                degraded = true;
                warningMsg = `Metric '${p.x.metricId}' is no longer available in catalogue.`;
                state.x = { metricId: 'total_carbs', aggregation: 'sum' };
            }
        }

        if (p.y && p.y.metricId) {
            const mY = GIACatalog.getMetric(p.y.metricId);
            if (mY) {
                state.y = { metricId: p.y.metricId, aggregation: p.y.aggregation || 'avg' };
            } else {
                degraded = true;
                warningMsg = `Metric '${p.y.metricId}' is no longer available in catalogue.`;
                state.y = { metricId: 'tdd', aggregation: 'sum' };
            }
        }

        if (p.y2) {
            const mY2 = GIACatalog.getMetric(p.y2.metricId);
            state.y2 = {
                enabled: !!p.y2.enabled,
                metricId: mY2 ? p.y2.metricId : 'tdd',
                aggregation: p.y2.aggregation || 'avg'
            };
        } else {
            state.y2 = { enabled: false, metricId: 'tdd', aggregation: 'avg' };
        }

        if (p.axisBounds && window.NSAxisPillars) {
            NSAxisPillars.setAllBounds(p.axisBounds);
        } else if (window.NSAxisPillars) {
            NSAxisPillars.setAllBounds({});
        }

        if (p.trend) {
            let bandVal = p.trend.band;
            if (!bandVal) {
                if (p.trend.showConfidenceBand === false && !p.trend.showPredictionBand) {
                    bandVal = 'none';
                } else if (p.trend.showPredictionBand && p.trend.showConfidenceBand !== false) {
                    bandVal = 'both_95';
                } else if (p.trend.showPredictionBand) {
                    bandVal = 'pi95';
                } else {
                    bandVal = 'ci95';
                }
            }
            state.trend = {
                model: p.trend.model || 'none',
                band: bandVal
            };
        }

        syncControlsFromState();

        if (degraded) {
            showAlert(warningMsg, true, 'warning');
        }

        // Record serialized spec matching the loaded view
        activeViewSpec = JSON.stringify(captureGraphDefinition());

        // Only trigger query if not during initial boot or explicitly skipped
        if (!options || (!options.isInitialBoot && !options.skipQuery)) {
            runQuery();
        }
    }

    function syncControlsFromState() {
        // Observation grain buttons
        document.querySelectorAll('.gia-grain-btn').forEach(btn => {
            if (btn.dataset.grain === state.grain) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });

        // Chart format buttons
        document.querySelectorAll('.gia-format-btn').forEach(btn => {
            if (btn.dataset.format === state.chartType) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });

        const isCycle = (state.cycleMode && state.cycleMode !== 'none');
        let cycleDimName = '';
        let cycleRangeLabel = '';
        if (isCycle) {
            if (state.cycleMode === 'diurnal_24h') {
                cycleDimName = 'Hour of Day';
                cycleRangeLabel = '[00:00–23:00]';
            } else if (state.cycleMode === 'weekly_7d') {
                cycleDimName = 'Day of Week';
                cycleRangeLabel = '[Mon–Sun]';
            } else if (state.cycleMode === 'annual_12m') {
                cycleDimName = 'Calendar Month';
                cycleRangeLabel = '[Jan–Dec]';
            } else if (state.cycleMode === 'custom_x') {
                cycleDimName = 'Cycle Day';
                cycleRangeLabel = `[Day 1–${Math.round(state.cyclePeriod || 3)}]`;
            }
        }

        // X Axis button & aggregation
        const xBtn = document.getElementById('gia-x-metric-btn');
        const xBtnText = document.getElementById('gia-x-metric-text');
        const xUnitText = document.getElementById('gia-x-metric-unit');
        const xAgg = document.getElementById('gia-x-agg-select');
        const swapBtn = document.getElementById('gia-swap-btn');

        if (isCycle) {
            if (xBtnText) xBtnText.textContent = cycleDimName;
            if (xUnitText) xUnitText.textContent = cycleRangeLabel;
            if (xBtn) {
                xBtn.disabled = true;
                xBtn.style.opacity = '0.9';
                xBtn.style.cursor = 'default';
                xBtn.title = 'X-axis is locked to the active cycle fold';
            }
            if (xAgg) {
                xAgg.disabled = true;
            }
            if (swapBtn) {
                swapBtn.disabled = true;
                swapBtn.style.opacity = '0.35';
                swapBtn.style.cursor = 'not-allowed';
                swapBtn.title = 'Swap unavailable in cycle fold mode';
            }
        } else {
            const xMetric = (state.x.metricId === 'date') ? { name: 'Calendar Date', unit: '' } : GIACatalog.getMetric(state.x.metricId);
            if (xBtnText) xBtnText.textContent = xMetric ? xMetric.name : (state.x.metricId === 'date' ? 'Calendar Date' : state.x.metricId);
            if (xUnitText && xMetric) xUnitText.textContent = xMetric.unit ? `[${xMetric.unit}]` : '';
            if (xBtn) {
                xBtn.disabled = false;
                xBtn.style.opacity = '';
                xBtn.style.cursor = '';
                xBtn.title = 'Select X-axis metric';
            }
            if (xAgg) {
                xAgg.value = state.x.aggregation;
                xAgg.disabled = (state.x.metricId === 'date');
            }
            if (swapBtn) {
                swapBtn.disabled = false;
                swapBtn.style.opacity = '';
                swapBtn.style.cursor = '';
                swapBtn.title = 'Swap X and Y metrics';
            }
        }

        // Y Axis button & aggregation
        const yMetric = GIACatalog.getMetric(state.y.metricId);
        const yBtnText = document.getElementById('gia-y-metric-text');
        const yUnitText = document.getElementById('gia-y-metric-unit');
        if (yBtnText) yBtnText.textContent = yMetric ? yMetric.name : state.y.metricId;
        if (yUnitText && yMetric) yUnitText.textContent = yMetric.unit ? `[${yMetric.unit}]` : '';

        const yAgg = document.getElementById('gia-y-agg-select');
        if (yAgg) yAgg.value = state.y.aggregation;

        // Secondary Y2 Axis toggle, button, & aggregation
        const y2Toggle = document.getElementById('gia-y2-enable-toggle');
        const isY2Active = !!(state.y2 && state.y2.enabled);
        if (y2Toggle) y2Toggle.checked = isY2Active;

        const y2Box = document.getElementById('gia-y2-box');
        if (y2Box) {
            if (isY2Active) {
                y2Box.classList.add('active');
                y2Box.classList.remove('inactive');
            } else {
                y2Box.classList.remove('active');
                y2Box.classList.add('inactive');
            }
        }

        const ySwapBtn = document.getElementById('gia-y1-y2-swap-btn');
        if (ySwapBtn) {
            ySwapBtn.disabled = !isY2Active;
        }

        const y2Metric = (state.y2 && state.y2.metricId) ? GIACatalog.getMetric(state.y2.metricId) : null;
        const y2BtnText = document.getElementById('gia-y2-metric-text');
        const y2UnitText = document.getElementById('gia-y2-metric-unit');
        if (y2BtnText) y2BtnText.textContent = y2Metric ? y2Metric.name : (state.y2 && state.y2.metricId ? state.y2.metricId : 'Select Secondary Metric...');
        if (y2UnitText && y2Metric) y2UnitText.textContent = y2Metric.unit ? `[${y2Metric.unit}]` : '';

        const y2Agg = document.getElementById('gia-y2-agg-select');
        if (y2Agg && state.y2) y2Agg.value = state.y2.aggregation || 'avg';

        // Trend controls
        const trendSelect = document.getElementById('gia-trend-select');
        if (trendSelect) trendSelect.value = state.trend.model || 'none';

        const corridorSelect = document.getElementById('gia-corridor-select');
        if (corridorSelect) corridorSelect.value = state.trend.band || 'ci95';

        // Update chart title in stage
        const titleEl = document.getElementById('gia-chart-title');
        if (titleEl) {
            const yTitle = yMetric ? yMetric.name : state.y.metricId;
            if (isCycle) {
                if (state.y2 && state.y2.enabled && y2Metric) {
                    titleEl.textContent = `${yTitle} & ${y2Metric.name} by ${cycleDimName}`;
                } else {
                    titleEl.textContent = `${yTitle} by ${cycleDimName}`;
                }
            } else {
                const xMetric = (state.x.metricId === 'date') ? { name: 'Calendar Date', unit: '' } : GIACatalog.getMetric(state.x.metricId);
                const xTitle = xMetric ? xMetric.name : (state.x.metricId === 'date' ? 'Calendar Date' : state.x.metricId);
                if (xTitle && yTitle) {
                    if (state.y2 && state.y2.enabled && y2Metric) {
                        titleEl.textContent = `${yTitle} & ${y2Metric.name} vs ${xTitle}`;
                    } else {
                        titleEl.textContent = `${yTitle} vs ${xTitle}`;
                    }
                }
            }
        }

        // Cycle Fold buttons
        document.querySelectorAll('.gia-cycle-btn').forEach(btn => {
            if (btn.dataset.cycle === (state.cycleMode || 'none')) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });

        // Custom cycle input row
        const customRow = document.getElementById('gia-cycle-custom-row');
        if (customRow) {
            customRow.style.display = (state.cycleMode === 'custom_x') ? 'block' : 'none';
        }
        const periodIn = document.getElementById('gia-cycle-period-input');
        if (periodIn) periodIn.value = state.cyclePeriod || 3.0;
        const anchorIn = document.getElementById('gia-cycle-anchor-input');
        if (anchorIn) anchorIn.value = state.cycleAnchor || '';

        // Day of Week Filter pills
        const curDow = state.dowFilter || [1, 2, 3, 4, 5, 6, 7];
        document.querySelectorAll('.gia-dow-pill').forEach(pill => {
            const dowVal = parseInt(pill.dataset.dow, 10);
            if (curDow.includes(dowVal)) {
                pill.classList.add('active');
            } else {
                pill.classList.remove('active');
            }
        });
    }

    function showAlert(msg, show, level) {
        const el = document.getElementById('gia-alert-banner');
        if (!el) return;
        if (!show || !msg) {
            el.style.display = 'none';
            el.textContent = '';
            el.className = 'gia-alert-banner';
            return;
        }
        el.textContent = msg;
        el.className = `gia-alert-banner ${level || 'error'}`;
        el.style.display = 'flex';
    }

    function getActiveDateRange() {
        const sEl = document.getElementById('start_date') || document.querySelector('input[name="start_date"]');
        const eEl = document.getElementById('end_date') || document.querySelector('input[name="end_date"]');

        let sVal = sEl ? sEl.value : '';
        let eVal = eEl ? eEl.value : '';

        if (!sVal || !eVal) {
            const urlParams = new URLSearchParams(window.location.search);
            sVal = urlParams.get('start_date') || '';
            eVal = urlParams.get('end_date') || '';
        }

        return { startDate: sVal, endDate: eVal };
    }

    let lastRenderedConfig = null;

    function getCurrentConfig() {
        const { startDate, endDate } = getActiveDateRange();
        return JSON.stringify({
            grain: state.grain,
            start_date: startDate,
            end_date: endDate,
            x: state.x,
            y: state.y,
            y2: state.y2,
            chartType: state.chartType,
            trend: state.trend,
            cycleMode: state.cycleMode,
            cyclePeriod: state.cyclePeriod,
            cycleAnchor: state.cycleAnchor,
            dowFilter: state.dowFilter
        });
    }

    function checkDirtyState() {
        const runBtn = document.getElementById('gia-run-btn');
        if (!runBtn) return;
        const currentConfig = getCurrentConfig();
        const isDirty = (lastRenderedConfig !== null) && (currentConfig !== lastRenderedConfig);

        if (isDirty) {
            runBtn.classList.add('gia-attention');
            let dot = runBtn.querySelector('.gia-attention-dot');
            if (!dot) {
                dot = document.createElement('span');
                dot.className = 'gia-attention-dot';
                dot.title = 'Changes pending — click to update graph';
                runBtn.appendChild(dot);
            }
            runBtn.title = 'Changes pending — click to update graph';
        } else {
            runBtn.classList.remove('gia-attention');
            const dot = runBtn.querySelector('.gia-attention-dot');
            if (dot) dot.remove();
            runBtn.title = 'Update graph with current parameters';
        }
    }

    let currentQueryController = null;
    let lastExecutionData = null;

    async function runQuery() {
        if (currentQueryController) {
            currentQueryController.abort();
            currentQueryController = null;
        }

        const controller = new AbortController();
        currentQueryController = controller;
        state.isLoading = true;

        const runBtn = document.getElementById('gia-run-btn');
        if (runBtn) {
            runBtn.disabled = true;
            runBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Running...';
        }

        showAlert('', false);

        const { startDate, endDate } = getActiveDateRange();
        if (!startDate || !endDate) {
            showAlert('Please select a valid date range using the date selector.', true, 'warning');
            state.isLoading = false;
            currentQueryController = null;
            if (runBtn) {
                runBtn.disabled = false;
                runBtn.innerHTML = '<i class="fa-solid fa-bolt"></i> Update Graph';
                checkDirtyState();
            }
            return;
        }

        // Capture closure-scoped parameters for this specific query run
        const isCycleMode = (state.cycleMode && state.cycleMode !== 'none');
        const queryX = isCycleMode ? { metricId: 'date', aggregation: 'none' } : Object.assign({}, state.x);
        const queryY = Object.assign({}, state.y);
        const queryY2 = (state.y2 && state.y2.enabled) ? Object.assign({}, state.y2) : null;
        const queryGrain = state.grain;
        const queryChartType = state.chartType;
        const queryTrend = Object.assign({}, state.trend);

        const payload = {
            grain: queryGrain,
            start_date: startDate,
            end_date: endDate,
            x: queryX,
            y: queryY,
            chartType: queryChartType,
            trend: queryTrend,
            cycle_mode: state.cycleMode || 'none',
            cycle_period: state.cyclePeriod || 3.0,
            cycle_anchor: state.cycleAnchor || startDate
        };

        if (state.dowFilter && state.dowFilter.length < 7) {
            payload.dow_filter = state.dowFilter;
        }

        if (queryY2) {
            payload.y2 = queryY2;
        }

        try {
            const resp = await fetch('/api/v1/graph_it_all/query', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal: controller.signal
            });

            const data = await resp.json();

            if (!resp.ok) {
                showAlert(data.error || `Server returned error ${resp.status}`, true, 'error');
                GIARenderer.clear();
                return;
            }

            // Resolve metric metadata using the EXACT query metrics that produced this data
            const cycleDimName = (state.cycleMode === 'diurnal_24h') ? 'Hour of Day' :
                                 (state.cycleMode === 'weekly_7d') ? 'Day of Week' :
                                 (state.cycleMode === 'annual_12m') ? 'Calendar Month' : 'Cycle Day';
            const xMetric = isCycleMode
                ? { id: 'date', name: cycleDimName, unit: '' }
                : ((queryX.metricId === 'date')
                    ? { id: 'date', name: 'Calendar Date', unit: '' }
                    : GIACatalog.getMetric(queryX.metricId));
            const yMetric = GIACatalog.getMetric(queryY.metricId);
            const y2Metric = (queryY2 && queryY2.metricId) ? GIACatalog.getMetric(queryY2.metricId) : null;

            GIARenderer.render(queryChartType, data, xMetric, yMetric, y2Metric);

            // Record execution data for CSV export
            lastExecutionData = {
                data: data.data || [],
                meta: data.meta || {},
                xMetric: xMetric,
                yMetric: yMetric,
                y2Metric: y2Metric,
                startDate: startDate,
                endDate: endDate
            };

            // Update status strip metadata
            const statusMetaEl = document.getElementById('gia-status-meta');
            if (statusMetaEl && data.meta) {
                const m = data.meta;
                statusMetaEl.textContent = `${m.points} points • ${m.nulls_excluded} nulls excluded • ${m.execution_ms}ms`;
            }

            // Successfully rendered — record clean baseline
            lastRenderedConfig = getCurrentConfig();
            checkDirtyState();

            // If a saved view was active, check if modified parameters differ from the view specification
            if (window.NSViews && typeof NSViews.getActiveViewId === 'function' && NSViews.getActiveViewId()) {
                const currentSpec = JSON.stringify(captureGraphDefinition());
                if (activeViewSpec && currentSpec !== activeViewSpec) {
                    NSViews.markViewDirty();
                    activeViewSpec = null;
                }
            }

        } catch (e) {
            if (e.name === 'AbortError') {
                return;
            }
            console.error('Graph-it-All query exception:', e);
            showAlert(`Failed to execute query: ${e.message}`, true, 'error');
            GIARenderer.clear();
        } finally {
            if (currentQueryController === controller) {
                currentQueryController = null;
                state.isLoading = false;
                if (runBtn) {
                    runBtn.disabled = false;
                    runBtn.innerHTML = '<i class="fa-solid fa-bolt"></i> Update Graph';
                    checkDirtyState();
                }
            }
        }
    }

    function exportCurrentData() {
        if (!lastExecutionData || !lastExecutionData.data || lastExecutionData.data.length === 0) {
            showAlert('No observation data points to export. Please run a query first.', true, 'warning');
            return;
        }

        const { data, xMetric, yMetric, y2Metric, startDate, endDate } = lastExecutionData;
        const hasY2 = !!(y2Metric && lastExecutionData.meta && lastExecutionData.meta.has_y2);

        const cleanHeader = (s) => (s || '').replace(/[\r\n",]/g, ' ').trim();
        const xName = cleanHeader(xMetric ? xMetric.name : 'X');
        const xUnit = xMetric && xMetric.unit ? ` (${cleanHeader(xMetric.unit)})` : '';
        const yName = cleanHeader(yMetric ? yMetric.name : 'Y');
        const yUnit = yMetric && yMetric.unit ? ` (${cleanHeader(yMetric.unit)})` : '';

        let csv = '';
        if (hasY2) {
            const y2Name = cleanHeader(y2Metric ? y2Metric.name : 'Y2');
            const y2Unit = y2Metric && y2Metric.unit ? ` (${cleanHeader(y2Metric.unit)})` : '';
            csv = `Date,"${xName}${xUnit}","${yName}${yUnit}","${y2Name}${y2Unit}"\r\n`;
            data.forEach(row => {
                const xVal = row[0];
                const yVal = row[1];
                const obsDate = row[2] || '';
                const y2Val = (row[3] !== null && row[3] !== undefined) ? row[3] : '';
                csv += `"${obsDate}",${xVal},${yVal},${y2Val}\r\n`;
            });
        } else {
            csv = `Date,"${xName}${xUnit}","${yName}${yUnit}"\r\n`;
            data.forEach(row => {
                const xVal = row[0];
                const yVal = row[1];
                const obsDate = row[2] || '';
                csv += `"${obsDate}",${xVal},${yVal}\r\n`;
            });
        }

        const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        const xPart = (xMetric && xMetric.id) || 'x';
        const yPart = (yMetric && yMetric.id) || 'y';
        const y2Part = hasY2 && y2Metric ? `_${y2Metric.id}` : '';
        const sDatePart = startDate || 'start';
        const eDatePart = endDate || 'end';
        link.setAttribute('href', url);
        link.setAttribute('download', `nightscout_${xPart}_vs_${yPart}${y2Part}_${sDatePart}_to_${eDatePart}.csv`);
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }

    function swapAxes() {
        if (state.x.metricId === 'date') {
            showAlert('Cannot swap axes when Calendar Date is the X-axis.', true, 'warning');
            return;
        }
        const temp = JSON.parse(JSON.stringify(state.x));
        state.x = JSON.parse(JSON.stringify(state.y));
        state.y = temp;
        syncControlsFromState();
        checkDirtyState();
    }

    function swapYAxes() {
        if (!state.y2 || !state.y2.enabled) {
            showAlert('Secondary Y₂ axis must be enabled to swap left and right axes.', true, 'info');
            return;
        }
        const tempMetric = state.y.metricId;
        const tempAgg = state.y.aggregation;
        state.y.metricId = state.y2.metricId;
        state.y.aggregation = state.y2.aggregation;
        state.y2.metricId = tempMetric;
        state.y2.aggregation = tempAgg;
        syncControlsFromState();
        checkDirtyState();
    }

    function initEvents() {
        // Grain buttons
        document.querySelectorAll('.gia-grain-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                if (state.grain === btn.dataset.grain) return;
                state.grain = btn.dataset.grain;
                syncControlsFromState();
                checkDirtyState();
            });
        });

        // Format buttons
        document.querySelectorAll('.gia-format-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                if (state.chartType === btn.dataset.format) return;
                state.chartType = btn.dataset.format;
                syncControlsFromState();
                checkDirtyState();
            });
        });

        // Cycle Fold buttons
        document.querySelectorAll('.gia-cycle-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const targetCycle = btn.dataset.cycle;
                if (state.cycleMode === targetCycle) return;

                // If switching to 24h diurnal, ensure grain is hourly (or 5min)
                if (targetCycle === 'diurnal_24h' && ['daily', 'weekly', 'monthly'].includes(state.grain)) {
                    state.grain = 'hourly';
                }

                state.cycleMode = targetCycle;
                syncControlsFromState();
                checkDirtyState();
            });
        });

        // Custom cycle period & anchor inputs
        const periodIn = document.getElementById('gia-cycle-period-input');
        if (periodIn) {
            periodIn.addEventListener('input', (e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val) && val > 0) {
                    state.cyclePeriod = val;
                    checkDirtyState();
                }
            });
        }

        const anchorIn = document.getElementById('gia-cycle-anchor-input');
        if (anchorIn) {
            anchorIn.addEventListener('change', (e) => {
                state.cycleAnchor = e.target.value;
                checkDirtyState();
            });
        }

        // Day of Week Filter pills
        document.querySelectorAll('.gia-dow-pill').forEach(pill => {
            pill.addEventListener('click', () => {
                const day = parseInt(pill.dataset.dow, 10);
                let cur = state.dowFilter ? state.dowFilter.slice() : [1, 2, 3, 4, 5, 6, 7];
                if (cur.includes(day)) {
                    // Prevent deselecting all days
                    if (cur.length > 1) {
                        cur = cur.filter(d => d !== day);
                    }
                } else {
                    cur.push(day);
                    cur.sort((a, b) => a - b);
                }
                state.dowFilter = cur;
                syncControlsFromState();
                checkDirtyState();
            });
        });

        // DOW Quick links
        const dowAllBtn = document.getElementById('gia-dow-all-btn');
        if (dowAllBtn) {
            dowAllBtn.addEventListener('click', () => {
                state.dowFilter = [1, 2, 3, 4, 5, 6, 7];
                syncControlsFromState();
                checkDirtyState();
            });
        }

        const dowWeekdaysBtn = document.getElementById('gia-dow-weekdays-btn');
        if (dowWeekdaysBtn) {
            dowWeekdaysBtn.addEventListener('click', () => {
                state.dowFilter = [1, 2, 3, 4, 5];
                syncControlsFromState();
                checkDirtyState();
            });
        }

        const dowWeekendsBtn = document.getElementById('gia-dow-weekends-btn');
        if (dowWeekendsBtn) {
            dowWeekendsBtn.addEventListener('click', () => {
                state.dowFilter = [6, 7];
                syncControlsFromState();
                checkDirtyState();
            });
        }

        // X Axis Metric click
        const xBtn = document.getElementById('gia-x-metric-btn');
        if (xBtn) {
            xBtn.addEventListener('click', () => {
                GIACatalog.openDrawer('x', (selected) => {
                    state.x.metricId = selected.id;
                    state.x.aggregation = selected.default_aggregation || 'avg';
                    syncControlsFromState();
                    checkDirtyState();
                });
            });
        }

        // Y Axis Metric click
        const yBtn = document.getElementById('gia-y-metric-btn');
        if (yBtn) {
            yBtn.addEventListener('click', () => {
                GIACatalog.openDrawer('y', (selected) => {
                    state.y.metricId = selected.id;
                    state.y.aggregation = selected.default_aggregation || 'avg';
                    syncControlsFromState();
                    checkDirtyState();
                });
            });
        }

        // Secondary Y2 Metric click
        const y2Btn = document.getElementById('gia-y2-metric-btn');
        if (y2Btn) {
            y2Btn.addEventListener('click', () => {
                GIACatalog.openDrawer('y2', (selected) => {
                    state.y2.metricId = selected.id;
                    state.y2.aggregation = selected.default_aggregation || 'avg';
                    syncControlsFromState();
                    checkDirtyState();
                });
            });
        }

        // Secondary Y2 Enable Toggle
        const y2Toggle = document.getElementById('gia-y2-enable-toggle');
        if (y2Toggle) {
            y2Toggle.addEventListener('change', (e) => {
                state.y2.enabled = e.target.checked;
                syncControlsFromState();
                checkDirtyState();
            });
        }

        // Secondary Y2 Aggregation select
        const y2Agg = document.getElementById('gia-y2-agg-select');
        if (y2Agg) {
            y2Agg.addEventListener('change', (e) => {
                state.y2.aggregation = e.target.value;
                checkDirtyState();
            });
        }

        // Swap X and Y1 button
        const swapBtn = document.getElementById('gia-swap-btn');
        if (swapBtn) {
            swapBtn.addEventListener('click', swapAxes);
        }

        // Swap Left (Y1) and Right (Y2) button
        const y1y2SwapBtn = document.getElementById('gia-y1-y2-swap-btn');
        if (y1y2SwapBtn) {
            y1y2SwapBtn.addEventListener('click', swapYAxes);
        }

        // Aggregation selects
        const xAgg = document.getElementById('gia-x-agg-select');
        if (xAgg) {
            xAgg.addEventListener('change', (e) => {
                state.x.aggregation = e.target.value;
                checkDirtyState();
            });
        }

        const yAgg = document.getElementById('gia-y-agg-select');
        if (yAgg) {
            yAgg.addEventListener('change', (e) => {
                state.y.aggregation = e.target.value;
                checkDirtyState();
            });
        }

        // Trend model select
        const trendSelect = document.getElementById('gia-trend-select');
        if (trendSelect) {
            trendSelect.addEventListener('change', (e) => {
                state.trend.model = e.target.value;
                checkDirtyState();
            });
        }

        // Corridor / Quantile Ribbon dropdown
        const corridorSelect = document.getElementById('gia-corridor-select');
        if (corridorSelect) {
            corridorSelect.addEventListener('change', (e) => {
                state.trend.band = e.target.value;
                checkDirtyState();
            });
        }

        // Export button
        const exportBtn = document.getElementById('gia-export-btn');
        if (exportBtn) {
            exportBtn.addEventListener('click', exportCurrentData);
        }

        // Run button
        const runBtn = document.getElementById('gia-run-btn');
        if (runBtn) {
            runBtn.addEventListener('click', runQuery);
        }

        // Listen for date changes dispatched by ns-date-selector
        window.addEventListener('ns-date-change', () => {
            checkDirtyState();
        });
        document.getElementById('start_date')?.addEventListener('change', checkDirtyState);
        document.getElementById('end_date')?.addEventListener('change', checkDirtyState);
        document.getElementById('date-preset')?.addEventListener('change', () => {
            setTimeout(checkDirtyState, 50);
        });
        document.addEventListener('dateRangeChanged', checkDirtyState);
    }

    async function init() {
        await GIACatalog.loadCatalog();
        GIACatalog.initDrawer();
        GIARenderer.initChart('graph-it-all-chart');

        if (window.NSAxisPillars) {
            NSAxisPillars.init({
                storageKey: null,
                clamp: {},
                onLiveUpdate: () => {
                    GIARenderer.reRenderCurrentBounds();
                },
                onCommit: () => {
                    checkDirtyState();
                }
            });
        }

        initEvents();

        // Help Popover trigger & dismiss delegation
        document.addEventListener('click', (e) => {
            const trigger = e.target.closest('.gia-popover-trigger');
            if (trigger) {
                e.stopPropagation();
                const wrapper = trigger.closest('.gia-popover-wrapper');
                const popover = wrapper ? wrapper.querySelector('.gia-guide-popover') : null;
                const isCurrentlyActive = popover && popover.classList.contains('active');

                // Close all GIA popovers first
                document.querySelectorAll('.gia-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.gia-popover-trigger.active').forEach(t => t.classList.remove('active'));

                if (popover && !isCurrentlyActive) {
                    popover.classList.add('active');
                    trigger.classList.add('active');
                }
                return;
            }

            if (e.target.closest('.gia-guide-popover')) {
                return;
            }

            document.querySelectorAll('.gia-guide-popover.active').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.gia-popover-trigger.active').forEach(t => t.classList.remove('active'));
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                document.querySelectorAll('.gia-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.gia-popover-trigger.active').forEach(t => t.classList.remove('active'));
            }
        });

        // Initialize NSViews integration and await DB saved-views hydration
        if (window.NSViews) {
            await NSViews.init({
                page: 'graph_it_all',
                activeViewKey: 'ns_graph_it_all_active_view',
                defaultViews: DEFAULT_GRAPH_VIEWS,
                titleElId: 'gia-chart-title',
                titleFor: (view) => view ? view.name : (document.getElementById('gia-chart-title') ? document.getElementById('gia-chart-title').textContent : 'Exploratory Analytics Workbench'),
                settingsPopoverId: 'gia-settings-popover',
                settingsBtnId: 'gia-settings-btn',
                captureState: captureGraphDefinition,
                applyState: applyGraphDefinition
            });
        }

        syncControlsFromState();
        lastRenderedConfig = getCurrentConfig();
        await runQuery();
    }

    global.GIABuilder = {
        init,
        getState,
        captureGraphDefinition,
        applyGraphDefinition,
        runQuery,
        swapAxes,
        swapYAxes,
        exportCurrentData
    };

    document.addEventListener('DOMContentLoaded', init);

})(window);
