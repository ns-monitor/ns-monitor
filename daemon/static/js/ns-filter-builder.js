/**
 * ns-filter-builder.js — Natural Language Sentence Builder for Daily Goals & Smart Filters
 *
 * Implements conversational sentence flows for Daily Goals and Smart Filters with dynamic
 * modifier guardrails (Moving Average, Streak, Sustained, Time of Day) and strict mutual
 * exclusivity between Moving Average and Duration.
 *
 * Fully compliant with AGENTS.md: pure Vanilla JS DOM manipulation (no reactive frameworks),
 * zero shadow CSS, strict use of ns-shared.css variables.
 */

(function(window) {
    'use strict';

    var instances = {};

    var COMPARATOR_LABELS = {
        'gte': 'at least',
        'gt': 'greater than',
        'lte': 'at most',
        'lt': 'less than',
        'eq': 'equal to',
        'occurred': 'has occurred'
    };

    var DAILY_MOVING_AVERAGE_CHOICES = [
        { value: 3, unit: 'days', label: '3 days' },
        { value: 7, unit: 'days', label: '7 days' },
        { value: 14, unit: 'days', label: '14 days' },
        { value: 30, unit: 'days', label: '30 days' },
        { value: 60, unit: 'days', label: '60 days' },
        { value: 90, unit: 'days', label: '90 days' },
        { value: 'custom', unit: 'days', label: 'Custom...' }
    ];

    var CONTINUOUS_MOVING_AVERAGE_CHOICES = [
        { value: 15, unit: 'minutes', label: '15 minutes' },
        { value: 30, unit: 'minutes', label: '30 minutes' },
        { value: 60, unit: 'minutes', label: '1 hour' },
        { value: 120, unit: 'minutes', label: '2 hours' },
        { value: 240, unit: 'minutes', label: '4 hours' },
        { value: 'custom', unit: 'minutes', label: 'Custom...' }
    ];

    function deepClone(obj) {
        return JSON.parse(JSON.stringify(obj));
    }

    function getMetricCategory(metricKey, cfg) {
        if (!cfg) return 'daily_standard';
        if (cfg.kind === 'event') return 'event';
        if (cfg.kind === 'continuous') return 'continuous';
        if (['tdd', 'carbs', 'lbgi', 'hbgi', 'gvi'].indexOf(metricKey) !== -1) {
            return 'daily_risk';
        }
        return 'daily_standard';
    }

    function cleanIncompatibleModifiers(clause, metricKey, cfg) {
        var category = getMetricCategory(metricKey, cfg);
        if (category === 'event') {
            delete clause.duration;
            delete clause.window;
            if (clause.modifier_order) {
                clause.modifier_order = clause.modifier_order.filter(function(k) { return k === 'timeScope' || k === 'preceding'; });
            }
        } else if (category === 'daily_risk') {
            delete clause.timeScope;
            if (clause.modifier_order) {
                clause.modifier_order = clause.modifier_order.filter(function(k) { return k !== 'timeScope'; });
            }
            if (clause.duration && (clause.duration.unit === 'minutes' || clause.duration.unit === 'hours')) {
                clause.duration.unit = 'days';
            }
            if (clause.window && (clause.window.unit === 'minutes' || clause.window.unit === 'hours')) {
                clause.window.unit = 'days';
                clause.window.value = 7;
            }
        } else if (category === 'daily_standard') {
            if (clause.duration && (clause.duration.unit === 'minutes' || clause.duration.unit === 'hours')) {
                clause.duration.unit = 'days';
            }
            if (clause.window && (clause.window.unit === 'minutes' || clause.window.unit === 'hours')) {
                clause.window.unit = 'days';
                clause.window.value = 7;
            }
        } else if (category === 'continuous') {
            if (clause.duration && (clause.duration.unit === 'days' || clause.duration.unit === 'weeks')) {
                clause.duration.unit = 'minutes';
                clause.duration.value = 15;
            }
            if (clause.window && clause.window.unit === 'days') {
                clause.window.unit = 'minutes';
                clause.window.value = 30;
            }
        }
    }

    function getDefaultRule(metrics, preferredMetric) {
        var metricKey = preferredMetric || 'tir';
        if (!metrics[metricKey]) {
            metricKey = Object.keys(metrics)[0] || 'tir';
        }
        var cfg = metrics[metricKey] || {};
        var rule = {
            metric: metricKey,
            comparator: cfg.default_comparator || 'gte',
            threshold: cfg.default_threshold !== undefined ? cfg.default_threshold : 70.0
        };
        return rule;
    }

    function getDefaultValueRule(metrics) {
        return {
            kind: 'value',
            metric: 'tir',
            window_type: 'single_day',
            rolling_days: null
        };
    }

    function getDefaultFilter(metrics) {
        return {
            op: 'AND',
            clauses: [
                getDefaultRule(metrics, 'tir')
            ]
        };
    }

    function NSFilterBuilderInstance(options) {
        this.containerId = options.containerId;
        this.container = document.getElementById(this.containerId);
        if (!this.container) {
            console.error('[NSFilterBuilder] Container not found:', this.containerId);
            return;
        }
        this.metrics = options.metrics || {};
        this.mode = options.mode || (options.isTargetMode ? 'target' : (options.initialFilter && options.initialFilter.kind === 'value' ? 'value' : 'filter'));
        this.isTargetMode = this.mode === 'target';
        this.targetKind = options.targetKind || 'evaluative';
        this.onChange = typeof options.onChange === 'function' ? options.onChange : function() {};

        if (this.mode === 'value') {
            this.state = options.initialFilter ? deepClone(options.initialFilter) : getDefaultValueRule(this.metrics);
        } else if (this.mode === 'target') {
            this.state = options.initialRule ? deepClone(options.initialRule) : getDefaultRule(this.metrics);
        } else {
            this.state = options.initialFilter ? deepClone(options.initialFilter) : getDefaultFilter(this.metrics);
        }

        // Global click listener to close open modifier menus
        this.activeMenu = null;
        var self = this;
        this._globalClickListener = function(e) {
            if (self.activeMenu && !self.activeMenu.contains(e.target) && !e.target.closest('.sentence-modifier-btn')) {
                self.activeMenu.classList.remove('show');
                self.activeMenu = null;
            }
        };
        document.addEventListener('click', this._globalClickListener);

        this.render();
    }

    NSFilterBuilderInstance.prototype.destroy = function() {
        if (this._globalClickListener) {
            document.removeEventListener('click', this._globalClickListener);
        }
    };

    NSFilterBuilderInstance.prototype.getFilter = function() {
        return deepClone(this.state);
    };

    NSFilterBuilderInstance.prototype.setFilter = function(newFilter) {
        if (newFilter && newFilter.kind === 'value') {
            this.mode = 'value';
        } else if (this.mode !== 'target') {
            this.mode = 'filter';
        }
        this.state = deepClone(newFilter);
        this.render();
        this.notifyChange();
    };

    NSFilterBuilderInstance.prototype.setMode = function(newMode) {
        if (this.mode === newMode) return;
        this.mode = newMode;
        if (newMode === 'value') {
            if (!this.state || this.state.kind !== 'value') {
                this.state = getDefaultValueRule(this.metrics);
            }
        } else if (newMode === 'filter') {
            if (!this.state || this.state.kind === 'value') {
                this.state = getDefaultFilter(this.metrics);
            }
        }
        this.render();
        this.notifyChange();
    };

    NSFilterBuilderInstance.prototype.getMode = function() {
        return this.mode;
    };

    NSFilterBuilderInstance.prototype.getTargetKind = function() {
        return this.targetKind || 'evaluative';
    };

    NSFilterBuilderInstance.prototype.setTargetKind = function(newKind) {
        this.targetKind = newKind || 'evaluative';
        this.render();
        this.notifyChange();
    };

    NSFilterBuilderInstance.prototype.notifyChange = function() {
        this.onChange(this.getFilter());
    };

    NSFilterBuilderInstance.prototype.render = function() {
        this.container.innerHTML = '';
        if (this.mode === 'value') {
            var card = document.createElement('div');
            card.className = 'sentence-builder-standalone';
            card.appendChild(this.renderValueSentence(this.state));
            this.container.appendChild(card);
        } else if (this.mode === 'target') {
            var card = document.createElement('div');
            card.className = 'sentence-builder-standalone';
            card.appendChild(this.renderClauseSentence(this.state, null, null, true));
            this.container.appendChild(card);
        } else {
            this.container.appendChild(this.renderGroup(this.state, null, 0));
        }
    };

    NSFilterBuilderInstance.prototype.renderGroup = function(group, parentGroup, depth) {
        var self = this;
        var groupCard = document.createElement('div');
        groupCard.className = 'sentence-builder-card';

        // Root or Nested Group Header
        var headerEl = document.createElement('div');
        headerEl.className = depth === 0 ? 'sentence-root-phrase' : 'sentence-nested-connector';

        var prefixSpan = document.createElement('span');
        if (depth === 0) {
            prefixSpan.textContent = 'Find days that match';
        } else {
            prefixSpan.textContent = 'Match';
        }
        headerEl.appendChild(prefixSpan);

        // Operator toggle button (ALL = AND, ANY = OR)
        var opBtn = document.createElement('button');
        opBtn.type = 'button';
        opBtn.className = 'btn btn-sm ' + (group.op === 'AND' ? 'btn-primary' : 'btn-secondary');
        opBtn.style.padding = '2px 10px';
        opBtn.style.fontWeight = '700';
        opBtn.textContent = group.op === 'AND' ? 'ALL ⏷' : 'ANY ⏷';
        opBtn.title = 'Click to toggle ALL (AND) / ANY (OR)';
        opBtn.addEventListener('click', function() {
            group.op = group.op === 'AND' ? 'OR' : 'AND';
            self.render();
            self.notifyChange();
        });
        headerEl.appendChild(opBtn);

        var suffixSpan = document.createElement('span');
        if (depth === 0) {
            suffixSpan.textContent = 'of the following conditions:';
        } else {
            suffixSpan.textContent = 'of these conditions:';
        }
        headerEl.appendChild(suffixSpan);

        // Right side: Remove nested subgroup button (if depth > 0)
        if (depth > 0 && parentGroup) {
            var removeGroupBtn = document.createElement('button');
            removeGroupBtn.type = 'button';
            removeGroupBtn.className = 'btn-ghost-icon btn-ghost-danger';
            removeGroupBtn.title = 'Remove this group';
            removeGroupBtn.style.marginLeft = 'auto';
            removeGroupBtn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
            removeGroupBtn.addEventListener('click', function() {
                var idx = parentGroup.clauses.indexOf(group);
                if (idx !== -1) {
                    parentGroup.clauses.splice(idx, 1);
                    self.render();
                    self.notifyChange();
                }
            });
            headerEl.appendChild(removeGroupBtn);
        }

        groupCard.appendChild(headerEl);

        // Clauses container (bulleted list)
        var clausesContainer = document.createElement('div');
        clausesContainer.style.display = 'flex';
        clausesContainer.style.flexDirection = 'column';
        clausesContainer.style.gap = '10px';
        clausesContainer.style.marginTop = '8px';

        if (group.clauses && group.clauses.length > 0) {
            group.clauses.forEach(function(clause, idx) {
                if (clause.clauses) {
                    // Nested subgroup (up to 1-level deep)
                    clausesContainer.appendChild(self.renderGroup(clause, group, depth + 1));
                } else {
                    clausesContainer.appendChild(self.renderClauseSentence(clause, group, idx, false));
                }
            });
        } else {
            var emptyNotice = document.createElement('div');
            emptyNotice.style.color = 'var(--text-tertiary)';
            emptyNotice.style.fontStyle = 'italic';
            emptyNotice.style.fontSize = '0.9em';
            emptyNotice.style.padding = '6px 0';
            emptyNotice.textContent = 'No conditions added yet.';
            clausesContainer.appendChild(emptyNotice);
        }

        groupCard.appendChild(clausesContainer);

        // Actions footer
        var actionsRow = document.createElement('div');
        actionsRow.style.display = 'flex';
        actionsRow.style.gap = '10px';
        actionsRow.style.marginTop = '14px';

        var addConditionBtn = document.createElement('button');
        addConditionBtn.type = 'button';
        addConditionBtn.className = 'btn btn-sm';
        addConditionBtn.innerHTML = '<i class="fa-solid fa-plus"></i> ' + (depth === 0 ? 'Add condition' : 'Add condition to this group');
        addConditionBtn.addEventListener('click', function() {
            if (!group.clauses) group.clauses = [];
            group.clauses.push(getDefaultRule(self.metrics));
            self.render();
            self.notifyChange();
        });
        actionsRow.appendChild(addConditionBtn);

        if (depth === 0) {
            var addNestedBtn = document.createElement('button');
            addNestedBtn.type = 'button';
            addNestedBtn.className = 'btn btn-secondary btn-sm';
            var nestedOp = group.op === 'AND' ? 'OR' : 'AND';
            addNestedBtn.innerHTML = '<i class="fa-solid fa-code-branch"></i> Add a group of conditions';
            addNestedBtn.addEventListener('click', function() {
                if (!group.clauses) group.clauses = [];
                group.clauses.push({
                    op: nestedOp,
                    clauses: [
                        getDefaultRule(self.metrics, 'bg'),
                        getDefaultRule(self.metrics, 'carbs')
                    ]
                });
                self.render();
                self.notifyChange();
            });
            actionsRow.appendChild(addNestedBtn);
        }

        groupCard.appendChild(actionsRow);
        return groupCard;
    };

    NSFilterBuilderInstance.prototype.renderClauseSentence = function(clause, parentGroup, clauseIdx, isStandaloneTarget) {
        var self = this;
        var metricKey = clause.metric || 'tir';
        var cfg = self.metrics[metricKey] || {};
        var category = getMetricCategory(metricKey, cfg);
        var isEvent = category === 'event';

        var rowWrapper;
        if (isStandaloneTarget) {
            rowWrapper = document.createElement('div');
            rowWrapper.className = 'sentence-flow-row';
        } else {
            rowWrapper = document.createElement('div');
            rowWrapper.className = 'sentence-clause-item';

            var bullet = document.createElement('span');
            bullet.className = 'sentence-bullet-dot';
            bullet.textContent = '•';
            rowWrapper.appendChild(bullet);
        }

        var clauseBody = document.createElement('div');
        clauseBody.className = isStandaloneTarget ? 'sentence-flow-row' : 'sentence-clause-body';

        var sentenceRow = document.createElement('div');
        sentenceRow.className = 'sentence-flow-row';

        // Ensure modifier_order is initialized
        if (!clause.modifier_order || !Array.isArray(clause.modifier_order)) {
            clause.modifier_order = [];
            if (clause.window) clause.modifier_order.push('window');
            if (clause.duration) clause.modifier_order.push('duration');
            if (clause.timeScope) clause.modifier_order.push('timeScope');
        }

        // 1. Initial Goal Intent prefix (for standalone daily goals)
        if (isStandaloneTarget) {
            var intentSelect = document.createElement('select');
            intentSelect.className = 'sentence-select sentence-intent-select';
            var optGoal = document.createElement('option');
            optGoal.value = 'evaluative';
            optGoal.textContent = 'Track as a Goal';
            var optRef = document.createElement('option');
            optRef.value = 'reference';
            optRef.textContent = 'Reference Benchmark';
            if (self.targetKind === 'reference') {
                optRef.selected = true;
            } else {
                optGoal.selected = true;
            }
            intentSelect.appendChild(optGoal);
            intentSelect.appendChild(optRef);
            intentSelect.addEventListener('change', function() {
                self.targetKind = intentSelect.value;
                self.notifyChange();
            });
            sentenceRow.appendChild(intentSelect);

            var colonSpan = document.createElement('span');
            colonSpan.style.fontWeight = '600';
            colonSpan.style.color = 'var(--text-secondary)';
            colonSpan.textContent = ':';
            sentenceRow.appendChild(colonSpan);
        }

        // 2. Metric Selector
        var metricSelect = document.createElement('select');
        metricSelect.className = 'sentence-select';

        var groups = [
            { id: 'daily', label: 'Daily Metrics' },
            { id: 'continuous', label: 'Continuous Metrics' },
            { id: 'event', label: 'Discrete Events' }
        ];

        groups.forEach(function(grp) {
            var optgroup = document.createElement('optgroup');
            optgroup.label = grp.label;
            Object.keys(self.metrics).forEach(function(mKey) {
                var m = self.metrics[mKey];
                if (m.kind === grp.id) {
                    var opt = document.createElement('option');
                    opt.value = mKey;
                    opt.textContent = m.label;
                    if (mKey === metricKey) opt.selected = true;
                    optgroup.appendChild(opt);
                }
            });
            if (optgroup.children.length > 0) {
                metricSelect.appendChild(optgroup);
            }
        });

        metricSelect.addEventListener('change', function() {
            var newKey = metricSelect.value;
            var newCfg = self.metrics[newKey] || {};
            clause.metric = newKey;
            clause.comparator = newCfg.default_comparator || 'gte';
            clause.threshold = newCfg.default_threshold !== undefined ? newCfg.default_threshold : 70.0;
            cleanIncompatibleModifiers(clause, newKey, newCfg);
            self.render();
            self.notifyChange();
        });
        sentenceRow.appendChild(metricSelect);

        // 3. Comparator & Threshold
        var hasComparator = !isEvent || metricKey === 'low_count' || metricKey === 'warning_count';
        if (hasComparator) {
            var compSelect = document.createElement('select');
            compSelect.className = 'sentence-select';
            var allowedComps = cfg.allowed_comparators || ['gt', 'gte', 'lt', 'lte', 'eq'];
            allowedComps.forEach(function(c) {
                var opt = document.createElement('option');
                opt.value = c;
                opt.textContent = COMPARATOR_LABELS[c] || c;
                if (c === clause.comparator) opt.selected = true;
                compSelect.appendChild(opt);
            });
            compSelect.addEventListener('change', function() {
                clause.comparator = compSelect.value;
                self.notifyChange();
            });
            sentenceRow.appendChild(compSelect);

            var threshInput = document.createElement('input');
            threshInput.type = 'number';
            threshInput.className = 'sentence-input';
            if (cfg.min !== undefined) threshInput.min = cfg.min;
            if (cfg.max !== undefined) threshInput.max = cfg.max;
            if (cfg.step !== undefined) threshInput.step = cfg.step;
            threshInput.value = clause.threshold !== undefined ? clause.threshold : (cfg.default_threshold || 0);

            threshInput.addEventListener('input', function() {
                var val = parseFloat(threshInput.value);
                if (!isNaN(val)) {
                    clause.threshold = val;
                    self.notifyChange();
                }
            });
            sentenceRow.appendChild(threshInput);

            var unitSpan = document.createElement('span');
            unitSpan.className = 'sentence-unit';
            unitSpan.textContent = (cfg.unit && cfg.unit !== 'index') ? cfg.unit : '';
            sentenceRow.appendChild(unitSpan);
        } else {
            var occurredSpan = document.createElement('span');
            occurredSpan.className = 'sentence-text';
            occurredSpan.textContent = 'occurred';
            sentenceRow.appendChild(occurredSpan);
        }

        // 4. Modifier Chip Creator Helpers
        function createMaChip() {
            var maChip = document.createElement('span');
            maChip.className = 'sentence-modifier-chip';

            var maPrefix = document.createElement('span');
            maPrefix.className = 'chip-label';
            maPrefix.textContent = 'over a';
            maChip.appendChild(maPrefix);

            var maChoices = category === 'continuous' ? CONTINUOUS_MOVING_AVERAGE_CHOICES : DAILY_MOVING_AVERAGE_CHOICES;
            var maSelect = document.createElement('select');
            maSelect.className = 'sentence-select';
            maSelect.style.padding = '2px 6px';
            maSelect.style.fontSize = '0.85rem';

            var currentVal = clause.window.value || (category === 'continuous' ? 30 : 7);
            var isCustom = true;

            maChoices.forEach(function(choice) {
                var opt = document.createElement('option');
                opt.value = choice.value;
                opt.textContent = choice.label;
                if (choice.value === currentVal) {
                    opt.selected = true;
                    isCustom = false;
                }
                maSelect.appendChild(opt);
            });

            if (isCustom) {
                maSelect.value = 'custom';
            }

            maChip.appendChild(maSelect);

            var customInput = document.createElement('input');
            customInput.type = 'number';
            customInput.className = 'sentence-input';
            customInput.style.width = '60px';
            customInput.style.padding = '2px 6px';
            customInput.style.fontSize = '0.85rem';
            customInput.min = '1';
            customInput.value = currentVal;
            customInput.style.display = isCustom ? 'inline-block' : 'none';

            customInput.addEventListener('input', function() {
                var v = parseFloat(customInput.value);
                if (!isNaN(v) && v > 0) {
                    clause.window.value = v;
                    self.notifyChange();
                }
            });
            maChip.appendChild(customInput);

            maSelect.addEventListener('change', function() {
                if (maSelect.value === 'custom') {
                    customInput.style.display = 'inline-block';
                    clause.window.value = parseFloat(customInput.value) || 7;
                } else {
                    customInput.style.display = 'none';
                    clause.window.value = parseFloat(maSelect.value);
                }
                self.notifyChange();
            });

            var maSuffix = document.createElement('span');
            maSuffix.className = 'chip-label';
            maSuffix.textContent = 'moving average';
            maChip.appendChild(maSuffix);

            var maDismiss = document.createElement('button');
            maDismiss.type = 'button';
            maDismiss.className = 'chip-dismiss';
            maDismiss.title = 'Remove Moving Average';
            maDismiss.innerHTML = '&times;';
            maDismiss.addEventListener('click', function() {
                delete clause.window;
                var wIdx = clause.modifier_order.indexOf('window');
                if (wIdx !== -1) clause.modifier_order.splice(wIdx, 1);
                self.render();
                self.notifyChange();
            });
            maChip.appendChild(maDismiss);

            return maChip;
        }

        function createDurChip() {
            var durChip = document.createElement('span');
            durChip.className = 'sentence-modifier-chip';

            var durLabel = document.createElement('span');
            durLabel.className = 'chip-label';
            durLabel.textContent = category === 'continuous' ? 'sustained for at least' : 'for at least';
            durChip.appendChild(durLabel);

            var durInput = document.createElement('input');
            durInput.type = 'number';
            durInput.className = 'sentence-input';
            durInput.style.width = '55px';
            durInput.style.padding = '2px 6px';
            durInput.style.fontSize = '0.85rem';
            durInput.min = '1';
            durInput.value = clause.duration.value !== undefined ? clause.duration.value : (category === 'continuous' ? 15 : 3);
            durInput.addEventListener('input', function() {
                var v = parseFloat(durInput.value);
                if (!isNaN(v) && v > 0) {
                    clause.duration.value = v;
                    self.notifyChange();
                }
            });
            durChip.appendChild(durInput);

            var durUnits = cfg.duration_units || (category === 'continuous' ? ['minutes', 'hours'] : ['days', 'weeks']);
            var durUnitSelect = document.createElement('select');
            durUnitSelect.className = 'sentence-select';
            durUnitSelect.style.padding = '2px 6px';
            durUnitSelect.style.fontSize = '0.85rem';

            durUnits.forEach(function(u) {
                var opt = document.createElement('option');
                opt.value = u;
                opt.textContent = u;
                if (clause.duration.unit === u) opt.selected = true;
                durUnitSelect.appendChild(opt);
            });
            durUnitSelect.addEventListener('change', function() {
                clause.duration.unit = durUnitSelect.value;
                self.notifyChange();
            });
            durChip.appendChild(durUnitSelect);

            if (category !== 'continuous') {
                var consecSpan = document.createElement('span');
                consecSpan.className = 'chip-label';
                consecSpan.textContent = 'consecutively';
                durChip.appendChild(consecSpan);
            }

            var durDismiss = document.createElement('button');
            durDismiss.type = 'button';
            durDismiss.className = 'chip-dismiss';
            durDismiss.title = 'Remove Duration';
            durDismiss.innerHTML = '&times;';
            durDismiss.addEventListener('click', function() {
                delete clause.duration;
                var dIdx = clause.modifier_order.indexOf('duration');
                if (dIdx !== -1) clause.modifier_order.splice(dIdx, 1);
                self.render();
                self.notifyChange();
            });
            durChip.appendChild(durDismiss);

            return durChip;
        }

        function createTimeScopeChip() {
            var tsChip = document.createElement('span');
            tsChip.className = 'sentence-modifier-chip';

            var tsLabel = document.createElement('span');
            tsLabel.className = 'chip-label';
            tsLabel.textContent = 'between';
            tsChip.appendChild(tsLabel);

            var startInput = document.createElement('input');
            startInput.type = 'time';
            startInput.className = 'sentence-input';
            startInput.style.width = '85px';
            startInput.style.padding = '2px 4px';
            startInput.style.fontSize = '0.85rem';
            startInput.value = clause.timeScope.start || '22:00';
            startInput.addEventListener('input', function() {
                clause.timeScope.start = startInput.value;
                self.notifyChange();
            });
            tsChip.appendChild(startInput);

            var andSpan = document.createElement('span');
            andSpan.className = 'chip-label';
            andSpan.textContent = 'and';
            tsChip.appendChild(andSpan);

            var endInput = document.createElement('input');
            endInput.type = 'time';
            endInput.className = 'sentence-input';
            endInput.style.width = '85px';
            endInput.style.padding = '2px 4px';
            endInput.style.fontSize = '0.85rem';
            endInput.value = clause.timeScope.end || '08:00';
            endInput.addEventListener('input', function() {
                clause.timeScope.end = endInput.value;
                self.notifyChange();
            });
            tsChip.appendChild(endInput);

            var tsDismiss = document.createElement('button');
            tsDismiss.type = 'button';
            tsDismiss.className = 'chip-dismiss';
            tsDismiss.title = 'Remove Time of Day';
            tsDismiss.innerHTML = '&times;';
            tsDismiss.addEventListener('click', function() {
                delete clause.timeScope;
                var tIdx = clause.modifier_order.indexOf('timeScope');
                if (tIdx !== -1) clause.modifier_order.splice(tIdx, 1);
                self.render();
                self.notifyChange();
            });
            tsChip.appendChild(tsDismiss);

            return tsChip;
        }

        function createPrecedingChip() {
            var precChip = document.createElement('span');
            precChip.className = 'sentence-modifier-chip';

            var precLabel = document.createElement('span');
            precLabel.className = 'chip-label';
            precLabel.textContent = 'in the preceding';
            precChip.appendChild(precLabel);

            var precInput = document.createElement('input');
            precInput.type = 'number';
            precInput.className = 'sentence-input';
            precInput.style.width = '55px';
            precInput.style.padding = '2px 6px';
            precInput.style.fontSize = '0.85rem';
            precInput.min = '1';
            precInput.value = clause.preceding.value !== undefined ? clause.preceding.value : 10;
            precInput.addEventListener('input', function() {
                var v = parseFloat(precInput.value);
                if (!isNaN(v) && v > 0) {
                    clause.preceding.value = v;
                    self.notifyChange();
                }
            });
            precChip.appendChild(precInput);

            var precUnitSelect = document.createElement('select');
            precUnitSelect.className = 'sentence-select';
            precUnitSelect.style.padding = '2px 6px';
            precUnitSelect.style.fontSize = '0.85rem';

            ['minutes', 'hours'].forEach(function(u) {
                var opt = document.createElement('option');
                opt.value = u;
                opt.textContent = u;
                if (clause.preceding.unit === u) opt.selected = true;
                precUnitSelect.appendChild(opt);
            });
            precUnitSelect.addEventListener('change', function() {
                clause.preceding.unit = precUnitSelect.value;
                self.notifyChange();
            });
            precChip.appendChild(precUnitSelect);

            var precDismiss = document.createElement('button');
            precDismiss.type = 'button';
            precDismiss.className = 'chip-dismiss';
            precDismiss.title = 'Remove Lookback';
            precDismiss.innerHTML = '&times;';
            precDismiss.addEventListener('click', function() {
                delete clause.preceding;
                var pIdx = clause.modifier_order.indexOf('preceding');
                if (pIdx !== -1) clause.modifier_order.splice(pIdx, 1);
                self.render();
                self.notifyChange();
            });
            precChip.appendChild(precDismiss);

            return precChip;
        }

        // 5. "+ Add Modifier" Button with Dropdown Menu
        var modWrap = document.createElement('div');
        modWrap.className = 'sentence-modifier-wrap';

        var modBtn = document.createElement('button');
        modBtn.type = 'button';
        modBtn.className = 'btn btn-secondary btn-sm sentence-modifier-btn';
        modBtn.style.padding = '3px 8px';
        modBtn.style.fontSize = '0.82rem';
        modBtn.innerHTML = '<i class="fa-solid fa-plus"></i> Add Modifier ⏷';

        var modMenu = document.createElement('div');
        modMenu.className = 'sentence-modifier-menu';

        // Evaluate modifiers matrix
        var hasDuration = !!clause.duration;
        var hasWindow = !!clause.window;
        var hasTimeScope = !!clause.timeScope;
        var hasPreceding = !!clause.preceding;

        // Modifier A: Moving Average (only Daily Standard, Daily Risk, Continuous)
        if (!isEvent) {
            var maItem = document.createElement('button');
            maItem.type = 'button';
            maItem.className = 'sentence-modifier-item';
            maItem.textContent = 'Moving Average';
            if (hasWindow) {
                maItem.disabled = true;
                maItem.title = 'Already added';
            } else if (hasDuration) {
                maItem.disabled = true;
                maItem.title = 'Mutually exclusive with Duration';
                maItem.textContent += ' (exclusive)';
            }
            maItem.addEventListener('click', function(e) {
                e.stopPropagation();
                modMenu.classList.remove('show');
                self.activeMenu = null;
                var defVal = category === 'continuous' ? 30 : 7;
                var defUnit = category === 'continuous' ? 'minutes' : 'days';
                clause.window = { type: 'rolling', value: defVal, unit: defUnit };
                delete clause.duration;
                var dIdx = clause.modifier_order.indexOf('duration');
                if (dIdx !== -1) clause.modifier_order.splice(dIdx, 1);
                if (clause.modifier_order.indexOf('window') === -1) clause.modifier_order.push('window');
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(maItem);
        }

        // Modifier B: Streak (for Daily Standard & Daily Risk)
        if (category === 'daily_standard' || category === 'daily_risk') {
            var streakItem = document.createElement('button');
            streakItem.type = 'button';
            streakItem.className = 'sentence-modifier-item';
            streakItem.textContent = 'Streak';
            if (hasDuration) {
                streakItem.disabled = true;
                streakItem.title = 'Already added';
            } else if (hasWindow) {
                streakItem.disabled = true;
                streakItem.title = 'Mutually exclusive with Moving Average';
                streakItem.textContent += ' (exclusive)';
            }
            streakItem.addEventListener('click', function(e) {
                e.stopPropagation();
                modMenu.classList.remove('show');
                self.activeMenu = null;
                clause.duration = { value: 3, unit: 'days' };
                delete clause.window;
                var wIdx = clause.modifier_order.indexOf('window');
                if (wIdx !== -1) clause.modifier_order.splice(wIdx, 1);
                if (clause.modifier_order.indexOf('duration') === -1) clause.modifier_order.push('duration');
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(streakItem);
        }

        // Modifier C: Sustained (for Continuous)
        if (category === 'continuous') {
            var sustItem = document.createElement('button');
            sustItem.type = 'button';
            sustItem.className = 'sentence-modifier-item';
            sustItem.textContent = 'Sustained Run';
            if (hasDuration) {
                sustItem.disabled = true;
                sustItem.title = 'Already added';
            } else if (hasWindow) {
                sustItem.disabled = true;
                sustItem.title = 'Mutually exclusive with Moving Average';
                sustItem.textContent += ' (exclusive)';
            }
            sustItem.addEventListener('click', function(e) {
                e.stopPropagation();
                modMenu.classList.remove('show');
                self.activeMenu = null;
                clause.duration = { value: 15, unit: 'minutes' };
                delete clause.window;
                var wIdx = clause.modifier_order.indexOf('window');
                if (wIdx !== -1) clause.modifier_order.splice(wIdx, 1);
                if (clause.modifier_order.indexOf('duration') === -1) clause.modifier_order.push('duration');
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(sustItem);
        }

        // Modifier D: Time of Day (Daily Standard, Continuous, Discrete Events)
        if (category !== 'daily_risk') {
            var todItem = document.createElement('button');
            todItem.type = 'button';
            todItem.className = 'sentence-modifier-item';
            todItem.textContent = 'Time of Day';
            if (hasTimeScope) {
                todItem.disabled = true;
                todItem.title = 'Already added';
            }
            todItem.addEventListener('click', function(e) {
                e.stopPropagation();
                modMenu.classList.remove('show');
                self.activeMenu = null;
                clause.timeScope = { start: '22:00', end: '08:00' };
                if (clause.modifier_order.indexOf('timeScope') === -1) clause.modifier_order.push('timeScope');
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(todItem);
        }

        // Modifier E: In the Preceding (Continuous & Discrete Events)
        if (category === 'continuous' || category === 'event') {
            var precItem = document.createElement('button');
            precItem.type = 'button';
            precItem.className = 'sentence-modifier-item';
            precItem.textContent = 'In the Preceding';
            if (hasPreceding) {
                precItem.disabled = true;
                precItem.title = 'Already added';
            }
            precItem.addEventListener('click', function(e) {
                e.stopPropagation();
                modMenu.classList.remove('show');
                self.activeMenu = null;
                clause.preceding = { value: 10, unit: 'hours' };
                if (clause.modifier_order.indexOf('preceding') === -1) clause.modifier_order.push('preceding');
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(precItem);
        }

        modBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (self.activeMenu && self.activeMenu !== modMenu) {
                self.activeMenu.classList.remove('show');
            }
            modMenu.classList.toggle('show');
            self.activeMenu = modMenu.classList.contains('show') ? modMenu : null;
        });

        modWrap.appendChild(modBtn);
        modWrap.appendChild(modMenu);

        // 6. Layout Sentence Row & Modifier Wrapping
        // Modifiers are rendered in the exact order added by the user
        var modifierChips = [];
        clause.modifier_order.forEach(function(modKey) {
            if (modKey === 'window' && clause.window && !isEvent) {
                modifierChips.push(createMaChip());
            } else if (modKey === 'duration' && clause.duration && !isEvent) {
                modifierChips.push(createDurChip());
            } else if (modKey === 'timeScope' && clause.timeScope) {
                modifierChips.push(createTimeScopeChip());
            } else if (modKey === 'preceding' && clause.preceding) {
                modifierChips.push(createPrecedingChip());
            }
        });

        if (modifierChips.length > 0) {
            // First modifier stays on Line 1
            sentenceRow.appendChild(modifierChips[0]);
        }

        if (modifierChips.length <= 1) {
            // Add Modifier button stays on Line 1
            sentenceRow.appendChild(modWrap);
            clauseBody.appendChild(sentenceRow);
        } else {
            // Subsequent modifiers wrap onto their own line(s)
            clauseBody.appendChild(sentenceRow);

            var subModifiersRow = document.createElement('div');
            subModifiersRow.className = 'sentence-sub-modifiers-row';
            for (var mIdx = 1; mIdx < modifierChips.length; mIdx++) {
                subModifiersRow.appendChild(modifierChips[mIdx]);
            }
            subModifiersRow.appendChild(modWrap);
            clauseBody.appendChild(subModifiersRow);
        }

        rowWrapper.appendChild(clauseBody);

        // Delete clause button (in smart filters mode)
        if (!isStandaloneTarget && parentGroup) {
            var deleteBtn = document.createElement('button');
            deleteBtn.type = 'button';
            deleteBtn.className = 'btn-ghost-icon';
            deleteBtn.style.color = 'var(--text-tertiary)';
            deleteBtn.style.marginLeft = 'auto';
            deleteBtn.style.fontSize = '1.2rem';
            deleteBtn.title = 'Remove condition';
            deleteBtn.innerHTML = '&times;';
            deleteBtn.addEventListener('click', function() {
                var idx = parentGroup.clauses.indexOf(clause);
                if (idx !== -1) {
                    parentGroup.clauses.splice(idx, 1);
                    self.render();
                    self.notifyChange();
                }
            });
            rowWrapper.appendChild(deleteBtn);
        }

        return rowWrapper;
    };

    NSFilterBuilderInstance.prototype.renderValueSentence = function(state) {
        var self = this;
        var metricKey = state.metric || 'tir';
        var cfg = self.metrics[metricKey] || {};

        var rowWrapper = document.createElement('div');
        rowWrapper.className = 'sentence-flow-row';
        rowWrapper.style.padding = '8px 4px';

        // 1. Prefix: "Show"
        var prefix = document.createElement('span');
        prefix.className = 'sentence-text';
        prefix.textContent = 'Show';
        rowWrapper.appendChild(prefix);

        // 2. Metric dropdown (Daily Clinical & Daily Risk metrics)
        var metricSelect = document.createElement('select');
        metricSelect.className = 'sentence-select';

        var standardKeys = ['tir', 'titr', 'tar', 'tbr', 'avg_bg', 'cv', 'gmi'];
        var riskKeys = ['tdd', 'carbs', 'lbgi', 'hbgi', 'gvi'];

        var optgroupStd = document.createElement('optgroup');
        optgroupStd.label = 'Clinical Metrics';
        standardKeys.forEach(function(mKey) {
            if (self.metrics[mKey]) {
                var opt = document.createElement('option');
                opt.value = mKey;
                opt.textContent = self.metrics[mKey].label;
                if (mKey === metricKey) opt.selected = true;
                optgroupStd.appendChild(opt);
            }
        });
        metricSelect.appendChild(optgroupStd);

        var optgroupRisk = document.createElement('optgroup');
        optgroupRisk.label = 'Therapy & Risk';
        riskKeys.forEach(function(mKey) {
            if (self.metrics[mKey]) {
                var opt = document.createElement('option');
                opt.value = mKey;
                opt.textContent = self.metrics[mKey].label;
                if (mKey === metricKey) opt.selected = true;
                optgroupRisk.appendChild(opt);
            }
        });
        metricSelect.appendChild(optgroupRisk);

        metricSelect.addEventListener('change', function() {
            state.metric = metricSelect.value;
            var newCfg = self.metrics[state.metric] || {};
            if (!newCfg.supports_timescope) {
                delete state.timeScope;
            }
            self.render();
            self.notifyChange();
        });
        rowWrapper.appendChild(metricSelect);

        // 3. "as"
        var asSpan = document.createElement('span');
        asSpan.className = 'sentence-text';
        asSpan.textContent = 'as';
        rowWrapper.appendChild(asSpan);

        // 4. Window / Aggregation dropdown
        var aggSelect = document.createElement('select');
        aggSelect.className = 'sentence-select';

        var currentVal = state.window_type === 'rolling' ? ('rolling:' + (state.rolling_days || 7)) : 'single_day';
        var aggChoices = [
            { id: 'single_day', label: "this day's value" },
            { id: 'rolling:7', label: "7-day moving average" },
            { id: 'rolling:14', label: "14-day moving average" },
            { id: 'rolling:30', label: "30-day moving average" },
            { id: 'rolling:60', label: "60-day moving average" },
            { id: 'rolling:90', label: "90-day moving average" }
        ];

        aggChoices.forEach(function(ch) {
            var opt = document.createElement('option');
            opt.value = ch.id;
            opt.textContent = ch.label;
            if (ch.id === currentVal) opt.selected = true;
            aggSelect.appendChild(opt);
        });

        aggSelect.addEventListener('change', function() {
            var val = aggSelect.value;
            if (val === 'single_day') {
                state.window_type = 'single_day';
                delete state.rolling_days;
            } else if (val.indexOf('rolling:') === 0) {
                state.window_type = 'rolling';
                state.rolling_days = parseInt(val.split(':')[1], 10);
            }
            self.notifyChange();
        });
        rowWrapper.appendChild(aggSelect);

        // 5. Optional Time of Day chip or button
        if (state.timeScope) {
            var tsChip = document.createElement('span');
            tsChip.className = 'sentence-modifier-chip';

            var tsText = document.createElement('span');
            var s = state.timeScope.start || '00:00';
            var e = state.timeScope.end || '23:59';
            if (s === '06:00' && e === '22:00') {
                tsText.textContent = 'during daytime (06:00–22:00)';
            } else if (s === '22:00' && e === '06:00') {
                tsText.textContent = 'overnight (22:00–06:00)';
            } else {
                tsText.textContent = 'between ' + s + ' and ' + e;
            }
            tsChip.appendChild(tsText);

            var removeBtn = document.createElement('span');
            removeBtn.className = 'sentence-chip-remove';
            removeBtn.innerHTML = '&times;';
            removeBtn.title = 'Remove time of day filter';
            removeBtn.addEventListener('click', function(ev) {
                ev.stopPropagation();
                delete state.timeScope;
                self.render();
                self.notifyChange();
            });
            tsChip.appendChild(removeBtn);
            rowWrapper.appendChild(tsChip);
        } else if (cfg.supports_timescope) {
            var modWrap = document.createElement('div');
            modWrap.className = 'sentence-modifier-wrap';

            var modBtn = document.createElement('button');
            modBtn.type = 'button';
            modBtn.className = 'btn btn-secondary btn-sm sentence-modifier-btn';
            modBtn.innerHTML = '<i class="fa-solid fa-plus"></i> Add Time of Day ⏷';

            var modMenu = document.createElement('div');
            modMenu.className = 'sentence-modifier-menu';

            var todDay = document.createElement('div');
            todDay.className = 'sentence-modifier-item';
            todDay.textContent = 'Daytime (06:00 – 22:00)';
            todDay.addEventListener('click', function() {
                state.timeScope = { start: '06:00', end: '22:00' };
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(todDay);

            var todNight = document.createElement('div');
            todNight.className = 'sentence-modifier-item';
            todNight.textContent = 'Overnight (22:00 – 06:00)';
            todNight.addEventListener('click', function() {
                state.timeScope = { start: '22:00', end: '06:00' };
                self.render();
                self.notifyChange();
            });
            modMenu.appendChild(todNight);

            modBtn.addEventListener('click', function(ev) {
                ev.stopPropagation();
                if (self.activeMenu && self.activeMenu !== modMenu) {
                    self.activeMenu.classList.remove('show');
                }
                modMenu.classList.toggle('show');
                self.activeMenu = modMenu.classList.contains('show') ? modMenu : null;
            });

            modWrap.appendChild(modBtn);
            modWrap.appendChild(modMenu);
            rowWrapper.appendChild(modWrap);
        }

        return rowWrapper;
    };

    // Public API
    window.NSFilterBuilder = {
        createFilterBuilder: function(options) {
            var inst = new NSFilterBuilderInstance(options);
            instances[options.containerId] = inst;
            return inst;
        },
        createTargetForm: function(options) {
            options.isTargetMode = true;
            var inst = new NSFilterBuilderInstance(options);
            instances[options.containerId] = inst;
            return inst;
        },
        getFilter: function(containerId) {
            return instances[containerId] ? instances[containerId].getFilter() : null;
        },
        setFilter: function(containerId, filter) {
            if (instances[containerId]) {
                instances[containerId].setFilter(filter);
            }
        },
        getMode: function(containerId) {
            return instances[containerId] ? instances[containerId].getMode() : 'filter';
        },
        setMode: function(containerId, mode) {
            if (instances[containerId]) {
                instances[containerId].setMode(mode);
            }
        },
        getTargetKind: function(containerId) {
            return instances[containerId] ? instances[containerId].getTargetKind() : 'evaluative';
        },
        setTargetKind: function(containerId, kind) {
            if (instances[containerId]) {
                instances[containerId].setTargetKind(kind);
            }
        }
    };

})(window);
