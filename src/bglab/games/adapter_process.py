"""Owned JSONL transport for a package Adapter process."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

import psutil

from bglab.games.registry import GameDefinition
from bglab.runtime_paths import node_executable, runtime_path


class AdapterProcess:
    """The only process wrapper that may send a state-changing dispatch."""

    def __init__(
        self,
        definition: GameDefinition,
        *,
        node_heap_mb: int = 256,
        request_timeout_s: float = 120.0,
    ) -> None:
        node = node_executable()
        bridge = runtime_path("scripts", "headless_adapter_bridge.cjs")
        if not bridge.is_file():
            raise RuntimeError("BGLab adapter bridge is missing; reinstall the application")
        command = [node, f"--max-old-space-size={node_heap_mb}"]
        if definition.adapter_script.suffix == ".ts":
            command.extend(["--import", "tsx"])
        command.extend([str(bridge), str(definition.adapter_script)])
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=definition.root, **process_options,
        )
        self._owned_windows_process = None
        if os.name == "nt":
            try:
                # Retain process identity at creation, before PID reuse can
                # make a later numeric lookup refer to another process.
                self._owned_windows_process = psutil.Process(self._process.pid)
            except psutil.NoSuchProcess:
                pass
        self._lock = threading.Lock()
        self._next_id = 0
        self._request_timeout_s = request_timeout_s

    def request(self, command: str, **payload: Any) -> Any:
        with self._lock:
            if self._process.poll() is not None:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                raise RuntimeError(f"adapter process exited: {stderr.strip()}")
            self._next_id += 1
            request_id = self._next_id
            assert self._process.stdin is not None and self._process.stdout is not None
            self._process.stdin.write(json.dumps({"id": request_id, "command": command, **payload}, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            response_lines: queue.Queue[str] = queue.Queue(maxsize=1)
            reader = threading.Thread(
                target=lambda: response_lines.put(self._process.stdout.readline()),
                name="bglab-adapter-response", daemon=True,
            )
            reader.start()
            try:
                line = response_lines.get(timeout=self._request_timeout_s)
            except queue.Empty as exc:
                self.close()
                raise RuntimeError("adapter process request timed out") from exc
            if not line:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                raise RuntimeError(f"adapter process returned no response: {stderr.strip()}")
            response = json.loads(line)
            if response.get("id") != request_id:
                raise RuntimeError("adapter process response id mismatch")
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "Adapter command failed")))
            return response.get("result")

    def start(self, config: dict) -> dict:
        return self.request("start", config=config)

    def restore(self, snapshot: dict) -> dict:
        return self.request("restore", snapshot=snapshot)

    def snapshot(self) -> dict:
        return self.request("snapshot")

    def authority_snapshot(self) -> dict:
        return self.request("authoritySnapshot")

    def final_result(self) -> dict | None:
        return self.request("finalResult")

    def view(self, seat: int) -> dict:
        return self.request("view", seat=seat)

    def coverage_catalog(self) -> dict:
        return self.request("coverageCatalog")

    def strategic_opportunity_catalog(self) -> list[dict]:
        return self.request("strategicOpportunityCatalog")

    def outcome_index(self, seat: int, request: dict | None = None) -> dict:
        return self.request("outcomeIndex", seat=seat, request=request or {})

    def enumerate_routes(
        self,
        request: dict,
        *,
        expected_decision_frame: dict | None = None,
    ) -> dict:
        if request.get("proposal") is not None and expected_decision_frame is None:
            raise RuntimeError("STALE_DECISION_FRAME")
        if expected_decision_frame is not None and request.get("decisionFrame") != expected_decision_frame:
            raise RuntimeError("STALE_DECISION_FRAME")
        result = self.request("enumerateRoutes", request=request)
        if (
            expected_decision_frame is not None
            and (not isinstance(result, dict) or result.get("decisionFrame") != expected_decision_frame)
        ):
            raise RuntimeError("STALE_DECISION_FRAME")
        return result

    def validate_transaction(self, decision_id: str, transaction: dict) -> dict:
        return self.request(
            "validateTransaction",
            decisionId=decision_id,
            transaction=transaction,
        )

    def scenario_fixture(self, spec: dict) -> dict:
        """Ask the package-owned test fixture builder for a validated snapshot."""
        return self.request("scenarioFixture", spec=spec)

    def dispatch(self, decision_id: str, transaction: dict) -> dict:
        return self.request("dispatch", decisionId=decision_id, transaction=transaction)

    def close(self) -> None:
        process = self._process
        if process.poll() is not None:
            return
        if os.name == "nt":
            root = self._owned_windows_process
            if root is None:
                process.kill()  # Popen retains the original process handle.
            else:
                try:
                    owned = list(reversed(root.children(recursive=True))) + [root]
                except psutil.NoSuchProcess:
                    owned = [root]
                for child in owned:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(owned, timeout=3)
                if alive:
                    raise RuntimeError("ADAPTER_PROCESS_CLEANUP_INCOMPLETE")
            process.wait(timeout=3)
            return
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)
