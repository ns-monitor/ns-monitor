/**
 * ns-date-selector.js - Canonical date selector and popover controller for ns-monitor
 * Supports Primary Range & Compare To, preset arithmetic, local storage persistence,
 * cross-page and cross-tab real-time synchronization.
 */

(function () {
    const MONTH_PRESET_SPANS = { prev_1m: 1, prev_3m: 3, prev_6m: 6, prev_12m: 12 };
    const MONTH_PRESET_SPANS_LP = { prev_1m: 1, prev_3m: 3, prev_6m: 6, prev_12m: 12 };

    function getTodayStr() {
        if (window.todayStr) return window.todayStr;
        const now = new Date();
        return formatLocal(now);
    }

    function getEarliestDate() {
        if (window.nsEarliestCgmDate) return window.nsEarliestCgmDate;
        const wrap = document.querySelector('.date-picker-wrap');
        if (wrap && wrap.dataset && wrap.dataset.earliestDate) {
            window.nsEarliestCgmDate = wrap.dataset.earliestDate;
            return window.nsEarliestCgmDate;
        }
        return null;
    }

    async function ensureEarliestDate() {
        const cached = getEarliestDate();
        if (cached) return cached;
        try {
            const resp = await fetch('/api/v1/earliest_cgm_date');
            if (resp.ok) {
                const data = await resp.json();
                if (data && data.earliest_date) {
                    window.nsEarliestCgmDate = data.earliest_date;
                    return data.earliest_date;
                }
            }
        } catch (e) {
            console.warn('[ns-date-selector] Error fetching earliest CGM date:', e);
        }
        return getTodayStr();
    }

    function formatLocal(d) {
        if (!d || isNaN(d.getTime())) return '';
        return d.getFullYear() + '-'
            + String(d.getMonth() + 1).padStart(2, '0') + '-'
            + String(d.getDate()).padStart(2, '0');
    }

    function parseLocal(dateStr) {
        if (!dateStr) return new Date();
        const parts = dateStr.split('-').map(Number);
        if (parts.length !== 3) return new Date();
        return new Date(parts[0], parts[1] - 1, parts[2]);
    }

    // Pages normally share the ns_date_* selection. A page can opt into an
    // isolated selection by setting data-date-state-namespace on its selector.
    function dateStateNamespace() {
        var wrap = document.querySelector('.date-picker-wrap');
        return wrap && wrap.dataset ? (wrap.dataset.dateStateNamespace || '') : '';
    }

    function dateStateKey(part) {
        var namespace = dateStateNamespace();
        return namespace ? ('ns_' + namespace + '_date_' + part) : ('ns_date_' + part);
    }

    function dateStateGet(part) {
        return localStorage.getItem(dateStateKey(part));
    }

    function dateStateSet(part, value) {
        localStorage.setItem(dateStateKey(part), value);
    }

    function formatRangeDisplay(startDateStr, endDateStr) {
        if (!startDateStr || !endDateStr) return '';
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        const s = parseLocal(startDateStr);
        const e = parseLocal(endDateStr);

        const diffDays = Math.max(1, Math.round((e - s) / 86400000) + 1);
        const dayCountSuffix = ` (${diffDays}d)`;

        const sDay = s.getDate();
        const sMon = months[s.getMonth()];
        const sYear = s.getFullYear();

        const eDay = e.getDate();
        const eMon = months[e.getMonth()];
        const eYear = e.getFullYear();

        let dateText = '';
        if (sYear === eYear) {
            if (sMon === eMon) {
                if (sDay === eDay) {
                    dateText = `${sDay} ${sMon} ${sYear}`;
                } else {
                    dateText = `${sDay}–${eDay} ${eMon} ${eYear}`;
                }
            } else {
                dateText = `${sDay} ${sMon} – ${eDay} ${eMon} ${eYear}`;
            }
        } else {
            dateText = `${sDay} ${sMon} ${sYear} – ${eDay} ${eMon} ${eYear}`;
        }
        return dateText + dayCountSuffix;
    }

    window.formatDMY = function (isoStr) {
        if (!isoStr) return isoStr;
        // ECharts can supply a numeric timestamp for a time-axis tooltip.
        // Formatting it as an ISO date here previously threw in Safari and
        // interrupted its chart event cycle.
        if (typeof isoStr !== 'string') {
            var asDate = new Date(isoStr);
            if (!isNaN(asDate.getTime())) return formatLocal(asDate).split('-').reverse().join('/');
            return String(isoStr);
        }
        const parts = isoStr.split('-');
        if (parts.length !== 3) return isoStr;
        const [y, m, d] = parts;
        return `${d}/${m}/${y}`;
    };

    window.getChartParams = function () {
        const start = document.getElementById('start_date')?.value || '';
        const end = document.getElementById('end_date')?.value || '';
        const p = new URLSearchParams();
        // Preserve original names for backward compatibility
        p.set('start_date', start);
        p.set('end_date', end);
        // New aliases used by orchestrator
        p.set('start', start);
        p.set('end', end);
        const grainMonthly = document.getElementById('grain-monthly');
        if (grainMonthly && grainMonthly.checked) {
            p.set('grain', 'monthly');
        }
        return p.toString();
    };

    window.initControls = function (today) {
        if (today) window.todayStr = today;
    };

    // ─── Popover UI Toggle ─────────────────────────────────────────────
    window.toggleDatePopover = function (event) {
        if (event) event.stopPropagation();
        const popover = document.getElementById('date-popover');
        if (!popover) return;
        const isHidden = popover.style.display === 'none' || !popover.style.display;
        popover.style.display = isHidden ? 'block' : 'none';
    };

    window.closeDatePopover = function () {
        const popover = document.getElementById('date-popover');
        if (popover) popover.style.display = 'none';
    };

    document.addEventListener('click', function (e) {
        const popover = document.getElementById('date-popover');
        const trigger = document.getElementById('date-picker-trigger');
        if (!popover || popover.style.display === 'none') return;
        if (!popover.contains(e.target) && !trigger?.contains(e.target)) {
            closeDatePopover();
        }
    });

    // ─── Trigger Text Formatting ───────────────────────────────────────
    window.updateTriggerText = function () {
        const sEl = document.getElementById('start_date');
        const eEl = document.getElementById('end_date');
        const triggerText = document.getElementById('trigger-range-text');
        if (!sEl || !eEl || !triggerText) return;

        const rangeStr = formatRangeDisplay(sEl.value, eEl.value);
        triggerText.textContent = rangeStr || 'Select dates';

        const compareText = document.getElementById('trigger-compare-text');
        if (compareText) {
            const compToggle = document.getElementById('compare-toggle');
            if (compToggle && compToggle.checked) {
                const lpS = document.getElementById('start_date-lastperiod')?.value;
                const lpE = document.getElementById('end_date-lastperiod')?.value;
                if (lpS && lpE) {
                    compareText.textContent = ` vs ${formatRangeDisplay(lpS, lpE)}`;
                } else {
                    compareText.textContent = '';
                }
            } else {
                compareText.textContent = '';
            }
        }
    };

    function updateNavLinks(sVal, eVal) {
        if (!sVal || !eVal) return;
        const datePages = ['/patterns', '/trends', '/timeline', '/omnipod', '/cgm', '/clinical_reports', '/clinical-reports', '/novel_reports', '/novel-reports', '/sandbox', '/graph-it-all'];
        const navLinks = document.querySelectorAll('.sb-link, a.btn');
        navLinks.forEach(b => {
            if (!b.href) return;
            try {
                const url = new URL(b.href, window.location.origin);
                if (datePages.includes(url.pathname) || url.searchParams.has('start_date')) {
                    url.searchParams.set('start_date', sVal);
                    url.searchParams.set('end_date', eVal);
                    b.href = url.pathname + url.search;
                }
            } catch (err) {}
        });
    }

    // ─── Data Sync Dispatcher ──────────────────────────────────────────
    window.nsTriggerDataSync = function () {
        const sVal = document.getElementById('start_date')?.value || '';
        const eVal = document.getElementById('end_date')?.value || '';
        const lpS = document.getElementById('start_date-lastperiod')?.value || '';
        const lpE = document.getElementById('end_date-lastperiod')?.value || '';
        const compToggle = document.getElementById('compare-toggle');
        const isCompare = compToggle ? compToggle.checked : false;

        updateTriggerText();
        updateNavLinks(sVal, eVal);

        // Fire custom window event
        window.dispatchEvent(new CustomEvent('ns-date-change', {
            detail: {
                startDate: sVal,
                endDate: eVal,
                compareEnabled: isCompare,
                compareStartDate: lpS,
                compareEndDate: lpE
            }
        }));

        // Trigger page-local syncAndSubmit or onDateOrPresetChange hook
        if (typeof window.syncAndSubmit === 'function') {
            window.syncAndSubmit();
        } else if (typeof window.onDateOrPresetChange === 'function') {
            window.onDateOrPresetChange();
        } else if (typeof window.onDateChange === 'function') {
            window.onDateChange(sVal, eVal, lpS, lpE);
        }
    };

    // ─── Primary Range Calculations ────────────────────────────────────
    window.nsApplyPreset = function (daysParam, shouldSubmit = true) {
        const presetEl = document.getElementById('date-preset');
        let preset = daysParam !== undefined ? daysParam.toString() : (presetEl ? presetEl.value : '7');

        const now = new Date();
        let start, end;
        const includeTodayEl = document.getElementById('include-today');

        if (preset === 'today') {
            start = new Date();
            end = new Date();
            if (includeTodayEl) includeTodayEl.checked = true;
        } else if (preset === 'yesterday') {
            start = new Date();
            start.setDate(start.getDate() - 1);
            end = new Date(start);
            if (includeTodayEl) includeTodayEl.checked = false;
        } else if (preset === 'all_time') {
            const earliest = getEarliestDate();
            if (!earliest) {
                ensureEarliestDate().then(d => {
                    window.nsEarliestCgmDate = d;
                    nsApplyPreset('all_time', shouldSubmit);
                });
                return;
            }
            start = parseLocal(earliest);
            end = new Date();
            if (includeTodayEl) includeTodayEl.checked = true;
        } else if (preset in MONTH_PRESET_SPANS) {
            const n = MONTH_PRESET_SPANS[preset];
            start = new Date(now.getFullYear(), now.getMonth() - n, 1);
            end = new Date(now.getFullYear(), now.getMonth(), 0);
        } else {
            if (!preset) preset = "7";
            const includeToday = includeTodayEl ? includeTodayEl.checked : false;
            end = new Date();
            if (!includeToday) end.setDate(end.getDate() - 1);

            const span = (preset === 'last2') ? 2 : (preset === 'last3') ? 3 : parseInt(preset, 10);
            start = new Date(end);
            start.setDate(start.getDate() - span + 1);
        }

        if (start && end) {
            const sStr = formatLocal(start);
            const eStr = formatLocal(end);

            const sEl = document.getElementById('start_date');
            const eEl = document.getElementById('end_date');
            if (sEl) sEl.value = sStr;
            if (eEl) eEl.value = eStr;

            dateStateSet('start', sStr);
            dateStateSet('end', eStr);
            dateStateSet('preset', preset);
            if (includeTodayEl) {
                dateStateSet('include_today', includeTodayEl.checked ? 'true' : 'false');
            }

            nsRecomputeComparisonIfPreset();
            if (shouldSubmit) {
                nsTriggerDataSync();
            } else {
                updateTriggerText();
            }
        }
    };

    window.nsOnPresetChange = function () {
        const val = document.getElementById('date-preset')?.value || '';
        dateStateSet('preset', val);
        nsApplyPreset(val);
    };

    window.nsSyncInputs = function () {
        const sEl = document.getElementById('start_date');
        const eEl = document.getElementById('end_date');
        if (!sEl || !eEl) return;

        // No page here has data past today, so no reason to be able to
        // query into the future (Harry's report, 1 Sep 2026). Belt-and-
        // braces on top of the inputs' own max="{{ today_local }}" --
        // that's not reliably enforced for every input interaction across
        // browsers, this is the one place all custom-range edits funnel
        // through regardless of how the value got changed.
        const today = getTodayStr();
        if (eEl.value > today) eEl.value = today;
        if (sEl.value > today) sEl.value = today;

        const sVal = sEl.value;
        const eVal = eEl.value;

        const presetEl = document.getElementById('date-preset');
        if (presetEl) presetEl.value = "";

        const isToday = (eVal === getTodayStr());
        const incEl = document.getElementById('include-today');
        if (incEl) incEl.checked = isToday;

        dateStateSet('start', sVal);
        dateStateSet('end', eVal);
        dateStateSet('preset', '');
        if (incEl) dateStateSet('include_today', isToday ? 'true' : 'false');

        nsRecomputeComparisonIfPreset();
        nsTriggerDataSync();
    };

    window.nsOnIncludeTodayChange = function () {
        const incEl = document.getElementById('include-today');
        const isChecked = incEl ? incEl.checked : false;
        const presetEl = document.getElementById('date-preset');
        const preset = presetEl ? presetEl.value : '';

        if (preset in MONTH_PRESET_SPANS) {
            return;
        }

        if (preset === 'all_time') {
            const earliest = getEarliestDate();
            const sEl = document.getElementById('start_date');
            const eEl = document.getElementById('end_date');
            if (earliest && sEl && eEl) {
                const now = new Date();
                if (!isChecked) {
                    now.setDate(now.getDate() - 1);
                }
                const eStr = formatLocal(now);
                sEl.value = earliest;
                eEl.value = eStr;
                dateStateSet('start', earliest);
                dateStateSet('end', eStr);
                if (incEl) dateStateSet('include_today', isChecked ? 'true' : 'false');
                nsRecomputeComparisonIfPreset();
                nsTriggerDataSync();
                return;
            }
        }

        if (preset === 'today' && !isChecked) {
            if (presetEl) presetEl.value = 'yesterday';
            dateStateSet('preset', 'yesterday');
            nsApplyPreset('yesterday');
            return;
        }

        if (preset === 'yesterday' && isChecked) {
            if (presetEl) presetEl.value = 'today';
            dateStateSet('preset', 'today');
            nsApplyPreset('today');
            return;
        }

        const sEl = document.getElementById('start_date');
        const eEl = document.getElementById('end_date');
        if (!sEl || !eEl || !sEl.value || !eEl.value) return;

        const start = parseLocal(sEl.value);
        const end = parseLocal(eEl.value);
        const todayLocal = getTodayStr();

        if (isChecked) {
            if (eEl.value < todayLocal) {
                start.setDate(start.getDate() + 1);
                end.setDate(end.getDate() + 1);
            }
        } else {
            start.setDate(start.getDate() - 1);
            end.setDate(end.getDate() - 1);
        }

        const sStr = formatLocal(start);
        const eStr = formatLocal(end);
        sEl.value = sStr;
        eEl.value = eStr;

        dateStateSet('start', sStr);
        dateStateSet('end', eStr);
        if (incEl) dateStateSet('include_today', isChecked ? 'true' : 'false');

        nsRecomputeComparisonIfPreset();
        nsTriggerDataSync();
    };

    window.nsShiftPeriod = function (direction) {
        const sEl = document.getElementById('start_date');
        const eEl = document.getElementById('end_date');
        const presetEl = document.getElementById('date-preset');
        if (!sEl || !eEl || !sEl.value || !eEl.value) return;

        const currentPreset = presetEl ? presetEl.value : '';
        const start = parseLocal(sEl.value);
        const end = parseLocal(eEl.value);

        let sStr, eStr, newPreset;

        if (currentPreset in MONTH_PRESET_SPANS) {
            const n = MONTH_PRESET_SPANS[currentPreset];
            const anchorIndex = end.getFullYear() * 12 + end.getMonth() + 1;
            const newAnchorIndex = anchorIndex + direction * n;
            const newAnchorYear = Math.floor(newAnchorIndex / 12);
            const newAnchorMonth = ((newAnchorIndex % 12) + 12) % 12;

            const newStart = new Date(newAnchorYear, newAnchorMonth - n, 1);
            const newEnd = new Date(newAnchorYear, newAnchorMonth, 0);

            sStr = formatLocal(newStart);
            eStr = formatLocal(newEnd);
            newPreset = currentPreset;
        } else {
            const spanDays = Math.round((end - start) / 86400000) + 1;
            start.setDate(start.getDate() + direction * spanDays);
            end.setDate(end.getDate() + direction * spanDays);
            sStr = formatLocal(start);
            eStr = formatLocal(end);
            newPreset = '';
        }

        // Same "no future" rule as nsSyncInputs -- "next period" past the
        // current one has nothing to show. If the whole computed period
        // would be in the future, treat it as a no-op rather than landing
        // on some clamped, no-longer-meaningful range; if only the end
        // overshoots, clamp just that.
        const today = getTodayStr();
        if (sStr > today) return;
        if (eStr > today) eStr = today;

        sEl.value = sStr;
        eEl.value = eStr;
        if (presetEl) presetEl.value = newPreset;

        const isToday = (eStr === getTodayStr());
        const incEl = document.getElementById('include-today');
        if (incEl) incEl.checked = isToday;

        dateStateSet('start', sStr);
        dateStateSet('end', eStr);
        dateStateSet('preset', newPreset);
        if (incEl) dateStateSet('include_today', isToday ? 'true' : 'false');

        nsRecomputeComparisonIfPreset();
        nsTriggerDataSync();
    };

    // ─── Comparison Section Support ────────────────────────────────────
    window.nsUpdateComparisonUiState = function () {
        const toggle = document.getElementById('compare-toggle');
        const compareSection = document.querySelector('.compare-section');
        if (!toggle || !compareSection) return;

        const isChecked = toggle.checked;
        compareSection.classList.toggle('is-disabled', !isChecked);

        const stepBtns = compareSection.querySelectorAll('.step-btn');
        stepBtns.forEach(btn => btn.disabled = !isChecked);

        const inputs = compareSection.querySelectorAll('.pill-date-input, select');
        inputs.forEach(input => input.disabled = !isChecked);
    };

    window.nsPersistLastPeriod = function () {
        const toggle = document.getElementById('compare-toggle');
        if (!toggle) return;

        const isVisible = toggle.checked;
        const sVal = document.getElementById('start_date-lastperiod')?.value || '';
        const eVal = document.getElementById('end_date-lastperiod')?.value || '';
        const presetVal = document.getElementById('date-preset-lastperiod')?.value || '';

        localStorage.setItem('ns_compare_enabled', isVisible ? 'true' : 'false');
        localStorage.setItem('ns_compare_start', sVal);
        localStorage.setItem('ns_compare_end', eVal);
        localStorage.setItem('ns_compare_preset', presetVal);
    };

    window.nsRecomputeComparisonIfPreset = function () {
        const lpPresetEl = document.getElementById('date-preset-lastperiod');
        if (!lpPresetEl) return;
        const lpPreset = lpPresetEl.value;
        if (!lpPreset) return;

        const mainStartVal = document.getElementById('start_date')?.value;
        if (!mainStartVal) return;

        const mainStart = parseLocal(mainStartVal);
        const lpEnd = new Date(mainStart);
        lpEnd.setDate(lpEnd.getDate() - 1);

        let lpStart;
        if (lpPreset in MONTH_PRESET_SPANS_LP) {
            const n = MONTH_PRESET_SPANS_LP[lpPreset];
            lpStart = new Date(lpEnd.getFullYear(), lpEnd.getMonth() - n + 1, 1);
        } else {
            const spanDays = parseInt(lpPreset, 10);
            lpStart = new Date(lpEnd);
            lpStart.setDate(lpStart.getDate() - spanDays + 1);
        }

        const lpSEl = document.getElementById('start_date-lastperiod');
        const lpEEl = document.getElementById('end_date-lastperiod');
        if (lpSEl) lpSEl.value = formatLocal(lpStart);
        if (lpEEl) lpEEl.value = formatLocal(lpEnd);

        nsPersistLastPeriod();
    };

    window.nsOnCompareToggleChange = function () {
        nsUpdateComparisonUiState();
        const toggle = document.getElementById('compare-toggle');
        if (toggle && toggle.checked) {
            const lpS = document.getElementById('start_date-lastperiod')?.value;
            const lpE = document.getElementById('end_date-lastperiod')?.value;
            if (!lpS || !lpE) {
                nsRecomputeComparisonIfPreset();
            }
        }
        nsPersistLastPeriod();
        nsTriggerDataSync();
    };

    window.nsOnPresetChangeLastPeriod = function () {
        const lpPreset = document.getElementById('date-preset-lastperiod')?.value;
        if (lpPreset) {
            nsRecomputeComparisonIfPreset();
        }
        nsPersistLastPeriod();
        nsTriggerDataSync();
    };

    window.nsSyncInputsLastPeriod = function () {
        const lpPresetEl = document.getElementById('date-preset-lastperiod');
        if (lpPresetEl) lpPresetEl.value = "";
        nsPersistLastPeriod();
        nsTriggerDataSync();
    };

    window.nsShiftPeriodLastPeriod = function (direction) {
        const sEl = document.getElementById('start_date-lastperiod');
        const eEl = document.getElementById('end_date-lastperiod');
        const presetEl = document.getElementById('date-preset-lastperiod');
        if (!sEl || !eEl || !sEl.value || !eEl.value) return;

        const currentPreset = presetEl ? presetEl.value : '';
        const start = parseLocal(sEl.value);
        const end = parseLocal(eEl.value);

        let sStr, eStr, newPreset;

        if (currentPreset in MONTH_PRESET_SPANS_LP) {
            const n = MONTH_PRESET_SPANS_LP[currentPreset];
            const anchorIndex = end.getFullYear() * 12 + end.getMonth() + 1;
            const newAnchorIndex = anchorIndex + direction * n;
            const newAnchorYear = Math.floor(newAnchorIndex / 12);
            const newAnchorMonth = ((newAnchorIndex % 12) + 12) % 12;

            const newStart = new Date(newAnchorYear, newAnchorMonth - n, 1);
            const newEnd = new Date(newAnchorYear, newAnchorMonth, 0);

            sStr = formatLocal(newStart);
            eStr = formatLocal(newEnd);
            newPreset = currentPreset;
        } else {
            const spanDays = Math.round((end - start) / 86400000) + 1;
            start.setDate(start.getDate() + direction * spanDays);
            end.setDate(end.getDate() + direction * spanDays);
            sStr = formatLocal(start);
            eStr = formatLocal(end);
            newPreset = '';
        }

        sEl.value = sStr;
        eEl.value = eStr;
        if (presetEl) presetEl.value = newPreset;

        nsPersistLastPeriod();
        nsTriggerDataSync();
    };

    // ─── Cross-Tab Storage Event Listener ──────────────────────────────
    window.addEventListener('storage', function (e) {
        if (!e.key || !e.key.startsWith('ns_')) return;

        if (e.key === dateStateKey('start') || e.key === dateStateKey('end') || e.key === dateStateKey('preset') || e.key === dateStateKey('include_today')) {
            const s = dateStateGet('start');
            const eDate = dateStateGet('end');
            const p = dateStateGet('preset');
            const inc = dateStateGet('include_today') === 'true';

            const sEl = document.getElementById('start_date');
            const eEl = document.getElementById('end_date');
            const pEl = document.getElementById('date-preset');
            const incEl = document.getElementById('include-today');

            if (sEl && s) sEl.value = s;
            if (eEl && eDate) eEl.value = eDate;
            if (pEl && p !== null) pEl.value = p;
            if (incEl) incEl.checked = inc;

            updateTriggerText();
            if (typeof window.syncAndSubmit === 'function') {
                window.syncAndSubmit();
            } else if (typeof window.onDateOrPresetChange === 'function') {
                window.onDateOrPresetChange();
            }
        } else if (e.key.startsWith('ns_compare_')) {
            const compToggle = document.getElementById('compare-toggle');
            if (compToggle) {
                compToggle.checked = localStorage.getItem('ns_compare_enabled') === 'true';
                const lpS = localStorage.getItem('ns_compare_start');
                const lpE = localStorage.getItem('ns_compare_end');
                const lpP = localStorage.getItem('ns_compare_preset');

                const lpSEl = document.getElementById('start_date-lastperiod');
                const lpEEl = document.getElementById('end_date-lastperiod');
                const lpPEl = document.getElementById('date-preset-lastperiod');

                if (lpSEl && lpS) lpSEl.value = lpS;
                if (lpEEl && lpE) lpEEl.value = lpE;
                if (lpPEl && lpP) lpPEl.value = lpP;

                nsUpdateComparisonUiState();
                updateTriggerText();
                if (typeof window.syncAndSubmit === 'function') {
                    window.syncAndSubmit();
                }
            }
        }
    });

    function migrateAndPurgeLegacyKeys() {
        try {
            // 1. Migrate primary date keys to canonical ns_date_* if not yet set
            if (!localStorage.getItem('ns_date_start')) {
                const legacyStart = localStorage.getItem('adv_start') || localStorage.getItem('omnipod_start') || localStorage.getItem('ns_start_date');
                const legacyEnd = localStorage.getItem('adv_end') || localStorage.getItem('omnipod_end') || localStorage.getItem('ns_end_date');
                const legacyPreset = localStorage.getItem('adv_preset') || localStorage.getItem('omnipod_preset');

                if (legacyStart && legacyEnd) {
                    localStorage.setItem('ns_date_start', legacyStart);
                    localStorage.setItem('ns_date_end', legacyEnd);
                    if (legacyPreset) localStorage.setItem('ns_date_preset', legacyPreset);
                }
            }

            // 2. Migrate comparison keys to canonical ns_compare_* if not yet set
            if (localStorage.getItem('ns_compare_enabled') === null && localStorage.getItem('adv_lp_visible') !== null) {
                const lpVis = localStorage.getItem('adv_lp_visible');
                localStorage.setItem('ns_compare_enabled', (lpVis === 'true' || lpVis === '1') ? 'true' : 'false');
            }
            if (!localStorage.getItem('ns_compare_start') && localStorage.getItem('adv_lp_start')) {
                localStorage.setItem('ns_compare_start', localStorage.getItem('adv_lp_start'));
            }
            if (!localStorage.getItem('ns_compare_end') && localStorage.getItem('adv_lp_end')) {
                localStorage.setItem('ns_compare_end', localStorage.getItem('adv_lp_end'));
            }
            if (!localStorage.getItem('ns_compare_preset') && localStorage.getItem('adv_lp_preset')) {
                localStorage.setItem('ns_compare_preset', localStorage.getItem('adv_lp_preset'));
            }

            // 3. Atomically purge all legacy and obsolete keys from localStorage
            const legacyKeys = [
                'adv_start', 'adv_end', 'adv_preset',
                'omnipod_start', 'omnipod_end', 'omnipod_preset',
                'ns_start_date', 'ns_end_date',
                'adv_lp_visible', 'adv_lp_start', 'adv_lp_end', 'adv_lp_preset', 'adv_lp_include_today'
            ];
            legacyKeys.forEach(k => localStorage.removeItem(k));
        } catch (e) {
            console.warn('[ns-date-selector] Legacy key purge failed:', e);
        }
    }

    // ─── Initializer ───────────────────────────────────────────────────
    window.nsInitDateSelector = function () {
        migrateAndPurgeLegacyKeys();

        const sEl = document.getElementById('start_date');
        const eEl = document.getElementById('end_date');
        if (!sEl || !eEl) return;

        const initialStart = sEl.value;
        const initialEnd = eEl.value;

        const wrap = document.querySelector('.date-picker-wrap');
        if (wrap && wrap.dataset && wrap.dataset.earliestDate) {
            window.nsEarliestCgmDate = wrap.dataset.earliestDate;
        }

        // Isolated pages must never inherit the shared date selection.
        const isolatedDateState = !!dateStateNamespace();
        const savedStart = dateStateGet('start');
        const savedEnd = dateStateGet('end');
        const savedPreset = dateStateGet('preset');
        const incStored = dateStateGet('include_today');

        if (savedPreset === 'all_time') {
            const inc = (incStored !== null) ? (incStored === 'true') : true;
            const incEl = document.getElementById('include-today');
            if (incEl) incEl.checked = inc;

            const earliest = getEarliestDate() || savedStart;
            const now = new Date();
            if (!inc) now.setDate(now.getDate() - 1);
            const eStr = formatLocal(now);
            const sStr = earliest || savedStart;

            if (sStr && eStr) {
                sEl.value = sStr;
                eEl.value = eStr;
                dateStateSet('start', sStr);
                dateStateSet('end', eStr);
                dateStateSet('preset', 'all_time');
                if (incEl) dateStateSet('include_today', inc ? 'true' : 'false');
            }

            const pEl = document.getElementById('date-preset');
            if (pEl) pEl.value = 'all_time';
        } else if (savedStart && savedEnd) {
            sEl.value = savedStart;
            eEl.value = savedEnd;
            dateStateSet('start', savedStart);
            dateStateSet('end', savedEnd);

            const isToday = (savedEnd === getTodayStr());
            const incEl = document.getElementById('include-today');
            if (incEl) {
                incEl.checked = (incStored !== null) ? (incStored === 'true') : isToday;
            }

            const pEl = document.getElementById('date-preset');
            if (pEl) {
                if (savedPreset) {
                    pEl.value = savedPreset;
                } else {
                    const start = parseLocal(savedStart);
                    const end = parseLocal(savedEnd);
                    const span = Math.round((end - start) / 86400000) + 1;
                    const presetVal = [7, 14, 30, 60, 90, 180].includes(span) ? span.toString() : "";
                    pEl.value = presetVal;
                }
            }
        } else if (savedPreset && savedPreset !== "custom") {
            nsApplyPreset(savedPreset, false);
        } else if (isolatedDateState && initialStart && initialEnd) {
            // HbA1c deliberately has its own date state.  On its first visit
            // there is no saved state, so retain the server-provided range
            // instead of silently replacing it with the generic seven days.
            dateStateSet('start', initialStart);
            dateStateSet('end', initialEnd);
            const incEl = document.getElementById('include-today');
            if (incEl) {
                incEl.checked = initialEnd === getTodayStr();
                dateStateSet('include_today', incEl.checked ? 'true' : 'false');
            }
        } else {
            nsApplyPreset('7', false);
        }

        // Initialize Comparison section if present
        const compToggle = document.getElementById('compare-toggle');
        if (compToggle) {
            const lpVisible = localStorage.getItem('ns_compare_enabled') === 'true';
            const lpStart = localStorage.getItem('ns_compare_start');
            const lpEnd = localStorage.getItem('ns_compare_end');
            const lpPreset = localStorage.getItem('ns_compare_preset') || '7';

            compToggle.checked = lpVisible;
            const lpPEl = document.getElementById('date-preset-lastperiod');
            const lpSEl = document.getElementById('start_date-lastperiod');
            const lpEEl = document.getElementById('end_date-lastperiod');

            if (lpPEl && lpPreset) lpPEl.value = lpPreset;
            if (lpSEl && lpStart) lpSEl.value = lpStart;
            if (lpEEl && lpEnd) lpEEl.value = lpEnd;

            nsUpdateComparisonUiState();
            nsRecomputeComparisonIfPreset();
        }

        const finalStart = sEl.value;
        const finalEnd = eEl.value;
        updateTriggerText();
        updateNavLinks(finalStart, finalEnd);

        if (initialStart && initialEnd && (initialStart !== finalStart || initialEnd !== finalEnd)) {
            nsTriggerDataSync();
        }
    };

    // Auto-init when DOM is loaded
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', nsInitDateSelector);
    } else {
        nsInitDateSelector();
    }

    window.addEventListener('pageshow', function () {
        const sEl = document.getElementById('start_date');
        const eEl = document.getElementById('end_date');
        if (!sEl || !eEl) return;
        const savedStart = dateStateGet('start');
        const savedEnd = dateStateGet('end');
        if (savedStart && savedEnd && (sEl.value !== savedStart || eEl.value !== savedEnd)) {
            nsInitDateSelector();
            nsTriggerDataSync();
        }
    });
})();

// -----------------------------------------------------------------------
// Shared date-range helpers. Called from multiple pages (agp.html,
// timeline.html, and likely others) as bare globals -- formatDMY() and
// getChartParams() were never actually defined anywhere in the codebase
// despite being called from at least two pages (each guarded by its own
// try/catch, which silently masked the failure rather than fixing it).
// Genuinely shared, not page-local -- belongs here with the rest of the
// date-selector logic, not duplicated per page. 28 Aug 2026.
// -----------------------------------------------------------------------

/**
 * Formats an ISO date string (YYYY-MM-DD) as DD/MM/YYYY for display.
 * Distinct from Trace's page-local formatDMYY(), which truncates to a
 * 2-digit year -- this one keeps the full year.
 */
function formatDMY(isoStr) {
    if (!isoStr) return '';
    const parts = String(isoStr).split('-');
    if (parts.length !== 3) return isoStr;
    return `${parts[2]}/${parts[1]}/${parts[0]}`;
}

/**
 * Reads the current start_date/end_date input values (the shared
 * _header_date_selector.html inputs every page using this module
 * includes) and returns them as a URL query string -- consumable both
 * as `?${getChartParams()}` and as `new URLSearchParams(getChartParams())`.
 */
function getChartParams() {
    const startEl = document.getElementById('start_date');
    const endEl = document.getElementById('end_date');
    const params = new URLSearchParams();
    if (startEl && startEl.value) params.set('start_date', startEl.value);
    if (endEl && endEl.value) params.set('end_date', endEl.value);
    return params.toString();
}
