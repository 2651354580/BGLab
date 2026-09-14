(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.BGLabGameBridge = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  const page = root?.window || root;

  function resolve(value) {
    return typeof value === 'function' ? value() : value;
  }

  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object') {
      return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
    }
    return JSON.stringify(value);
  }

  async function stateHash(snapshot) {
    const Encoder = page?.TextEncoder || root?.TextEncoder || TextEncoder;
    const cryptoImpl = page?.crypto || root?.crypto || crypto;
    const bytes = new Encoder().encode(canonical(snapshot));
    const digest = await cryptoImpl.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  }

  function timerSet(callback, delay) {
    const set = page?.setTimeout || root?.setTimeout || setTimeout;
    return set(callback, delay);
  }

  function timerClear(handle) {
    const clear = page?.clearTimeout || root?.clearTimeout || clearTimeout;
    clear(handle);
  }

  function now() {
    const clock = page?.performance || root?.performance;
    return clock?.now?.() ?? Date.now();
  }

  function createGameBridge(options = {}) {
    const adapterOption = options.adapter;
    const frontendOption = options.frontend;
    const Socket = options.WebSocketImpl || page?.WebSocket || root?.WebSocket;
    const requestTimeoutMs = options.requestTimeoutMs ?? 180000;
    const reconnectInitialMs = options.reconnectInitialMs ?? 500;
    const reconnectMaxMs = options.reconnectMaxMs ?? 5000;
    const deferEventsDuringValidation = options.deferEventsDuringValidation ?? true;
    const onConfirmed = options.onConfirmed || null;
    const openState = Socket?.OPEN ?? 1;
    const connectingState = Socket?.CONNECTING ?? 0;

    const bridge = {
      _ws: null,
      _ready: false,
      _pending: null,
      _validationInFlight: false,
      _retries: 0,
      _reconnectTimer: null,

      validationInFlight() {
        return this._validationInFlight;
      },

      trace(event, details = {}) {
        if (!page?.__BGLAB_HSA026_TRACE__ && !page?.location?.search?.includes('hsa026Trace=1')) return;
        const traceItems = page.__BglabAcceptanceTrace || [];
        traceItems.push({event, at: now(), ...details});
        page.__BglabAcceptanceTrace = traceItems;
      },

      init() {
        if (page?.BG_REPLAY_MODE) return false;
        return this._tryConnect();
      },

      _tryConnect() {
        if (page?.BG_REPLAY_MODE) return false;
        const currentState = this._ws?.readyState;
        if (this._ws && (currentState === openState || currentState === connectingState)) return false;
        if (this._reconnectTimer) {
          timerClear(this._reconnectTimer);
          this._reconnectTimer = null;
        }
        this._retries += 1;
        if (!Socket) {
          this._scheduleReconnect();
          return false;
        }
        try {
          const socket = new Socket('ws://127.0.0.1:7333');
          this._ws = socket;
          socket.onopen = () => {
            if (this._ws !== socket) return;
            this._ready = true;
            this._retries = 0;
            this._send({type: 'frontend_hello'});
            resolve(frontendOption)?.bridgeReady?.();
          };
          socket.onclose = () => {
            if (this._ws !== socket) return;
            this._ready = false;
            this._ws = null;
            this._rejectPending(new Error('Bridge disconnected; game paused'));
            this._scheduleReconnect();
          };
          socket.onerror = () => {
            if (this._ws !== socket) return;
            if (socket.readyState === openState) {
              this._scheduleReconnect();
              return;
            }
            this._ready = false;
            this._ws = null;
            this._rejectPending(new Error('Bridge disconnected; game paused'));
            this._scheduleReconnect();
          };
          socket.onmessage = event => this._onMessage(event);
          return true;
        } catch (_) {
          this._scheduleReconnect();
          return false;
        }
      },

      _send(payload) {
        if (!this._ws || this._ws.readyState !== openState) return false;
        if (['ai_turn', 'action_validation_result', 'state_snapshot'].includes(payload.type)) {
          this.trace(`${payload.type}_sent`, {
            turnId: payload.turnId || null,
            frontendTurnId: payload.result?.frontendTurnId || null,
            frontendStateHash: payload.result?.frontendStateHash || null,
          });
        }
        this._ws.send(JSON.stringify({
          ...payload,
          gameId: page?.BG_GAME_ID || null,
          ...(page?.BG_SESSION_CAPABILITY
            ? {sessionCapability: page.BG_SESSION_CAPABILITY}
            : {}),
        }));
        return true;
      },

      send(payload) {
        return this._send(payload);
      },

      _canonical(value) {
        return canonical(value);
      },

      async _stateHash(snapshot) {
        return stateHash(snapshot);
      },

      async _onMessage(event) {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch (error) {
          (root?.console || console).error('[bridge] invalid JSON', error);
          return;
        }

        if (message.type === 'validate_action') {
          this._validationInFlight = true;
          this.trace('validate_action_received', {turnId: message.turnId || null});
          try {
            this.trace('frontend_apply_start', {turnId: message.turnId || null});
            const adapter = resolve(adapterOption);
            let result = message.mode === 'routes'
              ? {ok: false, code: 'AUTHORITY_WORKER_REQUIRED', message: 'Route enumeration is handled by the host authority worker.'}
              : adapter.dispatch(message.decisionId, message.transaction);
            if (result?.ok) {
              const snapshot = adapter.snapshot();
              result = {
                ...result,
                frontendTurnId: snapshot.decisionId,
                frontendStateHash: await this._stateHash(snapshot),
              };
            }
            this._send({type: 'action_validation_result', requestId: message.requestId, turnId: message.turnId, result});
          } catch (error) {
            this._send({
              type: 'action_validation_result',
              requestId: message.requestId,
              turnId: message.turnId,
              result: {
                ok: false,
                code: 'FRONTEND_DISPATCH_ERROR',
                message: error instanceof Error ? error.message : String(error),
              },
            });
          } finally {
            this._validationInFlight = false;
          }
          return;
        }

        const pending = this._pending;
        if (message.type === 'ai_action' && pending && message.turnId === pending.turnId) {
          this.trace('ai_action_received', {
            turnId: message.turnId || null,
            confirmedTurnId: message.confirmedTurnId || null,
            confirmedStateHash: message.confirmedStateHash || null,
          });
          if (message.adapterCommitted) {
            const adapter = resolve(adapterOption);
            const snapshot = adapter.snapshot();
            if (message.confirmedTurnId !== snapshot.decisionId) {
              this._rejectPending(new Error(`AI frontend confirmation turn mismatch: ${snapshot.decisionId} != ${message.confirmedTurnId}`), pending);
              return;
            }
            if (message.confirmedStateHash !== await this._stateHash(snapshot)) {
              this._rejectPending(new Error('AI frontend confirmation state hash mismatch'), pending);
              return;
            }
          }
          if (this._pending !== pending) return;
          this.trace('frontend_confirmed', {
            turnId: message.turnId || null,
            confirmedTurnId: message.confirmedTurnId || null,
            confirmedStateHash: message.confirmedStateHash || null,
          });
          try {
            const handled = typeof onConfirmed === 'function'
              ? onConfirmed(message, this)
              : false;
            if (!handled) this.persist();
          } catch (error) {
            this._rejectPending(error instanceof Error ? error : new Error(String(error)), pending);
            return;
          }
          this._resolvePending({
            content: message.content || '',
            transaction: message.transaction || message.action || null,
            adapterCommitted: Boolean(message.adapterCommitted),
            confirmedTurnId: message.confirmedTurnId || null,
            confirmedStateHash: message.confirmedStateHash || null,
            ...(message.decisionSource === 'host_fallback' ? {
              decisionSource: message.decisionSource,
              fallback: message.fallback,
            } : {}),
          }, pending);
          return;
        }

        if (message.type === 'ai_error' && pending && message.turnId === pending.turnId) {
          this._rejectPending(new Error(message.error || 'AI API error; game paused'), pending);
          return;
        }
        if (message.type === 'ai_retry' && pending && message.turnId === pending.turnId) {
          this._resolvePending({retry: true}, pending);
          return;
        }
        if (message.type === 'snapshot_error') {
          resolve(frontendOption)?.pause?.(message.error || 'Snapshot persistence failed');
        }
      },

      onMessage(event) {
        return this._onMessage(event);
      },

      _scheduleReconnect() {
        if (this._reconnectTimer || page?.BG_REPLAY_MODE) return;
        const delay = Math.min(reconnectMaxMs, reconnectInitialMs * Math.max(1, this._retries));
        this._reconnectTimer = timerSet(() => {
          this._reconnectTimer = null;
          this._tryConnect();
        }, delay);
      },

      _resolvePending(value, pending = this._pending) {
        if (!pending || this._pending !== pending) return false;
        this._pending = null;
        timerClear(pending.timer);
        pending.resolve(value);
        return true;
      },

      _rejectPending(error, pending = this._pending) {
        if (!pending || this._pending !== pending) return false;
        this._pending = null;
        timerClear(pending.timer);
        pending.reject(error instanceof Error ? error : new Error(String(error)));
        return true;
      },

      async requestAITurn(pid) {
        for (let count = 0; count < 30 && !this._ready; count += 1) {
          await new Promise(resolve => timerSet(resolve, 500));
        }
        if (!this._ready) throw new Error('Bridge not connected');
        if (this._pending) throw new Error('An AI request is already pending');
        const adapter = resolve(adapterOption);
        const snapshot = adapter.snapshot();
        if (!this.persist()) throw new Error('Bridge not connected');
        const adapterView = adapter.view(pid);
        snapshot.adapterView = adapterView;
        const turnId = adapterView.decisionId;
        return new Promise((resolvePromise, rejectPromise) => {
          const pending = {
            turnId,
            timer: null,
            resolve: resolvePromise,
            reject: rejectPromise,
          };
          pending.timer = timerSet(() => {
            this._rejectPending(new Error('AI API timeout; game paused'), pending);
          }, requestTimeoutMs);
          this._pending = pending;
          if (!this._send({type: 'ai_turn', turnId, pid, state: snapshot})) {
            this._rejectPending(new Error('Bridge not connected'), pending);
          }
        });
      },

      persist() {
        const adapter = resolve(adapterOption);
        if (!adapter) return false;
        if (deferEventsDuringValidation && this._validationInFlight) return true;
        const snapshot = adapter.snapshot();
        const finalResult = adapter.finalResult?.();
        return this._send({
          type: 'state_snapshot',
          turnId: snapshot.decisionId,
          state: snapshot,
          ...(finalResult ? {finalResult} : {}),
        });
      },
    };

    return bridge;
  }

  return {createGameBridge};
});
