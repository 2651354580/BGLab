"""A single changing status line; animation never appends transcript content."""

from rich.text import Text
from rich.cells import cell_len
from textual.widgets import Static

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
ACTIVITY_WORDS = {
    "正在处理请求": ("思考中", "推敲中", "梳理中"),
    "正在准备工具": ("准备调用", "整理参数"),
    "正在运行工具": ("运行工具",),
}


def motion_enabled(widget: Static) -> bool:
    return widget.is_on_screen and widget.app.animation_level != "none"


def shimmer_text(value: str, frame: int, *, base: str = "#a0abc0", highlight: str = "#eff4ff", active: bool = True) -> Text:
    """Move a soft highlight across fixed cells; never shift the text itself."""
    result = Text(no_wrap=True)
    if not active:
        result.append(value, style=base)
        return result
    width = max((cell_len(line) for line in value.splitlines()), default=1)
    center = (frame % 60) / 59 * (width + 12) - 6
    colors = []
    for step in range(17):
        colors.append("#{:02x}{:02x}{:02x}".format(*(
            round(int(base[i:i + 2], 16) + (int(highlight[i:i + 2], 16) - int(base[i:i + 2], 16)) * step / 16)
            for i in (1, 3, 5)
        )))
    column = 0
    for char in value:
        if char == "\n":
            result.append(char)
            column = 0
            continue
        strength = max(0, 1 - abs(column - center) / 6)
        result.append(char, style=colors[round(strength * 16)])
        column += cell_len(char)
    return result


class ActivityLine(Static):
    def __init__(self, label: str = "正在处理请求", **kwargs) -> None:
        super().__init__("", **kwargs)
        self.label = label
        self.active = True
        self.frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.12, self._tick)
        self.set_status(self.label, active=self.active)

    def set_status(self, label: str, *, active: bool = True) -> None:
        if (self.label, self.active) != (label, active):
            self.frame = 0
        self.label, self.active = label, active
        self._refresh()

    @property
    def display_label(self) -> str:
        words = ACTIVITY_WORDS.get(self.label, (self.label,))
        return words[(self.frame // 20) % len(words)] if self.active else self.label

    def _refresh(self) -> None:
        moving = self.active and motion_enabled(self)
        text = Text(f"  {SPINNER_FRAMES[self.frame % len(SPINNER_FRAMES)] if moving else '·'} ", style="#9ab9ff")
        text.append(shimmer_text(self.display_label, self.frame, active=moving))
        text.append("…" if self.active else "", style="#a0abc0")
        self.update(text)

    def _tick(self) -> None:
        if self.active and motion_enabled(self):
            self.frame = (self.frame + 1) % 120
            self._refresh()
