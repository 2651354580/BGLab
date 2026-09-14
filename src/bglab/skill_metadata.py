"""Portable Skill metadata without importing either mode's Skill registry."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_HEADER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)", re.DOTALL)


def parse_yaml_mapping(text: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid Skill YAML metadata") from exc
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError("Skill metadata must be a mapping with string keys")
    return value


def read_skill_document(path: Path, *, legacy_encoding: bool = False) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        if not legacy_encoding:
            raise
        text = path.read_text(encoding="gb18030")
    match = _HEADER.match(text)
    if not match:
        return {}, text
    return parse_yaml_mapping(match.group(1)), match.group(2).strip()


def allows_implicit_invocation(metadata: dict, skill_dir: Path) -> bool:
    """Honor vendor opt-outs locally; neither API interprets these files for us."""
    disabled = metadata.get("disable-model-invocation", False)
    if not isinstance(disabled, bool):
        raise ValueError("disable-model-invocation must be a YAML boolean")
    sidecar = skill_dir / "agents" / "openai.yaml"
    allowed = True
    if sidecar.is_file():
        config = parse_yaml_mapping(sidecar.read_text(encoding="utf-8-sig"))
        policy = config.get("policy", {})
        if not isinstance(policy, dict):
            raise ValueError("Skill policy must be a mapping")
        allowed = policy.get("allow_implicit_invocation", True)
        if not isinstance(allowed, bool):
            raise ValueError("allow_implicit_invocation must be a YAML boolean")
    return allowed and not disabled
