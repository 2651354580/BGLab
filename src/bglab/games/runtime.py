"""Persistent, half-asynchronous browser game runtime.

The browser owns game state.  This module owns persistent AI conversations and
connects a validated BgAct directly to the waiting WebSocket request.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Callable

from bglab.agents.in_process import InProcessTeammateConfig, InProcessTeammateRunner
from bglab.compaction.autocompact import CompactTracker
from bglab.engine.deps import QueryDeps
from bglab.engine.turn_input import TurnInput
from bglab.games.persistence.store import GameStore
from bglab.games.attachments import build_game_session_head_text
from bglab.games.plans import end_action_plans, plan_state, reconcile_turn_plan, restore_plans, visible_plans
from bglab.games.fallback import try_host_fallback
from bglab.games.model_recovery import restore_recovery
from bglab.games.features import (
    DEFAULT_GAME_PROFILE,
    GameAgentProfile,
    GameFeatureProfile,
)
from bglab.games.registry import GameDefinition, get_game
from bglab.games.final_result import validate_final_result
from bglab.games.decision_surface import render_runtime_decision_frame
from bglab.games.scoring_facts import (
    validate_scoring_decision_facts,
)
from bglab.games.replay import authority_hash
from bglab.games.submission_state import SubmissionState
from bglab.games.tools.tool_factory import (
    AttemptClosedError,
)
from bglab.hooks.state import StopHooksState
from bglab.games.convergence import GameLoopDetector
from bglab.llm.provider_slots import (
    ProviderSlot,
    ProviderSlotPolicy,
    load_provider_slot_policy,
)
from bglab.llm.failure_presentation import project_provider_failure
from bglab.llm.types import ProviderFailureInfo
from bglab.llm.usage import empty_usage_totals, merge_usage_totals, record_request_usage


logger = logging.getLogger(__name__)


def _best_effort_store_telemetry(
    store: GameStore,
    game_id: str,
    method: str,
    payload: dict,
) -> None:
    try:
        getattr(store, method)(payload)
    except Exception as exc:
        logger.warning(
            "game telemetry write failed game_id=%s method=%s error=%s",
            game_id,
            method,
            str(exc)[:300],
        )


def _bgact_result_succeeded(
    operation: Any,
    metadata: Mapping[str, Any],
) -> bool:
    outcome_kind = metadata.get("outcome_kind")
    if operation == "check":
        return outcome_kind in {
            "check_complete",
            "convergence_correction",
            "repair_correction",
        }
    return operation == "commit" and outcome_kind == "commit_complete"


class GameRuntimeError(RuntimeError):
    pass


class GameAPIError(GameRuntimeError):
    pass


class ProviderFailureError(GameAPIError):
    """A structured transport/provider failure surfaced by one submit."""

    def __init__(self, failure: ProviderFailureInfo):
        self.provider_failure = failure
        super().__init__(failure.reason)


class GameInvariantError(GameRuntimeError):
    pass


class AuthorityBindingError(GameAPIError):
    """A browser-supplied AI frame is not the currently confirmed frame."""

    code = "STALE_AUTHORITY_FRAME"

    def __init__(self, message: str, *, expected_turn_id: str | None = None):
        self.expected_turn_id = expected_turn_id
        super().__init__(message)


_MANUAL_RETRY_REASONS = frozenset({
    "decision_deadline",
    "session_delivery_deadline",
    "provider_failure",
    "api_error",
})


def _authorized_manual_retry_record(
    attempt: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Validate one exhausted attempt and return its next durable grant."""
    if (
        attempt.get("status") != "EXHAUSTED"
        or attempt.get("reason") not in _MANUAL_RETRY_REASONS
    ):
        raise GameAPIError("manual retry requires an exhausted provider attempt")
    if attempt.get("identity") != identity:
        raise GameAPIError("manual retry DecisionFrame identity mismatch")
    current = attempt.get("manualRetry")
    if (
        isinstance(current, dict)
        and current.get("status") == "AUTHORIZED"
        and current.get("identity") == identity
    ):
        return copy.deepcopy(current)
    current_epoch = (
        current.get("epoch", 0) if isinstance(current, dict) else 0
    )
    if isinstance(current_epoch, bool) or not isinstance(current_epoch, int):
        raise GameInvariantError("manual retry epoch is malformed")
    return {
        "epoch": max(0, current_epoch) + 1,
        "identity": copy.deepcopy(identity),
        "status": "AUTHORIZED",
    }


def authorize_persisted_manual_retry(
    store: GameStore,
    pid: int,
    identity: dict[str, Any],
) -> int:
    """Atomically add an exact retry grant without constructing a runtime."""
    data = store.read_agent_state(pid)
    if not isinstance(data, dict):
        raise GameAPIError("paused game agent state is missing")
    attempt = data.get("attempt")
    if not isinstance(attempt, dict):
        raise GameAPIError("paused game attempt state is missing")
    manual = _authorized_manual_retry_record(attempt, identity)
    updated = copy.deepcopy(data)
    updated_attempt = copy.deepcopy(attempt)
    updated_attempt["manualRetry"] = manual
    updated["attempt"] = updated_attempt
    store.write_agent_state(pid, updated)
    return int(manual["epoch"])


@dataclass(frozen=True)
class _DisabledGameSkillBundle:
    """Minimal immutable identity for profiles with Layer 2 Skills disabled."""

    engine: str
    version: str = "none"
    root: None = None
    skills: tuple = ()
    fingerprint: str = "none"


def _render_model_decision_frame(
    frame: dict[str, Any],
    *,
    include_decision_examples: bool = False,
    prefer_model_facts: bool = True,
) -> str:
    """Render the package projection; rich output is diagnostic-only."""
    if include_decision_examples:
        raise ValueError("decision examples are not part of the production Frame")
    if prefer_model_facts:
        return render_runtime_decision_frame(copy.deepcopy(frame))
    from bglab.games.decision_surface import (
        render_internal_runtime_frame_for_historical_diagnostics,
    )

    return render_internal_runtime_frame_for_historical_diagnostics(
        copy.deepcopy(frame),
    )


def _game_turn_input(
    frame: dict[str, Any],
    *,
    retry_instruction: str | None = None,
    resuming_same_turn: bool = False,
) -> TurnInput:
    if retry_instruction:
        return TurnInput(retry_instruction, is_meta=True)
    if resuming_same_turn:
        return TurnInput(
            "Recovery: continue the same authoritative DecisionFrame "
            "from the existing history. Do not repeat the Frame or "
            "restart the analysis; continue from the latest Tool "
            "result and submit one valid BgAct.",
            is_meta=True,
        )
    return TurnInput(
        "## Authoritative DecisionFrame (Authoritative turn snapshot)\n"
        + _render_model_decision_frame(frame),
        kind="authority_snapshot",
    )


EventCallback = Callable[[dict], Any]
GAME_DECISION_TIMEOUT_SECONDS = 900.0
GAME_DECISION_MODEL_REQUEST_LIMIT = 14


def _serialize_agent_profile(profile: GameAgentProfile) -> dict[str, Any]:
    """Return the durable, model-visible identity for one experiment seat."""
    return {
        "features": {
            field.name: getattr(profile.features, field.name)
            for field in fields(GameFeatureProfile)
        },
        "enabled_tool_names": sorted(profile.enabled_tool_names),
        "skill_bundle_version": profile.skill_bundle_version,
    }


def _action_delivery_timeout(provider_timeout_seconds: float | None) -> float | None:
    """Leave a bounded handoff window after the provider deadline.

    The agent task owns the provider timeout.  The caller needs a small extra
    window to receive a tool result that is already returning and to publish
    its validated commit without racing the provider deadline.
    """
    if provider_timeout_seconds is None:
        return None
    return provider_timeout_seconds + min(
        5.0,
        max(0.1, provider_timeout_seconds * 0.05),
    )


def restore_agent_profiles(
    manifest: dict[str, Any],
    player_count: int,
) -> list[GameAgentProfile]:
    """Strictly reconstruct persisted per-seat capability identities."""
    rows = manifest.get("agent_profiles") if isinstance(manifest, dict) else None
    if not isinstance(rows, list) or len(rows) != player_count:
        raise GameInvariantError(
            "persisted agent_profiles must match the saved player count",
        )
    feature_fields = {field.name for field in fields(GameFeatureProfile)}
    restored: list[GameAgentProfile] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}] must be an object",
            )
        if not set(row).issubset({
            "features", "enabled_tool_names", "skill_bundle_version",
            "skill_bundle_fingerprint",
        }):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}] has unknown fields",
            )
        raw_features = row.get("features")
        if not isinstance(raw_features, dict):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].features is malformed",
            )
        features = copy.deepcopy(raw_features)
        legacy_profile_fields = {"context_history", "decision_staging"}
        if (
            set(features) - feature_fields - legacy_profile_fields
            or feature_fields - set(features)
        ):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].features is malformed",
            )
        legacy_context = features.pop("context_history", "convergent")
        legacy_staging = features.pop("decision_staging", "single-response")
        if legacy_context not in {"convergent", "append-only"}:
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].features.context_history "
                "must be convergent or append-only",
            )
        if legacy_staging not in {"single-response", "natural-then-act"}:
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].features.decision_staging "
                "must be single-response or natural-then-act",
            )
        if features.get("name") == "release-semantic-v2-append-only":
            features["name"] = "release-semantic-v2"
        if not isinstance(features.get("name"), str) or not features["name"].strip():
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].features.name is malformed",
            )
        if any(
            not isinstance(features[name], bool)
            for name in feature_fields - {"name"}
        ):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].features flags must be boolean",
            )
        tool_names = row.get("enabled_tool_names")
        if (
            not isinstance(tool_names, list)
            or any(not isinstance(name, str) or not name.strip() for name in tool_names)
            or len(set(tool_names)) != len(tool_names)
        ):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].enabled_tool_names is malformed",
            )
        version = row.get("skill_bundle_version")
        if not isinstance(version, str) or not version.strip():
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].skill_bundle_version is malformed",
            )
        fingerprint = row.get("skill_bundle_fingerprint")
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or not fingerprint
        ):
            raise GameInvariantError(
                f"persisted agent_profiles[{index}].skill_bundle_fingerprint is malformed",
            )
        try:
            profile = GameFeatureProfile(**features)
            restored.append(GameAgentProfile(
                features=profile,
                enabled_tool_names=frozenset(tool_names),
                skill_bundle_version=version,
            ))
        except (TypeError, ValueError) as exc:
            raise GameInvariantError(
                f"persisted agent_profiles[{index}] is invalid: {exc}",
            ) from exc
    return restored

def _turn_number(turn_id: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(turn_id).split(":"))
    except (TypeError, ValueError):
        return None


def _snapshot_ordinal(turn_id: str | None, player_count: int) -> int | None:
    if not turn_id:
        return None
    try:
        turn_text, player_text, *_ = str(turn_id).split(":")
        return int(turn_text) * max(player_count, 1) + int(player_text)
    except (TypeError, ValueError):
        return None


def _emit(callback: EventCallback | None, event: dict) -> None:
    if callback is None:
        return
    try:
        result = callback(event)
    except Exception:
        # Event consumers are telemetry only. The authoritative Adapter
        # transaction and durable game-store records have already been handled
        # by the caller and must not be converted into a failed action because
        # a console/browser observer is unavailable.
        return
    if asyncio.iscoroutine(result):
        task = asyncio.create_task(result)

        def consume_callback_error(completed: asyncio.Task) -> None:
            try:
                completed.result()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(consume_callback_error)


def _game_model_call(
    call_model: Any,
    *,
    provider_policy: ProviderSlotPolicy | None = None,
    recovery_profile: Any = None,
    action_protocol: str = "semantic-v2",
) -> Any:
    """Apply one atomic, thinking-aware retry policy to game model calls."""

    async def call_with_game_policy(**kwargs: Any):
        if action_protocol != "semantic-v2":
            raise ValueError(
                f"unsupported historical action protocol: {action_protocol}; "
                "current runtime requires semantic-v2"
            )

        # Preserve the immutable per-submit slot through the game retry
        # wrapper; this must not be stored on shared QueryDeps.
        provider_slot = kwargs.get("provider_slot")
        request_policy = getattr(recovery_profile, "provider_request", None)
        configured_attempts = int(
            getattr(request_policy, "max_attempts", 1) or 1,
        )
        if provider_policy is not None:
            configured_attempts = min(
                configured_attempts,
                max(1, int(provider_policy.max_attempts)),
            )
        kwargs["max_retries"] = configured_attempts
        kwargs["fallback_model"] = None
        if request_policy is not None:
            kwargs["atomic_attempts"] = request_policy.atomic_attempts
            for key in (
                "first_event_timeout_seconds",
                "idle_timeout_seconds",
                "attempt_timeout_seconds",
                "parallel_tool_calls",
            ):
                value = getattr(request_policy, key)
                if value is not None:
                    kwargs[key] = value
        if provider_slot is not None:
            kwargs["provider_slot"] = provider_slot
        async for event in call_model(**kwargs):
            yield event

    return call_with_game_policy


@dataclass
class TurnHandle:
    turn_id: str
    action: asyncio.Future
    completion: asyncio.Task
    # Queued same-seat DecisionFrames do not start their provider-delivery
    # budget until the preceding post-action task has drained.
    started: asyncio.Future | None = None


@dataclass(frozen=True)
class ConfirmedAuthorityFrame:
    """Immutable identity for one host-confirmed AI decision frame.

    ``state`` is copied when the frame is constructed and when callers read it
    through ``snapshot``.  The dataclass is frozen so a queued frame cannot be
    retargeted to a later decision while it waits for an earlier attempt to
    drain.  ``token`` identifies the complete persisted snapshot envelope;
    ``authority_hash`` identifies only game authority and intentionally omits
    the browser-provided ``adapterView``.
    """

    pid: int
    turn_id: str
    state: dict[str, Any]
    authority_hash: str
    token: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", copy.deepcopy(self.state))

    @property
    def snapshot(self) -> dict[str, Any]:
        """Return a detached copy for a caller that needs model input."""
        return copy.deepcopy(self.state)


def _authority_wrapper(state: dict[str, Any]) -> dict[str, Any]:
    """Read the package snapshot wrapper without trusting browser views."""
    wrapper = state.get("wrapper")
    if not isinstance(wrapper, dict) or "currentPlayer" not in wrapper:
        compact = state.get("st")
        if isinstance(compact, dict):
            wrapper = compact
    if not isinstance(wrapper, dict) or "currentPlayer" not in wrapper:
        nested = state.get("state")
        wrapper = nested.get("wrapper") if isinstance(nested, dict) else None
        if not isinstance(wrapper, dict) or "currentPlayer" not in wrapper:
            compact = nested.get("st") if isinstance(nested, dict) else None
            if isinstance(compact, dict):
                wrapper = compact
    return wrapper if isinstance(wrapper, dict) else {}


def _snapshot_token(record: dict[str, Any]) -> str:
    """Return an exact token for one persisted snapshot envelope."""
    return hashlib.sha256(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()


def _authority_frame_is_current(
    store: GameStore,
    frame: ConfirmedAuthorityFrame,
) -> bool:
    record = store.read_snapshot()
    if not isinstance(record, dict):
        return False
    if store.read_pending_replay_turn() is not None:
        return False
    return _snapshot_token(record) == frame.token


def _retrieve_future_exception(future: asyncio.Future | asyncio.Task) -> None:
    """Mark a Future/Task exception retrieved without changing await semantics."""
    if not future.done() or future.cancelled():
        return
    future.exception()


def _normalize_attempt_reason(reason: Any) -> str:
    text = str(reason or "").strip()
    return text[:200] or "cancelled"


@dataclass(frozen=True)
class GameAttemptToken:
    """Immutable identity captured by every handler belonging to one turn."""

    game_id: str
    decision_id: str
    seat: int
    state_hash: str
    generation: int


@dataclass
class GameAttemptLease:
    """Controller-owned OPEN -> CLOSING -> CLOSED attempt state."""

    token: GameAttemptToken
    status: str = "OPEN"
    reason: str = ""

    def is_open(self, token: GameAttemptToken) -> bool:
        return self.status == "OPEN" and token == self.token

    def close(self, reason: str = "cancelled") -> bool:
        """Fence the lease exactly once; return whether this call changed it."""
        if self.status != "OPEN":
            return False
        self.reason = _normalize_attempt_reason(reason)
        self.status = "CLOSING"
        return True

    def mark_closed(self) -> None:
        if self.status != "CLOSED":
            self.status = "CLOSED"


def _stale_attempt_result() -> str:
    return json.dumps({
        "status": "invalid",
        "stateChanged": False,
        "error": {
            "code": "STALE_ATTEMPT_REJECTED",
            "message": "This DecisionFrame attempt is no longer active.",
        },
    }, ensure_ascii=False)


class GameTeammateController:
    """Game-specific controller around the shared in-process teammate runner."""

    def __init__(
        self,
        pid: int,
        game_id: str,
        store: GameStore,
        game_rules: str,
        *,
        model: str = "deepseek-chat",
        call_model: Any = None,
        event_callback: EventCallback | None = None,
        restored: dict | None = None,
        recovery_state: dict[str, int] | None = None,
        team_name: str | None = None,
        chat_sink: Callable[[int, Any, str], str] | None = None,
        public_state_provider: Callable[[int], list[dict]] | None = None,
        definition: GameDefinition | None = None,
        action_validator: Callable[[str, int, dict], Any] | None = None,
        profile: GameFeatureProfile = DEFAULT_GAME_PROFILE,
        enabled_tool_names: frozenset[str] = frozenset(),
        skill_bundle_version: str = "none",
        skill_bundle: Any = None,
        stop_after_action: bool = False,
        action_protocol: str = "semantic-v2",
        provider_policy: ProviderSlotPolicy | None = None,
        host_fallback_enabled: bool = False,
    ):
        from bglab.games.tools.tool_factory import create_all_tools
        from bglab.llm.client import call_model as production_call_model
        from bglab.session.feature_flags import FeatureFlags

        self.pid = pid
        self.host_fallback_enabled = host_fallback_enabled
        self.host_fallbacks: dict[str, dict] = {}
        # Model-only decisions are bounded by actual requests and each
        # provider attempt. A wall clock for the entire decision can interrupt
        # healthy Check/Commit progress; explicit operator deadlines still work.
        self.api_turn_timeout_seconds = GAME_DECISION_TIMEOUT_SECONDS if host_fallback_enabled else None
        self.game_id = game_id
        self.store = store
        self.definition = definition or get_game("splendor")
        self.engine = self.definition.id
        self.model = model
        self.profile = profile
        if action_protocol != "semantic-v2":
            raise ValueError(
                f"unsupported historical action protocol: {action_protocol}; "
                "current runtime requires semantic-v2"
            )
        self.action_protocol = action_protocol
        self.agent_profile = GameAgentProfile(
            features=profile,
            enabled_tool_names=enabled_tool_names,
            skill_bundle_version=skill_bundle_version,
        )
        self.skills_enabled = (
            profile.skills
            and self.agent_profile.skill_bundle_version != "none"
        )
        if self.skills_enabled:
            from bglab.games.skills.loader import (
                get_bundle_matching_skill_metadata,
                get_bundle_skill_listing,
                resolve_game_skill_bundle,
            )
            from bglab.tools.skill import SkillTool

            self.skill_bundle = skill_bundle or resolve_game_skill_bundle(
                self.engine,
                self.agent_profile.skill_bundle_version,
            )
        else:
            self.skill_bundle = skill_bundle or _DisabledGameSkillBundle(self.engine)
        self.enabled_tool_names = self.agent_profile.enabled_tool_names
        # Capability is selected only by the persisted Profile. Deterministic
        # harnesses that do not want auxiliary calls must select a Profile
        # without those Modules rather than changing behavior by injection.
        self._memory_selection_enabled = profile.memory_selection
        self._memory_extraction_enabled = profile.memory_extraction
        self._reporting_enabled = profile.battle_reports
        self.event_callback = event_callback
        self.team_name = team_name or f"bg-{game_id}"
        self.chat_sink = chat_sink
        self.public_state_provider = public_state_provider
        self.action_validator = action_validator
        self._production_call_model = call_model is None
        self.game_rules = game_rules
        self._game_skill_bodies = ""
        self._game_relink_state: dict[str, Any] | None = None
        if self.skills_enabled:
            self.game_skill_listing = get_bundle_skill_listing(self.skill_bundle)
        else:
            self.game_skill_listing = ""
        self.game_turn_count = 0
        self.phase = "IDLE"
        self.last_received_turn_id: str | None = None
        self.last_committed_turn_id: str | None = None
        self.committed_actions: dict[str, dict] = {}
        self.pending_chat: list[dict] = []
        self.loaded_skills: list[str] = []
        self.loaded_skill_phases: dict[str, str] = {}
        self.usage_totals = empty_usage_totals()
        self.request_records: list[dict[str, Any]] = []
        self.surfaced_game_memories: dict[str, float] = {}
        self._memory_force_pending = False
        self._memory_extraction_task: asyncio.Task | None = None
        self._stopping = False
        self._chat_sequence = 0
        self._active_tools: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._handles: dict[str, TurnHandle] = {}
        self._scheduled_turn_handle: TurnHandle | None = None
        self._attempt_generation = 0
        self._attempt_identity: dict[str, Any] | None = None
        self._attempt_ordinal = 0
        self._attempt_retry_count = 0
        self._attempt_status = "IDLE"
        self._attempt_reason = ""
        self._attempt_prompt_delivered = False
        self._manual_retry_epoch = 0
        self._manual_retry_identity: dict[str, Any] | None = None
        self._manual_retry_status = "NONE"
        self._active_attempt: GameAttemptLease | None = None
        self.provider_slots = provider_policy or load_provider_slot_policy({"model": model})
        self._active_provider_slot = self.provider_slots.primary
        self._explicit_slot_credentials: dict[str, bool] | None = None
        self._pending_drain: asyncio.Task | None = None
        self._deadline_task: asyncio.Task | None = None
        self._runner_submit_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_turn_group_actions: list[tuple[str, dict, int]] = []
        self._active_turn_group_id = ""
        self._last_decision_frame: dict[str, Any] | None = None
        self._last_visible_attachments: list[dict[str, Any]] = []
        try:
            self._owner_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._owner_loop = None

        self.tools, self.tool_ctx = create_all_tools(
            pid, self.definition.action_profile,
            profile=profile, enabled_tool_names=self.enabled_tool_names,
            action_protocol=action_protocol,
            definition=self.definition,
        )
        if "BgObserve" not in self.enabled_tool_names:
            self.tools = [tool for tool in self.tools if tool.name != "BgObserve"]

        def load_game_skill(args: dict) -> str:
            call_args = dict(args)
            requested_name = str(call_args.get("skill", "")).strip()
            skill_name = requested_name
            state = self.tool_ctx.get("_state", {})
            for guide in get_bundle_matching_skill_metadata(
                self.skill_bundle, state, seat=self.pid, limit=32,
            ):
                exact_name = str(guide["name"])
                if requested_name == exact_name or any(
                    requested_name.startswith(exact_name + separator)
                    for separator in ("：", ":", " — ", " - ")
                ):
                    skill_name = exact_name
                    call_args["skill"] = exact_name
                    break
            return self._activate_game_skill(
                call_args,
                skill_name,
                event_type="skill_loaded",
            )

        if self.skills_enabled:
            from bglab.games.prompt.agent_runtime import GAME_SKILL_TOOL_PROMPT

            self.tools.append(replace(
                SkillTool, call=load_game_skill,
                description="按需读取本局的一份专题策略方法。",
                prompt=GAME_SKILL_TOOL_PROMPT + self.game_skill_listing,
            ))
        self.tool_ctx["_persist_state"] = self.persist
        self.tool_ctx["_chat_inbox"] = lambda: self.pending_chat
        self.tool_ctx['_chat_enabled'] = profile.chat
        self.tool_ctx["_chat_sink"] = lambda target, message, **kwargs: (
            self.chat_sink(self.pid, target, message, **kwargs)
            if self.chat_sink is not None
            else "Game chat transport is not available."
        )
        from bglab.games.query_profile import build_game_query_profile

        query_profile = build_game_query_profile(
            self,
            stop_after_action=stop_after_action,
        )

        selected_call_model = (
            call_model
            if call_model is not None
            else _game_model_call(
                production_call_model,
                provider_policy=self.provider_slots,
                recovery_profile=query_profile.recovery,
                action_protocol=self.action_protocol,
            )
        )
        self.deps = QueryDeps(
            call_model=selected_call_model,
            compact_tracker=CompactTracker(),
            loop_detector=GameLoopDetector(),
            stop_hooks_state=StopHooksState(),
            feature_flags=FeatureFlags(bg_enabled=True),
            query_profile=query_profile,
        )
        from bglab.engine.prompt_profiles import resolve_prompt_bundle_sync

        def request_prompt_resolver(
            *,
            tools: list[Any],
            model: str,
            permission_mode: str,
        ):
            del permission_mode
            return resolve_prompt_bundle_sync(
                query_profile.prompt,
                engine=self.engine,
                model=model,
                feature_profile=replace(
                    profile,
                    skills=self.skills_enabled,
                    basic_tools=any(tool.name == "BgAct" for tool in tools),
                ),
                action_protocol=action_protocol,
                enabled_tool_names=frozenset(tool.name for tool in tools),
            )

        self.deps.request_prompt_resolver = request_prompt_resolver
        prompt_bundle = request_prompt_resolver(
            tools=[tool.to_tool_definition() for tool in self.tools],
            model=self.model,
            permission_mode="bypass",
        )
        self.system_prompt = prompt_bundle.system_prompt
        self.deps._game_attachment_agent = self
        self.deps._game_action_protocol = action_protocol
        self.deps._game_tool_ctx = self.tool_ctx
        self.deps._game_memories_preloaded = True
        self.deps._game_recovery_state = recovery_state if recovery_state is not None else {"total": 0}

        def persist_missing_act_reminder(total: int) -> None:
            self.store.update_manifest(missing_act_recoveries=total)
            event = {
                "type": "missing_act_reminder",
                "game_id": self.game_id,
                "turn_id": self.last_received_turn_id,
                "pid": self.pid,
                "total": total,
            }
            self.store.log_event(event)
            _emit(self.event_callback, event)
            # Preserve the one-reminder boundary across pause/restart without
            # writing any model-visible text into the cache.
            self.store.write_agent_state(self.pid, self.serialize())

        self.deps._game_recovery_persist = persist_missing_act_reminder

        def persist_model_recovery() -> None:
            lease = self._active_attempt
            if lease is not None and lease.is_open(lease.token):
                self.store.write_agent_state(self.pid, self.serialize())

        self.deps._game_model_recovery_persist = persist_model_recovery
        self.deps._game_convergence_delivery_keys = set()
        self.deps._game_delivered_attachment_ids = set()
        self.transcript_session_id = f"game-{game_id}-p{pid}"
        self.tool_ctx["_session_id"] = self.transcript_session_id
        restored_messages = list((restored or {}).get("messages", []))
        if restored and "messages" not in restored:
            restored_messages = self._load_transcript()
        self.runner = InProcessTeammateRunner(InProcessTeammateConfig(
            name=f"ai-p{pid}",
            team_name=self.team_name,
            system_prompt=self.system_prompt,
            tools=[tool.to_tool_definition() for tool in self.tools],
            handlers={tool.name: tool.call for tool in self.tools},
            deps=self.deps,
            model=self.model,
            max_turns=GAME_DECISION_MODEL_REQUEST_LIMIT,
            permission_mode="bypass",
            session_id=self.transcript_session_id,
            messages=restored_messages,
            session_meta={"mode": "game", "game_id": game_id, "pid": pid},
            event_callback=self._handle_runner_event,
        ))
        self._base_handlers = dict(self.runner.config.handlers)
        if restored:
            self.restore(restored)

    @property
    def messages(self) -> list[dict]:
        return self.runner.messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        self.runner.messages = list(value)

    def _load_transcript(self) -> list[dict]:
        from pathlib import Path
        from bglab.persistence import load_messages_from_boundary
        from bglab.persistence.transcript import TranscriptWriter

        path = TranscriptWriter(session_id=self.transcript_session_id).path
        if not Path(path).is_file():
            return []
        messages, _ = load_messages_from_boundary(path)
        return messages

    def _attempt_identity_for(self, turn_id: str, state: dict) -> dict[str, Any]:
        state_hash = authority_hash(state)
        return {
            "gameId": self.game_id,
            "decisionId": turn_id,
            "seat": self.pid,
            "stateHash": state_hash,
        }

    def set_provider_slots(
        self,
        policy: ProviderSlotPolicy,
        *,
        credential_configured: bool | dict[str, bool] = True,
    ) -> None:
        """Install a non-secret slot policy for deterministic runtime calls."""
        self.provider_slots = policy
        self._active_provider_slot = policy.primary
        if isinstance(credential_configured, dict):
            self._explicit_slot_credentials = {
                str(key): bool(value)
                for key, value in credential_configured.items()
            }
        else:
            self._explicit_slot_credentials = {
                slot.credential_env: bool(credential_configured)
                for slot in (policy.primary, policy.standby)
                if slot is not None
            }

    def _slot_credential_configured(self, slot: ProviderSlot) -> bool:
        if self._explicit_slot_credentials is not None:
            return bool(self._explicit_slot_credentials.get(slot.credential_env, False))
        try:
            from bglab.llm.credentials import credential_status

            return bool(credential_status(slot.credential_env).configured)
        except Exception:
            return False

    def retry_slot_for(
        self, failure: ProviderFailureInfo | None,
    ) -> ProviderSlot | None:
        """Choose one drained retry slot; never switch back from Standby."""
        if failure is None or not failure.switch_slot:
            return None
        if self._active_provider_slot.id == "standby":
            return None
        standby = self.provider_slots.standby
        if (
            standby is not None
            and standby.enabled
            and self._slot_credential_configured(standby)
        ):
            return standby
        # Preserve the existing bounded same-slot retry when no usable
        # standby is configured.
        return self.provider_slots.primary

    def provider_retry_instruction(self) -> str:
        if self._attempt_prompt_delivered:
            return (
                "Recovery: the previous Provider request failed after receiving "
                "the authoritative DecisionFrame. Retry this same decision now; "
                "do not repeat the DecisionFrame, and submit one valid BgAct."
            )
        return ""

    def manual_retry_instruction(self) -> str:
        """Return the short prompt for an explicit user-authorized retry."""
        if self._attempt_prompt_delivered:
            return (
                "Recovery (user-authorized): retry this same authoritative "
                "DecisionFrame now. Do not repeat the DecisionFrame and do "
                "not guess an action; submit one valid BgAct."
            )
        return "Recovery (user-authorized): submit one valid BgAct now."

    def authorize_manual_retry(self, identity: dict[str, Any]) -> int:
        """Persist one exact, idempotent user retry authorization."""
        if self._active_task is not None and not self._active_task.done():
            raise GameAPIError("manual retry requires the prior attempt to drain")
        if self._active_attempt is not None and self._active_attempt.status == "OPEN":
            raise GameAPIError("manual retry requires the prior attempt to close")
        manual = _authorized_manual_retry_record(
            self.serialize()["attempt"],
            identity,
        )
        self._manual_retry_epoch = int(manual["epoch"])
        self._manual_retry_identity = copy.deepcopy(manual["identity"])
        self._manual_retry_status = str(manual["status"])
        self.persist()
        return self._manual_retry_epoch

    def manual_retry_ready(self) -> dict[str, Any] | None:
        if (
            self._manual_retry_status != "AUTHORIZED"
            or not isinstance(self._manual_retry_identity, dict)
        ):
            return None
        return {
            "epoch": self._manual_retry_epoch,
            "identity": copy.deepcopy(self._manual_retry_identity),
        }

    def retry_available(self, turn_id: str, state: dict) -> bool:
        """Return whether this exact DecisionFrame still has one retry budget."""
        return (
            self._attempt_identity == self._attempt_identity_for(turn_id, state)
            and self._attempt_ordinal == 1
            and self._attempt_retry_count == 0
            and self._attempt_status in {"CLOSED", "FAILED"}
            and self._attempt_reason in {
                "decision_deadline", "session_delivery_deadline",
                "provider_failure", "frontend_disconnected",
            }
        )

    def retry_instruction(self) -> str:
        """Build the short recovery prompt used after durable frame delivery."""
        if self._attempt_prompt_delivered:
            return (
                "Recovery: the previous attempt timed out after receiving the "
                "authoritative DecisionFrame. Retry this same decision now; do "
                "not repeat the DecisionFrame, and submit one valid BgAct."
            )
        return ""

    def mark_attempt_exhausted(self) -> None:
        """Persist terminal retry exhaustion after all owned work has drained."""
        self._attempt_status = "EXHAUSTED"
        self.persist()

    def _mark_prompt_delivered(self, lease: GameAttemptLease) -> None:
        if not lease.is_open(lease.token):
            return
        self._attempt_prompt_delivered = True
        self.persist()

    def restore(self, data: dict) -> None:
        restored_engine = data.get("engine")
        if restored_engine and restored_engine != self.engine:
            raise GameInvariantError(
                f"agent engine mismatch: {restored_engine} != {self.engine}",
            )
        restored_profile = data.get("agent_profile")
        if (
            restored_profile is not None
            and restored_profile != _serialize_agent_profile(self.agent_profile)
        ):
            raise GameInvariantError("agent profile mismatch during restore")
        restored_skill_fingerprint = data.get("skill_bundle_fingerprint")
        if (
            restored_skill_fingerprint is not None
            and restored_skill_fingerprint != self.skill_bundle.fingerprint
        ):
            raise GameInvariantError("agent Skill bundle mismatch during restore")
        restored_action_protocol = data.get("action_protocol")
        if (
            restored_action_protocol is not None
            and restored_action_protocol != self.action_protocol
        ):
            raise GameInvariantError("agent action protocol mismatch during restore")
        if data.get("messages"):
            # Migration path for saves created by the superseded standalone loop.
            self.messages = list(data["messages"])
        self.phase = "IDLE"  # in-flight work is replayed from the confirmed snapshot
        self.last_received_turn_id = data.get("last_received_turn_id")
        self.last_committed_turn_id = data.get("last_committed_turn_id")
        self.committed_actions = dict(data.get("committed_actions", {}))
        self._pending_turn_group_actions = [
            (str(item["turn_id"]), copy.deepcopy(item["action"]), int(item["elapsed_ms"]))
            for item in data.get("pending_turn_group_actions", [])
            if isinstance(item, dict)
            and isinstance(item.get("turn_id"), str)
            and isinstance(item.get("action"), dict)
        ]
        self.pending_chat = list(data.get("pending_chat", []))
        self.loaded_skills = list(data.get("loaded_skills", []))
        self.loaded_skill_phases = dict(data.get("loaded_skill_phases", {}))
        restored_usage = dict(data.get("usage_totals", {}))
        self.usage_totals = empty_usage_totals()
        merge_usage_totals(self.usage_totals, restored_usage)
        self.surfaced_game_memories = dict(data.get("surfaced_game_memories", {}))
        memory_state = dict(data.get("game_memory_state", {}))
        hooks_state = self.deps.stop_hooks_state
        hooks_state.last_game_memory_message_uuid = memory_state.get("cursor")
        hooks_state.last_game_memory_message_count = int(memory_state.get("message_count", 0) or 0)
        hooks_state.game_memory_extract_count = int(memory_state.get("extract_count", 0) or 0)
        hooks_state.game_turns_since_last_extraction = int(memory_state.get("turns_since", 0) or 0)
        self._memory_force_pending = bool(memory_state.get("force_pending", False))
        self._chat_sequence = int(data.get("chat_sequence", 0) or 0)
        self.game_turn_count = int(data.get("game_turn_count", 0) or 0)
        delivery_keys = data.get("convergence_delivery_keys", [])
        self.deps._game_convergence_delivery_keys = {
            str(item) for item in delivery_keys if isinstance(item, str)
        }
        delivered_attachment_ids = data.get("delivered_attachment_ids", [])
        self.deps._game_delivered_attachment_ids = {
            str(item)
            for item in delivered_attachment_ids
            if isinstance(item, str)
        }
        self.deps._game_active_decision_identity = copy.deepcopy(
            data.get("active_decision_identity")
        )
        self.deps._game_missing_act_this_turn = int(
            data.get("missing_act_attempts", 0) or 0
        )
        self.deps._game_missing_act_decision_id = data.get("missing_act_decision_id")
        self.deps._game_model_recovery = restore_recovery(data.get("model_recovery"))
        restore_plans(self.tool_ctx, data)
        self.host_fallbacks = copy.deepcopy(data.get("host_fallbacks", {}))
        # Legacy text is retained privately for audit, never promoted to active plans.
        self.tool_ctx["_plan"] = data.get("plan", "")
        self.tool_ctx["_scratchpad"] = data.get("scratchpad", "")
        self.tool_ctx["_plan_phase"] = data.get("plan_phase", "unknown")
        self.tool_ctx["_restored_semantic_v2_lifecycle"] = copy.deepcopy(
            data.get("active_semantic_v2_lifecycle")
        )
        self.tool_ctx["_semantic_lifecycle_pending_confirmation"] = bool(
            data.get("active_semantic_v2_lifecycle_pending_confirmation", False)
        )
        self.tool_ctx["_semantic_latest_check_route_count"] = max(
            0,
            int(data.get("semantic_latest_check_route_count", 0) or 0),
        )
        self.tool_ctx["_semantic_latest_ready_route_count"] = max(
            0,
            int(data.get("semantic_latest_ready_route_count", 0) or 0),
        )
        attempt = data.get("attempt")
        if isinstance(attempt, dict):
            identity = attempt.get("identity")
            if isinstance(identity, dict):
                self._attempt_identity = copy.deepcopy(identity)
            self._attempt_generation = max(0, int(attempt.get("generation", 0) or 0))
            self._attempt_ordinal = max(0, int(attempt.get("ordinal", 0) or 0))
            self._attempt_retry_count = max(0, int(attempt.get("retryCount", 0) or 0))
            self._attempt_status = str(attempt.get("status", "IDLE") or "IDLE")
            self._attempt_reason = _normalize_attempt_reason(attempt.get("reason", "")) if attempt.get("reason") else ""
            self._attempt_prompt_delivered = bool(attempt.get("promptDelivered", False))
            slot_meta = attempt.get("slot")
            if isinstance(slot_meta, dict):
                for slot in (self.provider_slots.primary, self.provider_slots.standby):
                    if slot is not None and all(
                        slot_meta.get(key) == getattr(slot, attr)
                        for key, attr in (
                            ("slotId", "id"),
                            ("provider", "provider"),
                            ("model", "reference"),
                        )
                    ):
                        self._active_provider_slot = slot
                        break
            manual_retry = attempt.get("manualRetry")
            if manual_retry is not None:
                if not isinstance(manual_retry, dict):
                    raise GameInvariantError("manual retry state must be an object")
                epoch = manual_retry.get("epoch")
                identity = manual_retry.get("identity")
                status = manual_retry.get("status")
                if (
                    isinstance(epoch, bool)
                    or not isinstance(epoch, int)
                    or epoch < 0
                    or not isinstance(identity, dict)
                    or status not in {"AUTHORIZED", "CONSUMED"}
                ):
                    raise GameInvariantError("manual retry state is malformed")
                if status == "AUTHORIZED" and identity != self._attempt_identity:
                    raise GameInvariantError(
                        "manual retry identity does not match the active attempt",
                    )
                self._manual_retry_epoch = epoch
                self._manual_retry_identity = copy.deepcopy(identity)
                self._manual_retry_status = status

    def serialize(self) -> dict:
        return {
            "pid": self.pid,
            "engine": self.engine,
            "agent_profile": _serialize_agent_profile(self.agent_profile),
            "skill_bundle_fingerprint": self.skill_bundle.fingerprint,
            "action_protocol": self.action_protocol,
            "transcript_session_id": self.transcript_session_id,
            "plans": copy.deepcopy(plan_state(self.tool_ctx)),
            "host_fallbacks": copy.deepcopy(self.host_fallbacks),
            "plan": self.tool_ctx.get("_plan", ""),
            "scratchpad": self.tool_ctx.get("_scratchpad", ""),
            "plan_phase": self.tool_ctx.get("_plan_phase", "unknown"),
            "phase": self.phase,
            "last_received_turn_id": self.last_received_turn_id,
            "last_committed_turn_id": self.last_committed_turn_id,
            "committed_actions": self.committed_actions,
            "pending_turn_group_actions": [
                {"turn_id": turn_id, "action": action, "elapsed_ms": elapsed_ms}
                for turn_id, action, elapsed_ms in self._pending_turn_group_actions
            ],
            "pending_chat": self.pending_chat,
            "loaded_skills": self.loaded_skills,
            "loaded_skill_phases": self.loaded_skill_phases,
            "usage_totals": dict(self.usage_totals),
            "surfaced_game_memories": self.surfaced_game_memories,
            "chat_sequence": self._chat_sequence,
            "game_turn_count": self.game_turn_count,
            "game_memory_state": {
                "cursor": self.deps.stop_hooks_state.last_game_memory_message_uuid,
                "message_count": self.deps.stop_hooks_state.last_game_memory_message_count,
                "extract_count": self.deps.stop_hooks_state.game_memory_extract_count,
                "turns_since": self.deps.stop_hooks_state.game_turns_since_last_extraction,
                "force_pending": self._memory_force_pending,
            },
            "missing_act_recoveries": getattr(self.deps, "_game_missing_act_total", 0),
            "missing_act_attempts": getattr(self.deps, "_game_missing_act_this_turn", 0),
            "missing_act_decision_id": getattr(self.deps, "_game_missing_act_decision_id", None),
            "model_recovery": copy.deepcopy(getattr(self.deps, "_game_model_recovery", None)),
            "active_decision_identity": copy.deepcopy(
                getattr(self.deps, "_game_active_decision_identity", None),
            ),
            "convergence_delivery_keys": sorted(
                getattr(self.deps, "_game_convergence_delivery_keys", set()),
            ),
            "delivered_attachment_ids": sorted(
                getattr(self.deps, "_game_delivered_attachment_ids", set()),
            ),
            "active_semantic_v2_lifecycle": copy.deepcopy(
                self.tool_ctx.get("_semantic_lifecycle_v2")
            ),
            "active_semantic_v2_lifecycle_pending_confirmation": bool(
                self.tool_ctx.get("_semantic_lifecycle_pending_confirmation", False)
            ),
            "semantic_latest_check_route_count": int(
                self.tool_ctx.get("_semantic_latest_check_route_count", 0) or 0
            ),
            "semantic_latest_ready_route_count": int(
                self.tool_ctx.get("_semantic_latest_ready_route_count", 0) or 0
            ),
            "attempt": {
                "identity": copy.deepcopy(self._attempt_identity),
                "generation": self._attempt_generation,
                "ordinal": self._attempt_ordinal,
                "status": self._attempt_status,
                "reason": self._attempt_reason,
                "promptDelivered": self._attempt_prompt_delivered,
                "retryCount": self._attempt_retry_count,
                "manualRetry": (
                    {
                        "epoch": self._manual_retry_epoch,
                        "identity": copy.deepcopy(self._manual_retry_identity),
                        "status": self._manual_retry_status,
                    }
                    if self._manual_retry_status in {"AUTHORIZED", "CONSUMED"}
                    and isinstance(self._manual_retry_identity, dict)
                    else None
                ),
                "slot": {
                    "slotId": self._active_provider_slot.id,
                    "provider": self._active_provider_slot.provider,
                    "model": self._active_provider_slot.reference,
                },
            },
        }

    def persist(self) -> None:
        self.runner.persist()
        self.store.write_agent_state(self.pid, self.serialize())

    def _consume_convergence_delivery(self, metadata: dict) -> None:
        """Record only runtime-issued, redacted delivery metadata."""
        if not isinstance(metadata, dict):
            return
        attachment_id = metadata.get("id")
        decision_id = metadata.get("decision_id")
        stage = metadata.get("stage")
        if not all(isinstance(value, str) and value for value in (
            attachment_id, decision_id, stage,
        )):
            return
        if stage != "thread_attachment":
            return
        self._record_convergence_delivery(
            attachment_id=attachment_id,
            decision_id=decision_id,
            stage=stage,
        )

    def _record_convergence_delivery(
        self,
        *,
        attachment_id: str,
        decision_id: str,
        stage: str,
    ) -> None:
        """Persist redacted convergence-delivery facts with bounded deduping."""
        delivery_keys = getattr(
            self.deps, "_game_convergence_delivery_keys", None,
        )
        if not isinstance(delivery_keys, set):
            delivery_keys = set()
            self.deps._game_convergence_delivery_keys = delivery_keys
        active_identity = getattr(
            self.deps, "_game_active_decision_identity", None,
        )
        identity_key = (
            json.dumps(
                active_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if isinstance(active_identity, dict)
            else decision_id
        )
        key = f"{identity_key}|{stage}|{attachment_id}"
        if key in delivery_keys:
            return
        delivery_keys.add(key)
        event = {
            "type": "game_action_convergence_delivery",
            "id": attachment_id,
            "gameId": self.game_id,
            "decisionId": decision_id,
            "seat": self.pid,
            "stage": stage,
        }
        self.store.log_event(event)
        _emit(self.event_callback, event)
        self.store.write_agent_state(self.pid, self.serialize())

    async def _await_pending_drain(self) -> None:
        pending = self._pending_drain
        current = asyncio.current_task()
        if pending is None or pending is current:
            return
        await asyncio.gather(pending, return_exceptions=True)
        if self._pending_drain is pending:
            self._pending_drain = None

    def _attempt_handlers(self, lease: GameAttemptLease) -> dict[str, Callable[[dict], Any]]:
        """Capture one lease in every handler closure used by a query."""
        token = lease.token

        def is_open() -> bool:
            return lease.is_open(token)

        self.tool_ctx["_attempt_guard"] = is_open
        guarded: dict[str, Callable[[dict], Any]] = {}
        base_handlers = getattr(self, "_base_handlers", self.runner.config.handlers)
        for name, handler in base_handlers.items():
            def invoke(
                args: dict,
                _handler: Callable[[dict], Any] = handler,
                _is_open: Callable[[], bool] = is_open,
            ) -> Any:
                if not _is_open():
                    return _stale_attempt_result()
                result = _handler(args)
                if not inspect.isawaitable(result):
                    return result if _is_open() else _stale_attempt_result()

                async def await_result() -> Any:
                    try:
                        value = await result
                    except AttemptClosedError:
                        return _stale_attempt_result()
                    if not _is_open():
                        return _stale_attempt_result()
                    return value

                return await_result()

            guarded[name] = invoke
        self.runner.config.handlers = guarded
        return guarded

    def _attempt_event_callback(
        self, lease: GameAttemptLease,
    ) -> Callable[[Any], Any]:
        token = lease.token

        def callback(event: Any) -> Any:
            if not lease.is_open(token):
                return None
            return self._handle_runner_event(event)

        return callback

    async def _watch_attempt_deadline(self, lease: GameAttemptLease) -> None:
        if self.api_turn_timeout_seconds is None:
            return
        try:
            await asyncio.sleep(max(0.0, float(self.api_turn_timeout_seconds)))
        except asyncio.CancelledError:
            return
        if not lease.is_open(lease.token):
            return
        lease.close("decision_deadline")
        self._attempt_reason = "decision_deadline"
        runner_attempt = self.runner.active_attempt
        if runner_attempt is not None:
            runner_attempt.abort("decision_deadline")
        runner_submit_task = self._runner_submit_task
        current = asyncio.current_task()
        if (
            runner_submit_task is not None
            and runner_submit_task is not current
            and not runner_submit_task.done()
        ):
            runner_submit_task.cancel()
            await asyncio.gather(runner_submit_task, return_exceptions=True)

    async def cancel_current_attempt(self, reason: str = "cancelled") -> None:
        """Fence and drain the current DecisionFrame without stopping the agent."""
        reason = _normalize_attempt_reason(reason)
        lease = self._active_attempt
        self._attempt_reason = reason
        if lease is not None:
            lease.close(reason)
        runner_attempt = self.runner.active_attempt
        if runner_attempt is not None:
            runner_attempt.abort(reason)
        await self.runner.cancel_current_attempt(reason, handle=runner_attempt)
        active_task = self._active_task
        current = asyncio.current_task()
        if active_task is not None and active_task is not current and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
        if lease is not None:
            lease.mark_closed()
            if self._active_attempt is lease:
                self._active_attempt = None
            self._attempt_status = "CLOSED"
            self._attempt_reason = lease.reason or self._attempt_reason
        if self._deadline_task is not None and self._deadline_task is not current:
            self._deadline_task.cancel()
            await asyncio.gather(self._deadline_task, return_exceptions=True)
            self._deadline_task = None

    def schedule_cancel_current_attempt(self, reason: str = "cancelled") -> None:
        """Synchronously fence stale work, then schedule its owned drain."""
        reason = _normalize_attempt_reason(reason)
        lease = self._active_attempt
        self._attempt_reason = reason
        controller_task = self._active_task
        runner_attempt = self.runner.active_attempt
        deadline_task = self._deadline_task
        if lease is not None:
            lease.close(reason)
        if runner_attempt is not None:
            runner_attempt.abort(reason)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        pending = self._pending_drain
        if pending is not None and not pending.done():
            return
        runner_submit_task = self._runner_submit_task
        task = loop.create_task(self._drain_closed_attempt(
            lease,
            controller_task,
            runner_attempt,
            runner_submit_task,
            deadline_task,
            reason,
        ))
        self._pending_drain = task

        def clear(done: asyncio.Task) -> None:
            if self._pending_drain is done:
                self._pending_drain = None

        task.add_done_callback(clear)

    async def _drain_closed_attempt(
        self,
        lease: GameAttemptLease | None,
        controller_task: asyncio.Task | None,
        runner_attempt: Any,
        runner_submit_task: asyncio.Task | None,
        deadline_task: asyncio.Task | None,
        reason: str,
    ) -> None:
        """Drain exactly the tasks captured when a stale snapshot fenced them."""
        if runner_attempt is not None:
            await self.runner.cancel_current_attempt(
                reason,
                handle=runner_attempt,
            )
        elif runner_submit_task is not None and runner_submit_task is not asyncio.current_task():
            runner_submit_task.cancel()
            await asyncio.gather(runner_submit_task, return_exceptions=True)
        current = asyncio.current_task()
        if controller_task is not None and controller_task is not current and not controller_task.done():
            controller_task.cancel()
            await asyncio.gather(controller_task, return_exceptions=True)
        if deadline_task is not None and deadline_task is not current and not deadline_task.done():
            deadline_task.cancel()
            await asyncio.gather(deadline_task, return_exceptions=True)
        if lease is not None:
            lease.mark_closed()
            if self._active_attempt is lease:
                self._active_attempt = None
            self._attempt_status = "CLOSED"
            self._attempt_reason = lease.reason or self._attempt_reason

    async def _submit_runner(
        self,
        prompt: TurnInput | str,
        attachments: list[dict],
        *,
        prompt_delivery: Callable[[], Any] | None = None,
        provider_slot: ProviderSlot | None = None,
    ):
        """Submit one DecisionFrame through the shared query loop."""
        lease = self._active_attempt
        if lease is None:
            raise GameInvariantError("missing active game attempt")
        handlers = self._attempt_handlers(lease)
        event_callback = self._attempt_event_callback(lease)
        task = asyncio.create_task(
            self.runner.submit(
                prompt,
                attachments=attachments,
                handlers=handlers,
                event_callback=event_callback,
                attempt_guard=lambda: lease.is_open(lease.token),
                prompt_delivery=prompt_delivery,
                provider_slot=provider_slot,
            ),
            name=f"bg-submit-{self.game_id}-p{self.pid}-{lease.token.generation}",
        )
        self._runner_submit_task = task
        try:
            return await task
        finally:
            if self._runner_submit_task is task:
                self._runner_submit_task = None

    def _model_work_timeout_seconds(self) -> float | None:
        # Reserve part of the existing decision lease for draining the runner
        # and committing recovery. User cancellation still closes that lease.
        return max(0.001, self.api_turn_timeout_seconds * 0.8) if self.host_fallback_enabled and self.api_turn_timeout_seconds is not None else None

    def invoke_game_skill(self, args: dict) -> str:
        """Load one guide only from this controller's immutable bundle."""
        from bglab.games.skills.loader import invoke_game_skill

        return invoke_game_skill(self.skill_bundle, args)

    def _activate_game_skill(
        self,
        args: dict,
        skill_name: str,
        *,
        event_type: str,
        phase: str | None = None,
    ) -> str:
        result = self.invoke_game_skill(args)
        if (
            skill_name
            and not result.startswith("Error")
            and "not found" not in result.lower()
        ):
            if skill_name not in self.loaded_skills:
                self.loaded_skills.append(skill_name)
            self.loaded_skill_phases[skill_name] = str(
                phase or self.tool_ctx.get("_strategy_phase", "unknown")
            )
            self._sync_game_skill_relink_bodies()
            self.store.log_event({
                "type": event_type,
                "game_id": self.game_id,
                "pid": self.pid,
                "skill": skill_name,
                "turn_id": self.last_received_turn_id,
            })
            self.persist()
        return result

    def _sync_game_skill_relink_bodies(self) -> None:
        """Retain invoked reference methods, including their applicability."""
        if not self.skills_enabled:
            self._game_skill_bodies = ""
            return
        from bglab.games.skills.loader import get_bundle_invoked_skill_bodies

        self._game_skill_bodies = get_bundle_invoked_skill_bodies(
            self.skill_bundle,
            self.loaded_skills,
            seat=self.pid,
        )

    def confirm_action_snapshot(self, state: dict) -> None:
        """Expire bindings after durable confirmation, retaining turn numbering."""
        try:
            confirmed = SubmissionState(self.tool_ctx).confirm_snapshot(authority_hash(state))
        except ValueError as exc:
            raise GameInvariantError(str(exc)) from exc
        if confirmed:
            self.persist()

    def enqueue_chat(self, sender_pid: int | str, message: str, message_id: int | None = None) -> None:
        if message_id is not None and any(item['id'] == message_id for item in self.pending_chat):
            return
        self._chat_sequence = max(self._chat_sequence + 1, message_id or 0)
        self.pending_chat.append({
            "id": message_id if message_id is not None else self._chat_sequence,
            "from": sender_pid,
            "message": message,
        })
        self.persist()

    def acknowledge_chat(self, message_id: int) -> None:
        remaining = [item for item in self.pending_chat if item['id'] != message_id]
        if len(remaining) != len(self.pending_chat):
            self.pending_chat = remaining
            self.persist()

    def _handle_runner_event(self, event: Any) -> None:
        event_type = getattr(getattr(event, "type", None), "value", "")
        if event_type == "tool_use_end" and event.tool_use is not None:
            tool_name = event.tool_use.name
            tool_input = copy.deepcopy(getattr(event.tool_use, "input", {}))
            check_input = {"redacted": True} if tool_name == "BgChat" else tool_input
            record = {"tool": tool_name, "input": check_input}
            self._active_tools[event.tool_use.id] = record
            self.tool_ctx.get("_tool_trace", []).append({"tool": tool_name, "input": check_input})
            tool_event = {
                "type": "tool_called", "game_id": self.game_id,
                "turn_id": self.last_received_turn_id, "pid": self.pid, **record,
            }
            _best_effort_store_telemetry(
                self.store, self.game_id, "log_event", tool_event,
            )
            _emit(self.event_callback, tool_event)
            return
        if event_type != "tool_result" or event.tool_result is None:
            return
        record = self._active_tools.pop(event.tool_result.tool_use_id, {"tool": "unknown-tool"})
        tool_name = record["tool"]
        semantic_ok = not event.tool_result.is_error
        content = str(getattr(event.tool_result, "content", ""))
        result_metadata = dict(getattr(event.tool_result, "metadata", {}) or {})
        if tool_name == "BgChat":
            payload: Any = {"redacted": True}
        else:
            try:
                payload = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = content[:4_000]
        if tool_name == "BgAct":
            error_code = result_metadata.get("public_code")
            if result_metadata.get("outcome_kind") == "infrastructure_failure":
                diagnostic = str(error_code or "BG_ACT_INFRASTRUCTURE_FAILURE")
                if not getattr(self.deps, "_game_invariant_error", ""):
                    self.deps._game_invariant_error = diagnostic[:500]
                # query_loop checks this shared AbortEvent immediately after
                # yielding the tool result, so it returns ABORTED_TOOLS and
                # skips MAX_TURNS recovery/provider retry.
                abort_event = getattr(self.runner, "_abort_event", None)
                if abort_event is not None:
                    abort_event.set("tool_infrastructure_failure")
        if (
            tool_name == "BgAct"
            and result_metadata.get("outcome_kind") in {
                "convergence_correction",
                "repair_correction",
            }
        ):
            semantic_ok = True
        elif semantic_ok and tool_name == "BgAct":
            operation = (
                record.get("input", {}).get("operation")
                if isinstance(record.get("input"), dict)
                else None
            )
            semantic_ok = _bgact_result_succeeded(operation, result_metadata)
        result_event = {
            "type": "tool_result",
            "game_id": self.game_id,
            "turn_id": self.last_received_turn_id,
            "pid": self.pid,
            "tool": tool_name,
            "ok": semantic_ok,
            "result": payload,
            "outcome_kind": result_metadata.get("outcome_kind"),
            "state_changed": result_metadata.get("state_changed"),
        }
        if (
            tool_name == "BgAct"
            and result_metadata.get("outcome_kind") == "check_complete"
        ):
            checked_routes = result_metadata.get("checkedRoutes")
            checked_route_aliases = result_metadata.get("checkedRouteAliases")
            checked_route_ids = result_metadata.get("checkedRouteIds")
            if isinstance(checked_routes, list):
                result_event["checkedRoutes"] = copy.deepcopy(checked_routes)
            if isinstance(checked_route_aliases, dict):
                result_event["checkedRouteAliases"] = copy.deepcopy(
                    checked_route_aliases,
                )
            if isinstance(checked_route_ids, dict):
                result_event["checkedRouteIds"] = copy.deepcopy(
                    checked_route_ids,
                )
        self.tool_ctx.get("_tool_trace", []).append({"tool": tool_name, "ok": semantic_ok})
        _best_effort_store_telemetry(
            self.store, self.game_id, "log_event", result_event,
        )
        _emit(self.event_callback, result_event)

    def start_turn(
        self,
        turn_id: str,
        state: dict,
        legal_actions: list[dict] | None = None,
        *,
        authority_state: dict | None = None,
        authority_frame: ConfirmedAuthorityFrame | None = None,
        retry_instruction: str | None = None,
        provider_slot: ProviderSlot | None = None,
        _promoted: bool = False,
    ) -> TurnHandle:
        existing = self._handles.get(turn_id)
        if existing is not None:
            if retry_instruction is None:
                return existing
            # A bounded retry gets a fresh generation after the prior handle
            # has fully drained.  Reusing the closed handle would immediately
            # surface its CancelledError and spin the session retry loop.
            if not existing.completion.done():
                raise GameInvariantError(
                    "cannot retry a DecisionFrame before its prior handle drains"
                )
            if not existing.action.done():
                # The drained controller task may have been cancelled while it
                # was fencing the lease, before it could publish its terminal
                # action exception.  No caller awaits this stale action after a
                # retry, so cancel it explicitly before replacing the handle.
                existing.action.cancel()
            self._handles.pop(turn_id, None)
        if turn_id in self.committed_actions:
            loop = asyncio.get_running_loop()
            action_future = loop.create_future()
            action_future.set_result(copy.deepcopy(self.committed_actions[turn_id]))

            async def replay_committed() -> dict:
                return {"action": action_future.result(), "report": ""}

            task = asyncio.create_task(replay_committed())
            handle = TurnHandle(turn_id=turn_id, action=action_future, completion=task)
            self._handles[turn_id] = handle
            return handle
        incoming_number = _turn_number(turn_id)
        committed_number = _turn_number(self.last_committed_turn_id)
        if (incoming_number is not None and committed_number is not None
                and incoming_number <= committed_number):
            loop = asyncio.get_running_loop()
            action_future = loop.create_future()
            action_future.set_exception(GameInvariantError(
                f"stale turnId {turn_id}; last committed is {self.last_committed_turn_id}"
            ))

            async def reject_stale() -> dict:
                return {"action": None, "report": "stale turn rejected"}

            handle = TurnHandle(
                turn_id=turn_id, action=action_future,
                completion=asyncio.create_task(reject_stale()),
            )
            self._handles[turn_id] = handle
            return handle
        prior_task = self._active_task
        if (
            not _promoted
            and retry_instruction is None
            and prior_task is not None
            and not prior_task.done()
            and self._attempt_status == "COMMITTED"
        ):
            return self._schedule_confirmed_turn(
                turn_id,
                state,
                legal_actions,
                authority_state=authority_state,
                authority_frame=authority_frame,
                provider_slot=provider_slot,
                prior_completion=prior_task,
            )
        # _run_turn serialises on _lock. A same-player turn arriving while the
        # prior post-action report is finishing therefore queues instead of
        # failing; the frontend remains free after the prior BgAct commit.
        loop = asyncio.get_running_loop()
        action_future = loop.create_future()
        replay_state = copy.deepcopy(
            authority_state if authority_state is not None else state,
        )
        # Model hydration is presentation-only and may add derived fields.
        # Attempt/retry identity stays bound to the authority replay state.
        identity = self._attempt_identity_for(turn_id, replay_state)
        if authority_frame is not None:
            identity["stateHash"] = authority_frame.authority_hash
        same_identity = self._attempt_identity == identity
        if (
            self._manual_retry_status == "AUTHORIZED"
            and self._manual_retry_identity != identity
        ):
            raise GameAPIError("manual retry DecisionFrame identity mismatch")
        manual_retry = (
            retry_instruction is None
            and same_identity
            and self._manual_retry_status == "AUTHORIZED"
            and self._manual_retry_identity == identity
        )
        if not same_identity:
            self._attempt_identity = copy.deepcopy(identity)
            self._attempt_ordinal = 0
            self._attempt_retry_count = 0
            self._attempt_status = "IDLE"
            self._attempt_reason = ""
            self._attempt_prompt_delivered = False
            if self._manual_retry_status == "AUTHORIZED":
                self._manual_retry_status = "CONSUMED"
        if (
            retry_instruction is None
            and same_identity
            and self._attempt_retry_count >= 1
            and not manual_retry
        ):
            # A persisted retry budget is consumed even if the process stopped
            # between generations.  Never create a free provider attempt after
            # restore; surface one terminal API error through the normal
            # session pause path instead.
            error = GameAPIError("AI retry budget exhausted for this DecisionFrame")
            action_future.set_exception(error)
            _retrieve_future_exception(action_future)

            async def exhausted_attempt() -> dict:
                return {"action": None, "report": "retry budget exhausted"}

            handle = TurnHandle(
                turn_id=turn_id,
                action=action_future,
                completion=asyncio.create_task(exhausted_attempt()),
            )
            self._handles[turn_id] = handle
            return handle
        if manual_retry:
            retry_instruction = self.manual_retry_instruction()
            # Consume durably before any new provider submit. This manual cycle
            # intentionally does not replenish the automatic outer retry.
            self._manual_retry_status = "CONSUMED"
            self.deps._game_model_recovery = restore_recovery(None)
            self.store.update_manifest(
                manual_retry_epoch=self._manual_retry_epoch,
                manual_retry_status="consumed",
            )
        elif retry_instruction is not None:
            if not self.retry_available(turn_id, replay_state):
                raise GameAPIError("AI retry budget exhausted for this DecisionFrame")
            self._attempt_retry_count = 1
        self._attempt_generation += 1
        self._attempt_ordinal += 1
        self._attempt_status = "OPEN"
        self._attempt_reason = ""
        self._active_provider_slot = provider_slot or self.provider_slots.primary
        state_hash = identity["stateHash"]
        prior_lease = self._active_attempt
        if prior_lease is not None and prior_lease.status == "OPEN":
            self.schedule_cancel_current_attempt("superseded")
        lease = GameAttemptLease(GameAttemptToken(
            game_id=self.game_id,
            decision_id=turn_id,
            seat=self.pid,
            state_hash=state_hash,
            generation=self._attempt_generation,
        ))
        self._active_attempt = lease
        # A tool-infrastructure diagnostic belongs to one DecisionFrame only;
        # a later generation must not inherit it and pause spuriously.
        self.deps._game_invariant_error = ""
        # Persist safe attempt identity/budget before any provider submit.
        self.persist()
        task = asyncio.create_task(
            self._run_turn(
                turn_id, state, replay_state, action_future, lease,
                authority_frame=authority_frame,
                retry_instruction=retry_instruction,
                provider_slot=self._active_provider_slot,
            ),
            name=f"bg-{self.game_id}-p{self.pid}-{turn_id}",
        )
        def surface_turn_failure(completed: asyncio.Task) -> None:
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None and not action_future.done():
                action_future.set_exception(error)
            _retrieve_future_exception(action_future)

        def close_lease(completed: asyncio.Task) -> None:
            if self._active_attempt is lease:
                lease.mark_closed()
                self._active_attempt = None
            if self._attempt_status == "OPEN":
                self._attempt_status = "CLOSED"
            if self._active_task is completed:
                self._active_task = None

        task.add_done_callback(surface_turn_failure)
        task.add_done_callback(close_lease)
        self._active_task = task
        handle = TurnHandle(turn_id=turn_id, action=action_future, completion=task)
        self._handles[turn_id] = handle
        return handle

    def _schedule_confirmed_turn(
        self,
        turn_id: str,
        state: dict,
        legal_actions: list[dict] | None,
        *,
        authority_state: dict | None,
        authority_frame: ConfirmedAuthorityFrame | None,
        provider_slot: ProviderSlot | None,
        prior_completion: asyncio.Task,
    ) -> TurnHandle:
        """Schedule one immutable same-seat frame behind post-action work.

        The new frame is never exposed to the older query.  Only a short
        lifecycle notice becomes eligible at the existing post-tool
        attachment boundary.  Provider delivery timing starts after
        promotion, so a slow battle report cannot consume the next decision's
        API budget.
        """
        queued = self._scheduled_turn_handle
        if queued is not None and not queued.completion.done():
            raise GameInvariantError(
                "same-seat turn scheduler already contains a pending turn",
            )
        loop = asyncio.get_running_loop()
        action_future = loop.create_future()
        started_future = loop.create_future()
        queued_state = copy.deepcopy(state)
        queued_legal = copy.deepcopy(legal_actions)
        queued_authority_state = copy.deepcopy(authority_state)
        notice = {
            "decisionId": turn_id,
            "queuedAfter": self.last_committed_turn_id or "",
        }
        self.deps._game_pending_decision_notice = notice
        handle: TurnHandle

        async def promote() -> dict:
            inner: TurnHandle | None = None
            try:
                # asyncio.wait does not propagate cancellation from this
                # mailbox task into the older post-action task.
                await asyncio.wait({prior_completion})
                if self._stopping:
                    raise asyncio.CancelledError
                if getattr(self.deps, "_game_pending_decision_notice", None) == notice:
                    self.deps._game_pending_decision_notice = None
                self._handles.pop(turn_id, None)
                inner = self.start_turn(
                    turn_id,
                    queued_state,
                    queued_legal,
                    authority_state=queued_authority_state,
                    authority_frame=authority_frame,
                    provider_slot=provider_slot,
                    _promoted=True,
                )
                if not started_future.done():
                    started_future.set_result(None)
                done, _ = await asyncio.wait(
                    {inner.action, inner.completion},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if inner.action in done:
                    committed = inner.action.result()
                    if not action_future.done():
                        action_future.set_result(committed)
                else:
                    inner.completion.result()
                    raise GameInvariantError(
                        "queued AI turn completed without a valid BgAct",
                    )
                result = await inner.completion
                if not action_future.done():
                    action_future.set_result(inner.action.result())
                return result
            except asyncio.CancelledError:
                if not started_future.done():
                    started_future.cancel()
                if not action_future.done():
                    action_future.cancel()
                raise
            except BaseException as exc:
                if not started_future.done():
                    started_future.set_exception(exc)
                    _retrieve_future_exception(started_future)
                if not action_future.done():
                    action_future.set_exception(exc)
                    _retrieve_future_exception(action_future)
                raise
            finally:
                if getattr(self.deps, "_game_pending_decision_notice", None) == notice:
                    self.deps._game_pending_decision_notice = None
                if self._scheduled_turn_handle is handle:
                    self._scheduled_turn_handle = None

        completion = asyncio.create_task(
            promote(),
            name=f"bg-turn-scheduler-{self.game_id}-p{self.pid}-{turn_id}",
        )
        handle = TurnHandle(
            turn_id=turn_id,
            action=action_future,
            completion=completion,
            started=started_future,
        )
        self._scheduled_turn_handle = handle
        self._handles[turn_id] = handle
        return handle

    async def _run_turn(
        self,
        turn_id: str,
        state: dict,
        replay_state: dict,
        action_future: asyncio.Future,
        lease: GameAttemptLease,
        *,
        authority_frame: ConfirmedAuthorityFrame | None = None,
        retry_instruction: str | None = None,
        provider_slot: ProviderSlot | None = None,
    ) -> dict:
        await self._await_pending_drain()
        if not lease.is_open(lease.token):
            raise asyncio.CancelledError
        if authority_frame is not None:
            current_record = self.store.read_snapshot()
            if not _authority_frame_is_current(self.store, authority_frame):
                expected_turn_id = (
                    current_record.get("turn_id")
                    if isinstance(current_record, dict)
                    and isinstance(current_record.get("turn_id"), str)
                    else authority_frame.turn_id
                )
                raise AuthorityBindingError(
                    "confirmed authority changed before provider dispatch",
                    expected_turn_id=expected_turn_id,
                )
        async with self._lock:
            attempt_token = lease.token
            resuming_same_turn = turn_id == self.last_received_turn_id

            def attempt_is_open() -> bool:
                return lease.is_open(attempt_token)

            def stale_payload() -> dict[str, Any]:
                return {
                    "status": "invalid",
                    "stateChanged": False,
                    "error": {
                        "code": "STALE_ATTEMPT_REJECTED",
                        "message": "This DecisionFrame attempt is no longer active.",
                    },
                }

            def guarded_validator(callback: Callable[..., Any], *args: Any) -> Any:
                if not attempt_is_open():
                    return stale_payload()
                result = callback(*args)
                if not inspect.isawaitable(result):
                    return result if attempt_is_open() else stale_payload()

                async def await_result() -> Any:
                    result_value = await result
                    return result_value if attempt_is_open() else stale_payload()

                return await_result()

            if turn_id != self.last_received_turn_id:
                self.game_turn_count += 1
            self.deps._game_turn = self.game_turn_count
            self.phase = "THINKING"
            self.last_received_turn_id = turn_id
            # The lease token was built from the host authority state before
            # model hydration. Presentation fields must never redefine the
            # semantic-v2 authority identity inside the query.
            state_hash = attempt_token.state_hash
            snapshot_revision = state.get("v")
            if (
                isinstance(snapshot_revision, bool)
                or not isinstance(snapshot_revision, (int, str))
                or snapshot_revision == ""
            ):
                snapshot_revision = state.get("schemaVersion", 0)
            identity = {
                "gameId": self.game_id, "decisionId": turn_id, "seat": self.pid,
                "stateHash": state_hash, "snapshotRevision": snapshot_revision,
                "rulesVersion": self.definition.snapshot_version,
                "adapterVersion": self.definition.action_profile,
            }
            restored_semantic = self.tool_ctx.pop("_restored_semantic_v2_lifecycle", None)
            if restored_semantic is None:
                restored_semantic = copy.deepcopy(self.tool_ctx.get("_semantic_lifecycle_v2"))
            restored_semantic_pending = bool(
                self.tool_ctx.get(
                    "_semantic_lifecycle_pending_confirmation", False,
                )
            )
            stored_latest_route_count = (
                max(
                    0,
                    int(self.tool_ctx.get("_semantic_latest_check_route_count", 0) or 0),
                )
                if restored_semantic_pending
                else 0
            )
            stored_latest_ready_count = (
                max(
                    0,
                    int(self.tool_ctx.get("_semantic_latest_ready_route_count", 0) or 0),
                )
                if restored_semantic_pending
                else 0
            )
            self.tool_ctx.update({
                "_game_id": self.game_id,
                "_decision_id": turn_id,
                "_state": state,
                "_authority_state_hash": state_hash,
                "_snapshot_revision": snapshot_revision,
                "_rules_version": self.definition.snapshot_version,
                "_adapter_version": self.definition.action_profile,
                "_act_submitted": False,
                "_chat_batch": [],
                "_chat_reply_requests": 0,
                "_committed_transaction": None,
                "_canonical_action": None,
                "_rejected_attempts": [],
                "_plan_dirty": False,
                "_tool_trace": [],
                "_semantic_lifecycle_v2": None,
                "_semantic_lifecycle_pending_confirmation": restored_semantic_pending,
                "_semantic_commit_fence": None,
                "_semantic_argument_normalizations": [],
                "_semantic_latest_check_route_count": stored_latest_route_count,
                "_semantic_latest_ready_route_count": stored_latest_ready_count,
                "_semantic_reconciliation_required": False,
                "_semantic_reconciliation_error": "",
                "_attempt_guard": attempt_is_open,
            })

            if self.action_validator is not None:
                self.tool_ctx["_transaction_validator"] = (
                    lambda transaction: guarded_validator(
                        self.action_validator, turn_id, self.pid, transaction,
                    )
                )
            else:
                from bglab.games.transactions import validate_transaction
                self.tool_ctx["_transaction_validator"] = (
                    lambda transaction: guarded_validator(
                        validate_transaction, state, self.pid, transaction,
                    )
                )

            def commit_action(result: Any) -> None:
                if not attempt_is_open():
                    raise AttemptClosedError("STALE_ATTEMPT_REJECTED")
                if action_future.done():
                    raise GameRuntimeError("BgAct attempted to commit more than once")
                if self.store.read_pending_replay_turn() is not None:
                    raise GameInvariantError(
                        "previous committed replay turn has no confirmed after-state",
                    )
                public = result.to_public_dict() if hasattr(result, "to_public_dict") else dict(result)
                transaction = public["transaction"]
                canonical_action = public["canonicalAction"]
                fallback = self.host_fallbacks.get(turn_id)
                if self.tool_ctx.get("_host_fallback_dispatch") == turn_id:
                    if (not fallback or fallback.get("status") != "prepared"
                            or fallback.get("authorityHash") != self.tool_ctx.get("_authority_state_hash")
                            or fallback.get("inputSurfaceHash") != self.tool_ctx.get("_semantic_input_surface_hash")):
                        raise GameInvariantError("HOST_FALLBACK_ORIGIN_MISMATCH")
                    public["decisionSource"] = "host_fallback"
                    public["fallback"] = {key: fallback[key] for key in ("decisionId", "reason", "source")}
                elif fallback and fallback.get("status") == "prepared":
                    fallback["status"] = "superseded"
                replay_index = self.store.replay_turn_count()
                if not attempt_is_open():
                    raise AttemptClosedError("STALE_ATTEMPT_REJECTED")
                before_hash = self.store.write_replay_frame(
                    replay_index,
                    replay_state,
                )
                self.store.write_pending_replay_turn({
                    "schemaVersion": 1,
                    "index": replay_index,
                    "turnId": turn_id,
                    "seat": self.pid,
                    "transaction": copy.deepcopy(transaction),
                    "canonicalAction": copy.deepcopy(canonical_action),
                    "effects": copy.deepcopy(public.get("effects", [])),
                    "beforeFrame": replay_index,
                    "beforeHash": before_hash,
                    **({"decisionSource": "host_fallback", "fallback": copy.deepcopy(public["fallback"])}
                       if public.get("decisionSource") == "host_fallback" else {}),
                })
                public["turnGroupId"] = self._active_turn_group_id
                self.phase = "ACTION_COMMITTED"
                self.last_committed_turn_id = turn_id
                self.committed_actions[turn_id] = copy.deepcopy(public)
                if public.get("decisionSource") == "host_fallback":
                    self.host_fallbacks[turn_id]["status"] = "committed"
                end_action_plans(self.tool_ctx, public.get("boundaryReason"))
                if not attempt_is_open():
                    raise AttemptClosedError("STALE_ATTEMPT_REJECTED")
                self._attempt_status = "COMMITTED"
                self.persist()
                if not attempt_is_open():
                    raise AttemptClosedError("STALE_ATTEMPT_REJECTED")
                _best_effort_store_telemetry(self.store, self.game_id, "log_event", {
                    "type": "action_committed", "turn_id": turn_id,
                    "pid": self.pid, "transaction": transaction,
                    "canonical_action": canonical_action,
                    **({"decisionSource": "host_fallback", "fallback": copy.deepcopy(public["fallback"])}
                       if public.get("decisionSource") == "host_fallback" else {}),
                })
                _best_effort_store_telemetry(self.store, self.game_id, "log_turn_decision", {
                    "turn_id": turn_id,
                    "pid": self.pid,
                    "state": state,
                    "loaded_skills": list(self.loaded_skills),
                    "loaded_skill_phases": dict(self.loaded_skill_phases),
                    "strategy_phase": self.tool_ctx.get("_strategy_phase", "unknown"),
                    "plan": self.tool_ctx.get("_plan", ""),
                    "plan_phase": self.tool_ctx.get("_plan_phase", "unknown"),
                    "plans": visible_plans(self.tool_ctx),
                    **({"decisionSource": "host_fallback", "fallback": copy.deepcopy(public["fallback"])}
                       if public.get("decisionSource") == "host_fallback" else {}),
                    "transaction": copy.deepcopy(transaction),
                    "canonical_action": copy.deepcopy(canonical_action),
                    "effects": copy.deepcopy(public.get("effects", [])),
                    "outcome": copy.deepcopy(public.get("outcome", {})),
                    "rejected_attempts": copy.deepcopy(self.tool_ctx.get("_rejected_attempts", [])),
                })
                _emit(self.event_callback, {
                    "type": "action_committed", "game_id": self.game_id,
                    "turn_id": turn_id, "pid": self.pid,
                    "transaction": transaction, "canonical_action": canonical_action,
                    "outcome": copy.deepcopy(public.get("outcome", {})),
                    **({"decisionSource": "host_fallback", "fallback": copy.deepcopy(public["fallback"])}
                       if public.get("decisionSource") == "host_fallback" else {}),
                })
                action_future.set_result(public)
                # The committed action is already durable. From this point the
                # frontend is free and this worker is independently finishing.
                self.phase = "POST_ACTION"

            self.tool_ctx["_action_sink"] = commit_action
            frame_state = copy.deepcopy(state)
            # BgObserve and BgAct must read the same actor-authorized facts
            # that are serialized into the provider's DecisionFrame.
            self.tool_ctx["_state"] = frame_state
            frame = self._decision_frame(turn_id, frame_state)
            self._last_decision_frame = copy.deepcopy(frame)
            self.tool_ctx["_decision_frame"] = copy.deepcopy(frame)
            decision_id = frame["decisionId"]
            self.tool_ctx["_decision_id"] = decision_id
            self.tool_ctx["_turn_group_id"] = frame.get("turnGroupId")
            reconcile_turn_plan(self.tool_ctx, frame["turnGroupId"])
            from bglab.games.authority_worker import AuthorityWorker
            from bglab.games.semantic_validation import freeze_semantic_surface
            from bglab.games.request_surface import build_game_request_context
            from bglab.games.tools.semantic_act_v2 import (
                SEMANTIC_V2_RETRIEVAL_SURFACE_HASH,
            )
            from bglab.games.tools.semantic_lifecycle import (
                restore_turn_semantic_lifecycle,
                semantic_identity_from_ctx,
                serialize_semantic_lifecycle,
            )

            act_tool = next(tool for tool in self.tools if tool.name == "BgAct")
            surface = freeze_semantic_surface(
                input_version="semantic-input/v2",
                prompt=self.system_prompt,
                frame=frame,
                tool_description=act_tool.prompt,
                schema=act_tool.parameters,
                request_context=build_game_request_context(
                    definition=self.definition,
                    feature_profile=self.profile,
                    game_rules=build_game_session_head_text(
                        definition=self.definition,
                        seat=self.pid,
                    ),
                    tools=self.tools,
                    thinking_policy=(
                        self.deps.query_profile.budget_thinking
                    ),
                    query_profile=self.deps.query_profile,
                ),
            )
            self.tool_ctx["_semantic_input_surface_hash"] = surface.composite_hash
            self.tool_ctx["_semantic_retrieval_surface_hash"] = (
                SEMANTIC_V2_RETRIEVAL_SURFACE_HASH
            )
            semantic_identity = semantic_identity_from_ctx(self.tool_ctx)
            valid_semantic_v2 = (
                restore_turn_semantic_lifecycle(restored_semantic, semantic_identity)
                if semantic_identity is not None
                else None
            )
            self.tool_ctx["_semantic_lifecycle_v2"] = (
                serialize_semantic_lifecycle(valid_semantic_v2)
                if valid_semantic_v2 is not None
                else None
            )
            semantic_snapshot = copy.deepcopy(replay_state)
            self.tool_ctx["_semantic_worker_factory"] = (
                lambda snapshot=semantic_snapshot, current_decision=decision_id: (
                    AuthorityWorker(
                        self.definition,
                        copy.deepcopy(snapshot),
                        decision_id=current_decision,
                    )
                )
            )
            # QueryDeps (and therefore its LoopDetector) is reused by this
            # teammate across DecisionFrames.  A new authoritative identity
            # starts a new detector window; ordinary query continuations and
            # stop-hook re-submits never pass through this boundary.
            active_identity = getattr(
                self.deps, "_game_active_decision_identity", None,
            )
            identity_changed = active_identity != identity
            if identity_changed:
                self.deps._game_model_recovery = restore_recovery(None)
                if self.deps.loop_detector is not None:
                    self.deps.loop_detector.reset()
                self.deps._game_convergence_delivery_keys = set()
                self.deps._game_loop_alert = None
            if getattr(self.deps, "_game_missing_act_decision_id", None) != decision_id:
                self.deps._game_missing_act_this_turn = 0
                self.deps._game_missing_act_decision_id = decision_id
            self.deps._game_active_decision_identity = copy.deepcopy(identity)
            self.deps._game_active_decision_id = decision_id
            self._active_turn_group_id = frame["turnGroupId"]
            prompt = _game_turn_input(
                frame,
                retry_instruction=retry_instruction,
                resuming_same_turn=resuming_same_turn,
            )
            self._game_relink_state = {
                "game_id": self.game_id,
                "pid": self.pid,
                "turn_id": turn_id,
                "last_committed_turn_id": self.last_committed_turn_id,
                "committed_action_ids": list(self.committed_actions),
                "plans": visible_plans(self.tool_ctx),
            }
            api_error_text = ""
            invariant_error_text = ""
            turn_usage = empty_usage_totals()
            request_record_start = len(getattr(self.deps, "_request_records", []))
            started = time.monotonic()

            try:
                if (
                    authority_frame is not None
                    and not _authority_frame_is_current(self.store, authority_frame)
                ):
                    raise AuthorityBindingError(
                        "confirmed authority changed before provider dispatch",
                        expected_turn_id=turn_id,
                    )
                self._deadline_task = asyncio.create_task(
                    self._watch_attempt_deadline(lease),
                    name=f"bg-deadline-{self.game_id}-p{self.pid}-{lease.token.generation}",
                )
                run_result = None
                try:
                    run_result = await asyncio.wait_for(self._submit_runner(
                        prompt,
                        [],
                        prompt_delivery=lambda: self._mark_prompt_delivered(lease),
                        provider_slot=provider_slot,
                    ), timeout=self._model_work_timeout_seconds())
                finally:
                    # The runner has drained. Preserve observations in memory
                    # even if cancellation prevented a result from returning.
                    # Closed attempts never persist or emit UI events here.
                    records = getattr(self.deps, "_request_records", [])[request_record_start:]
                    self.request_records.extend(
                        {"kind": "action", "turnId": turn_id, **record} for record in records
                    )
                    if run_result is None:
                        for record in records:
                            if "requestUsage" in record:
                                # Cancellation discards the runner's return
                                # value, not requests already made. Recollect
                                # only this drained submit, without late writes.
                                record_request_usage(self.usage_totals, record["requestUsage"])
                            elif record.get("kind") == "compaction":
                                record_request_usage(self.usage_totals, {
                                    "input_tokens": record.get("inputTokens"),
                                    "output_tokens": record.get("outputTokens"),
                                    "cache_hit_tokens": record.get("cacheReadTokens"),
                                    "cache_miss_tokens": record.get("cacheMissTokens"),
                                    "cache_details_supported": record.get("cacheDetailsSupported", False),
                                    "provider_attempts": record.get("providerAttempts", 0),
                                    "provider_timeouts": record.get("providerTimeouts", 0),
                                    "usage_missing_requests": int(not record.get("usageAvailable", False)),
                                }, purpose="compaction")
                merge_usage_totals(turn_usage, getattr(run_result, "usage", None))
                self._last_visible_attachments = copy.deepcopy(
                    getattr(self.deps, "_last_attachment_messages", []),
                )
                provider_failure = getattr(run_result, "provider_failure", None)
                if provider_failure is not None and not action_future.done():
                    await try_host_fallback(self, provider_failure)
                elif (not action_future.done()
                      and str(getattr(run_result, "terminal_reason", "")) in
                      {"blocking_limit", "prompt_too_long"}):
                    await try_host_fallback(self, "context_capacity")
                elif (not action_future.done()
                      and str(getattr(run_result, "terminal_reason", "")) in
                      {"completed", "max_turns", "stop_hook_prevented"}):
                    await try_host_fallback(self, "decision_exhausted")
                if provider_failure is not None and not action_future.done():
                    # This submit is finished even though the DecisionFrame will
                    # retry on another slot. Preserve its Provider attempts so
                    # failover cost is not silently reported as zero.
                    merge_usage_totals(self.usage_totals, turn_usage)
                    self._attempt_reason = "provider_failure"
                    raise ProviderFailureError(provider_failure)
                api_error_text = (
                    f"AI API timeout after {self.api_turn_timeout_seconds:g} seconds"
                    if lease.reason == "decision_deadline"
                    else "\n".join(run_result.errors)
                )
            except ProviderFailureError:
                raise
            except asyncio.CancelledError:
                if lease.reason == "decision_deadline":
                    deadline_task = self._deadline_task
                    if deadline_task is not None and deadline_task is not asyncio.current_task():
                        await asyncio.shield(deadline_task)
                    api_error_text = (
                        f"AI API timeout after {self.api_turn_timeout_seconds:g} seconds"
                    )
                    run_result = None
                else:
                    raise
            except asyncio.TimeoutError:
                api_error_text = (
                    f"AI API timeout after {self.api_turn_timeout_seconds:g} seconds"
                )
                run_result = None
                await try_host_fallback(self, "decision_exhausted")
            except AuthorityBindingError:
                raise
            except Exception as exc:
                invariant_error_text = str(exc)
                run_result = None

            deadline_task = self._deadline_task
            if deadline_task is not None and deadline_task is not asyncio.current_task():
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)
                if self._deadline_task is deadline_task:
                    self._deadline_task = None

            if not action_future.done():
                merge_usage_totals(self.usage_totals, turn_usage)
                invariant_diagnostic = (
                    getattr(self.deps, "_game_invariant_error", "") or invariant_error_text
                )
                diagnostic = api_error_text or invariant_diagnostic
                if (
                    not diagnostic
                    and self.tool_ctx.get("_semantic_reconciliation_required")
                ):
                    detail = str(
                        self.tool_ctx.get("_semantic_reconciliation_error", "")
                    ).strip()
                    diagnostic = (
                        "BgAct commit requires host reconciliation"
                        + (f": {detail}" if detail else "")
                    )
                terminal_reason = str(
                    getattr(run_result, "terminal_reason", "") if run_result is not None else ""
                ).strip()
                if not diagnostic and terminal_reason:
                    diagnostic = (
                        "AI turn ended before a valid BgAct "
                        f"(terminal_reason={terminal_reason})"
                    )
                error_cls = GameAPIError if api_error_text else GameInvariantError
                exc = error_cls(diagnostic or "AI turn ended without a valid BgAct")
                if not action_future.done():
                    action_future.set_exception(exc)
                _retrieve_future_exception(action_future)
                self.phase = "PAUSED_API_ERROR" if api_error_text else "PAUSED_INVARIANT"
                # A closed attempt no longer owns transcript or agent-state
                # persistence. The session/controller that confirmed the
                # drain owns pause metadata; an old task must not overwrite
                # the newer attempt's durable state.
                if lease.is_open(attempt_token):
                    self.persist()
                raise exc

            merge_usage_totals(self.usage_totals, turn_usage)
            action = action_future.result()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if action.get("boundaryReason") == "new_information":
                self._pending_turn_group_actions.append((turn_id, action, elapsed_ms))
                report = ""
            elif self._reporting_enabled:
                group_actions = [*self._pending_turn_group_actions, (turn_id, action, elapsed_ms)]
                self._pending_turn_group_actions.clear()
                self._schedule_turn_report(turn_id, state, action, elapsed_ms, group_actions=group_actions)
                report = ""
            elif self.profile.battle_reports:
                report = (
                    run_result.final_text.strip()
                    if run_result is not None else ""
                )
                if report:
                    self._write_turn_report(
                        turn_id, report, turn_usage, elapsed_ms,
                    )
            else:
                report = ""
            if self.profile.chat:
                from bglab.games.chat import finish_unanswered

                finish_unanswered(self.tool_ctx)
            self.phase = "IDLE"
            self.persist()
            return {"action": action, "report": report}

    def _write_turn_report(
        self,
        turn_id: str,
        report: str,
        usage: dict[str, Any],
        elapsed_ms: int,
        *,
        source: str = "player_final_text",
    ) -> None:
        self.store.log_turn_report({
            "turn_id": turn_id, "pid": self.pid, "report": report,
            "elapsed_ms": elapsed_ms, "plan": self.tool_ctx.get("_plan", ""),
            "usage": dict(usage), "source": source,
        })
        event = {
            "type": "turn_report", "game_id": self.game_id,
            "turn_id": turn_id, "pid": self.pid, "text": report,
            "source": source,
        }
        self.store.log_event(event)
        _emit(self.event_callback, event)

    def _schedule_turn_report(
        self,
        turn_id: str,
        state: dict,
        action: dict,
        elapsed_ms: int,
        *,
        group_actions: list[tuple[str, dict, int]] | None = None,
    ) -> None:
        """Fork a disposable factual reporter without delaying the next game turn."""
        if self._stopping:
            return
        action_summary = {
            "turn_group_id": action.get("turnGroupId"),
            "committed_decision_ids": [entry[0] for entry in (group_actions or [(turn_id, action, elapsed_ms)])],
            "canonical_action": action.get("canonicalAction"),
            "effects": action.get("effects", []),
            "outcome": action.get("outcome", {}),
            "tool_chain": [
                item for item in copy.deepcopy(self.tool_ctx.get("_tool_trace", []))
                if item.get("tool") != "BgPlan"
            ],
            "pre_action_scores": [
                player.get("score") for player in state.get("playerstorage", [])
                if isinstance(player, dict)
            ],
        }

        async def run_reporter() -> None:
            from bglab.utils.forked_agent import run_forked_agent

            result = await run_forked_agent(
                prompt=(
                    "Write one Chinese board-game battle report of at most 80 words from "
                    "the following committed facts. State only the action, authoritative outcome, "
                    "and concise tool names. Do not mention a future plan, target, intent, hidden "
                    "state, legal alternatives, or chain-of-thought. scoreDelta is this action's "
                    "points; scoreAfter is the total score after it. Never call scoreAfter points "
                    "earned.\n"
                    + json.dumps(action_summary, ensure_ascii=False)
                ),
                system_prompt=(
                    "You are a disposable board-game report worker. You have no tools and "
                    "may only restate supplied facts concisely."
                ),
                model=self.model,
                max_turns=1,
            )
            merge_usage_totals(self.usage_totals, result.usage)
            for record in getattr(result, "requests", []):
                self.request_records.append({"kind": "report", "turnId": turn_id, **record})
            text = result.text.strip()
            if text:
                self._write_turn_report(
                    turn_id, text, result.usage, elapsed_ms,
                    source="stop_hook_fork",
                )
            else:
                event = {
                    "type": "turn_report_failed", "game_id": self.game_id,
                    "turn_id": turn_id, "pid": self.pid,
                }
                self.store.log_event(event)
                _emit(self.event_callback, event)
            self.persist()

        task = asyncio.create_task(
            run_reporter(), name=f"bg-report-{self.game_id}-p{self.pid}-{turn_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _decision_frame(self, turn_id: str, state: dict) -> dict[str, Any]:
        """Project one seat-authorized semantic Frame without engine grammar."""
        adapter_view = state.get("adapterView") or {}
        if not isinstance(adapter_view, dict):
            adapter_view = {}
        wrapper = state.get("wrapper") or {}
        actor_seat = int(adapter_view.get("seat", self.pid))
        public_state = adapter_view.get("publicState")
        if not isinstance(public_state, dict):
            public_state = {}
        decision_surface = adapter_view.get("decisionSurface")
        if not isinstance(decision_surface, dict):
            decision_surface = {}
        private_state = adapter_view.get("privateState")
        decision_facts: dict[str, Any] = {}
        if not isinstance(private_state, dict) or actor_seat != self.pid:
            private_state = None
        else:
            private_state = copy.deepcopy(private_state)
            raw_decision_facts = private_state.pop("decisionFacts", None)
            if isinstance(raw_decision_facts, dict):
                decision_facts = raw_decision_facts
            scoring_facts = decision_facts.get("scoringDecisionFacts")
            if scoring_facts is not None:
                try:
                    validate_scoring_decision_facts(scoring_facts)
                except ValueError as error:
                    raise GameInvariantError(
                        f"invalid package scoringDecisionFacts: {error}",
                    ) from error
            if not private_state:
                private_state = None
        surface_scoring = decision_surface.get("scoringDecisionFacts")
        if surface_scoring is not None:
            try:
                validate_scoring_decision_facts(surface_scoring)
            except ValueError as error:
                raise GameInvariantError(
                    f"invalid decisionSurface scoring facts: {error}",
                ) from error
        scoring_facts = (
            copy.deepcopy(surface_scoring)
            if isinstance(surface_scoring, dict)
            else copy.deepcopy(decision_facts.get("scoringDecisionFacts") or {})
        )
        current_actions = GameTeammateController._package_current_actions(
            self.definition,
            decision_surface,
        )
        coverage = adapter_view.get("turnOutcomeSummary")
        if not isinstance(coverage, dict):
            coverage = {"coverageStatus": "not_explored", "enumerationComplete": False}
        turn = int(wrapper.get("turn", 0) or 0)
        current = int(wrapper.get("currentPlayer", actor_seat) or actor_seat)
        frame = {
            "schemaVersion": 2,
            "authority": "game-adapter",
            "decisionId": str(adapter_view.get("decisionId") or turn_id),
            "turnGroupId": str(adapter_view.get("turnGroupId") or f"turn:{turn}:seat:{current}"),
            "actorSeat": actor_seat,
            "publicState": GameTeammateController._package_frame_state(
                public_state,
                decision_surface,
            ),
            "seatPrivateState": GameTeammateController._strip_frame_internals(
                private_state,
            ),
            "currentActions": current_actions,
            "outcomeCoverage": GameTeammateController._strip_frame_internals(
                coverage,
            ),
            "scoringFacts": GameTeammateController._strip_frame_internals(
                scoring_facts,
            ),
            "informationBoundaries": copy.deepcopy(
                decision_surface.get("informationBoundaries")
                if isinstance(decision_surface.get("informationBoundaries"), list)
                else []
            ),
        }
        model_facts = decision_surface.get("modelFacts")
        if isinstance(model_facts, dict):
            frame["modelFacts"] = copy.deepcopy(model_facts)
        try:
            render_runtime_decision_frame(frame)
        except ValueError as error:
            raise GameInvariantError(f"invalid DecisionFrame: {error}") from error
        return frame

    @staticmethod
    def _package_current_actions(
        definition: GameDefinition,
        decision_surface: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from bglab.games.semantic_validation import load_semantic_descriptor

        descriptor = load_semantic_descriptor(definition)
        contract_names = set(descriptor.model_action_names)
        raw_actions = decision_surface.get("currentActions")
        if not isinstance(raw_actions, list) or not raw_actions:
            raise GameInvariantError(
                f"{definition.id} decisionSurface.currentActions is required",
            )
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, dict):
                raise GameInvariantError(
                    f"{definition.id} currentActions[{index}] must be an object",
                )
            action_name = str(raw.get("action", "")).strip()
            if action_name not in contract_names:
                raise GameInvariantError(
                    f"{definition.id} currentActions[{index}] is not package semantic action",
                )
            if action_name in seen:
                continue
            seen.add(action_name)
            entries.append(
                GameTeammateController._strip_frame_internals(raw),
            )
        return entries

    @staticmethod
    def _strip_frame_internals(value: Any) -> Any:
        forbidden = {
            "op", "effectid", "programid", "candidateid",
            "candidatetoken", "routequery", "enginesteps", "transaction",
        }
        if isinstance(value, dict):
            return {
                key: GameTeammateController._strip_frame_internals(child)
                for key, child in value.items()
                if "".join(
                    character
                    for character in str(key).lower()
                    if character.isalnum()
                ) not in forbidden
            }
        if isinstance(value, list):
            return [
                GameTeammateController._strip_frame_internals(child)
                for child in value
            ]
        return copy.deepcopy(value)

    @staticmethod
    def _public_frame_state(value: Any) -> Any:
        cleaned = GameTeammateController._strip_frame_internals(value)
        if isinstance(cleaned, dict):
            # Pending-effect internals are represented by currentActions and
            # informationBoundaries; raw effect queue IDs are not model state.
            cleaned.pop("pendingEffects", None)
        return cleaned

    @staticmethod
    def _package_frame_state(
        public_state: dict[str, Any],
        decision_surface: dict[str, Any],
    ) -> dict[str, Any]:
        """Prefer the package-owned decision projection over raw game state.

        Packages already know which public facts, costs, effects, and stable
        identifiers matter at the current authority boundary. The shared
        runtime only removes duplicated/non-production payloads and engine
        internals; it does not reinterpret game rules.
        """
        sections = decision_surface.get("narrativeSections")
        if not isinstance(sections, list) or not sections:
            return GameTeammateController._public_frame_state(public_state)
        projected = copy.deepcopy(decision_surface)
        for duplicate in (
            "scoringDecisionFacts",
            "informationBoundaries",
            "decisionExamples",
            "decisionGuidance",
            "currentActions",
            "modelFacts",
        ):
            projected.pop(duplicate, None)
        return GameTeammateController._public_frame_state(projected)

    def _turn_prompt(self, turn_id: str, state: dict) -> str:
        frame = self._decision_frame(turn_id, state)
        return (
            "## Authoritative DecisionFrame (Authoritative turn snapshot)\n"
            + _render_model_decision_frame(frame)
        )

    async def _game_memory_attachments(self, turn_prompt: str) -> list[dict]:
        """Select at most two relevant personal ``game`` memories before the turn."""
        if not self._memory_selection_enabled:
            return []
        if self.game_turn_count != 1 and self.game_turn_count % 3 != 0:
            return []
        from bglab.memory import get_relevant_memory_attachments
        from bglab.memory.memdir import get_game_memory_dir, scan_memory_files

        mem_dir = get_game_memory_dir(self.engine, f"ai-p{self.pid}")
        current_mtimes = {
            str(item.get("filePath", "")): float(item.get("mtimeMs", 0) or 0)
            for item in scan_memory_files(mem_dir)
        }
        surfaced = {
            path for path, seen_mtime in self.surfaced_game_memories.items()
            if current_mtimes.get(path) == float(seen_mtime or 0)
        }
        self.surfaced_game_memories = {
            path: mtime for path, mtime in self.surfaced_game_memories.items()
            if current_mtimes.get(path) == float(mtime or 0)
        }
        if len(self.surfaced_game_memories) >= 4:
            return []
        selected = await get_relevant_memory_attachments(
            turn_prompt,
            already_surfaced=surfaced,
            mem_dir=mem_dir,
            max_selected=min(2, 4 - len(self.surfaced_game_memories)),
            max_bytes_per_memory=1_500,
            max_lines_per_memory=80,
            allowed_statuses={"active", "validated"},
            model=self.model,
        )
        attachments: list[dict] = []
        for group in selected:
            for memory in group.get("memories", []):
                path = str(memory.get("path", ""))
                filename = str(memory.get("filename", "memory.md"))
                if path:
                    self.surfaced_game_memories[path] = float(memory.get("mtimeMs", 0) or 0)
                attachments.append({
                    "role": "user",
                    "type": "attachment",
                    "attachment_type": "game_memory",
                    "content": [{"type": "text", "text": (
                        "<game-memory>\n"
                        f"{memory.get('content', '')}\n"
                        "Use only when the current board matches its conditions.\n"
                        "</game-memory>"
                    )}],
                    "_is_meta": True,
                    "_attachment_id": f"game_memory:{self.game_turn_count}:{filename}",
                })
        if attachments:
            self.persist()
        return attachments

    def _schedule_game_memory_extraction(self, *, force: bool = False) -> None:
        """Schedule one extraction tick owned by this persistent teammate."""
        if not self._memory_extraction_enabled:
            return
        snapshot = getattr(self.deps.stop_hooks_state, "last_snapshot", None)
        if snapshot is None:
            if force:
                self._memory_force_pending = True
                self.persist()
            return
        from bglab.hooks.stop_hooks import _background_game_memory_extraction

        def create_task() -> None:
            if self._memory_extraction_task is not None and not self._memory_extraction_task.done():
                self._memory_force_pending = self._memory_force_pending or force
                self.persist()
                return
            loop = asyncio.get_running_loop()
            effective_force = force or self._memory_force_pending
            self._memory_force_pending = False
            task = loop.create_task(
                _background_game_memory_extraction(
                    self.deps.stop_hooks_state,
                    snapshot,
                    [],
                    {
                        "engine": self.engine, "game_id": self.game_id,
                        "agent_id": f"ai-p{self.pid}", "force": effective_force,
                        "persist_state": self.persist,
                        "event_callback": self.event_callback,
                    },
                ),
                name=f"bg-memory-{self.game_id}-p{self.pid}-turn-{self.game_turn_count}",
            )
            self._memory_extraction_task = task
            self._background_tasks.add(task)
            def completed(done: asyncio.Task) -> None:
                self._background_tasks.discard(done)
                self._memory_extraction_task = None
                if done.cancelled() and effective_force:
                    self._memory_force_pending = True
                self.persist()
                if self._memory_force_pending and not self._stopping:
                    self._schedule_game_memory_extraction(force=True)
            task.add_done_callback(completed)

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        owner_loop = self._owner_loop or current_loop
        if owner_loop is None or owner_loop.is_closed():
            return
        if current_loop is owner_loop:
            create_task()
        else:
            owner_loop.call_soon_threadsafe(create_task)

    async def stop(self) -> None:
        """Stop the runner and every background task owned by this teammate."""
        if self._stopping:
            return
        self._stopping = True
        queued = self._scheduled_turn_handle
        if queued is not None and not queued.completion.done():
            queued.completion.cancel()
            await asyncio.gather(queued.completion, return_exceptions=True)
        if queued is not None and not queued.action.done():
            queued.action.cancel()
        self._scheduled_turn_handle = None
        # An exhausted Provider attempt is already closed and its reason is
        # durable recovery identity.  The user may authorize the retry before
        # or after restarting the host, so shutdown must preserve both states.
        preserve_exhausted_recovery = (
            self._attempt_status == "EXHAUSTED"
            and (
                self._attempt_reason in _MANUAL_RETRY_REASONS
                or self._manual_retry_status == "AUTHORIZED"
            )
        )
        preserved_reason = self._attempt_reason
        if preserve_exhausted_recovery:
            if (
                self._active_attempt is not None
                or (self._active_task is not None and not self._active_task.done())
            ):
                await self.cancel_current_attempt("shutdown")
            self._attempt_status = "EXHAUSTED"
            self._attempt_reason = preserved_reason
        else:
            await self.cancel_current_attempt("shutdown")
        await self.runner.stop()
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.persist()


# Compatibility import for callers written against the superseded standalone
# game-agent design. The active implementation is controller-only; the shared
# InProcessTeammateRunner owns the model/query loop.
PersistentGameAgent = GameTeammateController


class GameSessionRuntime:
    def __init__(
        self,
        game_id: str,
        player_types: list[str],
        names: list[str],
        game_rules: str,
        *,
        store: GameStore | None = None,
        model: str = "deepseek-chat",
        call_model: Any = None,
        event_callback: EventCallback | None = None,
        restore: bool = False,
        team_name: str | None = None,
        definition: GameDefinition | None = None,
        action_validator: Callable[[str, int, dict], Any] | None = None,
        profile: GameFeatureProfile = DEFAULT_GAME_PROFILE,
        agent_profiles: list[GameAgentProfile] | None = None,
        stop_after_action: bool = False,
        action_protocol: str = "semantic-v2",
        provider_policy: ProviderSlotPolicy | None = None,
        host_fallback_enabled: bool = False,
    ):
        self.game_id = game_id
        self.player_types = player_types
        self.names = names
        self.store = store or GameStore(game_id)
        manifest = self.store.read_manifest() if restore else {}
        stored_engine = str(manifest.get("engine", "splendor"))
        self.definition = definition or get_game(stored_engine)
        self.engine = self.definition.id
        if agent_profiles is not None and len(agent_profiles) != len(player_types):
            raise ValueError("agent_profiles must match player_types length")
        self.profile = profile
        self.action_protocol = action_protocol
        self.provider_slots = provider_policy or load_provider_slot_policy({"model": model})
        if agent_profiles is None:
            if profile.skills:
                from bglab.games.skills.loader import get_skillset_version

                default_skill_version = get_skillset_version(self.engine)
            else:
                default_skill_version = "none"
            self.agent_profiles = [
                GameAgentProfile(
                    features=profile,
                    skill_bundle_version=default_skill_version,
                )
                for _ in player_types
            ]
        else:
            self.agent_profiles = agent_profiles
        if manifest.get("engine") and manifest["engine"] != self.engine:
            raise GameInvariantError(
                f"session engine mismatch: {manifest['engine']} != {self.engine}",
            )
        stored_action_protocol = manifest.get("action_protocol")
        if (
            restore
            and stored_action_protocol is not None
            and stored_action_protocol != action_protocol
        ):
            raise GameInvariantError("session action protocol mismatch during restore")
        skills_requested = any(
            item.features.skills and item.skill_bundle_version != "none"
            for item in self.agent_profiles
        )
        if skills_requested:
            from bglab.games.skills.loader import resolve_game_skill_bundle

        resolved_skill_bundles = []
        for item in self.agent_profiles:
            if item.features.skills and item.skill_bundle_version != "none":
                resolved_skill_bundles.append(resolve_game_skill_bundle(
                    self.engine,
                    item.skill_bundle_version,
                ))
            else:
                resolved_skill_bundles.append(
                    _DisabledGameSkillBundle(self.engine),
                )
        serialized_profiles = []
        for item, bundle in zip(self.agent_profiles, resolved_skill_bundles):
            serialized = _serialize_agent_profile(item)
            serialized["skill_bundle_fingerprint"] = bundle.fingerprint
            serialized_profiles.append(serialized)
        stored_profiles = manifest.get("agent_profiles")
        if restore and stored_profiles is not None and stored_profiles != serialized_profiles:
            raise GameInvariantError("agent profile mismatch during restore")
        self.store.update_manifest(
            profile=profile.name,
            agent_profiles=serialized_profiles,
            action_protocol=action_protocol,
            host_fallback_enabled=host_fallback_enabled,
        )
        self.event_callback = event_callback
        self.team_name = team_name or f"bg-{game_id}"
        self.status = "finished" if restore and manifest.get("status") == "finished" else "active"
        if restore and manifest.get("status") != "finished":
            self.store.update_manifest(status="active", error=None)
        self.browser_chat_callback: Callable[[dict], Any] | None = None
        self.public_chat_history: list[dict] = [
            event for event in self.store.read_events()
            if event.get("type") in {"game_chat", "game_chat_status"}
        ][-20:]
        self._turns: dict[str, TurnHandle] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._stale_turns_to_retry: set[str] = set()
        self._skill_evolution_scheduled = False
        try:
            self._owner_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._owner_loop = None
        self.agents: dict[int, GameTeammateController] = {}
        self.last_confirmed_turn_id = manifest.get("last_confirmed_turn_id")
        self.recovery_state = {"total": int(manifest.get("missing_act_recoveries", 0) or 0)}
        for pid, player_type in enumerate(player_types):
            if player_type != "ai":
                continue
            agent_profile = self.agent_profiles[pid]
            restored = self.store.read_agent_state(pid) if restore else None
            self.agents[pid] = GameTeammateController(
                pid, game_id, self.store, game_rules, model=model,
                call_model=call_model, event_callback=self._handle_agent_event,
                restored=restored,
                recovery_state=self.recovery_state,
                team_name=self.team_name,
                chat_sink=self._route_chat,
                public_state_provider=self._public_player_context,
                definition=self.definition,
                action_validator=action_validator,
                profile=agent_profile.features,
                enabled_tool_names=agent_profile.enabled_tool_names,
                skill_bundle_version=agent_profile.skill_bundle_version,
                skill_bundle=resolved_skill_bundles[pid],
                stop_after_action=stop_after_action,
                action_protocol=action_protocol,
                provider_policy=self.provider_slots,
                host_fallback_enabled=host_fallback_enabled,
            )
        self.chat_log = None
        if any(item.features.chat for item in self.agent_profiles):
            from bglab.games.chat import GameChatLog

            self.chat_log = GameChatLog(
                self.store, self.player_types, publish=self._publish_chat,
                enqueue=lambda pid, sender, message, mid: self.agents[pid].enqueue_chat(sender, message, mid),
                acknowledge=lambda pid, mid: self.agents[pid].acknowledge_chat(mid) if pid in self.agents else None,
                enabled=lambda pid: self.agent_profiles[pid].features.chat,
            )
            for pid, agent in self.agents.items():
                agent.tool_ctx['_chat_finish_unanswered'] = lambda ids, pid=pid: self.chat_log.mark_unanswered(pid, ids)
        if any(item.features.skill_evolution for item in self.agent_profiles):
            self._resume_pending_skill_reviews()
        if any(agent._memory_extraction_enabled for agent in self.agents.values()):
            self._resume_pending_memory_extractions()

    def set_action_validator(
        self, validator: Callable[[str, int, dict], Any] | None,
    ) -> None:
        """Attach the current frontend adapter as the authoritative action validator."""
        for agent in self.agents.values():
            agent.action_validator = validator

    def bind_confirmed_ai_turn(
        self,
        pid: int,
        turn_id: str,
        claimed_state: dict,
    ) -> ConfirmedAuthorityFrame:
        """Bind a browser AI request to the one durable host snapshot.

        This is deliberately a read-only admission gate.  It runs before a
        controller creates a handle or persists an attempt, and never pauses
        the session on a stale browser frame.
        """
        pending = self.store.read_pending_replay_turn()
        if pending is not None:
            raise AuthorityBindingError(
                "a durable replay action is awaiting frontend confirmation",
                expected_turn_id=(
                    str(pending.get("turnId"))
                    if isinstance(pending, dict) and pending.get("turnId") is not None
                    else None
                ),
            )
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid not in self.agents
            or pid < 0
            or pid >= len(self.player_types)
            or self.player_types[pid] != "ai"
        ):
            raise AuthorityBindingError(f"P{pid} is not an AI seat")
        if not isinstance(turn_id, str) or not turn_id:
            raise AuthorityBindingError("AI turnId is invalid")
        if not isinstance(claimed_state, dict):
            raise AuthorityBindingError("AI claimed state must be an object")

        record = self.store.read_snapshot()
        if not isinstance(record, dict):
            raise AuthorityBindingError(
                "no confirmed authority snapshot is available",
            )
        confirmed_state = record.get("state")
        if not isinstance(confirmed_state, dict):
            raise AuthorityBindingError(
                "confirmed authority snapshot state is malformed",
            )
        expected_turn_id = record.get("turn_id")
        if not isinstance(expected_turn_id, str) or not expected_turn_id:
            raise AuthorityBindingError(
                "confirmed authority snapshot turnId is malformed",
            )
        if expected_turn_id != turn_id:
            raise AuthorityBindingError(
                f"stale AI turnId {turn_id}; expected {expected_turn_id}",
                expected_turn_id=expected_turn_id,
            )

        if confirmed_state.get("decisionId") != turn_id:
            raise AuthorityBindingError(
                "confirmed snapshot decisionId does not match requested turnId",
                expected_turn_id=expected_turn_id,
            )
        if claimed_state.get("decisionId") != turn_id:
            raise AuthorityBindingError(
                "claimed snapshot decisionId does not match requested turnId",
                expected_turn_id=expected_turn_id,
            )
        confirmed_wrapper = _authority_wrapper(confirmed_state)
        if confirmed_wrapper.get("phase") in {"finished", "gameover"}:
            raise AuthorityBindingError(
                "confirmed authority snapshot is terminal and has no AI decision",
                expected_turn_id=expected_turn_id,
            )
        current_player = confirmed_wrapper.get("currentPlayer")
        if (
            isinstance(current_player, bool)
            or not isinstance(current_player, int)
            or current_player != pid
        ):
            raise AuthorityBindingError(
                "confirmed snapshot currentPlayer does not match AI seat",
                expected_turn_id=expected_turn_id,
            )
        confirmed_hash = authority_hash(confirmed_state)
        if authority_hash(claimed_state) != confirmed_hash:
            raise AuthorityBindingError(
                "claimed authority hash does not match confirmed snapshot",
                expected_turn_id=expected_turn_id,
            )
        pending = self.store.read_pending_replay_turn()
        if pending is not None:
            raise AuthorityBindingError(
                "a durable replay action is awaiting frontend confirmation",
                expected_turn_id=(
                    str(pending.get("turnId"))
                    if isinstance(pending, dict) and pending.get("turnId") is not None
                    else expected_turn_id
                ),
            )
        return ConfirmedAuthorityFrame(
            pid=pid,
            turn_id=turn_id,
            state=copy.deepcopy(confirmed_state),
            authority_hash=confirmed_hash,
            token=_snapshot_token(record),
        )

    def _revalidate_confirmed_ai_turn(
        self,
        frame: ConfirmedAuthorityFrame,
    ) -> ConfirmedAuthorityFrame:
        """Re-read the durable frame immediately before provider dispatch."""
        current = self.bind_confirmed_ai_turn(
            frame.pid, frame.turn_id, frame.state,
        )
        if current.token != frame.token:
            raise AuthorityBindingError(
                "confirmed authority changed while the AI frame was queued",
                expected_turn_id=current.turn_id,
            )
        return current

    def manual_retry_ready(self) -> dict[str, Any] | None:
        """Return the sole persisted retry authorization, if present."""
        ready = [
            {"pid": pid, **record}
            for pid, agent in self.agents.items()
            if (record := agent.manual_retry_ready()) is not None
        ]
        if len(ready) > 1:
            raise GameInvariantError("multiple manual retries are authorized")
        return ready[0] if ready else None

    def set_provider_slots(
        self,
        primary: ProviderSlot,
        standby: ProviderSlot | None = None,
        *,
        credential_configured: bool | dict[str, bool] = True,
    ) -> None:
        """Set the non-secret slot policy used by subsequent AI attempts."""
        policy = ProviderSlotPolicy(primary=primary, standby=standby)
        self.provider_slots = policy
        for agent in self.agents.values():
            agent.set_provider_slots(
                policy, credential_configured=credential_configured,
            )

    @staticmethod
    def _agent_semantic_lifecycle(agent: GameTeammateController) -> dict | None:
        return SubmissionState(agent.tool_ctx).lifecycle_payload()

    @staticmethod
    def _discard_agent_committed_action(
        agent: GameTeammateController,
        decision_id: str,
    ) -> bool:
        if decision_id not in agent.committed_actions:
            return False
        agent.committed_actions.pop(decision_id, None)
        fallback = getattr(agent, "host_fallbacks", {}).get(decision_id)
        if fallback and fallback.get("status") == "committed":
            fallback["status"] = "rolled_back"
        if agent.last_committed_turn_id == decision_id:
            remaining = [
                turn_id for turn_id in agent.committed_actions
                if _turn_number(turn_id) is not None
            ]
            agent.last_committed_turn_id = max(
                remaining, key=lambda turn_id: _turn_number(turn_id) or (),
                default=None,
            )
        return True

    def _reconcile_terminal_same_decision(self, state: dict) -> bool:
        """Retain or repair a final cache only from matching authority and replay."""
        if self._snapshot_wrapper(state).get('phase') not in {'finished', 'gameover'}:
            return False
        path = self.store.dir / 'replay' / 'turns.jsonl'
        if not path.is_file():
            return False
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
            turn = json.loads(lines[-1]) if lines else None
        except (OSError, ValueError) as exc:
            raise GameInvariantError('terminal replay is unreadable') from exc
        decision_id = state.get('decisionId')
        if not isinstance(turn, dict) or turn.get('turnId') != decision_id:
            return False
        committed = state.get('committed')
        receipt = committed.get(decision_id) if isinstance(committed, dict) else None
        result = receipt.get('result') if isinstance(receipt, dict) else None
        transaction = result.get('transaction') or result.get('action') if isinstance(result, dict) else None
        if (not isinstance(transaction, dict) or transaction != turn.get('transaction')
                or turn.get('afterHash') != authority_hash(state)
                or turn.get('beforeHash') == turn.get('afterHash')):
            raise GameInvariantError('terminal replay does not prove the confirmed transaction')
        agent = self.agents.get(turn.get('seat'))
        if agent is None:
            return True  # A confirmed human final move has no seat-agent cache.
        cached = agent.committed_actions.get(decision_id)
        if cached is not None and cached.get('transaction') != transaction:
            raise GameInvariantError('terminal committed cache differs from replay')
        fallback = getattr(agent, 'host_fallbacks', {}).get(decision_id)
        if turn.get('decisionSource') == 'host_fallback':
            if (not isinstance(fallback, dict)
                    or fallback.get('authorityHash') != turn.get('beforeHash')
                    or {key: fallback.get(key) for key in ('decisionId', 'reason', 'source')} != turn.get('fallback')):
                raise GameInvariantError('terminal host origin differs from replay')
        changed = False
        if cached is None:
            agent.committed_actions[decision_id] = {
                'status': 'committed', 'transaction': copy.deepcopy(transaction),
                'canonicalAction': copy.deepcopy(turn['canonicalAction']),
                'effects': copy.deepcopy(turn['effects']), 'boundaryReason': 'game_finished',
                'stateChanged': True,
                **({'decisionSource': 'host_fallback', 'fallback': copy.deepcopy(turn['fallback'])}
                   if turn.get('decisionSource') == 'host_fallback' else {}),
            }
            changed = True
        if fallback is not None and turn.get('decisionSource') == 'host_fallback' and fallback.get('status') != 'committed':
            fallback['status'] = 'committed'
            changed = True
        if agent.last_committed_turn_id != decision_id:
            agent.last_committed_turn_id = decision_id
            changed = True
        agent.confirm_action_snapshot(state)
        if changed:
            agent.persist()
        return True

    def _reconcile_agents_against_snapshot(self, state: dict) -> None:
        """Reconcile optimistic agent state against one authoritative snapshot."""
        if self._reconcile_terminal_same_decision(state):
            return
        decision_id = state.get("decisionId") if isinstance(state, dict) else None
        if not isinstance(decision_id, str) or not decision_id:
            return
        for agent in self.agents.values():
            changed = False
            semantic_lifecycle = self._agent_semantic_lifecycle(agent)
            lifecycle = semantic_lifecycle
            identity = (
                lifecycle.get("identity")
                if isinstance(lifecycle, dict)
                else None
            )
            lifecycle_decision = (
                identity.get("decisionId")
                if isinstance(identity, dict)
                else None
            )
            if lifecycle_decision == decision_id:
                # No durable pending replay means this exact authoritative
                # snapshot is proof that a prepared/failed fence did not
                # survive in authority. Reopen only the matching lifecycle;
                # an unrelated or advanced identity is never cleared here.
                try:
                    changed = SubmissionState(agent.tool_ctx).reopen_after_rollback(decision_id)
                except ValueError as exc:
                    raise GameInvariantError(str(exc)) from exc
            else:
                # A later confirmed identity clears lifecycle/fence state only
                # through the normal state-hash confirmation path.
                agent.confirm_action_snapshot(state)
            # With no pending replay, the same authoritative decision proves
            # that the optimistic committed-action cache is ahead of authority.
            changed = (
                self._discard_agent_committed_action(agent, decision_id)
                or changed
            )
            if changed:
                agent.persist()

    def reconcile_confirmed_snapshot(self, state: dict) -> dict | None:
        """Reconcile saved authority with replay and semantic commit fences.

        A durable pending turn at the same decision must be redispatched from
        the restored authority snapshot. An advanced decision confirms it and
        can finalize the replay. With no pending turn, the exact same decision
        is evidence of rollback and only its matching uncertain fence is
        reopened.
        """
        decision_id = state.get("decisionId") if isinstance(state, dict) else None
        if not isinstance(decision_id, str) or not decision_id:
            return None
        pending = self.store.read_pending_replay_turn()
        if isinstance(pending, dict):
            pending_turn_id = pending.get("turnId")
            if pending_turn_id == decision_id:
                from bglab.games.replay import authority_hash

                transaction = pending.get("transaction")
                seat = pending.get("seat")
                before_hash = pending.get("beforeHash")
                before_frame = pending.get("beforeFrame")
                if (
                    not isinstance(transaction, dict)
                    or not isinstance(seat, int)
                    or not isinstance(before_frame, int)
                    or isinstance(before_frame, bool)
                    or before_frame != pending.get("index")
                    or not isinstance(before_hash, str)
                    or len(before_hash) != 64
                    or any(char not in "0123456789abcdef" for char in before_hash.lower())
                ):
                    raise GameInvariantError(
                        "pending replay action is malformed and cannot be recovered",
                    )
                frame_path = (
                    self.store.dir / "replay" / "frames"
                    / f"{before_frame:04d}.json"
                )
                try:
                    before_frame_record = json.loads(
                        frame_path.read_text(encoding="utf-8"),
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    raise GameInvariantError(
                        "pending replay before-frame is unavailable for recovery",
                    ) from exc
                if (
                    not isinstance(before_frame_record, dict)
                    or before_frame_record.get("index") != before_frame
                    or before_frame_record.get("authorityHash") != before_hash
                ):
                    raise GameInvariantError(
                        "pending replay before-frame does not match its durable hash",
                    )
                incoming_hash = authority_hash(state)
                if incoming_hash == before_hash:
                    return {
                        "status": "redispatch_required",
                        "turnId": decision_id,
                        "seat": seat,
                        "transaction": copy.deepcopy(transaction),
                    }
                wrapper = self._snapshot_wrapper(state)
                if wrapper.get("phase") not in {"finished", "gameover"}:
                    raise GameInvariantError(
                        "same-decision authority changed without reaching a terminal state",
                    )
                committed = state.get("committed")
                committed_entry = (
                    committed.get(decision_id)
                    if isinstance(committed, dict)
                    else None
                )
                committed_result = (
                    committed_entry.get("result")
                    if isinstance(committed_entry, dict)
                    else None
                )
                committed_transaction = (
                    committed_result.get("transaction")
                    or committed_result.get("action")
                    if isinstance(committed_result, dict)
                    else None
                )
                if committed_transaction != transaction:
                    raise GameInvariantError(
                        "terminal snapshot does not prove the exact pending transaction",
                    )
                # Some adapters keep the final decision identity after the
                # committed action. A changed authority hash plus terminal
                # phase is its confirmed after-frame, not rollback evidence.
                return None
            # The saved authority already advanced past the pending decision.
            # Reuse the ordinary durable snapshot path to append its after
            # frame, clear the pending record, and confirm candidate state.
            self.save_snapshot(decision_id, state)
            return None
        self._reconcile_agents_against_snapshot(state)
        return None

    def cancel_stale_ai_turns(self, confirmed_turn_id: str) -> list[str]:
        """Cancel model work for an AI turn superseded by a newer snapshot."""
        confirmed_ordinal = _snapshot_ordinal(
            confirmed_turn_id, len(self.player_types),
        )
        if confirmed_ordinal is None:
            return []
        cancelled: list[str] = []
        for agent in self.agents.values():
            received_ordinal = _snapshot_ordinal(
                agent.last_received_turn_id, len(self.player_types),
            )
            active_task = getattr(agent, "_active_task", None)
            received_turn_id = agent.last_received_turn_id
            action_already_committed = (
                isinstance(received_turn_id, str)
                and received_turn_id in agent.committed_actions
            )
            if (
                received_ordinal is not None
                and received_ordinal < confirmed_ordinal
                and active_task is not None
                and not active_task.done()
                and not action_already_committed
            ):
                cancel_attempt = getattr(agent, "schedule_cancel_current_attempt", None)
                if callable(cancel_attempt):
                    cancel_attempt("confirmed snapshot superseded this AI turn")
                else:
                    abort_event = getattr(
                        getattr(agent, "runner", None), "_abort_event", None,
                    )
                    if abort_event is not None:
                        abort_event.set("confirmed snapshot superseded this AI turn")
                    active_task.cancel()
                cancelled.append(str(agent.last_received_turn_id))
        self._stale_turns_to_retry.update(cancelled)
        return cancelled

    def consume_stale_turn_retries(self) -> list[str]:
        retries = sorted(self._stale_turns_to_retry)
        self._stale_turns_to_retry.clear()
        return retries

    def _handle_agent_event(self, event: dict) -> None:
        """Forward agent telemetry and expose only final reports to the browser audience."""
        _emit(self.event_callback, event)
        if event.get("type") != "turn_report" or self.browser_chat_callback is None:
            return
        result = self.browser_chat_callback(event)
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)

    def _resume_pending_memory_extractions(self) -> None:
        """Resume a durable end-game extraction cancelled by an early close."""
        from bglab.hooks.stop_hooks import CacheSafeSnapshot

        for agent in self.agents.values():
            if not agent._memory_force_pending:
                continue
            if getattr(agent.deps.stop_hooks_state, "last_snapshot", None) is None:
                agent.deps.stop_hooks_state.last_snapshot = CacheSafeSnapshot(
                    system_prompt=agent.system_prompt,
                    user_context={},
                    system_context={},
                    messages=list(agent.messages),
                    cwd="",
                )
            agent._schedule_game_memory_extraction(force=True)

    def _resume_pending_skill_reviews(self) -> None:
        """Use the current server loop to finish reviews interrupted by an early close."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        from bglab.games.skills.evolution import evolve_game_skills

        for pending_store in GameStore.pending_skill_reviews():
            if pending_store.game_id == self.game_id:
                continue
            pending_store.update_manifest(skill_review_status="in_progress")
            pending_engine = str(
                pending_store.read_manifest().get("engine", self.engine),
            )

            async def resume_one(
                store: GameStore = pending_store,
                engine: str = pending_engine,
            ) -> None:
                try:
                    await evolve_game_skills(store, engine)
                except asyncio.CancelledError:
                    store.update_manifest(skill_review_status="pending")
                    raise
                except Exception as exc:
                    store.update_manifest(
                        skill_review_status="pending",
                        skill_review_error=str(exc)[:300],
                    )
                    store.log_event({
                        "type": "skill_review_failed",
                        "game_id": store.game_id,
                        "error": str(exc)[:300],
                    })

            task = loop.create_task(
                resume_one(),
                name=f"bg-resume-skill-review-{pending_store.game_id}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    def _public_player_context(self, viewer_pid: int) -> list[dict]:
        """Return observable runtime metadata without leaking private reasoning."""
        players: list[dict] = []
        for pid, player_type in enumerate(self.player_types):
            if pid == viewer_pid:
                continue
            item: dict[str, Any] = {
                "pid": pid,
                "name": self.names[pid] if pid < len(self.names) else f"P{pid}",
                "type": player_type,
            }
            agent = self.agents.get(pid)
            if agent is not None:
                last_action = None
                if agent.last_committed_turn_id:
                    last_action = agent.committed_actions.get(agent.last_committed_turn_id)
                item.update({
                    "status": agent.phase,
                    "last_received_turn_id": agent.last_received_turn_id,
                    "last_committed_turn_id": agent.last_committed_turn_id,
                    "last_action": last_action,
                    "loaded_skills": list(agent.loaded_skills),
                })
            players.append(item)
        if self.public_chat_history:
            players.append({
                "recent_public_chat": self.public_chat_history[-3:],
            })
        return players

    def _resolve_chat_target(self, target: Any) -> int:
        if isinstance(target, bool):
            raise GameRuntimeError("invalid game chat target")
        if isinstance(target, int):
            pid = target
        else:
            text = str(target).strip()
            if text.lower() in {"leader", "lead", "code-agent", "code_agent"}:
                raise GameRuntimeError("BgChat cannot target the Leader")
            match = __import__("re").fullmatch(r"[Pp]?(\d+)", text)
            if match:
                pid = int(match.group(1))
            else:
                try:
                    pid = self.names.index(text)
                except ValueError as exc:
                    raise GameRuntimeError(f"unknown game chat target: {target}") from exc
        if not 0 <= pid < len(self.player_types):
            raise GameRuntimeError(f"game chat target P{pid} is outside this game")
        return pid

    def _publish_chat(self, event: dict) -> None:
        self.public_chat_history.append(dict(event))
        self.public_chat_history = self.public_chat_history[-20:]
        _emit(self.event_callback, event)
        if self.browser_chat_callback is not None:
            result = self.browser_chat_callback(event)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    def _route_chat(self, sender_pid: int, target: Any, message: str, *, reply_to: int | None = None, client_id: str | None = None) -> str:
        if self.chat_log is None:
            raise GameRuntimeError('此存档未启用游戏聊天。')
        if self.status == 'finished' and reply_to is None:
            raise GameRuntimeError('对局已结束，AI 不再接收新消息。')
        try:
            return self.chat_log.send(sender_pid, self._resolve_chat_target(target), message, reply_to=reply_to, client_id=client_id)
        except ValueError as exc:
            raise GameRuntimeError(str(exc)) from exc

    def route_human_chat(self, sender_pid: int, target: Any, message: str, *, client_id: str | None = None) -> str:
        if not 0 <= sender_pid < len(self.player_types):
            raise GameRuntimeError("human chat sender is outside this game")
        if self.player_types[sender_pid] != "human":
            raise GameRuntimeError(f"P{sender_pid} is not a human player")
        return self._route_chat(sender_pid, target, message, client_id=client_id)

    async def handle_ai_turn(
        self,
        pid: int,
        turn_id: str,
        state: dict,
        legal_actions: list[dict] | None = None,
        *,
        authority_state: dict | None = None,
        model_state: dict | None = None,
        authority_frame: ConfirmedAuthorityFrame | None = None,
        retry_reason: str | None = None,
    ) -> dict:
        if self.status.startswith("paused"):
            raise GameRuntimeError(f"session is {self.status}")
        if pid not in self.agents:
            raise GameRuntimeError(f"P{pid} is not an AI player")
        agent = self.agents[pid]
        strict_authority = authority_frame is not None
        if strict_authority:
            if (
                authority_frame.pid != pid
                or authority_frame.turn_id != turn_id
            ):
                raise AuthorityBindingError(
                    "authority frame identity does not match AI request",
                    expected_turn_id=authority_frame.turn_id,
                )
            replay_state = authority_frame.snapshot
            if model_state is None:
                model_input = replay_state
                model_input.pop("adapterView", None)
            else:
                if not isinstance(model_state, dict):
                    raise AuthorityBindingError("host model state must be an object")
                if authority_hash(model_state) != authority_frame.authority_hash:
                    raise AuthorityBindingError(
                        "host model state does not match confirmed authority",
                        expected_turn_id=authority_frame.turn_id,
                    )
                model_input = copy.deepcopy(model_state)
        else:
            # Preserve the legacy/unit-test boundary.  Production callers use
            # the explicit authority_frame seam above.  Unbound callers still
            # default replay authority to an exact persisted snapshot when one
            # exists, keeping presentation-only adapterView data out of the
            # durable before-frame.
            stored_record = self.store.read_snapshot()
            stored_state = (
                stored_record.get("state")
                if isinstance(stored_record, dict)
                and stored_record.get("turn_id") == turn_id
                else None
            )
            replay_state = copy.deepcopy(
                authority_state
                if authority_state is not None
                else stored_state
                if isinstance(stored_state, dict)
                else state,
            )
            model_input = copy.deepcopy(model_state if model_state is not None else state)
        if not isinstance(model_input.get("adapterView"), dict):
            from bglab.games.adapter_process import AdapterProcess

            projection_adapter = AdapterProcess(self.definition)
            try:
                projection_adapter.restore(copy.deepcopy(replay_state))
                model_input["adapterView"] = projection_adapter.view(pid)
            finally:
                projection_adapter.close()
        state_for_model = copy.deepcopy(model_input)
        retry_instruction: str | None = (
            agent.retry_instruction()
            if retry_reason is not None and isinstance(agent, GameTeammateController)
            else None
        )
        if (
            retry_reason is not None
            and isinstance(agent, GameTeammateController)
            and not agent.retry_available(turn_id, replay_state)
        ):
            error = GameRuntimeError(
                "frontend reconnect retry budget exhausted for this DecisionFrame",
            )
            self.pause(
                "paused_frontend_error", str(error), turn_id=turn_id, pid=pid,
            )
            raise error
        provider_slot: ProviderSlot | None = None
        # Exactly one Player query submission is admitted here. Provider
        # attempts are already bounded inside ErrorRecoveryProfile; exhaustion
        # pauses the session until an explicit user-authorized retry.
        for _single_query_submission in (None,):
            if strict_authority:
                authority_frame = self._revalidate_confirmed_ai_turn(authority_frame)
            if isinstance(agent, GameTeammateController):
                handle = agent.start_turn(
                    turn_id,
                    state_for_model,
                    authority_state=replay_state,
                    **(
                        {"authority_frame": authority_frame}
                        if strict_authority
                        else {}
                    ),
                    retry_instruction=retry_instruction,
                    provider_slot=provider_slot,
                )
            else:
                # Runtime test doubles and external controllers keep the original
                # public two-argument boundary.
                handle = agent.start_turn(turn_id, state_for_model)
            self._turns[turn_id] = handle
            started_future = getattr(handle, "started", None)
            try:
                if started_future is not None:
                    await asyncio.shield(started_future)
                done, _ = await asyncio.wait(
                    {handle.action, handle.completion},
                    timeout=_action_delivery_timeout(agent.api_turn_timeout_seconds),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if handle.action in done:
                    committed = handle.action.result()
                elif handle.completion in done:
                    handle.completion.result()
                    raise GameInvariantError("AI turn completed without a valid BgAct")
                else:
                    raise asyncio.TimeoutError
                return {
                    "type": "ai_action", "pid": pid, "turnId": turn_id,
                    "action": committed["transaction"],
                    "transaction": committed["transaction"],
                    "canonicalAction": committed.get("canonicalAction"),
                    "effects": committed.get("effects", []),
                    "confirmedTurnId": committed.get("frontendTurnId"),
                    "confirmedStateHash": committed.get("frontendStateHash"),
                    **({"decisionSource": "host_fallback", "fallback": copy.deepcopy(committed["fallback"])}
                       if committed.get("decisionSource") == "host_fallback" else {}),
                }
            except AuthorityBindingError:
                await asyncio.gather(handle.completion, return_exceptions=True)
                raise
            except ProviderFailureError as exc:
                await asyncio.gather(handle.completion, return_exceptions=True)
                cancel_attempt = getattr(agent, "cancel_current_attempt", None)
                if callable(cancel_attempt):
                    await cancel_attempt("provider_failure")
                if isinstance(agent, GameTeammateController):
                    agent.mark_attempt_exhausted()
                error = GameAPIError(
                    f"Provider failure after bounded retries: {exc.provider_failure.reason}"
                )
                self.pause(
                    "paused_api_error", str(error), turn_id=turn_id, pid=pid,
                    provider_failure=exc.provider_failure,
                )
                raise error from exc
            except asyncio.TimeoutError:
                error = GameAPIError(
                    f"AI API timeout after {agent.api_turn_timeout_seconds:g} seconds "
                    "before valid BgAct"
                )
                cancel_attempt = getattr(agent, "cancel_current_attempt", None)
                if callable(cancel_attempt):
                    await cancel_attempt("session_delivery_deadline")
                    await asyncio.gather(handle.completion, return_exceptions=True)
                else:
                    if not handle.action.done():
                        handle.action.cancel()
                    if not handle.completion.done():
                        handle.completion.cancel()
                if isinstance(agent, GameTeammateController):
                    agent.mark_attempt_exhausted()
                self.pause(
                    "paused_api_error", str(error), turn_id=turn_id, pid=pid,
                    provider_failure=ProviderFailureInfo(
                        reason="timeout", retryable_same_slot=True,
                        switch_slot=False,
                    ),
                )
                raise error
            except asyncio.CancelledError:
                promoted = (
                    started_future is None
                    or (
                        started_future.done()
                        and not started_future.cancelled()
                        and started_future.exception() is None
                    )
                )
                if not promoted:
                    if not handle.completion.done():
                        handle.completion.cancel()
                        await asyncio.gather(
                            handle.completion, return_exceptions=True,
                        )
                    if not handle.action.done():
                        handle.action.cancel()
                    raise
                attempt_reason = (
                    agent._attempt_reason
                    if isinstance(agent, GameTeammateController)
                    else ""
                )
                if (
                    isinstance(agent, GameTeammateController)
                    and agent._active_attempt is not None
                    and agent._active_attempt.reason
                ):
                    attempt_reason = agent._active_attempt.reason
                already_fenced = attempt_reason in {
                    "decision_deadline", "session_delivery_deadline",
                    "frontend_disconnected",
                }
                if (
                    isinstance(agent, GameTeammateController)
                    and not already_fenced
                    and not handle.completion.done()
                ):
                    await agent.cancel_current_attempt("cancelled")
                elif not handle.completion.done():
                    await asyncio.gather(handle.completion, return_exceptions=True)
                if self.status in {"stopped", "finished"}:
                    raise
                if not (
                    isinstance(agent, GameTeammateController)
                    and agent._attempt_reason in {
                        "decision_deadline", "session_delivery_deadline",
                    }
                ):
                    raise
                if isinstance(agent, GameTeammateController):
                    agent.mark_attempt_exhausted()
                error = GameAPIError(
                    f"AI API timeout after {agent.api_turn_timeout_seconds:g} seconds "
                    "before valid BgAct"
                )
                self.pause(
                    "paused_api_error", str(error), turn_id=turn_id, pid=pid,
                    provider_failure=ProviderFailureInfo(
                        reason="timeout", retryable_same_slot=True,
                        switch_slot=False,
                    ),
                )
                raise error
            except Exception as exc:
                await asyncio.gather(handle.completion, return_exceptions=True)
                if self.status in {"stopped", "finished"}:
                    # Normal shutdown cancels any in-flight AI turn. Do not let the
                    # cancelled request race with stop() and overwrite its terminal
                    # state with paused_invariant.
                    raise
                if isinstance(exc, GameAPIError):
                    cancel_attempt = getattr(agent, "cancel_current_attempt", None)
                    attempt_reason = (
                        agent._attempt_reason
                        if isinstance(agent, GameTeammateController)
                        else ""
                    )
                    deadline_failure = attempt_reason in {
                        "decision_deadline", "session_delivery_deadline",
                    }
                    if callable(cancel_attempt):
                        await cancel_attempt(
                            attempt_reason if deadline_failure else "api_error",
                        )
                    # Unstructured query API errors have already consumed the
                    # query loop's bounded recovery.  They do not replay the
                    # whole DecisionFrame through this deadline retry layer.
                    if isinstance(agent, GameTeammateController):
                        agent.mark_attempt_exhausted()
                status = "paused_api_error" if isinstance(exc, GameAPIError) else "paused_invariant"
                self.pause(status, str(exc), turn_id=turn_id, pid=pid)
                raise

    def pause(
        self,
        status: str,
        error: str,
        *,
        turn_id: str = "",
        pid: int | None = None,
        provider_failure: ProviderFailureInfo | None = None,
    ) -> None:
        self.status = status
        issue = None
        if status == "paused_api_error":
            failure = provider_failure or ProviderFailureInfo(
                reason=(
                    "timeout" if "timeout" in error.casefold()
                    else "provider_failure"
                ),
                retryable_same_slot=True,
                switch_slot=False,
            )
            projected = project_provider_failure(failure)
            issue = {
                "category": projected.category,
                "title": projected.title,
                "recoverable": projected.recoverable,
                "statusCode": projected.status_code,
            }
        stored_error = (
            issue["title"] if isinstance(issue, dict) else str(error)[:300]
        )
        self.store.update_manifest(
            status=status,
            error=stored_error,
            pause_turn_id=turn_id,
            pause_pid=pid,
            pause_issue=issue,
        )
        event = {
            "type": "game_paused", "turn_id": turn_id, "pid": pid,
            "error": stored_error, "status": status, "issue": issue,
        }
        self.store.log_event(event)
        _emit(self.event_callback, {
            "type": "game_paused", "game_id": self.game_id,
            **event,
        })

    @staticmethod
    def _snapshot_wrapper(snapshot: dict | None) -> dict:
        if not isinstance(snapshot, dict):
            return {}
        wrapper = snapshot.get("wrapper")
        if isinstance(wrapper, dict):
            return wrapper
        compact = snapshot.get("st")
        return compact if isinstance(compact, dict) else {}

    def _best_effort_telemetry(self, method: str, payload: dict) -> None:
        _best_effort_store_telemetry(self.store, self.game_id, method, payload)

    def _prepare_frontend_human_action(
        self, before_state: dict | None, state: dict,
    ) -> dict | None:
        """Prepare one confirmed human Adapter transaction for the replay WAL.

        Browser adapters retain committed transactions in the snapshot.  The
        runtime used to record only AI decisions, which made a later AI
        before-frame conflict with the previous AI after-frame whenever a
        human moved between them.  Human actions are already authoritative at
        this point; prepare their immutable before-frame and pending replay
        record before the new confirmed snapshot is written.
        """
        if not isinstance(before_state, dict) or not isinstance(state, dict):
            return None
        before_committed = before_state.get("committed")
        current_committed = state.get("committed")
        if before_committed is None:
            before_committed = {}
        if not isinstance(before_committed, dict) or not isinstance(current_committed, dict):
            return None
        new_ids = [
            str(decision_id)
            for decision_id in current_committed
            if decision_id not in before_committed
        ]
        if not new_ids:
            return None
        if len(new_ids) != 1:
            raise GameInvariantError(
                "REPLAY_STORE_CORRUPT: one snapshot introduced multiple committed actions",
            )
        decision_id = new_ids[0]
        before_wrapper = self._snapshot_wrapper(before_state)
        pid = before_wrapper.get("currentPlayer")
        if not isinstance(pid, int) or not 0 <= pid < len(self.player_types):
            return None
        if self.player_types[pid] != "human":
            return None
        entry = current_committed.get(decision_id)
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            raise GameInvariantError(
                "REPLAY_STORE_CORRUPT: human committed action has no result",
            )
        # Browser adapters may expose both the replayable Adapter transaction
        # and a presentation-oriented canonical action.  Replay must retain
        # the former; canonical actions are not accepted by dispatch().
        transaction = result.get("transaction") or result.get("action")
        if not isinstance(transaction, dict) or not isinstance(transaction.get("steps"), list) or not transaction["steps"]:
            # Some packages retain raw actions in authority for compatibility.
            # Ask the restored package for its recorded chain; browser views
            # and raw action shapes are never interpreted as replay authority.
            from bglab.games.adapter_process import AdapterProcess

            verifier = AdapterProcess(self.definition)
            try:
                verifier.restore(copy.deepcopy(state))
                receipt = verifier.view(pid).get("lastCommittedTransaction")
                if not isinstance(receipt, dict) or receipt.get("decisionId") != decision_id:
                    raise GameInvariantError(
                        "REPLAY_STORE_CORRUPT: human committed transaction receipt is missing or stale",
                    )
                transaction = receipt.get("transaction")
                if not isinstance(transaction, dict) or not isinstance(transaction.get("steps"), list) or not transaction["steps"]:
                    raise GameInvariantError(
                        "REPLAY_STORE_CORRUPT: human committed receipt has no transaction steps",
                    )
                verifier.restore(copy.deepcopy(before_state))
                replayed = verifier.dispatch(decision_id, copy.deepcopy(transaction))
                if (
                    not isinstance(replayed, dict)
                    or not replayed.get("ok")
                    or replayed.get("events", []) != result.get("events", result.get("effects", []))
                    or authority_hash(verifier.snapshot()) != authority_hash(state)
                ):
                    raise GameInvariantError(
                        "REPLAY_STORE_CORRUPT: human transaction receipt does not reproduce confirmed authority",
                    )
            finally:
                verifier.close()
        replay_index = self.store.replay_turn_count()
        before_hash = self.store.write_replay_frame(replay_index, before_state)
        if self.store.replay_frame_count() != replay_index + 1:
            raise GameInvariantError(
                "REPLAY_STORE_CORRUPT: replay frame sequence is not contiguous",
            )
        pending = {
            "schemaVersion": 1,
            "index": replay_index,
            "turnId": decision_id,
            "seat": pid,
            "transaction": copy.deepcopy(transaction),
            "canonicalAction": copy.deepcopy(
                result.get("canonicalAction", result.get("action")),
            ),
            "effects": copy.deepcopy(result.get("events", result.get("effects", []))),
            "beforeFrame": replay_index,
            "beforeHash": before_hash,
        }
        self.store.write_pending_replay_turn(pending)
        return pending

    @staticmethod
    def _human_replay_decision_payload(
        pending: dict,
        before_state: dict | None,
        state: dict,
    ) -> dict:
        committed = state.get("committed") if isinstance(state, dict) else None
        entry = (
            committed.get(pending.get("turnId"))
            if isinstance(committed, dict)
            else None
        )
        result = entry.get("result") if isinstance(entry, dict) else None
        return {
            "turn_id": pending.get("turnId"),
            "pid": pending.get("seat"),
            "state": copy.deepcopy(before_state),
            "loaded_skills": [],
            "loaded_skill_phases": {},
            "strategy_phase": "human",
            "plan": "",
            "plan_phase": "human",
            "transaction": copy.deepcopy(pending.get("transaction")),
            "canonical_action": copy.deepcopy(pending.get("canonicalAction")),
            "effects": copy.deepcopy(pending.get("effects", [])),
            "outcome": copy.deepcopy(
                result.get("outcome", {}) if isinstance(result, dict) else {}
            ),
            "rejected_attempts": [],
        }

    def save_snapshot(
        self,
        turn_id: str,
        state: dict,
        *,
        final_result: dict | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        incoming_ordinal = _snapshot_ordinal(turn_id, len(self.player_types))
        confirmed_ordinal = _snapshot_ordinal(
            self.last_confirmed_turn_id, len(self.player_types),
        )
        if (
            incoming_ordinal is not None and confirmed_ordinal is not None
            and incoming_ordinal < confirmed_ordinal
        ):
            raise GameInvariantError(
                f"stale snapshot {turn_id}; last confirmed is {self.last_confirmed_turn_id}"
            )
        wrapper = state.get("st", state.get("wrapper", {})) if isinstance(state, dict) else {}
        if self.status == "finished" and wrapper.get("phase") not in {"finished", "gameover"}:
            raise GameInvariantError("a non-finished snapshot cannot replace a finished game")
        terminal = wrapper.get("phase") in {"finished", "gameover"}
        validated_final_result: dict | None = None
        if terminal and not final_result:
            raise GameInvariantError(
                "FINAL_RESULT_REQUIRED: terminal snapshot requires a non-empty final_result",
            )
        if final_result is not None:
            if not terminal:
                raise GameInvariantError(
                    "FINAL_RESULT_INVALID: non-terminal snapshot included final_result",
                )
            try:
                validated_final_result = validate_final_result(
                    final_result,
                    len(self.player_types),
                )
            except ValueError as exc:
                raise GameInvariantError(f"FINAL_RESULT_INVALID: {exc}") from exc
            from bglab.games.adapter_process import AdapterProcess

            verifier = AdapterProcess(self.definition)
            try:
                verifier.restore(state)
                expected_result = verifier.final_result()
            finally:
                verifier.close()
            try:
                expected_result = validate_final_result(
                    expected_result,
                    len(self.player_types),
                )
            except ValueError as exc:
                raise GameInvariantError(
                    f"FINAL_RESULT_INVALID: package verifier returned {exc}",
                ) from exc
            if expected_result != validated_final_result:
                raise GameInvariantError(
                    "FINAL_SCORE_MISMATCH: submitted result differs from package replay",
                )

        pending_before_save = self.store.read_pending_replay_turn()
        authority_decision_id = (
            state.get("decisionId") if isinstance(state, dict) else None
        )
        if not isinstance(authority_decision_id, str) or not authority_decision_id:
            authority_decision_id = turn_id
        same_pending_decision = (
            isinstance(pending_before_save, dict)
            and pending_before_save.get("turnId") == authority_decision_id
        )
        if same_pending_decision:
            # This is the pre-action snapshot, not an after-frame. Preserve
            # both the pending replay proof and candidate fence so the host can
            # redispatch the exact durable transaction. Never manufacture a
            # confirmed replay edge from two copies of the same decision.
            reconcile_state = state
            if state.get("decisionId") != authority_decision_id:
                reconcile_state = {
                    **copy.deepcopy(state),
                    "decisionId": authority_decision_id,
                }
            recovery = self.reconcile_confirmed_snapshot(reconcile_state)
            if isinstance(recovery, dict):
                return recovery
        if pending_before_save is None:
            self._reconcile_agents_against_snapshot(state)
        self.cancel_stale_ai_turns(turn_id)
        prior_record = self.store.read_snapshot()
        prior_state = (
            prior_record.get("state")
            if isinstance(prior_record, dict)
            else None
        )
        human_pending_created = None
        if pending_before_save is None:
            human_pending_created = self._prepare_frontend_human_action(
                prior_state, state,
            )
        self.store.write_snapshot(state, turn_id, metadata=metadata)
        pending_replay_turn = self.store.read_pending_replay_turn()
        if pending_replay_turn is not None:
            after_index = int(pending_replay_turn["index"]) + 1
            after_hash = self.store.write_replay_frame(after_index, state)
            complete_turn = copy.deepcopy(pending_replay_turn)
            complete_turn.update(afterFrame=after_index, afterHash=after_hash)
            self.store.append_replay_turn(complete_turn)
        elif self.store.replay_frame_count() == 0:
            self.store.write_replay_frame(0, state)
        for agent in self.agents.values():
            agent.confirm_action_snapshot(state)
            if terminal:
                previous_revision = plan_state(agent.tool_ctx)["revision"]
                end_action_plans(agent.tool_ctx, "game_finished")
                if plan_state(agent.tool_ctx)["revision"] != previous_revision:
                    agent.persist()
        self.last_confirmed_turn_id = turn_id
        if self.status.startswith("paused"):
            # A reconnect may re-send the last browser state while the runtime is
            # paused. Snapshot confirmation must not silently unpause the session.
            self.store.update_manifest(status=self.status)
        snapshot_event = {
            "type": "state_snapshot", "game_id": self.game_id,
            "turn_id": turn_id, "turn": wrapper.get("turn", 0),
            "current_player": wrapper.get("currentPlayer", 0),
            "phase": wrapper.get("phase", ""),
        }
        self._best_effort_telemetry("log_event", snapshot_event)
        _emit(self.event_callback, snapshot_event)
        if not terminal and pending_replay_turn is not None:
            self.store.clear_pending_replay_turn()
            if human_pending_created is not None:
                self._record_human_decision(human_pending_created, prior_state, state)
        if terminal:
            scores = [
                int(player["total"])
                for player in sorted(
                    validated_final_result["players"],
                    key=lambda player: player["seat"],
                )
            ]
            winner = validated_final_result["winner"]
            # Final results and the replay WAL are authoritative. Diagnostics
            # must neither delay their durability nor turn a confirmed action
            # into a failed submission.
            self.store.update_manifest(
                status="finished", winner=winner, final_scores=scores,
                final_result=validated_final_result,
                finished_at=datetime.now(timezone.utc).isoformat(),
                strategy_metrics=None, strategy_metrics_status="not_run",
                strategy_metrics_error=None,
                usage=self._usage_summary(),
            )
            if pending_replay_turn is not None:
                self.store.clear_pending_replay_turn()
            self.status = "finished"
            if human_pending_created is not None:
                self._record_human_decision(human_pending_created, prior_state, state)
            _emit(self.event_callback, {
                "type": "game_finished",
                "game_id": self.game_id,
                "turn_id": turn_id,
                "winner": winner,
                "winners": validated_final_result["winners"],
                "scores": scores,
                "final_result": validated_final_result,
            })
            if self.chat_log is not None:
                for pid, agent in self.agents.items():
                    current_batch = set(agent.tool_ctx.get('_chat_batch', []))
                    self.chat_log.mark_unanswered(pid, [item['id'] for item in agent.pending_chat if item['id'] not in current_batch], game_finished=True)
            self._record_strategy_metrics()
            for agent in self.agents.values():
                if agent._memory_extraction_enabled:
                    agent._schedule_game_memory_extraction(force=True)
            if any(item.features.skill_evolution for item in self.agent_profiles):
                self._schedule_skill_evolution()

    def _record_human_decision(self, pending: dict, before: dict | None, after: dict) -> None:
        try:
            payload = self._human_replay_decision_payload(pending, before, after)
            self._best_effort_telemetry("log_turn_decision", payload)
        except Exception as exc:
            self._best_effort_telemetry("log_event", {
                "type": "human_decision_telemetry_unavailable", "game_id": self.game_id,
                "turn_id": pending.get("turnId"), "error_type": type(exc).__name__,
            })

    def _record_strategy_metrics(self) -> None:
        metrics = None
        status = "unavailable"
        error = None
        try:
            from bglab.games.strategy_metrics import compute_store_strategy_metrics

            metrics = compute_store_strategy_metrics(self.store.dir)
            status = "available"
        except Exception as exc:
            error = type(exc).__name__
            self._best_effort_telemetry("log_event", {
                "type": "strategy_metrics_unavailable", "game_id": self.game_id,
                "stage": "compute", "error_type": error,
            })
        try:
            self.store.update_manifest(
                strategy_metrics=metrics, strategy_metrics_status=status,
                strategy_metrics_error=error,
            )
        except Exception as exc:
            self._best_effort_telemetry("log_event", {
                "type": "strategy_metrics_unavailable", "game_id": self.game_id,
                "stage": "persist", "error_type": type(exc).__name__,
            })

    def _usage_summary(self) -> dict[str, Any]:
        by_player = {
            str(pid): dict(agent.usage_totals)
            for pid, agent in sorted(self.agents.items())
        }
        totals = empty_usage_totals()
        for usage in by_player.values():
            merge_usage_totals(totals, usage)
        cache_total = totals["cache_hit_tokens"] + totals["cache_miss_tokens"]
        totals["cache_hit_rate"] = (
            round(totals["cache_hit_tokens"] / cache_total, 6)
            if cache_total else None
        )
        return {"by_player": by_player, "totals": totals}

    async def wait_for_post_game_tasks(self, timeout_seconds: float = 180.0) -> bool:
        """Let durable memory and Skill workers finish before a headless process exits."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        current = asyncio.current_task()
        while True:
            tasks = {
                task for task in self._background_tasks
                if not task.done() and task is not current
            }
            for agent in self.agents.values():
                tasks.update(
                    task for task in agent._background_tasks
                    if not task.done() and task is not current
                )
            if not tasks:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                return False

    def _schedule_skill_evolution(self) -> None:
        if not any(item.features.skill_evolution for item in self.agent_profiles):
            return
        if self._skill_evolution_scheduled:
            return
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        owner_loop = self._owner_loop or current_loop
        if owner_loop is None or owner_loop.is_closed():
            self.store.update_manifest(skill_review_status="pending")
            return
        self._skill_evolution_scheduled = True
        self.store.update_manifest(skill_review_status="in_progress")
        from bglab.games.skills.evolution import evolve_game_skills

        def create_task() -> None:
            loop = asyncio.get_running_loop()

            async def run_review() -> None:
                try:
                    await evolve_game_skills(self.store, self.engine)
                except asyncio.CancelledError:
                    self.store.update_manifest(skill_review_status="pending")
                    raise
                except Exception as exc:
                    self._skill_evolution_scheduled = False
                    self.store.update_manifest(
                        skill_review_status="pending",
                        skill_review_error=str(exc)[:300],
                    )
                    self.store.log_event({
                        "type": "skill_review_failed",
                        "game_id": self.game_id,
                        "error": str(exc)[:300],
                    })

            task = loop.create_task(
                run_review(),
                name=f"bg-skill-review-{self.game_id}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        if current_loop is owner_loop:
            create_task()
        else:
            owner_loop.call_soon_threadsafe(create_task)

    async def stop(self) -> None:
        was_finished = self.status == "finished"
        if was_finished:
            self.status = "finished"
        elif not self.status.startswith("paused"):
            self.status = "stopped"
        tasks = []
        for handle in self._turns.values():
            if not handle.action.done():
                handle.action.set_exception(GameRuntimeError("game stopped"))
            _retrieve_future_exception(handle.action)
            if not handle.completion.done():
                handle.completion.cancel()
                tasks.append(handle.completion)
            _retrieve_future_exception(handle.completion)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.agents:
            await asyncio.gather(
                *(agent.stop() for agent in self.agents.values()),
                return_exceptions=True,
            )
        background = list(self._background_tasks)
        if background:
            for task in background:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            if self._skill_evolution_scheduled:
                manifest = self.store.read_manifest()
                if manifest.get("skill_review_status") != "complete":
                    self.store.update_manifest(skill_review_status="pending")
        self._background_tasks.clear()
        self.store.update_manifest(status=self.status, usage=self._usage_summary())
