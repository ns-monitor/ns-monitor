/**
 * Empirical Periodicity Explorer — Frontend Engine
 *
 * Coordinates server-side Lomb-Scargle spectrum, phase folding,
 * telemetry meter, and annual calendar raster.
 */

(() => {
    'use strict';

    const METRIC_CONFIGS = {
        cv: { label: 'CV (%)', unit: '%' },
        titr: { label: 'TITR (%)', unit: '%' },
        tir: { label: 'TIR (%)', unit: '%' },
        tdd: { label: 'TDD (U)', unit: 'U' },
        mean: { label: 'Mean BG', unit: 'mmol/L' },
        sd: { label: 'SD (mmol/L)', unit: 'mmol/L' }
    };

    // State
    let activeMetric = 'cv';
    let isDetrended = true;
    let currentPeriod = 14.0;
    let analysisData = null;
    let abortController = null;
    let isRequestPending = false;
    let stepDebounceTimer = null;
    let rasterSlantValue = 0.0;

    // Chart instances
    let spectrumChart = null;
    let epochFoldChart = null;

    // DOM Elements
    const metricBtnGroup = document.getElementById('metricBtnGroup');
    const linearDetrendCheck = document.getElementById('linearDetrendCheck');
    const displayDateSpan = document.getElementById('displayDateSpan');
    const displayObsCount = document.getElementById('displayObsCount');
    const periodicityStatus = document.getElementById('periodicityStatus') || document.getElementById('periodicity-status');

    const tier1CardsContainer = document.getElementById('tier1CardsContainer');
    const spectrumContainer = document.getElementById('periodicity-spectrum');
    const peakButtonContainer = document.getElementById('peakButtonContainer');

    const foldedPeriodBadge = document.getElementById('foldedPeriodBadge');
    const resonanceLockPill = document.getElementById('resonanceLockPill');
    const resonancePercentLabel = document.getElementById('resonancePercentLabel');
    const resonanceProgressBar = document.getElementById('resonanceProgressBar');
    const resonanceSlopeHint = document.getElementById('resonanceSlopeHint');
    const foldedKruskalBadge = document.getElementById('foldedKruskalBadge');
    const foldedSpreadBadge = document.getElementById('foldedSpreadBadge');
    const foldedCycleCount = document.getElementById('foldedCycleCount');

    const stepMinus1 = document.getElementById('stepMinus1');
    const stepMinus01 = document.getElementById('stepMinus01');
    const stepCurrentDisplay = document.getElementById('stepCurrentDisplay');
    const stepPlus01 = document.getElementById('stepPlus01');
    const stepPlus1 = document.getElementById('stepPlus1');
    const snapNearestPeakBtn = document.getElementById('snapNearestPeakBtn');

    const periodicityBand = document.getElementById('periodicityBand');
    const periodScrubber = document.getElementById('periodScrubber');
    const manualPeriodInput = document.getElementById('manualPeriodInput');
    const applyManualPeriodBtn = document.getElementById('applyManualPeriodBtn');

    const rasterCanvas = document.getElementById('calendarRasterCanvas');
    const rasterMinLabel = document.getElementById('rasterMinLabel');
    const rasterMaxLabel = document.getElementById('rasterMaxLabel');

    function getDateRange() {
        const startInput = document.getElementById('start_date');
        const endInput = document.getElementById('end_date');
        return [
            startInput ? startInput.value : '',
            endInput ? endInput.value : ''
        ];
    }

    function syncControls(p) {
        currentPeriod = Math.max(2.0, Math.min(400.0, Math.round(p * 10) / 10));
        const pStr = currentPeriod.toFixed(1);

        if (manualPeriodInput && document.activeElement !== manualPeriodInput) {
            manualPeriodInput.value = pStr;
        }
        if (stepCurrentDisplay) {
            stepCurrentDisplay.textContent = `${pStr}d`;
        }
        if (foldedPeriodBadge) {
            foldedPeriodBadge.textContent = `Period: ${pStr} Days`;
        }

        // Auto-switch scrubber band if macro period is chosen
        if (currentPeriod > 60.0 && periodicityBand) {
            periodicityBand.value = '400';
        }
        updateScrubberLimits();
        if (periodScrubber && document.activeElement !== periodScrubber) {
            periodScrubber.value = currentPeriod;
        }
    }

    function updateScrubberLimits() {
        if (!periodScrubber || !periodicityBand) return;
        const isMacro = periodicityBand.value === '400';
        periodScrubber.min = isMacro ? '60.0' : '2.0';
        periodScrubber.max = isMacro ? '400.0' : '60.0';
    }

    function setMetric(newMetric) {
        if (!METRIC_CONFIGS[newMetric] || activeMetric === newMetric) return;
        activeMetric = newMetric;

        // Sync all metric dropdowns
        document.querySelectorAll('.periodicity-metric-select').forEach(sel => {
            sel.value = newMetric;
        });

        // Sync top button group
        if (metricBtnGroup) {
            metricBtnGroup.querySelectorAll('.periodicity-metric-btn').forEach(btn => {
                if (btn.dataset.metric === newMetric) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });
        }

        loadAnalysis(currentPeriod);
    }

    async function loadAnalysis(targetPeriod, isPeriodOnly = false) {
        if (targetPeriod !== undefined && targetPeriod !== null) {
            syncControls(targetPeriod);
        }

        const [startDate, endDate] = getDateRange();
        if (!startDate || !endDate) return;

        if (abortController) {
            abortController.abort();
        }
        abortController = new AbortController();

        isRequestPending = true;
        if (periodicityStatus && periodicityStatus.style.display !== 'none') {
            periodicityStatus.style.display = 'none';
            periodicityStatus.textContent = '';
        }

        try {
            const params = new URLSearchParams({
                start_date: startDate,
                end_date: endDate,
                metric: activeMetric,
                detrend: String(isDetrended),
                period: currentPeriod.toFixed(1)
            });

            const res = await fetch('/api/v1/periodicity?' + params.toString(), {
                signal: abortController.signal
            });

            const data = await res.json();
            if (!res.ok || data.error) {
                if (periodicityStatus) {
                    periodicityStatus.style.display = 'block';
                    periodicityStatus.textContent = data.error || 'Analysis unavailable for this date range.';
                }
                return;
            }

            analysisData = data;
            isRequestPending = false;

            if (periodicityStatus) {
                periodicityStatus.style.display = 'none';
                periodicityStatus.textContent = '';
            }

            // Sync period from server if adjusted
            if (data.period) {
                syncControls(data.period);
            }

            if (isPeriodOnly) {
                // Targeted period update: only re-render Tier 3 and Scout needle
                renderPhaseFoldChart(data);
                updateScoutNeedleInPlace();
            } else {
                // Full reload: render all visual tiers
                renderHeaderBadges();
                renderTier1Findings(data);
                renderSpectrumChart(data);
                renderPhaseFoldChart(data);
                renderCalendarRaster(data);
            }

        } catch (err) {
            if (err.name !== 'AbortError') {
                if (periodicityStatus) {
                    periodicityStatus.style.display = 'block';
                    periodicityStatus.textContent = 'Error loading periodicity analysis.';
                }
                console.error('Periodicity fetch failed:', err);
            }
        } finally {
            isRequestPending = false;
        }
    }

    function debouncedStep(newPeriod) {
        syncControls(newPeriod);
        // Instant visual needle update if spectrum exists
        updateScoutNeedleInPlace();

        clearTimeout(stepDebounceTimer);
        stepDebounceTimer = setTimeout(() => {
            loadAnalysis(currentPeriod, true);
        }, 120);
    }

    function renderHeaderBadges() {
        // Sync all metric dropdowns
        document.querySelectorAll('.periodicity-metric-select').forEach(sel => {
            sel.value = activeMetric;
        });

        if (analysisData) {
            const [startDate, endDate] = getDateRange();
            if (displayDateSpan) displayDateSpan.textContent = `${startDate} → ${endDate}`;
            if (displayObsCount) displayObsCount.textContent = `${analysisData.valid_count.toLocaleString()} Days`;
        }
    }

    function renderTier1Findings(data) {
        if (!tier1CardsContainer || !data || !data.spectrum) return;
        const peaks = data.spectrum.peaks || [];

        // 1. High-Frequency (< 25d)
        const highPeaks = peaks.filter(p => p.period < 25.0);
        const dominantHigh = highPeaks[0];
        const card1 = document.createElement('div');
        card1.className = 'periodicity-finding-card';
        card1.innerHTML = `
            <div>
                <div class="periodicity-finding-head">
                    <span class="periodicity-finding-title">High-Frequency Rhythm (&lt; 25d)</span>
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span class="periodicity-finding-badge ${dominantHigh && dominantHigh.is_sig99 ? 'periodicity-badge-sig' : (dominantHigh ? 'periodicity-badge-mod' : 'periodicity-badge-flat')}">
                            ${dominantHigh ? (dominantHigh.is_sig99 ? 'PROVEN (p < 0.01)' : 'MODERATE (p < 0.05)') : 'NOISE FLOOR'}
                        </span>
                        <div class="periodicity-popover-wrapper">
                            <button type="button" class="periodicity-popover-trigger" aria-label="Tier 1 High-Frequency Guide" title="Guide">
                                <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                                    <path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/>
                                    <path d="M7.002 11a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM7.1 4.995a.905.905 0 1 1 1.8 0l-.35 3.507a.553.553 0 0 1-1.1 0L7.1 4.995z"/>
                                </svg>
                            </button>
                            <div class="periodicity-guide-popover" onclick="event.stopPropagation()">
                                <h4>Tier 1 — High-Frequency Cycles (&lt; 25 Days)</h4>
                                <h5>What this box is for:</h5>
                                <p>This section flags rapid, short-term rhythms that repeat every few days up to three weeks.</p>
                                <h5>How to interpret it:</h5>
                                <ul>
                                    <li><strong>What you are looking for:</strong> Common diabetes equipment patterns, such as a 3-day pump pod change or a 10-to-14-day continuous glucose sensor cycle.</li>
                                    <li><strong>Why it happens:</strong> New sensors often require 24–48 hours to settle, while older sensors can become erratic or drift near the end of their lifespan. Infusion sets may also see insulin absorption decline by day three.</li>
                                    <li><strong>What action to take:</strong> If you see a confirmed cycle that matches your device schedule, inspect Tier 3 to see which days show higher variability. It may highlight a need to adjust insertion routines or avoid making major profile tweaks when a sensor is settling.</li>
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="periodicity-finding-val">
                    ${dominantHigh ? `T = ${dominantHigh.period.toFixed(1)} Days` : 'No Dominant Signal'}
                </div>
                <div class="periodicity-finding-desc">
                    ${dominantHigh 
                        ? `Dominant empirical peak (Power: ${dominantHigh.power.toFixed(1)}). ${highPeaks.length > 1 ? `Secondary peaks found at ${highPeaks.slice(1, 3).map(p => p.period.toFixed(1) + 'd').join(', ')}.` : ''}`
                        : 'Daily fluctuations below 25 days exhibit no repeating periodic cycles.'}
                </div>
            </div>
            <div class="periodicity-finding-foot">Empirical Peak Discovery</div>
        `;

        // 2. Mid-Frequency (25-45d)
        const midPeaks = peaks.filter(p => p.period >= 25.0 && p.period <= 45.0);
        const isMidEmpty = midPeaks.length === 0;
        const card2 = document.createElement('div');
        card2.className = 'periodicity-finding-card';
        card2.innerHTML = `
            <div>
                <div class="periodicity-finding-head">
                    <span class="periodicity-finding-title">Mid-Frequency Zone (25–45d)</span>
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span class="periodicity-finding-badge ${isMidEmpty ? 'periodicity-badge-sig' : 'periodicity-badge-mod'}">
                            ${isMidEmpty ? 'DEBUNKED (Artifact)' : 'WEAK SIGNAL'}
                        </span>
                        <div class="periodicity-popover-wrapper">
                            <button type="button" class="periodicity-popover-trigger" aria-label="Tier 1 Mid-Frequency Guide" title="Guide">
                                <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                                    <path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/>
                                    <path d="M7.002 11a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM7.1 4.995a.905.905 0 1 1 1.8 0l-.35 3.507a.553.553 0 0 1-1.1 0L7.1 4.995z"/>
                                </svg>
                            </button>
                            <div class="periodicity-guide-popover" onclick="event.stopPropagation()">
                                <h4>Tier 1 — Mid-Frequency Zone (25 to 45 Days)</h4>
                                <h5>What this box is for:</h5>
                                <p>This area monitors roughly monthly intervals to test whether your numbers follow an underlying four-to-five-week biological clock.</p>
                                <h5>How to interpret it:</h5>
                                <ul>
                                    <li><strong>The &ldquo;Monthly&rdquo; Illusion:</strong> Standard diabetes charts often use 14-day or 30-day rolling averages. Smoothing data this way can create gentle, undulating waves that look like regular monthly biological cycles, even when none exist (known as a filter artifact).</li>
                                    <li><strong>What raw data shows:</strong> This tool bypasses smoothing filters entirely. If this card reports a flat or quiet signal, any monthly pattern seen on standard trend lines is simply an optical illusion caused by the math behind rolling averages.</li>
                                    <li><strong>What action to take:</strong> If confirmed flat, do not search for monthly lifestyle or hormonal causes—your underlying daily numbers are not repeating on a 30-day rhythm.</li>
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="periodicity-finding-val">
                    ${isMidEmpty ? 'Noise Floor (Flat)' : `Peak at ${midPeaks[0].period.toFixed(1)}d`}
                </div>
                <div class="periodicity-finding-desc">
                    ${isMidEmpty 
                        ? 'Raw data shows zero spectral peak in this band. The ~30-day wave visible on rolling graphs is mathematically confirmed as a Slutsky-Yule filter illusion.' 
                        : `Weak peak with power ${midPeaks[0].power.toFixed(1)}. May represent residual harmonic leakage.`}
                </div>
            </div>
            <div class="periodicity-finding-foot">Filter Distortion Test</div>
        `;

        // 3. Macro Seasonality (> 90d)
        const macroPeaks = peaks.filter(p => p.period >= 90.0);
        const dominantMacro = macroPeaks[0];
        const card3 = document.createElement('div');
        card3.className = 'periodicity-finding-card';
        card3.innerHTML = `
            <div>
                <div class="periodicity-finding-head">
                    <span class="periodicity-finding-title">Macro Seasonality (&gt; 90d)</span>
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span class="periodicity-finding-badge ${dominantMacro && dominantMacro.is_sig99 ? 'periodicity-badge-sig' : (dominantMacro ? 'periodicity-badge-mod' : 'periodicity-badge-flat')}">
                            ${dominantMacro ? (dominantMacro.is_sig99 ? 'PROVEN (p < 0.01)' : 'MODERATE (p < 0.05)') : 'INCONCLUSIVE'}
                        </span>
                        <div class="periodicity-popover-wrapper">
                            <button type="button" class="periodicity-popover-trigger" aria-label="Tier 1 Macro Seasonality Guide" title="Guide">
                                <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                                    <path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/>
                                    <path d="M7.002 11a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM7.1 4.995a.905.905 0 1 1 1.8 0l-.35 3.507a.553.553 0 0 1-1.1 0L7.1 4.995z"/>
                                </svg>
                            </button>
                            <div class="periodicity-guide-popover" onclick="event.stopPropagation()">
                                <h4>Tier 1 — Macro Seasonality (&gt; 90 Days)</h4>
                                <h5>What this box is for:</h5>
                                <p>This card monitors long-term trends spanning months or seasons across your full multi-year history.</p>
                                <h5>How to interpret it:</h5>
                                <ul>
                                    <li><strong>What you are looking for:</strong> Seasonal shifts, especially annual winter increases in insulin resistance or changes during summer holidays.</li>
                                    <li><strong>Why it happens:</strong> Temperature, physical activity levels, illness frequency, and seasonal eating patterns all alter insulin sensitivity across the year.</li>
                                    <li><strong>Understanding the numbers:</strong> An annual pattern often produces twin spikes: one at a full calendar year (365 days) and another half-year echo (around 180 days). This is normal when seasonal changes arrive as a distinct multi-month block rather than a gentle, continuous wave.</li>
                                    <li><strong>What action to take:</strong> A verified annual signal suggests planning seasonal profile adjustments (e.g., increasing basal rates in winter).</li>
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="periodicity-finding-val">
                    ${dominantMacro ? `T ≈ ${dominantMacro.period.toFixed(0)} Days` : 'No Signal'}
                </div>
                <div class="periodicity-finding-desc">
                    ${dominantMacro 
                        ? `Multi-year seasonal wave confirmed. Power peaks at ${dominantMacro.period.toFixed(0)}d (${dominantMacro.period > 300 ? 'full solar year' : 'semi-annual harmonic'}).` 
                        : 'Requires 2+ full years to establish statistically robust low-frequency seasonality.'}
                </div>
            </div>
            <div class="periodicity-finding-foot">Long-Wave Environmental Shift</div>
        `;
        tier1CardsContainer.replaceChildren(card1, card2, card3);
    }

    function renderSpectrumChart(data) {
        if (!spectrumContainer || !window.echarts || !data || !data.spectrum) return;
        if (!spectrumChart) {
            spectrumChart = echarts.init(spectrumContainer, null, { renderer: 'canvas' });
            spectrumChart.on('click', (params) => {
                if (params && params.dataIndex !== undefined && data && data.spectrum) {
                    const pickedPeriod = data.spectrum.periods[params.dataIndex];
                    if (pickedPeriod >= 2.0 && pickedPeriod <= 400.0) {
                        debouncedStep(pickedPeriod);
                    }
                }
            });
        }

        const spec = data.spectrum;
        const periods = spec.periods;
        const powers = spec.powers;
        const z95 = spec.z95;
        const z99 = spec.z99;
        const peaks = spec.peaks || [];

        // Peak jump pills below chart
        if (peakButtonContainer) {
            if (peaks.length === 0) {
                const emptySpan = document.createElement('span');
                emptySpan.style.cssText = 'color: var(--text-tertiary); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.75rem;';
                emptySpan.textContent = 'No peaks exceed 95% threshold';
                peakButtonContainer.replaceChildren(emptySpan);
            } else {
                const buttons = peaks.map(peak => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'periodicity-peak-pill';
                    btn.innerHTML = `<span>T = ${peak.period.toFixed(1)}d</span> <span style="font-size: 0.68rem; color: var(--text-secondary); font-weight: normal;">(P: ${peak.power.toFixed(1)})</span>`;
                    btn.addEventListener('click', () => {
                        debouncedStep(peak.period);
                    });
                    return btn;
                });
                peakButtonContainer.replaceChildren(...buttons);
            }
        }

        // Calculate key logarithmic tick indices (clinical & harmonic milestones)
        const targetTicks = [2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 180, 270, 365, 400];
        const tickIndices = new Set();
        targetTicks.forEach(t => {
            let bestIdx = 0;
            let bestDiff = 999;
            periods.forEach((p, idx) => {
                const diff = Math.abs(p - t);
                if (diff < bestDiff) {
                    bestDiff = diff;
                    bestIdx = idx;
                }
            });
            tickIndices.add(bestIdx);
        });

        // Find closest index for Scout Needle
        let closestIdx = 0;
        let minDiff = 999;
        periods.forEach((p, idx) => {
            const diff = Math.abs(p - currentPeriod);
            if (diff < minDiff) {
                minDiff = diff;
                closestIdx = idx;
            }
        });

        // Ensure Y-axis encompasses all series including 99% FAP ceiling and 95% threshold with generous headroom
        const maxPower = Math.max(...powers, 0);
        const yTop = Math.max(maxPower * 1.15, (z99 || 0) * 1.20, (z95 || 0) * 1.20, 12);

        const option = {
            backgroundColor: 'transparent',
            animation: false,
            grid: {
                left: 55,
                right: 35,
                top: 25,
                bottom: 45
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: '#161b22',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 11, fontFamily: 'ui-monospace, monospace' },
                formatter: (items) => {
                    if (!items || !items.length) return '';
                    const idx = items[0].dataIndex;
                    const p = periods[idx];
                    const pow = powers[idx];
                    return `Period: <strong>${p.toFixed(1)} Days</strong><br/>Lomb-Scargle Power: <strong>${pow.toFixed(2)}</strong>`;
                }
            },
            xAxis: {
                type: 'category',
                data: periods.map(p => p.toFixed(1)),
                name: 'Period in Days (Logarithmic Continuum 2 to 400 Days)',
                nameLocation: 'middle',
                nameGap: 28,
                nameTextStyle: { color: '#8b949e', fontSize: 11 },
                axisLine: { lineStyle: { color: '#30363d' } },
                axisTick: {
                    show: true,
                    interval: (idx) => tickIndices.has(idx),
                    lineStyle: { color: '#30363d' }
                },
                splitLine: {
                    show: true,
                    interval: (idx) => tickIndices.has(idx),
                    lineStyle: { color: '#21262d' }
                },
                axisLabel: {
                    color: '#8b949e',
                    fontSize: 10,
                    fontFamily: 'ui-monospace, monospace',
                    interval: (idx) => tickIndices.has(idx),
                    formatter: (val, idx) => {
                        const p = periods[idx];
                        if (p < 10) return p.toFixed(p % 1 === 0 ? 0 : 1);
                        return String(Math.round(p));
                    }
                }
            },
            yAxis: {
                type: 'value',
                min: 0,
                max: Math.ceil(yTop),
                name: 'Power',
                nameTextStyle: { color: '#8b949e', fontSize: 11 },
                axisLine: { lineStyle: { color: '#30363d' } },
                splitLine: { lineStyle: { color: '#21262d' } },
                axisLabel: { color: '#8b949e', fontSize: 10, fontFamily: 'ui-monospace, monospace' }
            },
            series: [
                {
                    name: 'Raw Spectral Power',
                    type: 'line',
                    data: powers,
                    showSymbol: false,
                    lineStyle: { color: '#34d399', width: 2 },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(52, 211, 153, 0.25)' },
                            { offset: 1, color: 'rgba(52, 211, 153, 0.01)' }
                        ])
                    },
                    markLine: {
                        symbol: 'none',
                        silent: true,
                        data: [
                            {
                                yAxis: z99,
                                lineStyle: { color: '#f43f5e', type: 'dashed', width: 1.5 },
                                label: { formatter: '99% FAP Ceiling (p < 0.01)', position: 'end', color: '#f43f5e', fontSize: 10 }
                            },
                            {
                                yAxis: z95,
                                lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.2 },
                                label: { formatter: '95% FAP (p < 0.05)', position: 'end', color: '#f59e0b', fontSize: 10 }
                            },
                            {
                                xAxis: closestIdx,
                                lineStyle: { color: '#38bdf8', width: 2, type: 'solid' },
                                label: { formatter: `Scout: ${currentPeriod.toFixed(1)}d`, position: 'start', color: '#38bdf8', fontSize: 11, fontWeight: 'bold' }
                            }
                        ]
                    }
                }
            ]
        };

        spectrumChart.setOption(option, true);
    }

    function updateScoutNeedleInPlace() {
        if (!spectrumChart || !analysisData || !analysisData.spectrum) return;
        const periods = analysisData.spectrum.periods;
        const z95 = analysisData.spectrum.z95;
        const z99 = analysisData.spectrum.z99;

        let closestIdx = 0;
        let minDiff = 999;
        periods.forEach((p, idx) => {
            const diff = Math.abs(p - currentPeriod);
            if (diff < minDiff) {
                minDiff = diff;
                closestIdx = idx;
            }
        });

        spectrumChart.setOption({
            series: [{
                markLine: {
                    symbol: 'none',
                    silent: true,
                    data: [
                        { yAxis: z99, lineStyle: { color: '#f43f5e', type: 'dashed', width: 1.5 }, label: { formatter: '99% FAP Ceiling', position: 'end', color: '#f43f5e' } },
                        { yAxis: z95, lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.2 }, label: { formatter: '95% FAP', position: 'end', color: '#f59e0b' } },
                        { xAxis: closestIdx, lineStyle: { color: '#38bdf8', width: 2, type: 'solid' }, label: { formatter: `Scout: ${currentPeriod.toFixed(1)}d`, position: 'start', color: '#38bdf8', fontWeight: 'bold' } }
                    ]
                }
            }]
        });
    }

    function renderPhaseFoldChart(data) {
        if (!data || !data.fold) return;
        const fold = data.fold;
        const tele = data.telemetry || {};
        const cfg = METRIC_CONFIGS[activeMetric] || { label: activeMetric, unit: '' };

        // 1. Update Telemetry Box
        const score = tele.score !== undefined ? tele.score : 10;
        if (resonancePercentLabel) resonancePercentLabel.textContent = `${score}%`;
        if (resonanceProgressBar) resonanceProgressBar.style.width = `${score}%`;

        if (foldedCycleCount) {
            foldedCycleCount.textContent = `${Math.round(fold.cycles || 0)} Complete Cycles Folded`;
        }
        if (foldedSpreadBadge) {
            foldedSpreadBadge.textContent = `${(fold.spread || 0).toFixed(2)} ${cfg.unit}`;
        }

        // Directional Hint
        if (resonanceSlopeHint) {
            if (tele.nearest_peak && tele.distance !== null && tele.distance <= 0.25) {
                resonanceSlopeHint.innerHTML = `<span style="color: #34d399; font-weight: 700;">★ LOCKED ON PEAK: ${tele.nearest_peak.period.toFixed(1)}d (P: ${tele.nearest_peak.power.toFixed(1)})</span>`;
            } else if (tele.nearest_peak && tele.distance !== null) {
                const arrow = tele.gradient > 0 ? '▲ Stepping Right Increases Resonance' : '▼ Stepping Left Increases Resonance';
                resonanceSlopeHint.textContent = `${arrow} (${tele.distance.toFixed(1)}d away from ${tele.nearest_peak.period.toFixed(1)}d peak)`;
            } else {
                resonanceSlopeHint.textContent = 'Flat noise floor';
            }
        }

        // Resonance Lock Pill & Dynamic Color Shifts
        let waveColor = '#8b949e';
        let waveFill = 'rgba(139, 148, 158, 0.1)';

        if (resonanceLockPill) {
            if (tele.is_sig99 || score >= 80) {
                resonanceLockPill.textContent = 'RESONANCE LOCK (p < 0.001)';
                resonanceLockPill.className = 'periodicity-finding-badge periodicity-badge-sig';
                if (resonanceProgressBar) resonanceProgressBar.style.background = '#34d399';
                waveColor = '#34d399';
                waveFill = 'rgba(52, 211, 153, 0.25)';
            } else if (tele.is_sig95 || score >= 45) {
                resonanceLockPill.textContent = 'APPROACHING PEAK (p < 0.05)';
                resonanceLockPill.className = 'periodicity-finding-badge periodicity-badge-mod';
                if (resonanceProgressBar) resonanceProgressBar.style.background = '#38bdf8';
                waveColor = '#38bdf8';
                waveFill = 'rgba(56, 189, 248, 0.2)';
            } else {
                resonanceLockPill.textContent = 'NOISE FLOOR (FLAT)';
                resonanceLockPill.className = 'periodicity-finding-badge periodicity-badge-flat';
                if (resonanceProgressBar) resonanceProgressBar.style.background = '#6e7681';
                waveColor = '#8b949e';
                waveFill = 'rgba(110, 118, 129, 0.1)';
            }
        }

        // Kruskal-Wallis badge
        if (foldedKruskalBadge) {
            const pVal = fold.p_value;
            if (pVal < 0.001) {
                foldedKruskalBadge.textContent = 'p < 0.001 (Highly Significant)';
                foldedKruskalBadge.style.color = '#34d399';
            } else if (pVal < 0.05) {
                foldedKruskalBadge.textContent = `p = ${pVal.toFixed(3)} (Significant)`;
                foldedKruskalBadge.style.color = '#fbbf24';
            } else {
                foldedKruskalBadge.textContent = `p = ${pVal.toFixed(2)} (Insignificant / Flat)`;
                foldedKruskalBadge.style.color = '#8b949e';
            }
        }

        // 2. Render Phase Fold EChart
        const chartElem = document.getElementById('epochFoldChart');
        if (!chartElem || !window.echarts) return;
        if (!epochFoldChart) {
            epochFoldChart = echarts.init(chartElem, null, { renderer: 'canvas' });
        }

        const bins = fold.bins || [];
        const binCount = bins.length;
        const labels = bins.map((_, i) => {
            const startDay = (i / binCount) * currentPeriod;
            const endDay = ((i + 1) / binCount) * currentPeriod;
            return `${startDay.toFixed(1)}–${endDay.toFixed(1)}d`;
        });

        const medians = bins.map(b => b.median);
        const q1s = bins.map(b => b.q1);
        const q3s = bins.map(b => b.q3);
        const spreads = bins.map(b => (b.q3 !== null && b.q1 !== null) ? (b.q3 - b.q1) : null);

        // Calculate dynamic Y-axis range with 20% padding
        const allFoldVals = [];
        bins.forEach(b => {
            if (b.q1 !== null && b.q1 !== undefined) allFoldVals.push(b.q1);
            if (b.median !== null && b.median !== undefined) allFoldVals.push(b.median);
            if (b.q3 !== null && b.q3 !== undefined) allFoldVals.push(b.q3);
        });

        let yMin = 0, yMax = 100;
        if (allFoldVals.length > 0) {
            const rawMin = Math.min(...allFoldVals);
            const rawMax = Math.max(...allFoldVals);
            const span = (rawMax - rawMin) || (Math.abs(rawMin) * 0.1) || 1.0;
            const pad = span * 0.20;
            yMin = Math.max(0, Math.floor((rawMin - pad) * 10) / 10);
            yMax = Math.ceil((rawMax + pad) * 10) / 10;
        }

        const option = {
            backgroundColor: 'transparent',
            animation: false,
            grid: {
                left: 55,
                right: 35,
                top: 25,
                bottom: 45
            },
            tooltip: {
                trigger: 'axis',
                backgroundColor: '#161b22',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 11, fontFamily: 'ui-monospace, monospace' },
                formatter: (items) => {
                    const idx = items[0].dataIndex;
                    const b = bins[idx];
                    if (!b || b.median === null) return `Phase: ${labels[idx]}<br/>No observations in bin`;
                    return `Phase: <strong>${labels[idx]}</strong><br/>` +
                           `Median: <strong>${b.median.toFixed(2)} ${cfg.unit}</strong><br/>` +
                           `IQR: <strong>${b.q1.toFixed(2)} → ${b.q3.toFixed(2)}</strong><br/>` +
                           `Obs: ${b.count || 0}`;
                }
            },
            xAxis: {
                type: 'category',
                data: labels,
                name: `Phase Bin across ${currentPeriod.toFixed(1)}-Day Cycle`,
                nameLocation: 'middle',
                nameGap: 26,
                nameTextStyle: { color: '#8b949e', fontSize: 11 },
                axisLine: { lineStyle: { color: '#30363d' } },
                splitLine: { show: true, lineStyle: { color: '#21262d' } },
                axisLabel: { color: '#8b949e', fontSize: 10, fontFamily: 'ui-monospace, monospace' }
            },
            yAxis: {
                type: 'value',
                scale: true,
                min: yMin,
                max: yMax,
                name: cfg.label,
                nameTextStyle: { color: '#8b949e', fontSize: 11 },
                axisLine: { lineStyle: { color: '#30363d' } },
                splitLine: { lineStyle: { color: '#21262d' } },
                axisLabel: { color: '#8b949e', fontSize: 10, fontFamily: 'ui-monospace, monospace' }
            },
            series: [
                // Base IQR series (lower bound) - stacked and invisible
                {
                    name: 'IQR Lower (Q1)',
                    type: 'line',
                    data: q1s,
                    stack: 'iqr',
                    showSymbol: false,
                    lineStyle: { opacity: 0 },
                    silent: true
                },
                // Spread IQR series (upper - lower) - stacked and shaded
                {
                    name: 'IQR Ribbon (25th–75th)',
                    type: 'line',
                    data: spreads,
                    stack: 'iqr',
                    showSymbol: false,
                    lineStyle: { color: waveColor, width: 1, type: 'dashed' },
                    areaStyle: { color: waveFill },
                    silent: true
                },
                // Median wave line
                {
                    name: `Phase Median (${cfg.label})`,
                    type: 'line',
                    data: medians,
                    symbol: 'circle',
                    symbolSize: 6,
                    lineStyle: { color: waveColor, width: 3 },
                    itemStyle: { color: waveColor }
                }
            ]
        };

        epochFoldChart.setOption(option, true);
    }

    function renderCalendarRaster(data) {
        if (!rasterCanvas || !data || !data.records) return;
        const records = data.records;
        const metricKey = data.metric || activeMetric;
        const cfg = METRIC_CONFIGS[metricKey] || { unit: '' };

        const lo = data.raster ? data.raster.p3 : 0;
        const hi = data.raster ? data.raster.p97 : 100;
        const denom = (hi - lo) > 1e-6 ? (hi - lo) : 1.0;

        if (rasterMinLabel) rasterMinLabel.textContent = `${lo.toFixed(1)}${cfg.unit}`;
        if (rasterMaxLabel) rasterMaxLabel.textContent = `${hi.toFixed(1)}${cfg.unit}`;

        updateLegendGradient(rasterSlantValue, metricKey);

        // Group records by year and day-of-year
        const yearMap = {};
        records.forEach(r => {
            const v = r[metricKey];
            const dParts = String(r.date).split('-').map(Number);
            if (dParts.length !== 3) return;
            const yr = dParts[0];
            const dateObj = new Date(Date.UTC(dParts[0], dParts[1] - 1, dParts[2]));
            const startOfYear = new Date(Date.UTC(yr, 0, 1));
            const dayOfYear = Math.floor((dateObj - startOfYear) / (86400000)) + 1;

            if (!yearMap[yr]) yearMap[yr] = {};
            yearMap[yr][dayOfYear] = (v !== null && v !== undefined) ? Number(v) : null;
        });

        const years = Object.keys(yearMap).map(Number).sort((a, b) => a - b);
        if (years.length === 0) return;

        const canvas = rasterCanvas;
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = 190 * dpr;
        ctx.scale(dpr, dpr);

        const width = rect.width;
        const height = 190;
        ctx.clearRect(0, 0, width, height);

        const labelWidth = 55;
        const cellAreaWidth = width - labelWidth - 10;
        const cellWidth = cellAreaWidth / 366;
        const rowHeight = (height - 30) / Math.max(1, years.length);

        years.forEach((yr, rIdx) => {
            const y = 10 + rIdx * rowHeight;

            ctx.fillStyle = '#8b949e';
            ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
            ctx.textAlign = 'right';
            ctx.fillText(`${yr}`, labelWidth - 10, y + rowHeight * 0.7);

            for (let day = 1; day <= 366; day++) {
                const val = yearMap[yr][day];
                const x = labelWidth + (day - 1) * cellWidth;

                if (val !== null && val !== undefined) {
                    const norm = Math.max(0, Math.min(1, (val - lo) / denom));
                    ctx.fillStyle = getHeatmapColor(norm, metricKey, rasterSlantValue);
                    ctx.fillRect(x, y, Math.max(1, cellWidth), rowHeight - 2);
                } else {
                    // Distinct missing-day slate cell
                    ctx.fillStyle = '#161b22';
                    ctx.fillRect(x, y, Math.max(1, cellWidth), rowHeight - 2);
                }
            }
        });

        // Month labels along the bottom with winter highlighting
        const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        const monthStarts = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335];

        monthStarts.forEach((day, idx) => {
            const x = labelWidth + (day - 1) * cellWidth;
            if (idx >= 5 && idx <= 7) {
                ctx.fillStyle = '#fbbf24'; // Southern Hemisphere Winter
                ctx.font = 'bold 10px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
            } else {
                ctx.fillStyle = '#8b949e';
                ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
            }
            ctx.textAlign = 'left';
            ctx.fillText(monthNames[idx], x, height - 6);
        });
    }

    function updateLegendGradient(slant, metric) {
        const slider = document.getElementById('raster-slant-slider') || document.querySelector('.periodicity-heat-legend-bar');
        if (!slider) return;
        const active = metric || activeMetric;
        const isHigherBetter = (active === 'tir' || active === 'titr');
        const s = (slant !== undefined && slant !== null) ? slant : rasterSlantValue;
        const gamma = Math.pow(2, s);
        const invGamma = 1.0 / gamma;

        // Base color stops
        const stops = isHigherBetter
            ? [
                { p: 0.00, color: '#ef4444' },
                { p: 0.18, color: '#f97316' },
                { p: 0.45, color: '#eab308' },
                { p: 0.60, color: '#84cc16' },
                { p: 0.78, color: '#10b981' },
                { p: 1.00, color: '#065f46' }
            ]
            : [
                { p: 0.00, color: '#065f46' },
                { p: 0.22, color: '#10b981' },
                { p: 0.40, color: '#84cc16' },
                { p: 0.55, color: '#eab308' },
                { p: 0.82, color: '#f97316' },
                { p: 1.00, color: '#ef4444' }
            ];

        const gradStops = stops.map(stop => {
            const shiftedP = Math.min(100, Math.max(0, Math.pow(stop.p, invGamma) * 100));
            return `${stop.color} ${shiftedP.toFixed(1)}%`;
        }).join(', ');

        slider.style.background = `linear-gradient(to right, ${gradStops})`;
    }

    function getHeatmapColor(t, metric, slant) {
        const active = metric || activeMetric;
        // When number is low: green for CV, TDD, MeanBG, SD. Green when high for TIR, TITR.
        const isHigherBetter = (active === 'tir' || active === 'titr');
        const s = (slant !== undefined && slant !== null) ? slant : rasterSlantValue;

        // Apply gamma slant to normalized value t (0 to 1)
        const gamma = Math.pow(2, s);
        const tPrime = Math.max(0, Math.min(1, Math.pow(Math.max(0, Math.min(1, t)), gamma)));

        // u ranges from 0 (best / dark pine green) to 1 (worst / red)
        const u = isHigherBetter ? (1.0 - tPrime) : tPrime;
        const clampedU = Math.max(0, Math.min(1, u));

        // 6 color stops with adjusted thresholds:
        // 0.00: Deep Forest Green rgb(6, 95, 70)
        // 0.22: Vibrant Emerald Green rgb(16, 185, 129)
        // 0.40: Crisp Lime Green rgb(132, 204, 22)
        // 0.55: Warm Amber-Yellow rgb(234, 179, 8)
        // 0.82: Vivid Orange rgb(249, 115, 22)
        // 1.00: Red rgb(239, 68, 68)

        if (clampedU < 0.22) {
            const f = clampedU / 0.22;
            const r = Math.round(6 + f * (16 - 6));
            const g = Math.round(95 + f * (185 - 95));
            const b = Math.round(70 + f * (129 - 70));
            return `rgb(${r}, ${g}, ${b})`;
        } else if (clampedU < 0.40) {
            const f = (clampedU - 0.22) / 0.18;
            const r = Math.round(16 + f * (132 - 16));
            const g = Math.round(185 + f * (204 - 185));
            const b = Math.round(129 + f * (22 - 129));
            return `rgb(${r}, ${g}, ${b})`;
        } else if (clampedU < 0.55) {
            const f = (clampedU - 0.40) / 0.15;
            const r = Math.round(132 + f * (234 - 132));
            const g = Math.round(204 + f * (179 - 204));
            const b = Math.round(22 + f * (8 - 22));
            return `rgb(${r}, ${g}, ${b})`;
        } else if (clampedU < 0.82) {
            const f = (clampedU - 0.55) / 0.27;
            const r = Math.round(234 + f * (249 - 234));
            const g = Math.round(179 + f * (115 - 179));
            const b = Math.round(8 + f * (22 - 8));
            return `rgb(${r}, ${g}, ${b})`;
        } else {
            const f = (clampedU - 0.82) / 0.18;
            const r = Math.round(249 + f * (239 - 249));
            const g = Math.round(115 + f * (68 - 115));
            const b = Math.round(22 + f * (68 - 22));
            return `rgb(${r}, ${g}, ${b})`;
        }
    }

    // Event Wire-up
    function initEvents() {
        if (metricBtnGroup) {
            metricBtnGroup.addEventListener('click', (e) => {
                const btn = e.target.closest('.periodicity-metric-btn');
                if (!btn) return;
                setMetric(btn.dataset.metric);
            });
        }

        // Wire change listener across all interlinked metric dropdowns
        document.querySelectorAll('.periodicity-metric-select').forEach(sel => {
            sel.addEventListener('change', (e) => {
                setMetric(e.target.value);
            });
        });

        if (linearDetrendCheck) {
            linearDetrendCheck.addEventListener('change', (e) => {
                isDetrended = e.target.checked;
                loadAnalysis(currentPeriod);
            });
        }

        if (stepMinus1) stepMinus1.addEventListener('click', () => debouncedStep(currentPeriod - 1.0));
        if (stepMinus01) stepMinus01.addEventListener('click', () => debouncedStep(currentPeriod - 0.1));
        if (stepPlus01) stepPlus01.addEventListener('click', () => debouncedStep(currentPeriod + 0.1));
        if (stepPlus1) stepPlus1.addEventListener('click', () => debouncedStep(currentPeriod + 1.0));

        if (snapNearestPeakBtn) {
            snapNearestPeakBtn.addEventListener('click', () => {
                if (!analysisData || !analysisData.spectrum || !analysisData.spectrum.peaks) return;
                const peaks = analysisData.spectrum.peaks;
                if (peaks.length === 0) return;
                let nearest = peaks[0];
                let minDiff = Math.abs(currentPeriod - nearest.period);
                peaks.forEach(p => {
                    const diff = Math.abs(currentPeriod - p.period);
                    if (diff < minDiff) {
                        minDiff = diff;
                        nearest = p;
                    }
                });
                debouncedStep(nearest.period);
            });
        }

        if (periodicityBand) {
            periodicityBand.addEventListener('change', () => {
                updateScrubberLimits();
                if (periodScrubber) {
                    periodScrubber.value = currentPeriod;
                }
            });
        }

        if (periodScrubber) {
            periodScrubber.addEventListener('input', (e) => {
                const val = parseFloat(e.target.value);
                if (!isNaN(val)) debouncedStep(val);
            });
        }

        if (applyManualPeriodBtn && manualPeriodInput) {
            applyManualPeriodBtn.addEventListener('click', () => {
                const val = parseFloat(manualPeriodInput.value);
                if (!isNaN(val) && val >= 2.0 && val <= 400.0) {
                    debouncedStep(val);
                }
            });
        }

        // Global Arrow Key Stepping
        window.addEventListener('keydown', (e) => {
            const el = document.activeElement;
            if (el && (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) || el.isContentEditable)) {
                // If focused inside scrubber range slider, allow native navigation
                if (el === periodScrubber) return;
                return;
            }

            if (e.key === 'ArrowLeft') {
                e.preventDefault();
                debouncedStep(currentPeriod - (e.shiftKey ? 1.0 : 0.1));
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                debouncedStep(currentPeriod + (e.shiftKey ? 1.0 : 0.1));
            }
        });

        // Date selector integration
        window.addEventListener('ns-date-change', () => {
            loadAnalysis(currentPeriod);
        });

        // Resize handling
        window.addEventListener('resize', () => {
            if (spectrumChart) spectrumChart.resize();
            if (epochFoldChart) epochFoldChart.resize();
            if (analysisData) renderCalendarRaster(analysisData);
        });

        // Heatmap Colour Slant Slider
        const slantSlider = document.getElementById('raster-slant-slider');
        const slantLabel = document.getElementById('rasterSlantLabel');

        function updateSlant(val) {
            rasterSlantValue = Math.max(-1.0, Math.min(1.0, Math.round(val * 100) / 100));
            if (slantSlider && parseFloat(slantSlider.value) !== rasterSlantValue) {
                slantSlider.value = String(rasterSlantValue);
            }
            if (slantLabel) {
                slantLabel.textContent = `${rasterSlantValue > 0 ? '+' : ''}${rasterSlantValue.toFixed(2)}`;
            }
            if (slantSlider) {
                const titleText = rasterSlantValue === 0.0
                    ? 'Palette Balance: Neutral (0.00) — Drag to shift dominance, double-click to reset'
                    : `Palette Balance: ${rasterSlantValue > 0 ? '+' : ''}${rasterSlantValue.toFixed(2)} — Double-click to reset`;
                slantSlider.title = titleText;
            }
            updateLegendGradient(rasterSlantValue);
            if (analysisData) {
                renderCalendarRaster(analysisData);
            }
        }

        if (slantSlider) {
            slantSlider.addEventListener('input', (e) => {
                const val = parseFloat(e.target.value) || 0.0;
                updateSlant(val);
            });

            slantSlider.addEventListener('dblclick', () => {
                updateSlant(0.0);
            });
        }

        if (slantLabel) {
            slantLabel.addEventListener('click', () => {
                updateSlant(0.0);
            });
        }

        initPopovers();
    }

    function initPopovers() {
        document.addEventListener('click', (e) => {
            const trigger = e.target.closest('.periodicity-popover-trigger');
            if (trigger) {
                e.stopPropagation();
                const wrapper = trigger.closest('.periodicity-popover-wrapper');
                const popover = wrapper ? wrapper.querySelector('.periodicity-guide-popover') : null;
                const isCurrentlyActive = popover && popover.classList.contains('active');

                // Close all popovers first
                document.querySelectorAll('.periodicity-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.periodicity-popover-trigger.active').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.periodicity-popover-wrapper.active').forEach(w => w.classList.remove('active'));

                // If it was not active, open it
                if (popover && !isCurrentlyActive) {
                    popover.classList.add('active');
                    trigger.classList.add('active');
                    if (wrapper) wrapper.classList.add('active');
                }
                return;
            }

            // Clicked inside an open popover? Don't close
            if (e.target.closest('.periodicity-guide-popover')) {
                return;
            }

            // Clicked outside: close all
            document.querySelectorAll('.periodicity-guide-popover.active').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.periodicity-popover-trigger.active').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.periodicity-popover-wrapper.active').forEach(w => w.classList.remove('active'));
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                document.querySelectorAll('.periodicity-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.periodicity-popover-trigger.active').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.periodicity-popover-wrapper.active').forEach(w => w.classList.remove('active'));
            }
        });
    }

    // Auto-boot
    document.addEventListener('DOMContentLoaded', () => {
        initEvents();
        updateScrubberLimits();
        loadAnalysis(14.0);
    });

})();
