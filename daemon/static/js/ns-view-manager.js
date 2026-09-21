/*
 * ns-view-manager.js — shared saved-views manager for the chart pages.
 *
 * Patterns (agp.html), Trends (timeline.html) and Trace (dashboard_daily.html)
 * each had their own near-identical copy of this: ~15 functions, ~250 lines,
 * three times over. One copy now; page differences go in the config object.
 *
 * Phase 8 Migration:
 * - Storage backend is now PostgreSQL via /api/v1/saved_views?page=...
 * - Implements the In-Memory Cache Pattern: initial async fetch populates cache,
 *   keeping getAllViews(), findView(), and renderViewControls() completely synchronous.
 * - Implements Touch-One Materialization (§3.1): built-ins stay as live code defaults
 *   until edited/deleted, at which point only that one view materializes in the DB.
 * - activeViewKey stays in localStorage as ephemeral per-session state.
 *
 * Required DOM (identical on all three pages):
 *   #view-pill-btn  #active-view-label  #view-dropdown-popover  #all-views-list
 *   #view-save-prompt-row  #view-save-input-row  #new-view-name
 *   #tab-context-menu  #tab-context-header  #ctx-delete
 *
 * Usage:
 *   NSViews.init({
 *       page:          'trends', // 'trace' | 'trends' | 'patterns'
 *       activeViewKey: 'ns_trends_active_view',
 *       defaultViews:  DEFAULT_TRENDS_VIEWS,
 *       titleElId:     'trends-chart-title-text',
 *       titleFor:      (view) => ...,
 *       settingsPopoverId: 'trends-settings-popover',
 *       settingsBtnId:     'trends-settings-btn',
 *       captureState:  () => ({ grain, legend, types, bounds }),
 *       applyState:    (view) => { ...page applies the view and re-renders... }
 *   });
 */
(function (global) {
    'use strict';

    var cfg = null;
    var contextTargetViewId = null;
    var cachedViews = null;
    var dbRows = [];

    function el(id) {
        return document.getElementById(id);
    }

    // --- Merge & Materialization Logic (§3.1) -------------------------------

    function mergeViews(rows) {
        var defaults = (cfg && cfg.defaultViews) ? cfg.defaultViews : [];
        var merged = [];

        // For each built-in in cfg.defaultViews:
        defaults.forEach(function (def) {
            var match = (rows || []).find(function (r) {
                return r.source_default_id && String(r.source_default_id) === String(def.id);
            });
            if (!match) {
                // No match -> show unmaterialized code version
                merged.push(Object.assign({}, def, {
                    id: String(def.id),
                    isDefault: true,
                    dbId: null,
                    source_default_id: null
                }));
            } else if (!match.is_deleted) {
                // Match, is_deleted = false -> DB materialized version wins
                // Unpack payload directly onto the view object
                var item = Object.assign(
                    {
                        id: String(match.id),
                        name: match.name,
                        isDefault: true,
                        dbId: match.id,
                        source_default_id: match.source_default_id
                    },
                    match.payload || {}
                );
                merged.push(item);
            }
            // Match, is_deleted = true -> omitted (tombstone)
        });

        // Append all normal custom views (source_default_id is null/empty and is_deleted is false)
        (rows || []).forEach(function (r) {
            if (!r.source_default_id && !r.is_deleted) {
                var item = Object.assign(
                    {
                        id: String(r.id),
                        name: r.name,
                        isDefault: false,
                        dbId: r.id,
                        source_default_id: null
                    },
                    r.payload || {}
                );
                merged.push(item);
            }
        });

        return merged;
    }

    // --- Storage & Cache (Synchronous Interface) ---------------------------

    function getAllViews() {
        if (cachedViews !== null) return cachedViews;
        return mergeViews(dbRows);
    }

    function saveAllViews(views) {
        // Kept for backward compatibility
        cachedViews = views || [];
        renderViewControls();
    }

    function getActiveViewId() {
        if (!cfg || !cfg.activeViewKey) return null;
        return localStorage.getItem(cfg.activeViewKey);
    }

    function getDefaultView() {
        var views = getAllViews();
        return (views && views.length > 0) ? views[0] : null;
    }

    function getActiveView() {
        var id = getActiveViewId();
        if (id) {
            var v = findView(id);
            if (v) return v;
        }
        return getDefaultView();
    }

    function setActiveViewId(id) {
        if (!cfg || !cfg.activeViewKey) return;
        if (id) {
            localStorage.setItem(cfg.activeViewKey, String(id));
        } else {
            localStorage.removeItem(cfg.activeViewKey);
        }
    }

    function findView(id) {
        return getAllViews().find(function (v) { return String(v.id) === String(id); }) || null;
    }

    function fetchViewsFromDb() {
        if (!cfg || !cfg.page) return Promise.resolve();
        return fetch('/api/v1/saved_views?page=' + encodeURIComponent(cfg.page))
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (data) {
                if (Array.isArray(data)) {
                    dbRows = data;
                    cachedViews = mergeViews(dbRows);

                    // Apply the persisted active view now that DB-backed views
                    // actually exist. init() only has the built-in defaults in
                    // cache, so a user-saved view could never be found -- and
                    // applyState() is otherwise only reachable from applyView(),
                    // i.e. a click. The result was a page that showed the saved
                    // view's NAME in the title while rendering page defaults,
                    // and only came right when the view was re-selected by hand.
                    // Pages' applyState re-render themselves if their data
                    // buffer is populated, and are a render no-op if it isn't
                    // (the pending initial load then picks up the state), so
                    // this is safe whichever fetch wins the race.
                    var activeId = getActiveViewId();
                    var restored = activeId ? findView(activeId) : null;
                    if (activeId && !restored) {
                        // View was deleted (e.g. from another device or session): clean up orphaned pointer
                        setActiveViewId(null);
                    } else if (restored && cfg && typeof cfg.applyState === 'function') {
                        cfg.applyState(restored, { isInitialBoot: true });
                    }

                    renderViewControls();
                }
            })
            .catch(function (err) {
                console.warn('[NSViews] Failed to fetch saved views from DB:', err);
            });
    }

    // Called by a page whenever any setting the view captured is changed by
    // hand. Drops the active view so the pill stops claiming a named view is
    // in effect. No-op when nothing is active.
    function markViewDirty() {
        if (!cfg) return;
        if (getActiveViewId()) {
            setActiveViewId(null);
            renderViewControls();
        }
    }

    // --- Rendering ---------------------------------------------------------

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function defaultTitleFor(activeView) {
        return activeView ? activeView.name : cfg.defaultTitle;
    }

    function renderViewControls() {
        if (!cfg) return;

        var views = getAllViews();
        var activeId = getActiveViewId();
        var activeView = activeId
            ? views.find(function (v) { return String(v.id) === String(activeId); })
            : null;

        var pillBtn = el('view-pill-btn');
        var labelEl = el('active-view-label');
        var titleEl = cfg.titleElId ? el(cfg.titleElId) : null;
        var titleFn = cfg.titleFor || defaultTitleFor;

        if (titleEl) titleEl.textContent = titleFn(activeView || null);
        if (labelEl) labelEl.textContent = activeView ? activeView.name : 'Select View';
        if (pillBtn) pillBtn.classList.toggle('active', !!activeView);

        var listEl = el('all-views-list');
        if (!listEl) return;

        listEl.innerHTML = views.map(function (v) {
            var isActive = String(v.id) === String(activeId);
            var id = escapeHtml(v.id);
            return '' +
                '<div class="view-list-item ' + (isActive ? 'active' : '') + '"' +
                ' data-view-id="' + id + '"' +
                ' onclick="applyView(\'' + id + '\')"' +
                ' oncontextmenu="openTabContextMenu(\'' + id + '\', event)">' +
                '<span class="item-title">' + escapeHtml(v.name) + '</span>' +
                '<span style="display:inline-flex; align-items:center; gap:8px;">' +
                (isActive ? '<i class="fa-solid fa-check" style="font-size:0.8em;"></i>' : '') +
                '<i class="fa-solid fa-trash view-item-delete" title="Delete view"' +
                ' onclick="deleteView(\'' + id + '\', event)"></i>' +
                '</span></div>';
        }).join('');
    }

    // --- Dropdown ----------------------------------------------------------

    function closeViewDropdown() {
        var popover = el('view-dropdown-popover');
        if (popover) popover.classList.remove('active');
    }

    function toggleViewDropdown(event) {
        if (event) event.stopPropagation();
        var popover = el('view-dropdown-popover');
        if (!popover) return;
        var opening = !popover.classList.contains('active');
        if (opening && cfg.settingsPopoverId) {
            var settingsPopover = el(cfg.settingsPopoverId);
            if (settingsPopover) settingsPopover.classList.remove('active');
        }
        popover.classList.toggle('active');
        if (popover.classList.contains('active')) cancelSaveView();
    }

    function showSaveViewInput() {
        var promptRow = el('view-save-prompt-row');
        var inputRow = el('view-save-input-row');
        var input = el('new-view-name');
        if (promptRow) promptRow.style.display = 'none';
        if (inputRow) inputRow.style.display = 'flex';
        if (input) {
            input.value = '';
            input.focus();
        }
    }

    function cancelSaveView() {
        var promptRow = el('view-save-prompt-row');
        var inputRow = el('view-save-input-row');
        if (promptRow) promptRow.style.display = 'block';
        if (inputRow) inputRow.style.display = 'none';
    }

    // --- View lifecycle ----------------------------------------------------

    function applyView(viewId) {
        // A completed long-press (see setupLongPress) opens the context menu
        // instead of switching views -- the tap/click that the browser still
        // synthesizes after that touch lifts must not also apply the view.
        if (suppressNextClick) {
            suppressNextClick = false;
            return;
        }

        var view = findView(viewId);
        if (!view) return;

        setActiveViewId(viewId);
        cfg.applyState(view);
        renderViewControls();
        closeViewDropdown();
    }

    function saveCurrentView() {
        var input = el('new-view-name');
        var name = (input ? input.value : '').trim();
        if (!name) return;

        var statePayload = cfg.captureState();
        var postBody = {
            page: cfg.page,
            name: name,
            payload: statePayload,
            is_builtin: false,
            source_default_id: null,
            is_deleted: false
        };

        fetch('/api/v1/saved_views', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(postBody)
        })
        .then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        })
        .then(function (created) {
            dbRows.push(created);
            cachedViews = mergeViews(dbRows);
            setActiveViewId(String(created.id));
            cancelSaveView();
            renderViewControls();
            closeViewDropdown();
        })
        .catch(function (err) {
            console.error('[NSViews] Error saving view:', err);
            alert('Failed to save view: ' + err.message);
        });
    }

    function deleteView(id, event) {
        if (event) event.stopPropagation();

        var target = findView(id);
        if (!target) return;

        // Mutation branching (§3.1):
        if (target.isDefault && !target.dbId) {
            // Unmaterialized built-in: POST tombstone row to materialize deletion
            fetch('/api/v1/saved_views', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    page: cfg.page,
                    name: target.name,
                    payload: {},
                    is_builtin: true,
                    source_default_id: target.id,
                    is_deleted: true
                })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (created) {
                dbRows.push(created);
                cachedViews = mergeViews(dbRows);
                if (String(getActiveViewId()) === String(id)) setActiveViewId(null);
                renderViewControls();
            })
            .catch(function (err) {
                console.error('[NSViews] Delete error:', err);
            });
        } else if (target.isDefault && target.dbId) {
            // Already materialized built-in: PUT tombstone
            fetch('/api/v1/saved_views/' + target.dbId, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_deleted: true })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (updated) {
                var idx = dbRows.findIndex(function (r) { return r.id === target.dbId; });
                if (idx !== -1) dbRows[idx] = updated;
                cachedViews = mergeViews(dbRows);
                if (String(getActiveViewId()) === String(id)) setActiveViewId(null);
                renderViewControls();
            })
            .catch(function (err) {
                console.error('[NSViews] Delete error:', err);
            });
        } else if (target.dbId) {
            // Custom view: DELETE row
            fetch('/api/v1/saved_views/' + target.dbId, {
                method: 'DELETE'
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function () {
                dbRows = dbRows.filter(function (r) { return r.id !== target.dbId; });
                cachedViews = mergeViews(dbRows);
                if (String(getActiveViewId()) === String(id)) setActiveViewId(null);
                renderViewControls();
            })
            .catch(function (err) {
                console.error('[NSViews] Delete error:', err);
            });
        }
    }

    // --- Right-click context menu ------------------------------------------

    function openTabContextMenu(viewId, event) {
        if (event) {
            event.preventDefault();
            event.stopPropagation();
        }
        contextTargetViewId = String(viewId);

        var targetView = findView(viewId);
        if (!targetView) return;

        var menu = el('tab-context-menu');
        var header = el('tab-context-header');
        var deleteItem = el('ctx-delete');

        if (header) header.textContent = targetView.name;
        if (deleteItem) deleteItem.style.display = 'flex';

        if (menu) {
            menu.style.left = Math.min(event.clientX, window.innerWidth - 230) + 'px';
            menu.style.top = Math.min(event.clientY, window.innerHeight - 180) + 'px';
            menu.style.display = 'block';
        }
    }

    function closeTabContextMenu() {
        var menu = el('tab-context-menu');
        if (menu) menu.style.display = 'none';
    }

    // --- Touch long-press -> context menu (3 Sep 2026 iPad touch pass) -----
    //
    // Right-click (`oncontextmenu`, wired inline in the row markup above)
    // never fires from touch. Long-press is the standard equivalent on
    // iOS/Android. Delegated on the stable #all-views-list container rather
    // than per-row, so it survives renderViewControls()' innerHTML rebuild on
    // every render -- the same reason the row's own onclick/oncontextmenu are
    // inline attributes rather than JS-attached listeners.
    //
    // Deliberately hold-down only for this first pass, per Harry's direction
    // (no kebab/hamburger trigger) -- see
    // docs/analysis/ipad-touch-support-plan.md Phase C. Gated on
    // pointerType !== 'mouse' so mouse users keep using real right-click,
    // unaffected.
    var LONG_PRESS_MS = 500;
    var LONG_PRESS_MOVE_TOLERANCE_PX = 10;
    var longPressTimer = null;
    var longPressViewId = null;
    var longPressStartX = 0, longPressStartY = 0;
    var suppressNextClick = false;

    function setupLongPress() {
        var listEl = el('all-views-list');
        if (!listEl) return;

        listEl.addEventListener('pointerdown', function (e) {
            if (e.pointerType === 'mouse') return;
            var row = e.target.closest ? e.target.closest('.view-list-item') : null;
            if (!row) return;

            longPressViewId = row.getAttribute('data-view-id');
            longPressStartX = e.clientX;
            longPressStartY = e.clientY;

            clearTimeout(longPressTimer);
            longPressTimer = setTimeout(function () {
                longPressTimer = null;
                if (!longPressViewId) return;
                // The click that follows this touch's eventual pointerup must
                // not also apply the view -- see the guard at the top of
                // applyView(). Cleared on a short timer too, in case this
                // touch ends via pointercancel with no click ever following.
                suppressNextClick = true;
                openTabContextMenu(longPressViewId, {
                    clientX: longPressStartX,
                    clientY: longPressStartY,
                    preventDefault: function () {},
                    stopPropagation: function () {}
                });
                setTimeout(function () { suppressNextClick = false; }, 400);
            }, LONG_PRESS_MS);
        });

        listEl.addEventListener('pointermove', function (e) {
            if (!longPressTimer) return;
            var dx = e.clientX - longPressStartX;
            var dy = e.clientY - longPressStartY;
            if (Math.sqrt(dx * dx + dy * dy) > LONG_PRESS_MOVE_TOLERANCE_PX) {
                clearTimeout(longPressTimer);
                longPressTimer = null;
            }
        });

        function cancelLongPress() {
            clearTimeout(longPressTimer);
            longPressTimer = null;
        }
        listEl.addEventListener('pointerup', cancelLongPress);
        listEl.addEventListener('pointercancel', cancelLongPress);
    }

    function contextTarget() {
        if (!contextTargetViewId) return null;
        var views = getAllViews();
        var target = views.find(function (v) { return String(v.id) === String(contextTargetViewId); });
        return target ? { views: views, target: target } : null;
    }

    function onContextUpdate() {
        closeTabContextMenu();
        var ctx = contextTarget();
        if (!ctx) return;

        var target = ctx.target;
        var statePayload = cfg.captureState();

        // Mutation branching (§3.1):
        if (target.isDefault && !target.dbId) {
            // Unmaterialized built-in: POST materialized update
            fetch('/api/v1/saved_views', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    page: cfg.page,
                    name: target.name,
                    payload: statePayload,
                    is_builtin: true,
                    source_default_id: target.id
                })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (created) {
                dbRows.push(created);
                cachedViews = mergeViews(dbRows);
                applyView(String(created.id));
            })
            .catch(function (err) {
                console.error('[NSViews] Context update error:', err);
            });
        } else if (target.dbId) {
            // Already materialized: PUT update
            fetch('/api/v1/saved_views/' + target.dbId, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ payload: statePayload })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (updated) {
                var idx = dbRows.findIndex(function (r) { return r.id === target.dbId; });
                if (idx !== -1) dbRows[idx] = updated;
                cachedViews = mergeViews(dbRows);
                applyView(String(target.dbId));
            })
            .catch(function (err) {
                console.error('[NSViews] Context update error:', err);
            });
        }
    }

    function saveActiveView(callback) {
        var target = findView(getActiveViewId()) || getDefaultView();
        if (!target) return;
        var statePayload = cfg.captureState();

        if (target.isDefault && !target.dbId) {
            fetch('/api/v1/saved_views', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    page: cfg.page,
                    name: target.name,
                    payload: statePayload,
                    is_builtin: true,
                    source_default_id: target.id
                })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (created) {
                dbRows.push(created);
                cachedViews = mergeViews(dbRows);
                setActiveViewId(String(created.id));
                renderViewControls();
                if (callback) callback(created);
            })
            .catch(function (err) {
                console.error('[NSViews] Save active view error:', err);
                if (callback) callback(null, err);
            });
        } else if (target.dbId) {
            fetch('/api/v1/saved_views/' + target.dbId, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ payload: statePayload })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (updated) {
                var idx = dbRows.findIndex(function (r) { return r.id === target.dbId; });
                if (idx !== -1) dbRows[idx] = updated;
                cachedViews = mergeViews(dbRows);
                setActiveViewId(String(target.dbId));
                renderViewControls();
                if (callback) callback(updated);
            })
            .catch(function (err) {
                console.error('[NSViews] Save active view error:', err);
                if (callback) callback(null, err);
            });
        }
    }

    function onContextDuplicate() {
        closeTabContextMenu();
        var ctx = contextTarget();
        if (!ctx) return;

        var newName = prompt('Enter name for duplicated view:', ctx.target.name + ' (Copy)');
        if (!newName || !newName.trim()) return;

        var payload = {};
        Object.keys(ctx.target).forEach(function (k) {
            if (k !== 'id' && k !== 'name' && k !== 'isDefault' && k !== 'dbId' && k !== 'source_default_id') {
                payload[k] = ctx.target[k];
            }
        });

        fetch('/api/v1/saved_views', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                page: cfg.page,
                name: newName.trim(),
                payload: payload,
                is_builtin: false
            })
        })
        .then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        })
        .then(function (created) {
            dbRows.push(created);
            cachedViews = mergeViews(dbRows);
            applyView(String(created.id));
        })
        .catch(function (err) {
            console.error('[NSViews] Duplicate error:', err);
        });
    }

    function onContextRename() {
        closeTabContextMenu();
        var ctx = contextTarget();
        if (!ctx) return;

        var target = ctx.target;
        var newName = prompt('Enter new name for this view:', target.name);
        if (!newName || !newName.trim()) return;
        newName = newName.trim();

        // Mutation branching (§3.1):
        if (target.isDefault && !target.dbId) {
            // Unmaterialized built-in: POST materialized rename
            var payload = {};
            Object.keys(target).forEach(function (k) {
                if (k !== 'id' && k !== 'name' && k !== 'isDefault' && k !== 'dbId' && k !== 'source_default_id') {
                    payload[k] = target[k];
                }
            });
            fetch('/api/v1/saved_views', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    page: cfg.page,
                    name: newName,
                    payload: payload,
                    is_builtin: true,
                    source_default_id: target.id
                })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (created) {
                dbRows.push(created);
                cachedViews = mergeViews(dbRows);
                if (String(getActiveViewId()) === String(target.id)) {
                    setActiveViewId(String(created.id));
                }
                renderViewControls();
            })
            .catch(function (err) {
                console.error('[NSViews] Rename error:', err);
            });
        } else if (target.dbId) {
            // Already materialized: PUT rename
            fetch('/api/v1/saved_views/' + target.dbId, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName })
            })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (updated) {
                var idx = dbRows.findIndex(function (r) { return r.id === target.dbId; });
                if (idx !== -1) dbRows[idx] = updated;
                cachedViews = mergeViews(dbRows);
                renderViewControls();
            })
            .catch(function (err) {
                console.error('[NSViews] Rename error:', err);
            });
        }
    }

    function onContextDelete() {
        closeTabContextMenu();
        if (!contextTargetViewId) return;
        deleteView(contextTargetViewId);
    }

    // --- Init --------------------------------------------------------------

    function init(config) {
        cfg = Object.assign({
            page: null,
            activeViewKey: null,
            viewsKey: null,
            defaultViews: [],
            titleElId: null,
            defaultTitle: '',
            titleFor: null,
            settingsPopoverId: null,
            settingsBtnId: null,
            captureState: function () { return {}; },
            applyState: function () {}
        }, config || {});

        // Resolve page name if not explicitly passed
        if (!cfg.page) {
            if (cfg.viewsKey && cfg.viewsKey.indexOf('trends') !== -1) cfg.page = 'trends';
            else if (cfg.viewsKey && cfg.viewsKey.indexOf('trace') !== -1) cfg.page = 'trace';
            else if (cfg.viewsKey && (cfg.viewsKey.indexOf('patterns') !== -1 || cfg.viewsKey.indexOf('agp') !== -1)) cfg.page = 'patterns';
            else if (cfg.viewsKey && cfg.viewsKey.indexOf('hba1c') !== -1) cfg.page = 'hba1c';
            else if (cfg.viewsKey && cfg.viewsKey.indexOf('calendar') !== -1) cfg.page = 'calendar';
            else if (cfg.activeViewKey && cfg.activeViewKey.indexOf('trends') !== -1) cfg.page = 'trends';
            else if (cfg.activeViewKey && cfg.activeViewKey.indexOf('trace') !== -1) cfg.page = 'trace';
            else if (cfg.activeViewKey && (cfg.activeViewKey.indexOf('patterns') !== -1 || cfg.activeViewKey.indexOf('agp') !== -1)) cfg.page = 'patterns';
            else if (cfg.activeViewKey && cfg.activeViewKey.indexOf('hba1c') !== -1) cfg.page = 'hba1c';
            else if (cfg.activeViewKey && cfg.activeViewKey.indexOf('calendar') !== -1) cfg.page = 'calendar';
        }

        // Non-destructive migration for legacy activeViewKey aliases
        if (cfg.activeViewKey) {
            try {
                var legacyKeyAliases = {
                    'ns_patterns_active_view': ['ns_agp_active_view_id', 'ns_agp_active_view'],
                    'ns_hba1c_active_view': ['ns_hba1c_active_view_id']
                };
                var aliases = legacyKeyAliases[cfg.activeViewKey];
                if (aliases) {
                    var currentVal = localStorage.getItem(cfg.activeViewKey);
                    for (var ai = 0; ai < aliases.length; ai++) {
                        var oldVal = localStorage.getItem(aliases[ai]);
                        if (oldVal !== null) {
                            if (currentVal === null) {
                                localStorage.setItem(cfg.activeViewKey, oldVal);
                                currentVal = oldVal;
                            }
                            localStorage.removeItem(aliases[ai]);
                        }
                    }
                }
            } catch (e) {
                console.warn('Migration error for activeViewKey:', e);
            }
        }

        // Initialize cache with unmaterialized defaults
        cachedViews = mergeViews([]);

        // One outside-click handler for the context menu, the view dropdown and
        // the page's settings popover.
        document.addEventListener('click', function (e) {
            closeTabContextMenu();

            var popover = el('view-dropdown-popover');
            var pillBtn = el('view-pill-btn');
            if (popover && pillBtn && !popover.contains(e.target) && !pillBtn.contains(e.target)) {
                popover.classList.remove('active');
            }

            if (!cfg.settingsPopoverId || !cfg.settingsBtnId) return;
            var settingsPopover = el(cfg.settingsPopoverId);
            var settingsBtn = el(cfg.settingsBtnId);
            if (settingsPopover && settingsBtn &&
                !settingsPopover.contains(e.target) && !settingsBtn.contains(e.target)) {
                if (e.target.closest && e.target.closest('.metric-style-popover')) return;
                settingsPopover.classList.remove('active');
            }
        });

        // Render synchronously from code defaults first
        renderViewControls();
        setupLongPress();

        // Fetch DB rows asynchronously to populate custom/materialized views
        var p = fetchViewsFromDb();
        initPromise = new Promise(function (resolve) {
            var timer = setTimeout(function () {
                resolve({ source: 'timeout' });
            }, 600);
            if (p && typeof p.then === 'function') {
                p.then(function () {
                    clearTimeout(timer);
                    resolve({ source: 'views' });
                }).catch(function (err) {
                    clearTimeout(timer);
                    resolve({ source: 'error', error: err });
                });
            } else {
                clearTimeout(timer);
                resolve({ source: 'views' });
            }
        });
        return initPromise;
    }

    var initPromise = null;

    global.NSViews = {
        init: init,
        ready: function () { return initPromise || Promise.resolve({ source: 'ready' }); },
        getAllViews: getAllViews,
        getDefaultView: getDefaultView,
        saveAllViews: saveAllViews,
        getActiveViewId: getActiveViewId,
        getActiveView: getActiveView,
        saveActiveView: saveActiveView,
        setActiveViewId: setActiveViewId,
        findView: findView,
        markViewDirty: markViewDirty,
        renderViewControls: renderViewControls
    };

    // Published as globals because the markup wires these through inline
    // onclick/oncontextmenu attributes, which resolve against window.
    global.applyView = applyView;
    global.deleteView = deleteView;
    global.saveCurrentView = saveCurrentView;
    global.saveActiveView = saveActiveView;
    global.toggleViewDropdown = toggleViewDropdown;
    global.showSaveViewInput = showSaveViewInput;
    global.cancelSaveView = cancelSaveView;
    global.openTabContextMenu = openTabContextMenu;
    global.closeTabContextMenu = closeTabContextMenu;
    global.onContextUpdate = onContextUpdate;
    global.onContextDuplicate = onContextDuplicate;
    global.onContextDelete = onContextDelete;
    global.onContextRename = onContextRename;

    // Pages call these as bare identifiers throughout their inline scripts.
    global.renderViewControls = renderViewControls;
    global.markViewDirty = markViewDirty;
    global.getAllViews = getAllViews;
    global.getDefaultView = getDefaultView;
    global.saveAllViews = saveAllViews;
    global.getActiveViewId = getActiveViewId;
    global.getActiveView = getActiveView;
    global.saveActiveView = saveActiveView;
    global.setActiveViewId = setActiveViewId;
    global.findView = findView;
})(window);
