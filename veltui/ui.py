"""What veltui draws: tabs, the chat log, the prompt, lists, help.

Colours come from the terminal itself (an ANSI theme, transparent background),
so veltui looks like the rest of your setup: yellow titles, grey frames.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from markdown_it import MarkdownIt
from rich import box
from rich import markdown as md
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Size
from textual.message import Message as TMessage
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import Input, Static, TextArea

from .models import Model, name_of
from .store import Message

TABS = ("chat", "history", "models")

# --- the terminal's own colours ----------------------------------------------

TERMINAL = Theme(
    name="terminal", ansi=True, dark=True,
    primary="ansi_yellow", secondary="ansi_cyan", accent="ansi_yellow",
    warning="ansi_yellow", error="ansi_red", success="ansi_green",
    foreground="ansi_default", background="ansi_default", surface="ansi_default",
    panel="ansi_default", boost="ansi_default",
    variables={
        "ansi-background": "ansi_default",
        "ansi-foreground": "ansi_default",
        "border": "ansi_default",
        "border-blurred": "ansi_bright_black",
        "input-cursor-background": "ansi_default",
        "input-cursor-foreground": "ansi_default",
        "input-cursor-text-style": "reverse",
        "input-selection-background": "ansi_bright_black",
        "input-selection-foreground": "ansi_default",
        "screen-selection-background": "ansi_bright_black",
        "screen-selection-foreground": "ansi_default",
    },
)

# Rich styles for Markdown in replies — no backgrounds, terminal colours only
MARKDOWN = RichTheme({
    "markdown.h1": "bold yellow", "markdown.h2": "bold yellow", "markdown.h3": "bold",
    "markdown.h4": "bold", "markdown.h5": "bold", "markdown.h6": "dim",
    "markdown.code": "cyan", "markdown.code_block": "",
    "markdown.block_quote": "italic dim", "markdown.hr": "bright_black",
    "markdown.item.bullet": "yellow", "markdown.item.number": "yellow",
    "markdown.link": "cyan underline", "markdown.link_url": "cyan underline",
    "markdown.table.border": "bright_black", "markdown.table.header": "bold",
})

ACCENT = Style(color="yellow", bold=True)
YELLOW = Style(color="yellow")
DIM = Style(dim=True)
GREY = Style(color="bright_black")
BOLD = Style(bold=True)
RED = Style(color="red")
CURSOR = Style(reverse=True)
CURSOR_BLUR = Style(bold=True, underline=True)

SPINNER = "|/-\\"


def spinner() -> str:
    return SPINNER[int(time.monotonic() * 8) % len(SPINNER)]


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def when(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    lt, now = time.localtime(ts), time.localtime()
    if lt[:3] == now[:3]:
        return f"today {clock(ts)}"
    if d < 6 * 86400:
        return time.strftime("%a %H:%M", lt).lower()
    return time.strftime("%d %b %Y" if lt.tm_year != now.tm_year else "%d %b", lt).lower()


def fit(text: str | Text, width: int, style: Style | None = None) -> Text:
    """`text` cut with … or padded to exactly `width` cells."""
    t = text.copy() if isinstance(text, Text) else Text(text, style=style or "")
    t.no_wrap = True
    if t.cell_len > width:
        t.truncate(max(0, width), overflow="ellipsis")
    t.pad_right(max(0, width - t.cell_len))
    return t


# --- Markdown -----------------------------------------------------------------

class _Heading(md.Heading):
    def __rich_console__(self, console, options):
        text = self.text.copy()
        text.justify = "left"
        yield text


class _Code(md.CodeBlock):
    """A code block in a square frame with its language in the corner."""

    def __rich_console__(self, console, options):
        code = str(self.text).rstrip()
        lang = "" if self.lexer_name == "text" else self.lexer_name
        syntax = Syntax(code, self.lexer_name, theme="ansi_dark", word_wrap=True,
                        background_color="default")
        yield Panel(syntax, box=box.SQUARE, border_style="bright_black",
                    title=Text(lang, YELLOW) if lang else None, title_align="left",
                    expand=False, padding=(0, 1))


class _Rule(md.HorizontalRule):
    def __rich_console__(self, console, options):
        yield Rule(style="bright_black")


class Reply(md.Markdown):
    elements = {**md.Markdown.elements, "heading_open": _Heading, "fence": _Code,
                "code_block": _Code, "hr": _Rule}

    def __init__(self, text: str):
        super().__init__(text, code_theme="ansi_dark", hyperlinks=True)


_MD = MarkdownIt("commonmark")


def code_blocks(text: str) -> list[str]:
    return [t.content.rstrip("\n") for t in _MD.parse(text) if t.type in ("fence", "code_block")]


# --- tabs ---------------------------------------------------------------------

class TabBar(Static):
    """` [chat]  history  models` — click a name, or press 1–3."""

    class Picked(TMessage):
        def __init__(self, tab: str):
            super().__init__()
            self.tab = tab

    def __init__(self, **kw):
        super().__init__(**kw)
        self.active = TABS[0]
        self._hits: list[tuple[int, int, str]] = []

    def on_mount(self):
        self.set_active(self.active)

    def set_active(self, tab: str):
        self.active = tab
        t = Text(no_wrap=True)
        self._hits = []
        for name in TABS:
            if t.cell_len:
                t.append("  ")
            label = f"[{name}]" if name == tab else name
            self._hits.append((t.cell_len, t.cell_len + len(label), name))
            t.append(label, ACCENT if name == tab else Style())
        self.update(t)

    def on_click(self, event: events.Click):
        off = event.get_content_offset(self)
        if off is None:
            return
        for a, b, name in self._hits:
            if a <= off.x < b:
                self.post_message(self.Picked(name))


# --- the chat ---------------------------------------------------------------------

class Turn(Static):
    """One message in the log. A reply in progress re-renders as it streams."""

    def __init__(self, msg: Message, *, pending: bool = False):
        super().__init__(classes="turn")
        self.msg = msg
        self.pending = pending
        self.status = "waiting for duck.ai…"
        self.started = time.monotonic()
        self.error = ""
        self.hint = ""

    def on_mount(self):
        self.refresh_body()

    def head(self) -> Text:
        m = self.msg
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(clock(m.at), GREY)
        t.append("  ")
        if m.role == "user":
            t.append("you", BOLD)
        else:
            t.append(name_of(m.model).lower() if m.model else "duck.ai", ACCENT)
        if m.note:
            t.append(f"  · {m.note}", DIM)
        return t

    def body(self) -> RenderableType:
        m = self.msg
        if m.role == "user":
            parts: list[RenderableType] = []
            if m.shown or not m.files:
                parts.append(Text(m.shown if m.shown or m.files else m.content))
            if m.files:
                parts.append(Text("+ " + ", ".join(m.files), GREY))
            return Group(*parts)
        if self.error:
            t = Text()
            t.append(f"✕ {self.error}", RED)
            if self.hint:
                t.append(f"\n{self.hint}", GREY)
            return Group(Reply(m.content), t) if m.content else t
        if self.pending and not m.content:
            secs = int(time.monotonic() - self.started)
            t = Text()
            t.append(f"{spinner()} ", YELLOW)
            t.append(self.status, DIM)
            if secs >= 2:
                t.append(f"  {secs}s", GREY)
            return t
        if self.pending:
            return Reply(m.content + " ▍")
        return Reply(m.content) if m.content.strip() else Text("(empty reply)", DIM)

    def refresh_body(self):
        self.update(Group(self.head(), self.body()))


class Note(Static):
    """A line from veltui itself: » info, ! warning, ✕ error."""

    MARKS = {"info": ("» ", GREY), "warn": ("! ", YELLOW), "error": ("✕ ", RED)}

    def __init__(self, text: str, kind: str = "info"):
        mark, style = self.MARKS[kind]
        t = Text(mark, style)
        t.append(text, DIM if kind == "info" else style)
        super().__init__(t, classes="note")


class Boot(Static):
    """What an empty chat shows: a little BIOS-style status screen."""

    def show(self, version: str, model: Model, state: str, saved: bool):
        rows = [
            ("model", Text(model.name) + Text(f"  ({model.kind})", GREY)),
            ("duck.ai", state_text(state) + (Text("  — wakes up as you type", GREY)
                                             if state == "asleep" else Text())),
            ("saving", Text("on — this chat saves itself", YELLOW) if saved
             else Text("off — ctrl+s keeps this chat", GREY)),
        ]
        t = Text()
        t.append("veltui", BOLD)
        t.append("▪", RED)              # the red dot of an old ThinkPad's logo
        t.append(f" {version}", GREY)
        t.append("  duck.ai in your terminal\n\n", DIM)
        for label, value in rows:
            t.append(f"  {label} ", DIM)
            t.append("." * (12 - len(label)) + " ", GREY)
            t.append_text(value)
            t.append("\n")
        t.append("\n  type below and press enter  ·  / commands  ·  ? help", DIM)
        self.update(t)


def state_text(state: str, note: str = "") -> Text:
    if state == "busy":
        return Text(f"{spinner()} answering", YELLOW)
    if state == "starting":
        return Text(f"{spinner()} connecting", DIM)
    if state == "ready":
        return Text("● ready", YELLOW)
    if state == "asleep":
        return Text("○ asleep", GREY)
    if state == "offline" and note:
        return Text("✕ offline", RED)
    return Text("○ not connected", GREY)


class ChatLog(VerticalScroll):
    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
        Binding("g", "scroll_home", show=False),
        Binding("G", "scroll_end", show=False),
        Binding("ctrl+d,space", "page_down", show=False),
        Binding("ctrl+u", "page_up", show=False),
    ]


# keys the app gets the first say on while the prompt has focus
ASKED = ("enter", "tab", "shift+tab", "backspace", "pageup", "pagedown")


class Prompt(TextArea):
    """The message box. Enter sends, ctrl+j / shift+enter adds a line;
    ↑/↓ walk the menu or the input history at the edges of the text."""

    class Submitted(TMessage):
        pass

    def __init__(self, **kw):
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus",
                         highlight_cursor_line=False, **kw)
        # a steady block: a blinking one repaints twice a second, forever
        self.cursor_blink = False
        self.ask_app: Callable[[str, str], bool] = lambda key, text="": False

    async def _on_key(self, event: events.Key):
        key = event.key
        if key in ASKED and self.ask_app(key, ""):
            event.stop()
            event.prevent_default()
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
            return
        if key in ("ctrl+j", "shift+enter", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key in ("tab", "shift+tab"):
            event.stop()
            event.prevent_default()
            return

    async def _on_paste(self, event: events.Paste):
        # Textual runs the base class's paste handler as well unless told not to
        # — veltui 0.1 forgot that and every paste landed twice
        if self.ask_app("paste", event.text):
            event.stop()
            event.prevent_default()

    def action_cursor_up(self, select: bool = False):
        if not select and self.ask_app("up", ""):
            return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False):
        if not select and self.ask_app("down", ""):
            return
        super().action_cursor_down(select)


class Suggest(Static):
    """The completion menu over the prompt: commands, their arguments, paths."""

    ROWS = 8

    def show(self, items: list[tuple[str, str, str, bool]], index: int, title: str):
        if not items:
            self.display = False
            return
        start = 0 if index < self.ROWS else index - self.ROWS + 1
        start = max(0, min(start, len(items) - self.ROWS))
        w = max(10, self.size.width - 2) if self.size.width else 60
        lw = min(max(len(it[1]) for it in items) + 2, max(12, w // 2))
        t = Text(no_wrap=True)
        for i in range(start, min(len(items), start + self.ROWS)):
            _, label, detail, _ = items[i]
            row = fit(Text(label, YELLOW if i != index else Style()), lw)
            row.append_text(fit(Text(detail, DIM if i != index else Style()), w - lw))
            if i == index:
                row.stylize(CURSOR)
            if t.cell_len:
                t.append("\n")
            t.append_text(row)
        self.border_title = title
        more = len(items) - self.ROWS
        self.border_subtitle = (f"{index + 1} of {len(items)}" if index >= 0 else
                                f"+{more} more" if more > 0 else "tab picks")
        self.update(t)
        self.display = True


# --- lists (history, models) ---------------------------------------------------------

class ListBox(ScrollView, can_focus=True):
    """A plain scrolling list: j/k move, enter picks, click twice picks."""

    BINDINGS = [
        Binding("down,j", "move(1)", show=False),
        Binding("up,k", "move(-1)", show=False),
        Binding("home,g", "edge(-1)", show=False),
        Binding("end,G", "edge(1)", show=False),
        Binding("pagedown,ctrl+d", "page(1)", show=False),
        Binding("pageup,ctrl+u", "page(-1)", show=False),
        Binding("enter,l", "pick", show=False),
    ]

    class Picked(TMessage):
        def __init__(self, box: ListBox, index: int):
            super().__init__()
            self.box = box
            self.index = index

    def __init__(self, render: Callable[[Any, int], Text], empty: str = "", **kw):
        super().__init__(**kw)
        self.items: list = []
        self.cursor = 0
        self._row = render
        self._empty = empty

    def set_items(self, items: list, cursor: int | None = None):
        self.items = list(items)
        if cursor is not None:
            self.cursor = cursor
        self.cursor = max(0, min(self.cursor, len(self.items) - 1))
        self.virtual_size = Size(self.scrollable_content_region.width, max(1, len(self.items)))
        self.border_subtitle = f"{self.cursor + 1} of {len(self.items)}" if self.items else ""
        self._keep_visible()
        self.refresh()

    def on_resize(self, event: events.Resize):
        self.virtual_size = Size(self.scrollable_content_region.width, max(1, len(self.items)))

    def _keep_visible(self):
        h = self.scrollable_content_region.height
        if h <= 0:
            return
        top = int(self.scroll_y)
        if self.cursor < top:
            self.scroll_to(y=self.cursor, animate=False, immediate=True)
        elif self.cursor >= top + h:
            self.scroll_to(y=self.cursor - h + 1, animate=False, immediate=True)

    def set_cursor(self, i: int):
        if not self.items:
            return
        self.cursor = max(0, min(i, len(self.items) - 1))
        self.border_subtitle = f"{self.cursor + 1} of {len(self.items)}"
        self._keep_visible()
        self.refresh()

    def action_move(self, d: int):
        self.set_cursor(self.cursor + d)

    def action_edge(self, d: int):
        self.set_cursor(len(self.items) - 1 if d > 0 else 0)

    def action_page(self, d: int):
        self.set_cursor(self.cursor + d * max(1, self.scrollable_content_region.height - 1))

    def action_pick(self):
        if self.items:
            self.post_message(self.Picked(self, self.cursor))

    def on_click(self, event: events.Click):
        off = event.get_content_offset(self)
        if off is None:
            return
        i = off.y + int(self.scroll_y)
        if not 0 <= i < len(self.items):
            return
        again = i == self.cursor
        self.set_cursor(i)
        if again or event.chain >= 2:
            self.action_pick()

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()

    def render_line(self, y: int) -> Strip:
        w = self.scrollable_content_region.width
        i = y + int(self.scroll_y)
        if not self.items:
            text = Text(self._empty if y == 0 else "", DIM)
        elif 0 <= i < len(self.items):
            text = self._row(self.items[i], w)
            if i == self.cursor and self.has_focus:
                text = Text(text.plain)          # one even bar, not a patchwork
        else:
            return Strip.blank(w)
        strip = Strip(list(text.render(self.app.console)), text.cell_len)
        strip = strip.extend_cell_length(w).crop(0, w)
        if self.items and i == self.cursor:
            strip = strip.apply_style(CURSOR if self.has_focus else CURSOR_BLUR)
        return strip


# --- boxes over the app ------------------------------------------------------------

_BOX_CSS = """
    align: center middle;
    & > Vertical {
        width: 84;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 0 1;
        border: solid ansi_default;
        border-title-color: ansi_yellow;
        border-title-style: bold;
        border-subtitle-color: ansi_bright_black;
        background: ansi_default;
    }
"""


class TextScreen(ModalScreen):
    """A boxed page over the app (help) — esc / q / ? closes it."""

    BINDINGS = [Binding("escape,q,question_mark,f1", "dismiss", show=False)]
    DEFAULT_CSS = "TextScreen {" + _BOX_CSS + """
        & VerticalScroll { height: auto; max-height: 100%; scrollbar-size-vertical: 1;
                           scrollbar-color: ansi_bright_black;
                           scrollbar-background: ansi_default; }
    }"""

    def __init__(self, title: str, body: Text):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical() as v:
            v.border_title = self._title
            v.border_subtitle = "esc closes"
            yield VerticalScroll(Static(self._body))


class Confirm(ModalScreen[bool]):
    """y / n."""

    BINDINGS = [Binding("y,enter", "answer(True)", show=False),
                Binding("n,escape,q", "answer(False)", show=False)]
    DEFAULT_CSS = "Confirm {" + _BOX_CSS + " & > Vertical { width: 64; } }"

    def __init__(self, title: str, question: str):
        super().__init__()
        self._title = title
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical() as v:
            v.border_title = self._title
            v.border_subtitle = "y / n"
            yield Static(Text(self._question))

    def action_answer(self, yes: bool):
        self.dismiss(yes)


class Ask(ModalScreen[str | None]):
    """One line of input — enter keeps it, esc drops it."""

    BINDINGS = [Binding("escape", "cancel", show=False)]
    DEFAULT_CSS = "Ask {" + _BOX_CSS + """
        & > Vertical { width: 64; }
        & Input { border: none; padding: 0; height: 1; background: ansi_default; }
        & Input > .input--cursor { background: ansi_default; color: ansi_default;
                                   text-style: reverse; }
    }"""

    def __init__(self, title: str, value: str = ""):
        super().__init__()
        self._title = title
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical() as v:
            v.border_title = self._title
            v.border_subtitle = "enter keeps · esc cancels"
            yield Input(self._value)

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)

    def action_cancel(self):
        self.dismiss(None)


# --- help ------------------------------------------------------------------------------

KEYS: list[tuple[str, list[tuple[str, str]]]] = [
    ("typing", [
        ("enter", "send · run a /command"),
        ("ctrl+j  shift+enter", "new line"),
        ("tab  ↑ ↓", "move in the / menu"),
        ("↑ ↓", "on the first / last line: your earlier messages"),
        ("esc", "stop the reply · otherwise leave the prompt"),
        ("pgup pgdn", "scroll the chat"),
        ("ctrl+s  ctrl+n", "save this chat · new chat"),
    ]),
    ("outside the prompt (esc)", [
        ("i  enter  /", "back to typing"),
        ("j k  g G  ctrl+d ctrl+u", "scroll"),
        ("y  c", "copy the last reply · its last code block"),
        ("r  e", "ask again · edit your last message"),
        ("n  s", "new chat · save"),
        ("1 2 3", "tabs: chat · history · models"),
        ("q", "quit (ctrl+q anywhere)"),
    ]),
    ("history and models", [
        ("j k  enter", "move · open the chat / use the model"),
        ("d  R", "delete · rename a saved chat"),
        ("esc", "back to the chat"),
    ]),
]

COMMANDS: list[tuple[str, str, str]] = [
    ("new", "", "start a new chat"),
    ("model", "[name|n]", "switch model (no name: the models tab)"),
    ("file", "<path> [question]", "send a text file — tab completes the path"),
    ("save", "[title]", "save this chat; from then on it saves itself"),
    ("rename", "<title>", "rename this chat"),
    ("history", "", "saved chats"),
    ("export", "[path]", "write this chat to a Markdown file"),
    ("copy", "[code]", "copy the last reply, or its last code block"),
    ("retry", "", "ask the last question again"),
    ("help", "", "keys and commands"),
    ("quit", "", "exit veltui"),
]


def help_text() -> Text:
    t = Text()
    for title, rows in KEYS:
        t.append(f"{title}\n", ACCENT)
        for k, d in rows:
            t.append(f"  {k:<24}", YELLOW)
            t.append(f"{d}\n")
        t.append("\n")
    t.append("commands", ACCENT)
    t.append("  (type / in the prompt)\n", DIM)
    for name, args, desc in COMMANDS:
        t.append(f"  /{name:<8}", YELLOW)
        t.append(f"{args:<20}", DIM)
        t.append(f"{desc}\n")
    t.append("\npaste or drop a file's path into the empty prompt to attach it; "
             "backspace in the empty prompt drops it again.", DIM)
    return t
