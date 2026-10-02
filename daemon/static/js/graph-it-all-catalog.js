/**
 * daemon/static/js/graph-it-all-catalog.js
 * Client-side metric ledger, domain groupings, and modal selection drawer.
 */
(function(global) {
    'use strict';

    const DOMAINS = {
        glucose: { label: 'Glucose & Glycemic', order: 1 },
        insulin: { label: 'Insulin Regimen', order: 2 },
        events: { label: 'Events & Frequency', order: 2.5 },
        carbs: { label: 'Carbs & Nutrition', order: 3 },
        hardware: { label: 'Hardware Kinetics', order: 4 },
        clinical: { label: 'Clinical & Glycemic Risk', order: 5 },
        temporal: { label: 'Temporal Axes', order: 0 }
    };

    let metricsCatalog = [];
    let activeSelectingAxis = null; // 'x' or 'y'
    let onSelectCallback = null;

    async function loadCatalog() {
        try {
            const resp = await fetch('/api/v1/graph_it_all/catalogue');
            if (!resp.ok) throw new Error('Failed to load catalogue');
            const data = await resp.json();
            metricsCatalog = data.metrics || [];
        } catch (e) {
            console.error('Error fetching Graph-it-All catalogue:', e);
        }
    }

    function getMetric(id) {
        return metricsCatalog.find(m => m.id === id) || null;
    }

    function getAllMetrics() {
        return metricsCatalog;
    }

    function openDrawer(axis, onSelect) {
        activeSelectingAxis = axis;
        onSelectCallback = onSelect;

        const backdrop = document.getElementById('gia-drawer-backdrop');
        const drawer = document.getElementById('gia-drawer');
        const searchInput = document.getElementById('gia-drawer-search-input');
        const titleEl = document.getElementById('gia-drawer-title');

        if (titleEl) {
            titleEl.textContent = `Select ${axis.toUpperCase()} Axis Metric`;
        }
        if (searchInput) {
            searchInput.value = '';
        }

        renderDrawerMetrics('');

        if (backdrop) backdrop.style.display = 'block';
        if (drawer) drawer.style.display = 'flex';
        if (searchInput) searchInput.focus();
    }

    function closeDrawer() {
        activeSelectingAxis = null;
        onSelectCallback = null;
        const backdrop = document.getElementById('gia-drawer-backdrop');
        const drawer = document.getElementById('gia-drawer');
        if (backdrop) backdrop.style.display = 'none';
        if (drawer) drawer.style.display = 'none';
    }

    function renderDrawerMetrics(filterText) {
        const bodyEl = document.getElementById('gia-drawer-body');
        if (!bodyEl) return;

        bodyEl.innerHTML = '';
        const query = (filterText || '').toLowerCase().trim();

        // Group by domain
        const grouped = {};
        metricsCatalog.forEach(m => {
            // If selecting Y or Y2 axis, hide date (date is only for X axis)
            if ((activeSelectingAxis === 'y' || activeSelectingAxis === 'y2') && m.id === 'date') return;

            if (query && !m.name.toLowerCase().includes(query) && !m.id.toLowerCase().includes(query)) {
                return;
            }

            const dom = m.domain || 'other';
            if (!grouped[dom]) grouped[dom] = [];
            grouped[dom].push(m);
        });

        const sortedDomains = Object.keys(grouped).sort((a, b) => {
            const ordA = (DOMAINS[a] && DOMAINS[a].order) || 99;
            const ordB = (DOMAINS[b] && DOMAINS[b].order) || 99;
            return ordA - ordB;
        });

        sortedDomains.forEach(dom => {
            const groupWrap = document.createElement('div');
            groupWrap.className = 'gia-domain-group';

            const header = document.createElement('div');
            header.className = 'gia-domain-header';
            header.textContent = (DOMAINS[dom] && DOMAINS[dom].label) || dom;
            groupWrap.appendChild(header);

            grouped[dom].forEach(m => {
                const currentGrain = (global.GIABuilder && global.GIABuilder.getState) ? global.GIABuilder.getState().grain : 'daily';
                const isGrainValid = !m.valid_grains || m.valid_grains.includes(currentGrain);

                const item = document.createElement('div');
                item.className = 'gia-metric-item' + (isGrainValid ? '' : ' disabled');
                if (!isGrainValid) {
                    item.style.opacity = '0.45';
                    item.style.cursor = 'not-allowed';
                    item.title = `Not available for ${currentGrain} grain (Requires ${m.valid_grains.join(', ')})`;
                }

                item.innerHTML = `
                    <div>
                        <div class="gia-metric-name">${m.name}</div>
                        <div class="gia-metric-meta">${m.unit ? `[${m.unit}]` : ''} • ID: ${m.id} ${!isGrainValid ? `• (Requires ${m.valid_grains.join('/')})` : ''}</div>
                    </div>
                    <span class="gia-badge" style="font-size: 0.68rem;">${isGrainValid ? 'Select' : 'Incompatible'}</span>
                `;
                if (isGrainValid) {
                    item.addEventListener('click', () => {
                        if (onSelectCallback) {
                            onSelectCallback(m);
                        }
                        closeDrawer();
                    });
                }
                groupWrap.appendChild(item);
            });

            bodyEl.appendChild(groupWrap);
        });

        if (sortedDomains.length === 0) {
            bodyEl.innerHTML = '<div style="color: var(--text-tertiary); text-align: center; padding: 20px;">No metrics match your search.</div>';
        }
    }

    function initDrawer() {
        const backdrop = document.getElementById('gia-drawer-backdrop');
        const closeBtn = document.getElementById('gia-drawer-close-btn');
        const searchInput = document.getElementById('gia-drawer-search-input');

        if (backdrop) {
            backdrop.addEventListener('click', closeDrawer);
        }
        if (closeBtn) {
            closeBtn.addEventListener('click', closeDrawer);
        }
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                renderDrawerMetrics(e.target.value);
            });
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') closeDrawer();
            });
        }
    }

    global.GIACatalog = {
        loadCatalog,
        getMetric,
        getAllMetrics,
        openDrawer,
        closeDrawer,
        initDrawer
    };

})(window);
