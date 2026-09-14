"""Memory 目录操作 — 对齐 memdir/memdir.ts + memoryScan.ts + frontmatterParser.ts。

MEMORY.md 索引格式, memory file frontmatter (YAML), scan/read/write 操作。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from datetime import datetime

import yaml

logger = logging.getLogger("bglab.memory")

DIR_EXISTS_GUIDANCE = (
    "This directory already exists and you can write to it directly with the Write tool "
    "(do not run mkdir or check for its existence)."
)

FRONTMATTER_RE = re.compile(r'^---\s*\n([\s\S]*?)---\s*\n?', re.MULTILINE)

MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000

VALID_TYPES = {"user", "feedback", "project", "reference", "game"}
GAME_MEMORY_ACTIVE_STATUSES = {"active", "validated"}


def get_memory_dir(cwd: str | None = None) -> Path:
    """获取 auto memory 目录。对齐 getAutoMemPath()。"""
    home = Path.home() / ".bglab" / "memory"
    if cwd:
        sanitized = re.sub(r'[^a-zA-Z0-9_\-]', '-', str(cwd))
        sanitized = sanitized.strip('-')[:100]
        home = home / sanitized
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_game_memory_dir(engine: str, agent_id: str | None = None) -> Path:
    """获取游戏记忆目录，并按持久 AI 身份隔离个人经验。"""
    home = Path.home() / ".bglab" / "memory" / "games" / engine
    if agent_id:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(agent_id)).strip("-.")
        home = home / "agents" / (safe_id or "default")
    home.mkdir(parents=True, exist_ok=True)
    return home


def load_entrypoint(
    memory_dir: Path,
    *,
    max_lines: int = MAX_ENTRYPOINT_LINES,
    max_bytes: int = MAX_ENTRYPOINT_BYTES,
) -> str | None:
    """读取 MEMORY.md 索引内容。对齐 loadMemoryPrompt → truncateEntrypointContent()。

    Returns: 截断后的内容, 不存在返回 None。
    """
    ep = memory_dir / "MEMORY.md"
    if not ep.is_file():
        return None

    raw = ep.read_text(encoding="utf-8").strip()
    total_lines = len(raw.split('\n'))
    total_bytes = len(raw.encode("utf-8"))

    # 截断逻辑 — 对齐 truncateEntrypointContent()
    line_truncated = total_lines > max_lines
    byte_truncated = total_bytes > max_bytes

    if not line_truncated and not byte_truncated:
        return raw

    content_lines = raw.split('\n')
    if line_truncated:
        content_lines = content_lines[:max_lines]

    content = '\n'.join(content_lines)
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > max_bytes:
        cut = content.rfind('\n', 0, max_bytes)
        content = content[:cut] if cut > 0 else content[:max_bytes]

    
    if byte_truncated and not line_truncated:
        reason = f"too large ({total_bytes} bytes, limit: {max_bytes}) — index entries are too long"
    elif line_truncated and not byte_truncated:
        reason = f"{total_lines} lines (limit: {max_lines})"
    else:
        reason = f"{total_lines} lines and {total_bytes} bytes"

    content += (
        f"\n\n> WARNING: MEMORY.md is {reason}. "
        "Only part of it was loaded. Keep index entries to one line under ~200 chars; "
        "move detail into topic files."
    )

    return content


def scan_memory_files(memory_dir: Path) -> list[dict]:
    """扫描 memory 目录下的所有 .md 文件（不含 MEMORY.md）。对齐 scanMemoryFiles()。

    对齐 memoryScan.ts MemoryHeader: filename, filePath, mtimeMs, description, type。
    """
    results = []
    for f in sorted(memory_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            frontmatter = _parse_frontmatter(f)
            evidence_games = frontmatter.get("evidence_games", [])
            if isinstance(evidence_games, str):
                try:
                    parsed = yaml.safe_load(evidence_games)
                    evidence_games = parsed if isinstance(parsed, list) else []
                except yaml.YAMLError:
                    evidence_games = []
            results.append({
                "filename": f.name,
                "filePath": str(f),
                "mtimeMs": int(stat.st_mtime * 1000),
                "name": frontmatter.get("name", f.stem),
                "description": frontmatter.get("description", ""),
                "type": frontmatter.get("type", "project"),
                "status": frontmatter.get("status", "legacy"),
                "confidence": frontmatter.get("confidence", ""),
                "evidenceGames": [str(value) for value in evidence_games],
                "evidenceCount": int(frontmatter.get("evidence_count", 0) or 0),
            })
        except Exception:
            continue
    return results


def sync_memory_index(
    memory_dir: Path, *, max_entries: int = 40, active_only: bool = False,
) -> str:
    """Atomically rebuild ``MEMORY.md`` from the typed topic files."""
    scanned = scan_memory_files(memory_dir)
    if active_only:
        scanned = [
            item for item in scanned
            if item.get("status") in GAME_MEMORY_ACTIVE_STATUSES
        ]
    memories = sorted(
        scanned,
        key=lambda item: item.get("mtimeMs", 0),
        reverse=True,
    )[:max(0, max_entries)]
    lines: list[str] = []
    for memory in memories:
        name = str(memory.get("name") or Path(memory["filename"]).stem)
        description = " ".join(str(memory.get("description") or "").split())
        if len(description) > 150:
            description = description[:147].rstrip() + "..."
        line = f"- [{name}]({memory['filename']})"
        if description:
            line += f" — {description}"
        lines.append(line)
    content = "\n".join(lines) + ("\n" if lines else "")
    memory_dir.mkdir(parents=True, exist_ok=True)
    target = memory_dir / "MEMORY.md"
    fd, tmp_name = tempfile.mkstemp(prefix=".MEMORY.md.", dir=str(memory_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return content


def normalize_game_memory_lifecycle(
    memory_dir: Path,
    *,
    evidence_game_id: str = "",
    changed_filenames: set[str] | None = None,
) -> dict[str, int]:
    """Quarantine one-game claims and enforce evidence-based promotion.

    Legacy game files are deliberately migrated to candidates. A maintenance
    fork may request promotion, but deterministic validation only permits it
    after evidence from distinct games reaches the required threshold.
    """
    metrics = {
        "normalized": 0, "candidates": 0, "active": 0,
        "validated": 0, "retired": 0,
    }
    changed = changed_filenames or set()
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        frontmatter = _parse_frontmatter(path)
        if frontmatter.get("type") != "game":
            continue
        body = read_memory_file(str(path)) or ""
        raw_evidence = frontmatter.get("evidence_games", [])
        if isinstance(raw_evidence, str):
            try:
                parsed = yaml.safe_load(raw_evidence)
                raw_evidence = parsed if isinstance(parsed, list) else []
            except yaml.YAMLError:
                raw_evidence = []
        evidence = list(dict.fromkeys(str(v).strip() for v in raw_evidence if str(v).strip()))
        if evidence_game_id and path.name in changed and evidence_game_id not in evidence:
            evidence.append(evidence_game_id)

        requested = str(frontmatter.get("status", "candidate")).lower()
        if requested == "retired":
            status = "retired"
            confidence = "retired"
        elif len(evidence) >= 3 and requested in {"active", "validated"}:
            status = "validated"
            confidence = "validated"
        elif len(evidence) >= 2 and requested in {"active", "validated"}:
            status = "active"
            confidence = "supported"
        else:
            status = "candidate"
            confidence = "tentative"

        normalized = dict(frontmatter)
        normalized.update({
            "type": "game",
            "status": status,
            "confidence": confidence,
            "evidence_games": evidence,
            "evidence_count": len(evidence),
        })
        expected_changed = normalized != frontmatter
        if expected_changed:
            normalized["last_verified"] = datetime.now().astimezone().isoformat(timespec="seconds")
            write_memory_file(memory_dir, path.name, normalized, body)
            metrics["normalized"] += 1
        metrics[status if status != "candidate" else "candidates"] += 1
    return metrics


def read_memory_file(filepath: str) -> str | None:
    """读取一个 memory 文件的完整内容（去掉 frontmatter 用于注入）。"""
    try:
        content = Path(filepath).read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(content)
        if match:
            return content[match.end():].strip()
        return content.strip()
    except Exception as e:
        logger.warning(f"Failed to read memory file {filepath}: {e}")
        return None


MAX_MEMORY_LINES = 200
MAX_MEMORY_BYTES = 4_000  


def read_memories_for_surfacing(
    selected: list[dict],
    *,
    max_bytes: int = MAX_MEMORY_BYTES,
    max_lines: int = MAX_MEMORY_LINES,
) -> list[dict]:
    """读取选中的 memory 文件用于 attachment 注入。

    对齐 readMemoriesForSurfacing():
      - 截断到 MAX_MEMORY_LINES 行 / MAX_MEMORY_BYTES 字节
      - 截断时追加提示: 让模型用 Read 工具看完整文件
      - 返回 header (freshness) 信息

    Returns: [{path, filename, content, mtimeMs, header, limit?}, ...]
    """
    results = []
    for m in selected:
        content = read_memory_file(m["filePath"])
        if not content:
            continue

        lines = content.split('\n')
        truncated = False
        byte_limit = max(1, int(max_bytes))

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True

        content_out = '\n'.join(lines)
        content_bytes = content_out.encode("utf-8")
        if len(content_bytes) > byte_limit:
            cut = content_out.rfind('\n', 0, byte_limit)
            content_out = content_out[:cut] if cut > 0 else content_out[:byte_limit]
            truncated = True

        if truncated:
            content_out += (
                f"\n\n> This memory file was truncated "
                f"({'first %d lines' % max_lines if len(lines) >= max_lines else '%d byte limit' % byte_limit}). "
                f"Use the Read tool to view the complete file at: {m['filePath']}"
            )

        results.append({
            "path": m["filePath"],
            "filename": m["filename"],
            "content": content_out,
            "mtimeMs": m["mtimeMs"],
            "header": _memory_header(m["filePath"], m.get("mtimeMs", 0)),
        })
    return results


def _memory_header(filepath: str, mtime_ms: int) -> str:
    """构建 memory attachment header。对齐 memoryHeader()。"""
    import datetime
    age_s = time.time() - (mtime_ms / 1000)
    if age_s < 3600:
        staleness = f"Memory (updated {int(age_s / 60)} minutes ago)"
    elif age_s < 86400:
        staleness = f"Memory (updated {int(age_s / 3600)} hours ago)"
    elif age_s < 86400 * 30:
        staleness = f"Memory (updated {int(age_s / 86400)} days ago)"
    else:
        staleness = ""
    if staleness:
        return f"{staleness}\n\nMemory: {filepath}:"
    return f"Memory: {filepath}:"


def write_memory_file(
    memory_dir: Path,
    filename: str,
    frontmatter: dict,
    content: str,
) -> str:
    ""
    filepath = memory_dir / filename

    # 验证 filename 安全
    safe = re.sub(r'[^a-zA-Z0-9_.\-]', '_', filename)
    if safe != filename:
        logger.warning(f"Sanitized filename: {filename} → {safe}")
        filename = safe
        filepath = memory_dir / filename

    # 构建 frontmatter (YAML 格式)
    fm_text = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False,
    ).strip()
    full = "---\n" + fm_text + "\n---\n\n" + content.strip() + "\n"

    # 原子写入: 写临时文件 → rename
    tmp = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(memory_dir), suffix=".tmp")
        tmp = Path(tmp_path)
        os.write(fd, full.encode("utf-8"))
        os.close(fd)
        tmp.replace(filepath)
        logger.debug(f"Written memory file: {filename}")
    except Exception:
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

    return str(filepath)


def update_entrypoint(memory_dir: Path, filename: str, hook: str) -> None:
    """更新 MEMORY.md 索引，添加一行。对齐 insertMessageChain。"""
    ep = memory_dir / "MEMORY.md"
    line = f"- [{_title_from_filename(filename)}]({filename}) — {hook}\n"

    if ep.exists():
        existing = ep.read_text(encoding="utf-8")
        lines = existing.split('\n')
        if len(lines) >= MAX_ENTRYPOINT_LINES:
            existing = existing.strip() + "\n" + line
        else:
            existing = existing.rstrip() + "\n" + line
        ep.write_text(existing, encoding="utf-8")
    else:
        ep.write_text(line, encoding="utf-8")


# ═══════════════════════════════════════════════════════════
# Frontmatter 解析 — 对齐 frontmatterParser.ts
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(filepath: Path) -> dict:
    """解析 memory 文件 frontmatter。对齐 parseFrontmatter() + parseMemoryType()。

    策略:
      1. 正则提取 frontmatter block
      2. yaml.safe_load 解析
      3. 失败则用 quoteProblematicValues 重试 (行级 fallback)
      4. 验证 type 字段是 4 种合法值之一
    """
    content = filepath.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}

    fm_text = match.group(1)

    # 尝试 YAML 解析
    result = _try_parse_yaml(fm_text)
    if result is None:
        # YAML 失败, fallback 到行级解析 (对齐 quoteProblematicValues 重试)
        result = _fallback_parse(fm_text)

    # 验证 type — 对齐 parseMemoryType()
    mem_type = result.get("type", "")
    if mem_type not in VALID_TYPES:
        logger.debug(f"Invalid memory type '{mem_type}' in {filepath.name}, falling back to 'project'")
        result["type"] = "project"

    return result


def _try_parse_yaml(fm_text: str) -> dict | None:
    """用 yaml.safe_load 解析 frontmatter。失败返回 None。"""
    try:
        data = yaml.safe_load(fm_text)
        if isinstance(data, dict):
            return {k: v if v is not None else "" for k, v in data.items()}
        return None
    except yaml.YAMLError:
        return None


def _fallback_parse(fm_text: str) -> dict:
    """行级 fallback 解析 — 对齐 quoteProblematicValues retry。"""
    result = {}
    for line in fm_text.split('\n'):
        line = line.strip()
        if ':' in line:
            k, v = line.split(':', 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            result[k] = v
    return result


def _needs_yaml_quoting(s: str) -> bool:
    """判断字符串是否需要 YAML 引号 (含特殊字符)。"""
    return bool(re.search(r'[:#\{\}\[\],&*?|><!%@`]', s))


def _title_from_filename(filename: str) -> str:
    return filename.replace('.md', '').replace('_', ' ')
