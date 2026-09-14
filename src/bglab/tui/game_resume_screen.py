"""Select an existing game without changing its store or active session."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class GameResumeScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "返回")]
    CSS = """
    GameResumeScreen { align: center middle; background: #10131c 85%; }
    #game-resume-dialog { width: 76; max-width: 100%; height: 24; max-height: 100%; padding: 1 2; background: #1e2432; }
    #game-resume-title { height: 1; text-style: bold; color: #e6e9f2; }
    #game-resume-caption { height: auto; margin: 1 0; color: #a0abc0; }
    #game-resume-list { height: 1fr; min-height: 1; padding: 0; border: none; background: #1e2432; color: #bdc6d8; }
    #game-resume-list > .option-list--option { padding: 0 1; }
    #game-resume-list > .option-list--option-highlighted { background: #354568; color: #eff4ff; }
    #game-resume-help { height: 1; margin-top: 1; color: #78869f; }
    """

    def __init__(self, games: list[dict]) -> None:
        super().__init__()
        self.games = games

    def compose(self) -> ComposeResult:
        options = []
        for game in self.games:
            date = str(game.get("started_at", ""))
            try:
                date = datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
            except ValueError:
                date = date[:16]
            title = str(game.get("title") or game.get("engine") or "桌游")
            count = game.get("player_count")
            label = Text(f"{title}" + (f" · {count} 人局" if count else ""), style="bold")
            label.append(f"\n{date}  ·  {game['game_id']}", style="dim")
            options.append(Option(label, id=game["game_id"]))
        with Container(id="game-resume-dialog"):
            yield Static("恢复对局", id="game-resume-title")
            yield Static("选择要继续的存档，保留原来的席位与进度。", id="game-resume-caption")
            yield OptionList(*options, id="game-resume-list")
            yield Static("↑↓ 选择   Enter 恢复   Esc 返回", id="game-resume-help")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
