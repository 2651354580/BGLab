from __future__ import annotations

from bglab.slash_commands.registry import CmdType, CommandRegistry, SlashCommand
from bglab.games.registry import GameRegistryError, discover_games, resolve_game


class GameStartArgumentError(ValueError):
    pass


def parse_bg_manual_test_request(text: str) -> dict | None:
    """Parse the deliberately small natural-language manual-test entrypoint."""
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return None
    if normalized.casefold().startswith("manual-test "):
        query = normalized.split(None, 1)[1]
    elif normalized.casefold().startswith("test "):
        query = normalized.split(None, 1)[1]
    elif normalized.startswith("测试"):
        query = normalized[2:].strip()
    else:
        return None
    # This shortcut bypasses the model, so only an exact game alias proves
    # launch intent. A request to test code (including game code) stays chat.
    if not normalized.casefold().startswith("manual-test "):
        query = query.rstrip("。.!！")
        if not any(
            query.casefold() == alias.casefold()
            for game in discover_games().values()
            for alias in (game.id, game.title, *game.aliases)
        ):
            return None
    if not query:
        raise GameStartArgumentError(
            f"manual-test requires a game name. Available: {_available_games()}",
        )
    try:
        engine = resolve_game(query).id
    except GameRegistryError as exc:
        raise GameStartArgumentError(
            f"unknown game alias {query!r}: {exc}. Available: {_available_games()}",
        ) from exc
    return {"engine": engine, "mode": "manual-test", "player_count": 2}


def _available_games() -> str:
    return "、".join(
        definition.title for definition in discover_games().values()
    ) or "(none)"


def parse_bg_start_args(items: list[str]) -> dict:
    mode = "ai_vs_ai"
    player_count = 2
    alias_parts: list[str] = []
    for item in items:
        lowered = item.lower()
        if lowered in {"manual-test", "manual", "test", "测试"}:
            mode = "manual-test"
            continue
        if lowered in {"human", "player", "人类"}:
            mode = "human_vs_ai"
            continue
        try:
            player_count = int(item)
            continue
        except ValueError:
            alias_parts.append(item)
    engine = "splendor"
    if alias_parts:
        query = " ".join(alias_parts)
        try:
            engine = resolve_game(query).id
        except GameRegistryError as exc:
            raise GameStartArgumentError(
                f"unknown game alias {query!r}: {exc}. Available: {_available_games()}",
            ) from exc
    return {"engine": engine, "mode": mode, "player_count": player_count}


def _help() -> str:
    return (
        f"网页桌游命令（可用：{_available_games()}）：\n"
        "  /bg start [游戏名] [human|manual-test] [人数] — 开启对局（默认宝石 2 AI）\n"
        "  测试璀璨宝石 | 测试花砖物语 | 测试姬路城 — 同浏览器控制标准二人局\n"
        "  /bg stop                  — 关闭前端并返回 Code Agent\n"
        "  /bg retry [game_id]       — 重试同一已暂停局面\n"
        "  /bg resume [game_id]      — 恢复存档；TUI 可选择，命令行多份存档需指定 ID\n"
        "  /bg replay [game_id]      — 打开已完成对局的只读逐回合回放"
    )


def _bg_handler(args: str, reg: CommandRegistry) -> str:
    del reg
    parts = args.strip().split()
    if not parts:
        return _help()

    if parts[0].startswith("测试") or parts[0].casefold() in {"test", "manual-test"}:
        try:
            start_args = parse_bg_manual_test_request(args)
        except GameStartArgumentError as exc:
            return f"ERROR: {exc}"
        if start_args is None:
            return _help()
        from bglab.tools.bg_play import _bg_tool_call
        return _bg_tool_call(start_args)

    subcommand = parts[0].lower()
    if subcommand == "stop":
        from bglab.tools.bg_play import stop_game
        return stop_game()
    if subcommand == "resume":
        from bglab.tools.bg_play import resume_game
        return resume_game(parts[1] if len(parts) > 1 else None)
    if subcommand == "retry":
        from bglab.tools.bg_play import retry_game
        return retry_game(parts[1] if len(parts) > 1 else None)
    if subcommand == "replay":
        from bglab.tools.bg_play import start_replay
        return start_replay(parts[1] if len(parts) > 1 else None)
    if subcommand != "start":
        return _help()

    try:
        start_args = parse_bg_start_args(parts[1:])
    except GameStartArgumentError as exc:
        return f"ERROR: {exc}"

    from bglab.tools.bg_play import _bg_tool_call
    return _bg_tool_call(start_args)


def register_bg_commands(registry: CommandRegistry) -> None:
    registry.register(SlashCommand(
        name="/bg",
        description="Start, stop, retry, or resume a browser board-game session",
        type=CmdType.LOCAL,
        category="tools",
        argument_hint=(
            "start [game] [human] [players] | stop | retry [game_id] | resume [game_id] | "
            "replay [game_id]"
        ),
        handler=_bg_handler,
    ))

