"""Skills 加载器：按需发现用户及项目中的 SKILL.md。

扫描 user + project skills 目录，解析 SKILL.md YAML frontmatter，
返回结构化 skill 信息列表。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from bglab.skill_metadata import allows_implicit_invocation, parse_yaml_mapping, read_skill_document

logger = logging.getLogger("bglab.skills")

FRONTMATTER_RE = re.compile(r'^---\s*\n([\s\S]*?)---\s*\n?', re.MULTILINE)

# 变量替换 — 对齐 utils/argumentSubstitution.ts + loadSkillsDir.ts:344-369
# 支持 $ARGUMENTS, $ARGUMENTS[0..n], $0..$9, ${CLAUDE_SKILL_DIR}, ${CLAUDE_SESSION_ID}
_INDEXED_RE = re.compile(r'\$ARGUMENTS\[(\d+)\]')
_SHORT_INDEX_RE = re.compile(r'\$(\d+)(?!\w)')
_SKILL_DIR_RE = re.compile(r'\$\{CLAUDE_SKILL_DIR\}')
_SESSION_ID_RE = re.compile(r'\$\{CLAUDE_SESSION_ID\}')


def _split_args(args: str) -> list[str]:
    if not args or not args.strip():
        return []
    return args.split()


def _get_session_id() -> str:
    return os.environ.get("BGLAB_SESSION_ID") or str(os.getpid())


def substitute_skill_vars(
    body: str,
    args: str,
    skill_dir: str | None = None,
) -> str:
    """对齐 loadSkillsDir.ts:344-369 getPromptForCommand 中的变量替换。

    顺序: 命名 → $ARGUMENTS[i] → $i → $ARGUMENTS → ${CLAUDE_SKILL_DIR} → ${CLAUDE_SESSION_ID}
    """
    parsed = _split_args(args)

    out = _INDEXED_RE.sub(
        lambda m: parsed[int(m.group(1))] if int(m.group(1)) < len(parsed) else '',
        body,
    )
    out = _SHORT_INDEX_RE.sub(
        lambda m: parsed[int(m.group(1))] if int(m.group(1)) < len(parsed) else '',
        out,
    )
    out = out.replace('$ARGUMENTS', args)
    if skill_dir:
        sd = skill_dir.replace('\\', '/') if os.name == 'nt' else skill_dir
        out = _SKILL_DIR_RE.sub(lambda _: sd, out)
    out = _SESSION_ID_RE.sub(lambda _: _get_session_id(), out)
    return out


def _get_user_skills_dir() -> Path | None:
    """用户级 skills 目录: ~/.bglab/skills/"""
    home = Path.home() / ".bglab" / "skills"
    return home if home.is_dir() else None


def _get_project_skills_dirs(cwd: str) -> list[Path]:
    """项目级 skills 目录: 从 cwd 向上遍历到 git root 或 home，
    收集 .claude、.agents、.bglab 下的 skills 目录。

    排序：父级到子级；后扫描的近层和原生目录覆盖先前同名条目。
    """
    cwd_path = Path(cwd).resolve()
    home = Path.home()
    git_root = _find_git_root(cwd_path)
    stop_at = git_root or home

    parents: list[Path] = []
    cur = cwd_path
    while True:
        parents.append(cur)
        if cur == stop_at or cur.parent == cur:
            break
        cur = cur.parent

    return [
        directory / namespace / "skills"
        for directory in reversed(parents)
        for namespace in (".claude", ".agents", ".bglab")
        if (directory / namespace / "skills").is_dir()
    ]


def _find_git_root(cwd: Path) -> Path | None:
    """找到当前目录所属的 git 仓库根。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=str(cwd), timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def _parse_skill_md(filepath: Path) -> dict | None:
    """解析单个 SKILL.md 文件，提取 frontmatter 字段。

    Returns: {name, description, when_to_use, allowed_tools, ...} or None
    """
    try:
        frontmatter, body = read_skill_document(filepath)
        implicit = allows_implicit_invocation(frontmatter, filepath.parent)
    except Exception:
        logger.debug(f"Cannot read {filepath}")
        return None

    if not frontmatter:
        return None

    # description: frontmatter → first line of body
    description = frontmatter.get("description", "")
    if not description and body:
        description = body.split("\n")[0].strip().lstrip("# ")[:200]

    skill_dir_name = filepath.parent.name
    name = frontmatter.get("name", skill_dir_name)

    return {
        "name": name,
        "skill_dir_name": skill_dir_name,
        "description": description,
        "when_to_use": frontmatter.get("when_to_use", ""),
        "allowed_tools": frontmatter.get("allowed-tools", frontmatter.get("allowed_tools", "")),
        "argument_hint": frontmatter.get("argument-hint", frontmatter.get("argument_hint", "")),
        "model": frontmatter.get("model", ""),
        "disable_model_invocation": not implicit,
        "body": body,
    }


def _parse_yaml_simple(text: str) -> dict:
    """Compatibility entry point for the shared YAML parser."""
    return parse_yaml_mapping(text)


def _scan_skills_dir(skills_base: Path) -> list[dict]:
    """扫描一个 skills 目录，返回所有有效 skill。
    对齐 loadSkillsFromSkillsDir() — 每个子目录一个 skill。
    """
    if not skills_base.is_dir():
        return []

    results: list[dict] = []
    try:
        for entry in sorted(skills_base.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                parsed = _parse_skill_md(skill_md)
                if parsed:
                    parsed["_source_dir"] = str(entry)
                    results.append(parsed)
    except PermissionError:
        pass

    return results


def _deduplicate_by_path(skills: list[dict]) -> list[dict]:
    ""
    seen: set[str] = set()
    result: list[dict] = []
    for s in reversed(skills):
        key = s["skill_dir_name"]
        if key not in seen:
            seen.add(key)
            result.append(s)
    result.reverse()
    return result


# ═══════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════

async def load_skills(cwd: str) -> list[dict[str, str]]:
    """加载所有可用 Skills — 名字+描述。

    加载顺序（对齐 getSkillDirCommands）:
      1. User skills:  ~/.bglab/skills/
      2. Project skills: <cwd>/.../.claude/skills/
    
    去重: project skills 覆盖同名的 user skills。
    """
    all_skills: list[dict] = []

    # 1. User skills
    user_dir = _get_user_skills_dir()
    if user_dir:
        user_skills = _scan_skills_dir(user_dir)
        for s in user_skills:
            s["_source"] = "user"
        all_skills.extend(user_skills)
        logger.debug(f"load_skills: {len(user_skills)} from user ({user_dir})")

    # 2. Project skills
    project_dirs = _get_project_skills_dirs(cwd)
    for proj_dir in project_dirs:
        proj_skills = _scan_skills_dir(proj_dir)
        for s in proj_skills:
            s["_source"] = "project"
        all_skills.extend(proj_skills)
        logger.debug(f"load_skills: {len(proj_skills)} from project ({proj_dir})")

    # 去重
    all_skills = _deduplicate_by_path(all_skills)
    logger.debug(f"load_skills: {len(all_skills)} total after dedup")

    return all_skills


async def get_skill_attachment(skills: list[dict]) -> str:
    """生成 skills 的 attachment 文本 — 用于注入对话。
    
    对齐 SkillTool prompt 中的 listing 格式。
    """
    if not skills:
        return "[skills] No skills loaded"

    lines = ["# Available skills (invoke via /name or Skill tool):"]
    for s in skills:
        if s.get("disable_model_invocation") is True:
            continue
        name = s.get("name", "")
        desc = s.get("description", "")
        when = s.get("when_to_use", "")
        lines.append(f"\n## {name}")
        if desc:
            lines.append(f"  {desc}")
        if when:
            lines.append(f"  When to use: {when}")

    return "\n".join(lines)
