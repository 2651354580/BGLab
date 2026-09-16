(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BGLabGameShell = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const INTERACTION_STATES = new Set([
    'idle',
    'source_selectable',
    'source_selected',
    'target_selectable',
    'effect_pending',
    'confirmation_pending',
    'resolving',
    'waiting_next_decision',
    'finished',
    'error',
  ]);
  const PLAYER_KINDS = new Set(['human', 'ai']);
  const FIELD_TONES = new Set(['default', 'positive', 'warning', 'danger']);
  const TIMELINE_KINDS = new Set([
    'choice', 'payment', 'gain', 'movement', 'score', 'phase', 'error',
  ]);

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function typeError(path, message) {
    throw new TypeError(`${path} ${message}`);
  }

  function record(value, path) {
    if (!isRecord(value)) typeError(path, 'must be an object');
    return value;
  }

  function nonEmptyString(value, path) {
    if (typeof value !== 'string' || value.length === 0) {
      typeError(path, 'must be a non-empty string');
    }
    return value;
  }

  function optionalString(value, path) {
    if (value === undefined || value === null) return undefined;
    return nonEmptyString(value, path);
  }

  function finiteNumber(value, path) {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      typeError(path, 'must be a finite number');
    }
    return value;
  }

  function integer(value, path) {
    if (!Number.isInteger(value)) typeError(path, 'must be an integer');
    return value;
  }

  function scalar(value, path) {
    if (typeof value !== 'string' && typeof value !== 'number') {
      typeError(path, 'must be a string or number');
    }
    if (typeof value === 'number' && !Number.isFinite(value)) {
      typeError(path, 'must be a finite number');
    }
    return value;
  }

  function boolean(value, path, fallback) {
    if (value === undefined && fallback !== undefined) return fallback;
    if (typeof value !== 'boolean') typeError(path, 'must be a boolean');
    return value;
  }

  function array(value, path) {
    if (!Array.isArray(value)) typeError(path, 'must be an array');
    return value;
  }

  function optionalNumber(value, path) {
    if (value === undefined || value === null) return undefined;
    return finiteNumber(value, path);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[character]));
  }

  function attr(value) {
    return escapeHtml(value);
  }

  function normalizeGame(value) {
    const input = record(value, 'view.game');
    const progressPercent = input.progressPercent == null
      ? undefined
      : integer(input.progressPercent, 'view.game.progressPercent');
    if (progressPercent !== undefined && (progressPercent < 0 || progressPercent > 100)) {
      typeError('view.game.progressPercent', 'must be between 0 and 100');
    }
    const progressItemsInput = input.progressItems === undefined || input.progressItems === null
      ? undefined
      : array(input.progressItems, 'view.game.progressItems');
    const progressIds = new Set();
    const progressItems = progressItemsInput?.map((item, index) => {
      const path = `view.game.progressItems[${index}]`;
      const progress = record(item, path);
      const id = nonEmptyString(progress.id, `${path}.id`);
      if (progressIds.has(id)) typeError(path, `contains duplicate progress item id ${id}`);
      progressIds.add(id);
      const tone = progress.tone === undefined || progress.tone === null ? 'default' : progress.tone;
      if (!FIELD_TONES.has(tone)) typeError(`${path}.tone`, 'is not a known progress tone');
      return {
        id,
        label: nonEmptyString(progress.label, `${path}.label`),
        value: scalar(progress.value, `${path}.value`),
        tone,
      };
    });
    return {
      id: nonEmptyString(input.id, 'view.game.id'),
      title: nonEmptyString(input.title, 'view.game.title'),
      phaseLabel: nonEmptyString(input.phaseLabel, 'view.game.phaseLabel'),
      progressLabel: nonEmptyString(input.progressLabel, 'view.game.progressLabel'),
      progressPercent,
      progressItems,
      primaryInstruction: nonEmptyString(
        input.primaryInstruction,
        'view.game.primaryInstruction',
      ),
    };
  }

  function normalizeField(value, path) {
    const input = record(value, path);
    const fieldValue = scalar(input.value, `${path}.value`);
    const secondaryValue = input.secondaryValue === undefined || input.secondaryValue === null
      ? undefined
      : scalar(input.secondaryValue, `${path}.secondaryValue`);
    const tone = input.tone === undefined || input.tone === null ? 'default' : input.tone;
    if (!FIELD_TONES.has(tone)) typeError(`${path}.tone`, 'is not a known field tone');
    return {
      id: nonEmptyString(input.id, `${path}.id`),
      label: nonEmptyString(input.label, `${path}.label`),
      value: fieldValue,
      secondaryValue,
      icon: optionalString(input.icon, `${path}.icon`),
      tone,
    };
  }

  function normalizeFieldGroup(value, path) {
    const input = record(value, path);
    const fields = array(input.fields, `${path}.fields`);
    const fieldIds = new Set();
    const normalizedFields = fields.map((field, index) => {
      const normalized = normalizeField(field, `${path}.fields[${index}]`);
      if (fieldIds.has(normalized.id)) typeError(path, `contains duplicate field id ${normalized.id}`);
      fieldIds.add(normalized.id);
      return normalized;
    });
    return {
      id: nonEmptyString(input.id, `${path}.id`),
      label: nonEmptyString(input.label, `${path}.label`),
      fields: normalizedFields,
      collapsible: boolean(input.collapsible, `${path}.collapsible`, false),
    };
  }

  function normalizePlayer(value, path) {
    const input = record(value, path);
    const kind = input.kind;
    if (!PLAYER_KINDS.has(kind)) typeError(`${path}.kind`, 'must be human or ai');
    const score = input.score === null ? null : finiteNumber(input.score, `${path}.score`);
    const gameFields = array(input.gameFields, `${path}.gameFields`);
    const groupIds = new Set();
    const normalizedGroups = gameFields.map((group, index) => {
      const normalized = normalizeFieldGroup(group, `${path}.gameFields[${index}]`);
      if (groupIds.has(normalized.id)) typeError(path, `contains duplicate field group id ${normalized.id}`);
      groupIds.add(normalized.id);
      return normalized;
    });
    return {
      seat: integer(input.seat, `${path}.seat`),
      name: nonEmptyString(input.name, `${path}.name`),
      kind,
      isViewer: boolean(input.isViewer, `${path}.isViewer`),
      isActive: boolean(input.isActive, `${path}.isActive`),
      score,
      statusLabel: optionalString(input.statusLabel, `${path}.statusLabel`),
      gameFields: normalizedGroups,
    };
  }

  function normalizeActor(value, path) {
    if (value === undefined || value === null) return null;
    const input = record(value, path);
    return {
      seat: integer(input.seat, `${path}.seat`),
      name: nonEmptyString(input.name, `${path}.name`),
    };
  }

  function normalizeValueDelta(value, path) {
    if (value === undefined || value === null) return undefined;
    const input = record(value, path);
    return {
      before: optionalNumber(input.before, `${path}.before`),
      after: optionalNumber(input.after, `${path}.after`),
      delta: optionalNumber(input.delta, `${path}.delta`),
    };
  }

  function normalizeTimelineEntries(value, path = 'view.timeline', ids = new Set()) {
    const entries = array(value, path);
    return entries.map((entry, index) => {
      const entryPath = `${path}[${index}]`;
      const input = record(entry, entryPath);
      const id = nonEmptyString(input.id, `${entryPath}.id`);
      if (ids.has(id)) typeError(entryPath, `contains duplicate timeline entry id ${id}`);
      ids.add(id);
      const kind = input.kind;
      if (!TIMELINE_KINDS.has(kind)) typeError(`${entryPath}.kind`, 'is not a known timeline kind');
      const children = input.children === undefined || input.children === null
        ? []
        : normalizeTimelineEntries(input.children, `${entryPath}.children`, ids);
      return {
        id,
        turnId: optionalString(input.turnId, `${entryPath}.turnId`),
        actor: normalizeActor(input.actor, `${entryPath}.actor`),
        kind,
        summary: nonEmptyString(input.summary, `${entryPath}.summary`),
        icon: optionalString(input.icon, `${entryPath}.icon`),
        valueDelta: normalizeValueDelta(input.valueDelta, `${entryPath}.valueDelta`),
        children,
      };
    });
  }

  function normalizeInteraction(value, path = 'view.interaction') {
    const input = record(value, path);
    const state = input.state;
    if (!INTERACTION_STATES.has(state)) typeError(`${path}.state`, 'is not a known interaction state');
    const validatedSteps = input.validatedSteps === undefined || input.validatedSteps === null
      ? []
      : array(input.validatedSteps, `${path}.validatedSteps`).map((step, index) => (
        nonEmptyString(step, `${path}.validatedSteps[${index}]`)
      ));
    let error;
    if (input.error !== undefined && input.error !== null) {
      const errorInput = record(input.error, `${path}.error`);
      error = {
        code: nonEmptyString(errorInput.code, `${path}.error.code`),
        message: nonEmptyString(errorInput.message, `${path}.error.message`),
      };
    }
    return {
      state,
      instruction: nonEmptyString(input.instruction, `${path}.instruction`),
      sourceLabel: optionalString(input.sourceLabel, `${path}.sourceLabel`),
      targetLabel: optionalString(input.targetLabel, `${path}.targetLabel`),
      validatedSteps,
      canCancel: boolean(input.canCancel, `${path}.canCancel`, false),
      canSkip: boolean(input.canSkip, `${path}.canSkip`, false),
      canConfirm: boolean(input.canConfirm, `${path}.canConfirm`, false),
      cancelLabel: optionalString(input.cancelLabel, `${path}.cancelLabel`) || '取消',
      skipLabel: optionalString(input.skipLabel, `${path}.skipLabel`) || '跳过',
      confirmLabel: optionalString(input.confirmLabel, `${path}.confirmLabel`) || '确认',
      error,
    };
  }

  function normalizeFinalResult(value) {
    if (value === undefined || value === null) return undefined;
    const input = record(value, 'view.finalResult');
    const rows = array(input.rows, 'view.finalResult.rows').map((row, index) => {
      const path = `view.finalResult.rows[${index}]`;
      const rowInput = record(row, path);
      return {
        id: nonEmptyString(rowInput.id, `${path}.id`),
        label: nonEmptyString(rowInput.label, `${path}.label`),
        values: array(rowInput.values, `${path}.values`).map((entry, valueIndex) => (
          finiteNumber(entry, `${path}.values[${valueIndex}]`)
        )),
      };
    });
    return {
      ...(input.columns === undefined ? {} : {columns: array(input.columns, 'view.finalResult.columns').map((label, index) => nonEmptyString(label, `view.finalResult.columns[${index}]`))}),
      title: nonEmptyString(input.title, 'view.finalResult.title'),
      winnerSeats: array(input.winnerSeats, 'view.finalResult.winnerSeats').map((seat, index) => (
        integer(seat, `view.finalResult.winnerSeats[${index}]`)
      )),
      summary: nonEmptyString(input.summary, 'view.finalResult.summary'),
      rows,
    };
  }

  function normalizeView(value) {
    const input = record(value, 'view');
    const players = array(input.players, 'view.players').map((player, index) => (
      normalizePlayer(player, `view.players[${index}]`)
    ));
    const seats = new Set();
    for (const player of players) {
      if (seats.has(player.seat)) typeError('view.players', `contains duplicate seat ${player.seat}`);
      seats.add(player.seat);
    }
    const activeSeat = input.activeSeat === null ? null : integer(input.activeSeat, 'view.activeSeat');
    return {
      game: normalizeGame(input.game),
      activeSeat,
      players,
      interaction: normalizeInteraction(input.interaction),
      timeline: normalizeTimelineEntries(input.timeline),
      finalResult: normalizeFinalResult(input.finalResult),
    };
  }

  function activePlayerLabel(view) {
    const active = view.players.find(player => player.seat === view.activeSeat);
    return active ? active.name : view.activeSeat === null ? '暂无' : `座位 ${view.activeSeat + 1}`;
  }

  function turnStatusLabel(view) {
    if (view.activeSeat === null || view.interaction.state === 'finished') return '游戏结束';
    const active = view.players.find(player => player.seat === view.activeSeat);
    if (!active) return `轮到座位 ${view.activeSeat + 1}`;
    if (view.interaction.state === 'resolving' || view.interaction.state === 'waiting_next_decision') {
      return `${active.name}正在行动`;
    }
    return active.isViewer ? '轮到你了！' : `轮到${active.name}`;
  }

  function renderTopBarNormalized(view) {
    const active = view.players.find(player => player.seat === view.activeSeat);
    return [
      `<header class="bglab-game-topbar" data-game-id="${attr(view.game.id)}" data-viewer-turn="${attr(Boolean(active?.isViewer))}" aria-label="${attr(`${view.game.title} · ${view.game.phaseLabel}`)}">`,
      `  <strong class="bglab-turn-status" role="status" data-active-seat="${attr(view.activeSeat === null ? '' : view.activeSeat)}">${escapeHtml(turnStatusLabel(view))}</strong>`,
      '</header>',
    ].join('');
  }

  function renderProgress(view) {
    const percent = view.game.progressPercent;
    const progressItems = view.game.progressItems?.length
      ? [
        `<div class="bglab-progress-items" data-progress-count="${attr(view.game.progressItems.length)}">`,
        view.game.progressItems.map(item => [
          `<div class="bglab-progress-item" data-progress-id="${attr(item.id)}" data-tone="${attr(item.tone)}">`,
          `  <span class="bglab-progress-item-label">${escapeHtml(item.label)}</span>`,
          `  <strong class="bglab-progress-item-value">${escapeHtml(item.value)}</strong>`,
          '</div>',
        ].join('')).join(''),
        '</div>',
      ].join('')
      : `<span class="bglab-progress-label">${escapeHtml(view.game.progressLabel)}</span>`;
    const progressBar = percent === undefined ? '' : [
      `<div class="bglab-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${attr(percent)}">`,
      `  <span class="bglab-progress-value" style="width:${attr(percent)}%"></span>`,
      '</div>',
    ].join('');
    return [
      `<section class="bglab-game-progress" aria-label="${attr(view.game.progressLabel)}">`,
      '  <div class="bglab-section-heading">',
      '    <h2>游戏进度</h2>',
      view.game.progressItems?.length ? '' : `    ${progressItems}`,
      '  </div>',
      view.game.progressItems?.length ? progressItems : '',
      `  <p class="bglab-progress-phase">${escapeHtml(view.game.phaseLabel)}</p>`,
      progressBar,
      '</section>',
    ].join('');
  }

  function renderFieldGroup(group) {
    const fields = group.fields.map(field => [
      `<div class="bglab-game-field" data-field-id="${attr(field.id)}" data-tone="${attr(field.tone)}"${field.icon ? ` data-icon="${attr(field.icon)}"` : ''}>`,
      field.icon ? `  <span class="bglab-game-field-icon" data-field-icon="${attr(field.icon)}" aria-hidden="true"></span>` : '',
      `  <span class="bglab-game-field-label">${escapeHtml(field.label)}</span>`,
      `  <strong class="bglab-game-field-value">${escapeHtml(field.value)}</strong>`,
      field.secondaryValue !== undefined
        ? `  <span class="bglab-game-field-secondary">${escapeHtml(field.secondaryValue)}</span>`
        : '',
      '</div>',
    ].join('')).join('');
    const heading = `<h3 class="bglab-field-group-label">${escapeHtml(group.label)}</h3>`;
    return [
      `<section class="bglab-game-field-group" data-field-group-id="${attr(group.id)}" data-collapsible="${attr(group.collapsible)}">`,
      heading,
      `<div class="bglab-game-field-list">${fields}</div>`,
      '</section>',
    ].join('');
  }

  function renderPlayer(player) {
    const fields = player.gameFields.map(renderFieldGroup).join('');
    const score = player.score === null ? '—' : `${escapeHtml(player.score)} 分`;
    return [
      `<article class="bglab-player-card" data-seat="${attr(player.seat)}" data-player-kind="${attr(player.kind)}" data-player-active="${attr(player.isActive)}" data-player-viewer="${attr(player.isViewer)}">`,
      '  <div class="bglab-player-card-header">',
      `    <span class="bglab-player-kind" aria-label="${player.kind === 'ai' ? 'AI' : '人类'}">${player.kind === 'ai' ? 'AI' : '人'}</span>`,
      `    <strong class="bglab-player-name">${escapeHtml(player.name)}</strong>`,
      player.isActive ? '    <span class="bglab-player-active">行动中</span>' : '',
      '  </div>',
      '  <div class="bglab-player-score">',
      '    <span>分数</span>',
      `    <strong>${score}</strong>`,
      '  </div>',
      player.statusLabel ? `  <p class="bglab-player-status">${escapeHtml(player.statusLabel)}</p>` : '',
      `  <div class="bglab-player-fields">${fields}</div>`,
      '</article>',
    ].join('');
  }

  function renderDecisionPanel(interaction) {
    const steps = interaction.validatedSteps.length === 0 ? '' : [
      '  <ol class="bglab-decision-steps">',
      interaction.validatedSteps.map(step => `    <li>${escapeHtml(step)}</li>`).join(''),
      '  </ol>',
    ].join('');
    const error = interaction.error ? [
      `  <p class="bglab-shell-error" data-error-code="${attr(interaction.error.code)}" role="alert">`,
      `    ${escapeHtml(interaction.error.message)}`,
      '  </p>',
    ].join('') : '';
    return [
      `<section class="bglab-decision-panel" data-interaction-state="${attr(interaction.state)}">`,
      '  <div class="bglab-section-heading"><h2>当前决策</h2></div>',
      `  <p class="bglab-decision-instruction">${escapeHtml(interaction.instruction)}</p>`,
      interaction.sourceLabel ? `  <p class="bglab-decision-source"><span>来源</span>${escapeHtml(interaction.sourceLabel)}</p>` : '',
      interaction.targetLabel ? `  <p class="bglab-decision-target"><span>目标</span>${escapeHtml(interaction.targetLabel)}</p>` : '',
      steps,
      error,
      '</section>',
    ].join('');
  }

  function renderValueDelta(valueDelta) {
    if (!valueDelta) return '';
    if (valueDelta.before !== undefined && valueDelta.after !== undefined) {
      return `<span class="bglab-timeline-delta">${escapeHtml(valueDelta.before)} → ${escapeHtml(valueDelta.after)}</span>`;
    }
    if (valueDelta.delta !== undefined) {
      return `<span class="bglab-timeline-delta">变化 ${escapeHtml(valueDelta.delta)}</span>`;
    }
    return '';
  }

  function renderTimelineChild(entry) {
    const children = entry.children.length === 0 ? '' : [
      '<ol class="bglab-timeline-children">',
      entry.children.map(renderTimelineChild).join(''),
      '</ol>',
    ].join('');
    return [
      `<li class="bglab-timeline-child" data-entry-id="${attr(entry.id)}" data-kind="${attr(entry.kind)}">`,
      entry.icon ? `<span class="bglab-timeline-icon" data-icon="${attr(entry.icon)}"></span>` : '',
      `<span class="bglab-timeline-summary">${escapeHtml(entry.summary)}</span>`,
      renderValueDelta(entry.valueDelta),
      children,
      '</li>',
    ].join('');
  }

  function renderTimelineEntry(entry) {
    const children = entry.children.length === 0 ? '' : [
      '<ol class="bglab-timeline-children">',
      entry.children.map(renderTimelineChild).join(''),
      '</ol>',
    ].join('');
    const actorName = entry.actor ? entry.actor.name : '';
    const actorPrefix = actorName ? `${actorName} ` : '';
    const summary = actorPrefix && entry.summary.startsWith(actorPrefix)
      ? entry.summary.slice(actorPrefix.length)
      : entry.summary;
    const actor = entry.actor
      ? `<strong class="bglab-timeline-actor" data-actor-seat="${attr(entry.actor.seat)}">${escapeHtml(entry.actor.name)}</strong>`
      : '';
    const message = `<span class="bglab-timeline-summary">${actor}${actor && summary ? ' ' : ''}${escapeHtml(summary)}</span>`;
    return [
      `<li class="bglab-timeline-entry" data-entry-id="${attr(entry.id)}" data-turn-id="${attr(entry.turnId || entry.id)}" data-kind="${attr(entry.kind)}">`,
      '  <div class="bglab-timeline-entry-head">',
      entry.icon ? `    <span class="bglab-timeline-icon" data-icon="${attr(entry.icon)}"></span>` : '',
      `    ${message}`,
      `    ${renderValueDelta(entry.valueDelta)}`,
      '  </div>',
      children,
      '</li>',
    ].join('');
  }

  function renderTimelineNormalized(entries) {
    return [
      '<section class="bglab-event-timeline" aria-label="游戏流程">',
      '  <div class="bglab-section-heading"><h2>游戏流程</h2></div>',
      entries.length === 0
        ? '  <p class="bglab-timeline-empty">暂无已确认行动</p>'
        : ['  <ol class="bglab-timeline-list">', entries.map(renderTimelineEntry).join(''), '  </ol>'].join(''),
      '</section>',
    ].join('');
  }

  function renderSidebarNormalized(view) {
    return [
      '<aside class="bglab-game-sidebar">',
      renderProgress(view),
      '<section class="bglab-player-list" aria-label="玩家信息">',
      '  <div class="bglab-section-heading"><h2>玩家</h2></div>',
      view.players.map(renderPlayer).join(''),
      '</section>',
      renderTimelineNormalized(view.timeline),
      '</aside>',
    ].join('');
  }

  function normalizeSlots(value) {
    if (value === undefined || value === null) return {};
    const input = record(value, 'slots');
    const legacyPrimary = typeof input.actionHtml === 'string' ? input.actionHtml : '';
    return {
      turnSurfaceHtml: typeof input.turnSurfaceHtml === 'string' ? input.turnSurfaceHtml : '',
      boardHtml: typeof input.boardHtml === 'string' ? input.boardHtml : '',
      actionPrimaryHtml: typeof input.actionPrimaryHtml === 'string' ? input.actionPrimaryHtml : legacyPrimary,
      actionAuxiliaryHtml: typeof input.actionAuxiliaryHtml === 'string' ? input.actionAuxiliaryHtml : '',
      actionRollbackHtml: typeof input.actionRollbackHtml === 'string' ? input.actionRollbackHtml : '',
      overlayHtml: typeof input.overlayHtml === 'string' ? input.overlayHtml : '',
    };
  }

  function renderActionDockContent(interaction, primaryHtml = '', auxiliaryHtml = '', rollbackHtml = '') {
    const locked = interaction.state === 'resolving' || interaction.state === 'finished';
    const disabled = actionAllowed => (!actionAllowed || locked) ? ' disabled' : '';
    const primaryButtons = [
      interaction.canSkip
        ? `  <button type="button" class="bglab-action-button bglab-action-skip" data-shell-action="skip"${disabled(interaction.canSkip)}>${escapeHtml(interaction.skipLabel)}</button>`
        : '',
      interaction.canConfirm
        ? `  <button type="button" class="bglab-action-button bglab-action-confirm" data-shell-action="confirm"${disabled(interaction.canConfirm)}>${escapeHtml(interaction.confirmLabel)}</button>`
        : '',
    ].filter(Boolean);
    const cancelButton = interaction.canCancel
      ? `  <button type="button" class="bglab-action-button bglab-action-cancel" data-shell-action="cancel"${disabled(interaction.canCancel)}>${escapeHtml(interaction.cancelLabel)}</button>`
      : '';
    const error = interaction.error
      ? `<p class="bglab-action-error" role="alert">${escapeHtml(interaction.error.message)}</p>`
      : '';
    return [
      '<div class="bglab-action-state" aria-live="polite">',
      `  <span class="bglab-action-state-label">${escapeHtml(interaction.instruction)}</span>`,
      '</div>',
      primaryHtml ? `<div class="bglab-game-action-content bglab-game-action-primary">${primaryHtml}</div>` : '',
      primaryButtons.length > 0 ? `<div class="bglab-action-buttons">${primaryButtons.join('')}</div>` : '',
      auxiliaryHtml ? `<div class="bglab-game-action-content bglab-game-action-auxiliary">${auxiliaryHtml}</div>` : '',
      cancelButton ? `<div class="bglab-action-buttons bglab-action-cancel-control">${cancelButton}</div>` : '',
      rollbackHtml ? `<div class="bglab-game-action-content bglab-game-action-rollback">${rollbackHtml}</div>` : '',
      error,
    ].join('');
  }

  function renderFinalResult(result) {
    if (!result) return '';
    if (result.columns && result.rows.some(row => row.values.length !== result.columns.length)) {
      throw new Error('final result columns must label every value');
    }
    const header = result.columns ? `<thead><tr><th scope="col"></th>${result.columns.map(label => `<th scope="col">${escapeHtml(label)}</th>`).join('')}</tr></thead>` : '';
    const rows = result.rows.map(row => [
      `<tr data-result-row-id="${attr(row.id)}">`,
      `  <th scope="row">${escapeHtml(row.label)}</th>`,
      row.values.map(value => `<td>${escapeHtml(value)}</td>`).join(''),
      '</tr>',
    ].join('')).join('');
    const winners = result.winnerSeats.map(seat => `<li data-winner-seat="${attr(seat)}">座位 ${escapeHtml(seat + 1)}</li>`).join('');
    return [
      '<section class="bglab-final-result" data-final-result="true" role="dialog" aria-label="终局计分" aria-live="polite">',
      `  <h2>${escapeHtml(result.title)}</h2>`,
      `  <p class="bglab-final-summary">${escapeHtml(result.summary)}</p>`,
      winners ? `  <ul class="bglab-final-winners">${winners}</ul>` : '',
      result.rows.length > 0 ? `  <table class="bglab-final-table">${header}<tbody>${rows}</tbody></table>` : '',
      '<button type="button" class="bglab-action-button" data-result-visibility="close">收起计分，查看棋盘</button>',
      '</section>',
    ].join('');
  }

  function renderTopBar(view) {
    return renderTopBarNormalized(normalizeView(view));
  }

  function renderSidebar(view) {
    return renderSidebarNormalized(normalizeView(view));
  }

  function renderTimeline(entries) {
    return renderTimelineNormalized(normalizeTimelineEntries(entries, 'entries'));
  }

  function renderActionDock(interaction) {
    const normalized = normalizeInteraction(interaction, 'interaction');
    return [
      `<section class="bglab-action-dock" data-interaction-state="${attr(normalized.state)}">`,
      renderActionDockContent(normalized),
      '</section>',
    ].join('');
  }

  function renderShell(view, slots) {
    const normalized = normalizeView(view);
    const normalizedSlots = normalizeSlots(slots);
    return [
      `<main class="bglab-game-shell" data-game-id="${attr(normalized.game.id)}" data-interaction-state="${attr(normalized.interaction.state)}">`,
      renderTopBarNormalized(normalized),
      `  <section class="bglab-action-dock" data-interaction-state="${attr(normalized.interaction.state)}">`,
      renderActionDockContent(
        normalized.interaction,
        normalizedSlots.actionPrimaryHtml,
        normalizedSlots.actionAuxiliaryHtml,
        normalizedSlots.actionRollbackHtml,
      ),
      normalized.finalResult ? '<button type="button" class="bglab-action-button" data-result-visibility="open">查看终局计分</button>' : '',
      '  </section>',
      normalizedSlots.turnSurfaceHtml
        ? `<section class="bglab-turn-surface" aria-label="当前行动选择">${normalizedSlots.turnSurfaceHtml}</section>`
        : '',
      '<section class="bglab-game-layout">',
      `  <section class="bglab-board-viewport" aria-label="游戏棋盘">${normalizedSlots.boardHtml}</section>`,
      renderSidebarNormalized(normalized),
      '</section>',
      `  <section class="bglab-overlay-host" aria-live="polite">${renderFinalResult(normalized.finalResult)}${normalizedSlots.overlayHtml}</section>`,
      '</main>',
    ].join('');
  }

  function bindActions(rootElement, handlers) {
    if (!rootElement || typeof rootElement.querySelectorAll !== 'function') {
      typeError('rootElement', 'must support querySelectorAll');
    }
    const actionHandlers = isRecord(handlers) ? handlers : {};
    const controls = rootElement.querySelectorAll('[data-shell-action]');
    for (const control of controls) {
      const action = control?.dataset?.shellAction;
      const handler = action && typeof actionHandlers[action] === 'function'
        ? actionHandlers[action]
        : null;
      if (!handler) {
        control.disabled = true;
        control.setAttribute?.('aria-disabled', 'true');
        continue;
      }
      control.addEventListener('click', event => {
        if (control.disabled) return;
        handler(event, action);
      });
    }
    const result = rootElement.querySelector?.('[data-final-result]');
    if (result) {
      const open = rootElement.querySelector('[data-result-visibility="open"]');
      const close = rootElement.querySelector('[data-result-visibility="close"]');
      const setVisible = visible => {
        result.hidden = !visible;
        (visible ? close : open)?.focus();
      };
      open?.addEventListener('click', () => setVisible(true));
      close?.addEventListener('click', () => setVisible(false));
      result.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); setVisible(false); }
      });
    }
  }

  return {
    normalizeView,
    renderTopBar,
    renderSidebar,
    renderTimeline,
    renderActionDock,
    renderShell,
    bindActions,
  };
});
