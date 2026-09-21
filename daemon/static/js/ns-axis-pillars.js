/*
 * ============================================================================
 * ns-axis-pillars.js — THE ONE axis-drag/scroll/reset implementation
 * ============================================================================
 *
 * If you are about to write code that lets someone drag, scroll-wheel, or
 * double-click an axis to change its range on ANY chart page in this app —
 * STOP. That code already exists, here, and is shared by Patterns (agp.html),
 * Trends (timeline.html) and Trace (dashboard_daily.html). Extend this file
 * and its per-page config, or ask Harry before writing a fourth copy.
 *
 * WHY THIS FILE EXISTS
 * ---------------------
 * Until 27 Aug 2026 each of the three chart pages had its own ~150-200 line
 * copy of this: drag-lock state, wheel handling, double-click reset, the
 * floating value hint, and a clamp table. The copies drifted:
 *   - Trends alone had data-driven auto-bounds and a debounced wheel;
 *     Patterns/Trace lacked both.
 *   - Trends' Y-axis double-click reset broke because its own re-render
 *     destroyed the DOM pillar mid-gesture -- a bug the other two pages
 *     never had because their code happened to be structured differently.
 *   - Trace's clamp table floored the Bolus/IOB axis at zero, silently
 *     breaking negative IOB (extended suspends, temp-basal accounting)
 *     because Bolus and IOB share one axis and only Bolus is genuinely
 *     always >= 0. Nobody could see that in one place; it took a live bug
 *     report to find it.
 * Three copies of gesture code is how a fix to one becomes a bug in the
 * other two. See daemon/static/js/ns-view-manager.js for the exact same
 * story with the saved-views dropdown -- same lesson, same fix shape.
 *
 * WHAT LIVES HERE vs WHAT STAYS ON THE PAGE
 * -------------------------------------------
 * This module owns: bound storage (in-memory + optional localStorage),
 * clamping, the drag/wheel/double-click gesture engine, the floating hint
 * badge, and (for Patterns/Trends/Trace's shared "stacked 54px pillar"
 * layout) the DOM positioning of the pillars themselves.
 *
 * Each page still owns: its own series/axis-group definitions, computing
 * the *data-driven* auto bounds every render (that logic is inherently
 * different per page -- different metrics, different data shapes -- and
 * does not belong here), and re-rendering its own chart library instance.
 * Patterns' pillar *layout* is also page-owned (a fixed left glucose pillar
 * plus legend-driven right pillars, structurally unlike Trends/Trace's
 * axis-index-map layout) -- it calls NSAxisPillars.createPillar() directly
 * rather than the shared renderPillars() layout helper.
 *
 * HOW TO EXTEND THIS FOR A NEW METRIC OR A NEW PAGE
 * ---------------------------------------------------
 * - New metric sharing an existing axis group with a genuinely different
 *   valid range (the IOB/Bolus situation): add or adjust an entry in that
 *   page's `clamp` config passed to init(). Do not add a special case
 *   inside this file for one page's metric.
 * - New chart page needing axis drag: call NSAxisPillars.init() once with
 *   this page's clamp table, storage key (or null for session-only, see
 *   below), and the onLiveUpdate/onCommit callbacks. Use renderPillars()
 *   if your axis layout matches Trends/Trace's stacked-54px convention,
 *   otherwise call createPillar() directly the way Patterns does.
 *
 * DELIBERATE PER-PAGE DIFFERENCES (do not "fix" these to match each other)
 * ---------------------------------------------------------------------------
 * - storageKey: Trends persists manually-set bounds to localStorage
 *   independently of saved views (`ns_trends_yaxis_bounds`) so a tweak
 *   survives a reload even with no view active. Patterns and Trace do not
 *   have a standalone key -- their manual bounds are session-only unless
 *   captured into a saved view. This was true before the extraction and is
 *   preserved as-is; changing it is a product decision, not a refactor.
 * - clamp tables differ per page because the metrics differ. A clamp is a
 *   floor/ceiling on a *value*, so it must be judged per metric, not
 *   copied from another page.
 *
 * BEHAVIOUR CHANGE MADE DURING THIS EXTRACTION (27 Aug 2026)
 * --------------------------------------------------------------
 * getEffectiveBounds() now prefers the page's data-driven calculated bounds
 * over its static per-series defaultMin/defaultMax when both are present.
 * Trends' pre-extraction code did the opposite (defaultMin/Max won), which
 * meant "reset to auto" on any series with `defaultMin: 0` (TIR, TITR, TDD,
 * Carbs, GVI, LBGI, HBGI, Avg BG, CV) always reset the lower bound to a flat
 * 0 instead of fitting the real data range -- silently defeating the auto-fit
 * that Trends' own lastComputedBounds machinery had already computed.
 * defaultMin/defaultMax are still used, but now only as the fallback when no
 * calculated bounds exist yet (e.g. before first data load), and inside
 * clamp() as an absolute floor/ceiling regardless of data or manual input.
 */
(function (global) {
    'use strict';

    var cfg = null;
    var liveBounds = {};
    var activeDragLock = null;
    var wheelEndTimer = null;
    var hintHideTimer = null;
    var hintEl = null;

    // --- Storage -------------------------------------------------------------

    function loadStoredBounds() {
        if (!cfg.storageKey) return {};
        try {
            return JSON.parse(localStorage.getItem(cfg.storageKey) || '{}');
        } catch (e) {
            return {};
        }
    }

    function persistBounds() {
        if (!cfg.storageKey) return; // session-only page (Patterns, Trace): by design, see file banner
        localStorage.setItem(cfg.storageKey, JSON.stringify(liveBounds));
    }

    // --- Bound getters/setters (delegate targets for each page's own
    //     get/set/reset function names -- see file banner) --------------------

    function getCustomBound(group, type) {
        if (liveBounds[group] && liveBounds[group][type] !== undefined && liveBounds[group][type] !== null) {
            return liveBounds[group][type];
        }
        return null;
    }

    // persist: true writes to storage (if configured) and fires onCommit --
    // used for a finished gesture or a typed value. false is for the live
    // in-flight value during a drag/wheel tick: onLiveUpdate re-renders the
    // chart cheaply from cached data, but nothing is written to storage or
    // to the saved-view dirty flag until the gesture actually ends.
    function setCustomBound(group, type, val, persist) {
        if (!liveBounds[group]) liveBounds[group] = {};
        liveBounds[group][type] =
            (val === null || val === undefined || isNaN(val) || val === '') ? null : parseFloat(val);
        if (persist) {
            persistBounds();
            if (cfg.onCommit) cfg.onCommit();
        }
    }

    function resetBound(group) {
        delete liveBounds[group];
        persistBounds();
        if (cfg.onLiveUpdate) cfg.onLiveUpdate();
        if (cfg.onCommit) cfg.onCommit();
    }

    function getAllBounds() {
        return JSON.parse(JSON.stringify(liveBounds));
    }

    // Used by each page's saved-views applyState() hook to load a view's
    // captured bounds wholesale. Does not itself trigger a render or mark a
    // view dirty -- the caller is already mid-applyState and will render once
    // at the end, so doing it here would be a second, redundant render.
    function setAllBounds(bounds) {
        liveBounds = bounds ? JSON.parse(JSON.stringify(bounds)) : {};
        persistBounds();
    }

    // axisInfo: { group, decimals, defaultMin, defaultMax, calculatedMin,
    //             calculatedMax }. calculatedMin/Max are this page's
    // data-driven auto bounds for the current render, if it computed any.
    // Precedence: manual override > data-driven auto bounds > static
    // per-series default > hardcoded 0/10 last resort. See the "BEHAVIOUR
    // CHANGE" note in the file banner for why calculated now outranks default.
    function getEffectiveBounds(axisInfo) {
        var group = axisInfo.group;
        var customMin = getCustomBound(group, 'min');
        var customMax = getCustomBound(group, 'max');

        var min = customMin !== null ? customMin
            : (axisInfo.calculatedMin !== undefined ? axisInfo.calculatedMin
            : (axisInfo.defaultMin !== undefined ? axisInfo.defaultMin : 0));
        var max = customMax !== null ? customMax
            : (axisInfo.calculatedMax !== undefined ? axisInfo.calculatedMax
            : (axisInfo.defaultMax !== undefined ? axisInfo.defaultMax : 10));

        if (max <= min) max = min + 1;
        return { min: min, max: max };
    }

    // --- Clamping --------------------------------------------------------------
    //
    // cfg.clamp is a plain table: { groupName: { min: number|null, max: number|null } }.
    // A missing entry, or a missing min/max within an entry, means "no floor/
    // ceiling on that side" -- not "floor at 0". Deliberately a declarative
    // table rather than an if/else chain: the Bolus/IOB bug this extraction
    // fixed was exactly an if/else chain that floored a whole axis *group* at
    // zero without noticing the group held two metrics with different valid
    // ranges. A table makes "what is this group's floor and why" a one-line
    // lookup instead of a branch buried in a function, and makes a
    // shared-axis situation like Bolus/IOB visible at a glance when adding a
    // new metric to an existing group.
    function clampBound(group, isUpper, val) {
        if (val === null || val === undefined || isNaN(val)) return null;
        var v = parseFloat(val);
        var rule = (cfg.clamp || {})[group];
        if (rule) {
            if (!isUpper && rule.min !== undefined && rule.min !== null && v < rule.min) v = rule.min;
            if (isUpper && rule.max !== undefined && rule.max !== null && v > rule.max) v = rule.max;
        }
        return Math.round(v * 100) / 100;
    }

    // --- Floating value hint ("GMI (%): Upper Limit (Max) = 8.2") --------------

    function showHint(axisInfo, isUpper, clientX, clientY) {
        if (!hintEl) return;
        var bounds = getEffectiveBounds(axisInfo);
        var val = isUpper ? bounds.max : bounds.min;
        var formatted = (axisInfo.decimals !== undefined && typeof val === 'number')
            ? val.toFixed(axisInfo.decimals)
            : (Math.round(val * 10) / 10);

        var titlePart = isUpper ? 'Upper Limit (Max)' : 'Lower Limit (Min)';
        if (axisInfo.inverse) {
            titlePart = isUpper ? 'Depth (Max)' : 'Ceiling (Min)';
        }

        hintEl.innerHTML = '<span style="color:' + axisInfo.color + ';">' + axisInfo.name + '</span>: ' +
            titlePart + ' = <b>' + formatted + '</b>';
        hintEl.style.left = clientX + 'px';
        hintEl.style.top = clientY + 'px';
        hintEl.style.display = 'block';

        clearTimeout(hintHideTimer);
        hintHideTimer = setTimeout(hideHint, 1200);
    }

    function showResetHint(axisInfo, clientX, clientY) {
        if (!hintEl) return;
        hintEl.innerHTML = '<span style="color:' + axisInfo.color + ';">' + axisInfo.name + '</span>: Axis bounds <b>Reset to Auto</b>';
        hintEl.style.left = clientX + 'px';
        hintEl.style.top = clientY + 'px';
        hintEl.style.display = 'block';
        clearTimeout(hintHideTimer);
        hintHideTimer = setTimeout(hideHint, 1200);
    }

    function hideHint() {
        if (hintEl) hintEl.style.display = 'none';
    }

    // --- Pillar element: one per active axis, split into an upper (drag/
    //     scroll = Max) and lower (drag/scroll = Min) half. A pillar is a
    //     transparent DOM element stacked over the chart canvas at the
    //     screen coordinates of one axis, because the chart itself is a
    //     <canvas> with no real elements to attach axis-specific listeners
    //     to. See daemon/templates/agp.html's updateAgpAxisPillars() for an
    //     example of a page computing its own left/top/width/height per
    //     axis and calling createPillar() directly rather than using
    //     renderPillars() below. ---------------------------------------------

    function createPillar(axisInfo, left, top, width, height) {
        var pillar = document.createElement('div');
        pillar.className = 'axis-pillar';
        pillar.style.position = 'absolute';
        pillar.style.left = left + 'px';
        pillar.style.top = top + 'px';
        pillar.style.width = width + 'px';
        pillar.style.height = height + 'px';
        pillar.style.pointerEvents = 'auto';
        pillar.style.display = 'flex';
        pillar.style.flexDirection = 'column';

        var isInv = !!axisInfo.inverse;
        var upperHalf = document.createElement('div');
        upperHalf.className = 'axis-pillar-half upper';
        upperHalf.style.flex = '1';
        upperHalf.style.cursor = 'n-resize';
        upperHalf.title = axisInfo.name + ': Drag or scroll ' + (isInv ? 'Ceiling (Min)' : 'Upper Limit (Max)') + '. Double-click to reset.';
        attachPillarEvents(upperHalf, axisInfo, isInv ? false : true, height);

        var lowerHalf = document.createElement('div');
        lowerHalf.className = 'axis-pillar-half lower';
        lowerHalf.style.flex = '1';
        lowerHalf.style.cursor = 's-resize';
        lowerHalf.title = axisInfo.name + ': Drag or scroll ' + (isInv ? 'Depth (Max)' : 'Lower Limit (Min)') + '. Double-click to reset.';
        attachPillarEvents(lowerHalf, axisInfo, isInv ? true : false, height);

        pillar.appendChild(upperHalf);
        pillar.appendChild(lowerHalf);
        return pillar;
    }

    function stepFor(axisInfo) {
        // Calibrated discrete step per metric precision: 0 decimals -> 1 unit
        // (TIR, TITR, Carbs, Low Events...), 1 decimal -> 0.1 unit (GMI, Avg
        // BG, CV, TDD...), 2 decimals -> 0.05 unit (GVI, Risk, ISF...).
        if (axisInfo.decimals === 0) return 1.0;
        if (axisInfo.decimals === 2) return 0.05;
        return 0.1;
    }

    function attachPillarEvents(halfEl, axisInfo, isUpper, plotHeight) {
        halfEl.addEventListener('wheel', function (e) {
            e.preventDefault();
            e.stopPropagation();

            var bounds = getEffectiveBounds(axisInfo);
            var baseStep = stepFor(axisInfo);
            var step = baseStep * (e.deltaY > 0 ? 1 : -1);
            if (axisInfo.inverse) step = -step;

            if (isUpper) {
                var newMax = bounds.max + step;
                if (newMax <= bounds.min + 2 * baseStep) newMax = bounds.min + 2 * baseStep;
                setCustomBound(axisInfo.group, 'max', clampBound(axisInfo.group, true, newMax), false);
            } else {
                var newMin = bounds.min - step;
                if (newMin >= bounds.max - 2 * baseStep) newMin = bounds.max - 2 * baseStep;
                setCustomBound(axisInfo.group, 'min', clampBound(axisInfo.group, false, newMin), false);
            }

            if (cfg.onLiveUpdate) cfg.onLiveUpdate();
            showHint(axisInfo, isUpper, e.clientX, e.clientY);

            // Debounced commit: a single trackpad flick dispatches 30-60 raw
            // wheel events in under half a second (see ns-view-manager.js-era
            // gotcha notes in STATUS.md for the same trap in a drag context).
            // Persisting and marking the view dirty on every one of those
            // events is wasted work and, for a page with a storageKey, wasted
            // localStorage writes. Settle for cfg.wheelDebounceMs of quiet
            // before committing once.
            clearTimeout(wheelEndTimer);
            wheelEndTimer = setTimeout(function () {
                persistBounds();
                if (cfg.onCommit) cfg.onCommit();
            }, cfg.wheelDebounceMs);
        }, { passive: false });

        // pointerdown covers mouse, touch, and pen through one path (3 Sep
        // 2026 iPad touch pass -- see docs/analysis/ipad-touch-support-plan.md).
        // e.button === 0 is correct for touch too: touch pointerdown always
        // reports button 0 (the "primary" button), same as a mouse left-click.
        halfEl.addEventListener('pointerdown', function (e) {
            if (e.button !== 0) return;
            e.preventDefault();
            e.stopPropagation();

            // Pointer capture keeps move/up events targeted at this half even
            // if a finger drifts off its 54px width mid-drag -- more reliable
            // on touch than relying on window-level listeners alone (which we
            // still keep, see onPointerMove/onPointerUp, for pointer types/
            // browsers where capture isn't honoured).
            try { halfEl.setPointerCapture(e.pointerId); } catch (err) {}

            var bounds = getEffectiveBounds(axisInfo);
            activeDragLock = {
                axisInfo: axisInfo,
                isUpper: isUpper,
                startY: e.clientY,
                startMin: bounds.min,
                startMax: bounds.max,
                plotHeight: plotHeight,
                moved: false
            };
            document.body.style.userSelect = 'none';
        });

        // Double-click (mouse) resets this axis to its auto bounds. resetBound()
        // already re-renders (onLiveUpdate) and commits (onCommit) internally
        // -- do not add a second render call here. A second render was
        // exactly the 27 Aug Trends bug: the page's render function rebuilds
        // the pillar container via innerHTML, which destroys the pillar
        // element the browser is mid-double-click on. If that destruction
        // happens between the first and second click, the two clicks land on
        // different DOM elements, the browser never dispatches `dblclick` to
        // either, and the reset silently never fires. One render call here,
        // full stop.
        halfEl.addEventListener('dblclick', function (e) {
            e.preventDefault();
            e.stopPropagation();
            activeDragLock = null;
            document.body.style.userSelect = '';
            resetBound(axisInfo.group);
            showResetHint(axisInfo, e.clientX, e.clientY);
        });

        // Double-tap (touch/pen) is dblclick's equivalent -- native dblclick
        // synthesis from a double-tap is not reliable enough across browsers
        // to depend on, so this is detected explicitly. Gated on pointerType
        // !== 'mouse' so mouse users keep the exact dblclick path above,
        // unchanged. Only counts as a tap (for double-tap purposes) if this
        // gesture never crossed the drag threshold -- see the `moved` flag
        // set in onPointerMove -- so a real drag followed by a quick
        // adjustment can never misfire a reset.
        var lastTapTime = 0, lastTapX = 0, lastTapY = 0;
        halfEl.addEventListener('pointerup', function (e) {
            if (e.pointerType === 'mouse') return;
            if (activeDragLock && activeDragLock.moved) return;

            var now = Date.now();
            var dx = e.clientX - lastTapX, dy = e.clientY - lastTapY;
            var dist = Math.sqrt(dx * dx + dy * dy);

            if (now - lastTapTime < 300 && dist < 20) {
                activeDragLock = null;
                document.body.style.userSelect = '';
                resetBound(axisInfo.group);
                showResetHint(axisInfo, e.clientX, e.clientY);
                lastTapTime = 0;
            } else {
                lastTapTime = now;
                lastTapX = e.clientX;
                lastTapY = e.clientY;
            }
        });
    }

    // --- Shared stacked-pillar layout (Trends/Trace convention: axes stack
    //     outward from the plot in 54px slots, left and right independently).
    //     Patterns does not use this -- its layout is legend-driven with a
    //     fixed left pillar, so it calls createPillar() directly. -------------

    function renderPillars(containerId, chartNodeId, layout) {
        var container = document.getElementById(containerId);
        var chartNode = document.getElementById(chartNodeId);
        if (!container || !chartNode || !layout) return;
        container.innerHTML = '';

        var width = chartNode.clientWidth;
        var height = chartNode.clientHeight;
        var top = layout.top || 40;
        var bottom = layout.bottom || 60;
        var plotHeight = height - top - bottom;

        (layout.activeLeftAxes || []).forEach(function (axisInfo, i) {
            var rightX = layout.left - (i * 54);
            var leftX = Math.max(0, rightX - 54);
            container.appendChild(createPillar(axisInfo, leftX, top, 54, plotHeight));
        });

        (layout.activeRightAxes || []).forEach(function (axisInfo, i) {
            var leftX = width - layout.right + (i * 54);
            container.appendChild(createPillar(axisInfo, leftX, top, 54, plotHeight));
        });
    }

    // --- Global drag gesture (one set of listeners per page, shared across
    //     every pillar -- mousedown on a pillar half sets activeDragLock,
    //     these listeners do the rest regardless of which axis is locked) ----

    function onPointerMove(e) {
        if (!activeDragLock) return;
        e.preventDefault();
        var lock = activeDragLock;
        var deltaY = e.clientY - lock.startY;

        // Ignore sub-threshold movement so a plain click/tap (which fires a
        // tiny move before up on most trackpads and every touch) doesn't
        // register as a drag. Trends had this; Patterns/Trace didn't -- now
        // all three do. `moved` is also this gesture's signal to the
        // double-tap handler above that a real drag happened, not a tap.
        if (Math.abs(deltaY) < cfg.dragThresholdPx) return;
        lock.moved = true;

        var baseStep = stepFor(lock.axisInfo);
        // 25px of physical mouse movement = exactly one step unit.
        var deltaVal = (-deltaY / 25) * baseStep;
        if (lock.axisInfo.inverse) deltaVal = -deltaVal;

        if (lock.isUpper) {
            var newMax = lock.startMax + deltaVal;
            if (newMax <= lock.startMin + 2 * baseStep) newMax = lock.startMin + 2 * baseStep;
            setCustomBound(lock.axisInfo.group, 'max', clampBound(lock.axisInfo.group, true, newMax), false);
        } else {
            var newMin = lock.startMin + deltaVal;
            if (newMin >= lock.startMax - 2 * baseStep) newMin = lock.startMax - 2 * baseStep;
            setCustomBound(lock.axisInfo.group, 'min', clampBound(lock.axisInfo.group, false, newMin), false);
        }

        if (cfg.onLiveUpdate) cfg.onLiveUpdate();
        showHint(lock.axisInfo, lock.isUpper, e.clientX, e.clientY);
    }

    function onPointerUp() {
        if (!activeDragLock) return;
        activeDragLock = null;
        document.body.style.userSelect = '';
        persistBounds();
        // Deliberately no onLiveUpdate() call here -- see the dblclick
        // comment above. The chart is already current from the live
        // pointermove renders; re-rendering again here would rebuild the
        // pillar container and risk the same double-click race.
        //
        // Also used directly as the pointercancel handler (see init()): an
        // iPadOS system gesture (Control Center, dock swipe, palm rejection)
        // interrupting a drag fires pointercancel, not pointerup. Without
        // this, activeDragLock would stay stuck set and further scale input
        // would silently do nothing until reload.
        if (cfg.onCommit) cfg.onCommit();
    }

    // --- Init --------------------------------------------------------------

    function init(config) {
        cfg = Object.assign({
            clamp: {},
            storageKey: null,
            hintElId: 'axis-scale-hint',
            wheelDebounceMs: 300,
            dragThresholdPx: 3,
            onLiveUpdate: function () {},
            onCommit: function () {}
        }, config || {});

        hintEl = document.getElementById(cfg.hintElId);
        liveBounds = loadStoredBounds();

        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('pointerup', onPointerUp);
        window.addEventListener('pointercancel', onPointerUp);
    }

    global.NSAxisPillars = {
        init: init,
        getCustomBound: getCustomBound,
        setCustomBound: setCustomBound,
        resetBound: resetBound,
        getAllBounds: getAllBounds,
        setAllBounds: setAllBounds,
        getEffectiveBounds: getEffectiveBounds,
        clampBound: clampBound,
        createPillar: createPillar,
        renderPillars: renderPillars,
        hideHint: hideHint
    };
})(window);
