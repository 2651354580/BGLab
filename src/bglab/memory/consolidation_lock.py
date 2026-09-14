"""Consolidation lock — 对齐 services/autoDream/consolidationLock.ts。

防止多个 session 同时执行 auto-dream 时的竞态条件。
使用 lock 文件的 PID + mtime 机制:
  - lock 文件记录持有者 PID
  - 1 小时过期 (HOLDER_STALE_MS) — 防止 PID 重用误判
  - write + verify 防获取竞态
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("bglab.memory")

HOLDER_STALE_MS = 3_600_000  # 1 小时

LOCK_FILENAME = ".consolidate-lock"


def try_acquire_consolidation_lock(memory_dir: Path) -> float | None:
    ""
    lock_path = memory_dir / LOCK_FILENAME
    pid = os.getpid()

    prior_mtime: float | None = None
    holder_pid: int | None = None

    if lock_path.exists():
        try:
            lock_stat = lock_path.stat()
            prior_mtime = lock_stat.st_mtime
            raw = lock_path.read_text().strip()
            parsed = int(raw) if raw else None
            holder_pid = parsed if parsed is not None else None
        except (ValueError, OSError) as e:
            logger.warning(f"Failed to read lock file: {e}")
            lock_path.unlink(missing_ok=True)
            prior_mtime = None

    # 检查锁是否被活跃进程持有
    if prior_mtime is not None:
        age_ms = (time.time() - prior_mtime) * 1000
        if age_ms < HOLDER_STALE_MS and holder_pid is not None and _is_process_alive(holder_pid):
            logger.debug(
                f"Consolidation lock held by live PID {holder_pid} "
                f"(age: {age_ms/1000:.0f}s), skipping"
            )
            return None  # 获取失败
        logger.debug(
            f"Lock {'expired' if age_ms >= HOLDER_STALE_MS else 'held by dead PID '+str(holder_pid)}, re-acquiring"
        )

    # Write PID + verify
    memory_dir.mkdir(parents=True, exist_ok=True)
    _write_lock(lock_path, pid)
    try:
        verify = int(lock_path.read_text().strip())
        if verify == pid:
            return prior_mtime if prior_mtime is not None else 0.0
    except (ValueError, OSError):
        pass

    logger.debug("Lock write+verify failed (race detected)")
    return None  # 竞态失败


def rollback_consolidation_lock(memory_dir: Path, prior_mtime: float = 0.0) -> None:
    """回滚锁 — 对齐 rollbackConsolidationLock(priorMtime)。

    priorMtime = 0 → 删除锁文件 (恢复无文件状态)。
    priorMtime > 0 → 清空 PID 体 + 回退 mtime 到 priorMtime。
    清空 PID 是关键的——否则本进程会看起来仍在持有锁。
    """
    lock_path = memory_dir / LOCK_FILENAME
    if not lock_path.exists():
        return
    try:
        if prior_mtime == 0.0:
            lock_path.unlink()
        else:
            lock_path.write_text("")  # 清空 PID — 防止自身被误认为持有者
            os.utime(lock_path, (prior_mtime, prior_mtime))
    except OSError as e:
        logger.warning(f"Failed to rollback lock: {e}")


def read_last_consolidated_at(memory_dir: Path) -> float:
    """读取上次整合时间。对齐 readLastConsolidatedAt()。

    Returns: Unix timestamp (秒), 无锁文件返回 0。
    """
    lock_path = memory_dir / LOCK_FILENAME
    if lock_path.exists():
        try:
            return lock_path.stat().st_mtime
        except OSError:
            pass
    return 0.0


def release_consolidation_lock(memory_dir: Path) -> None:
    ""
    logger.debug("Consolidation lock released (file retained as timestamp)")


# ═══════════════════════════════════════════════════════════
# Internal
# ═══════════════════════════════════════════════════════════

def _write_lock(lock_path: Path, pid: int) -> None:
    lock_path.write_text(str(pid))


def _is_process_alive(pid: int) -> bool:
    """检查 PID 是否仍然存活 (跨平台)。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows ``os.kill(pid, 0)`` is not a POSIX-style existence
        # probe: it can terminate the target process with exit code 0.  Use
        # read-only process querying so lock acquisition can never kill its
        # own agent process.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                and exit_code.value == still_active
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
