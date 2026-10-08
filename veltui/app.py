"""veltui: the app — the chat, its commands and keys, history and models.

The duck.ai side lives in duck.py (a worker thread with the browser); this
file keeps the conversation and decides what to send. veltui's own copy of
the chat is the source of truth: whenever duck.ai's chat can't be trusted to
match it (after loading a chat, a model switch, an error, a browser restart)
the next message carries the conversation so far as context.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message as TMessage
from textual.widgets import ContentSwitcher, Static

from . import __version__, files
from .duck import Duck, Event, primer
from .models import DEFAULT, MODELS, Model, by_id, find_model, name_of
from .store import ChatRow, Message, Store, title_from
from .ui import (ACCENT, COMMANDS, DIM, GREY, MARKDOWN, RED, TERMINAL, YELLOW, Ask, Boot,
                 ChatLog, Confirm, ListBox, Note, Prompt, Suggest, TabBar, TextScreen,
                 Turn, code_blocks, fit, help_text, state_text, when)

MenuItem = tuple[str, str, str, bool]       # (fill, label, detail, runs on enter)


class DuckEvent(TMessage):
    """An Event from the duck.ai thread (post_message is thread-safe)."""

    def __init__(self, ev: Event):
        super().__init__()
        self.ev = ev


class Veltui(App):
    TITLE = "veltui"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
    }
    #top {
        height: 1;
        padding: 0 2;
    }
    TabBar {
        width: 1fr;
    }
    #state {
        width: auto;
    }
    #views, #chat {
        height: 1fr;
    }
    ChatLog, ListBox {
        height: 1fr;
        margin: 0 1;
        border: solid ansi_bright_black;
        border-title-color: ansi_yellow;
        border-title-style: bold;
        border-subtitle-color: ansi_bright_black;
        border-subtitle-align: right;
        scrollbar-size-vertical: 1;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: ansi_default;
        scrollbar-color-active: ansi_yellow;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
    }
    ChatLog:focus, ListBox:focus {
        border: solid ansi_default;
    }
    ChatLog {
        padding: 0 1;
    }
    ListBox {
        overflow-x: hidden;
    }
    .turn, .note {
        height: auto;
        margin: 0 0 1 0;
    }
    #boot {
        height: auto;
        margin: 1 0 0 1;
    }
    Suggest {
        display: none;
        height: auto;
        margin: 0 1;
        padding: 0 1;
        border: solid ansi_bright_black;
        border-title-color: ansi_yellow;
        border-subtitle-color: ansi_bright_black;
        border-subtitle-align: right;
    }
    #prompt-box {
        height: auto;
        margin: 0 1;
        border: solid ansi_bright_black;
        border-title-color: ansi_yellow;
        border-title-style: bold;
        border-subtitle-color: ansi_bright_black;
        border-subtitle-align: right;
    }
    #prompt-box:focus-within {
        border: solid ansi_default;
    }
    #caret {
        width: 2;
        height: 1;
    }
    Prompt {
        width: 1fr;
        height: auto;
        max-height: 8;
        border: none;
        padding: 0;
        background: ansi_default;
        scrollbar-size-vertical: 1;
        scrollbar-color: ansi_bright_black;
        scrollbar-background: ansi_default;
    }
    Prompt:focus {
        border: none;
    }
    Prompt > .text-area--cursor {
        background: ansi_default;
        color: ansi_default;
        text-style: reverse;
    }
    Prompt > .text-area--placeholder {
        color: ansi_bright_black;
    }
    Prompt > .text-area--selection {
        background: ansi_bright_black;
    }
    #footer {
        height: 1;
        padding: 0 2;
    }
    #hints {
        width: 1fr;
    }
    #helpkey {
        width: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", show=False, priority=True),
        Binding("ctrl+s", "save", show=False, priority=True),
        Binding("ctrl+n", "new_chat", show=False, priority=True),
        Binding("f1", "show_help", show=False, priority=True),
        Binding("escape", "escape", show=False),
        Binding("question_mark", "show_help", show=False),
        Binding("1", "tab('chat')", show=False),
        Binding("2", "tab('history')", show=False),
        Binding("3", "tab('models')", show=False),
        Binding("i,a,enter", "type", show=False),
        Binding("slash,colon", "type('/')", show=False),
        Binding("y", "copy_reply", show=False),
        Binding("c", "copy_code", show=False),
        Binding("r", "retry", show=False),
        Binding("e", "edit", show=False),
        Binding("n", "new_chat", show=False),
        Binding("s", "save", show=False),
        Binding("d", "delete_chat", show=False),
        Binding("R", "rename_chat", show=False),
        Binding("q", "quit", show=False),
    ]

    def __init__(self, model: Model | None = None, *, headless: bool = True,
                 idle_minutes: float = 10, store: Store | None = None, duck_factory=Duck):
        # palette numbers, never RGB: the terminal paints them with its own theme
        super().__init__(ansi_color=True)
        self.store = store or Store()
        self.model = model or by_id(self.store.get("model")) or DEFAULT
        if model:
            self.store.put("model", model.id)
        self.messages: list[Message] = []
        self.chat_id: int | None = None
        self.chat_title = ""
        self.synced = False              # duck.ai's chat holds exactly self.messages
        self.job = 0                     # the duck.ai job a reply is pending for
        self.pending: Turn | None = None
        self.primed_n = 0
        self.attachments: list[files.Attachment] = []
        self.sent_atts: list[files.Attachment] = []
        self.sent: list[str] = []        # what was typed, for ↑/↓
        self.sent_i = 0
        self.draft = ""
        self.menu: list[MenuItem] = []
        self.menu_i = -1
        self._menu_title = ""
        self.duck_state = "offline"
        self.duck_note = ""
        self._headless = headless
        self._idle = idle_minutes * 60
        self._duck_factory = duck_factory
        self.duck: Duck | None = None
        self._expect: str | None = None  # prompt text set by veltui, not typed
        self._flash_timer = None
        self._dirty = False

    # --- layout ---

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield TabBar(id="tabs")
            yield Static(id="state")
        with ContentSwitcher(initial="chat", id="views"):
            with Vertical(id="chat"):
                with ChatLog(id="log"):
                    yield Boot(id="boot")
                yield Suggest(id="suggest")
                with Horizontal(id="prompt-box"):
                    yield Static(Text("> ", ACCENT), id="caret")
                    yield Prompt(id="prompt", placeholder="message duck.ai…")
            yield ListBox(self._history_row, "no saved chats yet — ctrl+s keeps the one "
                                             "you're in", id="history")
            yield ListBox(self._model_row, id="models")
        with Horizontal(id="footer"):
            yield Static(id="hints")
            yield Static(Text.assemble(("[?]", ACCENT), " help"), id="helpkey")

    def on_mount(self):
        self.register_theme(TERMINAL)
        self.theme = "terminal"
        self.console.push_theme(MARKDOWN)
        self.tabs = self.query_one(TabBar)
        self.views = self.query_one(ContentSwitcher)
        self.chat_log = self.query_one(ChatLog)
        self.boot = self.query_one(Boot)
        self.suggest = self.query_one(Suggest)
        self.prompt_box = self.query_one("#prompt-box")
        self.prompt = self.query_one(Prompt)
        self.history = self.query_one("#history", ListBox)
        self.models = self.query_one("#models", ListBox)
        self.hints = self.query_one("#hints", Static)
        self.state_line = self.query_one("#state", Static)
        self.prompt.ask_app = self._prompt_key
        self.prompt_box.border_title = "prompt"
        self.history.border_title = "saved chats"
        self.models.border_title = "models"
        self.chat_log.anchor()
        self.duck = self._duck_factory(lambda ev: self.post_message(DuckEvent(ev)),
                                       headless=self._headless, idle=self._idle)
        self.duck.warm(self.model)
        # animation frames only while something moves; an idle veltui never wakes up
        self._ticker = self.set_interval(0.1, self._tick, pause=True)
        self.prompt.focus()
        self._show_boot()
        self._show_state()
        self._show_chat_title()
        self._show_hints()

    def on_unmount(self):
        if self.duck:
            self.duck.close()
        self.store.close()

    # --- little helpers ---

    @property
    def tab(self) -> str:
        return self.views.current or "chat"

    def flash(self, text: str, style: Style = YELLOW, secs: float = 3.0):
        self.hints.update(Text(text, style, no_wrap=True, overflow="ellipsis"))
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self._flash_timer = self.set_timer(secs, self._end_flash)

    def _end_flash(self):
        self._flash_timer = None
        self._show_hints()

    def note(self, text: str, kind: str = "info"):
        """A line from veltui in the log — above a reply that's still coming."""
        n = Note(text, kind)
        if self.pending is not None and self.pending.parent is not None:
            self.chat_log.mount(n, before=self.pending)
        else:
            self.chat_log.mount(n)
        self.boot.display = False

    def _moving(self) -> bool:
        return self.duck_state in ("busy", "starting") or self.pending is not None

    def _wake_ticker(self):
        if self._moving():
            self._ticker.resume()

    def _tick(self):
        if self.duck_state in ("busy", "starting"):
            self._show_state()
            if self.boot.display:
                self._show_boot()
        if self.pending is not None and (self._dirty or not self.pending.msg.content):
            self.pending.refresh_body()
            self._dirty = False
        if not self._moving():
            self._ticker.pause()

    def on_descendant_focus(self, _):
        self._show_hints()

    def on_descendant_blur(self, _):
        self._show_hints()

    def _show_state(self):
        m = self.model
        if self.pending is not None and self.pending.msg.model:
            m_name = name_of(self.pending.msg.model)
        else:
            m_name = m.name
        t = Text(m_name.lower(), GREY)
        t.append("  ")
        t.append_text(state_text(self.duck_state, self.duck_note))
        self.state_line.update(t)

    def _show_boot(self):
        if self.boot.display:
            self.boot.show(__version__, self.model, self.duck_state, self.chat_id is not None)

    def _show_chat_title(self):
        n = len(self.messages)
        self.chat_log.border_title = "chat" + (f" · {self.chat_title}" if self.chat_title else "")
        if not n:
            self.chat_log.border_subtitle = ""
        else:
            saved = "saved" if self.chat_id is not None else "not saved"
            self.chat_log.border_subtitle = f"{saved} · {n} message{'s' * (n != 1)}"

    def _show_attachments(self):
        if self.attachments:
            names = ", ".join(f"{a.name} ({a.lines} lines)" for a in self.attachments)
            self.prompt_box.border_subtitle = f"+ {names} · backspace drops"
        else:
            self.prompt_box.border_subtitle = ""

    def _show_hints(self):
        if self._flash_timer is not None:
            return
        if self.tab == "history":
            s = "enter open · d delete · R rename · esc back"
        elif self.tab == "models":
            s = "enter use · esc back"
        elif self.focused is self.prompt:
            if self.menu:
                s = "↑↓ tab pick · enter take · esc close"
            elif self.job:
                s = "esc stop · pgup pgdn scroll"
            else:
                s = "enter send · ctrl+j new line · / commands · esc leave the prompt"
        else:
            s = ("esc stop · " if self.job else "") + \
                "i type · j k scroll · y copy · c code · r retry · e edit · n new · s save"
        self.hints.update(Text(s, DIM, no_wrap=True,
                                                     overflow="ellipsis"))

    # --- the conversation ---

    def _mount(self, w):
        self.boot.display = False
        self.chat_log.mount(w)

    def _turn_of(self, msg: Message) -> Turn | None:
        return next((t for t in self.chat_log.query(Turn) if t.msg is msg), None)

    def send(self, text: str) -> bool:
        if self.job:
            self.flash("still answering — esc stops it")
            return False
        atts, text = self.attachments, text.strip()
        if not text and not atts:
            return False
        content = "\n\n".join([a.block() for a in atts] + ([text] if text else []))
        msg = Message("user", content, shown=text, files=tuple(a.name for a in atts))
        self.sent_atts, self.attachments = atts, []
        self._show_attachments()
        self.messages.append(msg)
        self._mount(Turn(msg))
        self._ask()
        return True

    def _ask(self):
        """Get a reply to the last message (a user one)."""
        prior = [(m.role, m.content) for m in self.messages[:-1]]
        content = self.messages[-1].content
        primed, self.primed_n = primer(prior, content) if prior else (None, 0)
        self.pending = Turn(Message("assistant", "", model=self.model.id), pending=True)
        self._mount(self.pending)
        self.chat_log.anchor()
        self.job = self.duck.chat(content, self.model, fresh=not self.synced, primed=primed)
        self._wake_ticker()
        self._show_chat_title()
        self._show_hints()

    def on_duck_event(self, message: DuckEvent):
        ev = message.ev
        if ev.kind == "state":
            self.duck_state, self.duck_note = ev.text, ev.note
            self._show_state()
            self._show_boot()
            self._wake_ticker()
            return
        if ev.kind == "warn":
            self.note(ev.text, "warn")
            return
        p = self.pending
        if p is None or ev.job != self.job:
            return                                   # left over from an older reply
        if ev.kind == "status":
            p.status = ev.text
        elif ev.kind == "primed":
            if self.primed_n:
                n = self.primed_n
                self.note(f"new duck.ai chat — the {n} earlier message{'s' * (n != 1)} "
                          "went along as context")
        elif ev.kind == "model":
            p.msg.model = ev.text
            p.refresh_body()
            self._show_state()
        elif ev.kind == "chunk":
            p.msg.content += ev.text
            self._dirty = True
        elif ev.kind == "done":
            self._finish(ev.text, ev.note)
        elif ev.kind == "error":
            self._fail(ev.text)

    def _finish(self, text: str, note: str):
        p = self.pending
        m = p.msg
        m.content = text or m.content
        m.note = note
        m.model = m.model or self.model.id
        self.pending, self.job = None, 0
        if not m.content.strip():
            p.remove()
            if note == "stopped":
                self._unsend(None, "stopped before duck.ai answered")
            else:
                self._unsend(None, "duck.ai sent an empty reply")
            return
        p.pending = False
        p.refresh_body()
        self.messages.append(m)
        self.synced = True
        self._autosave()
        self._show_state()
        self._show_chat_title()
        self._show_hints()

    def _fail(self, err: str):
        p = self.pending
        self.pending, self.job = None, 0
        p.pending = False
        p.error = err
        self._unsend(p, "")

    def _unsend(self, reply: Turn | None, why: str):
        """The last question got no answer: take it back out of the chat and
        put it back in the prompt, so enter sends it again."""
        self.synced = False
        user = self.messages.pop() if self.messages and self.messages[-1].role == "user" \
            else None
        restored = False
        if user is not None:
            t = self._turn_of(user)
            if t is not None:
                user.note = "not answered"
                t.refresh_body()
            if not self.prompt.text.strip() and not self.attachments:
                self._set_prompt(user.shown)
                self.attachments = list(self.sent_atts)
                self._show_attachments()
                restored = True
        hint = "it's back in the prompt — enter sends it again" if restored else ""
        if reply is not None:
            reply.hint = hint
            reply.refresh_body()
        else:
            self.note(why + (f" — {hint}" if hint else ""), "info")
        self._show_state()
        self._show_chat_title()
        self._show_hints()

    def _autosave(self):
        if self.chat_id is not None:
            self.chat_id = self.store.save_chat(self.chat_id, self.chat_title, self.model.id,
                                                self.messages)
            self._refresh_history()

    def _clear_log(self):
        for w in list(self.chat_log.query(".turn, .note")):
            w.remove()
        self.boot.display = True

    def _load_into_log(self):
        self._clear_log()
        for m in self.messages:
            self._mount(Turn(m))
        self.chat_log.anchor()

    # --- the prompt ---

    def _set_prompt(self, text: str):
        self._expect = text
        self.prompt.text = text
        self.prompt.move_cursor(self.prompt.document.end)
        self._update_menu(text)

    def on_text_area_changed(self, event):
        if event.text_area is not self.prompt:
            return
        text = self.prompt.text
        if self._expect is not None and text == self._expect:
            self._expect = None
            return
        self._expect = None
        self._update_menu(text)
        if self.duck_state == "asleep" and text.strip():
            # Firefox was put away while idle: start it while the message is typed
            self.duck_state = "starting"
            self.duck.warm(self.model)
            self._wake_ticker()

    def on_prompt_submitted(self, _):
        text = self.prompt.text
        line = text.strip()
        if not line and not self.attachments:
            return
        if line.startswith("/") and "\n" not in line:
            if self._drill(line):
                return
            if self._is_command(line):
                self._remember(line)
                self._set_prompt("")
                self.run_command(line)
                return
        if self.job:
            self.flash("still answering — esc stops it")
            return
        self._remember(text)
        self._set_prompt("")
        self.send(text)

    def _remember(self, text: str):
        if text.strip() and (not self.sent or self.sent[-1] != text):
            self.sent.append(text)
        self.sent_i = len(self.sent)
        self.draft = ""

    def _recall(self, d: int) -> bool:
        if not self.sent:
            return False
        if self.sent_i >= len(self.sent):
            self.draft = self.prompt.text
        i = max(0, min(len(self.sent), self.sent_i + d))
        if i != self.sent_i:
            self.sent_i = i
            self._set_prompt(self.draft if i == len(self.sent) else self.sent[i])
            self._update_menu("")
        return True

    def _prompt_key(self, key: str, text: str = "") -> bool:
        """The prompt asks before it handles these keys. True: done here."""
        if key == "paste":
            return self._paste(text)
        if key == "pageup":
            self.chat_log.scroll_page_up()
            return True
        if key == "pagedown":
            self.chat_log.scroll_page_down()
            return True
        if key in ("tab", "shift+tab"):
            if self.menu:
                if key == "tab" and (self.menu_i >= 0 or len(self.menu) == 1):
                    self._menu_take(max(0, self.menu_i), run=False)
                else:
                    self._menu_move(1 if key == "tab" else -1)
            return True
        if key in ("up", "down"):
            if self.menu:
                self._menu_move(-1 if key == "up" else 1)
                return True
            y = self._cursor_row()
            if key == "up" and y == 0:
                return self._recall(-1)
            if key == "down" and y == self._last_row():
                return self._recall(1)
            return False
        if key == "enter":
            if self.menu and self.menu_i >= 0:
                self._menu_take(self.menu_i, run=True)
                return True
            return False
        if key == "backspace":
            if not self.prompt.text and self.attachments:
                a = self.attachments.pop()
                self._show_attachments()
                self.flash(f"dropped {a.name}")
                return True
        return False

    def _cursor_row(self) -> int:
        try:
            return self.prompt.wrapped_document.location_to_offset(
                self.prompt.cursor_location).y
        except Exception:
            return self.prompt.cursor_location[0]

    def _last_row(self) -> int:
        try:
            return self.prompt.wrapped_document.height - 1
        except Exception:
            return self.prompt.document.line_count - 1

    def _paste(self, text: str) -> bool:
        """A path pasted (or dropped) into the empty prompt attaches that file."""
        raw = text.strip()
        if self.prompt.text.strip() or not raw or "\n" in raw:
            return False
        p = files.as_path(raw)
        if p is None:
            return False
        if p.is_dir():
            self._set_prompt(f"/file {files.clean_path(raw).rstrip('/')}/")
            return True
        self._attach_file(files.clean_path(raw))
        return True

    def _attach_file(self, path: str) -> bool:
        try:
            att = files.read(path)
        except (OSError, ValueError) as e:
            self.flash(str(e), RED, 5)
            return False
        self.attachments.append(att)
        self._show_attachments()
        self.flash(f"attached {att.name} — ask about it, or just press enter")
        return True

    # --- the / menu ---

    def _menu_for(self, value: str) -> tuple[list[MenuItem], str]:
        if not value.startswith("/") or "\n" in value:
            return [], ""
        body = value[1:]
        if " " not in body:
            w = body.lower()
            return [(f"/{n} ", f"/{n} {a}".strip(), d, not a or a.startswith("["))
                    for n, a, d in COMMANDS if n.startswith(w)], "commands"
        word, arg = body.split(" ", 1)
        word = word.lower()
        if word == "file":
            return [(f"/file {fill}", label, detail, not label.endswith("/"))
                    for fill, label, detail in files.completions(arg)], "files"
        if word == "model":
            a = arg.strip().lower()
            if " " in a:
                return [], ""
            return [(f"/model {m.aliases[0] if m.aliases else m.id}", m.name,
                     m.kind + ("  · in use" if m == self.model else ""), True)
                    for m in MODELS
                    if not a or any(k.startswith(a) for k in
                                    (m.id.lower(), m.name.lower(), *m.aliases))], "models"
        if word == "copy" and "code".startswith(arg.strip().lower()) and arg.strip():
            return [("/copy code", "code", "the last code block", True)], "copy"
        return [], ""

    def _update_menu(self, value: str):
        self.menu, title = self._menu_for(value)
        self.menu_i = -1
        self.suggest.show(self.menu, -1, title)
        self._menu_title = title
        self._show_hints()

    def _menu_move(self, d: int):
        if self.menu_i < 0:
            self.menu_i = 0 if d > 0 else len(self.menu) - 1
        else:
            self.menu_i = (self.menu_i + d) % len(self.menu)
        self.suggest.show(self.menu, self.menu_i, self._menu_title)

    def _menu_take(self, i: int, *, run: bool):
        fill, _, _, final = self.menu[i]
        self._set_prompt(fill)
        if run and final:
            self.on_prompt_submitted(None)

    def _drill(self, line: str) -> bool:
        """Enter on `/file <folder>` opens the folder in the menu."""
        word, _, arg = line[1:].partition(" ")
        if word.lower() != "file" or not arg.strip():
            return False
        path, question = files.split_path_arg(arg)
        p = files.as_path(path)
        if question or p is None or not p.is_dir():
            return False
        self._set_prompt(f"/file {path.rstrip('/')}/")
        return True

    def _is_command(self, line: str) -> bool:
        """`/etc/fstab is it ok?` is a message, not a command."""
        word = line[1:].split(" ", 1)[0]
        return "/" not in word or not files.as_path("/" + word)

    # --- commands ---

    def run_command(self, line: str):
        word, _, arg = line[1:].partition(" ")
        word, arg = word.lower(), arg.strip()
        if word in ("new", "reset"):
            self.action_new_chat()
        elif word in ("model", "models"):
            if not arg:
                self.action_tab("models")
            elif m := find_model(arg):
                self.use_model(m)
            else:
                self.note(f"no model “{arg}” — /model lists them", "error")
        elif word == "file":
            self._cmd_file(arg)
        elif word == "save":
            self.action_save(arg)
        elif word == "rename":
            if not arg:
                self.note("/rename needs a title: /rename my chat", "error")
            else:
                self._rename_current(arg)
        elif word in ("history", "load", "chats"):
            self.action_tab("history")
        elif word == "export":
            self._cmd_export(arg)
        elif word == "copy" and arg.lower() == "code":
            self.action_copy_code()
        elif word == "copy":
            self.action_copy_reply()
        elif word in ("retry", "again"):
            self.action_retry()
        elif word in ("help", "h", "?", "keys"):
            self.action_show_help()
        elif word in ("quit", "exit", "q"):
            self.exit()
        else:
            self.note(f"unknown command /{word} — /help lists them", "error")

    def _cmd_file(self, arg: str):
        if not arg:
            self.note("/file <path> [question] — tab completes the path", "error")
            return
        path, question = files.split_path_arg(arg)
        if not self._attach_file(path):
            return
        if question:
            if not self.send(question):
                self.flash("still answering — the file stays attached")

    def _cmd_export(self, arg: str):
        if not self.messages:
            self.flash("nothing to export yet")
            return
        title = self.chat_title or title_from(self.messages) or "chat"
        slug = re.sub(r"[^\w-]+", "-", title.lower()).strip("-")[:40] or "chat"
        name = f"veltui-{slug}-{time.strftime('%Y%m%d-%H%M')}.md"
        path = Path(files.clean_path(arg)).expanduser() if arg else Path.cwd() / name
        if path.is_dir():
            path = path / name
        try:
            path.write_text(to_markdown(title, self.messages), encoding="utf-8")
        except OSError as e:
            self.note(f"couldn't write {path}: {e.strerror or e}", "error")
            return
        self.note(f"wrote {path}")

    def use_model(self, m: Model):
        if self.job:
            self.flash("wait for the reply (esc stops it), then switch")
            return
        if m == self.model:
            self.flash(f"already on {m.name}")
            return
        self.model = m
        self.store.put("model", m.id)
        self.synced = False
        self.duck.warm(m)
        if self.messages:
            self.note(f"model: {m.name} — it gets this conversation as context with your "
                      "next message")
        else:
            self.flash(f"model: {m.name}")
        self.models.set_items(MODELS)
        self._show_state()
        self._show_boot()

    # --- actions (keys) ---

    def action_tab(self, tab: str):
        self.views.current = tab
        self.tabs.set_active(tab)
        if tab == "chat":
            self.prompt.focus()
        elif tab == "history":
            self._refresh_history()
            self.history.focus()
        else:
            self.models.set_items(MODELS, MODELS.index(self.model))
            self.models.focus()
        self._show_hints()

    def on_tab_bar_picked(self, event: TabBar.Picked):
        self.action_tab(event.tab)

    def action_type(self, prefix: str = ""):
        if self.tab != "chat":
            self.action_tab("chat")
        self.prompt.focus()
        if prefix and not self.prompt.text:
            self._set_prompt(prefix)

    def action_escape(self):
        if self.tab != "chat":
            self.action_tab("chat")
        elif self.job:
            self.duck.stop(self.job)
            if self.pending is not None:
                self.pending.status = "stopping…"
        elif self.menu:
            self._update_menu("")
        elif self.focused is self.prompt:
            self.chat_log.focus()
        self._show_hints()

    def action_show_help(self):
        self.push_screen(TextScreen("help", help_text()))

    def action_new_chat(self):
        if self.tab != "chat":
            self.action_tab("chat")
        if self.job:
            self.flash("still answering — esc stops it first")
            return
        if self.chat_id is None and len(self.messages) >= 2:
            self.push_screen(Confirm("new chat", "this chat isn't saved — drop it?"),
                             lambda yes: yes and self._new_chat())
        else:
            self._new_chat()

    def _new_chat(self):
        self.messages, self.chat_id, self.chat_title = [], None, ""
        self.synced = False
        self.attachments = []
        self._show_attachments()
        self._clear_log()
        self._show_boot()
        self._show_chat_title()
        self.prompt.focus()
        self.flash("new chat")

    def action_save(self, title: str = ""):
        if not self.messages:
            self.flash("nothing to save yet")
            return
        first = self.chat_id is None
        if title:
            self.chat_title = title
        self.chat_title = self.chat_title or title_from(self.messages) or "untitled"
        self.chat_id = self.store.save_chat(self.chat_id, self.chat_title, self.model.id,
                                            self.messages)
        self._show_chat_title()
        self._refresh_history()
        self.flash(f"saved “{self.chat_title}”" + (" — new messages save themselves" if first
                                                   else ""))

    def _rename_current(self, title: str):
        self.chat_title = title
        if self.chat_id is not None:
            self.store.rename(self.chat_id, title)
            self._refresh_history()
        self._show_chat_title()
        self.flash(f"renamed to “{title}”")

    def _last_reply(self) -> Message | None:
        return next((m for m in reversed(self.messages) if m.role == "assistant"), None)

    def action_copy_reply(self):
        m = self._last_reply()
        if m is None:
            self.flash("no reply to copy yet")
        else:
            self.copy(m.content, "the reply")

    def action_copy_code(self):
        m = self._last_reply()
        blocks = code_blocks(m.content) if m else []
        if not blocks:
            self.flash("no code block in the last reply")
        else:
            self.copy(blocks[-1], "the code block")

    def copy(self, text: str, what: str):
        self.copy_to_clipboard(text)                 # OSC 52: kitty, foot, wezterm …
        for cmd in _clipboard_cmds():                # and the desktop's own clipboard
            try:
                subprocess.run(cmd, input=text.encode(), timeout=2, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except (OSError, subprocess.SubprocessError):
                continue
        self.flash(f"copied {what} · {len(text)} chars")

    def action_retry(self):
        if self.tab != "chat" or self.job:
            return
        if not self.messages or self.messages[-1].role != "assistant":
            self.flash("nothing to ask again")
            return
        old = self.messages.pop()
        t = self._turn_of(old)
        if t is not None:
            t.remove()
        self.synced = False        # a clean duck.ai chat, so the old answer doesn't steer
        self._ask()

    def action_edit(self):
        if self.tab != "chat" or self.job:
            return
        i = next((i for i in range(len(self.messages) - 1, -1, -1)
                  if self.messages[i].role == "user"), None)
        if i is None:
            self.flash("nothing to edit yet")
            return
        user = self.messages[i]
        for m in self.messages[i:]:
            t = self._turn_of(m)
            if t is not None:
                t.remove()
        del self.messages[i:]
        self.synced = False
        if not self.messages:
            self.boot.display = True
            self._show_boot()
        self._set_prompt(user.shown)
        self.prompt.focus()
        self._show_chat_title()
        if user.files:
            self.flash("attachments dropped — attach them again if you need them")

    # --- history ---

    def _refresh_history(self):
        cur = self.history.cursor
        self.history.set_items(self.store.chats(), cur)

    def _history_row(self, row: ChatRow, w: int) -> Text:
        meta_w = 44 if w >= 80 else 0
        t = Text()
        t.append("● " if row.id == self.chat_id else "  ", YELLOW)
        t.append_text(fit(row.title, max(8, w - 2 - meta_w)))
        if meta_w:
            t.append_text(fit(Text(name_of(row.model).lower(), GREY), 18))
            t.append_text(fit(Text(f"{row.count} msgs", GREY), 10))
            t.append_text(fit(Text(when(row.updated), GREY), 16))
        return t

    def _model_row(self, m: Model, w: int) -> Text:
        t = Text()
        t.append("● " if m == self.model else "  ", YELLOW)
        t.append_text(fit(m.name, 20))
        t.append_text(fit(Text(m.kind, YELLOW if m.kind == "think" else GREY), 8))
        if w > 40:
            t.append_text(fit(Text(m.id, GREY), w - 30))
        return t

    def on_list_box_picked(self, event: ListBox.Picked):
        if event.box is self.models:
            self.use_model(MODELS[event.index])
            self.action_tab("chat")
            return
        row: ChatRow = self.history.items[event.index]
        if self.job:
            self.flash("still answering — esc stops it first")
            return
        if row.id == self.chat_id:
            self.action_tab("chat")
            return
        if self.chat_id is None and len(self.messages) >= 2:
            self.push_screen(Confirm("open chat", "the chat you're in isn't saved — drop it?"),
                             lambda yes: yes and self._open_chat(row.id))
        else:
            self._open_chat(row.id)

    def _open_chat(self, chat_id: int):
        got = self.store.load(chat_id)
        if got is None:
            self.flash("that chat is gone")
            self._refresh_history()
            return
        row, msgs = got
        self.messages, self.chat_id, self.chat_title = msgs, row.id, row.title
        self.synced = False
        m = by_id(row.model)
        if m and m != self.model:
            self.model = m
            self.duck.warm(m)
            self._show_state()
        self._load_into_log()
        self._show_chat_title()
        self._refresh_history()
        self.action_tab("chat")
        if msgs:
            self.note("your next message takes this conversation to duck.ai as context")

    def action_delete_chat(self):
        if self.tab != "history" or not self.history.items:
            return
        row: ChatRow = self.history.items[self.history.cursor]

        def done(yes: bool):
            if not yes:
                return
            self.store.delete(row.id)
            if row.id == self.chat_id:
                self.chat_id = None      # what's on screen stays, just unsaved now
                self._show_chat_title()
            self._refresh_history()
            self.flash(f"deleted “{row.title}”")
        self.push_screen(Confirm("delete", f"delete “{row.title}” for good?"), done)

    def action_rename_chat(self):
        if self.tab != "history" or not self.history.items:
            return
        row: ChatRow = self.history.items[self.history.cursor]

        def done(title: str | None):
            if not title:
                return
            self.store.rename(row.id, title)
            if row.id == self.chat_id:
                self.chat_title = title
                self._show_chat_title()
            self._refresh_history()
        self.push_screen(Ask("rename", row.title), done)


def _clipboard_cmds() -> list[list[str]]:
    if sys.platform == "win32":
        return [["clip"]]
    if sys.platform == "darwin":
        return [["pbcopy"]]
    cmds = []
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmds.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            cmds.append(["xclip", "-selection", "clipboard"])
        elif shutil.which("xsel"):
            cmds.append(["xsel", "-ib"])
    return cmds


def to_markdown(title: str, messages: list[Message]) -> str:
    out = [f"# {title}", ""]
    for m in messages:
        who = "you" if m.role == "user" else name_of(m.model or "duck.ai")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.at))
        out += [f"## {who} · {stamp}" + (f" · {m.note}" if m.note else ""), "", m.content, ""]
    return "\n".join(out)
