/**
 * basal-eval-engine.js — In-Memory Interactive Modal Basal Evaluation Engine
 *
 * Provides sub-50ms reactive filtering and non-parametric diurnal percentile
 * aggregation (288 discrete 5-min bins) over any arbitrary date range.
 */

(function (window) {
    'use strict';

    function percentile(arr, p) {
        if (!arr || arr.length === 0) return 0;
        if (arr.length === 1) return arr[0];
        const sorted = arr.slice().sort((a, b) => a - b);
        const index = (p / 100) * (sorted.length - 1);
        const lower = Math.floor(index);
        const upper = Math.ceil(index);
        const weight = index - lower;
        return sorted[lower] * (1 - weight) + sorted[upper] * weight;
    }

    class BasalEvalEngine {
        constructor() {
            this.rawBuckets = [];
            this.totalDays = 0;
            this.startDate = '';
            this.endDate = '';

            // User-adjustable filter parameters (Defaults)
            this.filters = {
                mealTailHours: 3.0,
                cobCutoff: 0.0,
                uamTailHours: 2.0,
                smbMode: 'effective', // 'effective' | 'burst' | 'strict'
                strictSmbMinutes: 30,
                hypoRecoveryMask: true // exclude 90m after BG < 3.5 mmol/L
            };
        }

        loadDataset(payload) {
            this.startDate = payload.start_date || '';
            this.endDate = payload.end_date || '';
            this.totalDays = payload.total_days || 1;
            this.rawBuckets = (payload.rows || []).map(r => {
                const epoch = new Date(r.ts).getTime();
                return {
                    ...r,
                    epoch: isNaN(epoch) ? 0 : epoch
                };
            });
            // Ensure strictly sorted by time
            this.rawBuckets.sort((a, b) => a.epoch - b.epoch);
        }

        updateFilter(key, value) {
            if (this.filters.hasOwnProperty(key)) {
                this.filters[key] = value;
            }
        }

        compute() {
            if (!this.rawBuckets || this.rawBuckets.length === 0) {
                return this._emptyResult();
            }

            const mealTailMs = this.filters.mealTailHours * 3600 * 1000;
            const uamTailMs = this.filters.uamTailHours * 3600 * 1000;
            const strictSmbMs = this.filters.strictSmbMinutes * 60 * 1000;
            const burstSmbWindowMs = 30 * 60 * 1000;
            const burstRecoveryMs = 60 * 60 * 1000;
            const hypoRecoveryMs = 90 * 60 * 1000;

            let lastMealTime = -Infinity;
            let lastUamTime = -Infinity;
            let lastAnySmbTime = -Infinity;
            let lastBurstSmbTime = -Infinity;
            let lastHypoTime = -Infinity;

            // Rolling 30m SMB burst tracker
            const smbRecentQueue = [];

            // Step 1: Sequential timeline pass to tag exclusion boundaries
            const annotated = [];
            for (let i = 0; i < this.rawBuckets.length; i++) {
                const b = this.rawBuckets[i];
                const t = b.epoch;

                // Track meal bolus or manual carbs
                if ((b.meal_bolus && b.meal_bolus > 0) || (b.carbs && b.carbs > 0)) {
                    lastMealTime = t;
                }

                // Track true UAM excursion
                if (b.dev_class === 'UAM') {
                    lastUamTime = t;
                }

                // Track SMBs
                if (b.smb_bolus && b.smb_bolus > 0) {
                    lastAnySmbTime = t;
                    smbRecentQueue.push({ t, units: b.smb_bolus });
                }

                // Prune SMB burst tracker queue older than 30m
                while (smbRecentQueue.length > 0 && (t - smbRecentQueue[0].t) > burstSmbWindowMs) {
                    smbRecentQueue.shift();
                }
                const rollingSmbUnits = smbRecentQueue.reduce((acc, curr) => acc + curr.units, 0);
                if (rollingSmbUnits >= 0.3) {
                    lastBurstSmbTime = t;
                }

                // Track severe hypoglycemia (< 3.5 mmol/L)
                if (b.bg !== null && b.bg < 3.5) {
                    lastHypoTime = t;
                }

                // Exclusion logic
                let isClean = true;
                let reason = 'CLEAN';

                if (b.cob > this.filters.cobCutoff) {
                    isClean = false;
                    reason = 'COB';
                } else if ((t - lastMealTime) < mealTailMs) {
                    isClean = false;
                    reason = 'MEAL_TAIL';
                } else if ((t - lastUamTime) < uamTailMs) {
                    isClean = false;
                    reason = 'UAM_TAIL';
                } else if (this.filters.smbMode === 'strict' && (t - lastAnySmbTime) < strictSmbMs) {
                    isClean = false;
                    reason = 'SMB_STRICT';
                } else if (this.filters.smbMode === 'burst' && (t - lastBurstSmbTime) < burstRecoveryMs) {
                    isClean = false;
                    reason = 'SMB_BURST';
                } else if (this.filters.hypoRecoveryMask && (t - lastHypoTime) < hypoRecoveryMs) {
                    isClean = false;
                    reason = 'HYPO_REBOUND';
                }

                annotated.push({
                    ...b,
                    isClean,
                    reason
                });
            }

            // Step 2: Bin into 288 diurnal time-of-day slots (00:00 to 23:55)
            const bins = Array.from({ length: 288 }, () => ({
                cleanBuckets: [],
                allBuckets: [],
                cleanDays: new Set()
            }));

            let totalCleanCount = 0;
            let totalEvaluatedCount = annotated.length;

            for (let i = 0; i < annotated.length; i++) {
                const b = annotated[i];
                const binIdx = Math.max(0, Math.min(287, b.time_bin));
                bins[binIdx].allBuckets.push(b);
                if (b.isClean) {
                    bins[binIdx].cleanBuckets.push(b);
                    bins[binIdx].cleanDays.add(b.day);
                    totalCleanCount++;
                }
            }

            // Step 3: Compute non-parametric statistics for each diurnal bin
            const binStats = [];
            for (let i = 0; i < 288; i++) {
                const bin = bins[i];
                const clean = bin.cleanBuckets;
                const N = bin.cleanDays.size;

                // Time labels
                const h = Math.floor(i / 12);
                const m = (i % 12) * 5;
                const timeLabel = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');

                // Panel 1: Net IOB Percentiles
                const iobVals = clean.map(b => b.iob);
                const iobP25 = iobVals.length > 0 ? percentile(iobVals, 25) : 0;
                const iobMedian = iobVals.length > 0 ? percentile(iobVals, 50) : 0;
                const iobP75 = iobVals.length > 0 ? percentile(iobVals, 75) : 0;

                // Panel 2: Basal Delivery & Profiles
                const schedVals = clean.length > 0 ? clean.map(b => b.scheduled_basal) : bin.allBuckets.map(b => b.scheduled_basal);
                const schedBasal = schedVals.length > 0 ? percentile(schedVals, 50) : 0;

                const enactedVals = clean.map(b => b.basal_rate);
                const enactedBasal = enactedVals.length > 0 ? percentile(enactedVals, 50) : 0;

                // Effective Basal: Basal Rate + (SMB units / 5-min hour fraction)
                const effectiveVals = clean.map(b => b.basal_rate + (b.smb_bolus / (5.0 / 60.0)));
                const effectiveBasal = effectiveVals.length > 0 ? percentile(effectiveVals, 50) : enactedBasal;

                // Panel 3: Modal Deviation State Composition (EQUAL / SENS / RES)
                let equalCount = 0;
                let sensCount = 0;
                let resCount = 0;

                for (let k = 0; k < clean.length; k++) {
                    const cls = clean[k].dev_class;
                    if (cls === 'EQUAL') equalCount++;
                    else if (cls === 'SENS') sensCount++;
                    else if (cls === 'RES') resCount++;
                    else if (cls === 'UAM') resCount++; // in clean non-meal state, unexplained rises act as resistance
                    else equalCount++;
                }

                const totalClassified = equalCount + sensCount + resCount;
                const pctEqual = totalClassified > 0 ? (equalCount / totalClassified) * 100 : 0;
                const pctSens = totalClassified > 0 ? (sensCount / totalClassified) * 100 : 0;
                const pctRes = totalClassified > 0 ? (resCount / totalClassified) * 100 : 0;

                // Confidence classification
                let confidence = 'low';
                const confidenceRatio = this.totalDays > 0 ? (N / this.totalDays) : 0;
                if (confidenceRatio >= 0.40 && N >= 6) {
                    confidence = 'high';
                } else if (confidenceRatio >= 0.20 && N >= 3) {
                    confidence = 'medium';
                }

                binStats.push({
                    binIndex: i,
                    timeLabel,
                    N,
                    totalDays: this.totalDays,
                    confidenceRatio,
                    confidence,
                    // Panel 1
                    iobP25: Math.round(iobP25 * 100) / 100,
                    iobMedian: Math.round(iobMedian * 100) / 100,
                    iobP75: Math.round(iobP75 * 100) / 100,
                    // Panel 2
                    schedBasal: Math.round(schedBasal * 100) / 100,
                    enactedBasal: Math.round(enactedBasal * 100) / 100,
                    effectiveBasal: Math.round(effectiveBasal * 100) / 100,
                    basalDelta: Math.round((effectiveBasal - schedBasal) * 100) / 100,
                    // Panel 3
                    pctEqual: Math.round(pctEqual * 10) / 10,
                    pctSens: Math.round(pctSens * 10) / 10,
                    pctRes: Math.round(pctRes * 10) / 10
                });
            }

            const overallCleanPct = totalEvaluatedCount > 0 ? Math.round((totalCleanCount / totalEvaluatedCount) * 1000) / 10 : 0;

            return {
                startDate: this.startDate,
                endDate: this.endDate,
                totalDays: this.totalDays,
                totalBuckets: totalEvaluatedCount,
                cleanBuckets: totalCleanCount,
                cleanPct: overallCleanPct,
                binStats
            };
        }

        _emptyResult() {
            return {
                startDate: this.startDate,
                endDate: this.endDate,
                totalDays: this.totalDays,
                totalBuckets: 0,
                cleanBuckets: 0,
                cleanPct: 0,
                binStats: []
            };
        }
    }

    window.BasalEvalEngine = BasalEvalEngine;
})(window);
