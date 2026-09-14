""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class CmdType:
    ""
    LOCAL = "local"
    PROMPT = "prompt"
    LOCAL_JSX = "local-jsx"


@dataclass
class SlashCommand:
    ""
    name: str
    description: str
    type: str = CmdType.LOCAL  # "local" | "prompt" | "local-jsx"
    category: str = ""
    aliases: list[str] = field(default_factory=list)
    argument_hint: str = ""
    user_invocable: bool = True         # if False, only model can invoke
    is_hidden: bool = False             # hidden from help/typeahead

    # Local commands
    handler: Callable[[str, "CommandRegistry"], str | None] | None = None

    # Prompt commands
    get_prompt: Callable[[str], str] | None = None


class CommandRegistry:
    ""

    def __init__(self):
        self._commands: dict[str, SlashCommand] = {}
        self._register_all()

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name.lower()] = cmd
        for alias in cmd.aliases:
            self._commands[alias.lower()] = cmd

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name.lower())

    def search(self, partial: str) -> list[SlashCommand]:
        partial = partial.lower().lstrip("/")
        seen: set[str] = set()
        results: list[SlashCommand] = []
        for cmd in self.list_commands():
            if cmd.is_hidden:
                continue
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            n = cmd.name.lower().lstrip("/")
            if partial in n or partial in cmd.description.lower():
                results.append(cmd)
        return sorted(results, key=lambda c: c.name)

    def list_commands(self) -> list[SlashCommand]:
        """All unique commands sorted by category then name."""
        seen: set[str] = set()
        cmds: list[SlashCommand] = []
        for name, cmd in sorted(self._commands.items(), key=lambda x: x[0]):
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            cmds.append(cmd)
        return sorted(cmds, key=lambda c: (c.category, c.name))

    def commands_by_category(self) -> dict[str, list[SlashCommand]]:
        cats: dict[str, list[SlashCommand]] = {}
        seen: set[str] = set()
        for cmd in self.list_commands():
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            cat = cmd.category or "other"
            cats.setdefault(cat, []).append(cmd)
        return dict(sorted(cats.items()))

    def execute(self, text: str) -> dict:
        ""
        if not text.startswith("/"):
            return {"type": "not_found"}

        parts = text.split(maxsplit=1)
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self.get(name)
        if cmd is None:
            return {
                "type": "local",
                "data": f"Unknown slash command: {name}. Type /help to see available commands.",
            }

        
        if cmd.type == CmdType.PROMPT and cmd.get_prompt:
            prompt = cmd.get_prompt(args)
            if prompt is not None:
                return {"type": "prompt", "data": prompt}

        if cmd.type in (CmdType.LOCAL, CmdType.LOCAL_JSX) and cmd.handler:
            result = cmd.handler(args, self)
            if result is not None:
                return {"type": "local", "data": result}

        return {"type": "not_found"}

    def _register_all(self) -> None:
        # ═══════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════
        self.register(SlashCommand(
            name="/plan", description="Enable plan mode — write operations disabled",
            type=CmdType.LOCAL, category="modes",
            handler=_h_plan, argument_hint="[reason]",
        ))
        self.register(SlashCommand(
            name="/default", description="Return to default permission mode",
            type=CmdType.LOCAL, category="modes",
            handler=_h_default,
        ))
        self.register(SlashCommand(
            name="/accept-edits", description="Automatically accept file edits",
            type=CmdType.LOCAL, category="modes",
            handler=_h_accept_edits,
        ))

        # ═══════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════
        self.register(SlashCommand(
            name="/clear", description="Clear conversation history and free context",
            type=CmdType.LOCAL, category="session", aliases=["/cls"],
            handler=_h_clear,
        ))
        self.register(SlashCommand(
            name="/compact", description="Manually compact context window",
            type=CmdType.LOCAL, category="session",
            handler=_h_compact,
        ))
        self.register(SlashCommand(
            name="/resume", description="Resume a previous conversation",
            type=CmdType.LOCAL, category="session",
            handler=_h_resume,
        ))
        self.register(SlashCommand(
            name="/delete", description="Delete saved sessions",
            type=CmdType.LOCAL, category="session",
            handler=_h_delete,
        ))
        self.register(SlashCommand(
            name="/model", description="View or change current model",
            type=CmdType.LOCAL, category="session",
            handler=_h_model, argument_hint="[model-name]",
        ))
        self.register(SlashCommand(
            name="/models", description="Configure Primary and Standby provider slots",
            type=CmdType.LOCAL, category="session",
            handler=_h_models, argument_hint="[primary|standby|test|disable]",
        ))
        self.register(SlashCommand(
            name="/status", description="Show session overview and status",
            type=CmdType.LOCAL, category="session", aliases=["/stats"],
            handler=_h_status,
        ))
        self.register(SlashCommand(
            name="/config", description="Show current configuration",
            type=CmdType.LOCAL, category="session", aliases=["/settings"],
            handler=_h_config,
        ))
        self.register(SlashCommand(
            name="/cost", description="Show total cost and duration",
            type=CmdType.LOCAL, category="session", aliases=["/usage"],
            handler=_h_cost,
        ))
        self.register(SlashCommand(
            name="/context", description="Show current context window usage",
            type=CmdType.LOCAL, category="session",
            handler=_h_context,
        ))
        self.register(SlashCommand(
            name="/tag", description="Tag the current session",
            type=CmdType.LOCAL, category="session",
            handler=_h_tag, argument_hint="[tag-name]",
        ))
        self.register(SlashCommand(
            name="/tasks", description="List and manage background tasks",
            type=CmdType.LOCAL, category="session",
            handler=_h_tasks,
        ))
        self.register(SlashCommand(
            name="/rename", description="Rename the current session",
            type=CmdType.LOCAL, category="session",
            handler=_h_rename, argument_hint="[name]",
        ))
        self.register(SlashCommand(
            name="/theme", description="Toggle or set terminal theme",
            type=CmdType.LOCAL, category="session",
            handler=_h_theme, argument_hint="[dark|light|list]",
        ))

        # ═══════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════
        self.register(SlashCommand(
            name="/mem", description="List and manage memory files",
            type=CmdType.LOCAL, category="tools", aliases=["/memory"],
            handler=_h_mem,
        ))
        self.register(SlashCommand(
            name="/agents", description="List available sub-agent types",
            type=CmdType.LOCAL, category="tools",
            handler=_h_agents,
        ))
        self.register(SlashCommand(
            name="/doctor", description="Diagnose installation and settings",
            type=CmdType.LOCAL, category="tools",
            handler=_h_doctor,
        ))
        self.register(SlashCommand(
            name="/files", description="List files currently in context",
            type=CmdType.LOCAL, category="tools",
            handler=_h_files,
        ))
        self.register(SlashCommand(
            name="/diff", description="View uncommitted changes and per-turn diffs",
            type=CmdType.LOCAL, category="tools",
            handler=_h_diff,
        ))
        self.register(SlashCommand(
            name="/vim", description="Toggle Vim editing mode",
            type=CmdType.LOCAL, category="tools",
            handler=_h_vim,
        ))
        self.register(SlashCommand(
            name="/add-dir", description="Add a working directory to file scope",
            type=CmdType.LOCAL, category="tools",
            handler=_h_add_dir, argument_hint="[path]",
        ))
        self.register(SlashCommand(
            name="/permissions", description="View and manage permission rules",
            type=CmdType.LOCAL, category="tools",
            handler=_h_permissions, argument_hint="[list|add|remove|mode]",
        ))
        self.register(SlashCommand(
            name="/mcp", description="Show MCP server status and tools",
            type=CmdType.LOCAL, category="tools",
            handler=_h_mcp, argument_hint="[list|reload|discover]",
        ))
        self.register(SlashCommand(
            name="/hooks", description="Show hook system status",
            type=CmdType.LOCAL, category="tools",
            handler=_h_hooks,
        ))

        # ═══════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════
        self.register(SlashCommand(
            name="/init", description="Initialize a new CLAUDE.md for the project",
            type=CmdType.PROMPT, category="prompt",
            get_prompt=_skill_init,
        ))
        self.register(SlashCommand(
            name="/review", description="Review a pull request",
            type=CmdType.PROMPT, category="prompt",
            get_prompt=_skill_review,
        ))
        self.register(SlashCommand(
            name="/security-review", description="Security review of pending changes",
            type=CmdType.PROMPT, category="prompt",
            get_prompt=_skill_security_review,
        ))

        # ═══════════════════════════════════════════════════════
        # SYSTEM
        # ═══════════════════════════════════════════════════════
        self.register(SlashCommand(
            name="/help", description="Show available commands",
            type=CmdType.LOCAL, category="system", aliases=["/h", "/?"],
            handler=_h_help,
        ))
        self.register(SlashCommand(
            name="/exit", description="Exit bglab",
            type=CmdType.LOCAL, category="system", aliases=["/q", "/quit"],
            handler=_h_exit,
        ))
        self.register(SlashCommand(
            name="/stop", description="Save session and exit gracefully",
            type=CmdType.LOCAL, category="system",
            handler=_h_stop,
        ))
        self.register(SlashCommand(
            name="/version", description="Show version information",
            type=CmdType.LOCAL, category="system",
            handler=_h_version,
        ))

        # ═══════════════════════════════════════════════════════
        # SHOW / DEBUG — prompt inspection commands
        # ═══════════════════════════════════════════════════════
        self.register(SlashCommand(
            name="/show", description="Show prompt data (prompt/messages/context/flags)",
            type=CmdType.LOCAL, category="debug",
            handler=_h_show, argument_hint="[prompt [<N>|all] | context [<key>] | messages [<N>] | flags]",
        ))
        self.register(SlashCommand(
            name="/ff", description="Manage feature flags (list/toggle/set)",
            type=CmdType.LOCAL, category="debug",
            handler=_h_feature_flags, argument_hint="[list|name [on/off]]",
            aliases=["/feature-flags", "/flags"],
        ))

        # ═══════════════════════════════════════════════════════
        # BG — browser board games commands
        # ═══════════════════════════════════════════════════════
        try:
            from bglab.slash_commands.bg import register_bg_commands
            register_bg_commands(self)
        except ImportError:
            pass  # games package not available

        # ═══════════════════════════════════════════════════════
        
        # ═══════════════════════════════════════════════════════
        self._register_bundled_skills()

    def _register_bundled_skills(self) -> None:
        ""
        try:
            import bglab.skills  # noqa: F401 — triggers __init__ which registers all 5
            from bglab.skills.base import get_bundled_skills
            for skill in get_bundled_skills():
                if not skill.user_invocable:
                    continue
                cmd = SlashCommand(
                    name=f"/{skill.name}",
                    description=skill.description,
                    type=CmdType.PROMPT,
                    category="skills",
                    argument_hint=skill.argument_hint,
                    get_prompt=skill.get_prompt,
                )
                self.register(cmd)
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════
# LOCAL COMMAND HANDLERS
# ═════════════════════════════════════════════════════════════

def _h_plan(args: str, reg: CommandRegistry) -> str:
    # Side effects handled by CLI/NDJSON dispatch layer
    reason = args.strip()
    detail = f"\nReason: {reason}" if reason else ""
    return f"Entered plan mode. Write operations are disabled.{detail}\nToggle with /plan again or /default to return."

def _h_default(args: str, reg: CommandRegistry) -> str:
    return "Default mode. All operations enabled."

def _h_accept_edits(args: str, reg: CommandRegistry) -> str:
    return "Accept edits mode. File edits auto-approved."

def _h_clear(args: str, reg: CommandRegistry) -> str:
    return "Conversation cleared."

def _h_compact(args: str, reg: CommandRegistry) -> str:
    return "Context compaction triggered."

def _h_resume(args: str, reg: CommandRegistry) -> str:
    import os
    try:
        cwd = os.getcwd()
        from bglab.persistence import list_sessions, load_messages_from_boundary
        sessions = list_sessions(cwd)
        if not sessions:
            sessions = list_sessions()
            if not sessions:
                return (
                    f"No saved sessions found.\n"
                    f"Saved transcripts: ~/.bglab/transcripts/\n"
                    f"Use --resume flag when launching to reload a session."
                )
        latest = sessions[0]
        msgs, _ = load_messages_from_boundary(latest["path"])
        sid = latest["session_id"][:8]
        count = len(msgs)
        ts = latest["modified"]
        path = latest["path"]
        return (
            f"Latest session: {sid}\n"
            f"Messages: {count}\n"
            f"Modified: {ts}\n"
            f"Path: {path}\n\n"
            f"To resume, restart with: python -m bglab.cli --resume"
        )
    except Exception as e:
        return f"Error loading sessions: {e}"


def _h_delete(args: str, reg: CommandRegistry) -> str:
    return "Use the session picker to select sessions to delete."

def _h_model(args: str, reg: CommandRegistry) -> str:
    from bglab.session.state import session_state
    if args.strip():
        # Side effect handled by CLI/NDJSON dispatch; handler just returns text
        return f"Model set to: {args.strip()}"
    return f"Current model: {session_state.model}\nUse /model <name> to switch."


def _h_models(args: str, reg: CommandRegistry) -> str:
    """Describe the Textual provider-slot editor without starting a provider."""
    return "Provider slots open in the Textual editor. Use /models or /models primary."

def _h_status(args: str, reg: CommandRegistry) -> str:
    import os, platform, sys
    from bglab.session.state import session_state
    from bglab.memory.memdir import get_memory_dir, scan_memory_files, load_entrypoint
    cwd = os.getcwd()
    mem_dir = get_memory_dir(cwd)
    mem_count = len(scan_memory_files(mem_dir)) if mem_dir.exists() else 0
    entrypoint = load_entrypoint(mem_dir)
    ep_lines = len(entrypoint.split("\n")) if entrypoint else 0
    try:
        from bglab.llm.providers import resolve_model
        resolved = resolve_model(session_state.model)
        model_label = f"{resolved.provider.display_name} / {resolved.model.display_name}"
    except ValueError:
        model_label = session_state.model
    return (
        f"Model: {model_label}  Mode: {session_state.permission_mode}\n"
        f"Turns: {session_state.total_turns}\n"
        f"Memory: {mem_count} files  MEMORY.md: {ep_lines} lines\n"
        f"Platform: {platform.system()} {platform.release()}  Python: {sys.version.split()[0]}\n"
        f"cwd: {cwd}"
    )

def _h_config(args: str, reg: CommandRegistry) -> str:
    import os
    from bglab.session.state import session_state
    from bglab.session.settings import load_settings
    settings = load_settings()
    ff_active = []
    try:
        from bglab.session.feature_flags import get_feature_flags
        ff = get_feature_flags()
        ff_active = [f["name"] for f in ff.list_all() if f["value"]]
    except Exception:
        pass
    try:
        from bglab.llm.providers import resolve_model
        resolved = resolve_model(session_state.model)
        provider_name = resolved.provider.display_name
        model_name = resolved.model.id
    except ValueError:
        provider_name = "unknown"
        model_name = session_state.model
    lines = [
        f"provider  = {provider_name}",
        f"model     = {model_name}",
        f"mode      = {session_state.permission_mode}",
        f"vim       = {'ON' if session_state.vim_mode else 'OFF'}",
        f"cwd       = {os.getcwd()}",
        f"theme     = {settings.get('theme', 'dark')}",
    ]
    if ff_active:
        lines.append(f"ff        = {', '.join(ff_active[:5])}")
    if session_state.extra_dirs:
        lines.append(f"extra-dirs = {len(session_state.extra_dirs)} dir(s)")
    return "\n".join(lines)

def _h_cost(args: str, reg: CommandRegistry) -> str:
    from bglab.session.state import session_state
    turns = session_state.total_turns
    tokens = session_state.context_tokens_used
    return (
        f"Turns: {turns}\n"
        f"Context tokens: ~{tokens:,} / {session_state.context_tokens_total:,}\n"
        f"Token tracking requires /ff show_token_usage on for per-turn data."
    )

def _h_context(args: str, reg: CommandRegistry) -> str:
    from bglab.session.state import session_state
    used = session_state.context_tokens_used
    total = session_state.context_tokens_total
    pct = (used / total * 100) if total > 0 else 0
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = "[" + "#" * filled + "-" * (bar_len - filled) + "]"
    return (
        f"Context window: {bar} {pct:.1f}%\n"
        f"Tokens: ~{used:,} / {total:,}\n"
        f"Turns: {session_state.total_turns}\n"
        f"Model: {session_state.model}"
    )

def _h_tag(args: str, reg: CommandRegistry) -> str:
    from bglab.session.state import session_state
    from bglab.session.settings import save_setting
    tag = args.strip()
    if not tag:
        current = session_state.session_tag
        if current:
            return f"Current tag: {current}"
        return "No tag set. Use /tag <name>."
    session_state.session_tag = tag
    save_setting("session_tag", tag)
    return f"Session tagged: {tag}"

def _h_tasks(args: str, reg: CommandRegistry) -> str:
    import os, glob as _glob
    from bglab.session.state import session_state
    cwd = session_state.cwd or os.getcwd()
    task_dir = os.path.join(cwd, ".bglab", "tasks")
    if not os.path.isdir(task_dir):
        return "No background tasks.\nTask queue: .bglab/tasks/ (does not exist yet)"
    tasks = _glob.glob(os.path.join(task_dir, "*.json"))
    if not tasks:
        return "No pending tasks in queue."
    lines = [f"Tasks ({len(tasks)}):"]
    for t in sorted(tasks)[:10]:
        name = os.path.basename(t)
        size = os.path.getsize(t)
        lines.append(f"  {name} ({size}B)")
    return "\n".join(lines)

def _h_mem(args: str, reg: CommandRegistry) -> str:
    import os
    try:
        from bglab.memory.memdir import get_memory_dir, scan_memory_files, load_entrypoint
        cwd = os.getcwd()
        mem_dir = get_memory_dir(cwd)
        entrypoint = load_entrypoint(mem_dir)
        lines = ["Memory:"]
        if entrypoint:
            entries = [ln for ln in entrypoint.split("\n") if ln.startswith("- [")]
            lines.append(f"  Index: {len(entries)} entries in MEMORY.md")
        if mem_dir.exists():
            files = scan_memory_files(mem_dir)
            if files:
                lines.append(f"  Files ({len(files)}):")
                for f in files[:12]:
                    t = f.get("type", "?")
                    fn = f.get("filename", "?")[:35]
                    d = f.get("description", "")[:55]
                    lines.append(f"    [{t:12s}] {fn:35s} {d}")
            else:
                lines.append("  No memory files yet.")
        else:
            lines.append("  No memory directory.")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading memory: {e}"

def _h_agents(args: str, reg: CommandRegistry) -> str:
    try:
        from bglab.tools.agent import BUILT_IN_AGENTS
        agents = BUILT_IN_AGENTS
    except Exception:
        agents = {}
    if not agents:
        return "No sub-agents available."
    lines = [f"Available agents ({len(agents)}):", ""]
    for name, defn in sorted(agents.items()):
        tools = defn.get("tools", [])
        tool_str = ", ".join(tools[:5])
        desc = defn.get("whenToUse", "")[:100]
        lines.append(f"  {name}")
        lines.append(f"    Tools: {tool_str}")
        lines.append(f"    When: {desc}")
    return "\n".join(lines)

def _h_doctor(args: str, reg: CommandRegistry) -> str:
    import sys, platform, shutil, os

    python_path = sys.executable
    node = shutil.which("node")
    npm = shutil.which("npm")
    git = shutil.which("git")

    # Check settings file
    settings_path = os.path.expanduser("~/.bglab/settings.json")
    has_settings = os.path.exists(settings_path)

    # Check mcp config
    mcp_path = os.path.expanduser("~/.bglab/mcp.json")
    has_mcp = os.path.exists(mcp_path)

    lines = [
        f"Platform:    {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python:      {sys.version.split()[0]} ({python_path})",
        f"Node:        {node or 'NOT FOUND'}",
        f"npm:         {npm or 'NOT FOUND'}",
        f"Git:         {git or 'NOT FOUND'}",
        f"Shell:       {os.environ.get('SHELL', os.environ.get('COMSPEC', '?'))}",
        f"Settings:    {'OK' if has_settings else 'NOT FOUND'} ({settings_path})",
        f"MCP config:  {'OK' if has_mcp else 'NOT FOUND'} ({mcp_path})",
    ]
    return "\n".join(lines)

def _h_files(args: str, reg: CommandRegistry) -> str:
    import os
    from bglab.session.state import session_state
    cwd = os.getcwd()
    dirs = [cwd] + list(session_state.extra_dirs)
    lines = ["Directories in scope:"]
    for d in dirs:
        marker = "*" if d == cwd else " "
        exists = os.path.isdir(d)
        note = "" if exists else " (not found)"
        lines.append(f"  [{marker}] {d}{note}")
    lines.append("")
    lines.append("Use /add-dir <path> to add more directories.")
    return "\n".join(lines)

def _h_diff(args: str, reg: CommandRegistry) -> str:
    import subprocess, os
    try:
        # Check if we're in a git repo first
        rev = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True,
            timeout=5, cwd=os.getcwd(),
        )
        if rev.returncode != 0:
            return "Not a git repository. Use git init or cd to a git repo."
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True,
            timeout=10, cwd=os.getcwd(),
        )
        if result.returncode != 0:
            return f"Git diff failed.\n{result.stderr.strip()}"
        if not result.stdout.strip():
            return "No uncommitted changes in working tree."
        # Also get the full diff
        full = subprocess.run(
            ["git", "diff", "--unified=3"],
            capture_output=True, text=True,
            timeout=10, cwd=os.getcwd(),
        )
        output = result.stdout.strip()
        if full.stdout.strip():
            diff_lines = full.stdout.strip().split("\n")
            if len(diff_lines) > 80:
                output += "\n\n(Use git diff for full output — showing first 80 lines of patch)"
                output += "\n" + "\n".join(diff_lines[:80])
            else:
                output += "\n\n" + full.stdout.strip()
        return output
    except FileNotFoundError:
        return "Git not found. Install git to use /diff."
    except subprocess.TimeoutExpired:
        return "Git diff timed out."
    except Exception as e:
        return f"Error running git diff: {e}"

def _h_vim(args: str, reg: CommandRegistry) -> str:
    from bglab.session.state import session_state
    from bglab.session.settings import save_setting
    session_state.vim_mode = not session_state.vim_mode
    save_setting("vim_mode", session_state.vim_mode)
    state = "ON" if session_state.vim_mode else "OFF"
    return f"Vim mode: {state}"

def _h_help(args: str, reg: CommandRegistry) -> str:
    cats = reg.commands_by_category()
    lines: list[str] = ["Commands"]
    for cat_name, cmds in cats.items():
        lines.append(f"\n  {cat_name.upper()}")
        for c in cmds:
            if c.is_hidden:
                continue
            aliases_str = ""
            if c.aliases:
                aliases_str = " (" + " ".join(c.aliases) + ")"
            hint = f" {c.argument_hint}" if c.argument_hint else ""
            lines.append(f"    {c.name}{aliases_str}{hint}")
            lines.append(f"      {c.description}")
    return "\n".join(lines)

def _h_exit(args: str, reg: CommandRegistry) -> str:
    import sys
    sys.exit(0)


def _h_stop(args: str, reg: CommandRegistry) -> str:
    # Persist is handled by the dispatch layer (cli_ndjson / cli).
    # This handler just returns the confirmation text.
    return "Session saved. You can resume later with /resume."

def _h_version(args: str, reg: CommandRegistry) -> str:
    import sys, platform
    try:
        from importlib.metadata import PackageNotFoundError, version
        __version__ = version("bglab")
    except PackageNotFoundError:
        __version__ = "source checkout"
    return (
        f"bglab v{__version__}\n"
        f"Python {sys.version.split()[0]} on {platform.system()} {platform.release()}\n"
        f"Session: {_get_session_id()}"
    )


def _get_session_id() -> str:
    try:
        from bglab.persistence.transcript import TranscriptWriter
        import os
        # Try to find existing writer or return "none"
        return "none (transcript not started)"
    except Exception:
        return "?"


# ═════════════════════════════════════════════════════════════
# NEW COMMAND HANDLERS (added per user request)
# ═════════════════════════════════════════════════════════════

def _h_add_dir(args: str, reg: CommandRegistry) -> str:
    """Add a directory to the agent's file access scope."""
    from bglab.session.state import session_state
    from bglab.session.settings import save_setting
    import os

    path = args.strip()
    if not path:
        dirs = [os.getcwd()] + list(session_state.extra_dirs)
        lines = ["Working directories:"]
        for i, d in enumerate(dirs):
            marker = "*" if i == 0 else " "
            lines.append(f"  [{marker}] {d}")
        return "\n".join(lines)

    expanded = os.path.expanduser(os.path.expandvars(path))
    abs_path = os.path.abspath(expanded)
    if not os.path.isdir(abs_path):
        return f"Not a directory: {abs_path}"
    if abs_path in session_state.extra_dirs:
        return f"Already added: {abs_path}"
    session_state.extra_dirs.append(abs_path)
    save_setting("extra_dirs", session_state.extra_dirs)
    return f"Added: {abs_path}\nTotal directories: {len(session_state.extra_dirs) + 1}"


def _h_permissions(args: str, reg: CommandRegistry) -> str:
    """Show and manage permission rules."""
    from bglab.session.state import session_state
    from bglab.permissions.types import PermissionMode

    parts = args.strip().split()
    sub = parts[0].lower() if parts else ""

    if sub == "mode" and len(parts) > 1:
        new_mode = parts[1].lower()
        valid = [m.value for m in PermissionMode]
        if new_mode in valid:
            session_state.permission_mode = new_mode
            from bglab.session.settings import save_setting
            save_setting("permission_mode", new_mode)
            return f"Permission mode set to: {new_mode}"
        return f"Invalid mode: {new_mode}. Valid modes: {', '.join(valid)}"

    if sub == "add" and len(parts) >= 3:
        tool = parts[1]
        action = parts[2].lower()
        if action not in ("allow", "deny", "ask"):
            return f"Action must be: allow, deny, or ask. Got: {action}"
        return f"Rule added: {action} {tool} (session scope, non-persistent)"

    if sub == "remove" and len(parts) > 1:
        return f"Rule for '{parts[1]}' removed (session scope)."

    if sub and sub not in {"mode", "add", "remove"}:
        return (
            f"Unknown permissions sub-command: {sub}. "
            "Use mode, add, or remove."
        )

    # Default: show current mode + rules
    lines = [
        f"Permission mode: {session_state.permission_mode}",
        f"Available modes: default, plan, accept_edits, bypass",
        "",
        "Session rules: none",
        "Use /permissions mode <name> to switch modes.",
        "Use /permissions add <tool> <allow|deny|ask> to add a rule.",
    ]
    return "\n".join(lines)


def _h_mcp(args: str, reg: CommandRegistry) -> str:
    """Show MCP server status and tools."""
    sub = args.strip().lower()
    if sub not in {"", "list", "reload", "discover"}:
        return f"Unknown MCP sub-command: {sub}. Use list, reload, or discover."
    try:
        from bglab.mcp.manager import get_mcp_manager, CONFIG_PATH
    except ImportError:
        return "MCP unavailable: the optional MCP manager is not installed."

    mgr = get_mcp_manager()

    if sub == "reload":
        mgr.clear()
        configs = mgr.load_config()
        if not configs:
            return "No MCP servers configured.\nConfig file: " + str(CONFIG_PATH)
        return f"Reloaded {len(configs)} server(s)."

    if sub == "discover":
        import asyncio, concurrent.futures
        async def _discover():
            return await mgr.discover_all()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                tools = pool.submit(lambda: asyncio.run(_discover())).result(timeout=30)
                if not tools:
                    return "No tools discovered. Check server configs."
                lines = [f"Discovered {len(tools)} tool(s):"]
                for full_name, info in sorted(tools.items()):
                    lines.append(f"  {full_name} — {info.description[:80]}")
                return "\n".join(lines)
            except Exception as e:
                return f"Discovery failed: {e}"

    # Default: show config status
    configs = mgr.load_config()
    lines = [f"MCP config: {CONFIG_PATH}"]
    if not configs:
        lines.append("No servers configured.")
        lines.append("Add servers in ~/.bglab/mcp.json")
        return "\n".join(lines)
    lines.append(f"{len(configs)} server(s):")
    for name, cfg in configs.items():
        status = "enabled" if cfg.enabled else "disabled"
        lines.append(f"  {name} ({status}): {cfg.command} {' '.join(cfg.args)}")
    lines.append("\nUse /mcp discover to connect and list tools.")
    return "\n".join(lines)


def _h_hooks(args: str, reg: CommandRegistry) -> str:
    """Show hook system status."""
    try:
        from bglab.hooks.state import StopHooksState
        state = StopHooksState()
    except Exception:
        state = None

    lines = ["Hook System Status:", ""]

    # Stop hooks
    lines.append("  Stop hooks (post-turn):")
    lines.append("    - Prompt suggestion: enabled")
    lines.append("    - Memory extraction: enabled (every N turns)")
    lines.append("    - Auto-dream: enabled (24h + 5 sessions gate)")
    lines.append("    - User custom hooks: none configured")

    # State info
    if state:
        lines.append("")
        lines.append(f"  Sessions since dream: {getattr(state, 'dream_session_count', 0)}")
        lines.append(f"  Last dream: {getattr(state, 'last_dream_at', 'never')}")

    lines.append("")
    lines.append("Configure hooks in ~/.bglab/hooks.json")
    return "\n".join(lines)


def _h_rename(args: str, reg: CommandRegistry) -> str:
    """Rename the current session."""
    from bglab.session.settings import save_setting, load_settings

    name = args.strip()
    settings = load_settings()

    if not name:
        current = settings.get("session_name", "")
        if current:
            return f"Current session name: {current}"
        return "No session name set. Use /rename <name>."

    save_setting("session_name", name)
    return f"Session renamed to: {name}"


def _h_theme(args: str, reg: CommandRegistry) -> str:
    """Toggle or set terminal theme."""
    from bglab.session.settings import save_setting, load_settings

    THEMES = {
        "dark": "Dark theme (default)",
        "light": "Light theme",
        "monokai": "Monokai theme",
        "solarized": "Solarized theme",
    }

    sub = args.strip().lower()
    settings = load_settings()
    current = settings.get("theme", "dark")

    if sub == "list":
        lines = ["Available themes:"]
        for name, desc in THEMES.items():
            marker = "*" if name == current else " "
            lines.append(f"  [{marker}] {name} — {desc}")
        return "\n".join(lines)

    if sub in THEMES:
        save_setting("theme", sub)
        return f"Theme set to: {sub}"

    if sub:
        return (
            f"Invalid theme: {sub}. "
            f"Available themes: {', '.join(THEMES)}."
        )

    # Toggle: dark ↔ light
    new_theme = "light" if current == "dark" else "dark"
    save_setting("theme", new_theme)
    return f"Theme toggled: {current} -> {new_theme}\nUse /theme list to see all themes."


# ═════════════════════════════════════════════════════════════
# PROMPT COMMAND HANDLERS (expand to prompt text → forwarded to LLM)
# ═════════════════════════════════════════════════════════════

def _skill_init(args: str) -> str:
    return (
        "You MUST use the /init skill to initialize a new CLAUDE.md file.\n\n"
        "# /init\n"
        "Initialize a CLAUDE.md file with codebase documentation for the current project.\n\n"
        "## Steps\n"
        "1. Analyze the project structure, key directories, and configuration files\n"
        "2. Identify the main programming languages, frameworks, and tools used\n"
        "3. Create or update CLAUDE.md with:\n"
        "   - Project overview and purpose\n"
        "   - Build and test commands\n"
        "   - Code style and conventions\n"
        "   - Key architecture decisions\n"
        "4. Keep it concise — focus on what future agents need to know to work effectively"
    )

def _skill_review(args: str) -> str:
    return (
        "You MUST use the /review skill.\n\n"
        "# /review\n"
        "Review a pull request. Analyze the changes for correctness,"
        " security issues, performance regressions, and adherence to"
        " the project's conventions. Provide actionable feedback."
    )

def _skill_security_review(args: str) -> str:
    return (
        "You MUST use the /security-review skill.\n\n"
        "# /security-review\n"
        "Complete a security review of the pending changes on the current"
        " branch. Identify vulnerabilities, unsafe patterns, and compliance"
        " issues. Categorize findings by severity."
    )


# ═════════════════════════════════════════════════════════════
# SHOW / DEBUG HANDLERS
# ═════════════════════════════════════════════════════════════

def _h_show(args: str, reg: CommandRegistry):  # returns Rich renderable or str
    """Handle /show — display complete assembled prompt with drill-down.

    Sub-commands:
      /show                     — overview of all 3 parts + messages
      /show prompt <N>          — expand section N of system prompt
      /show prompt all          — full system prompt in pager
      /show context [<key>]     — expand context key, or all in pager
      /show messages [<N>]      — expand message N, or all in pager
      /show flags               — feature flag list
    """
    try:
        from bglab.prompt.viewer import (
            get_last, overview, prompt_detail, context_detail,
            messages_detail, render_str,
        )
    except ImportError:
        return "Prompt viewer not available."

    snap = get_last()
    args_stripped = args.strip()
    tokens = args_stripped.split(None, 1)
    subcmd = tokens[0].lower() if tokens else ""
    subargs = tokens[1] if len(tokens) > 1 else ""

    if subcmd == "flags":
        return _h_feature_flags("list", reg)

    if subcmd == "config" or subcmd == "flags-summary":
        try:
            from bglab.session.feature_flags import get_feature_flags
            ff = get_feature_flags()
            lines = ["Current Feature Flags:"]
            for f in ff.list_all():
                marker = "*" if f["value"] else " "
                lines.append(f"  [{marker}] {f['name']} = {f['value']}")
            lines.append("")
            lines.append("Use /ff <name> on/off to toggle.")
            return "\n".join(lines)
        except Exception as e:
            return f"Error reading feature flags: {e}"

    if not subcmd:
        return render_str(overview(snap))

    if subcmd == "prompt":
        if subargs == "all":
            text = prompt_detail(snap, None)
            return text if text else "No system prompt captured."
        elif subargs:
            try:
                idx = int(subargs)
            except ValueError:
                return f"Invalid section index: '{subargs}'. Use /show prompt <N> or /show prompt all."
            text = prompt_detail(snap, idx)
            return text if text else "No system prompt captured."
        else:
            return render_str(overview(snap))

    if subcmd == "context":
        key = subargs if subargs else None
        text = context_detail(snap, key)
        return text if text else "No context captured."

    if subcmd == "messages":
        if subargs:
            try:
                idx = int(subargs)
            except ValueError:
                return f"Invalid message index: '{subargs}'. Use /show messages <N> or /show messages."
            text = messages_detail(snap, idx)
            return text if text else "No messages captured."
        else:
            text = messages_detail(snap, None)
            return text if text else "No messages captured."

    return f"Unknown /show sub-command: '{subcmd}'. Try: prompt, messages, context, flags"

def _h_feature_flags(args: str, reg: CommandRegistry) -> str:
    """Handle /ff — manage feature flags.

    Sub-commands:
      /ff list              — list all feature flags and values
      /ff <name>            — toggle a boolean flag
      /ff <name> on/off     — set a flag value
      /ff <name> <value>    — set a string flag value

    Flags are persisted to settings.json and also overrideable via
    BGLAB_FF_<NAME> environment variables.
    """
    try:
        from bglab.session.feature_flags import (
            get_feature_flags,
            set_feature_flags,
            FeatureFlags,
        )
        ff = get_feature_flags()
    except ImportError:
        return "Feature flags system not available."
    except Exception as e:
        return f"Error loading feature flags: {e}"

    args = args.strip()

    if not args or args == "list":
        lines = ["Feature Flags:", ""]
        for f in ff.list_all():
            marker = "ON " if f["value"] else "OFF"
            lines.append(f"  [{marker}] {f['name']} = {f['value']} ({f['type']})")
        lines.append("")
        lines.append("Toggle:   /ff <name>          (toggles boolean flags)")
        lines.append("Set:      /ff <name> on/off    (explicit value)")
        lines.append("Env:      BGLAB_FF_<NAME>=1  (takes precedence)")
        return "\n".join(lines)

    parts = args.split(maxsplit=1)
    name = parts[0].lower()
    value_str = parts[1].strip().lower() if len(parts) > 1 else ""

    # Check if it's a valid flag name
    flag_names = {f["name"] for f in ff.list_all()}
    if name not in flag_names:
        similar = [n for n in flag_names if name in n]
        hint = f"\nSimilar: {', '.join(similar[:5])}" if similar else ""
        return f"Unknown flag: '{name}'. Use /ff list to see available flags.{hint}"

    try:
        if not value_str:
            # Toggle boolean flag
            result = ff.toggle(name)
            if result is None:
                return f"'{name}' is not a boolean flag. Use /ff {name} <value>."
            set_feature_flags(ff)
            return f"Feature flag '{name}' toggled: {'ON' if result else 'OFF'}"

        # Set explicit value
        ff.set(name, value_str)
        set_feature_flags(ff)
        new_val = getattr(ff, name, "?")
        return f"Feature flag '{name}' set to: {new_val}"
    except Exception as e:
        return f"Error: {e}"
