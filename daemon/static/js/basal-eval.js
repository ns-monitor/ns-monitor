/**
 * basal-eval.js — Visual ECharts Renderer and Controller for Modal Basal Evaluation
 */

(function () {
    'use strict';

    let engine = null;
    let chartIob = null;
    let chartBasal = null;
    let chartDev = null;
    let chartDensity = null;
    let isFetching = false;

    // DOM Elements
    const drawerEl = document.getElementById('be-filter-drawer');
    const toggleFiltersBtn = document.getElementById('be-toggle-filters-btn');
    const resetFiltersBtn = document.getElementById('be-reset-filters-btn');
    const loadingOverlay = document.getElementById('be-loading-overlay');
    const confidenceBadge = document.getElementById('be-confidence-badge');
    const confidenceText = document.getElementById('be-confidence-text');

    // Filter Control Inputs
    const mealTailSlider = document.getElementById('filter-meal-tail');
    const cobCutoffSlider = document.getElementById('filter-cob-cutoff');
    const uamTailSlider = document.getElementById('filter-uam-tail');
    const smbModeSwitch = document.getElementById('filter-smb-mode-switch');

    // Filter Value Displays & Chips
    const dispMealTail = document.getElementById('disp-meal-tail');
    const dispCobCutoff = document.getElementById('disp-cob-cutoff');
    const dispUamTail = document.getElementById('disp-uam-tail');
    const dispSmbModeDesc = document.getElementById('disp-smb-mode-desc');

    const chipMealTail = document.getElementById('chip-meal-tail');
    const chipCobCutoff = document.getElementById('chip-cob-cutoff');
    const chipUamTail = document.getElementById('chip-uam-tail');
    const chipSmbMode = document.getElementById('chip-smb-mode');

    // -------------------------------------------------------------
    // Strategic (i) Help Popovers
    // -------------------------------------------------------------
    function initHelpPopovers() {
        document.querySelectorAll('.be-popover-trigger').forEach(trigger => {
            trigger.addEventListener('click', function (e) {
                e.stopPropagation();
                const popover = this.parentElement.querySelector('.be-guide-popover');
                if (!popover) return;
                const wasActive = popover.classList.contains('active');

                // Close all other popovers
                document.querySelectorAll('.be-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.be-popover-trigger.active').forEach(t => t.classList.remove('active'));

                if (!wasActive) {
                    popover.classList.add('active');
                    this.classList.add('active');
                }
            });
        });

        // Close when clicking outside
        document.addEventListener('click', function (e) {
            if (!e.target.closest('.be-popover-wrapper')) {
                document.querySelectorAll('.be-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.be-popover-trigger.active').forEach(t => t.classList.remove('active'));
            }
        });
    }

    function initCharts() {
        const iobContainer = document.getElementById('be-chart-iob');
        const basalContainer = document.getElementById('be-chart-basal');
        const devContainer = document.getElementById('be-chart-dev');
        const densityContainer = document.getElementById('be-chart-density');

        if (!iobContainer || !basalContainer || !devContainer || !densityContainer) return;

        chartIob = echarts.init(iobContainer);
        chartBasal = echarts.init(basalContainer);
        chartDev = echarts.init(devContainer);
        chartDensity = echarts.init(densityContainer);

        // Connect crosshairs across all 4 panels
        echarts.connect([chartIob, chartBasal, chartDev, chartDensity]);

        window.addEventListener('resize', () => {
            chartIob && chartIob.resize();
            chartBasal && chartBasal.resize();
            chartDev && chartDev.resize();
            chartDensity && chartDensity.resize();
        });
    }

    function getCommonXAxis(labels) {
        return {
            type: 'category',
            data: labels,
            boundaryGap: false,
            axisLine: { lineStyle: { color: '#30363d' } },
            axisTick: { alignWithLabel: true, interval: 23 },
            axisLabel: {
                color: '#8b949e',
                interval: 23, // 2-hour major gridlines (24 bins = 2h)
                formatter: (val) => val
            },
            splitLine: {
                show: true,
                interval: 23,
                lineStyle: { color: 'rgba(48, 54, 61, 0.45)', type: 'dashed' }
            }
        };
    }

    function renderAllPanels(result) {
        if (!result || !result.binStats || result.binStats.length === 0) return;

        const bins = result.binStats;
        const timeLabels = bins.map(b => b.timeLabel);

        // Update Confidence Badge in Toolbar
        updateConfidenceBadge(result);

        // PANEL 1: Net IOB Modal Ribbon
        const iobMedian = bins.map(b => b.iobMedian);
        const iobP25 = bins.map(b => b.iobP25);
        const iobP75 = bins.map(b => b.iobP75);

        const iobOption = {
            animation: false,
            grid: { top: 36, right: 30, bottom: 25, left: 55 },
            tooltip: {
                trigger: 'axis',
                backgroundColor: '#161b22',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 12 },
                formatter: (params) => {
                    const idx = params[0].dataIndex;
                    const b = bins[idx];
                    return `<strong>${b.timeLabel}</strong> (Clean Days: ${b.N}/${b.totalDays})<br/>`
                        + `<span style="color:#58a6ff;">Median Net IOB:</span> <strong>${b.iobMedian} U</strong><br/>`
                        + `<span style="color:#8b949e;">IQR (25th–75th):</span> [${b.iobP25} U to ${b.iobP75} U]`;
                }
            },
            xAxis: getCommonXAxis(timeLabels),
            yAxis: {
                type: 'value',
                name: 'Net IOB (U)',
                nameLocation: 'end',
                nameGap: 12,
                nameTextStyle: { color: '#8b949e', fontSize: 11, align: 'left' },
                splitLine: { lineStyle: { color: 'rgba(48, 54, 61, 0.5)' } },
                axisLabel: { color: '#8b949e', formatter: '{value} U' }
            },
            series: [
                // Zero baseline
                {
                    type: 'line',
                    data: timeLabels.map(() => 0),
                    symbol: 'none',
                    lineStyle: { color: 'rgba(230, 237, 243, 0.35)', type: 'dashed', width: 1.5 },
                    silent: true
                },
                // Lower bound (p25)
                {
                    name: 'IOB P25',
                    type: 'line',
                    data: iobP25,
                    symbol: 'none',
                    lineStyle: { opacity: 0 },
                    stack: 'iob-confidence'
                },
                // Upper bound ribbon (p75 - p25)
                {
                    name: 'IOB IQR Ribbon',
                    type: 'line',
                    data: iobP75.map((v, i) => Math.max(0, v - iobP25[i])),
                    symbol: 'none',
                    lineStyle: { opacity: 0 },
                    areaStyle: { color: 'rgba(88, 166, 255, 0.22)' },
                    stack: 'iob-confidence'
                },
                // Median Curve
                {
                    name: 'Median Net IOB',
                    type: 'line',
                    data: iobMedian,
                    symbol: 'none',
                    smooth: true,
                    lineStyle: { color: '#58a6ff', width: 2.5 }
                }
            ]
        };
        chartIob.setOption(iobOption);

        // PANEL 2: Scheduled vs Enacted Basal Rate
        const schedBasal = bins.map(b => b.schedBasal);
        const enactedBasal = bins.map(b => b.enactedBasal);
        const effectiveBasal = bins.map(b => b.effectiveBasal);

        const basalOption = {
            animation: false,
            grid: { top: 40, right: 30, bottom: 25, left: 55 },
            legend: {
                data: ['Scheduled Profile', 'Enacted Pump Basal', 'Effective Total (Basal + SMB)'],
                textStyle: { color: '#8b949e', fontSize: 11 },
                top: 2,
                right: 30
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: '#161b22',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 12 },
                formatter: (params) => {
                    const idx = params[0].dataIndex;
                    const b = bins[idx];
                    return `<strong>${b.timeLabel}</strong> (Clean Days: ${b.N})<br/>`
                        + `<span style="color:#79c0ff;">Scheduled Profile:</span> <strong>${b.schedBasal} U/hr</strong><br/>`
                        + `<span style="color:#1f6feb;">Enacted Pump Basal:</span> <strong>${b.enactedBasal} U/hr</strong><br/>`
                        + `<span style="color:#f0883e;">Effective Total:</span> <strong>${b.effectiveBasal} U/hr</strong><br/>`
                        + `<span style="color:${b.basalDelta >= 0 ? '#2ea02e' : '#f85149'};">Basal Delta:</span> <strong>${b.basalDelta >= 0 ? '+' : ''}${b.basalDelta} U/hr</strong>`;
                }
            },
            xAxis: getCommonXAxis(timeLabels),
            yAxis: {
                type: 'value',
                name: 'Delivery (U/hr)',
                nameLocation: 'end',
                nameGap: 12,
                nameTextStyle: { color: '#8b949e', fontSize: 11, align: 'left' },
                splitLine: { lineStyle: { color: 'rgba(48, 54, 61, 0.5)' } },
                axisLabel: { color: '#8b949e', formatter: '{value} U/h' }
            },
            series: [
                {
                    name: 'Scheduled Profile',
                    type: 'line',
                    step: 'end',
                    data: schedBasal,
                    symbol: 'none',
                    lineStyle: { color: '#79c0ff', width: 2, type: 'dashed' }
                },
                {
                    name: 'Enacted Pump Basal',
                    type: 'line',
                    data: enactedBasal,
                    symbol: 'none',
                    smooth: true,
                    lineStyle: { color: '#1f6feb', width: 2 }
                },
                {
                    name: 'Effective Total (Basal + SMB)',
                    type: 'line',
                    data: effectiveBasal,
                    symbol: 'none',
                    smooth: true,
                    lineStyle: { color: '#f0883e', width: 2.2 }
                }
            ]
        };
        chartBasal.setOption(basalOption);

        // PANEL 3: Modal Deviation State Composition Strip
        const pctEqual = bins.map(b => b.pctEqual);
        const pctSens = bins.map(b => b.pctSens);
        const pctRes = bins.map(b => b.pctRes);

        const devOption = {
            animation: false,
            grid: { top: 20, right: 30, bottom: 25, left: 55 },
            tooltip: {
                trigger: 'axis',
                backgroundColor: '#161b22',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 12 },
                formatter: (params) => {
                    const idx = params[0].dataIndex;
                    const b = bins[idx];
                    return `<strong>${b.timeLabel} Deviation Composition</strong><br/>`
                        + `<span style="color:#8b949e;">Neutral (|dev| &lt; 2.0):</span> <strong>${b.pctEqual}%</strong><br/>`
                        + `<span style="color:#f85149;">Sensitivity (dropping):</span> <strong>${b.pctSens}%</strong><br/>`
                        + `<span style="color:#2ea02e;">Resistance (rising):</span> <strong>${b.pctRes}%</strong>`;
                }
            },
            xAxis: getCommonXAxis(timeLabels),
            yAxis: {
                type: 'value',
                max: 100,
                splitLine: { show: false },
                axisLabel: { color: '#8b949e', formatter: '{value}%' }
            },
            series: [
                {
                    name: 'Neutral (EQUAL)',
                    type: 'bar',
                    stack: 'dev-composition',
                    data: pctEqual,
                    itemStyle: { color: '#282828' }
                },
                {
                    name: 'Sensitivity (SENS)',
                    type: 'bar',
                    stack: 'dev-composition',
                    data: pctSens,
                    itemStyle: { color: '#d62d2d' }
                },
                {
                    name: 'Resistance (RES)',
                    type: 'bar',
                    stack: 'dev-composition',
                    data: pctRes,
                    itemStyle: { color: '#2ea02e' }
                }
            ]
        };
        chartDev.setOption(devOption);

        // FOOTER: Sample Density & Confidence Gauge
        const densityData = bins.map(b => {
            let color = '#484f58'; // low
            if (b.confidence === 'high') color = '#2ea02e';
            else if (b.confidence === 'medium') color = '#d29922';
            return {
                value: b.N,
                itemStyle: { color }
            };
        });

        const densityOption = {
            animation: false,
            grid: { top: 15, right: 30, bottom: 25, left: 55 },
            tooltip: {
                trigger: 'axis',
                backgroundColor: '#161b22',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 12 },
                formatter: (params) => {
                    const idx = params[0].dataIndex;
                    const b = bins[idx];
                    let confText = '<span style="color:#2ea02e;">High</span>';
                    if (b.confidence === 'medium') confText = '<span style="color:#d29922;">Medium</span>';
                    else if (b.confidence === 'low') confText = '<span style="color:#f85149;">Low / Sparse</span>';
                    return `<strong>${b.timeLabel} Sample Density</strong><br/>`
                        + `Clean Days: <strong>${b.N}</strong> / ${b.totalDays} (${Math.round(b.N / (b.totalDays || 1) * 100)}%)<br/>`
                        + `Confidence: <strong>${confText}</strong>`;
                }
            },
            xAxis: getCommonXAxis(timeLabels),
            yAxis: {
                type: 'value',
                splitLine: { lineStyle: { color: 'rgba(48, 54, 61, 0.35)' } },
                axisLabel: { color: '#8b949e', formatter: '{value}d' }
            },
            series: [
                {
                    name: 'Valid Days',
                    type: 'bar',
                    data: densityData,
                    barWidth: '75%'
                }
            ]
        };
        chartDensity.setOption(densityOption);
    }

    function updateConfidenceBadge(result) {
        if (!confidenceBadge || !confidenceText) return;
        const pct = result.cleanPct || 0;
        let badgeClass = 'high';
        let label = `Clean Basal Windows: ${pct}% of Timeline`;

        if (pct < 15) {
            badgeClass = 'low';
            label += ' (Very Sparse)';
        } else if (pct < 35) {
            badgeClass = 'medium';
            label += ' (Moderate Coverage)';
        } else {
            label += ' (Robust Coverage)';
        }

        confidenceBadge.className = `be-confidence-badge ${badgeClass}`;
        confidenceText.textContent = label;
    }

    function recomputeAndRefresh() {
        if (!engine) return;
        const result = engine.compute();
        renderAllPanels(result);
    }

    async function fetchDataset(startDate, endDate) {
        if (isFetching) return;
        isFetching = true;
        if (loadingOverlay) loadingOverlay.style.display = 'flex';

        try {
            const resp = await fetch(`/api/v1/basal_eval_data?start=${encodeURIComponent(startDate)}&end=${encodeURIComponent(endDate)}`);
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            const payload = await resp.json();
            if (payload.error) {
                throw new Error(payload.error);
            }
            engine.loadDataset(payload);
            recomputeAndRefresh();
        } catch (err) {
            console.error('[basal-eval] Failed to fetch dataset:', err);
            if (confidenceText) confidenceText.textContent = 'Error loading dataset: ' + err.message;
        } finally {
            isFetching = false;
            if (loadingOverlay) loadingOverlay.style.display = 'none';
        }
    }

    function setupControls() {
        initHelpPopovers();

        // Toggle Filter Drawer
        if (toggleFiltersBtn && drawerEl) {
            toggleFiltersBtn.addEventListener('click', () => {
                drawerEl.classList.toggle('open');
                toggleFiltersBtn.classList.toggle('btn-primary');
            });
        }

        // Meal Tail Slider
        if (mealTailSlider) {
            mealTailSlider.addEventListener('input', (e) => {
                const val = parseFloat(e.target.value);
                dispMealTail.textContent = val.toFixed(1) + ' hrs';
                chipMealTail.innerHTML = `Meal Tail: <strong>${val.toFixed(1)}h</strong>`;
                engine.updateFilter('mealTailHours', val);
                recomputeAndRefresh();
            });
        }

        // COB Cutoff Slider
        if (cobCutoffSlider) {
            cobCutoffSlider.addEventListener('input', (e) => {
                const val = parseFloat(e.target.value);
                dispCobCutoff.textContent = val.toFixed(1) + ' g';
                chipCobCutoff.innerHTML = `COB: <strong>${val.toFixed(0)}g</strong>`;
                engine.updateFilter('cobCutoff', val);
                recomputeAndRefresh();
            });
        }

        // UAM Tail Slider
        if (uamTailSlider) {
            uamTailSlider.addEventListener('input', (e) => {
                const val = parseFloat(e.target.value);
                dispUamTail.textContent = val.toFixed(1) + ' hrs';
                chipUamTail.innerHTML = `UAM Tail: <strong>${val.toFixed(1)}h</strong>`;
                engine.updateFilter('uamTailHours', val);
                recomputeAndRefresh();
            });
        }

        // SMB Mode Switch
        if (smbModeSwitch) {
            smbModeSwitch.addEventListener('click', (e) => {
                const btn = e.target.closest('.be-seg-btn');
                if (!btn) return;
                const mode = btn.dataset.mode;
                smbModeSwitch.querySelectorAll('.be-seg-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');

                let modeLabel = 'Effective Basal';
                let desc = 'Translates SMBs into equivalent delivery rate; preserves statistical power.';
                if (mode === 'burst') {
                    modeLabel = 'Burst Filter';
                    desc = 'Excludes 60m following bolus bursts (>0.3 U in 30m); retains routine maintenance ticks.';
                } else if (mode === 'strict') {
                    modeLabel = 'Strict 30m';
                    desc = 'Traditional blanket blackout after any micro-bolus (reduces sample size significantly).';
                }

                dispSmbModeDesc.textContent = desc;
                chipSmbMode.innerHTML = `SMB: <strong>${modeLabel}</strong>`;
                engine.updateFilter('smbMode', mode);
                recomputeAndRefresh();
            });
        }

        // Reset to Defaults
        if (resetFiltersBtn) {
            resetFiltersBtn.addEventListener('click', () => {
                mealTailSlider.value = 3.0;
                dispMealTail.textContent = '3.0 hrs';
                chipMealTail.innerHTML = 'Meal Tail: <strong>3.0h</strong>';
                engine.updateFilter('mealTailHours', 3.0);

                cobCutoffSlider.value = 0;
                dispCobCutoff.textContent = '0.0 g';
                chipCobCutoff.innerHTML = 'COB: <strong>0g</strong>';
                engine.updateFilter('cobCutoff', 0.0);

                uamTailSlider.value = 2.0;
                dispUamTail.textContent = '2.0 hrs';
                chipUamTail.innerHTML = 'UAM Tail: <strong>2.0h</strong>';
                engine.updateFilter('uamTailHours', 2.0);

                smbModeSwitch.querySelectorAll('.be-seg-btn').forEach(b => b.classList.remove('active'));
                const defaultBtn = smbModeSwitch.querySelector('[data-mode="effective"]');
                if (defaultBtn) defaultBtn.classList.add('active');
                dispSmbModeDesc.textContent = 'Translates SMBs into equivalent delivery rate; preserves statistical power.';
                chipSmbMode.innerHTML = 'SMB: <strong>Effective Basal</strong>';
                engine.updateFilter('smbMode', 'effective');

                recomputeAndRefresh();
            });
        }

        // Date range synchronization helpers
        function onDateRangeChanged(s, e) {
            const start = s || document.getElementById('start_date')?.value;
            const end = e || document.getElementById('end_date')?.value;
            if (start && end) {
                fetchDataset(start, end);
            }
        }

        // Canonical ns-date-selector hooks
        window.syncAndSubmit = () => onDateRangeChanged();
        window.onDateOrPresetChange = () => onDateRangeChanged();
        window.onDateChange = (s, e) => onDateRangeChanged(s, e);

        // Listen for custom date selector range changes
        window.addEventListener('ns-date-change', (e) => {
            const s = e.detail?.startDate || e.detail?.start;
            const end = e.detail?.endDate || e.detail?.end;
            if (s && end) {
                fetchDataset(s, end);
            }
        });
    }

    // Initialization on page ready
    document.addEventListener('DOMContentLoaded', () => {
        engine = new window.BasalEvalEngine();
        initCharts();
        setupControls();

        // Initial date detection
        const startInput = document.getElementById('start_date');
        const endInput = document.getElementById('end_date');

        let start = startInput ? startInput.value : '';
        let end = endInput ? endInput.value : '';

        if (!start || !end) {
            const today = new Date();
            const past30 = new Date(today.getTime() - 29 * 24 * 3600 * 1000);
            end = today.toISOString().slice(0, 10);
            start = past30.toISOString().slice(0, 10);
        }

        fetchDataset(start, end);
    });
})();
