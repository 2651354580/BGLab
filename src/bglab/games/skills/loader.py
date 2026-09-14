from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from bglab.skill_metadata import allows_implicit_invocation, read_skill_document


_CONDITION_RE = re.compile(
    r"^[\s\w<>=!().+-]+(?:AND|OR|and|or)?[\s\w<>=!().+-]*$"
)


class SkillBundleError(ValueError):
    """Raised when an immutable game Skill bundle cannot be verified."""


@dataclass(frozen=True)
class GameSkillBundle:
    engine: str
    version: str
    root: Path | None
    skills: Mapping[str, Mapping[str, str]]
    fingerprint: str


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    return read_skill_document(path, legacy_encoding=True)


def _score(player: dict, state: dict) -> int:
    explicit = player.get("score")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return int(explicit)
    total = 0
    carddb = state.get("carddb", {})
    for card in player.get("boughtCards", []):
        points = card.get("points")
        if not isinstance(points, (int, float)):
            points = carddb.get(str(card.get("id", 0)), {}).get("points", 0)
        total += int(points or 0)
    return total + len(player.get("boughtNobles", [])) * 3


def _discounts(player: dict, state: dict) -> dict[str, int]:
    colors = ["C", "S", "E", "R", "O"]
    result: dict[str, int] = {}
    carddb = state.get("carddb", {})
    for card in player.get("boughtCards", []):
        color = card.get("color", card.get("type", card.get("type2")))
        if color is None:
            color = carddb.get(str(card.get("id", 0)), {}).get("type")
        if isinstance(color, int) and not isinstance(color, bool):
            color = colors[color] if 0 <= color < len(colors) else None
        if color in colors:
            result[color] = result.get(color, 0) + 1
    return result


def extract_features(state: dict, seat: int = 0) -> dict:
    features = {}
    gs = state.get("gamestorage", {})
    package_game = state.get("game", {})
    package_players = package_game.get("players", []) if isinstance(package_game, dict) else []
    players = package_players if isinstance(package_players, list) and package_players else state.get("playerstorage", [])
    features["round"] = package_game.get(
        "round", gs.get("turn", state.get("wrapper", {}).get("turn", 0)),
    )
    wrapper = state.get("wrapper", {})
    features["turn"] = wrapper.get("turn", 0)
    features["player_count"] = wrapper.get(
        "pc", wrapper.get("playerCount", len(players)),
    )
    me = players[seat] if 0 <= seat < len(players) else {}
    my_score = _score(me, state) if me else 0
    opponent_score = max(
        (_score(player, state) for index, player in enumerate(players) if index != seat),
        default=0,
    )
    my_discounts = _discounts(me, state) if me else {}
    features["my_score"] = my_score
    features["opponent_score"] = opponent_score
    features["opponent_score_diff"] = my_score - opponent_score
    features["max_discount"] = max(my_discounts.values(), default=0)
    bought_cards = list(me.get("boughtCards", [])) if me else []
    features["purchased_cards"] = len(bought_cards)
    features["purchased_points"] = sum(
        int(card.get("points", 0) or 0) for card in bought_cards
    )
    features["zero_point_level_one_cards"] = sum(
        int(card.get("points", 0) or 0) == 0
        and int(card.get("lvl", card.get("level", 0)) or 0) == 1
        for card in bought_cards
    )
    if me:
        gems = me.get("gems", me)
        reserved = me.get("reservedCards", me.get("storedCards", []))
        features["token_count"] = sum(gems.get(c, 0) for c in ["C", "S", "E", "R", "O", "G"])
        features["reserved_count"] = len(reserved)
    else:
        features["token_count"] = 0
        features["reserved_count"] = 0
    features["any_score"] = max(
        (_score(player, state) for player in players),
        default=0,
    )
    noble_costs = [n.get("cost", {}) for n in gs.get("nobles", [])]
    if noble_costs:
        min_distance = min(
            sum(max(0, nc.get(k, 0) - my_discounts.get(k, 0)) for k in ["C", "S", "E", "R", "O"])
            for nc in noble_costs
        )
        features["noble_distance"] = min_distance
    public_game = state.get("adapterView", {}).get("publicState")
    if not isinstance(public_game, dict):
        public_game = package_game if isinstance(package_game, dict) else {}
    factories = public_game.get("factories", [])
    center = public_game.get("center", [])
    if isinstance(factories, list) and isinstance(center, list):
        features["nonempty_sources"] = sum(bool(source) for source in factories) + bool(center)
        features["center_tile_count"] = len(center)
        center_counts: dict[str, int] = {}
        for tile in center:
            color = str(tile)
            center_counts[color] = center_counts.get(color, 0) + 1
        features["center_max_color_count"] = max(center_counts.values(), default=0)
    return features


def evaluate_condition(condition: str, state: dict, seat: int = 0) -> bool:
    if not condition:
        return False
    if not _CONDITION_RE.fullmatch(condition):
        return False
    features = extract_features(state, seat=seat)
    expr = condition
    bool_map = {
        "early": "turn <= 5",
        "midgame": "turn > 5 AND turn <= 12",
    }
    for pattern, replacement in bool_map.items():
        if pattern in expr:
            expr = expr.replace(pattern, replacement)
    for key, val in features.items():
        expr = re.sub(rf"\b{re.escape(key)}\b", str(val), expr)
    try:
        safe_expr = expr.replace("AND", " and ").replace("OR", " or ")
        return bool(eval(safe_expr, {"__builtins__": {}}, {}))
    except Exception:
        return False


SKILL_DIR = Path(__file__).parent
_DISABLED_SKILL_ROOT = Path("__bglab_disabled_skill_root__")


def get_package_skill_root(engine_name: str) -> Path:
    """Return the manifest-owned package Skill root, or the legacy root."""
    try:
        from bglab.games.registry import get_game

        package_dir = get_game(engine_name).skills_path
    except Exception:
        package_dir = None
    return package_dir if package_dir is not None else SKILL_DIR / engine_name


def _package_active_version(engine_name: str) -> str | None:
    """Read the package pointer without treating a missing pointer as disabled."""
    active = get_package_skill_root(engine_name) / "active.json"
    if not active.is_file():
        return None
    try:
        return str(json.loads(active.read_text(encoding="utf-8")).get("version", ""))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SkillBundleError(f"{engine_name} active Skill pointer is invalid") from exc


def _package_skills_disabled(engine_name: str) -> bool:
    return _package_active_version(engine_name) == "none"


def _verified_release(root: Path, engine_name: str, version: str) -> Path | None:
    release = root / "releases" / version
    manifest_path = release / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if manifest.get("verified") is not True:
        return None
    if manifest.get("engine") not in {None, engine_name}:
        return None
    if str(manifest.get("version", version)) != version:
        return None
    return release


def _active_package_release(engine_name: str) -> Path | None:
    root = get_package_skill_root(engine_name)
    version = _package_active_version(engine_name)
    if version is None:
        return None
    if version == "none":
        # A sentinel keeps get_builtin_skill_dir from falling back to legacy
        # flat skills while allowing named historical releases to remain readable.
        return _DISABLED_SKILL_ROOT
    release = _verified_release(root, engine_name, version)
    if release is None:
        raise SkillBundleError(
            f"{engine_name} active Skill release is missing or unverified: {version}",
        )
    return release


def get_builtin_skill_dir(engine_name: str) -> Path:
    """Prefer manifest-owned package Skills, retaining the legacy migration path."""
    package_dir = get_package_skill_root(engine_name)
    active_release = _active_package_release(engine_name)
    if active_release is not None:
        return active_release
    if any(package_dir.glob("*/SKILL.md")):
        return package_dir
    return SKILL_DIR / engine_name


def get_skill_evolution_root(engine_name: str) -> Path:
    """Persistent root for proposals, immutable releases, and active pointer."""
    path = Path.home() / ".bglab" / "game-skills" / engine_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_evolved_skill_dir(engine_name: str) -> Path:
    """Resolve only the verified active overlay, with legacy compatibility."""
    root = get_skill_evolution_root(engine_name)
    active = root / "active.json"
    if active.is_file():
        try:
            version = str(json.loads(active.read_text(encoding="utf-8")).get("version", ""))
            release = root / "releases" / version
            manifest = release / "manifest.json"
            if (
                version and manifest.is_file()
                and json.loads(manifest.read_text(encoding="utf-8")).get("verified") is True
            ):
                return release
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    # Recovery: use the newest verified immutable release if the pointer is corrupt.
    releases = root / "releases"
    if releases.is_dir():
        for release in sorted(releases.iterdir(), reverse=True):
            manifest = release / "manifest.json"
            try:
                if manifest.is_file() and json.loads(
                    manifest.read_text(encoding="utf-8")
                ).get("verified") is True:
                    return release
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    # Existing flat overlays remain readable until the first release migrates them.
    return root


def _skill_mapping(roots: tuple[Path, ...]) -> tuple[dict[str, Mapping[str, str]], str]:
    merged: dict[str, tuple[Path, dict, str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for skill_dir in sorted(root.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_dir.is_dir() and skill_file.is_file():
                frontmatter, body = parse_frontmatter(skill_file)
                merged[skill_dir.name] = (skill_file, frontmatter, body)

    digest = hashlib.sha256()
    skills: dict[str, Mapping[str, str]] = {}
    for slug, (skill_file, frontmatter, body) in sorted(merged.items()):
        if frontmatter.get("status", "active") != "active":
            continue
        digest.update(slug.encode("utf-8"))
        digest.update(skill_file.read_bytes())
        implicit = allows_implicit_invocation(frontmatter, skill_file.parent)
        policy_file = skill_file.parent / "agents" / "openai.yaml"
        if policy_file.is_file():
            digest.update(b"agents/openai.yaml\0")
            digest.update(policy_file.read_bytes())
        skills[slug] = MappingProxyType({
            "slug": slug,
            "name": str(frontmatter.get("name", frontmatter.get("displayName", slug))),
            "description": str(frontmatter.get("description") or ""),
            "when": str(frontmatter.get("whenToUse") or ""),
            "priority": str(frontmatter.get("priority") or "0"),
            "implicit_invocation": "true" if implicit else "false",
            "body": body,
            "source_dir": str(skill_file.parent),
        })
    fingerprint = digest.hexdigest()[:12] if skills else "none"
    return skills, fingerprint


def _bundle_from_roots(
    engine_name: str, version: str, roots: tuple[Path, ...],
) -> GameSkillBundle:
    skills, fingerprint = _skill_mapping(roots)
    return GameSkillBundle(
        engine=engine_name,
        version=version,
        root=roots[0] if roots else None,
        skills=MappingProxyType(skills),
        fingerprint=fingerprint,
    )


def resolve_game_skill_bundle(engine_name: str, version: str) -> GameSkillBundle:
    """Resolve one immutable seat-local bundle before a game starts."""
    requested = str(version).strip()
    if requested == "none":
        return _bundle_from_roots(engine_name, "none", ())

    if requested == "current":
        if _package_skills_disabled(engine_name):
            return _bundle_from_roots(engine_name, "none", ())
        current = _bundle_from_roots(
            engine_name,
            "current",
            (get_builtin_skill_dir(engine_name), get_evolved_skill_dir(engine_name)),
        )
        if current.fingerprint == "none":
            raise SkillBundleError(f"{engine_name} has no active Skill bundle")
        return current

    roots = (
        get_package_skill_root(engine_name),
        Path.home() / ".bglab" / "game-skills" / engine_name,
    )
    for root in roots:
        release = _verified_release(root, engine_name, requested)
        if release is None:
            continue
        bundle = _bundle_from_roots(engine_name, requested, (release,))
        manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        expected = manifest.get("fingerprint")
        if expected is not None and expected != bundle.fingerprint:
            raise SkillBundleError(
                f"{engine_name} Skill release fingerprint mismatch: {requested}",
            )
        return bundle

    current = resolve_game_skill_bundle(engine_name, "current")
    if requested == current.fingerprint:
        return GameSkillBundle(
            engine=current.engine,
            version=requested,
            root=current.root,
            skills=current.skills,
            fingerprint=current.fingerprint,
        )
    raise SkillBundleError(
        f"{engine_name} Skill release does not exist: {requested}",
    )


def _bundle_matches(
    bundle: GameSkillBundle, state: dict | None, *, seat: int,
) -> list[Mapping[str, str]]:
    matches = []
    for skill in bundle.skills.values():
        if skill.get("implicit_invocation", "true") != "true":
            continue
        when = skill["when"]
        if state is not None and when and not evaluate_condition(when, state, seat=seat):
            continue
        matches.append(skill)
    return matches


def get_bundle_skill_listing(bundle: GameSkillBundle) -> str:
    entries = _bundle_matches(bundle, None, seat=0)
    if not entries:
        return ""
    return (
        "The following game strategy guides are available for use with the Skill tool:\n\n"
        + "\n".join(
            f"- {item['name']}: {item['description']} (when: {item['when']})"
            for item in entries
        )
    )


def get_bundle_matching_skill_metadata(
    bundle: GameSkillBundle, state: dict, *, seat: int = 0, limit: int = 2,
) -> list[dict[str, str | int]]:
    matches: list[dict[str, str | int]] = []
    for skill in _bundle_matches(bundle, state, seat=seat):
        try:
            priority = int(skill["priority"])
        except ValueError:
            priority = 0
        matches.append({
            "name": skill["name"],
            "description": skill["description"],
            "when": skill["when"],
            "priority": priority,
        })
    matches.sort(key=lambda item: (-int(item["priority"]), str(item["name"])))
    return matches[:max(0, limit)]


def get_bundle_active_skill_bodies(
    bundle: GameSkillBundle, state: dict, *, limit: int = 3, seat: int = 0,
) -> str:
    matches = []
    for skill in _bundle_matches(bundle, state, seat=seat):
        try:
            priority = int(skill["priority"])
        except ValueError:
            priority = 0
        matches.append((priority, skill["name"], skill["body"]))
    matches.sort(key=lambda item: (-item[0], item[1]))
    return "\n\n".join(
        f"=== {name} ===\n{body}" for _priority, name, body in matches[:limit]
    )


def get_bundle_invoked_skill_bodies(
    bundle: GameSkillBundle,
    names: list[str],
    *, state: dict | None = None, seat: int = 0,
) -> str:
    """Render only explicitly invoked bodies from one immutable seat bundle."""
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for requested in names:
        skill = next(
            (
                item for slug, item in bundle.skills.items()
                if requested in {slug, item["name"]}
            ),
            None,
        )
        if skill is None:
            continue
        if state is not None and skill["when"] and not evaluate_condition(skill["when"], state, seat=seat):
            continue
        name = str(skill["name"])
        if name in seen:
            continue
        seen.add(name)
        applicability = f"适用条件（当前是否满足以 Frame 为准）：{skill['when']}\n" if skill["when"] else ""
        selected.append((name, applicability + str(skill["body"])))
    return "\n\n".join(
        f"=== {name} ===\n{body}" for name, body in selected
    )


def invoke_game_skill(bundle: GameSkillBundle, args: dict) -> str:
    requested = str(args.get("skill", "")).strip()
    if not requested:
        return "Error: skill name is required"
    selected = next(
        (
            skill for slug, skill in bundle.skills.items()
            if requested in {slug, skill["name"]}
        ),
        None,
    )
    if selected is None:
        available = [skill["name"] for skill in _bundle_matches(bundle, None, seat=0)]
        return f"Skill '{requested}' not found. Available skills: {available}"
    if selected.get("implicit_invocation", "true") != "true":
        return f"Error: Skill '{requested}' does not allow model invocation."
    from bglab.skills.loader import substitute_skill_vars

    skill_args = str(args.get("args", ""))
    prompt = substitute_skill_vars(
        selected["body"], skill_args, selected["source_dir"],
    )
    suffix = f" with args: {skill_args}" if skill_args else ""
    return f"Skill '{selected['name']}' loaded{suffix}.\n\n{prompt}"


def _iter_skill_files(engine_name: str):
    """Yield built-ins merged with the writable overlay by directory slug."""
    if _package_skills_disabled(engine_name):
        return
    merged: dict[str, Path] = {}
    for root in (get_builtin_skill_dir(engine_name), get_evolved_skill_dir(engine_name)):
        if not root.exists():
            continue
        for skill_dir in sorted(root.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_dir.is_dir() and skill_file.exists():
                # The overlay is visited second and may update or retire a built-in.
                merged[skill_dir.name] = skill_file
    yield from sorted(merged.items())


def get_game_skill_listing(engine_name: str) -> str:
    """Return the model-visible catalog for on-demand Skill tool loading."""
    entries = load_skills_for_state(engine_name)
    if not entries or entries == ["(no strategy guides available)"]:
        return ""
    return (
        "The following game strategy guides are available for use with the Skill tool:\n\n"
        + "\n".join(line.strip() for line in entries[1:])
    )


def load_skills_for_state(engine_name: str, state: dict | None = None, seat: int = 0) -> list[str]:
    """Return active Skill metadata, filtered deterministically when state is given."""
    skill_entries = []
    for skill_slug, skill_file in _iter_skill_files(engine_name):
        frontmatter, _body = parse_frontmatter(skill_file)
        if frontmatter.get("status", "active") != "active":
            continue
        display_name = frontmatter.get("displayName", skill_slug)
        description = frontmatter.get("description", "")
        when = frontmatter.get("whenToUse", "")
        if state is not None and when and not evaluate_condition(when, state, seat=seat):
            continue
        entry = (
            f"  - {display_name}: {description} "
            f"(when: {when})"
        )
        skill_entries.append(entry)
    if not skill_entries:
        return ["(no strategy guides available)"]
    header = "[Strategy Guides] Read when conditions and apply when relevant:"
    return [header] + skill_entries


def get_active_skill_bodies(engine_name: str, state: dict, limit: int = 3, seat: int = 0) -> str:
    """Return only the highest-priority active strategy guides matching this state.

    This is deliberately deterministic: candidate or retired guides never enter a
    live game prompt, and the LLM is not asked to decide which broad handbook to
    trust before it has seen the current position.
    """
    matches: list[tuple[int, str, str]] = []
    for skill_slug, skill_file in _iter_skill_files(engine_name):
        frontmatter, body = parse_frontmatter(skill_file)
        if frontmatter.get("status", "active") != "active":
            continue
        when = frontmatter.get("whenToUse", "")
        if when and not evaluate_condition(when, state, seat=seat):
            continue
        try:
            priority = int(frontmatter.get("priority", "0"))
        except ValueError:
            priority = 0
        display_name = frontmatter.get("displayName", skill_slug)
        matches.append((priority, display_name, body))

    matches.sort(key=lambda item: (-item[0], item[1]))
    return "\n\n".join(
        f"=== {name} ===\n{body}" for _priority, name, body in matches[:limit]
    )


def strategy_phase(state: dict) -> str:
    """Return a coarse routing bucket, never an action recommendation."""
    features = extract_features(state)
    if features.get("any_score", 0) >= 8:
        return "endgame"
    if features.get("turn", 0) <= 5:
        return "opening"
    return "midgame"


def get_matching_skill_metadata(
    engine_name: str, state: dict, *, seat: int = 0, limit: int = 2,
) -> list[dict[str, str | int]]:
    """Return bounded, phase-relevant guide metadata without loading guide bodies."""
    matches: list[dict[str, str | int]] = []
    for skill_slug, skill_file in _iter_skill_files(engine_name):
        frontmatter, _body = parse_frontmatter(skill_file)
        if frontmatter.get("status", "active") != "active":
            continue
        when = frontmatter.get("whenToUse", "")
        if when and not evaluate_condition(when, state, seat=seat):
            continue
        try:
            priority = int(frontmatter.get("priority", "0"))
        except ValueError:
            priority = 0
        matches.append({
            "name": frontmatter.get("displayName", skill_slug),
            "description": frontmatter.get("description", ""),
            "when": when,
            "priority": priority,
        })
    matches.sort(key=lambda item: (-int(item["priority"]), str(item["name"])))
    return matches[:max(0, limit)]


def get_skillset_version(engine_name: str) -> str:
    """Return a reproducible fingerprint of active strategy files."""
    digest = hashlib.sha256()
    found = False
    for skill_slug, skill_file in _iter_skill_files(engine_name):
        frontmatter, _body = parse_frontmatter(skill_file)
        if frontmatter.get("status", "active") == "active":
            found = True
            digest.update(skill_slug.encode())
            digest.update(skill_file.read_bytes())
    return digest.hexdigest()[:12] if found else "none"


def get_all_skill_bodies(engine_name: str) -> str:
    """Return ALL skill bodies concatenated (for relink after compact)."""
    bodies = []
    for skill_slug, skill_file in _iter_skill_files(engine_name):
        frontmatter, body = parse_frontmatter(skill_file)
        if frontmatter.get("status", "active") != "active":
            continue
        display_name = frontmatter.get("displayName", skill_slug)
        bodies.append(f"=== {display_name} ===\n{body}")
    return "\n\n".join(bodies)
