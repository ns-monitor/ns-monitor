/*
 * ns-orchestrator.js — Shared query orchestrator and data projection engine.
 *
 * Provides a clean two-stage pipeline for registry-driven charts:
 *   Stage 1: Fetch & Cache — Keyed by (endpoint, params, dateRange) for finite-range charts (Trends, Patterns, Calendar).
 *   Stage 2: In-Memory Projection — Synchronous projection of active series,
 *            moving-average weights, timestamps, and units without network calls.
 *
 * Used by Trends (timeline.html) and Trace (dashboard_daily.html), and reused by Patterns.
 */
(function (global) {
    'use strict';

    var _cache = {};

    function serializeParams(params) {
        if (!params) return '';
        var keys = Object.keys(params).sort();
        var pairs = [];
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i];
            var v = params[k];
            if (v !== undefined && v !== null && v !== '') {
                pairs.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
            }
        }
        return pairs.join('&');
    }

    function buildCacheKey(endpoint, params) {
        return endpoint + '?' + serializeParams(params);
    }

    function stableSerialize(obj) {
        if (obj === null || typeof obj !== 'object') {
            return JSON.stringify(obj);
        }
        if (Array.isArray(obj)) {
            return '[' + obj.map(stableSerialize).join(',') + ']';
        }
        var keys = Object.keys(obj).sort();
        var pairs = [];
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i];
            var v = obj[k];
            if (v !== undefined) {
                pairs.push(JSON.stringify(k) + ':' + stableSerialize(v));
            }
        }
        return '{' + pairs.join(',') + '}';
    }

    function buildPostCacheKey(endpoint, body) {
        return 'POST:' + endpoint + ':' + stableSerialize(body);
    }

    var NSOrchestrator = {
        /**
         * Stage 1: Fetch and cache data from a provider endpoint (finite-range access pattern).
         * @param {Object} provider - { endpoint: string, params: Object }
         * @param {Object} queryParams - { start_date: string, end_date: string, grain: string, ... }
         * @param {boolean} [forceRefresh=false]
         * @returns {Promise<Array<Object>>}
         */
        fetch: function (provider, queryParams, forceRefresh) {
            var mergedParams = Object.assign({}, provider.params || {}, queryParams || {});
            var cacheKey = buildCacheKey(provider.endpoint, mergedParams);

            if (!forceRefresh && _cache[cacheKey] && _cache[cacheKey].data) {
                return Promise.resolve(_cache[cacheKey].data);
            }

            if (!forceRefresh && _cache[cacheKey] && _cache[cacheKey].promise) {
                return _cache[cacheKey].promise;
            }

            var urlParams = serializeParams(mergedParams) + '&cb=' + Date.now();
            var fetchPromise = null;
            if (typeof window !== 'undefined' && window.nsInterceptFetch) {
                fetchPromise = window.nsInterceptFetch(provider.endpoint, urlParams, null);
            } else {
                var url = provider.endpoint + '?' + urlParams;
                fetchPromise = fetch(url)
                    .then(function (res) {
                        if (!res.ok) {
                            throw new Error('HTTP error ' + res.status + ' fetching ' + provider.endpoint);
                        }
                        return res.json();
                    });
            }

            fetchPromise = fetchPromise.then(function (data) {
                    _cache[cacheKey] = {
                        data: data,
                        timestamp: Date.now()
                    };
                    return data;
                })
                .catch(function (err) {
                    delete _cache[cacheKey];
                    throw err;
                });

            _cache[cacheKey] = {
                promise: fetchPromise,
                timestamp: Date.now()
            };

            return fetchPromise;
        },

        /**
         * Stage 1 (POST): Fetch and cache data from a provider endpoint using POST with a JSON body.
         * Used for flag provider (Filter & Target Engine evaluation) with arbitrarily nested Filter ASTs (§4).
         * @param {Object} provider - { endpoint: string, method?: string, body?: Object }
         * @param {Object} [bodyOverride] - Optional payload or overrides merged with provider.body
         * @param {boolean} [forceRefresh=false]
         * @returns {Promise<Object>}
         */
        fetchPost: function (provider, bodyOverride, forceRefresh) {
            var payload = Object.assign({}, provider.body || {}, bodyOverride || {});
            var cacheKey = buildPostCacheKey(provider.endpoint, payload);

            if (!forceRefresh && _cache[cacheKey] && _cache[cacheKey].data) {
                return Promise.resolve(_cache[cacheKey].data);
            }

            if (!forceRefresh && _cache[cacheKey] && _cache[cacheKey].promise) {
                return _cache[cacheKey].promise;
            }

            var fetchPromise = fetch(provider.endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            })
                .then(function (res) {
                    if (!res.ok) {
                        throw new Error('HTTP error ' + res.status + ' posting to ' + provider.endpoint);
                    }
                    return res.json();
                })
                .then(function (data) {
                    _cache[cacheKey] = {
                        data: data,
                        timestamp: Date.now()
                    };
                    return data;
                })
                .catch(function (err) {
                    delete _cache[cacheKey];
                    throw err;
                });

            _cache[cacheKey] = {
                promise: fetchPromise,
                timestamp: Date.now()
            };

            return fetchPromise;
        },

        /**
         * Stage 2 (Trends): Synchronous in-memory projection of numeric series data from a cached dataset.
         * Resolves column naming differences (e.g. uppercase 'TIR' vs 'tir_7d') and handles nulls.
         * @param {Array<Object>} data - Cached dataset rows
         * @param {Object} registryEntry - Metric definition from registry
         * @param {string|number} grainId - '1', '7', '14', '30', '60', '90', or 'monthly'
         * @returns {Array<number|null>}
         */
        projectTrends: function (data, registryEntry, grainId) {
            if (!data || !Array.isArray(data) || data.length === 0 || !registryEntry) {
                return [];
            }

            var baseKey = registryEntry.dataKey || registryEntry.id;
            var isDailyBase = (grainId === '1' || grainId === 1 || grainId === 'monthly');
            var targetKey;

            if (isDailyBase) {
                targetKey = baseKey;
            } else {
                targetKey = baseKey.toLowerCase() + '_' + grainId + 'd';
            }

            return data.map(function (row) {
                if (!row) return null;
                var rawVal = row[targetKey];
                if (rawVal === undefined || rawVal === null) {
                    if (isDailyBase) {
                        rawVal = row[baseKey.toUpperCase()];
                    }
                }
                if (rawVal === undefined || rawVal === null || rawVal === '') {
                    return null;
                }
                var num = typeof rawVal === 'number' ? rawVal : parseFloat(rawVal);
                return isNaN(num) ? null : num;
            });
        },

        /**
         * Stage 2 (Trace): Synchronous in-memory Cartesian projection of [timestamp, numericValue] points
         * from a continuous multi-day sliding buffer (incremental-window access pattern).
         * @param {Array<Object>} bufferData - Buffer rows from layer2_five_minute_aggregate
         * @param {Object} registryEntry - Metric definition from TRACE_REGISTRY
         * @returns {Array<Array>} Array of [timestampString, number|null] tuples
         */
        projectTrace: function (bufferData, registryEntry) {
            if (!bufferData || !Array.isArray(bufferData) || bufferData.length === 0 || !registryEntry) {
                return [];
            }
            var key = registryEntry.dataKey || registryEntry.id;
            return bufferData.map(function (row) {
                if (!row) return [null, null];
                var ts = row.local_ts || row.ts;
                var val = row[key];
                if (val === undefined || val === null || val === '') {
                    return [ts, null];
                }
                var num = typeof val === 'number' ? val : parseFloat(val);
                return [ts, isNaN(num) ? null : num];
            });
        },

        /**
         * Clears in-memory cache.
         */
        clearCache: function () {
            _cache = {};
        }
    };

    global.NSOrchestrator = NSOrchestrator;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { NSOrchestrator: NSOrchestrator };
    }
})(typeof window !== 'undefined' ? window : this);
