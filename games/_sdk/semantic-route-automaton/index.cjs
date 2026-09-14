'use strict';

const { createHash } = require('node:crypto');

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.keys(value).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`
    ));
    return `{${entries.join(',')}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new TypeError('semantic route values must be JSON-compatible');
  return encoded;
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function positiveLimit(value, fallback, name) {
  const resolved = value === undefined ? fallback : value;
  if (!Number.isInteger(resolved) || resolved < 1) {
    throw new TypeError(`${name} must be a positive integer`);
  }
  return resolved;
}

function normalizeToken(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('semantic route token must be an object');
  }
  if (typeof value.action !== 'string' || value.action.length === 0) {
    throw new TypeError('semantic route token action must be non-empty text');
  }
  if (!value.args || typeof value.args !== 'object' || Array.isArray(value.args)) {
    throw new TypeError('semantic route token args must be an object');
  }
  return JSON.parse(canonicalJson({ action:value.action, args:value.args }));
}

/**
 * Build the complete finite semantic route language without retaining concrete
 * authority states or materialising accepted action chains.
 *
 * Concrete states are visited depth-first and immediately reduced to their
 * right-language class.  The memo table stores only canonical state keys and
 * class ids; after a recursive call returns, that concrete child state is no
 * longer retained by this module.
 */
function createSemanticRouteAutomaton(initialState, hooks, limits = {}) {
  if (!hooks || typeof hooks !== 'object') throw new TypeError('semantic route hooks are required');
  for (const name of ['stateKey', 'boundary', 'successors']) {
    if (typeof hooks[name] !== 'function') throw new TypeError(`semantic route hook ${name} is required`);
  }
  const maxNodes = positiveLimit(limits.maxNodes, 100000, 'maxNodes');
  const maxTimeMs = positiveLimit(limits.maxTimeMs, 120000, 'maxTimeMs');
  const started = performance.now();
  let exactStates = 0;
  let transitionsExplored = 0;
  let boundaryStates = 0;
  let maxDepth = 0;
  let maxBranching = 0;
  let boundedReason = null;
  let peakHeapUsedBytes = process.memoryUsage().heapUsed;
  let peakRssBytes = process.memoryUsage().rss;

  const memo = new Map();
  const visiting = new Set();
  const classBySignature = new Map();
  const states = [];
  const pathCounts = [];
  const rightLanguageHashes = [];

  const sampleMemory = () => {
    const usage = process.memoryUsage();
    peakHeapUsedBytes = Math.max(peakHeapUsedBytes, usage.heapUsed);
    peakRssBytes = Math.max(peakRssBytes, usage.rss);
  };
  const overBudget = () => {
    if (exactStates >= maxNodes) {
      boundedReason ??= 'max_nodes';
      return true;
    }
    if (performance.now() - started >= maxTimeMs) {
      boundedReason ??= 'max_time';
      return true;
    }
    if (typeof hooks.cancelled === 'function' && hooks.cancelled()) {
      boundedReason ??= 'cancelled';
      return true;
    }
    return false;
  };
  const stateKey = (state) => {
    const key = hooks.stateKey(state);
    if (typeof key !== 'string' || key.length === 0) {
      throw new TypeError('semantic route state key must be non-empty text');
    }
    return key;
  };
  const intern = (final, transitions) => {
    const signature = canonicalJson({
      final,
      transitions:transitions.map((edge) => ({ token:edge.token, to:edge.to })),
    });
    const existing = classBySignature.get(signature);
    if (existing !== undefined) return existing;
    const id = states.length;
    const pathCount = (final ? 1n : 0n)
      + transitions.reduce((sum, edge) => sum + pathCounts[edge.to], 0n);
    states.push({
      final,
      transitions:transitions.map((edge) => ({ ...edge.token, to:edge.to })),
    });
    pathCounts[id] = pathCount;
    rightLanguageHashes[id] = sha256(signature);
    classBySignature.set(signature, id);
    return id;
  };

  const visit = (state, depth) => {
    if (typeof hooks.normalizeState === 'function') {
      state = hooks.normalizeState(state, depth) ?? state;
    }
    const key = stateKey(state);
    const cached = memo.get(key);
    if (cached !== undefined) return cached;
    if (visiting.has(key)) throw new Error('CYCLIC_SEMANTIC_ROUTE_GRAPH');
    if (overBudget()) return null;
    exactStates += 1;
    maxDepth = Math.max(maxDepth, depth);
    visiting.add(key);
    try {
      const boundary = hooks.boundary(state);
      if (boundary !== null && boundary !== undefined && boundary !== false) {
        boundaryStates += 1;
        const terminal = intern(true, []);
        memo.set(key, terminal);
        return terminal;
      }

      const final = typeof hooks.accepting === 'function'
        && hooks.accepting(state, depth) === true;
      const rawSuccessors = hooks.successors(state, depth);
      if (!Array.isArray(rawSuccessors)) {
        throw new TypeError('semantic route successors must be an array');
      }
      maxBranching = Math.max(maxBranching, rawSuccessors.length);
      const unique = new Map();
      for (const successor of rawSuccessors) {
        if (!successor || typeof successor !== 'object' || !('state' in successor)) {
          throw new TypeError('semantic route successor must contain state and token');
        }
        const token = normalizeToken(successor.token);
        const tokenKey = canonicalJson(token);
        const previous = unique.get(tokenKey);
        if (previous) {
          const previousKey = previous.nextKey ?? stateKey(previous.state);
          const nextKey = stateKey(successor.state);
          previous.nextKey = previousKey;
          if (previousKey !== nextKey) {
            throw new Error(`NONDETERMINISTIC_SEMANTIC_ACTION:${tokenKey}`);
          }
        }
        if (!previous) unique.set(tokenKey, { token, state:successor.state, nextKey:null });
      }
      const ordered = [...unique.entries()].sort((left, right) => left[0].localeCompare(right[0]));
      const transitions = [];
      for (const [, successor] of ordered) {
        if (overBudget()) return null;
        transitionsExplored += 1;
        const child = visit(successor.state, depth + 1);
        if (child === null) return null;
        transitions.push({ token:successor.token, to:child });
        if ((transitionsExplored & 127) === 0) sampleMemory();
      }
      if (transitions.length === 0 && !final) {
        throw new Error(`DEAD_END_SEMANTIC_ROUTE_STATE:${sha256(key)}`);
      }
      const classId = intern(final, transitions);
      memo.set(key, classId);
      return classId;
    } finally {
      visiting.delete(key);
    }
  };

  const startState = visit(initialState, 0);
  sampleMemory();
  const complete = startState !== null && boundedReason === null;
  const elapsedMs = Math.round((performance.now() - started) * 1000) / 1000;
  if (!complete) {
    return {
      schemaVersion:1,
      coverageStatus:'bounded',
      enumerationComplete:false,
      startState:null,
      routeCount:null,
      states:[],
      diagnostics:{
        boundedReason,
        exactStates,
        transitionsExplored,
        boundaryStates,
        maxDepth,
        maxBranching,
        elapsedMs,
        peakHeapUsedBytes,
        peakRssBytes,
      },
    };
  }
  return {
    schemaVersion:1,
    coverageStatus:'complete',
    enumerationComplete:true,
    startState,
    routeCount:pathCounts[startState].toString(),
    states,
    diagnostics:{
      boundedReason:null,
      exactStates,
      transitionsExplored,
      boundaryStates,
      maxDepth,
      maxBranching,
      minimizedStates:states.length,
      minimizedTransitions:states.reduce((sum, item) => sum + item.transitions.length, 0),
      rightLanguageHash:rightLanguageHashes[startState],
      elapsedMs,
      peakHeapUsedBytes,
      peakRssBytes,
    },
  };
}

module.exports = { createSemanticRouteAutomaton };
