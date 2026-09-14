from __future__ import annotations

import json
import heapq
import itertools
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Callable, Hashable, Iterable, Mapping, Sequence

from .model import SemanticChain


RouteToken = tuple[str, str]
RouteTokens = tuple[RouteToken, ...]
RouteCost = tuple[int, int, int]


# The submitted chain is the model's stated intent; an Authority route is a
# legal completion of that intent.  The two sides are therefore deliberately
# asymmetric:
#
# - inserting a missing Authority action is cheap (the model may omit a
#   required continuation or derived step);
# - deleting an action/field explicitly supplied by the model is expensive;
# - changing an explicitly supplied action or field is also expensive.
#
# This is a weighted edit distance over the complete submitted JSON surface,
# not a strategy score.  It cannot invent intent that the model did not state.
_INSERT_ACTION_COST = 1
_DELETE_ACTION_COST = 8
_CHANGE_ACTION_COST = 8
_CHANGE_FIELD_COST = 3
# Ranking is lexicographic rather than a single blended score:
#
# 1. preserve every explicit model action and field;
# 2. among equally faithful completions, prefer executing an opened action over
#    inserting ``finish_action`` and abandoning it;
# 3. only then minimize ordinary mandatory completion steps.
#
# This prevents either a very short abandonment or a very long completion from
# overpowering the model's stated intent merely because route lengths differ.
_ZERO_COST: RouteCost = (0, 0, 0)


def _add_cost(left: RouteCost, right: RouteCost) -> RouteCost:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _model_visible_arguments(encoded: str) -> dict[str, Any] | None:
    try:
        arguments = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return {
        key: value
        for key, value in arguments.items()
        if not key.startswith("$")
    }


def _model_visible_token(token: RouteToken) -> RouteToken:
    kind, encoded_arguments = token
    arguments = _model_visible_arguments(encoded_arguments)
    return kind, _canonical_json(arguments) if arguments is not None else encoded_arguments


def canonical_chain_tokens(chain: SemanticChain) -> RouteTokens:
    if not isinstance(chain, SemanticChain):
        raise TypeError("route tokenization requires SemanticChain")
    tokens: list[RouteToken] = []
    for action in chain.actions:
        payload = action.to_dict()
        arguments = payload.get("args", {})
        if not isinstance(arguments, dict):
            raise TypeError("semantic action arguments must be an object")
        # One automaton symbol is one model-owned semantic action. This keeps
        # Wagner-Fischer alignment on action boundaries while the canonical
        # argument object still makes every field and value participate.
        tokens.append((f"action:{action.action}", _canonical_json(arguments)))
    return tuple(tokens)


@dataclass(frozen=True, slots=True)
class RouteMatch:
    distance: int
    matched_action_prefix: int
    tokens: RouteTokens


@dataclass(frozen=True, slots=True)
class _State:
    final: bool
    transitions: Mapping[RouteToken, int]


class _TrieNode:
    __slots__ = ("final", "children")

    def __init__(self) -> None:
        self.final = False
        self.children: dict[RouteToken, _TrieNode] = {}


def _validate_tokens(tokens: Iterable[RouteToken]) -> RouteTokens:
    value = tuple(tokens)
    for token in value:
        if (
            not isinstance(token, tuple)
            or len(token) != 2
            or not all(isinstance(item, str) for item in token)
        ):
            raise TypeError("route tokens must be pairs of strings")
    return value


class RouteAutomaton:
    def __init__(
        self,
        *,
        start_state: int,
        states: tuple[_State, ...],
        words: tuple[RouteTokens, ...] | None,
        word_count: int,
    ) -> None:
        self._start_state = start_state
        self._states = states
        self._words = words
        self._word_count = word_count

    @classmethod
    def build(cls, words: Iterable[Iterable[RouteToken]]) -> "RouteAutomaton":
        normalized = tuple(sorted({_validate_tokens(word) for word in words}))
        root = _TrieNode()
        for word in normalized:
            node = root
            for token in word:
                node = node.children.setdefault(token, _TrieNode())
            node.final = True

        states: list[_State] = []
        registry: dict[tuple[bool, tuple[tuple[RouteToken, int], ...]], int] = {}

        def intern(node: _TrieNode) -> int:
            transitions = tuple(
                (token, intern(child))
                for token, child in sorted(node.children.items())
            )
            signature = node.final, transitions
            existing = registry.get(signature)
            if existing is not None:
                return existing
            state_id = len(states)
            states.append(_State(
                final=node.final,
                transitions=MappingProxyType(dict(transitions)),
            ))
            registry[signature] = state_id
            return state_id

        start_state = intern(root)
        return cls(
            start_state=start_state,
            states=tuple(states),
            words=normalized,
            word_count=len(normalized),
        )

    @classmethod
    def from_compact(
        cls,
        *,
        start_state: int,
        states: Sequence[Mapping[str, Any]],
        word_count: int,
    ) -> "RouteAutomaton":
        if isinstance(start_state, bool) or not isinstance(start_state, int):
            raise ValueError("compact route start state must be an integer")
        if not isinstance(states, Sequence) or isinstance(states, (str, bytes)):
            raise ValueError("compact route states must be a sequence")
        if not states or not 0 <= start_state < len(states):
            raise ValueError("compact route start state is out of range")
        if isinstance(word_count, bool) or not isinstance(word_count, int) or word_count < 1:
            raise ValueError("compact route word count must be positive")

        parsed: list[_State] = []
        for state_index, raw_state in enumerate(states):
            if not isinstance(raw_state, Mapping):
                raise ValueError("compact route state must be an object")
            final = raw_state.get("final")
            raw_transitions = raw_state.get("transitions")
            if not isinstance(final, bool) or not isinstance(raw_transitions, Sequence):
                raise ValueError("compact route state must declare final and transitions")
            transitions: dict[RouteToken, int] = {}
            for raw_edge in raw_transitions:
                if not isinstance(raw_edge, Mapping):
                    raise ValueError("compact route transition must be an object")
                action = raw_edge.get("action")
                arguments = raw_edge.get("args")
                target = raw_edge.get("to")
                if not isinstance(action, str) or not action:
                    raise ValueError("compact route transition action must be non-empty")
                if not isinstance(arguments, Mapping):
                    raise ValueError("compact route transition args must be an object")
                if (
                    isinstance(target, bool)
                    or not isinstance(target, int)
                    or not 0 <= target < len(states)
                ):
                    raise ValueError("compact route transition target is out of range")
                token = (f"action:{action}", _canonical_json(dict(arguments)))
                if token in transitions:
                    raise ValueError(
                        f"compact route state {state_index} has duplicate transition"
                    )
                transitions[token] = target
            parsed.append(_State(
                final=final,
                transitions=MappingProxyType(dict(sorted(transitions.items()))),
            ))

        visiting: set[int] = set()
        visited: set[int] = set()

        @lru_cache(maxsize=None)
        def count_paths(state_id: int) -> int:
            if state_id in visiting:
                raise ValueError("compact route automaton must be acyclic")
            visiting.add(state_id)
            try:
                state = parsed[state_id]
                total = 1 if state.final else 0
                for target in state.transitions.values():
                    total += count_paths(target)
                visited.add(state_id)
                return total
            finally:
                visiting.remove(state_id)

        actual_word_count = count_paths(start_state)
        if len(visited) != len(parsed):
            raise ValueError("compact route automaton contains unreachable states")
        if actual_word_count != word_count:
            raise ValueError("compact route word count does not match the graph")
        return cls(
            start_state=start_state,
            states=tuple(parsed),
            words=None,
            word_count=word_count,
        )

    @property
    def word_count(self) -> int:
        return self._word_count

    @property
    def state_count(self) -> int:
        return len(self._states)

    @property
    def transition_count(self) -> int:
        return sum(len(state.transitions) for state in self._states)

    def accepts(self, tokens: Iterable[RouteToken]) -> bool:
        state_id = self._start_state
        for token in _validate_tokens(tokens):
            next_state = self._states[state_id].transitions.get(token)
            if next_state is None:
                return False
            state_id = next_state
        return self._states[state_id].final

    @staticmethod
    def _candidate_insertion_cost(token: RouteToken) -> RouteCost:
        if token[0] == "action:finish_action":
            return (0, 1, 0)
        return (0, 0, _INSERT_ACTION_COST)

    @staticmethod
    def _submitted_deletion_cost(token: RouteToken) -> RouteCost:
        del token
        return (_DELETE_ACTION_COST, 0, 0)

    @staticmethod
    def _substitution_cost(
        candidate: RouteToken,
        submitted: RouteToken,
    ) -> RouteCost:
        if _model_visible_token(candidate) == _model_visible_token(submitted):
            return _ZERO_COST
        candidate_kind, candidate_value = candidate
        submitted_kind, submitted_value = submitted
        if candidate_kind != submitted_kind:
            return (
                _CHANGE_ACTION_COST,
                int(candidate_kind == "action:finish_action"),
                0,
            )
        candidate_arguments = _model_visible_arguments(candidate_value)
        submitted_arguments = _model_visible_arguments(submitted_value)
        if candidate_arguments is None or submitted_arguments is None:
            return (_CHANGE_FIELD_COST, 0, 0)
        changed_fields = sum(
            candidate_arguments.get(key, object())
            != submitted_arguments.get(key, object())
            for key in set(candidate_arguments) | set(submitted_arguments)
        )
        return (
            min(
                _CHANGE_ACTION_COST,
                max(1, changed_fields) * _CHANGE_FIELD_COST,
            ),
            0,
            0,
        )

    @staticmethod
    def _advance_row(
        query: RouteTokens,
        previous: tuple[RouteCost, ...],
        token: RouteToken,
    ) -> tuple[RouteCost, ...]:
        insertion_cost = RouteAutomaton._candidate_insertion_cost(token)
        current = [_add_cost(previous[0], insertion_cost)]
        for index, query_token in enumerate(query, start=1):
            current.append(min(
                _add_cost(
                    current[index - 1],
                    RouteAutomaton._submitted_deletion_cost(query_token),
                ),
                _add_cost(previous[index], insertion_cost),
                _add_cost(
                    previous[index - 1],
                    RouteAutomaton._substitution_cost(token, query_token),
                ),
            ))
        return tuple(current)

    @staticmethod
    def _matched_action_prefix(left: RouteTokens, right: RouteTokens) -> int:
        """Count complete identical semantic actions at the route start."""

        matched = 0
        for left_action, right_action in zip(left, right):
            if _model_visible_token(left_action) != _model_visible_token(right_action):
                break
            matched += 1
        return matched

    def nearest(
        self,
        query: Iterable[RouteToken],
        *,
        limit: int = 5,
        max_expansions: int = 250_000,
        distinct_key: Callable[[RouteTokens], Hashable] | None = None,
    ) -> tuple[RouteMatch, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("route match limit must be positive")
        if (
            isinstance(max_expansions, bool)
            or not isinstance(max_expansions, int)
            or max_expansions < 1
        ):
            raise ValueError("route match expansion budget must be positive")
        if distinct_key is not None and not callable(distinct_key):
            raise TypeError("route match distinct_key must be callable")
        query_tokens = _validate_tokens(query)
        initial_costs = [_ZERO_COST]
        for token in query_tokens:
            initial_costs.append(
                _add_cost(initial_costs[-1], self._submitted_deletion_cost(token))
            )
        initial_row = tuple(initial_costs)
        infinity: RouteCost = (10**18, 10**18, 10**18)

        @lru_cache(maxsize=None)
        def suffix_cost(state_id: int, query_index: int) -> RouteCost:
            state = self._states[state_id]
            best = infinity
            if state.final:
                best = _ZERO_COST
                for token in query_tokens[query_index:]:
                    best = _add_cost(best, self._submitted_deletion_cost(token))
            if query_index < len(query_tokens):
                best = min(
                    best,
                    _add_cost(
                        self._submitted_deletion_cost(query_tokens[query_index]),
                        suffix_cost(state_id, query_index + 1),
                    ),
                )
            for token, child_id in state.transitions.items():
                best = min(
                    best,
                    _add_cost(
                        self._candidate_insertion_cost(token),
                        suffix_cost(child_id, query_index),
                    ),
                )
                if query_index < len(query_tokens):
                    best = min(
                        best,
                        _add_cost(
                            self._substitution_cost(token, query_tokens[query_index]),
                            suffix_cost(child_id, query_index + 1),
                        ),
                    )
            return best

        @lru_cache(maxsize=None)
        def minimum_suffix(state_id: int) -> RouteTokens | None:
            state = self._states[state_id]
            options: list[RouteTokens] = [()] if state.final else []
            for token, child_id in state.transitions.items():
                child = minimum_suffix(child_id)
                if child is not None:
                    options.append((token, *child))
            return min(options) if options else None

        def maximum_prefix_match(state_id: int, prefix: RouteTokens) -> int:
            matched = self._matched_action_prefix(query_tokens, prefix)
            if matched != len(prefix) or matched >= len(query_tokens):
                return matched
            current = state_id
            while matched < len(query_tokens):
                target = self._states[current].transitions.get(query_tokens[matched])
                if target is None:
                    break
                current = target
                matched += 1
            return matched

        def branch_key(
            state_id: int,
            row: tuple[RouteCost, ...],
            prefix: RouteTokens,
        ) -> tuple[RouteCost, int, RouteTokens]:
            lower_distance = min(
                _add_cost(row[index], suffix_cost(state_id, index))
                for index in range(len(query_tokens) + 1)
            )
            suffix = minimum_suffix(state_id)
            if suffix is None:
                return infinity, 0, prefix
            return (
                lower_distance,
                -maximum_prefix_match(state_id, prefix),
                (*prefix, *suffix),
            )

        # Every accepted word has one path in a deterministic automaton.  Heap
        # branches carry an exact lower bound for every completion beneath that
        # prefix, so completed routes are emitted in the same order as the old
        # exhaustive sort without materialising the accepted language.
        counter = itertools.count()
        heap: list[tuple[
            tuple[RouteCost, int, RouteTokens],
            int,
            int,
            int | None,
            tuple[RouteCost, ...] | None,
            RouteTokens,
        ]] = []
        heapq.heappush(heap, (
            branch_key(self._start_state, initial_row, ()),
            0,
            next(counter),
            self._start_state,
            initial_row,
            (),
        ))
        matches: list[RouteMatch] = []
        seen_distinct_keys: set[Hashable] = set()
        expansions = 0
        while heap and len(matches) < limit:
            rank, kind, _, state_id, row, prefix = heapq.heappop(heap)
            if kind == 1:
                if distinct_key is not None:
                    key = distinct_key(prefix)
                    try:
                        if key in seen_distinct_keys:
                            continue
                        seen_distinct_keys.add(key)
                    except TypeError as exc:
                        raise TypeError(
                            "route match distinct_key must return a hashable value"
                        ) from exc
                matches.append(RouteMatch(
                    distance=sum(rank[0]),
                    matched_action_prefix=-rank[1],
                    tokens=prefix,
                ))
                continue
            expansions += 1
            if expansions > max_expansions:
                raise RuntimeError("compact route search expansion budget exhausted")
            if state_id is None or row is None:
                raise RuntimeError("compact route search heap is malformed")
            state = self._states[state_id]
            if state.final:
                matched = self._matched_action_prefix(query_tokens, prefix)
                result_rank = (row[-1], -matched, prefix)
                heapq.heappush(heap, (
                    result_rank,
                    1,
                    next(counter),
                    None,
                    None,
                    prefix,
                ))
            for token, child_id in state.transitions.items():
                child_prefix = (*prefix, token)
                child_row = self._advance_row(query_tokens, row, token)
                heapq.heappush(heap, (
                    branch_key(child_id, child_row, child_prefix),
                    0,
                    next(counter),
                    child_id,
                    child_row,
                    child_prefix,
                ))
        return tuple(matches)


__all__ = [
    "RouteAutomaton",
    "RouteMatch",
    "RouteToken",
    "RouteTokens",
    "canonical_chain_tokens",
]
