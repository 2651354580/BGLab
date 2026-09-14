"""Decision-local progress detection; reminders use the existing attachment slot."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any

from bglab.loop_detector import LoopDetector
from bglab.loop_detector.detector import LoopAlert, LoopSeverity


class GameLoopDetector(LoopDetector):
    """Repeated calls are evidence only when their observed result is unchanged.

    Comparing full calls, rather than tool names, allows Check/Commit and distinct
    alternatives through the same BgAct tool. The frame boundary resets this
    window. Interleaving other calls cannot erase repeated unproductive work.
    """

    def __init__(self):
        super().__init__()
        self._observations: deque[str] = deque(maxlen=14)

    def reset(self):
        super().reset()
        self._observations.clear()

    def check(self, tool_name: str, tool_input: dict, result: Any) -> LoopAlert:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                pass
        payload = json.dumps([tool_name, tool_input, result], sort_keys=True,
                             ensure_ascii=False, default=str)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        self._observations.append(fingerprint)
        count = self._observations.count(fingerprint)
        severity = (LoopSeverity.CRITICAL if count >= 5 else
                    LoopSeverity.WARNING if count >= 3 else LoopSeverity.NONE)
        return LoopAlert(severity, "decision_no_progress",
                         "The same call returned the same result.", tool_name, count)


def apply_game_loop_intervention(deps: Any, alert: Any, _blocks: list, _results: list) -> bool:
    """Signal bounded termination or queue facts for the registered attachment."""
    severity = getattr(alert, "severity", LoopSeverity.NONE)
    if severity != LoopSeverity.NONE:
        deps._game_loop_alert = {"severity": str(severity.value),
                                 "tool": alert.tool_name, "count": alert.count}
    else:
        deps._game_loop_alert = None
    recovery = getattr(getattr(deps, "query_profile", None), "recovery", None)
    if callable(getattr(recovery, "prepare_request", None)):
        from bglab.games.model_recovery import activate_recovery, recovery_state, NORMAL_REQUEST_ALLOWANCE
        if severity == LoopSeverity.CRITICAL:
            activate_recovery(deps, "tool_no_progress")
        elif recovery_state(deps)["requests"] >= NORMAL_REQUEST_ALLOWANCE:
            activate_recovery(deps, "decision_still_uncommitted")
        return False
    return severity == LoopSeverity.CRITICAL
