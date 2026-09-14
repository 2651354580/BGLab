const crypto = require('node:crypto');

function canonicalFingerprint(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalFingerprint).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalFingerprint(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function defaultHash(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function makeRootId(decisionId, actionFingerprint, hash = defaultHash) {
  return hash(`root:${decisionId}:${actionFingerprint}`);
}

function makeProgramId(decisionId, snapshotFingerprint, stepFingerprint, hash = defaultHash) {
  return hash(`program:${decisionId}:${snapshotFingerprint}:${stepFingerprint}`);
}

function nestedValue(value, path) {
  return path.split('.').reduce((current, key) => current && current[key], value);
}

function publicStep(step) {
  if (!step || typeof step !== 'object') return step;
  if (step.op !== undefined || step.type === undefined) return step;
  // Adapters expose authoritative actions as { op, ...fields }, while some
  // engine hooks retain the equivalent internal { type, ...fields } shape.
  // Compare the public step projection so a factual filter sees exactly the
  // operation fields that routes returns, without exposing engine-only type.
  return Object.fromEntries(Object.entries(step).filter(([key]) => key !== 'type').concat([['op', step.type]]));
}

function matchesBounds(value, requirement) {
  return (requirement.min === undefined || value >= requirement.min)
    && (requirement.max === undefined || value <= requirement.max)
    && (requirement.equals === undefined || value === requirement.equals);
}

function matchesStepFact(step, fact) {
  const projected = publicStep(step);
  return Boolean(projected && fact && typeof projected === 'object' && typeof fact === 'object'
    && Object.keys(fact).every((key) => canonicalFingerprint(projected[key]) === canonicalFingerprint(fact[key])));
}

function containsStepFacts(steps, facts) {
  const requested = facts || [];
  const available = steps || [];
  const stepOwners = available.map(() => -1);
  function assign(factIndex, seenSteps) {
    for (let stepIndex = 0; stepIndex < available.length; stepIndex += 1) {
      if (seenSteps.has(stepIndex) || !matchesStepFact(available[stepIndex], requested[factIndex])) continue;
      seenSteps.add(stepIndex);
      if (stepOwners[stepIndex] === -1 || assign(stepOwners[stepIndex], seenSteps)) {
        stepOwners[stepIndex] = factIndex;
        return true;
      }
    }
    return false;
  }
  return requested.every((_fact, factIndex) => assign(factIndex, new Set()));
}

function advanceStepFactMatches(matches, action, facts) {
  const next = [matches];
  for (let index = 0; index < facts.length; index += 1) {
    if (matches[index] || !matchesStepFact(action, facts[index])) continue;
    const advanced = [...matches];
    advanced[index] = true;
    next.push(advanced);
  }
  return next;
}

function matchesRequirement(outcome, steps, requirement) {
  const prefix = 'stepsContain.';
  if (typeof requirement.metric === 'string' && requirement.metric.startsWith(prefix)) {
    const path = requirement.metric.slice(prefix.length);
    return path.length > 0 && (steps || []).some((step) => matchesBounds(nestedValue(publicStep(step), path), requirement));
  }
  return matchesBounds(nestedValue(outcome, requirement.metric), requirement);
}

function remainingResourceRanges(programs) {
  const values = new Map();
  function collect(value, path) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      const entries = values.get(path) || [];
      entries.push(value);
      values.set(path, entries);
      return;
    }
    if (value && typeof value === 'object') {
      for (const key of Object.keys(value).sort()) collect(value[key], `${path}.${key}`);
    }
  }
  for (const program of programs) collect(program.outcome && program.outcome.remaining, 'remaining');
  return Object.fromEntries([...values].map(([path, entries]) => [path, { min: Math.min(...entries), max: Math.max(...entries) }]));
}

function numericRanges(programs) {
  const values = new Map();
  for (const program of programs) {
    for (const [key, value] of Object.entries(program.outcome || {})) {
      if (!Number.isFinite(value)) continue;
      const entries = values.get(key) || [];
      entries.push(value);
      values.set(key, entries);
    }
  }
  return Object.fromEntries([...values].map(([key, entries]) => [key, { min: Math.min(...entries), max: Math.max(...entries) }]));
}

function comparePath(left, right) {
  if (left.steps.length !== right.steps.length) return left.steps.length - right.steps.length;
  return left.fingerprint.localeCompare(right.fingerprint);
}

function createFiniteProgramExplorer({ decisionId, snapshot, programs, pageSize = 12, hash = defaultHash }) {
  const snapshotFingerprint = hash(canonicalFingerprint(snapshot));
  let representatives = null;
  let roots = null;
  let ranges = null;
  let optionalExitCount = null;
  function materialize() {
    if (representatives) return;
    const terminals = new Map();
    for (const candidate of programs()) {
      const program = { ...candidate, outcome: candidate.outcome || candidate.netOutcome || {}, fingerprint: canonicalFingerprint(candidate.steps) };
      const existing = terminals.get(program.terminalKey);
      if (!existing || program.steps.length < existing.steps.length || (program.steps.length === existing.steps.length && program.fingerprint.localeCompare(existing.fingerprint) < 0)) terminals.set(program.terminalKey, program);
    }
    representatives = [...terminals.values()].sort((left, right) => left.steps.length - right.steps.length || left.fingerprint.localeCompare(right.fingerprint));
    roots = new Map();
    for (const program of representatives) {
      const rootId = makeRootId(decisionId, canonicalFingerprint(program.rootKey), hash);
      const root = roots.get(rootId) || { rootId, initialAction: program.rootAction, label: canonicalFingerprint(program.rootAction), programs: [], families: new Set() };
      root.programs.push(program);
      for (const event of program.causalTrace || []) if (event.family || event.effect) root.families.add(event.family || event.effect);
      roots.set(rootId, root);
    }
    ranges = remainingResourceRanges(representatives.map((program) => ({ outcome: program.outcome })));
    optionalExitCount = representatives.filter((program) => program.termination === 'declined_optional').length;
  }
  let active = null;
  function requirementFingerprint(rootIds, requirements, programId, stepsContain) { return canonicalFingerprint({ rootIds: rootIds || null, requirements: requirements || [], programId: programId || null, stepsContain: stepsContain || [] }); }
  function cursorFilter(cursor, request) {
    try {
      const filter = JSON.parse(cursor.filter);
      if (cursor.filter !== requirementFingerprint(filter.rootIds, filter.requirements, filter.programId, filter.stepsContain)) throw new Error('invalid');
      if (request.rootIds !== undefined && canonicalFingerprint(request.rootIds) !== canonicalFingerprint(filter.rootIds)) throw new Error('invalid');
      if (request.requirements !== undefined && canonicalFingerprint(request.requirements) !== canonicalFingerprint(filter.requirements)) throw new Error('invalid');
      if (request.programId !== undefined && request.programId !== filter.programId) throw new Error('invalid');
      if (request.stepsContain !== undefined && canonicalFingerprint(request.stepsContain) !== canonicalFingerprint(filter.stepsContain)) throw new Error('invalid');
      return filter;
    } catch {
      throw new Error('STALE_ROUTE_CURSOR');
    }
  }
  function freeze(rootIds, requirements, programId, stepsContain) {
    materialize();
    const filter = requirementFingerprint(rootIds, requirements, programId, stepsContain);
    if (active && active.filter === filter) return active;
    const values = representatives.filter((program) => (!rootIds || rootIds.includes(makeRootId(decisionId, canonicalFingerprint(program.rootKey), hash)))
      && (requirements || []).every((requirement) => matchesRequirement(program.outcome, program.steps, requirement))
      && containsStepFacts(program.steps, stepsContain)).map((program) => ({
      programId: makeProgramId(decisionId, snapshotFingerprint, program.fingerprint, hash),
      rootId: makeRootId(decisionId, canonicalFingerprint(program.rootKey), hash), boundaryReason: program.boundaryReason || 'turn_passed', termination: program.termination || 'automatic', steps: program.steps, causalTrace: program.causalTrace || [], outcome: program.outcome, netOutcome: program.outcome, complete: true,
    })).filter((program) => !programId || program.programId === programId);
    active = { enumerationId: hash(`finite:${decisionId}:${filter}`), filter, values };
    return active;
  }
  return {
    outcomeIndex(request = {}) {
      if (request.maxNodes !== undefined || request.maxTimeMs !== undefined) {
        return { decisionId, enumerationComplete: false, coverageStatus: 'not_explored', roots: [] };
      }
      materialize();
      return { decisionId, enumerationComplete: true, coverageStatus: 'complete', distinctBoundaryStateCount: representatives.length, optionalExitCount, remainingResourceRanges: ranges, roots: [...roots.values()].map((root) => ({ rootId: root.rootId, initialAction: root.initialAction, label: root.label, distinctTerminalStateCount: root.programs.length, reachable: numericRanges(root.programs), triggeredFamilies: [...root.families].sort() })) };
    },
    enumerateRoutes(request = {}) {
      materialize();
      if (request.cursor) {
        let cursor;
        try { cursor = JSON.parse(Buffer.from(request.cursor, 'base64url').toString('utf8')); }
        catch (_) { throw new Error('STALE_ROUTE_CURSOR'); }
        const filter = cursorFilter(cursor, request);
        const frozen = freeze(filter.rootIds, filter.requirements, filter.programId, filter.stepsContain);
        if (cursor.enumerationId !== frozen.enumerationId) throw new Error('STALE_ROUTE_CURSOR');
        const limit = Math.min(pageSize, request.limit || pageSize); const values = frozen.values.slice(cursor.position, cursor.position + limit); const position = cursor.position + values.length;
        return { status: 'routes', decisionId, enumerationId: frozen.enumerationId, programs: values, nextCursor: position < frozen.values.length ? Buffer.from(JSON.stringify({ enumerationId: frozen.enumerationId, filter: frozen.filter, position })).toString('base64url') : null };
      }
      const frozen = freeze(request.rootIds, request.requirements, request.programId, request.stepsContain); const limit = Math.min(pageSize, request.limit || pageSize); const values = frozen.values.slice(0, limit);
      return { status: 'routes', decisionId, enumerationId: frozen.enumerationId, programs: values, nextCursor: values.length < frozen.values.length ? Buffer.from(JSON.stringify({ enumerationId: frozen.enumerationId, filter: frozen.filter, position: values.length })).toString('base64url') : null };
    },
  };
}

// Shared route-enumeration/transaction-validation authority map. This is a small,
// protocol-level seam: adapters may supply complete-program candidates without
// changing their existing explorer entry points.  `commit` below only releases
// this in-memory map; it never invokes a game action sink.
function createDecisionMap({ frame, programs, pageSize = 20, limits = {}, hash = defaultHash, eager = false }) {
  function validateFrame(value) {
    if (!value || typeof value !== 'object') throw new Error('INVALID_DECISION_FRAME');
    for (const key of ['gameId', 'decisionId', 'stateHash']) {
      if (typeof value[key] !== 'string' || value[key].length === 0) throw new Error('INVALID_DECISION_FRAME');
    }
    if (!Number.isInteger(value.seat) || value.seat < 0) throw new Error('INVALID_DECISION_FRAME');
    return { gameId: value.gameId, decisionId: value.decisionId, seat: value.seat, stateHash: value.stateHash };
  }

  const sourceIsFunction = typeof programs === 'function';
  const source = sourceIsFunction ? programs : () => programs || [];
  const eagerFastPath = !sourceIsFunction || (eager === true && source.length === 0);
  const limitsCopy = {
    maxNodes: Number.isFinite(limits.maxNodes) ? limits.maxNodes : Infinity,
    maxTimeMs: Number.isFinite(limits.maxTimeMs) ? limits.maxTimeMs : Infinity,
  };
  let activeFrame = validateFrame(frame);
  let records = [];
  let recordsByFingerprint = new Map();
  let directFingerprints = new Set();
  let nodes = new Map();
  let edges = new Map();
  let issued = new Map();
  let eagerMaterialized = false;
  let queryCache = new Map();
  let coverageByQuery = new Map();
  let sourceReads = 0;
  let materializationCount = 0;
  let nextProgramOrdinal = 1;
  let released = false;

  function frameFingerprint(value = activeFrame) { return canonicalFingerprint(value); }
  function stepFingerprint(value) { return canonicalFingerprint(value || []); }
  function sameStep(left, right) { return canonicalFingerprint(left) === canonicalFingerprint(right); }
  function stepSetFingerprint(steps) {
    return canonicalFingerprint((Array.isArray(steps) ? steps : [])
      .map((step) => canonicalFingerprint(step))
      .sort());
  }
  function proposalSteps(proposal) { return Array.isArray(proposal?.steps) ? proposal.steps : []; }
  function prefixFingerprint(steps) { return stepFingerprint(steps); }

  function resetStorage() {
    records = [];
    recordsByFingerprint = new Map();
    directFingerprints = new Set();
    nodes = new Map();
    edges = new Map();
    issued = new Map();
    eagerMaterialized = false;
    queryCache = new Map();
    coverageByQuery = new Map();
    sourceReads = 0;
    materializationCount = 0;
    nextProgramOrdinal = 1;
    released = false;
  }

  function normalizeProgram(candidate) {
    const steps = Array.isArray(candidate?.steps) ? candidate.steps.map((step) => structuredClone(step)) : [];
    const outcome = structuredClone(candidate?.outcome || candidate?.netOutcome || {});
    const canonical = candidate?.canonicalFingerprint || hash(canonicalFingerprint({
      steps,
      outcome,
      ...(candidate?.terminalKey !== undefined ? { terminalKey: candidate.terminalKey } : {}),
    }));
    return {
      ...structuredClone(candidate || {}),
      canonicalFingerprint: canonical,
      _sortKey: stepFingerprint(steps),
      steps,
      outcome,
      complete: true,
    };
  }

  function addGraph(program) {
    let previous = '<root>';
    nodes.set(previous, { key: previous, depth: 0 });
    program.steps.forEach((step, index) => {
      const next = `${previous}/${hash(stepFingerprint(program.steps.slice(0, index + 1))).slice(0, 24)}`;
      nodes.set(next, { key: next, depth: index + 1 });
      const edgeKey = `${previous}->${next}`;
      if (!edges.has(edgeKey)) edges.set(edgeKey, {
        key: edgeKey, from: previous, to: next, action: structuredClone(step), depth: index + 1,
      });
      previous = next;
    });
  }

  function addRecord(candidate, preferredProgramId = null) {
    const normalized = normalizeProgram(candidate);
    const existing = recordsByFingerprint.get(normalized.canonicalFingerprint);
    if (existing) return existing;
    let programId = preferredProgramId;
    if (programId !== null) {
      if (!/^p[1-9]\d*$/.test(programId)
        || records.some((program) => program.programId === programId)) {
        throw new Error('RESTORE_PROGRAM_MISMATCH');
      }
      nextProgramOrdinal = Math.max(nextProgramOrdinal, Number(programId.slice(1)) + 1);
    } else {
      while (records.some((program) => program.programId === `p${nextProgramOrdinal}`)) {
        nextProgramOrdinal += 1;
      }
      programId = `p${nextProgramOrdinal}`;
      nextProgramOrdinal += 1;
    }
    const program = { ...normalized, programId };
    records.push(program);
    recordsByFingerprint.set(program.canonicalFingerprint, program);
    addGraph(program);
    return program;
  }

  function queryKey(proposal, page, requestedSize) {
    return canonicalFingerprint({
      proposal: proposal || {},
      page,
      pageSize: requestedSize,
    });
  }

  function readSource({ proposal = {}, page = 1, requestedSize = pageSize } = {}) {
    // An eager source is materialized once and retained in `records`.  Later
    // pages (or repeat queries) must read that immutable retained set rather
    // than an empty result, otherwise pagination reports complete coverage
    // while page two silently loses every remaining program.
    if (eagerMaterialized) return { coverageStatus: 'complete', values: records };
    const key = queryKey(proposal, page, requestedSize);
    if (queryCache.has(key)) return queryCache.get(key);
    const started = Date.now();
    sourceReads += 1;
    materializationCount += 1;
    const raw = source({
      frame: structuredClone(activeFrame),
      proposal: structuredClone(proposal),
      prefix: structuredClone(proposalSteps(proposal)),
      page,
      pageSize: requestedSize,
    }) || [];
    const isEnvelope = raw && typeof raw === 'object' && !Array.isArray(raw)
      && ('programs' in raw || 'coverageStatus' in raw);
    const candidates = isEnvelope ? (raw.programs || []) : raw;
    const coverageStatus = isEnvelope && raw.coverageStatus ? raw.coverageStatus : 'complete';
    const values = [];
    for (const candidate of candidates) {
      if (Date.now() - started >= limitsCopy.maxTimeMs) break;
      if (values.length >= limitsCopy.maxNodes) break;
      values.push(normalizeProgram(candidate));
    }
    values
      .sort((left, right) => left.steps.length - right.steps.length
        || left._sortKey.localeCompare(right._sortKey))
      .forEach((program) => addRecord(program));
    const result = { coverageStatus, values: values.map((program) => recordsByFingerprint.get(program.canonicalFingerprint)) };
    queryCache.set(key, result);
    coverageByQuery.set(key, coverageStatus);
    if (!sourceIsFunction || (eagerFastPath && !isEnvelope)) eagerMaterialized = true;
    return result;
  }

  function commonPrefixLength(left, right) {
    const limit = Math.min(left.length, right.length);
    let index = 0;
    while (index < limit && sameStep(left[index], right[index])) index += 1;
    return index;
  }

  function publicProgram(program, proposalFingerprint) {
    issued.set(program.programId, { program, proposalFingerprint });
    return {
      programId: program.programId,
      steps: structuredClone(program.steps),
      outcome: structuredClone(program.outcome),
      complete: true,
      ...(Array.isArray(program.causalTrace)
        ? { causalTrace: structuredClone(program.causalTrace) } : {}),
      ...(program.netOutcome && typeof program.netOutcome === 'object'
        ? { netOutcome: structuredClone(program.netOutcome) } : {}),
      ...(program.rootId ? { rootId: program.rootId } : {}),
      ...(program.boundaryReason ? { boundaryReason: program.boundaryReason } : {}),
      ...(program.termination ? { termination: program.termination } : {}),
    };
  }

  function issueComplete(candidate = {}) {
    if (released) throw new Error('STOPPED_DECISION_MAP');
    if (candidate?.complete === false || !Array.isArray(candidate?.steps) || candidate.steps.length === 0) {
      throw new Error('INCOMPLETE_PROGRAM');
    }
    const program = addRecord(candidate);
    directFingerprints.add(program.canonicalFingerprint);
    const proposalFingerprint = canonicalFingerprint({
      intent: candidate.intent || '',
      steps: program.steps,
    });
    return publicProgram(program, proposalFingerprint);
  }

  function snapshot() {
    const queryStatuses = [...coverageByQuery.values()];
    const coverageStatus = eagerMaterialized || (queryStatuses.length > 0 && queryStatuses.every((status) => status === 'complete'))
      ? 'complete' : 'unknown';
    return {
      decisionFrame: structuredClone(activeFrame),
      released,
      coverageStatus: released ? 'stopped' : coverageStatus,
      nodes: nodes.size,
      edges: edges.size,
      programs: records.length,
      retainedNodes: nodes.size,
      retainedEdges: edges.size,
      retainedPrograms: records.length,
      wholePrograms: records.map((program) => program.programId),
      issuedProgramIds: [...issued.keys()],
      sourceReads,
      materializationCount,
    };
  }

  function enumerateRoutes(request = {}) {
    if (released) return {
      status: 'stopped', decisionFrame: structuredClone(activeFrame), programs: [],
      page: 1, pageSize, totalMatches: 0, other: null, wholeProgram: false, truncated: false,
    };
    const proposal = request.proposal || {};
    const steps = proposalSteps(proposal);
    const proposalFingerprint = canonicalFingerprint({ intent: proposal.intent || '', steps });
    const page = Number.isInteger(request.page) && request.page > 0 ? request.page : 1;
    const requestedSize = Number.isInteger(request.pageSize) && request.pageSize > 0 ? request.pageSize : pageSize;
    // A clone-validated program issued through `issueComplete` is already
    // authoritative for every step in the proposal prefix.  Reading the
    // potentially expensive global source again cannot strengthen that proof
    // and can time out before returning the known program.  Keep alternatives
    // explicitly unknown until the caller asks a query that is not covered by
    // the directly issued program.
    const directPrefix = steps.length > 0 && records.find((program) =>
      directFingerprints.has(program.canonicalFingerprint)
      && commonPrefixLength(program.steps, steps) === steps.length);
    const query = directPrefix
      ? { coverageStatus: 'unknown', values: [] }
      : readSource({ proposal, page, requestedSize });
    const queryRecords = [...new Map([
      ...(query.values || []).map((program) => [program.canonicalFingerprint, program]),
      ...records
        .filter((program) => directFingerprints.has(program.canonicalFingerprint))
        .map((program) => [program.canonicalFingerprint, program]),
    ]).values()];
    const exact = queryRecords.find((program) => program.steps.length === steps.length
      && commonPrefixLength(program.steps, steps) === steps.length);
    const sameStepSet = exact ? [] : queryRecords.filter((program) =>
      program.steps.length === steps.length
      && stepSetFingerprint(program.steps) === stepSetFingerprint(steps));
    let longest = 0;
    for (const program of queryRecords) longest = Math.max(longest, commonPrefixLength(program.steps, steps));
    const matching = exact
      ? [exact]
      : (sameStepSet.length > 0
        ? sameStepSet
        : (longest > 0 || steps.length === 0
        ? queryRecords.filter((program) => commonPrefixLength(program.steps, steps) === longest
          && program.steps.length >= longest)
        : []));
    const start = (page - 1) * requestedSize;
    const values = matching.slice(start, start + requestedSize).map((program) => publicProgram(program, proposalFingerprint));
    const end = start + values.length;
    const other = end < matching.length ? { page: page + 1, pageSize: requestedSize, total: matching.length } : null;
    const unknown = query.coverageStatus !== 'complete' || (matching.length === 0 && !exact && steps.length > 0);
    return {
      status: 'routes',
      decisionFrame: structuredClone(activeFrame),
      proposalFingerprint,
      exactMatch: Boolean(exact),
      sameStepSetMatch: !exact && sameStepSet.length > 0,
      longestLegalPrefix: longest,
      coverageStatus: unknown ? 'unknown' : 'complete',
      ...(unknown ? { unknownRegions: [prefixFingerprint(steps)] } : {}),
      programs: values,
      page,
      pageSize: requestedSize,
      totalMatches: matching.length,
      other,
      wholeProgram: values.length > 0 && values.every((program) => program.complete === true),
      truncated: false,
    };
  }

  function replaceFrame(nextFrame) {
    const next = validateFrame(nextFrame);
    if (frameFingerprint(next) === frameFingerprint()) return snapshot();
    activeFrame = next;
    resetStorage();
    return snapshot();
  }

  // Ownership-only commit: this releases the map and deliberately does not
  // call a game Adapter or action sink.
  function commit(programId) {
    const known = issued.has(programId);
    resetStorage();
    released = true;
    return { ok: known, released: true, programId, ...(known ? {} : { code: 'UNKNOWN_PROGRAM_ID' }) };
  }

  function stop() {
    resetStorage();
    released = true;
    return { released: true };
  }

  function serializeIssuedMappings() {
    return {
      version: 1,
      decisionFrame: structuredClone(activeFrame),
      mappings: [...issued.values()].map(({ program, proposalFingerprint }) => ({
        decisionFrame: structuredClone(activeFrame),
        proposalFingerprint,
        programId: program.programId,
        canonicalFingerprint: program.canonicalFingerprint,
        steps: structuredClone(program.steps),
        outcome: structuredClone(program.outcome),
        ...(program.terminalKey !== undefined ? { terminalKey: structuredClone(program.terminalKey) } : {}),
        directIssued: directFingerprints.has(program.canonicalFingerprint),
      })),
    };
  }

  function restoreIssuedMappings(serialized, { revalidate } = {}) {
    if (typeof revalidate !== 'function') throw new Error('RESTORE_REVALIDATION_REQUIRED');
    if (!serialized || frameFingerprint(serialized.decisionFrame) !== frameFingerprint()) throw new Error('STALE_DECISION_FRAME');
    const mappings = Array.isArray(serialized.mappings) ? serialized.mappings : [];
    const pending = [];
    for (const mapping of mappings) {
      if (!mapping || frameFingerprint(mapping.decisionFrame) !== frameFingerprint()) throw new Error('STALE_DECISION_FRAME');
      if (!revalidate(structuredClone(mapping.steps), structuredClone(mapping))) throw new Error('RESTORE_REVALIDATION_FAILED');
      if (mapping.directIssued === true) {
        const candidate = normalizeProgram({
          steps: mapping.steps,
          outcome: mapping.outcome,
          ...(mapping.terminalKey !== undefined ? { terminalKey: mapping.terminalKey } : {}),
        });
        if (candidate.canonicalFingerprint !== mapping.canonicalFingerprint) throw new Error('RESTORE_PROGRAM_MISMATCH');
        pending.push({ candidate, mapping, proposalFingerprint: mapping.proposalFingerprint, direct: true });
        continue;
      }
      readSource({ proposal: { steps: mapping.steps }, page: 1, requestedSize: pageSize });
      const program = records.find((candidate) => candidate.programId === mapping.programId
        && candidate.canonicalFingerprint === mapping.canonicalFingerprint
        && canonicalFingerprint(candidate.steps) === canonicalFingerprint(mapping.steps));
      if (!program) throw new Error('RESTORE_PROGRAM_MISMATCH');
      pending.push({ program, proposalFingerprint: mapping.proposalFingerprint, direct: false });
    }
    const resolved = pending.map((entry) => {
      if (!entry.direct) return entry;
      const program = addRecord(entry.candidate, entry.mapping.programId);
      if (program.programId !== entry.mapping.programId) throw new Error('RESTORE_PROGRAM_MISMATCH');
      directFingerprints.add(program.canonicalFingerprint);
      return { ...entry, program };
    });
    for (const { program, proposalFingerprint } of resolved) issued.set(program.programId, { program, proposalFingerprint });
    return { restored: mappings.length, issuedProgramIds: [...issued.keys()] };
  }

  return {
    enumerateRoutes,
    issueComplete,
    snapshot,
    replaceFrame,
    commit,
    stop,
    serializeIssuedMappings,
    restoreIssuedMappings,
  };
}

function createAuthorityGateway({
  currentIdentity,
  finiteExplorerForSeat,
  programsForSeat,
  decisionMapFactory,
  decisionMapPageSize = 20,
}) {
  if (typeof currentIdentity !== 'function'
    || typeof finiteExplorerForSeat !== 'function'
    || (decisionMapFactory !== undefined && typeof decisionMapFactory !== 'function')
    || (typeof decisionMapFactory !== 'function' && typeof programsForSeat !== 'function')) {
    throw new Error('INVALID_AUTHORITY_GATEWAY_OPTIONS');
  }
  const pageSize = Number.isInteger(decisionMapPageSize) && decisionMapPageSize > 0
    ? decisionMapPageSize : 20;
  let activeDecisionMap = null;
  let activeFiniteExplorer = null;

  function identity() {
    const value = currentIdentity();
    if (!value || typeof value.decisionId !== 'string' || !value.decisionId
      || !Number.isInteger(value.seat) || value.seat < 0) {
      throw new Error('INVALID_AUTHORITY_IDENTITY');
    }
    return value;
  }

  function frameMatches(frame, current) {
    return Boolean(frame && typeof frame === 'object'
      && typeof frame.gameId === 'string' && frame.gameId
      && typeof frame.decisionId === 'string' && frame.decisionId === current.decisionId
      && Number.isInteger(frame.seat) && frame.seat === current.seat
      && typeof frame.stateHash === 'string' && frame.stateHash);
  }

  function invalidate() {
    const released = activeDecisionMap
      ? activeDecisionMap.value.stop()
      : { released:false };
    activeDecisionMap = null;
    activeFiniteExplorer = null;
    return released;
  }

  function finiteExplorer(current) {
    const key = `${current.decisionId}:${current.seat}`;
    if (activeFiniteExplorer?.key === key) return activeFiniteExplorer.value;
    activeFiniteExplorer = null;
    const value = finiteExplorerForSeat(current.seat);
    if (!value || typeof value.enumerateRoutes !== 'function') {
      throw new Error('INVALID_FINITE_EXPLORER');
    }
    activeFiniteExplorer = { key, value };
    return value;
  }

  function decisionMapFor(frame, seat) {
    const key = canonicalFingerprint(frame);
    if (activeDecisionMap?.key === key) return activeDecisionMap.value;
    invalidate();
    const value = typeof decisionMapFactory === 'function'
      ? decisionMapFactory({
        frame,
        seat,
        pageSize,
        finiteExplorer:finiteExplorer(identity()),
      })
      : createDecisionMap({
        frame,
        pageSize,
        eager:true,
        programs:() => programsForSeat(seat),
      });
    if (!value || typeof value.enumerateRoutes !== 'function' || typeof value.stop !== 'function') {
      throw new Error('INVALID_DECISION_MAP_FACTORY');
    }
    activeDecisionMap = { key, value };
    return value;
  }

  function enumerateRoutes(request = {}) {
    const normalized = request && typeof request === 'object' && !Array.isArray(request)
      ? request : {};
    const current = identity();
    if (normalized.proposal !== undefined) {
      if (!frameMatches(normalized.decisionFrame, current)) {
        return {
          status:'invalid',
          stateChanged:false,
          error:{
            code:'STALE_DECISION_FRAME',
            message:'Refresh the current decision frame before proposal routes.',
          },
        };
      }
      return decisionMapFor(normalized.decisionFrame, current.seat).enumerateRoutes({
        proposal:normalized.proposal,
        page:normalized.page,
        pageSize:normalized.pageSize,
      });
    }
    return finiteExplorer(current).enumerateRoutes(normalized);
  }

  function outcomeIndex(request = {}) {
    const current = identity();
    const explorer = finiteExplorer(current);
    if (typeof explorer.outcomeIndex !== 'function') {
      throw new Error('FINITE_EXPLORER_OUTCOME_INDEX_REQUIRED');
    }
    return explorer.outcomeIndex(request);
  }

  return { enumerateRoutes, outcomeIndex, invalidate };
}

function createDecisionExplorer(initialState, seat, hooks, limits) {
  const now = hooks.now || limits.now || Date.now;
  const hash = hooks.hash || limits.hash || defaultHash;
  const decisionId = hooks.decisionId ? hooks.decisionId(initialState) : hash(canonicalFingerprint(initialState));
  const initialSnapshotFingerprint = hash(canonicalFingerprint(initialState));
  const { maxNodes, maxTimeMs, pageSize } = limits;
  const initialKey = hash(`state:${hooks.canonicalAuthorityKey(initialState)}`);
  const nodes = new Map();
  const edges = [];
  const edgesBySource = new Map();
  const roots = new Map();
  const rootOrder = [];
  const frontiers = new Map();
  const rootCoverage = new Map();
  const queuedNodes = new Set();
  const expandedNodes = new Set();
  const strategicWitnesses = new Map();
  let nodesExplored = 0;
  let transitionsApplied = 0;
  let graphVersion = 0;
  let exhausted = false;
  let activeEnumeration = null;
  let allPathEnumeration = null;
  let decisionStartedAt = null;

  function actionFingerprint(action) { return hooks.actionFingerprint(action); }
  function sortedActions(state) { return [...hooks.getLegalActions(state)].sort((left, right) => actionFingerprint(left).localeCompare(actionFingerprint(right))); }
  function describe(state) {
    const legalActions = sortedActions(state);
    const classification = hooks.classifyState(initialState, state, legalActions);
    if (classification.status === 'may_stop' && !legalActions.some((action) => hooks.isDeclineAction(action, state))) throw new Error('OPTIONAL_STATE_WITHOUT_DECLINE_ACTION');
    return { legalActions, classification };
  }
  function registerNode(state) {
    const storedState = hooks.cloneState(state);
    const description = describe(storedState);
    const key = hash(`state:${hooks.canonicalAuthorityKey(storedState)}`);
    const legalFingerprint = description.legalActions.map(actionFingerprint).join('|');
    const publicOutcome = description.classification.status === 'boundary' ? hooks.projectAuthorityOutcome(seat, initialState, storedState) : null;
    const safetyFingerprint = canonicalFingerprint({ legalFingerprint, classification: description.classification, publicOutcome });
    const existing = nodes.get(key);
    if (existing) {
      if (existing.safetyFingerprint !== safetyFingerprint) throw new Error('CANONICAL_STATE_COLLISION');
      return { key, record: existing, created: false };
    }
    const record = { key, state: storedState, ...description, legalFingerprint, publicOutcome, safetyFingerprint };
    nodes.set(key, record);
    graphVersion += 1;
    return { key, record, created: true };
  }
  function appendEdge(from, to, rootId, action, fingerprint, causalEvents) {
    const edge = { id: edges.length, from, to, rootId, actionFingerprint: fingerprint, action, causalEvents };
    edges.push(edge);
    const outgoing = edgesBySource.get(from) || [];
    outgoing.push(edge);
    edgesBySource.set(from, outgoing);
    graphVersion += 1;
    return edge;
  }
  function enqueue(rootId, nodeKey) {
    if (queuedNodes.has(nodeKey) || expandedNodes.has(nodeKey)) return;
    queuedNodes.add(nodeKey);
    frontiers.get(rootId).push({ nodeKey, rootId });
  }
  function initializeRoots() {
    if (rootOrder.length) return;
    const initialRecord = registerNode(initialState).record;
    for (const action of initialRecord.legalActions) {
      const fingerprint = actionFingerprint(action);
      const rootId = makeRootId(decisionId, fingerprint, hash);
      const applied = hooks.applyAction(hooks.cloneState(initialRecord.state), action);
      transitionsApplied += 1;
      const target = registerNode(applied.state);
      appendEdge(initialKey, target.key, rootId, action, fingerprint, hooks.traceTransition(initialRecord.state, action, applied.state, applied.events));
      roots.set(rootId, { rootId, action, actionFingerprint: fingerprint, targetKey: target.key });
      rootOrder.push(rootId);
      rootCoverage.set(rootId, { status: 'not_explored', nodesExplored: 0 });
      frontiers.set(rootId, []);
      enqueue(rootId, target.key);
    }
  }
  function requestedBudget(request) {
    const limit = (value, ceiling) => Number.isFinite(value) && value >= 0 ? Math.min(ceiling, value) : ceiling;
    return {
      maxNodes: limit(request.maxNodes, maxNodes),
      maxTimeMs: limit(request.maxTimeMs, maxTimeMs),
    };
  }
  function budgetReached(start, budget) {
    return nodesExplored >= budget.maxNodes
      || now() - start >= budget.maxTimeMs
      || now() - decisionStartedAt >= maxTimeMs;
  }
  function explore(request = {}) {
    if (decisionStartedAt === null) decisionStartedAt = now();
    initializeRoots();
    if (exhausted) return;
    const requested = request.rootIds ? [...request.rootIds].sort() : rootOrder;
    const allowed = requested.filter((rootId) => roots.has(rootId));
    const coveragePriorityNodes = new Set();
    const promoteQueuedNode = (nodeKey) => {
      for (const rootId of allowed) {
        const frontier = frontiers.get(rootId);
        const index = frontier.findIndex((entry) => entry.nodeKey === nodeKey);
        if (index <= 0) continue;
        const [entry] = frontier.splice(index, 1);
        frontier.unshift(entry);
      }
    };
    const prioritizeCoverageBranch = (nodeKey) => {
      const pending = [nodeKey];
      const seen = new Set();
      while (pending.length) {
        const current = pending.shift();
        if (seen.has(current)) continue;
        seen.add(current);
        coveragePriorityNodes.add(current);
        promoteQueuedNode(current);
        if (expandedNodes.has(current)) {
          for (const edge of edgesBySource.get(current) || []) pending.push(edge.to);
        }
      }
    };
    if (request.coverageTarget) {
      for (const edge of edges) {
        if ((request.rootIds && !request.rootIds.includes(edge.rootId))
          || !request.stepsContain.some((fact) => matchesStepFact(edge.action, fact))) continue;
        prioritizeCoverageBranch(edge.to);
      }
    }
    const start = now();
    const budget = requestedBudget(request);
    let advanced = true;
    while (advanced && !budgetReached(start, budget)) {
      advanced = false;
      const priorityRoot = request.coverageTarget
        ? allowed.find((rootId) => frontiers.get(rootId)
          .some((entry) => coveragePriorityNodes.has(entry.nodeKey)))
        : undefined;
      for (const rootId of priorityRoot ? [priorityRoot] : allowed) {
        if (budgetReached(start, budget)) break;
        const frontier = frontiers.get(rootId);
        const entry = frontier.shift();
        if (!entry) continue;
        advanced = true;
        const prioritized = coveragePriorityNodes.delete(entry.nodeKey);
        if (expandedNodes.has(entry.nodeKey)) continue;
        expandedNodes.add(entry.nodeKey);
        const record = nodes.get(entry.nodeKey);
        nodesExplored += 1;
        rootCoverage.set(rootId, { status: 'exploring', nodesExplored: (rootCoverage.get(rootId).nodesExplored || 0) + 1 });
        if (record.classification.status === 'boundary') continue;
        const inheritedCoverageTargets = [];
        const directCoverageTargets = [];
        for (const action of record.legalActions) {
          const fingerprint = actionFingerprint(action);
          const applied = hooks.applyAction(hooks.cloneState(record.state), action);
          transitionsApplied += 1;
          const target = registerNode(applied.state);
          appendEdge(record.key, target.key, rootId, action, fingerprint, hooks.traceTransition(record.state, action, applied.state, applied.events));
          enqueue(rootId, target.key);
          if (request.coverageTarget) {
            if (prioritized) inheritedCoverageTargets.push(target.key);
            if (request.stepsContain.some((fact) => matchesStepFact(action, fact))) {
              directCoverageTargets.push(target.key);
            }
          }
        }
        for (const nodeKey of inheritedCoverageTargets) prioritizeCoverageBranch(nodeKey);
        // Direct factual matches are promoted last so they stay ahead of
        // unrelated descendants inherited from an earlier matched step.
        for (const nodeKey of directCoverageTargets) prioritizeCoverageBranch(nodeKey);
        if (request.coverageTarget) {
          const witnessDeadline = Math.min(
            start + budget.maxTimeMs,
            decisionStartedAt + maxTimeMs,
          );
          const witness = programWitness(
            request.stepsContain,
            request.rootIds,
            request.coverageEffects,
            witnessDeadline,
          );
          if (witness) {
            const witnessKey = canonicalFingerprint({
              rootIds: request.rootIds || null,
              stepsContain: request.stepsContain,
              requiredEffects: request.coverageEffects || [],
            });
            strategicWitnesses.set(witnessKey, witness);
            advanced = false;
            break;
          }
        }
      }
    }
    exhausted = [...frontiers.values()].every((frontier) => frontier.length === 0);
    for (const rootId of rootOrder) {
      const coverage = rootCoverage.get(rootId);
      rootCoverage.set(rootId, { ...coverage, status: exhausted || !frontiers.get(rootId).length ? 'complete' : 'bounded' });
    }
  }

  function coverageStatus() { return exhausted ? 'complete' : 'bounded'; }
  function representativePrograms() {
    const parents = new Map([[initialKey, { depth: 0, fingerprint: '', parentEdgeId: null, rootId: null }]]);
    const queue = [initialKey];
    for (let cursor = 0; cursor < queue.length; cursor += 1) {
      const sourceKey = queue[cursor];
      const source = parents.get(sourceKey);
      const outgoing = [...(edgesBySource.get(sourceKey) || [])].sort((left, right) => left.actionFingerprint.localeCompare(right.actionFingerprint));
      for (const edge of outgoing) {
        const candidate = {
          depth: source.depth + 1,
          fingerprint: source.fingerprint ? `${source.fingerprint}|${edge.actionFingerprint}` : edge.actionFingerprint,
          parentEdgeId: edge.id,
          rootId: source.rootId || edge.rootId,
        };
        const previous = parents.get(edge.to);
        if (previous && (previous.depth < candidate.depth || (previous.depth === candidate.depth && previous.fingerprint.localeCompare(candidate.fingerprint) <= 0))) continue;
        parents.set(edge.to, candidate);
        queue.push(edge.to);
      }
    }
    const records = [];
    for (const [terminalKey, node] of nodes) {
      if (node.classification.status !== 'boundary' || !parents.has(terminalKey)) continue;
      const chain = [];
      const stateKeys = [terminalKey];
      let cursor = terminalKey;
      while (cursor !== initialKey) {
        const parent = parents.get(cursor);
        const edge = edges[parent.parentEdgeId];
        chain.push(edge);
        cursor = edge.from;
        stateKeys.push(cursor);
      }
      chain.reverse();
      stateKeys.reverse();
      const parent = parents.get(terminalKey);
      records.push({
        rootId: parent.rootId, terminalKey, boundaryReason: node.classification.boundaryReason,
        outcome: hooks.projectAuthorityOutcome(seat, initialState, node.state),
        edges: chain, stateKeys, fingerprint: parent.fingerprint,
      });
    }
    return records.sort((left, right) => left.edges.length - right.edges.length || left.fingerprint.localeCompare(right.fingerprint));
  }
  function programWitness(
    stepsContain,
    rootIds,
    requiredEffects = [],
    deadline = Infinity,
  ) {
    if (!stepsContain?.length || !rootOrder.length) return null;
    const allowedRoots = rootIds ? new Set(rootIds) : null;
    const initialMatches = stepsContain.map(() => false);
    const initialEffectMatches = requiredEffects.map(() => false);
    const queue = [{ nodeKey: initialKey, rootId: null, edgeIds: [], stateKeys: [initialKey], matches: initialMatches, effectMatches: initialEffectMatches }];
    const visited = new Set([`${initialKey}|root|${initialMatches.map(Number).join('')}|${initialEffectMatches.map(Number).join('')}`]);
    const maxWitnessStates = Math.max(
      1000,
      Math.min(500_000, Number.isFinite(maxNodes) ? maxNodes * 16 : 500_000),
    );
    for (let cursor = 0; cursor < queue.length; cursor += 1) {
      if (cursor >= maxWitnessStates || now() >= deadline) return null;
      const current = queue[cursor];
      const outgoing = [...(edgesBySource.get(current.nodeKey) || [])]
        .sort((left, right) => left.actionFingerprint.localeCompare(right.actionFingerprint));
      for (const edge of outgoing) {
        if (current.stateKeys.includes(edge.to)) continue;
        const rootId = current.rootId || edge.rootId;
        if (allowedRoots && !allowedRoots.has(rootId)) continue;
        const edgeEffects = new Set(edge.causalEvents
          .map((event) => event.family || event.effect || event.type)
          .filter((effect) => typeof effect === 'string'));
        const effectMatches = current.effectMatches.map((matched, index) => (
          matched || edgeEffects.has(requiredEffects[index])
        ));
        const edgeIds = [...current.edgeIds, edge.id];
        const stateKeys = [...current.stateKeys, edge.to];
        const target = nodes.get(edge.to);
        for (const matches of advanceStepFactMatches(current.matches, edge.action, stepsContain)) {
          if (target?.classification.status === 'boundary'
            && matches.every(Boolean) && effectMatches.every(Boolean)) {
            const chain = edgeIds.map((edgeId) => edges[edgeId]);
            return {
              rootId,
              terminalKey: edge.to,
              boundaryReason: target.classification.boundaryReason,
              outcome: target.publicOutcome,
              edges: chain,
              stateKeys,
              fingerprint: chain.map((item) => item.actionFingerprint).join('|'),
              coverageWitness: true,
            };
          }
          const key = `${edge.to}|${rootId}|${matches.map(Number).join('')}|${effectMatches.map(Number).join('')}`;
          if (visited.has(key)) continue;
          visited.add(key);
          queue.push({ nodeKey: edge.to, rootId, edgeIds, stateKeys, matches, effectMatches });
        }
      }
    }
    return null;
  }
  function allAcyclicPrograms(maxPaths = 10000) {
    const programs = [];
    const seenPrograms = new Set();
    let truncated = false;
    let traversalBudgetExhausted = false;
    let pathVisits = 0;
    const started = now();
    const maxPathVisits = Math.max(
      1000,
      Math.min(2_000_000, maxPaths * 128),
    );
    function pathBudgetReached() {
      pathVisits += 1;
      if (pathVisits > maxPathVisits || now() - started >= maxTimeMs) {
        traversalBudgetExhausted = true;
        return true;
      }
      return false;
    }
    function visit(nodeKey, rootId, path, stateKeys) {
      if (pathBudgetReached()) return;
      if (programs.length >= maxPaths) {
        truncated = true;
        return;
      }
      const node = nodes.get(nodeKey);
      if (!node) return;
      if (node.classification.status === 'boundary') {
        if (!path.length) return;
        const fingerprint = path.map((edge) => edge.actionFingerprint).join('|');
        const key = `${nodeKey}|${fingerprint}`;
        if (seenPrograms.has(key)) return;
        seenPrograms.add(key);
        programs.push({
          rootId,
          terminalKey:nodeKey,
          boundaryReason:node.classification.boundaryReason,
          outcome:node.publicOutcome,
          edges:[...path],
          stateKeys:[...stateKeys],
          fingerprint,
        });
        return;
      }
      const outgoingByAction = new Map();
      for (const edge of edgesBySource.get(nodeKey) || []) {
        const key = `${edge.actionFingerprint}|${edge.to}`;
        if (!outgoingByAction.has(key)) outgoingByAction.set(key, edge);
      }
      const outgoing = [...outgoingByAction.values()]
        .sort((left, right) => left.actionFingerprint.localeCompare(right.actionFingerprint));
      for (const edge of outgoing) {
        if (traversalBudgetExhausted) return;
        if (stateKeys.includes(edge.to)) continue;
        visit(
          edge.to,
          rootId || edge.rootId,
          [...path, edge],
          [...stateKeys, edge.to],
        );
        if (truncated) return;
      }
    }
    visit(initialKey, null, [], [initialKey]);
    return {
      programs:programs.sort((left, right) => (
        left.edges.length - right.edges.length
        || left.fingerprint.localeCompare(right.fingerprint)
      )),
      complete:coverageStatus() === 'complete' && !truncated && !traversalBudgetExhausted,
      truncated,
      traversalBudgetExhausted,
      pathVisits,
    };
  }
  function cachedAllAcyclicPrograms(maxPaths = 10000) {
    if (allPathEnumeration
      && allPathEnumeration.graphVersion === graphVersion
      && allPathEnumeration.maxPaths === maxPaths) {
      return allPathEnumeration.value;
    }
    const value = allAcyclicPrograms(maxPaths);
    allPathEnumeration = { graphVersion, maxPaths, value };
    return value;
  }
  function authorityPrograms() {
    const byTerminal = new Map(representativePrograms()
      .map((program) => [program.terminalKey, program]));
    for (const witness of strategicWitnesses.values()) {
      const current = byTerminal.get(witness.terminalKey);
      if (!current?.coverageWitness) byTerminal.set(witness.terminalKey, witness);
    }
    const programs = [...byTerminal.values()];
    return programs.sort((left, right) => Number(Boolean(right.coverageWitness)) - Number(Boolean(left.coverageWitness))
      || left.edges.length - right.edges.length || left.fingerprint.localeCompare(right.fingerprint));
  }
  function summary() {
    const complete = coverageStatus() === 'complete';
    const representatives = representativePrograms();
    const programViews = representatives.map((program) => ({ outcome: program.outcome }));
    const optionalExitCount = representatives.filter((program) => program.edges.some((edge) => hooks.isDeclineAction(edge.action, nodes.get(edge.from)?.state))).length;
    const ranges = remainingResourceRanges(programViews);
    const outcomeRanges = numericRanges(programViews);
    const triggeredFamiliesObserved = [...new Set(edges.flatMap((edge) => edge.causalEvents.map((event) => event.family || event.effect || event.type).filter(Boolean)))].sort();
    const reachableActionFamilies = new Map();
    const strategicOpportunities = new Map();
    for (const [programIndex, program] of authorityPrograms().entries()) {
      for (const edge of program.edges) {
        const stepsContain = hooks.routeFilterForAction
          ? hooks.routeFilterForAction(edge.action)
          : [{ op: String(edge.action?.type || '') }];
        if (!Array.isArray(stepsContain) || !stepsContain.length || stepsContain.some((step) => !step || typeof step.op !== 'string')) continue;
        const family = stepsContain[0].op;
        const variants = [...new Set(stepsContain.map((step) => typeof step.mode === 'string' ? step.mode : null).filter(Boolean))].sort();
        const key = canonicalFingerprint({ family, variants, stepsContain });
        const current = reachableActionFamilies.get(key) || {
          family, variants, routeQuery: { stepsContain }, programIndexes: new Set(), observedEffects: new Set(),
        };
        current.programIndexes.add(programIndex);
        for (const event of edge.causalEvents) {
          const effect = event.family || event.effect || event.type;
          if (typeof effect === 'string') current.observedEffects.add(effect);
        }
        reachableActionFamilies.set(key, current);
        const strategic = hooks.strategicOpportunityForAction?.(edge.action);
        if (strategic && typeof strategic.family === 'string'
          && typeof strategic.variant === 'string'
          && strategic.routeQuery?.stepsContain?.length) {
          const strategicKey = canonicalFingerprint({
            family: strategic.family, variant: strategic.variant,
            routeQuery: strategic.routeQuery,
          });
          const opportunity = strategicOpportunities.get(strategicKey) || {
            family: strategic.family,
            variant: strategic.variant,
            routeQuery: strategic.routeQuery,
            factualCosts: strategic.factualCosts || {},
            factualImmediateEffects: strategic.factualImmediateEffects || [],
            scoringEngineChanges: strategic.scoringEngineChanges || {},
            observedEffects: new Set(),
            observedCosts: new Map(),
            outcomes: [],
          };
          opportunity.outcomes.push(program.outcome);
          const laterEvents = program.edges
            .slice(program.edges.indexOf(edge) + 1)
            .flatMap((laterEdge) => laterEdge.causalEvents);
          for (const event of laterEvents) {
            for (const [resource, delta] of Object.entries(event.delta || {})) {
              if (typeof delta !== 'number' || delta >= 0) continue;
              const values = opportunity.observedCosts.get(resource) || [];
              values.push(-delta);
              opportunity.observedCosts.set(resource, values);
            }
          }
          for (const event of edge.causalEvents) {
            const effect = event.family || event.effect || event.type;
            if (typeof effect === 'string') opportunity.observedEffects.add(effect);
          }
          strategicOpportunities.set(strategicKey, opportunity);
        }
      }
    }
    const reachableFamilies = [...reachableActionFamilies.values()]
      .map((entry) => ({
        family: entry.family,
        variants: entry.variants,
        reachability: 'observed',
        coverageStatus: coverageStatus(),
        observedProgramCount: entry.programIndexes.size,
        routeQuery: entry.routeQuery,
        // Action hooks need not invent a cost when the authoritative action
        // carries none. An empty list is factual, not an unavailable claim.
        factualCosts: [],
        factualImmediateEffects: [...entry.observedEffects].sort(),
        observedEffects: [...entry.observedEffects].sort(),
      }))
      .sort((left, right) => canonicalFingerprint(left.routeQuery).localeCompare(canonicalFingerprint(right.routeQuery)));
    const strategicFactsByKey = new Map([...strategicOpportunities.values()]
      .map((entry) => {
        const observedOutcomeRanges = (() => {
          const totals = entry.outcomes
            .map((outcome) => outcome?.scoreIfGameEnded?.after?.total)
            .filter((value) => typeof value === 'number');
          const beforeTotals = entry.outcomes
            .map((outcome) => outcome?.scoreIfGameEnded?.before?.total)
            .filter((value) => typeof value === 'number');
          const range = (values) => values.length ? { min: Math.min(...values), max: Math.max(...values) } : undefined;
          const after = range(totals);
          const delta = range(totals.map((value, index) => value - (beforeTotals[index] ?? value)));
          const immediate = range(entry.outcomes.map((outcome) => {
            const afterValue = outcome?.scoreIfGameEnded?.after?.duringGame;
            const beforeValue = outcome?.scoreIfGameEnded?.before?.duringGame;
            return typeof afterValue === 'number' && typeof beforeValue === 'number'
              ? afterValue - beforeValue : undefined;
          }).filter((value) => typeof value === 'number'));
          const resourceDeltas = Object.fromEntries(
            [...new Set(entry.outcomes.flatMap((outcome) => Object.keys(outcome || {})
              .filter((key) => key.endsWith('Delta') && key !== 'scoreDelta')))]
              .sort()
              .flatMap((key) => {
                const observed = range(entry.outcomes
                  .map((outcome) => outcome?.[key])
                  .filter((value) => typeof value === 'number'));
                return observed ? [[key.slice(0, -'Delta'.length), observed]] : [];
              })
          );
          return {
            ...(after ? { scoreIfGameEndedAfterTotal: after } : {}),
            ...(delta ? { scoreIfGameEndedDelta: delta } : {}),
            ...(immediate ? { immediateScoreDelta: immediate } : {}),
            ...(Object.keys(resourceDeltas).length ? { resourceDeltas } : {}),
          };
        })();
        const factualImmediateEffects = [...new Set([
          ...entry.factualImmediateEffects,
          ...entry.observedEffects,
        ])].sort();
        const fact = {
        family: entry.family,
        variant: entry.variant,
        reachability: 'observed',
        routeQuery: entry.routeQuery,
        factualCosts: {
          ...entry.factualCosts,
          ...Object.fromEntries([...entry.observedCosts.entries()]
            .map(([resource, values]) => [resource, {
              min: Math.min(...values), max: Math.max(...values),
            }])),
        },
        factualImmediateEffects,
        immediateEffects: factualImmediateEffects,
        immediateScoreDelta: observedOutcomeRanges.immediateScoreDelta,
        endNowScoreDelta: observedOutcomeRanges.scoreIfGameEndedDelta,
        scoringEngineChanges: entry.scoringEngineChanges,
        observedOutcomeRanges,
        resourceInfluenceScoreRanges: observedOutcomeRanges.resourceDeltas || {},
        };
        return [canonicalFingerprint({ family: fact.family, variant: fact.variant, routeQuery: fact.routeQuery }), fact];
      }));
    for (const declared of hooks.strategicOpportunityCatalog?.(initialState, seat) || []) {
      if (!declared || typeof declared.family !== 'string' || typeof declared.variant !== 'string'
        || !declared.routeQuery?.stepsContain?.length) continue;
      const key = canonicalFingerprint({ family: declared.family, variant: declared.variant, routeQuery: declared.routeQuery });
      if (!strategicFactsByKey.has(key)) {
        strategicFactsByKey.set(key, {
          family: declared.family,
          variant: declared.variant,
          reachability: 'unknown',
          routeQuery: declared.routeQuery,
          factualCosts: declared.factualCosts || {},
          factualImmediateEffects: declared.factualImmediateEffects || [],
          immediateEffects: declared.factualImmediateEffects || [],
          scoringEngineChanges: declared.scoringEngineChanges || {},
        });
      }
    }
    const strategicFacts = [...strategicFactsByKey.values()]
      .sort((left, right) => canonicalFingerprint(left.routeQuery).localeCompare(canonicalFingerprint(right.routeQuery)));
    const base = {
      decisionId, rootIds: [...rootOrder], coverageStatus: coverageStatus(), enumerationComplete: complete, maxNodes, maxTimeMs,
      nodesExplored, transitionsApplied, edges: edges.map(({ action, causalEvents, ...edge }) => edge),
      reachableActionFamilies: reachableFamilies,
      strategicOpportunities: strategicFacts,
      rootCoverage: rootOrder.map((rootId) => ({ rootId, ...rootCoverage.get(rootId) })),
      roots: rootOrder.map((rootId) => {
        const root = roots.get(rootId);
        const rootPrograms = programViews.filter((_program, index) => representatives[index].rootId === rootId);
        return { rootId, initialAction: root.action, label: root.actionFingerprint, distinctTerminalStateCount: rootPrograms.length, reachable: numericRanges(rootPrograms), triggeredFamilies: triggeredFamiliesObserved };
      }),
    };
    if (complete) return { ...base, distinctBoundaryStateCount: representatives.length, optionalExitCount, remainingResourceRanges: ranges, outcomeRanges, triggeredFamilies: triggeredFamiliesObserved };
    return { ...base, code: 'INCOMPLETE_OUTCOME_INDEX', observedTerminalCount: representatives.length, observedOptionalExitCount: optionalExitCount, observedRemainingResourceRanges: ranges, observedOutcomeRanges: outcomeRanges, triggeredFamiliesObserved };
  }
  function freeze(rootIds, requirements, programId, stepsContain, allPaths = false, maxPaths = 10000) {
    const requirementFingerprint = canonicalFingerprint({ rootIds: rootIds || null, requirements: requirements || [], programId: programId || null, stepsContain: stepsContain || [], allPaths:Boolean(allPaths), maxPaths });
    if (activeEnumeration && activeEnumeration.graphVersion === graphVersion && activeEnumeration.requirementFingerprint === requirementFingerprint) return activeEnumeration;
    const pathSet = allPaths ? cachedAllAcyclicPrograms(maxPaths) : null;
    const programs = (pathSet ? pathSet.programs : authorityPrograms())
      .filter((program) => !rootIds || rootIds.includes(program.rootId))
      .filter((program) => (requirements || []).every((requirement) => matchesRequirement(
        program.outcome, program.edges.map((edge) => edge.action), requirement,
      )))
      .filter((program) => containsStepFacts(program.edges.map((edge) => edge.action), stepsContain))
      .filter((program) => !programId || makeProgramId(
        decisionId, initialSnapshotFingerprint, program.fingerprint, hash,
      ) === programId)
      .map((program) => {
        const actions = program.edges.map((edge) => edge.action);
        const facts = hooks.projectProgramFacts?.(
          seat, initialState, nodes.get(program.terminalKey)?.state, actions,
          program.edges.flatMap((edge) => edge.causalEvents),
        ) || {};
        return {
          programId: makeProgramId(decisionId, initialSnapshotFingerprint, program.fingerprint, hash), rootId: program.rootId,
          boundaryReason: program.boundaryReason,
          termination: program.edges.some((edge) => hooks.isDeclineAction(edge.action, nodes.get(edge.from)?.state)) ? 'declined_optional' : 'automatic',
          outcome: program.outcome,
          steps: program.edges.map((edge) => ({ action: edge.action, causalEvents: edge.causalEvents })),
          stateKeys: program.stateKeys,
          factualCosts: facts.factualCosts || {},
          factualGains: facts.factualGains || {},
          immediateEffects: facts.immediateEffects || [],
          immediateScoreDelta: facts.immediateScoreDelta ?? null,
          endNowScoreDelta: facts.endNowScoreDelta ?? null,
          scoringEngineChanges: facts.scoringEngineChanges || [],
        };
      });
    activeEnumeration = { enumerationId: hash(`routes:${decisionId}:${graphVersion}:${requirementFingerprint}`), graphVersion, requirementFingerprint, programs, pathSetComplete:pathSet ? pathSet.complete : coverageStatus() === 'complete' };
    return activeEnumeration;
  }
  function decodeCursor(cursor) { try { return JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')); } catch { throw new Error('INVALID_ROUTE_CURSOR'); } }
  function coverageDiverse(programs, request) {
    if (request.requirements || request.programId || request.stepsContain || programs.length < 2) return programs;
    const picked = []; const seen = new Set();
    const add = (program) => { if (program && !seen.has(program.programId)) { seen.add(program.programId); picked.push(program); } };
    add(programs.find((program) => !program.steps.some((step) => hooks.strategicOpportunityForAction?.(step.action))));
    for (const program of programs) {
      for (const step of program.steps) {
        const strategic = hooks.strategicOpportunityForAction?.(step.action);
        if (strategic) add(programs.find((candidate) => candidate.steps.some((item) => canonicalFingerprint(item.action) === canonicalFingerprint(step.action))));
      }
    }
    return [...picked, ...programs.filter((program) => !seen.has(program.programId))];
  }
  function exploreStrategicCoverage(request = {}) {
    const witnessDeadline = now() + requestedBudget(request).maxTimeMs;
    const catalog = [...(hooks.strategicOpportunityCatalog?.(initialState, seat) || [])]
      .filter((item) => item?.routeQuery?.stepsContain?.length)
      .sort((left, right) => canonicalFingerprint(left.routeQuery)
        .localeCompare(canonicalFingerprint(right.routeQuery)));
    for (const item of catalog) {
      const stepsContain = item.routeQuery.stepsContain;
      const requiredEffects = item.factualImmediateEffects || [];
      const witnessKey = canonicalFingerprint({ rootIds: request.rootIds || null, stepsContain, requiredEffects });
      let witness = strategicWitnesses.get(witnessKey)
        || programWitness(stepsContain, request.rootIds, requiredEffects, witnessDeadline);
      if (!witness && !exhausted) {
        explore({ ...request, coverageOnly: undefined, coverageTarget: true, stepsContain, coverageEffects: requiredEffects });
        witness = programWitness(
          stepsContain,
          request.rootIds,
          requiredEffects,
          witnessDeadline,
        );
      }
      if (witness) strategicWitnesses.set(witnessKey, witness);
    }
  }
  function cursorFilter(cursor, request) {
    try {
      const filter = JSON.parse(cursor.requirementFingerprint);
      if (cursor.requirementFingerprint !== canonicalFingerprint({ rootIds: filter.rootIds || null, requirements: filter.requirements || [], programId: filter.programId || null, stepsContain: filter.stepsContain || [], allPaths:Boolean(filter.allPaths), maxPaths:filter.maxPaths || 10000 })) throw new Error('invalid');
      if (request.rootIds !== undefined && canonicalFingerprint(request.rootIds) !== canonicalFingerprint(filter.rootIds)) throw new Error('invalid');
      if (request.requirements !== undefined && canonicalFingerprint(request.requirements) !== canonicalFingerprint(filter.requirements)) throw new Error('invalid');
      if (request.programId !== undefined && request.programId !== filter.programId) throw new Error('invalid');
      if (request.stepsContain !== undefined && canonicalFingerprint(request.stepsContain) !== canonicalFingerprint(filter.stepsContain)) throw new Error('invalid');
      if (request.allPaths !== undefined && Boolean(request.allPaths) !== Boolean(filter.allPaths)) throw new Error('invalid');
      if (request.maxPaths !== undefined && request.maxPaths !== filter.maxPaths) throw new Error('invalid');
      return filter;
    } catch {
      throw new Error('STALE_ROUTE_CURSOR');
    }
  }
  return {
    outcomeIndex(request = {}) {
      // One public request owns one wall-clock slice. The graph and node cap
      // remain cumulative, but a completed summary call must not consume the
      // time allowance of a later, more specific factual route query.
      decisionStartedAt = now();
      // The index advertises strategic opportunities, so declared witnesses
      // must receive the bounded budget before broad graph coverage. Otherwise
      // wall-clock variance can omit a legal family even though the same
      // isolated query finds it. General exploration uses only the remainder.
      exploreStrategicCoverage(request);
      if (!request.coverageOnly) explore(request);
      return summary();
    },
    enumerateRoutes(request = {}) {
      decisionStartedAt = now();
      if (request.cursor) {
        const cursor = decodeCursor(request.cursor);
        const filter = cursorFilter(cursor, request);
        const frozen = freeze(filter.rootIds, filter.requirements, filter.programId, filter.stepsContain, filter.allPaths, filter.maxPaths);
        if (cursor.enumerationId !== frozen.enumerationId) throw new Error('STALE_ROUTE_CURSOR');
        const limit = Math.min(request.limit || pageSize, pageSize);
        const ordered = cursor.coverageDiverse ? coverageDiverse(frozen.programs, {}) : frozen.programs;
        const programs = ordered.slice(cursor.position, cursor.position + limit);
        const position = cursor.position + programs.length;
        const coverage = frozen.pathSetComplete ? 'complete' : 'bounded';
        return { status: 'routes', decisionId, enumerationId: frozen.enumerationId, ...(cursor.coverageDiverse ? { ordering: 'coverage-diverse-not-ranked' } : {}), coverageStatus: coverage, enumerationComplete: coverage === 'complete', ...(coverage === 'bounded' ? { code: 'INCOMPLETE_OUTCOME_INDEX' } : {}), totalMatches: frozen.programs.length, programs, nextCursor: position < ordered.length ? Buffer.from(JSON.stringify({ enumerationId: frozen.enumerationId, position, requirementFingerprint: frozen.requirementFingerprint, ...(cursor.coverageDiverse ? { coverageDiverse: true } : {}) })).toString('base64url') : null };
      }
      if (request.stepsContain?.length) {
        explore({ ...request, coverageTarget: true });
      } else if (!request.requirements && !request.programId) {
        exploreStrategicCoverage(request);
        explore(request);
      } else {
        explore(request);
      }
      const frozen = freeze(request.rootIds, request.requirements, request.programId, request.stepsContain, request.allPaths, request.maxPaths || 10000);
      const limit = Math.min(request.limit || pageSize, pageSize);
      const ordered = coverageDiverse(frozen.programs, request);
      // A stateless worker bridge cannot retain an opaque cursor's page
      // store between calls.  The same factual filter plus this deterministic
      // offset reconstructs exactly the corresponding page.
      const offset = Math.max(0, Number.isInteger(request.offset) ? request.offset : 0);
      const programs = ordered.slice(offset, offset + limit);
      const coverage = frozen.pathSetComplete ? 'complete' : 'bounded';
      const position = offset + programs.length;
      return { status: 'routes', decisionId, enumerationId: frozen.enumerationId, ordering: 'coverage-diverse-not-ranked', coverageStatus: coverage, enumerationComplete: coverage === 'complete', ...(coverage === 'bounded' ? { code: 'INCOMPLETE_OUTCOME_INDEX' } : {}), totalMatches: frozen.programs.length, programs, nextCursor: position < ordered.length ? Buffer.from(JSON.stringify({ enumerationId: frozen.enumerationId, position, requirementFingerprint: frozen.requirementFingerprint, coverageDiverse: true })).toString('base64url') : null };
    },
    diagnostics() {
      return {
        canonicalNodeCount: nodes.size, storedAuthoritativeStateCount: nodes.size,
        canonicalEdgeCount: edges.length,
        boundaryNodeCount:[...nodes.values()].filter(
          (node) => node.classification.status === 'boundary',
        ).length,
        rootCount:roots.size,
        maxOutDegree:Math.max(0, ...[...edgesBySource.values()].map((items) => items.length)),
        nodesExplored,
        transitionsApplied,
        graphCoverageStatus: coverageStatus(),
        graphExhausted: exhausted,
        frontierEntryCount: [...frontiers.values()].reduce((count, frontier) => count + frontier.length, 0),
        frontierPathPayloadCount: 0, decisionStoreCount: 1, equivalentDecisionCacheCount: 0,
        immutableProgramRecordCount: activeEnumeration ? activeEnumeration.programs.length : 0,
        ...(allPathEnumeration ? {
          allPathMaxPaths:allPathEnumeration.maxPaths,
          allPathCount:allPathEnumeration.value.programs.length,
          allPathComplete:allPathEnumeration.value.complete,
          allPathTruncated:allPathEnumeration.value.truncated,
          allPathTraversalBudgetExhausted:allPathEnumeration.value.traversalBudgetExhausted,
          allPathVisits:allPathEnumeration.value.pathVisits,
        } : {}),
      };
    },
  };
}

module.exports = {
  createDecisionExplorer,
  createFiniteProgramExplorer,
  createDecisionMap,
  createAuthorityGateway,
  canonicalFingerprint,
  makeRootId,
  makeProgramId,
};
