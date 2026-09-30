/**
 * daemon/static/js/graph-it-all-renderer.js
 * Apache ECharts generation for Scatter, Line, Bar, 95% confidence corridor,
 * secondary Y2 axis, clinical normalcy bands, and NSAxisPillars integration.
 */
(function(global) {
    'use strict';

    let chartInstance = null;
    let lastRenderArgs = null;
    let lastLayout = null;

    function initChart(domId) {
        const el = document.getElementById(domId || 'graph-it-all-chart');
        if (!el) return null;

        if (chartInstance) {
            chartInstance.dispose();
        }

        chartInstance = echarts.init(el, null, { renderer: 'canvas' });
        window.addEventListener('resize', () => {
            if (chartInstance) {
                chartInstance.resize();
                if (lastLayout && window.NSAxisPillars) {
                    NSAxisPillars.renderPillars('gia-axis-pillars', 'graph-it-all-chart', lastLayout);
                }
            }
        });
        return chartInstance;
    }

    function isGlucoseMetric(metric) {
        if (!metric) return false;
        const u = (metric.unit || '').toLowerCase();
        const id = (metric.id || '').toLowerCase();
        return u === 'mmol/l' || id.includes('glucose') || id.includes('sgv');
    }

    function createGlucoseTargetArea() {
        return {
            silent: true,
            itemStyle: {
                color: 'rgba(46, 160, 67, 0.12)'
            },
            data: [
                [
                    {
                        name: 'Target Zone (3.9–10.0)',
                        yAxis: 3.9,
                        label: {
                            show: true,
                            position: 'insideTopRight',
                            formatter: '3.9–10.0 Target',
                            color: 'rgba(46, 160, 67, 0.85)',
                            fontSize: 10,
                            fontWeight: 600
                        }
                    },
                    { yAxis: 10.0 }
                ]
            ]
        };
    }

    function getMetricDecimals(metric) {
        if (!metric) return 1;
        const id = (metric.id || '').toLowerCase();
        const domain = (metric.domain || '').toLowerCase();
        const unit = (metric.unit || '').toLowerCase();

        // Explicit requirements: Carbs=0, cob=1, Insulin=1, IOB=1, In range stats=1, blood sugar=1
        if (id === 'cob') return 1;
        if (id === 'iob') return 1;
        if (id === 'total_carbs' || id === 'carbs' || domain === 'carbs' || unit === 'g') return 0;
        if (domain === 'insulin' || unit === 'u' || unit === 'u/hr' || id === 'tdd' || id.includes('bolus') || id.includes('basal')) return 1;
        if (unit === '%' || id.startsWith('tir') || id.startsWith('tbr') || id.startsWith('tar') || id === 'sensor_active_pct' || id.startsWith('gri')) return 1;
        if (domain === 'glucose' || unit === 'mmol/l' || id === 'mean_glucose' || id === 'glucose' || id === 'hba1c' || id === 'hbgi' || id === 'lbgi' || id === 'bgi' || id === 'bg_roc') return 1;

        return 1;
    }

    function formatMetricValue(val, metric) {
        if (val === null || val === undefined || isNaN(val)) return '—';
        if (typeof val === 'string') return val;
        const decimals = getMetricDecimals(metric);
        return Number(val).toFixed(decimals);
    }

    function render(chartType, queryResult, xMetric, yMetric, y2Metric, renderOptions) {
        const emptyEl = document.getElementById('gia-empty-state');
        const statsEl = document.getElementById('gia-stage-stats');

        if (!chartInstance) {
            initChart('graph-it-all-chart');
        }

        if (!chartInstance) return;

        lastRenderArgs = { chartType, queryResult, xMetric, yMetric, y2Metric };

        const data = (queryResult && queryResult.data) || [];
        const trend = (queryResult && queryResult.trend) || null;
        const meta = (queryResult && queryResult.meta) || {};
        const hasY2 = !!(y2Metric && meta.has_y2);

        // Failure handling (C3): If data is empty, clear chart and show empty placeholder
        if (data.length === 0) {
            chartInstance.clear();
            if (emptyEl) {
                emptyEl.textContent = meta.message || 'No data points found for this selection and date range.';
                emptyEl.style.display = 'block';
            }
            if (statsEl) statsEl.innerHTML = '';
            const pillarsEl = document.getElementById('gia-axis-pillars');
            if (pillarsEl) pillarsEl.innerHTML = '';
            return;
        }

        if (emptyEl) emptyEl.style.display = 'none';

        const isCycle = !!(meta && meta.is_cycle);
        const xm = xMetric || { id: 'date', name: (meta && meta.x_column) || 'X Axis', unit: '' };
        const ym = yMetric || { id: 'y', name: (meta && meta.y_column) || 'Y Axis', unit: '' };
        const y2m = hasY2 ? (y2Metric || { id: 'y2', name: meta.y2_metric || 'Y2 Axis', unit: meta.y2_unit || '' }) : null;

        const isXDate = (xm.id === 'date') || isCycle;
        const xTitle = (isCycle && meta.x_metric) ? meta.x_metric : xm.name;
        const xUnit = (!isCycle && xm.unit) ? ` (${xm.unit})` : '';
        const yUnit = ym.unit ? ` (${ym.unit})` : '';
        const y2Unit = (y2m && y2m.unit) ? ` (${y2m.unit})` : '';

        const y1IsGlucose = isGlucoseMetric(ym);
        const y2IsGlucose = hasY2 && isGlucoseMetric(y2m);

        // Update stats scorecard
        if (statsEl) {
            let statsHtml = `<span class="gia-stat-pill">Points: <strong>${meta.points || data.length}</strong></span>`;
            if (meta.nulls_excluded > 0) {
                statsHtml += `<span class="gia-stat-pill">Nulls Excluded: <strong>${meta.nulls_excluded}</strong></span>`;
            }
            if (trend) {
                statsHtml += `<span class="gia-stat-pill">Fit: <strong>${trend.equation}</strong></span>`;
                if (trend.r2 !== null && trend.r2 !== undefined) {
                    statsHtml += `<span class="gia-stat-pill">R²: <strong>${trend.r2.toFixed(3)}</strong></span>`;
                }
                if (trend.se) {
                    const seDecimals = getMetricDecimals(ym);
                    statsHtml += `<span class="gia-stat-pill">SE: <strong>${trend.se.toFixed(seDecimals)}</strong></span>`;
                }
            } else if (meta.trend_skipped_reason) {
                statsHtml += `<span class="gia-stat-pill" style="color: var(--text-tertiary);">${meta.trend_skipped_reason}</span>`;
            }

            // Help Popover to the right of stats scorecard
            statsHtml += `
                <div class="gia-popover-wrapper" style="margin-left: 6px;">
                    <button type="button" class="gia-popover-trigger" aria-label="Scorecard Statistics Guide" title="Scorecard Guide">
                        <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
                            <path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/>
                            <path d="M7.002 11a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM7.1 4.995a.905.905 0 1 1 1.8 0l-.35 3.507a.553.553 0 0 1-1.1 0L7.1 4.995z"/>
                        </svg>
                    </button>
                    <div class="gia-guide-popover gia-popover-right" onclick="event.stopPropagation()">
                        <h4>Scorecard Statistics Guide</h4>
                        <h5>Points:</h5>
                        <p>The total number of aggregated data points plotted on the chart for the selected date range and grain.</p>
                        <h5>Nulls Excluded:</h5>
                        <p>The count of time intervals omitted because one or both metrics lacked readings during that period.</p>
                        <h5>Fit (Equation):</h5>
                        <p>The mathematical curve fitted across your primary Y observations:
                        <ul>
                            <li><strong>Linear:</strong> <code>y = mx + c</code> (constant rate of change).</li>
                            <li><strong>Quadratic:</strong> <code>y = ax² + bx + c</code> (parabolic curve).</li>
                            <li><strong>Exponential / Power:</strong> Non-linear compound growth or decay.</li>
                            <li><strong>LOESS:</strong> Locally estimated scatterplot smoothing (non-parametric curve following data contours).</li>
                        </ul>
                        </p>
                        <h5>R² (Coefficient of Determination):</h5>
                        <p>Measures how much variation in Y is explained by X (from <strong>0.000 to 1.000</strong>):
                        <ul>
                            <li><strong>0.00 – 0.10:</strong> No meaningful correlation.</li>
                            <li><strong>0.10 – 0.50:</strong> Moderate relationship with notable scatter.</li>
                            <li><strong>0.50 – 1.00:</strong> Strong predictive correlation.</li>
                        </ul>
                        </p>
                        <h5>SE (Standard Error of the Estimate):</h5>
                        <p>The average vertical distance (measured in your primary Y metric's units) that actual data points deviate from the regression line. Lower SE indicates points hug the curve closely.</p>
                    </div>
                </div>
            `;
            statsEl.innerHTML = statsHtml;
        }

        // Compute calculated data min/max for Y1
        let y1Vals = data.map(d => d[1]).filter(v => typeof v === 'number' && isFinite(v));
        const selectedBand = trend ? (trend.selected_band || (trend.showConfidenceBand !== false ? 'ci95' : 'none')) : 'none';
        if (trend && selectedBand !== 'none') {
            const bands = trend.bands || {};
            const activeBands = (selectedBand === 'agp_fan')
                ? [bands.q5_95 || trend.prediction_corridor, bands.q25_75]
                : (selectedBand === 'both_95')
                    ? [bands.pi95 || trend.prediction_corridor, bands.ci95 || trend.corridor]
                    : [bands[selectedBand] || (selectedBand === 'pi95' ? trend.prediction_corridor : trend.corridor)];

            activeBands.forEach(bData => {
                if (bData && Array.isArray(bData)) {
                    bData.forEach(c => {
                        if (c && typeof c[1] === 'number' && isFinite(c[1])) y1Vals.push(c[1]);
                        if (c && typeof c[2] === 'number' && isFinite(c[2])) y1Vals.push(c[2]);
                    });
                }
            });
        }
        if (y1Vals.length === 0) y1Vals = [0, 10];
        let y1CalcMin = Math.min(...y1Vals);
        let y1CalcMax = Math.max(...y1Vals);
        if (y1CalcMin === y1CalcMax) {
            y1CalcMin -= 1;
            y1CalcMax += 1;
        }

        const y1Step = ym.unit === 'mmol/L' ? 1.0 : ((y1CalcMax - y1CalcMin > 50) ? 5 : 1);
        const y1AxisInfo = {
            group: 'y1',
            name: ym.name,
            color: hasY2 ? '#0969da' : '#57606a',
            step: y1Step,
            decimals: getMetricDecimals(ym),
            calculatedMin: Math.floor(y1CalcMin * 10) / 10,
            calculatedMax: Math.ceil(y1CalcMax * 10) / 10
        };

        // Compute calculated data min/max for Y2
        let y2AxisInfo = null;
        if (hasY2) {
            let y2Vals = data.map(d => d[3]).filter(v => typeof v === 'number' && !isNaN(v));
            if (y2Vals.length === 0) y2Vals = [0, 10];
            let y2CalcMin = Math.min(...y2Vals);
            let y2CalcMax = Math.max(...y2Vals);
            if (y2CalcMin === y2CalcMax) {
                y2CalcMin -= 1;
                y2CalcMax += 1;
            }
            const y2Step = (y2m.unit === 'mmol/L') ? 1.0 : ((y2CalcMax - y2CalcMin > 50) ? 5 : 1);
            y2AxisInfo = {
                group: 'y2',
                name: y2m.name,
                color: '#cf222e',
                step: y2Step,
                decimals: getMetricDecimals(y2m),
                calculatedMin: Math.floor(y2CalcMin * 10) / 10,
                calculatedMax: Math.ceil(y2CalcMax * 10) / 10
            };
        }

        const seriesList = [];

        // Base series (Scatter, Line, or Bar)
        if (chartType === 'scatter') {
            seriesList.push({
                name: `${xm.name} vs ${ym.name}`,
                type: 'scatter',
                yAxisIndex: 0,
                data: data.map(d => [d[0], d[1], d[2]]),
                symbolSize: 8,
                itemStyle: {
                    color: '#0969da',
                    opacity: 0.75,
                    borderColor: '#0550ae',
                    borderWidth: 1
                },
                ...(y1IsGlucose ? { markArea: createGlucoseTargetArea() } : {})
            });

            if (hasY2) {
                seriesList.push({
                    name: `${xm.name} vs ${y2m.name} (Y₂)`,
                    type: 'scatter',
                    yAxisIndex: 1,
                    data: data.filter(d => d[3] !== null && d[3] !== undefined).map(d => [d[0], d[3], d[2]]),
                    symbolSize: 8,
                    itemStyle: {
                        color: '#cf222e',
                        opacity: 0.75,
                        borderColor: '#a40e26',
                        borderWidth: 1
                    },
                    ...(y2IsGlucose && !y1IsGlucose ? { markArea: createGlucoseTargetArea() } : {})
                });
            }
        } else if (chartType === 'line') {
            seriesList.push({
                name: ym.name,
                type: 'line',
                yAxisIndex: 0,
                data: data.map(d => [d[0], d[1]]),
                smooth: true,
                symbol: 'circle',
                symbolSize: 4,
                lineStyle: {
                    color: '#0969da',
                    width: 2.2
                },
                itemStyle: {
                    color: '#0969da'
                },
                ...(y1IsGlucose ? { markArea: createGlucoseTargetArea() } : {})
            });

            if (hasY2) {
                seriesList.push({
                    name: `${y2m.name} (Y₂)`,
                    type: 'line',
                    yAxisIndex: 1,
                    data: data.filter(d => d[3] !== null && d[3] !== undefined).map(d => [d[0], d[3]]),
                    smooth: true,
                    symbol: 'diamond',
                    symbolSize: 4,
                    lineStyle: {
                        color: '#cf222e',
                        width: 2.2,
                        type: 'dashed'
                    },
                    itemStyle: {
                        color: '#cf222e'
                    },
                    ...(y2IsGlucose && !y1IsGlucose ? { markArea: createGlucoseTargetArea() } : {})
                });
            }
        } else if (chartType === 'bar') {
            seriesList.push({
                name: ym.name,
                type: 'bar',
                yAxisIndex: 0,
                data: data.map(d => [d[0], d[1]]),
                itemStyle: {
                    color: '#0969da',
                    borderRadius: [3, 3, 0, 0]
                },
                ...(y1IsGlucose ? { markArea: createGlucoseTargetArea() } : {})
            });

            if (hasY2) {
                seriesList.push({
                    name: `${y2m.name} (Y₂)`,
                    type: 'bar',
                    yAxisIndex: 1,
                    data: data.filter(d => d[3] !== null && d[3] !== undefined).map(d => [d[0], d[3]]),
                    itemStyle: {
                        color: 'rgba(207, 34, 46, 0.75)',
                        borderRadius: [3, 3, 0, 0]
                    },
                    ...(y2IsGlucose && !y1IsGlucose ? { markArea: createGlucoseTargetArea() } : {})
                });
            }
        }

        function createPolygonSeries(name, dataArr, fillColor, strokeColor, isDashed, zLevel) {
            return {
                name: name,
                type: 'custom',
                clip: true,
                silent: true,
                z: zLevel,
                yAxisIndex: 0,
                renderItem: function(params, api) {
                    if (params.dataIndex !== 0) return;
                    if (!dataArr || !Array.isArray(dataArr) || dataArr.length < 2) return;
                    const validData = dataArr.filter(d =>
                        d && typeof d[1] === 'number' && isFinite(d[1]) &&
                        typeof d[2] === 'number' && isFinite(d[2])
                    );
                    if (validData.length < 2) return;
                    const points = [];
                    for (let i = 0; i < validData.length; i++) {
                        const pt = api.coord([validData[i][0], validData[i][2]]);
                        if (pt && isFinite(pt[0]) && isFinite(pt[1])) points.push(pt);
                    }
                    for (let i = validData.length - 1; i >= 0; i--) {
                        const pt = api.coord([validData[i][0], validData[i][1]]);
                        if (pt && isFinite(pt[0]) && isFinite(pt[1])) points.push(pt);
                    }
                    if (points.length < 4) return;
                    const styleObj = {
                        fill: fillColor,
                        stroke: strokeColor,
                        lineWidth: 1
                    };
                    if (isDashed) {
                        styleObj.lineDash = [4, 4];
                    }
                    return {
                        type: 'polygon',
                        shape: { points: points },
                        style: styleObj
                    };
                },
                data: [0]
            };
        }

        // Add Statistical Overlays for primary Y axis
        if (trend && ((trend.corridor && trend.corridor.length > 0) || (trend.points && trend.points.length > 0))) {
            const corridor = trend.corridor || [];
            const bands = trend.bands || {};
            const selectedBand = trend.selected_band || (trend.showConfidenceBand !== false ? 'ci95' : 'none');

            if (selectedBand === 'agp_fan') {
                const qOuter = bands.q5_95 || trend.prediction_corridor;
                const qInner = bands.q25_75;
                if (qOuter && qOuter.length >= 2) {
                    seriesList.push(createPolygonSeries('5th–95th Percentile (AGP)', qOuter, 'rgba(9, 105, 218, 0.08)', 'rgba(9, 105, 218, 0.25)', true, 1));
                }
                if (qInner && qInner.length >= 2) {
                    seriesList.push(createPolygonSeries('25th–75th Percentile (IQR)', qInner, 'rgba(9, 105, 218, 0.18)', 'rgba(9, 105, 218, 0.40)', false, 2));
                }
            } else if (selectedBand === 'both_95') {
                const pi = bands.pi95 || trend.prediction_corridor;
                const ci = bands.ci95 || corridor;
                if (pi && pi.length >= 2) {
                    seriesList.push(createPolygonSeries('95% Prediction Interval', pi, 'rgba(9, 105, 218, 0.08)', 'rgba(9, 105, 218, 0.25)', true, 1));
                }
                if (ci && ci.length >= 2) {
                    seriesList.push(createPolygonSeries('95% Mean Confidence Corridor', ci, 'rgba(9, 105, 218, 0.18)', 'rgba(9, 105, 218, 0.40)', false, 2));
                }
            } else if (selectedBand !== 'none') {
                const targetBandData = bands[selectedBand] || (selectedBand === 'pi95' ? trend.prediction_corridor : corridor);
                if (targetBandData && targetBandData.length >= 2) {
                    let label = 'Confidence Corridor';
                    let isDashed = false;
                    let fill = 'rgba(9, 105, 218, 0.15)';
                    let stroke = 'rgba(9, 105, 218, 0.40)';

                    if (selectedBand === 'ci95') label = '95% Mean Confidence Corridor';
                    else if (selectedBand === 'ci99') label = '99% Mean Confidence Corridor';
                    else if (selectedBand === 'q25_75') label = '25th–75th Percentile (IQR)';
                    else if (selectedBand === 'q20_80') label = '20th–80th Percentile';
                    else if (selectedBand === 'q5_95') { label = '5th–95th Percentile (AGP)'; isDashed = true; fill = 'rgba(9, 105, 218, 0.08)'; }
                    else if (selectedBand === 'pi95') { label = '95% Prediction Interval'; isDashed = true; fill = 'rgba(9, 105, 218, 0.08)'; }

                    seriesList.push(createPolygonSeries(label, targetBandData, fill, stroke, isDashed, 2));
                }
            }

            // Trend Fit line
            const trendLineData = (trend.points && trend.points.length > 0)
                ? trend.points
                : (corridor.length > 0 ? corridor.map(c => [c[0], c[3]]) : []);

            if (trendLineData.length > 0) {
                seriesList.push({
                    name: `Fitted Trend (${trend.model})`,
                    type: 'line',
                    yAxisIndex: 0,
                    data: trendLineData,
                    lineStyle: {
                        color: '#1f2328',
                        width: 2.5
                    },
                    symbol: 'none',
                    z: 3,
                    tooltip: {
                        formatter: function(params) {
                            return `<strong>${trend.equation || trend.formula || ''}</strong><br/>R² = ${trend.r2 !== null && trend.r2 !== undefined ? Number(trend.r2).toFixed(3) : 'N/A'}`;
                        }
                    }
                });
            }
        }

        // Configure Left Y Axis (Y1)
        const leftYAxis = {
            type: 'value',
            name: ym.name + yUnit,
            nameLocation: 'middle',
            nameGap: 45,
            nameTextStyle: {
                color: hasY2 ? '#0969da' : '#57606a',
                fontSize: 12,
                fontWeight: 600
            },
            axisLine: { lineStyle: { color: hasY2 ? '#0969da' : '#d0d7de' } },
            axisTick: { lineStyle: { color: hasY2 ? '#0969da' : '#d0d7de' } },
            axisLabel: { color: '#57606a', fontSize: 11 },
            splitLine: { lineStyle: { color: '#f0f2f5' } }
        };

        if (window.NSAxisPillars) {
            const customMin = NSAxisPillars.getCustomBound('y1', 'min');
            const customMax = NSAxisPillars.getCustomBound('y1', 'max');
            if (customMin !== null && !isNaN(customMin)) leftYAxis.min = customMin;
            if (customMax !== null && !isNaN(customMax)) leftYAxis.max = customMax;
        }

        let yAxisConfig = leftYAxis;

        // Configure Right Y Axis (Y2)
        if (hasY2) {
            const rightYAxis = {
                type: 'value',
                position: 'right',
                name: y2m.name + y2Unit,
                nameLocation: 'middle',
                nameGap: 45,
                nameTextStyle: {
                    color: '#cf222e',
                    fontSize: 12,
                    fontWeight: 600
                },
                axisLine: { lineStyle: { color: '#cf222e' } },
                axisTick: { lineStyle: { color: '#cf222e' } },
                axisLabel: { color: '#cf222e', fontSize: 11 },
                splitLine: { show: false }
            };

            if (window.NSAxisPillars) {
                const customMinY2 = NSAxisPillars.getCustomBound('y2', 'min');
                const customMaxY2 = NSAxisPillars.getCustomBound('y2', 'max');
                if (customMinY2 !== null && !isNaN(customMinY2)) rightYAxis.min = customMinY2;
                if (customMaxY2 !== null && !isNaN(customMaxY2)) rightYAxis.max = customMaxY2;
            }

            yAxisConfig = [leftYAxis, rightYAxis];
        }

        const option = {
            backgroundColor: 'transparent',
            tooltip: {
                trigger: chartType === 'scatter' ? 'item' : 'axis',
                backgroundColor: 'rgba(31, 35, 40, 0.94)',
                borderColor: '#424a53',
                textStyle: { color: '#ffffff', fontSize: 12 },
                formatter: function(params) {
                    if (chartType === 'scatter') {
                        const pt = Array.isArray(params) ? params[0] : params;
                        if (!pt || !pt.data) return '';
                        const isY2Point = pt.seriesName && pt.seriesName.includes('(Y₂)');
                        const dateStr = pt.data[2] || '';
                        const xVal = (xm.id === 'date' || isCycle) ? pt.data[0] : formatMetricValue(pt.data[0], xm);
                        const yVal = formatMetricValue(pt.data[1], ym);
                        if (isY2Point && y2m) {
                            const y2Val = formatMetricValue(pt.data[1], y2m);
                            return `
                                <div style="font-weight: 700; border-bottom: 1px solid #424a53; padding-bottom: 4px; margin-bottom: 4px;">
                                    ${dateStr}
                                </div>
                                <div>${xm.name}: <strong>${xVal}</strong>${xUnit}</div>
                                <div><span style="color: #ff7b72;">${y2m.name} (Y₂)</span>: <strong>${y2Val}</strong>${y2Unit}</div>
                            `;
                        }
                        return `
                            <div style="font-weight: 700; border-bottom: 1px solid #424a53; padding-bottom: 4px; margin-bottom: 4px;">
                                ${dateStr}
                            </div>
                            <div>${xm.name}: <strong>${xVal}</strong>${xUnit}</div>
                            <div>${ym.name}: <strong>${yVal}</strong>${yUnit}</div>
                        `;
                    } else {
                        const items = Array.isArray(params) ? params : [params];
                        let out = `<div style="font-weight: 700; margin-bottom: 4px;">${items[0].axisValueLabel || ''}</div>`;
                        items.forEach(it => {
                            if (it.seriesName.includes('Confidence') || it.seriesName.includes('Prediction') || it.seriesName.includes('Percentile')) return;
                            const isY2 = it.seriesName.includes('(Y₂)');
                            const metricObj = isY2 ? y2m : ym;
                            const valStr = formatMetricValue(it.data[1], metricObj);
                            const unitStr = isY2 ? y2Unit : yUnit;
                            out += `<div>${it.seriesName}: <strong>${valStr}</strong>${unitStr}</div>`;
                        });
                        return out;
                    }
                }
            },
            grid: {
                top: 40,
                right: hasY2 ? 64 : 30,
                bottom: 60,
                left: 64,
                containLabel: false
            },
            xAxis: {
                type: isXDate ? 'category' : 'value',
                ...(isXDate ? { data: data.map(d => d[0]) } : {}),
                name: xTitle + xUnit,
                nameLocation: 'middle',
                nameGap: 32,
                nameTextStyle: {
                    color: '#24292f',
                    fontSize: 12,
                    fontWeight: 600
                },
                axisLine: { lineStyle: { color: '#d0d7de' } },
                axisTick: { lineStyle: { color: '#d0d7de' } },
                axisLabel: { color: '#57606a', fontSize: 11 },
                splitLine: { lineStyle: { color: '#f0f2f5' } }
            },
            yAxis: yAxisConfig,
            series: seriesList
        };

        chartInstance.setOption(option, true);

        // NSAxisPillars overlay rendering
        lastLayout = {
            top: 40,
            bottom: 60,
            left: 64,
            right: hasY2 ? 64 : 30,
            activeLeftAxes: [y1AxisInfo],
            activeRightAxes: hasY2 ? [y2AxisInfo] : []
        };

        if (window.NSAxisPillars && (!renderOptions || !renderOptions.skipPillars)) {
            NSAxisPillars.renderPillars('gia-axis-pillars', 'graph-it-all-chart', lastLayout);
        }
    }

    function reRenderCurrentBounds() {
        if (!lastRenderArgs) return;
        render(
            lastRenderArgs.chartType,
            lastRenderArgs.queryResult,
            lastRenderArgs.xMetric,
            lastRenderArgs.yMetric,
            lastRenderArgs.y2Metric,
            { skipPillars: true }
        );
    }

    function clear() {
        if (chartInstance) {
            chartInstance.clear();
        }
        const statsEl = document.getElementById('gia-stage-stats');
        if (statsEl) statsEl.innerHTML = '';
        const emptyEl = document.getElementById('gia-empty-state');
        if (emptyEl) {
            emptyEl.textContent = 'No graph generated yet. Select metrics and click Update.';
            emptyEl.style.display = 'block';
        }
        const pillarsEl = document.getElementById('gia-axis-pillars');
        if (pillarsEl) pillarsEl.innerHTML = '';
    }

    global.GIARenderer = {
        initChart,
        render,
        clear,
        reRenderCurrentBounds
    };

})(window);
