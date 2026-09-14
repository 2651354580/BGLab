'use strict';

const readline = require('node:readline');
const path = require('node:path');

const adapterModule = require(path.resolve(process.argv[2]));
const Adapter = Object.values(adapterModule).find(
  value => typeof value === 'function' && /BGLabAdapter$/.test(value.name),
);
if (!Adapter) throw new Error(`No exported *BGLabAdapter class in ${process.argv[2]}`);

function scenarioFixtureBuilder() {
  const adapterDirectory = path.dirname(path.resolve(process.argv[2]));
  const candidates = ['scenario-fixtures.cjs', 'scenario-fixtures.js', 'scenario-fixtures.ts']
    .map(name => path.resolve(adapterDirectory, '../../tests', name));
  for (const candidate of candidates) {
    try {
      const fixtureModule = require(candidate);
      if (typeof fixtureModule.buildScenarioFixture === 'function') {
        return fixtureModule.buildScenarioFixture;
      }
    } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  throw new Error('Package does not provide a scenario fixture builder');
}

let adapter = new Adapter();

function presentOutcome(result) {
  if (!result || typeof result !== 'object' || typeof adapter.renderPublicOutcome !== 'function') return result;
  const presented = {...result};
  if (result.outcome && Object.keys(result.outcome).length) {
    const summary = adapter.renderPublicOutcome(result.outcome);
    if (typeof summary !== 'string' || !summary.trim() || [...summary].length > 600) {
      throw new Error('publicSummary must be non-empty text of at most 600 characters');
    }
    presented.publicSummary = summary;
  }
  if (Array.isArray(result.programs)) presented.programs = result.programs.map(presentOutcome);
  return presented;
}

function handle(message) {
  switch (message.command) {
    case 'start':
      adapter.start(message.config || {});
      return adapter.snapshot();
    case 'snapshot':
      return adapter.snapshot();
    case 'finalResult':
      return adapter.finalResult();
    case 'view':
      return adapter.view(Number(message.seat || 0));
    case 'coverageCatalog':
      return adapter.coverageCatalog();
    case 'strategicOpportunityCatalog':
      return adapter.strategicOpportunityCatalog();
    case 'outcomeIndex':
      return adapter.outcomeIndex(Number(message.seat || 0), message.request || {});
    case 'enumerateRoutes':
      return presentOutcome(adapter.enumerateRoutes(message.request || {}));
    case 'validateTransaction':
      return presentOutcome(adapter.validateTransaction(message.decisionId, message.transaction));
    case 'scenarioFixture': {
      const build = scenarioFixtureBuilder();
      const snapshot = build(adapter.snapshot(), message.spec || {});
      adapter.restore(snapshot);
      return adapter.snapshot();
    }
    case 'dispatch':
      return adapter.dispatch(message.decisionId, message.transaction);
    case 'restore':
      adapter.restore(message.snapshot, {returnView:false});
      return typeof adapter.authoritySnapshot === 'function'
        ? adapter.authoritySnapshot()
        : adapter.snapshot();
    case 'authoritySnapshot':
      return typeof adapter.authoritySnapshot === 'function'
        ? adapter.authoritySnapshot()
        : adapter.snapshot();
    default:
      throw new Error(`Unknown command: ${message.command}`);
  }
}

const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
input.on('line', line => {
  let id = null;
  try {
    const message = JSON.parse(line);
    id = message.id;
    process.stdout.write(JSON.stringify({id, ok: true, result: handle(message)}) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({id, ok: false, error: error.stack || String(error)}) + '\n');
  }
});
