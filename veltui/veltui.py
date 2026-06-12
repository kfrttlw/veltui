#!/usr/bin/env python3
"""veltui — privacy-first AI chat in your terminal, powered by DuckDuckGo."""

import argparse
import atexit
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from time import monotonic

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.theme import Theme
from textual.widgets import Input, Static

# -----------------------------------------------------------------------------
#  Models
# -----------------------------------------------------------------------------

# the six models duck.ai exposes in its picker. `type` is just a label for the UI;
# `short` are the aliases /model accepts so you don't have to type the full id.
MODELS = [
    {
        "id":    "gpt-5-mini",
        "name":  "GPT-5 Mini",
        "type":  "think",
        "short": ["gpt5", "gpt-5"],
    },
    {
        "id":    "gpt-4o-mini",
        "name":  "GPT-4o Mini",
        "type":  "fast",
        "short": ["gpt4", "gpt-4", "gpt4omini"],
    },
    {
        "id":    "tinfoil/gpt-oss-120b",
        "name":  "GPT-OSS 120B",
        "type":  "think",
        "short": ["oss", "gpt-oss", "120b", "tinfoil"],
    },
    {
        "id":    "claude-haiku-4-5",
        "name":  "Claude Haiku 4.5",
        "type":  "fast",
        "short": ["claude", "haiku"],
    },
    {
        "id":    "meta-llama/Llama-4-Scout-17B-16E-Instruct",
        "name":  "Llama 4 Scout",
        "type":  "fast",
        "short": ["llama", "llama4", "meta", "scout"],
    },
    {
        "id":    "mistral-small-2603",
        "name":  "Mistral Small 4",
        "type":  "fast",
        "short": ["mistral", "mixtral"],
    },
]

DEFAULT_MODEL = MODELS[0]["id"]  # gpt-5-mini


def find_model(query: str) -> dict | None:
    q = query.strip().lower()
    if q.isdigit():
        idx = int(q) - 1
        return MODELS[idx] if 0 <= idx < len(MODELS) else None
    for m in MODELS:
        if q == m["id"].lower() or q in m["short"] or q in m["name"].lower():
            return m
    return None


def model_display(model_id: str) -> str:
    m = next((m for m in MODELS if m["id"] == model_id), None)
    if not m:
        return model_id
    tag = "[think]" if m["type"] == "think" else "[fast]"
    return f"{m['name']} {tag}"


# -----------------------------------------------------------------------------
#  Database
# -----------------------------------------------------------------------------

DB_DIR  = Path.home() / ".veltui"
DB_PATH = DB_DIR / "db.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    model      TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""


class DB:
    def __init__(self):
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(DB_PATH)
        self.con.executescript(_SCHEMA)
        self.con.commit()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str):
        self.con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        self.con.commit()

    def new_session(self, model: str) -> int:
        cur = self.con.execute("INSERT INTO sessions(model) VALUES(?)", (model,))
        self.con.commit()
        return cur.lastrowid

    def replace_messages(self, session_id: int, messages: list[dict]):
        """Overwrite a session's messages with a full snapshot (used by /save)."""
        self.con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        self.con.executemany(
            "INSERT INTO messages(session_id,role,content) VALUES(?,?,?)",
            [(session_id, m["role"], m["content"]) for m in messages],
        )
        self.con.execute(
            "UPDATE sessions SET updated_at=datetime('now') WHERE id=?",
            (session_id,),
        )
        self.con.commit()

    def session_name(self, session_id: int) -> str | None:
        row = self.con.execute(
            "SELECT name FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row[0] if row else None

    def get_messages(self, session_id: int) -> list[dict]:
        rows = self.con.execute(
            "SELECT role,content FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def list_sessions(self) -> list[tuple]:
        return self.con.execute(
            "SELECT id,name,model,updated_at FROM sessions ORDER BY updated_at DESC"
        ).fetchall()

    def rename_session(self, session_id: int, name: str):
        self.con.execute("UPDATE sessions SET name=? WHERE id=?", (name, session_id))
        self.con.commit()

    def delete_session(self, session_id: int):
        self.con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        self.con.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.con.commit()

    def clear_all(self):
        self.con.executescript("DELETE FROM messages; DELETE FROM sessions;")
        self.con.commit()


# -----------------------------------------------------------------------------
#  DuckDuckGo chat — we drive duck.ai's real web UI in a headless browser
#
#  duck.ai guards its chat endpoint with an anti-bot challenge (x-vqd-hash-1 +
#  x-fe-signals) that we can't reproduce from raw requests. Instead we let the
#  real site do all of that: type into its composer, click send, and stream its
#  own SSE response back out (see ChatBackend / _STREAM_HOOK below).
# -----------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
    "Gecko/20100101 Firefox/133.0"
)


# --- Playwright browser (reused for the whole session) ---

_pw_instance = None
_pw_browser  = None
_pw_page     = None  # persistent page — stays open for the session


_firefox_install_tried = False


def _ensure_firefox_installed(on_status=None) -> bool:
    """Download Playwright's Firefox build if it's missing.

    Runs at most once per process — so if the download itself fails (no network,
    say) it won't loop and keep re-fetching Firefox on every retry.
    """
    global _firefox_install_tried
    if _firefox_install_tried:
        return False
    _firefox_install_tried = True
    if on_status:
        on_status("first run: downloading Firefox (~80 MB)…")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "firefox"],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def _browser(on_status=None):
    global _pw_instance, _pw_browser
    if _pw_browser is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ConnectionError(
                "playwright is not installed.\n  run: pip install playwright"
            )
        _pw_instance = sync_playwright().start()
        try:
            _pw_browser = _pw_instance.firefox.launch(headless=True)
        except Exception as e:
            # 99% of the time this just means firefox isn't downloaded yet —
            # grab it once and retry, otherwise let the real error through
            need = "Executable doesn't exist" in str(e) or "playwright install" in str(e)
            if need and _ensure_firefox_installed(on_status):
                try:
                    _pw_browser = _pw_instance.firefox.launch(headless=True)
                except Exception as e2:
                    raise ConnectionError(
                        f"Firefox failed to launch after install: {e2}"
                    ) from e2
            else:
                raise ConnectionError(
                    f"Firefox failed to launch: {e}\n"
                    "  run: playwright install firefox"
                ) from e
    return _pw_browser


def _close_browser():
    global _pw_instance, _pw_browser, _pw_page
    if _pw_page:
        try:
            _pw_page.close()
        except Exception:
            pass
        _pw_page = None
    if _pw_browser:
        try:
            _pw_browser.close()
        except Exception:
            pass
        _pw_browser = None
    if _pw_instance:
        try:
            _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None


atexit.register(_close_browser)


# JS shim: tee duck.ai's own /chat response via clone() (non-invasive) and push
# each SSE `message` delta to Python through exposed bindings. This lets us stream
# the reply while the real site does all of its own anti-bot crypto for us.
_STREAM_HOOK = r"""
(() => {
  if (window.__veltuiHooked) return; window.__veltuiHooked = true;
  const orig = window.fetch;
  window.fetch = function(...args){
    const p = orig.apply(this, args);
    let url='';
    try { url = (typeof args[0]==='string')?args[0]:(args[0]&&args[0].url)||''; }
    catch(e){}
    if (url.includes('duckchat/v1/chat')) {
      p.then(r => {
        if (r.status !== 200) {
          r.clone().text()
            .then(t => window.__veltui_error(r.status, (t||'').slice(0, 300)))
            .catch(() => window.__veltui_error(r.status, ''));
        } else {
          _veltuiRead(r.clone());
        }
      }).catch(()=>{});
    }
    return p;
  };
  async function _veltuiRead(resp){
    try {
      const reader = resp.body.getReader();
      const dec = new TextDecoder(); let buf='';
      for(;;){
        const {done, value} = await reader.read();
        if (done) { window.__veltui_done(); break; }
        buf += dec.decode(value, {stream:true});
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const ln = buf.slice(0, i); buf = buf.slice(i+1);
          if (ln.startsWith('data:')) {
            const pl = ln.slice(5).trim();
            if (pl === '[DONE]') { window.__veltui_done(); continue; }
            try { const d = JSON.parse(pl); if (d.message) window.__veltui_chunk(d.message); }
            catch(e){}
          }
        }
      }
    } catch(e) { window.__veltui_done(); }
  }
})();
"""

# what the model cards are actually called in duck.ai's picker, keyed by our model id
_UI_MODEL = {
    "gpt-5-mini":                                "GPT-5 mini",
    "gpt-4o-mini":                               "GPT-4o mini",
    "tinfoil/gpt-oss-120b":                      "gpt-oss 120B",
    "claude-haiku-4-5":                          "Claude Haiku 4.5",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": "Llama 4 Scout",
    "mistral-small-2603":                        "Mistral Small 4",
}

# the streamed reply piles up here, pushed in chunk by chunk from the page
_stream_buf:   list[str] = []
_stream_done:  bool = False
_stream_error: str | None = None   # set if duck.ai answered with a non-200 status


def _push_chunk(text: str):
    _stream_buf.append(text)


def _mark_done():
    global _stream_done
    _stream_done = True


def _mark_error(status, body: str):
    """duck.ai returned a non-200 — translate it into a human message."""
    global _stream_error, _stream_done
    body = body or ""
    if "ERR_BN_LIMIT" in body or status == 429:
        _stream_error = ("DuckDuckGo rate limit reached — wait a few minutes, "
                         "or switch network / VPN")
    elif "ERR_CHALLENGE" in body:
        _stream_error = ("DuckDuckGo blocked the request (anti-bot challenge) — "
                         "try again, or switch network / VPN")
    else:
        _stream_error = f"DuckDuckGo returned an error (HTTP {status})"
    _stream_done = True


def _ensure_page(on_status=None):
    """Return (or create) the persistent duck.ai page, wired for streaming."""
    global _pw_page
    if _pw_page is None:
        br  = _browser(on_status)
        ctx = br.new_context(
            user_agent=_UA,
            viewport={"width": 1366, "height": 768},
            screen={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Europe/London",
        )
        pg = ctx.new_page()
        pg.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        pg.add_init_script(_STREAM_HOOK)
        pg.expose_function("__veltui_chunk", _push_chunk)
        pg.expose_function("__veltui_done",  _mark_done)
        pg.expose_function("__veltui_error", _mark_error)
        pg.goto("https://duck.ai/", wait_until="domcontentloaded", timeout=30_000)
        pg.wait_for_selector("textarea[name='user-prompt']", timeout=20_000)
        pg.wait_for_timeout(800)
        _pw_page = pg
    return _pw_page


def _dismiss(pg):
    """Close any transient popup (e.g. the 'Got It!' info card) that may block."""
    for name in ("Got It!", "Got it", "Accept", "Dismiss", "Okay"):
        try:
            b = pg.get_by_role("button", name=name, exact=True)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=1200)
        except Exception:
            pass


def _select_model(pg, ui_name: str):
    """Open the model picker, choose `ui_name`, confirm (starts a fresh chat).

    Every click here is a *real* Playwright click, never a JS `.click()`: duck.ai's
    bot-detection inspects event.isTrusted (it ships it in x-fe-signals), and
    synthetic clicks get the next request flagged with ERR_BN_LIMIT.
    """
    # open the picker — labelled "Switch model" in a thread, or the model name on
    # the landing page
    opened = False
    sw = pg.get_by_role("button", name="Switch model")
    if sw.count():
        sw.first.click(timeout=6_000)
        opened = True
    if not opened:
        for frag in ("GPT-5", "GPT-4o", "gpt-oss", "Llama", "Claude", "Mistral"):
            b = pg.get_by_role("button", name=frag)
            if b.count():
                try:
                    b.first.click(timeout=4_000)
                    opened = True
                    break
                except Exception:
                    continue
    pg.wait_for_timeout(700)
    # pick the model card by its visible name (scoped to the dialog if present)
    root = pg.get_by_role("dialog")
    root = root if root.count() else pg
    try:
        root.get_by_text(ui_name, exact=False).first.click(timeout=6_000)
    except Exception:
        pass
    pg.wait_for_timeout(400)
    btn = pg.get_by_role("button", name="Start New Chat")
    if btn.count():
        try:
            btn.first.click(timeout=6_000)
        except Exception:
            pass
    pg.wait_for_timeout(800)
    pg.wait_for_selector("textarea[name='user-prompt']", timeout=10_000)
    # give the fresh chat a moment to look human before the first message —
    # firing instantly is exactly what a bot would do (we would know)
    pg.wait_for_timeout(1200)


def _new_chat(pg):
    """Click 'New Chat' to drop the conversation context (used by /reset)."""
    try:
        pg.get_by_role("button", name="New Chat").first.click()
        pg.wait_for_timeout(400)
        pg.wait_for_selector("textarea[name='user-prompt']", timeout=10_000)
    except Exception:
        pass


def _send_message(pg, text: str):
    """Type a message into the composer and submit; reply streams via the hook."""
    _dismiss(pg)
    # duck.ai disables the textarea while a reply generates (and keeps it disabled
    # if the page is wedged, e.g. rate-limited). Wait for it to become editable, but
    # bail out fast with a clear message instead of a 30s click timeout.
    box = None
    for _ in range(30):                      # ~9s
        box = pg.query_selector("textarea[name='user-prompt']")
        if box and box.is_editable():
            break
        pg.wait_for_timeout(300)
    if box is None:
        raise RuntimeError("duck.ai input box not found")
    if not box.is_editable():
        raise RuntimeError("duck.ai is not responding (it may be rate-limited) — "
                           "wait a bit, or switch network / VPN")
    box.click(timeout=8_000)
    box.fill(text)
    pg.wait_for_timeout(150)
    _dismiss(pg)
    # the same button is "Ask" on the landing page but "Send" inside a thread. why. just why
    for name in ("Send", "Ask"):
        btn = pg.get_by_role("button", name=name, exact=True)
        try:
            if btn.count():
                btn.first.click(timeout=8_000)
                return
        except Exception:
            continue
    raise RuntimeError("duck.ai send button not found")


# -----------------------------------------------------------------------------
#  Chat backend — one dedicated thread owns every blocking / Playwright call
# -----------------------------------------------------------------------------

class ChatBackend:
    """One worker thread owns the whole duck.ai browser session.

    Playwright's sync API won't run inside Textual's asyncio loop, so the UI never
    touches it directly — it drops a job on the queue and this thread drives the
    page. Replies come back through callbacks the app wraps with `call_from_thread`.
    """

    def __init__(self):
        self._jobs: "queue.Queue" = queue.Queue()
        self._ui_model: str | None = None   # the model card currently selected on the page
        self._thread = threading.Thread(
            target=self._loop, name="veltui-backend", daemon=True
        )
        self._thread.start()

    def submit(self, *, messages, model, on_status, on_chunk, on_done, on_error):
        self._jobs.put(dict(
            kind="chat", messages=messages, model=model, on_status=on_status,
            on_chunk=on_chunk, on_done=on_done, on_error=on_error,
        ))

    def new_chat(self):
        """Drop duck.ai's server-side conversation context (used by /reset)."""
        self._jobs.put(dict(kind="new_chat"))

    def shutdown(self):
        self._jobs.put(None)

    # --- worker thread ---

    def _loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                _close_browser()
                return
            try:
                if job["kind"] == "new_chat":
                    if _pw_page is not None:
                        _new_chat(_pw_page)
                else:
                    self._handle_chat(**{k: v for k, v in job.items() if k != "kind"})
            except Exception as e:  # a dead worker thread would freeze the UI
                cb = job.get("on_error")
                if cb:
                    try:
                        cb(f"internal error: {e}")
                    except Exception:
                        pass

    def _handle_chat(self, *, messages, model,
                     on_status, on_chunk, on_done, on_error):
        # 1. open the page (the first call launches the browser & loads duck.ai)
        first = _pw_page is None
        if first:
            on_status("connecting…")
        try:
            pg = _ensure_page(on_status)
        except ConnectionError as e:
            on_error(str(e))
            return
        except Exception as e:
            on_error(f"could not connect: {e}")
            return
        if first:
            self._ui_model = "GPT-5 mini"   # duck.ai's default selection

        # 2. switch model if needed (this starts a fresh duck.ai chat)
        want = _UI_MODEL.get(model)
        if want and want != self._ui_model:
            on_status(f"switching to {want}…")
            try:
                _select_model(pg, want)
            except Exception:
                pass
            self._ui_model = want

        # 3. send + stream. ERR_BN_LIMIT is duck.ai's short-window anti-bot throttle
        #    (not the daily quota) — so we sit out ~6s once and retry before giving up.
        text = messages[-1]["content"] if messages else ""
        on_status("thinking…")
        reply, err = self._send_and_collect(pg, text, on_chunk)
        if err and ("rate limit" in err or "anti-bot" in err):
            on_status("duck.ai is throttling — backing off, retrying…")
            pg.wait_for_timeout(6000)
            reply, err = self._send_and_collect(pg, text, on_chunk)
        if err:
            on_error(err)
            return
        on_done(reply)

    def _send_and_collect(self, pg, text, on_chunk):
        """One send + stream pass. Returns (reply, error_message_or_None)."""
        global _stream_done, _stream_error
        _stream_buf.clear()
        _stream_done = False
        _stream_error = None
        try:
            _send_message(pg, text)
        except Exception as e:
            return "", f"send failed: {e}"

        # forward streamed chunks until the response ends (or we time out)
        seen, waited, limit = 0, 0, 180_000
        while not _stream_done and waited < limit:
            pg.wait_for_timeout(150)
            waited += 150
            while seen < len(_stream_buf):
                on_chunk(_stream_buf[seen])
                seen += 1
        while seen < len(_stream_buf):       # flush whatever landed after done
            on_chunk(_stream_buf[seen])
            seen += 1

        if _stream_error:                    # duck.ai answered with an error status
            return "", _stream_error
        reply = "".join(_stream_buf)
        if not reply.strip():
            return "", ("no response from DuckDuckGo — try again, "
                        "or switch network / VPN if it persists")
        return reply, None


# -----------------------------------------------------------------------------
#  UI
# -----------------------------------------------------------------------------

console = Console()

# the ascii logo. took me longer than i'd like to admit
_LOGO = r"""
 __   __ ___ _    _____ _   _ ___
 \ \ / // _ \ |  |_   _| | | |_ _|
  \ V /|  __/ |__  | | | |_| || |
   \_/  \___|____| |_|  \___/|___|
"""

COMMANDS: list[tuple[str, str]] = [
    ("/help",               "show this help"),
    ("/keys",               "keyboard shortcuts"),
    ("/model",              "list available models"),
    ("/model <n|name>",     "switch model by number or name"),
    ("/theme",              "list color themes"),
    ("/theme <n|name>",     "switch color theme"),
    ("/clear",              "clear the screen (keeps context)"),
    ("/reset",              "erase the conversation (wipes context)"),
    ("/file <path>",        "send a text/code file (add a question after the path)"),
    ("/save [name]",        "save this conversation to disk"),
    ("/history",            "list saved conversations"),
    ("/load <n>",           "load saved conversation by number"),
    ("/delete <n>",         "delete saved conversation by number"),
    ("/delete all",         "delete ALL saved conversations"),
    ("/rename <n> <name>",  "rename a saved conversation"),
    ("/exit",               "quit veltui"),
]

# Unique command words (first token of each entry) for Tab-completion + the
# live suggestion box. Keeps the first description seen for each word.
_CMD_INFO: list[tuple[str, str]] = []
_seen_cmd: set[str] = set()
for _c, _d in COMMANDS:
    _word = _c.split()[0]
    if _word not in _seen_cmd:
        _seen_cmd.add(_word)
        _CMD_INFO.append((_word, _d))


# A suggestion item is a 4-tuple consumed by the live menu:
#   (fill, label, desc, swatch)
#     fill   — the full input string to drop in when this item is chosen
#     label  — left column shown in the menu
#     desc   — right column (may be "")
#     swatch — a hex color to draw a ████ chip, or None
SuggestItem = tuple[str, str, str, str | None]


def _human_size(p: Path) -> str:
    try:
        n = float(p.stat().st_size)
    except OSError:
        return ""
    for u in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}T"


def _file_path_suggestions(lead: str, arg: str) -> list[SuggestItem]:
    """Filesystem path completion for `/file <path>` — Tab through folders/files
    like a shell. Stops once a space (the start of a question) shows up."""
    if " " in arg:                       # path is done, a question is being typed
        return []
    if arg:
        expanded = Path(arg).expanduser()
        if arg.endswith(("/", "\\")):
            base, prefix = expanded, ""
        else:
            base, prefix = expanded.parent, expanded.name
    else:
        base, prefix = Path.cwd(), ""
    dir_text = arg[: len(arg) - len(prefix)]   # the dir part, exactly as typed
    try:
        entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return []
    pl = prefix.lower()
    items: list[SuggestItem] = []
    for e in entries:
        if e.name.startswith(".") and not prefix.startswith("."):
            continue                     # hide dotfiles unless explicitly typing one
        if pl and not e.name.lower().startswith(pl):
            continue
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        # dirs end with "/" and no trailing space (Tab can drill in); files get a
        # trailing space so the next thing you type becomes the question
        sep  = "/" if is_dir else ""
        fill = f"{lead}file {dir_text}{e.name}{sep}" + ("" if is_dir else " ")
        items.append((fill, e.name + sep, "dir" if is_dir else _human_size(e), None))
        if len(items) >= 14:
            break
    return items


def _arg_suggestions(lead: str, word: str, arg: str) -> list[SuggestItem]:
    """Choices for the *argument* of a command (e.g. `/theme <here>`)."""
    if word == "file":
        return _file_path_suggestions(lead, arg)
    arg = arg.strip()
    if " " in arg:                      # past the first argument token — stop
        return []
    al    = arg.lower()
    items: list[SuggestItem] = []
    if word == "theme":
        for name, d in _THEME_DEFS.items():
            if name.startswith(al):
                items.append((f"{lead}theme {name} ", name, "", d["accent"]))
    elif word == "model":
        for m in MODELS:
            alias = m["short"][0] if m["short"] else m["id"]
            hay   = [m["id"].lower(), m["name"].lower(), *(s.lower() for s in m["short"])]
            if al == "" or any(h.startswith(al) for h in hay):
                tag = "think" if m["type"] == "think" else "fast"
                items.append((f"{lead}model {alias} ", m["name"], tag, None))
    elif word == "delete":
        if "all".startswith(al):
            items.append((f"{lead}delete all ", "all",
                          "remove every saved conversation", None))
    return items


def _suggestions_for(value: str) -> list[SuggestItem]:
    """Live-menu items for the current input value (commands, then arguments)."""
    if not value.startswith(("/", ":")):
        return []
    lead  = value[0]
    after = value[1:]
    if " " in after:                    # command word is done — suggest its argument
        word, arg = after.split(" ", 1)
        return _arg_suggestions(lead, word.lower(), arg)
    body = after.lower()                # still typing the command word itself
    return [
        (lead + name[1:] + " ", name, desc, None)
        for (name, desc) in _CMD_INFO
        if name[1:].lower().startswith(body)
    ]


def _help_panel():
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("command",     style="bold green", min_width=24)
    t.add_column("description", style="dim")
    for cmd, desc in COMMANDS:
        t.add_row(cmd, desc)
    return Panel(
        t,
        title=f"[bold]commands[/bold] · {len(COMMANDS)} total",
        border_style="cyan",
        padding=(0, 1),
    )


# everything you can press besides typing — /keys shows this table
_KEYS: list[tuple[str, str]] = [
    ("Tab",     "autocomplete commands and file paths · move through the menu"),
    ("↑ / ↓",   "recall input history · move through the menu"),
    ("Enter",   "send · run a command · open the highlighted folder"),
    ("Ctrl+T",  "next color theme"),
    ("Ctrl+L",  "clear the screen (keeps context)"),
    ("Ctrl+Q",  "quit (/exit works too)"),
]


def _keys_panel():
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("key",    style="bold green", min_width=24)
    t.add_column("action", style="dim")
    for key, action in _KEYS:
        t.add_row(key, action)
    return Panel(
        t,
        title="[bold]keyboard shortcuts[/bold]",
        border_style="cyan",
        padding=(0, 1),
    )


def _models_panel(current_id: str):
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("#",     style="dim", width=3)
    t.add_column("name",  min_width=18)
    t.add_column("type",  width=7)
    t.add_column("id",    style="dim")
    for i, m in enumerate(MODELS, 1):
        active   = m["id"] == current_id
        tag      = "[yellow]think[/yellow]" if m["type"] == "think" else "[green]fast[/green]"
        name_str = f"[bold green]{m['name']}[/bold green]" if active else m["name"]
        marker   = "✓  " if active else "   "
        t.add_row(str(i), name_str, tag, marker + m["id"])
    return Panel(t, title="[bold]models[/bold]", border_style="cyan", padding=(0, 1))


def _history_panel(sessions: list[tuple]):
    if not sessions:
        return Text("no saved conversations", style="dim")
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("#",       style="dim", width=4)
    t.add_column("name",    style="bold", min_width=22)
    t.add_column("model",   style="dim")
    t.add_column("updated", style="dim")
    for i, (sid, name, model, updated) in enumerate(sessions, 1):
        label = name or f"session-{sid}"
        t.add_row(str(i), label, model_display(model), (updated or "")[:16])
    return Panel(t, title="[bold]conversations[/bold]", border_style="cyan", padding=(0, 1))


# -----------------------------------------------------------------------------
#  File attachments (/file) — read a text/code file into the next message
# -----------------------------------------------------------------------------

# files larger than this are refused — a giant paste would choke the composer
# (and look bot-like to duck.ai). 100 KB of code is already a lot to ask about.
_MAX_FILE_BYTES = 100_000

# ext → code-fence language, so the file shows up nicely highlighted on the other end
_LANG_BY_EXT = {
    ".py": "python", ".pyw": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ps1": "powershell",
    ".rb": "ruby", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".sql": "sql",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".md": "markdown", ".xml": "xml", ".lua": "lua",
    ".r": "r", ".pl": "perl", ".dart": "dart", ".scala": "scala", ".vue": "vue",
}


def _clean_path(s: str) -> str:
    """Tidy a pasted path: strip surrounding quotes and a `file://` URI wrapper
    (Linux file managers paste those), so a copied path Just Works."""
    s = s.strip().strip('"').strip("'").strip()
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        p = unquote(urlparse(s).path)
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":   # /C:/x → C:/x on Windows
            p = p[1:]
        s = p
    return s


def _existing_path(s: str) -> bool:
    """True if `s` (a pasted/typed path, possibly quoted) exists on disk.
    Folders count too — pasting a folder opens it in the /file browser."""
    try:
        p = _clean_path(s)
        return bool(p) and Path(p).expanduser().exists()
    except OSError:
        return False


def _collapse_doubled_path(text: str) -> str:
    """Undo a terminal delivering one dropped/pasted path TWICE in a single
    paste event. Seen in the wild, in order of discovery: `"X""X"`, `XX`,
    `X` on two lines, and `"X" "X"` / `X X` with whitespace between the copies.
    Only collapses when the copy actually exists on disk, so normal text
    (even "haha haha") is safe."""
    candidates = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1 and all(ln == lines[0] for ln in lines):
        candidates.append(lines[0])
    for t in (text, *candidates):
        n = len(t)
        if n >= 8 and n % 2 == 0 and t[: n // 2] == t[n // 2:]:
            candidates.append(t[: n // 2].strip())
        # `X/X` or `X\X` — a folder fill ends with "/", so a replayed copy
        # glues on right after the slash (spaces are the token rule's job)
        if n >= 9 and n % 2 == 1 and t[n // 2] in "/\\" and t[: n // 2] == t[n // 2 + 1:]:
            candidates.append(t[: n // 2].strip())
    # the same quoted span repeated, possibly with whitespace between
    spans = re.findall(r'"([^"]+)"', text)
    if len(spans) > 1 and all(s == spans[0] for s in spans):
        if not re.sub(r'"[^"]*"', "", text).strip():    # nothing but the quotes
            candidates.append(spans[0])
    # the same bare token repeated — only path-looking ones (must have a slash),
    # otherwise a message like `meow meow` could get eaten if `meow` existed here
    tokens = text.split()
    if len(tokens) > 1 and all(tk == tokens[0] for tk in tokens):
        if "/" in tokens[0] or "\\" in tokens[0]:
            candidates.append(tokens[0])
    for cand in candidates:
        if _existing_path(cand):
            return cand
    return text


def _collapse_doubled_value(value: str) -> str:
    """Last line of defense against the double-pasted path: runs on every input
    change, so it catches the duplicate no matter HOW it arrived — a second
    Paste event, a keystroke replay (some consoles send a paste BOTH ways),
    anything. Only ever touches a value that is exactly `/file <same path
    twice>` or `<same path twice>` — normal typing can't look like that."""
    s = value.strip()
    if s[:1] in ("/", ":") and s[1:6].lower() == "file ":
        s = s[6:].strip()
    if not s:
        return value
    collapsed = _collapse_doubled_path(s)
    if collapsed == s:
        return value
    path = _clean_path(collapsed)
    quoted = f'"{path}"' if " " in path else path
    try:
        is_dir = Path(path).expanduser().is_dir()
    except OSError:
        is_dir = False
    if is_dir and not path.endswith(("/", "\\")):
        return f"/file {quoted}/"
    return f"/file {quoted} "


def _debug_log(msg: str) -> None:
    """Append to ~/.veltui/debug.log when VELTUI_DEBUG=1 — off by default
    (privacy-first: never log input to disk unless explicitly asked to)."""
    if os.environ.get("VELTUI_DEBUG", "") in ("", "0"):
        return
    try:
        DB_DIR.mkdir(exist_ok=True)
        with open(DB_DIR / "debug.log", "a", encoding="utf-8") as f:
            f.write(f"{monotonic():.3f} {msg}\n")
    except OSError:
        pass


def _fence_for(content: str) -> str:
    """Pick a code-fence longer than any backtick run inside the file, so a file
    that itself contains ``` (e.g. a Markdown doc) can't break out of the block."""
    longest = run = 0
    for ch in content:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _read_attachment(path_arg: str) -> tuple[str, str, str, int]:
    """Read a text/code file for /file. Returns (name, content, lang, nlines).

    Raises FileNotFoundError / IsADirectoryError / ValueError (too big) /
    UnicodeDecodeError (binary) — the caller turns these into tidy messages.
    """
    p = Path(_clean_path(path_arg)).expanduser()
    if not p.exists():
        raise FileNotFoundError(path_arg)
    if p.is_dir():
        raise IsADirectoryError(path_arg)
    size = p.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ValueError(
            f"{p.name} is too big ({size / 1024:.0f} KB) — "
            f"/file caps at {_MAX_FILE_BYTES // 1000} KB"
        )
    data = p.read_bytes()
    if b"\x00" in data:                       # NUL byte ⇒ binary, not even gonna try
        raise UnicodeDecodeError("utf-8", data, 0, 1, "binary file")
    content = data.decode("utf-8")            # raises UnicodeDecodeError on non-text
    content = content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    lang = _LANG_BY_EXT.get(p.suffix.lower(), "")
    nlines = content.count("\n") + 1 if content else 0
    return p.name, content, lang, nlines


# -----------------------------------------------------------------------------
#  Color themes
# -----------------------------------------------------------------------------
#
# CSS uses $background / $surface / $panel / $accent, which Textual derives from
# the active theme — so switching the theme recolors the whole UI for free.
# `accent` is also reused as the logo color (see _logo_renderable).

_THEME_DEFS: dict[str, dict] = {
    "burgundy": dict(primary="#c0395a", accent="#e0556f", background="#180a0f",
                     surface="#291520", panel="#341a27", foreground="#f0dde2"),
    "slate":    dict(primary="#8a93a6", accent="#aab4c8", background="#16181c",
                     surface="#20242b", panel="#2a2f37", foreground="#e6e9ef"),
    "midnight": dict(primary="#cfcfd6", accent="#f5f5f7", background="#08080a",
                     surface="#161618", panel="#202023", foreground="#ededf0"),
    "forest":   dict(primary="#4caf78", accent="#6fd79b", background="#0c130f",
                     surface="#15201a", panel="#1c2a22", foreground="#e1efe7"),
    "violet":   dict(primary="#9d7cff", accent="#b89bff", background="#13101f",
                     surface="#1e1830", panel="#28203f", foreground="#ece6f7"),
    "navy":     dict(primary="#4f8cff", accent="#74a6ff", background="#0a0f1f",
                     surface="#131a2e", panel="#1b243d", foreground="#e3e9f5"),
    "teal":     dict(primary="#2fb6b0", accent="#55d6cf", background="#08161a",
                     surface="#0f2329", panel="#163038", foreground="#def0ef"),
    "amber":    dict(primary="#e0a13a", accent="#f5bf63", background="#19130a",
                     surface="#271e10", panel="#332817", foreground="#f3ebda"),
}

DEFAULT_THEME = "burgundy"


def _build_theme(name: str, d: dict) -> Theme:
    return Theme(
        name=name,
        primary=d["primary"],
        accent=d["accent"],
        background=d["background"],
        surface=d["surface"],
        panel=d["panel"],
        foreground=d["foreground"],
        success="#4caf78",
        warning="#e0a13a",
        error="#ff5f6b",
        dark=True,
    )


def _theme_accent(name: str) -> str:
    return _THEME_DEFS.get(name, _THEME_DEFS[DEFAULT_THEME])["accent"]


# -----------------------------------------------------------------------------
#  App (Textual TUI — fixed logo on top, scrolling chat, input pinned bottom)
# -----------------------------------------------------------------------------

class PromptInput(Input):
    """The message box, with one extra trick: pasting the path of an existing
    file into an empty prompt turns it into a ready `/file <path>` command. So
    copy-path-then-paste (and drag-drop on terminals that paste the dropped path)
    attaches the file in a single step."""

    # last paste seen, as (text, monotonic-time) — used to swallow the duplicate
    _last_paste: tuple[str, float] = ("", 0.0)

    def _on_paste(self, event: events.Paste) -> None:
        raw = (event.text or "").strip()
        _debug_log(f"paste event: {event.text!r}  (value={self.value!r})")
        # Terminals double-deliver a paste/drag-drop in every way imaginable:
        #   1. TWO identical Paste events back-to-back   → swallow the second;
        #   2. ONE event with the text already doubled   → collapse it;
        #   3. once as keystrokes + once as a Paste (or a slow second event) —
        #      the path is already sitting in the prompt → swallow it.
        now = monotonic()
        prev_text, prev_t = self._last_paste
        self._last_paste = (raw, now)
        if raw and raw == prev_text and now - prev_t < 0.6:
            event.stop()
            return
        text = _collapse_doubled_path(raw)
        if text and "\n" not in text and _existing_path(text):
            path = _clean_path(text)
            quoted = f'"{path}"' if " " in path else path
            if Path(path).expanduser().is_dir():
                sep  = "" if path.endswith(("/", "\\")) else "/"
                fill = f"/file {quoted}{sep}"   # folder → open it in the browser
            else:
                fill = f"/file {quoted} "       # file → ready for a question
            # the prompt already holds this path (delivery #3) — replace a bare
            # path with the proper /file fill, otherwise just drop the duplicate
            if path in self.value:
                if self.value.strip() in (raw, text, path, quoted):
                    self.value = fill
                    self.cursor_position = len(self.value)
                event.stop()
                return
            if not self.value.strip():
                self.value = fill
                self.cursor_position = len(self.value)
                event.stop()
                return
        if text != raw:
            # we de-doubled a path but the prompt wasn't empty — insert it once
            if self.selection.is_empty:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, *self.selection)
            event.stop()
            return
        super()._on_paste(event)


class VeltuiTUI(App):
    TITLE = "veltui"

    CSS = """
    Screen {
        background: $background;
    }
    #logo {
        dock: top;
        height: auto;
        padding: 1 2 0 2;
    }
    #chat {
        padding: 0 2 1 2;   /* no top gap — the header's bottom rule hugs the chat */
    }
    #prompt {
        dock: bottom;
        margin: 1 2;
        border: round $accent;
        background: $surface;
    }
    #suggest {
        dock: bottom;
        height: auto;
        max-height: 12;
        margin: 0 2 4 2;
        padding: 0 1;
        background: $panel;
        border: round $accent;
        display: none;
    }
    .msg {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+l", "clear_chat", "clear", show=False),
        Binding("ctrl+t", "cycle_theme", "theme", show=False),
        Binding("tab", "complete", "complete", show=False, priority=True),
        Binding("up", "history_prev", "prev", show=False, priority=True),
        Binding("down", "history_next", "next", show=False, priority=True),
    ]

    def __init__(self, start_model: str | None = None):
        super().__init__()
        self.db = DB()
        if start_model and any(m["id"] == start_model for m in MODELS):
            self.model_id = start_model
            self.db.put("model", start_model)
        else:
            saved = self.db.get("model", DEFAULT_MODEL)
            self.model_id = (
                saved if any(m["id"] == saved for m in MODELS) else DEFAULT_MODEL
            )
        saved_theme = self.db.get("theme", DEFAULT_THEME)
        self._theme_name = saved_theme if saved_theme in _THEME_DEFS else DEFAULT_THEME
        # session_id stays None until the user explicitly /save — by default the
        # conversation lives only in memory and never touches disk.
        self.session_id: int | None = None
        self.messages:   list[dict] = []
        self.backend = ChatBackend()
        self._busy = False
        self._reply_widget: Static | None = None
        self._reply_text  = ""
        self._reply_label = Text()
        # command-suggestion state (Tab / ↑↓ move through these)
        self._suggest_items: list[SuggestItem] = []
        self._suggest_index   = -1     # -1 = nothing highlighted yet
        self._suggest_base    = ""     # the prefix the user actually typed
        self._pending_completions = 0  # how many programmatic input edits are in flight
        # input history (↑/↓ recall past messages & commands, like a shell)
        self._history: list[str] = []
        self._history_index = 0        # == len(history) means "the live draft"
        self._history_draft = ""       # the in-progress line stashed when browsing

    # --- layout ---

    def compose(self) -> ComposeResult:
        yield Static(self._logo_renderable(), id="logo")
        yield VerticalScroll(id="chat")
        yield PromptInput(placeholder="message…    ( /help for commands )", id="prompt")
        yield Static(id="suggest")

    def on_mount(self):
        for name, d in _THEME_DEFS.items():
            self.register_theme(_build_theme(name, d))
        self.theme = self._theme_name
        self.chat = self.query_one("#chat", VerticalScroll)
        self.query_one("#prompt", Input).focus()
        self._add_system(self._welcome_renderable())

    def _welcome_renderable(self):
        accent = _theme_accent(self._theme_name)
        return Group(
            Text.assemble(("✦  ", f"bold {accent}"), ("welcome to veltui", "bold")),
            Text("ask me anything · type / for commands · /keys for shortcuts",
                 style="dim"),
        )

    def on_unmount(self):
        self.backend.shutdown()
        self.backend._thread.join(timeout=5)

    def _logo_renderable(self):
        accent = _theme_accent(self._theme_name)
        logo = Text(_LOGO.strip("\n"), style=f"bold {accent}")
        model_line = Text.assemble(
            ("model: ", "dim"),
            (model_display(self.model_id), "bold green"),
        )
        # logo, air, then a tight line/model/line block right above the chat
        return Group(logo, Text(), Rule(style=accent), model_line, Rule(style=accent))

    # --- chat rendering ---

    def _mount_msg(self, renderable) -> Static:
        w = Static(renderable, classes="msg")
        self.chat.mount(w)
        self._scroll_end()
        return w

    def _add_user(self, text: str, attachments: list[str] | None = None):
        parts = [Text("you", style="bold green")]
        if text:
            parts.append(Text(text))
        if attachments:
            parts.append(Text("+ " + ", ".join(attachments), style="dim"))
        self._mount_msg(Group(*parts))

    def _add_assistant(self, text: str):
        label = Text(model_display(self.model_id), style="bold cyan")
        body  = Markdown(text) if text.strip() else Text("(empty)", style="dim")
        self._mount_msg(Group(label, body))

    def _add_system(self, renderable):
        self._mount_msg(renderable)

    def _scroll_end(self):
        self.call_after_refresh(self.chat.scroll_end, animate=False)

    def _at_bottom(self) -> bool:
        """Is the chat scrolled to (roughly) the bottom? Streaming output only
        sticks to the end while you're already there — scroll up and it leaves you
        alone instead of yanking you back down on every chunk."""
        return self.chat.scroll_offset.y >= self.chat.max_scroll_y - 2

    # --- streaming callbacks (always run on the UI thread) ---

    def _start_reply(self):
        self._reply_text  = ""
        self._reply_label = Text(model_display(self.model_id), style="bold cyan")
        self._reply_widget = Static(
            Group(self._reply_label, Text("●  thinking…", style="dim")),
            classes="msg",
        )
        self.chat.mount(self._reply_widget)
        self._scroll_end()

    def _set_status(self, msg: str):
        if self._reply_widget is not None:
            stick = self._at_bottom()
            self._reply_widget.update(
                Group(self._reply_label, Text(f"●  {msg}", style="dim"))
            )
            if stick:
                self._scroll_end()

    def _append_chunk(self, chunk: str):
        self._reply_text += chunk
        if self._reply_widget is not None:
            stick = self._at_bottom()
            self._reply_widget.update(
                Group(self._reply_label, Text(self._reply_text))
            )
            if stick:
                self._scroll_end()

    def _finish_reply(self, reply: str):
        stick = self._at_bottom()
        body = Markdown(reply) if reply.strip() else Text("(no response)", style="dim")
        if self._reply_widget is not None:
            self._reply_widget.update(Group(self._reply_label, body))
        if reply.strip():
            self.messages.append({"role": "assistant", "content": reply})
        self._reply_widget = None
        self._set_busy(False)
        if stick:
            self._scroll_end()

    def _show_error(self, err: str):
        # drop the unanswered user turn so the next try starts clean
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        msg = Text(f"error: {err}", style="bold red")
        if self._reply_widget is not None:
            self._reply_widget.update(Group(self._reply_label, msg))
            self._reply_widget = None
        else:
            self._add_system(msg)
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        self._busy = busy
        inp = self.query_one("#prompt", Input)
        inp.disabled = busy
        if not busy:
            inp.focus()

    # --- input handling ---

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != "prompt":
            return
        text = event.value.strip()
        # Enter on `/file <folder>` steps INTO the folder (menu shows its
        # contents) instead of erroring — Tab/↑↓ pick, Enter walks the path.
        if text and not self._busy:
            drill = self._dir_drill_value(text)
            if drill is not None:
                if event.input.value != drill:
                    self._pending_completions += 1   # our edit, not typing
                    event.input.value = drill
                event.input.cursor_position = len(drill)
                self._suggest_base  = drill
                self._suggest_index = -1
                self._refresh_suggest(drill)
                return
        event.input.value = ""
        if not text or self._busy:
            return
        # remember it for ↑/↓ recall (skip a back-to-back duplicate)
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = len(self._history)
        self._history_draft = ""
        if text.startswith(("/", ":")):
            self._handle_command(text)
        else:
            self._send(text)

    # --- command autocomplete ---

    def on_input_changed(self, event: Input.Changed):
        if event.input.id != "prompt":
            return
        # a doubled path landed in the input — by whatever route — drop one copy.
        # setting .value re-fires Changed with the fixed text, which runs the
        # normal suggestion logic below.
        fixed = _collapse_doubled_value(event.value)
        if fixed != event.value:
            _debug_log(f"value collapse: {event.value!r} -> {fixed!r}")
            event.input.value = fixed
            event.input.cursor_position = len(fixed)
            return
        if self._pending_completions > 0:   # this edit came from Tab — don't reset
            self._pending_completions -= 1
            return
        # genuine typing: recompute the menu from scratch, clear any highlight
        self._suggest_base  = event.value
        self._suggest_index = -1
        self._refresh_suggest(event.value)

    def _refresh_suggest(self, value: str):
        box   = self.query_one("#suggest", Static)
        items = _suggestions_for(value)
        self._suggest_items = items
        if items:
            box.update(self._suggest_renderable(items, self._suggest_index))
            box.display = True
        else:
            box.display = False
            self._suggest_index = -1

    def _suggest_renderable(self, items, index=-1):
        accent = _theme_accent(self._theme_name)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)   # label
        grid.add_column(no_wrap=True)   # color swatch (themes only)
        grid.add_column()               # description
        for i, (_fill, label, desc, swatch) in enumerate(items):
            chip = Text("████", style=swatch) if swatch else Text("")
            if i == index:
                grid.add_row(Text(f"▸ {label}", style="bold"), chip, Text(desc),
                             style=f"black on {accent}")
            else:
                grid.add_row(Text(f"  {label}", style="bold green"), chip,
                             Text(desc, style="dim"))
        if 0 <= index < len(items) and items[index][1].endswith("/"):
            hint = "Enter to open the folder · ↑↓ or Tab to move"
        elif len(items) > 1:
            hint = "↑↓ or Tab to move · Enter to run"
        else:
            hint = "Tab to complete · Enter to run"
        # a rule draws the boundary between the choices and the hint line
        return Group(grid, Rule(style="dim"), Text(hint, style="dim italic"))

    def action_complete(self):
        """Tab — advance the highlight forward through the live menu."""
        self._move_suggest(1)

    def _move_suggest(self, delta: int):
        items = self._suggest_items
        if not items:
            return
        if self._suggest_index == -1:
            self._suggest_index = 0 if delta > 0 else len(items) - 1
        else:
            self._suggest_index = (self._suggest_index + delta) % len(items)
        fill = items[self._suggest_index][0]
        inp  = self.query_one("#prompt", Input)
        if fill != inp.value:
            self._pending_completions += 1   # this edit is ours — don't treat as typing
            inp.value = fill
            inp.cursor_position = len(fill)
        # Completing the *only* command match commits it, so step the menu into
        # that command's argument choices (e.g. "/theme " → the list of themes).
        if len(items) == 1:
            self._suggest_base  = fill
            self._suggest_index = -1
            self._refresh_suggest(fill)
        else:
            box = self.query_one("#suggest", Static)
            box.update(self._suggest_renderable(items, self._suggest_index))
            box.display = True

    # --- input history (↑/↓) ---

    def action_history_prev(self):
        if self._suggest_items:          # menu open → move the highlight up
            self._move_suggest(-1)
        else:                            # menu closed → recall an earlier line
            self._recall_history(-1)

    def action_history_next(self):
        if self._suggest_items:
            self._move_suggest(+1)
        else:
            self._recall_history(+1)

    def _recall_history(self, delta: int):
        if self._busy or not self._history:
            return
        inp = self.query_one("#prompt", Input)
        if self._history_index >= len(self._history):
            self._history_draft = inp.value     # stash the in-progress line
        new = max(0, min(self._history_index + delta, len(self._history)))
        if new == self._history_index:
            return
        self._history_index = new
        text = self._history_draft if new == len(self._history) else self._history[new]
        # this edit is ours, not typing — don't let it pop the suggestion menu open
        # (otherwise recalling a "/theme …" line would flip ↑/↓ into menu navigation)
        if text != inp.value:
            self._pending_completions += 1
            inp.value = text
        inp.cursor_position = len(text)

    def _send(self, text: str, display_text: str | None = None,
              attachments: list[str] | None = None):
        self._add_user(text if display_text is None else display_text, attachments)
        self.messages.append({"role": "user", "content": text})
        self._set_busy(True)
        self._start_reply()
        self.backend.submit(
            messages=list(self.messages),
            model=self.model_id,
            on_status=lambda s: self.call_from_thread(self._set_status, s),
            on_chunk=lambda c: self.call_from_thread(self._append_chunk, c),
            on_done=lambda r: self.call_from_thread(self._finish_reply, r),
            on_error=lambda e: self.call_from_thread(self._show_error, e),
        )

    # --- commands ---

    def action_clear_chat(self):
        if not self._busy:
            self._cmd_clear()

    def _handle_command(self, line: str):
        if line.startswith(":"):
            line = "/" + line[1:]
        raw   = line[1:]
        parts = raw.split(None, 1)
        cmd   = parts[0].lower()
        args  = parts[1] if len(parts) > 1 else ""

        if cmd in ("help", "h", "?"):
            self._add_system(_help_panel())
        elif cmd in ("keys", "shortcuts", "hotkeys"):
            self._add_system(_keys_panel())
        elif cmd == "model":
            self._cmd_model(args)
        elif cmd == "theme":
            self._cmd_theme(args)
        elif cmd == "clear":
            self._cmd_clear()
        elif cmd == "reset":
            self._cmd_reset()
        elif cmd == "file":
            self._cmd_file(args)
        elif cmd == "save":
            self._cmd_save(args)
        elif cmd == "history":
            self._add_system(_history_panel(self.db.list_sessions()))
        elif cmd == "load":
            self._cmd_load(args)
        elif cmd == "delete":
            self._cmd_delete(args)
        elif cmd == "rename":
            self._cmd_rename(args)
        elif cmd in ("exit", "quit", "q"):
            self.exit()
        else:
            self._add_system(
                Text(f"unknown command: /{cmd}   (try /help)", style="red")
            )

    def _cmd_model(self, args: str):
        if not args:
            self._add_system(_models_panel(self.model_id))
            return
        m = find_model(args)
        if not m:
            self._add_system(
                Text(f"unknown model: {args!r}   (use /model to list)", style="red")
            )
            return
        self.model_id = m["id"]
        self.db.put("model", m["id"])
        if self.session_id:
            self.db.con.execute(
                "UPDATE sessions SET model=? WHERE id=?",
                (m["id"], self.session_id),
            )
            self.db.con.commit()
        self.query_one("#logo", Static).update(self._logo_renderable())
        self._add_system(
            Text.assemble(("switched to ", "green"), (m["name"], "bold"))
        )

    def _themes_panel(self):
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
        t.add_column("#",       style="dim", width=3)
        t.add_column("theme",   min_width=12)
        t.add_column("preview", width=8)
        for i, (name, d) in enumerate(_THEME_DEFS.items(), 1):
            active = name == self._theme_name
            label  = f"[bold green]{name}[/bold green]" if active else name
            marker = "✓ " if active else "  "
            swatch = f"[{d['accent']}]████[/]"
            t.add_row(str(i), marker + label, swatch)
        return Panel(
            t,
            title="[bold]themes[/bold]   ·   /theme <n|name>   ·   Ctrl+T",
            border_style="cyan",
            padding=(0, 1),
        )

    def _resolve_theme(self, query: str) -> str | None:
        q     = query.strip().lower()
        names = list(_THEME_DEFS)
        if q.isdigit():
            idx = int(q) - 1
            return names[idx] if 0 <= idx < len(names) else None
        if q in _THEME_DEFS:
            return q
        cand = [n for n in names if n.startswith(q)]
        return cand[0] if len(cand) == 1 else None

    def _apply_theme(self, name: str):
        self._theme_name = name
        self.theme = name
        self.db.put("theme", name)
        self.query_one("#logo", Static).update(self._logo_renderable())

    def _cmd_theme(self, args: str):
        if not args.strip():
            self._add_system(self._themes_panel())
            return
        name = self._resolve_theme(args)
        if not name:
            self._add_system(
                Text(f"unknown theme: {args!r}   (use /theme to list)", style="red")
            )
            return
        self._apply_theme(name)
        self._add_system(Text.assemble(("theme: ", "green"), (name, "bold")))

    def action_cycle_theme(self):
        names = list(_THEME_DEFS)
        i = (names.index(self._theme_name) + 1) % len(names) \
            if self._theme_name in names else 0
        self._apply_theme(names[i])
        self._add_system(
            Text.assemble(("theme: ", "green"), (names[i], "bold"), ("   (Ctrl+T)", "dim"))
        )

    def _cmd_clear(self):
        """Wipe the visible screen only — the conversation stays in context."""
        self.chat.remove_children()
        kept = len(self.messages)
        if kept:
            self._add_system(Text(
                f"screen cleared · {kept} message{'s' if kept != 1 else ''} "
                f"still in context   (/reset to wipe)",
                style="dim",
            ))
        else:
            self._add_system(Text("screen cleared", style="dim"))

    def _cmd_reset(self):
        """Erase the conversation — drops all context and clears the screen."""
        self.session_id = None
        self.messages = []
        self.chat.remove_children()
        self.backend.new_chat()   # also clear duck.ai's server-side context
        self._add_system(Text("conversation reset — context cleared", style="dim"))

    @staticmethod
    def _parse_file_arg(arg: str) -> tuple[str, str]:
        """Split a /file argument into (path, inline_message).

        `/file "C:\\a b.txt" what's this?`  → path is the quoted span, the rest is
        an inline question that sends immediately. Without quotes we take the
        longest leading run that is actually an existing file, so
        `/file notes.txt summarize` → path `notes.txt` + question `summarize`,
        while `/file my notes.txt` (a real file with a space) stays one path.
        """
        arg = arg.strip()
        if arg[:1] in ('"', "'"):
            q   = arg[0]
            end = arg.find(q, 1)
            if end != -1:
                return arg[1:end], arg[end + 1:].strip()
            return arg[1:].strip(), ""          # unterminated quote
        tokens = arg.split(" ")
        for i in range(len(tokens), 0, -1):
            cand = " ".join(tokens[:i])
            try:
                if cand and Path(_clean_path(cand)).expanduser().exists():
                    return cand, " ".join(tokens[i:]).strip()
            except OSError:
                pass
        return arg, ""                          # nothing exists → clean not-found

    def _dir_drill_value(self, text: str) -> str | None:
        """If the submitted line is `/file <existing folder>` (no question after
        it), return the input value that browses into that folder — the Enter key
        then *opens* folders while completing a path instead of erroring."""
        if text[:1] not in ("/", ":"):
            return None
        rest = text[1:]
        if " " not in rest:
            return None
        word, arg = rest.split(" ", 1)
        if word.lower() != "file" or not arg.strip():
            return None
        path_arg, question = self._parse_file_arg(arg)
        if question:
            return None
        try:
            if not Path(_clean_path(path_arg)).expanduser().is_dir():
                return None
        except OSError:
            return None
        sep = "" if path_arg.endswith(("/", "\\")) else "/"
        return f"{text[0]}file {path_arg}{sep}"

    def _send_file_message(self, name: str, content: str, lang: str, question: str):
        """Send a file's content (as a fenced code block) plus an optional
        question typed on the same line."""
        fence = _fence_for(content)
        block = f"{name}:\n{fence}{lang}\n{content}\n{fence}"
        full  = f"{block}\n\n{question}" if question else block
        self._send(full, display_text=question, attachments=[name])

    def _cmd_file(self, args: str):
        """Read a text/code file and send it right away. Put a question after the
        path to ask about it in the same message: `/file path explain this`."""
        arg = args.strip()
        if not arg:
            self._add_system(Text(
                "usage: /file <path>   ·   /file <path> your question"
                "   (quotes only needed if the path has spaces)",
                style="dim",
            ))
            return
        path_arg, question = self._parse_file_arg(arg)
        try:
            name, content, lang, _ = _read_attachment(path_arg)
        except FileNotFoundError:
            self._add_system(Text(f"file not found: {path_arg}", style="red"))
            return
        except IsADirectoryError:
            self._add_system(Text(f"that's a folder, not a file: {path_arg}", style="red"))
            return
        except ValueError as e:            # too big
            self._add_system(Text(str(e), style="red"))
            return
        except UnicodeDecodeError:
            self._add_system(Text(
                f"can't read {path_arg} — looks like a binary file "
                "(/file only supports text/code)",
                style="red",
            ))
            return
        except OSError as e:
            self._add_system(Text(f"couldn't read {path_arg}: {e}", style="red"))
            return
        self._send_file_message(name, content, lang, question)

    def _cmd_save(self, args: str):
        if not self.messages:
            self._add_system(Text("nothing to save", style="dim"))
            return
        name = args.strip() or None
        # First save → create the session on disk. Re-saving updates it in place.
        if self.session_id is None:
            self.session_id = self.db.new_session(self.model_id)
        if name:
            self.db.rename_session(self.session_id, name)
        self.db.con.execute(
            "UPDATE sessions SET model=? WHERE id=?", (self.model_id, self.session_id)
        )
        self.db.con.commit()
        self.db.replace_messages(self.session_id, self.messages)
        label = name or self.db.session_name(self.session_id) or f"session-{self.session_id}"
        self._add_system(Text.assemble(
            ("saved to disk as ", "green"), (label, "bold"),
            (f"   ({len(self.messages)} messages)", "dim"),
        ))

    def _cmd_load(self, args: str):
        sessions = self.db.list_sessions()
        try:
            idx = int(args.strip()) - 1
            if not (0 <= idx < len(sessions)):
                raise ValueError
        except ValueError:
            self._add_system(
                Text("invalid number — use /history to see the list", style="red")
            )
            return
        sid, name, model, _ = sessions[idx]
        self.session_id = sid
        self.messages   = self.db.get_messages(sid)
        if any(m["id"] == model for m in MODELS):
            self.model_id = model
            self.query_one("#logo", Static).update(self._logo_renderable())
        self.chat.remove_children()
        for msg in self.messages:
            if msg["role"] == "user":
                self._add_user(msg["content"])
            else:
                self._add_assistant(msg["content"])
        label = name or f"session-{sid}"
        self._add_system(Text.assemble(
            ("loaded ", "green"), (label, "bold"),
            (f"   ({len(self.messages)} messages)", "dim"),
        ))

    def _cmd_delete(self, args: str):
        arg = args.strip()
        if arg.lower() == "all":
            n = len(self.db.list_sessions())
            if not n:
                self._add_system(Text("no saved conversations to delete", style="dim"))
                return
            self.db.clear_all()
            self.session_id = None      # detach — current convo stays in memory
            self._add_system(Text.assemble(
                ("deleted all ", "green"), (str(n), "bold"),
                (f" saved conversation{'s' if n != 1 else ''}", "green"),
            ))
            return
        sessions = self.db.list_sessions()
        try:
            idx = int(arg) - 1
            if not (0 <= idx < len(sessions)):
                raise ValueError
        except ValueError:
            self._add_system(
                Text("invalid number — use /history, or /delete all", style="red")
            )
            return
        sid, name, *_ = sessions[idx]
        self.db.delete_session(sid)
        if self.session_id == sid:
            self.session_id = None  # detach — keep the convo in memory, just unsaved
        self._add_system(
            Text.assemble(("deleted ", "green"), (name or f"session-{sid}", "bold"))
        )

    def _cmd_rename(self, args: str):
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            self._add_system(Text("usage: /rename <n> <name>", style="red"))
            return
        sessions = self.db.list_sessions()
        try:
            idx = int(parts[0]) - 1
            if not (0 <= idx < len(sessions)):
                raise ValueError
        except ValueError:
            self._add_system(Text("invalid number", style="red"))
            return
        sid = sessions[idx][0]
        self.db.rename_session(sid, parts[1])
        self._add_system(
            Text.assemble(("renamed to ", "green"), (parts[1], "bold"))
        )


# -----------------------------------------------------------------------------
#  CLI entry point
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    epilog_lines = ["in-app commands:"]
    for cmd, desc in COMMANDS:
        epilog_lines.append(f"  {cmd:<26} {desc}")
    epilog_lines.append(
        f"\n  {len(COMMANDS)} commands total  (also :help / /help inside the app)"
    )

    return argparse.ArgumentParser(
        prog="veltui",
        description="privacy-first AI chat in your terminal, powered by DuckDuckGo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(epilog_lines),
    )


def _add_args(p: argparse.ArgumentParser):
    p.add_argument(
        "-m", "--model",
        metavar="MODEL",
        help="model to start with: name, short alias, or number (1-6)",
    )
    p.add_argument(
        "--list-models", action="store_true",
        help="print available models and exit",
    )
    p.add_argument(
        "--clear-history", action="store_true",
        help="delete all saved conversations and exit",
    )
    p.add_argument(
        "--version", action="version", version="veltui 0.1.0",
    )


def main():
    p = _build_parser()
    _add_args(p)
    args = p.parse_args()

    if args.list_models:
        for i, m in enumerate(MODELS, 1):
            console.print(f"  {i}. [bold]{m['name']:<20}[/bold] [dim]{m['id']}[/dim]")
        sys.exit(0)

    if args.clear_history:
        DB().clear_all()
        console.print("[green]all conversations cleared[/green]")
        sys.exit(0)

    start_model = None
    if args.model:
        m = find_model(args.model)
        if not m:
            console.print(f"[red]unknown model:[/red] {args.model!r}  (try --list-models)")
            sys.exit(1)
        start_model = m["id"]

    VeltuiTUI(start_model=start_model).run()


if __name__ == "__main__":
    main()
