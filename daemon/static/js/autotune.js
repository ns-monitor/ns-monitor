/**
 * Autotune Client-Side Orchestrator
 * Handles granular day selection, ECharts rendering, API calls, and (i) help popovers.
 */

(function () {
    let basalChart = null;
    let availableDays = [];
    let availableProfiles = {};
    let currentReport = null;

    // -------------------------------------------------------------
    // 1. STRATEGIC (i) HELP POPOVERS (Matching Graph-it-All pattern)
    // -------------------------------------------------------------
    function initHelpPopovers() {
        document.querySelectorAll('.at-popover-trigger').forEach(trigger => {
            trigger.addEventListener('click', function (e) {
                e.stopPropagation();
                const popover = this.parentElement.querySelector('.at-guide-popover');
                const wasActive = popover.classList.contains('active');

                // Close all other popovers
                document.querySelectorAll('.at-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.at-popover-trigger.active').forEach(t => t.classList.remove('active'));

                if (!wasActive) {
                    popover.classList.add('active');
                    this.classList.add('active');
                }
            });
        });

        // Close when clicking outside
        document.addEventListener('click', function (e) {
            if (!e.target.closest('.at-popover-wrapper')) {
                document.querySelectorAll('.at-guide-popover.active').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.at-popover-trigger.active').forEach(t => t.classList.remove('active'));
            }
        });
    }

    // -------------------------------------------------------------
    // 2. DAY SELECTION & PRESETS
    // -------------------------------------------------------------
    async function loadAvailableDays(startDate, endDate) {
        const grid = document.getElementById('day-grid');
        if (!grid) return;
        grid.innerHTML = '<div style="grid-column: 1/-1; padding: 20px; color: var(--text-secondary); text-align: center;"><i class="fa-solid fa-spinner fa-spin"></i> Inspecting 04:00-to-04:00 day windows...</div>';

        try {
            const resp = await fetch(`/api/v1/autotune/days?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            availableDays = data.days || [];
            renderDayGrid();
        } catch (e) {
            console.error('Failed to load autotune days:', e);
            grid.innerHTML = `<div style="grid-column: 1/-1; padding: 20px; color: var(--danger); text-align: center;">Error loading day windows: ${e.message}</div>`;
        }
    }

    function renderDayGrid() {
        const grid = document.getElementById('day-grid');
        if (!grid) return;

        if (availableDays.length === 0) {
            grid.innerHTML = '<div style="grid-column: 1/-1; padding: 20px; color: var(--text-secondary); text-align: center;">No completed 04:00-to-04:00 windows found in the selected date range.</div>';
            updateSelectedBadge();
            return;
        }

        grid.innerHTML = '';
        availableDays.forEach(day => {
            const label = document.createElement('label');
            const isChecked = day.recommended_default;
            label.className = `day-card ${isChecked ? 'selected' : ''}`;
            label.dataset.date = day.date_str;

            let pillHtml = '';
            if (day.is_clean) {
                pillHtml = `<span class="day-stat-pill badge-clean"><i class="fa-solid fa-check"></i> ${day.cgm_points} pts (${day.coverage_pct}%)</span>`;
            } else if (day.has_gaps) {
                pillHtml = `<span class="day-stat-pill badge-warn"><i class="fa-solid fa-triangle-exclamation"></i> ${day.cgm_points} pts (Gap: ${day.max_gap_min}m)</span>`;
            } else if (day.low_readings_count > 0) {
                pillHtml = `<span class="day-stat-pill badge-warn"><i class="fa-solid fa-triangle-exclamation"></i> ${day.cgm_points} pts (${day.low_readings_count} low)</span>`;
            } else {
                pillHtml = `<span class="day-stat-pill badge-warn"><i class="fa-solid fa-circle-exclamation"></i> ${day.cgm_points} pts (${day.coverage_pct}%)</span>`;
            }

            label.innerHTML = `
                <input type="checkbox" ${isChecked ? 'checked' : ''} data-date="${day.date_str}">
                <div class="day-info">
                    <span class="day-title">${day.display_title}</span>
                    <span class="day-span">${day.window_span}</span>
                    ${pillHtml}
                </div>
            `;

            const cb = label.querySelector('input');
            cb.addEventListener('change', () => {
                if (cb.checked) label.classList.add('selected');
                else label.classList.remove('selected');
                updateSelectedBadge();
            });

            grid.appendChild(label);
        });

        updateSelectedBadge();
    }

    function updateSelectedBadge() {
        const checked = document.querySelectorAll('#day-grid input[type="checkbox"]:checked');
        const badge = document.getElementById('selected-count-badge');
        if (badge) {
            badge.innerText = `${checked.length} Days Selected`;
        }
    }

    window.atPresetDays = function (type) {
        const cards = document.querySelectorAll('#day-grid .day-card');
        const total = cards.length;

        cards.forEach((card, idx) => {
            const cb = card.querySelector('input[type="checkbox"]');
            const dateStr = card.dataset.date;
            const dayObj = availableDays.find(d => d.date_str === dateStr) || {};

            let shouldCheck = false;
            if (type === 'all') {
                shouldCheck = true;
            } else if (type === 'default') {
                shouldCheck = (idx >= Math.max(0, total - 5)); // Last 5 days
            } else if (type === 'weekdays') {
                shouldCheck = !['Sat', 'Sun'].includes(dayObj.day_name);
            } else if (type === 'weekends') {
                shouldCheck = ['Sat', 'Sun'].includes(dayObj.day_name);
            } else if (type === 'clean') {
                shouldCheck = dayObj.is_clean;
            }

            cb.checked = shouldCheck;
            if (shouldCheck) card.classList.add('selected');
            else card.classList.remove('selected');
        });

        updateSelectedBadge();
    };

    // -------------------------------------------------------------
    // 3. BASE PROFILES LOADER & SCORECARD BASELINE PRE-POPULATION
    // -------------------------------------------------------------
    async function loadBaseProfiles() {
        const sel = document.getElementById('at-profile-select');
        if (!sel) return;

        try {
            const resp = await fetch('/api/v1/autotune/profiles');
            if (resp.ok) {
                const data = await resp.json();
                if (data.profiles && data.profiles.length > 0) {
                    sel.innerHTML = '';
                    availableProfiles = {};
                    data.profiles.forEach((p, idx) => {
                        availableProfiles[p.name] = p;
                        const opt = document.createElement('option');
                        opt.value = p.name;
                        opt.textContent = `${p.label} [DIA ${p.dia}h, ISF: ${p.isf_summary}, IC: ${p.ic_summary}]`;
                        if (idx === 0) opt.selected = true;
                        sel.appendChild(opt);
                    });

                    // Pre-populate scorecard with selected profile's baseline
                    applyBaselineToScorecard(sel.value);
                }
            }
        } catch (e) {
            console.warn('Could not load profile eras:', e);
        }
    }

    function applyBaselineToScorecard(profileName) {
        const p = availableProfiles[profileName];
        if (!p) return;

        const basalEl = document.getElementById('stat-basal-base');
        const isfEl = document.getElementById('stat-isf-base');
        const crEl = document.getElementById('stat-cr-base');
        const csfEl = document.getElementById('stat-csf-base');

        if (basalEl) {
            basalEl.innerText = `${p.basal_sum.toFixed(2)} U`;
            basalEl.classList.remove('has-tuned');
        }
        if (isfEl) {
            isfEl.innerText = p.isf_mmol.toFixed(2);
            isfEl.classList.remove('has-tuned');
        }
        if (crEl) {
            crEl.innerText = p.carb_ratio.toFixed(2);
            crEl.classList.remove('has-tuned');
        }
        if (csfEl) {
            csfEl.innerText = p.csf_mmol.toFixed(3);
            csfEl.classList.remove('has-tuned');
        }

        // Hide arrows and tuned results until calculation runs
        ['arrow-basal', 'arrow-isf', 'arrow-cr', 'arrow-csf'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
        ['stat-basal-tuned', 'stat-isf-tuned', 'stat-cr-tuned', 'stat-csf-tuned'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });

        // Reset delta messages to clean initial state
        const dBasal = document.getElementById('delta-basal-text');
        if (dBasal) dBasal.innerText = 'Baseline Profile • Click Run to calculate';
        const dIsf = document.getElementById('delta-isf-text');
        if (dIsf) dIsf.innerText = 'Baseline Profile • Awaiting execution';
        const dCr = document.getElementById('delta-cr-text');
        if (dCr) dCr.innerText = 'Baseline Profile • Awaiting execution';
        const dCsf = document.getElementById('delta-csf-text');
        if (dCsf) dCsf.innerText = 'Diagnostic ratio • Reporting only';

        ['delta-basal-container', 'delta-isf-container', 'delta-cr-container', 'delta-csf-container'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.className = 'metric-delta delta-neutral';
        });
    }

    // -------------------------------------------------------------
    // 4. RUN AUTOTUNE
    // -------------------------------------------------------------
    window.atRunAutotune = async function () {
        const checked = Array.from(document.querySelectorAll('#day-grid input[type="checkbox"]:checked')).map(cb => cb.dataset.date);
        if (checked.length === 0) {
            alert('Please select at least one day window to run Autotune.');
            return;
        }

        const runBtn = document.getElementById('at-run-btn');
        const origHtml = runBtn.innerHTML;
        runBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Tuning...';
        runBtn.disabled = true;

        const payload = {
            selected_days: checked,
            base_profile_name: document.getElementById('at-profile-select')?.value,
            autosens_min: parseFloat(document.getElementById('at-min-factor')?.value || '0.70'),
            autosens_max: parseFloat(document.getElementById('at-max-factor')?.value || '1.20'),
            min_5m_carb_impact: parseFloat(document.getElementById('at-carb-impact')?.value || '3.0')
        };

        try {
            const resp = await fetch('/api/v1/autotune/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.error || `HTTP ${resp.status}`);
            }

            const report = await resp.json();
            currentReport = report;
            renderReport(report);

        } catch (e) {
            alert(`Autotune Error: ${e.message}`);
        } finally {
            runBtn.innerHTML = origHtml;
            runBtn.disabled = false;
        }
    };

    // -------------------------------------------------------------
    // 5. RENDER REPORT (Scorecard, ECharts, Table, Diff)
    // -------------------------------------------------------------
    function renderReport(report) {
        const sum = report.summary;

        // 1. Scorecard Cards (Respecting mmol/L)
        const basalEl = document.getElementById('stat-basal-base');
        const isfEl = document.getElementById('stat-isf-base');
        const crEl = document.getElementById('stat-cr-base');
        const csfEl = document.getElementById('stat-csf-base');

        if (basalEl) {
            basalEl.innerText = `${sum.basal.baseline.toFixed(2)} U`;
            basalEl.classList.add('has-tuned');
        }
        if (isfEl) {
            isfEl.innerText = sum.isf.baseline_mmol.toFixed(2);
            isfEl.classList.add('has-tuned');
        }
        if (crEl) {
            crEl.innerText = sum.carb_ratio.baseline.toFixed(2);
            crEl.classList.add('has-tuned');
        }
        if (csfEl) {
            csfEl.innerText = sum.csf.baseline_mmol.toFixed(3);
            csfEl.classList.add('has-tuned');
        }

        // Show arrows & tuned values
        ['arrow-basal', 'arrow-isf', 'arrow-cr', 'arrow-csf'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'inline';
        });
        ['stat-basal-tuned', 'stat-isf-tuned', 'stat-cr-tuned', 'stat-csf-tuned'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'inline';
        });

        document.getElementById('stat-basal-tuned').innerText = `${sum.basal.tuned.toFixed(2)} U`;
        document.getElementById('delta-basal-text').innerText = `${sum.basal.delta >= 0 ? '+' : ''}${sum.basal.delta.toFixed(2)} U (${sum.basal.pct_change >= 0 ? '+' : ''}${sum.basal.pct_change.toFixed(1)}%) • Within caps`;
        const dBasalBox = document.getElementById('delta-basal-container');
        if (dBasalBox) dBasalBox.className = `metric-delta ${sum.basal.delta >= 0 ? 'delta-positive' : 'delta-negative'}`;

        document.getElementById('stat-isf-tuned').innerText = sum.isf.tuned_mmol.toFixed(2);
        document.getElementById('delta-isf-text').innerText = `${sum.isf.delta_mmol >= 0 ? '+' : ''}${sum.isf.delta_mmol.toFixed(2)} (${sum.isf.pct_change >= 0 ? '+' : ''}${sum.isf.pct_change.toFixed(1)}%) • Damped`;
        const dIsfBox = document.getElementById('delta-isf-container');
        if (dIsfBox) dIsfBox.className = `metric-delta ${sum.isf.delta_mmol <= 0 ? 'delta-positive' : 'delta-negative'}`;

        document.getElementById('stat-cr-tuned').innerText = sum.carb_ratio.tuned.toFixed(2);
        document.getElementById('delta-cr-text').innerText = `${sum.carb_ratio.delta >= 0 ? '+' : ''}${sum.carb_ratio.delta.toFixed(2)} (${sum.carb_ratio.pct_change >= 0 ? '+' : ''}${sum.carb_ratio.pct_change.toFixed(1)}%)`;
        const dCrBox = document.getElementById('delta-cr-container');
        if (dCrBox) dCrBox.className = 'metric-delta delta-neutral';

        document.getElementById('stat-csf-tuned').innerText = sum.csf.tuned_mmol.toFixed(3);
        const dCsfBox = document.getElementById('delta-csf-container');
        if (dCsfBox) dCsfBox.className = 'metric-delta delta-neutral';

        // 2. Render ECharts 24h Basal Profile
        renderBasalChart(report.hourly);

        // 3. Render Daily Progression Table
        renderProgressionTable(report.progression);

        // 4. Render Tabs (Hourly Table, JSON Diff, Markdown Report, Query Archive)
        renderHourlyTable(report.hourly);
        document.getElementById('at-diff-json').textContent = JSON.stringify({
            baseline_profile: report.baseline_profile,
            tuned_profile: report.tuned_profile
        }, null, 2);
        document.getElementById('at-diff-markdown').textContent = report.markdown_report;
        document.getElementById('at-diff-bounds').textContent = JSON.stringify(report.query_archive, null, 2);
    }

    function renderBasalChart(hourlyData) {
        if (!basalChart) {
            const chartDom = document.getElementById('basal-chart');
            if (!chartDom) return;
            basalChart = echarts.init(chartDom);
        }

        const hours = hourlyData.map(h => h.time_str);
        const baselineRates = hourlyData.map(h => h.pump_rate);
        const tunedRates = hourlyData.map(h => h.tuned_rate);
        const minCaps = hourlyData.map(h => h.min_cap);
        const bandSpan = hourlyData.map(h => +(h.max_cap - h.min_cap).toFixed(3));
        const deviationsMmol = hourlyData.map(h => h.deviation_mmol);

        const option = {
            backgroundColor: 'transparent',
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(22, 27, 34, 0.95)',
                borderColor: '#30363d',
                textStyle: { color: '#e6edf3', fontSize: 12 },
                formatter: function (params) {
                    const hIdx = params[0].dataIndex;
                    const h = hourlyData[hIdx];
                    let s = `<div style="font-weight: 600; margin-bottom: 4px;">Hour ${h.time_str}</div>`;
                    s += `<div>Baseline: <strong>${h.pump_rate.toFixed(2)} U/h</strong></div>`;
                    s += `<div>Tuned: <strong style="color: #2ecc71;">${h.tuned_rate.toFixed(2)} U/h</strong> (${h.delta_rate >= 0 ? '+' : ''}${h.delta_rate.toFixed(2)})</div>`;
                    s += `<div>Safety Bounds: [${h.min_cap.toFixed(2)}, ${h.max_cap.toFixed(2)}] ${h.capped ? '<span style="color:#f39c12;">(Capped)</span>' : ''}</div>`;
                    s += `<div style="margin-top: 4px; border-top: 1px solid #30363d; padding-top: 4px;">Net Hourly Dev: <strong>${h.deviation_mmol >= 0 ? '+' : ''}${h.deviation_mmol.toFixed(2)} mmol/L</strong></div>`;
                    return s;
                }
            },
            legend: {
                data: ['Baseline Pump Basal', 'Recommended Tuned Basal', 'Hourly BG Deviation (mmol/L)'],
                textStyle: { color: '#8b949e', fontSize: 12 },
                top: 0
            },
            grid: [
                { left: '50px', right: '30px', top: '40px', height: '60%' },
                { left: '50px', right: '30px', top: '75%', height: '18%' }
            ],
            xAxis: [
                {
                    type: 'category',
                    data: hours,
                    gridIndex: 0,
                    axisLine: { lineStyle: { color: '#30363d' } },
                    axisLabel: { color: '#8b949e', fontSize: 11 },
                    splitLine: { show: true, lineStyle: { color: 'rgba(255,255,255,0.04)' } }
                },
                {
                    type: 'category',
                    data: hours,
                    gridIndex: 1,
                    axisLine: { lineStyle: { color: '#30363d' } },
                    axisLabel: { show: false }
                }
            ],
            yAxis: [
                {
                    type: 'value',
                    name: 'Rate (U/h)',
                    nameTextStyle: { color: '#8b949e', fontSize: 11 },
                    gridIndex: 0,
                    axisLine: { show: false },
                    axisLabel: { color: '#8b949e', fontSize: 11 },
                    splitLine: { lineStyle: { color: '#21262d' } }
                },
                {
                    type: 'value',
                    name: 'Dev (mmol/L)',
                    nameTextStyle: { color: '#8b949e', fontSize: 10 },
                    gridIndex: 1,
                    axisLine: { show: false },
                    axisLabel: { color: '#8b949e', fontSize: 10 },
                    splitLine: { show: false }
                }
            ],
            series: [
                {
                    name: 'Baseline Pump Basal',
                    type: 'line',
                    step: 'start',
                    data: baselineRates,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    itemStyle: { color: '#2f81f7' },
                    lineStyle: { width: 2.5 }
                },
                {
                    name: 'Recommended Tuned Basal',
                    type: 'line',
                    step: 'start',
                    data: tunedRates,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    itemStyle: { color: '#2ecc71' },
                    lineStyle: { width: 2.5, type: 'dashed' }
                },
                {
                    name: 'Safety Base',
                    type: 'line',
                    step: 'start',
                    data: minCaps,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    lineStyle: { opacity: 0 },
                    stack: 'safety-band',
                    symbol: 'none'
                },
                {
                    name: 'Safety Corridor',
                    type: 'line',
                    step: 'start',
                    data: bandSpan,
                    xAxisIndex: 0,
                    yAxisIndex: 0,
                    lineStyle: { opacity: 0 },
                    areaStyle: { color: 'rgba(255, 255, 255, 0.04)' },
                    stack: 'safety-band',
                    symbol: 'none'
                },
                {
                    name: 'Hourly BG Deviation (mmol/L)',
                    type: 'bar',
                    data: deviationsMmol,
                    xAxisIndex: 1,
                    yAxisIndex: 1,
                    itemStyle: {
                        color: function (param) {
                            return param.value >= 0 ? 'rgba(47, 129, 247, 0.65)' : 'rgba(248, 81, 73, 0.65)';
                        },
                        borderRadius: [2, 2, 0, 0]
                    }
                }
            ]
        };

        basalChart.setOption(option);
    }

    function renderProgressionTable(progression) {
        const tbody = document.getElementById('progression-table-body');
        if (!tbody) return;
        tbody.innerHTML = '';

        progression.forEach((p, idx) => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><strong>Day ${idx + 1}:</strong> ${p.date_str}</td>
                <td class="tabular-num">${p.total_points} (${p.cgm_coverage_pct}%)</td>
                <td class="tabular-num">${p.meal_points}</td>
                <td class="tabular-num">${p.uam_points}</td>
                <td class="tabular-num">${p.basal_points}</td>
                <td class="tabular-num">${p.isf_points}</td>
                <td class="tabular-num" style="font-weight: 600;">${p.tuned_basal_total.toFixed(2)} U</td>
                <td class="tabular-num" style="font-weight: 600;">${p.tuned_isf_mmol.toFixed(2)}</td>
                <td class="tabular-num" style="font-weight: 600;">${p.tuned_cr.toFixed(2)}</td>
                <td><span class="badge ${p.status === 'Valid Day' ? 'badge-clean' : 'badge-warn'}">${p.status}</span></td>
            `;
            tbody.appendChild(tr);
        });
    }

    function renderHourlyTable(hourly) {
        const tbody = document.getElementById('hourly-table-body');
        if (!tbody) return;
        tbody.innerHTML = '';

        hourly.forEach(h => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${h.time_str}</td>
                <td class="tabular-num">${h.pump_rate.toFixed(2)} U/h</td>
                <td class="tabular-num" style="font-weight: 600;">${h.tuned_rate.toFixed(2)} U/h</td>
                <td class="tabular-num">${h.delta_rate >= 0 ? '+' : ''}${h.delta_rate.toFixed(2)}</td>
                <td class="tabular-num">${h.pct_change >= 0 ? '+' : ''}${h.pct_change.toFixed(1)}%</td>
                <td class="tabular-num">[${h.min_cap.toFixed(2)}, ${h.max_cap.toFixed(2)}]</td>
                <td><span class="badge ${h.capped ? 'badge-warn' : 'badge-clean'}">${h.capped ? 'CAPPED' : 'OK'}</span></td>
                <td class="tabular-num">${h.deviation_mmol >= 0 ? '+' : ''}${h.deviation_mmol.toFixed(2)} mmol/L</td>
            `;
            tbody.appendChild(tr);
        });
    }

    window.atSwitchTab = function (tabId) {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.style.display = 'none');
        event.target.classList.add('active');
        const content = document.getElementById(`tab-content-${tabId}`);
        if (content) content.style.display = 'block';
    };

    // -------------------------------------------------------------
    // 6. INITIALIZATION & DATE POPOVER SYNC
    // -------------------------------------------------------------
    document.addEventListener('DOMContentLoaded', function () {
        initHelpPopovers();
        loadBaseProfiles();

        const profileSel = document.getElementById('at-profile-select');
        if (profileSel) {
            profileSel.addEventListener('change', function () {
                applyBaselineToScorecard(this.value);
            });
        }

        const startInput = document.getElementById('start_date');
        const endInput = document.getElementById('end_date');
        const sVal = startInput ? startInput.value : '';
        const eVal = endInput ? endInput.value : '';

        if (sVal && eVal) {
            loadAvailableDays(sVal, eVal);
        }

        // Listen for date selector synchronization from ns-date-selector.js
        window.addEventListener('nsDateChange', function (e) {
            if (e.detail && e.detail.startDate && e.detail.endDate) {
                loadAvailableDays(e.detail.startDate, e.detail.endDate);
            }
        });

        // Date input change observation
        if (startInput && endInput) {
            startInput.addEventListener('change', () => {
                loadAvailableDays(startInput.value, endInput.value);
            });
            endInput.addEventListener('change', () => {
                loadAvailableDays(startInput.value, endInput.value);
            });
        }

        window.addEventListener('resize', () => {
            if (basalChart) basalChart.resize();
        });
    });

})();
