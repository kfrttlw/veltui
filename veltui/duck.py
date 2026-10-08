"""The duck.ai side: a headless Firefox with duck.ai open.

duck.ai protects its chat endpoint with an anti-bot challenge that veltui
doesn't try to imitate. It uses the real page instead: it types into the
page's own composer, clicks the page's own buttons, and reads the reply off
the page's own network response through a small fetch hook (HOOK below).

`Driver` does the page work and must stay on one thread (Playwright's sync
API is bound to the thread that started it). `Duck` gives it that thread and
a job queue, so the UI never blocks; results come back as `Event`s.
"""

from __future__ import annotations

import itertools
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, strftime
from typing import Callable

from .models import MODELS, PAGE_DEFAULT, Model, by_id, name_of

URL = "https://duck.ai/"

# Everything veltui looks for on the page. When duck.ai changes its UI, this
# is what breaks, and `veltui doctor` checks each of them.
COMPOSER = "textarea[name='user-prompt']"
SEND = ("Send", "Ask")                  # "Ask" on the landing page, "Send" in a thread
NEW_CHAT = ("New Chat",)
PICKER = ("Switch model",)              # else the button showing the current model's name
CONFIRM = ("Start New Chat",)           # confirms a model switch
STOP = ("Stop generating", "Stop Generating", "Stop")
POPUPS = ("Got It!", "Got it", "Accept", "Dismiss", "Okay")

# Only what costs nothing to turn off. Trimming Firefox's processes was tried
# and measured: ~1 % less memory, and one of those prefs made Firefox take 30 s
# to shut down. The real saving is closing it while idle (see Duck).
FIREFOX_PREFS = {
    "browser.cache.disk.enable": False,         # nothing written to disk
    "media.autoplay.default": 5,                # never autoplay anything
    "browser.sessionhistory.max_entries": 2,
}

FIRST_REQUEST_S = 20                    # duck.ai must start its request this soon after Send
REPLY_LIMIT_S = 300
STOP_GRACE_S = 3

# Tee duck.ai's own /chat response (clone(), the page still reads its copy) and
# hand each SSE `message` delta to Python. Every request gets a number so a
# late chunk of an old reply can't leak into the next one. The AbortController
# lets veltui stop a reply when the page has no Stop button to click.
HOOK = r"""
(() => {
  if (window.__veltuiHooked) return;
  window.__veltuiHooked = true;
  let seq = 0;
  const live = new Set();
  window.__veltuiSeq = () => seq;
  window.__veltuiAbort = () => {
    live.forEach(c => { try { c.abort(); } catch (e) {} });
    live.clear();
  };
  const emit = (...a) => { try { window.__veltui(...a); } catch (e) {} };
  const raw = s => { const r = window.__veltuiRaw; if (r && r.length < 400) r.push(s); };
  const orig = window.fetch;
  window.fetch = function (input, init) {
    let url = '';
    try { url = typeof input === 'string' ? input : (input && input.url) || String(input); }
    catch (e) {}
    if (!url.includes('duckchat/v1/chat')) return orig.apply(this, arguments);
    const n = ++seq;
    let model = '';
    try {
      const body = init && init.body;
      if (typeof body === 'string') model = String(JSON.parse(body).model || '');
    } catch (e) {}
    emit(n, 'req', model, '');
    let args = arguments, ctl = null;
    try {
      if (typeof AbortSignal.any === 'function') {
        ctl = new AbortController();
        const own = (init && init.signal) || (input && input.signal);
        const signal = own ? AbortSignal.any([own, ctl.signal]) : ctl.signal;
        args = [input, Object.assign({}, init, { signal })];
        live.add(ctl);
      }
    } catch (e) { args = arguments; ctl = null; }
    const drop = () => { if (ctl) live.delete(ctl); };
    const p = orig.apply(this, args);
    p.then(r => {
      if (!r.ok) {
        drop();
        r.clone().text().then(t => emit(n, 'error', String(r.status), (t || '').slice(0, 500)),
                              () => emit(n, 'error', String(r.status), ''));
      } else {
        read(n, r.clone()).finally(drop);
      }
    }, e => {
      drop();
      if (e && e.name === 'AbortError') emit(n, 'done', '', '');
      else emit(n, 'fail', String((e && e.message) || e), '');
    });
    return p;
  };
  async function read(n, resp) {
    let ended = false;
    const end = () => { if (!ended) { ended = true; emit(n, 'done', '', ''); } };
    try {
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).replace(/\r$/, '');
          buf = buf.slice(i + 1);
          if (!line.startsWith('data:')) continue;
          raw(line.slice(0, 300));
          const payload = line.slice(5).trim();
          if (payload === '[DONE]') { end(); continue; }
          let d;
          try { d = JSON.parse(payload); } catch (e) { continue; }
          if (d && (d.action === 'error' || d.type === 'error'))
            emit(n, 'error', String(d.status || ''), payload.slice(0, 500));
          else if (d && typeof d.message === 'string' && d.message)
            emit(n, 'chunk', d.message, '');
        }
      }
      end();
    } catch (e) {
      if (e && e.name === 'AbortError') end();
      else emit(n, 'fail', String((e && e.message) || e), '');
    }
  }
})();
"""


class DuckError(Exception):
    """Something on duck.ai's side, with a message fit to show as is."""


@dataclass
class Event:
    kind: str           # state · status · primed · model · chunk · done · error · warn
    text: str = ""
    job: int = 0
    note: str = ""      # done: "stopped" / "cut off"


# --- talking to duck.ai about the past ----------------------------------------

REPLAY_CHARS = 24_000
REPLAY_EACH = 6_000


def primer(history: list[tuple[str, str]], text: str) -> tuple[str, int]:
    """`text` with the earlier conversation in front of it, for a duck.ai chat
    that hasn't seen it (after loading a chat, a model switch, a crash).
    Returns the message and how many earlier messages made it in."""
    blocks: list[str] = []
    budget = REPLAY_CHARS
    for role, content in reversed(history):
        if len(content) > REPLAY_EACH:
            content = content[:REPLAY_EACH] + "\n[…]"
        block = f"[{role}]\n{content}"
        if blocks and len(block) > budget:
            break
        blocks.append(block)
        budget -= len(block)
    blocks.reverse()
    left_out = len(history) - len(blocks)
    head = ("Context: below is our conversation so far"
            + (f" (its first {left_out} messages left out)" if left_out else "")
            + ". Use it as context and reply only to the new message at the end.")
    return f"{head}\n\n" + "\n\n".join(blocks) + f"\n\n[new message]\n{text}", len(blocks)


def explain(status: str, body: str) -> str:
    """duck.ai's error answer → a sentence."""
    if "ERR_BN_LIMIT" in body or status == "429":
        return "DuckDuckGo rate limit — wait a few minutes, or switch network / VPN"
    if "ERR_CHALLENGE" in body:
        return "DuckDuckGo's anti-bot check refused the request — try again in a bit"
    code = next((w.strip('",:{}') for w in body.split() if "ERR_" in w), "")
    return f"DuckDuckGo error {status or ''} {code}".strip()


def _short(e: BaseException) -> str:
    """A Playwright error's first line, without the call-log noise."""
    s = str(e).strip()
    for line in s.splitlines():
        line = line.strip()
        if line and not line.startswith("="):
            s = line
            break
    for prefix in ("Locator.", "Page.", "BrowserType.", "Browser.", "ElementHandle."):
        if s.startswith(prefix) and ": " in s:
            s = s.split(": ", 1)[1]
    return s[:240]


# --- the page --------------------------------------------------------------------

class Driver:
    """Owns Firefox and the duck.ai tab. Call it from one thread only."""

    _install_tried = False

    def __init__(self, post: Callable[[Event], None], *, headless: bool = True):
        self.post = post
        self.headless = headless
        self.pw = self.browser = self.page = None
        self.picked: Model | None = None    # what the page's picker has selected
        self.turns = 0                      # messages sent in duck.ai's current chat
        self.streamed = False               # the last ask() got part of a reply
        self._inbox: list[tuple] = []

    # --- browser life ---

    def alive(self) -> bool:
        try:
            return (self.page is not None and not self.page.is_closed()
                    and self.browser is not None and self.browser.is_connected())
        except Exception:
            return False

    def open(self):
        if self.alive():
            return self.page
        self.close()
        self.post(Event("state", "starting"))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise DuckError("playwright isn't installed — pip install playwright") from None
        self.post(Event("status", "starting Firefox…"))
        self.pw = sync_playwright().start()
        self.browser = self._launch()
        ctx = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",                 # the button labels above are English
            reduced_motion="reduce",        # no CSS animations to paint for nobody
        )
        pg = ctx.new_page()
        pg.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        pg.add_init_script(HOOK)
        pg.expose_function("__veltui", self._on_page_event)
        self.post(Event("status", "opening duck.ai…"))
        try:
            pg.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            raise DuckError(f"duck.ai didn't load — are you online? ({_short(e)})") from None
        try:
            pg.wait_for_selector(COMPOSER, timeout=20_000)
        except Exception:
            raise DuckError("duck.ai loaded, but its message box isn't there "
                            "(run `veltui doctor`)") from None
        pg.wait_for_timeout(800)
        self.page = pg
        self.picked = PAGE_DEFAULT
        self.turns = 0
        return pg

    def _launch(self):
        def go():
            return self.pw.firefox.launch(headless=self.headless,
                                          firefox_user_prefs=FIREFOX_PREFS)
        try:
            return go()
        except Exception as e:
            missing = "Executable doesn't exist" in str(e) or "playwright install" in str(e)
            if not missing:
                raise DuckError(f"Firefox didn't start: {_short(e)}") from None
            if Driver._install_tried:
                raise DuckError("Firefox isn't installed — run: playwright install firefox") \
                    from None
        Driver._install_tried = True
        self.post(Event("status", "first run: downloading Firefox (~90 MB, once)…"))
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "firefox"],
                           capture_output=True, text=True)
        if r.returncode:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            raise DuckError(f"couldn't download Firefox ({tail[0][:160]}) — "
                            "run: playwright install firefox")
        try:
            return go()
        except Exception as e:
            raise DuckError(f"Firefox didn't start: {_short(e)}") from None

    def close(self):
        for thing, how in ((self.page, "close"), (self.browser, "close"), (self.pw, "stop")):
            if thing is not None:
                try:
                    getattr(thing, how)()
                except Exception:
                    pass
        self.pw = self.browser = self.page = None
        self.turns = 0

    # --- page events (called by Playwright on this same thread) ---

    def _on_page_event(self, seq, kind, a="", b=""):
        self._inbox.append((int(seq), str(kind), str(a or ""), str(b or "")))

    def _take(self) -> list[tuple]:
        out, self._inbox = self._inbox, []
        return out

    # --- clicking ---

    def click(self, labels: tuple[str, ...], timeout: int = 6_000) -> str:
        """Click the first visible button with one of `labels`.
        Returns "clicked", "disabled" or "missing"."""
        seen_disabled = False
        for label in labels:
            loc = self.page.get_by_role("button", name=label, exact=True)
            try:
                for i in range(loc.count()):
                    b = loc.nth(i)
                    if not b.is_visible():
                        continue
                    if not b.is_enabled():
                        seen_disabled = True
                        continue
                    b.click(timeout=timeout)
                    return "clicked"
            except Exception:
                continue
        return "disabled" if seen_disabled else "missing"

    def dismiss(self):
        """Close a popup that may cover the composer (the 'Got It!' card …)."""
        for label in POPUPS:
            try:
                b = self.page.get_by_role("button", name=label, exact=True)
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=1_200)
            except Exception:
                pass

    # --- chats and models ---

    def new_chat(self):
        """Start an empty duck.ai chat; reload the page if the button is gone."""
        if self.click(NEW_CHAT) != "clicked":
            self.page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        self.page.wait_for_selector(COMPOSER, timeout=15_000)
        self.page.wait_for_timeout(500)
        self.turns = 0

    def open_picker(self) -> bool:
        if self.click(PICKER) == "clicked":
            return True
        # on the landing page the picker is the button with the current model's name
        for frag in dict.fromkeys(m.ui.split()[0] for m in MODELS):
            loc = self.page.get_by_role("button", name=frag)
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=4_000)
                    return True
            except Exception:
                continue
        return False

    def switch(self, model: Model):
        """Select `model` in duck.ai's picker — which starts a new duck.ai chat.

        Every click is a real one, never a JS .click(): duck.ai's bot check
        looks at event.isTrusted.
        """
        pg = self.page
        self.dismiss()
        if not self.open_picker():
            raise DuckError("the model picker button wasn't found")
        pg.wait_for_timeout(700)
        dialog = pg.get_by_role("dialog")
        root = dialog.first if dialog.count() else pg
        card = root.get_by_text(model.ui, exact=False)
        if not card.count():
            pg.keyboard.press("Escape")
            raise DuckError(f"duck.ai's picker has no “{model.ui}”")
        card.first.click(timeout=6_000)
        pg.wait_for_timeout(400)
        self.click(CONFIRM)
        pg.wait_for_timeout(800)
        pg.wait_for_selector(COMPOSER, timeout=10_000)
        # a breath before the first message, the way a person would take one
        pg.wait_for_timeout(1_200)
        self.picked = model
        self.turns = 0

    # --- sending ---

    def type_and_send(self, text: str):
        pg = self.page
        self.dismiss()
        box = pg.locator(COMPOSER).first
        # duck.ai disables the box while it answers, and keeps it disabled when
        # it's rate-limiting — wait a little, then say so instead of hanging
        deadline = monotonic() + 10
        while True:
            try:
                if box.count() and box.is_editable():
                    break
            except Exception:
                pass
            if monotonic() > deadline:
                if box.count():
                    raise DuckError("duck.ai keeps its message box disabled — it may be "
                                    "rate-limiting you; wait a bit or switch network / VPN")
                raise DuckError("duck.ai's message box is gone (run `veltui doctor`)")
            pg.wait_for_timeout(300)
        box.click(timeout=8_000)
        box.fill(text)
        pg.wait_for_timeout(150)
        self.dismiss()
        how = self.click(SEND, timeout=8_000)
        if how == "disabled":
            raise DuckError("duck.ai won't send this — the message may be too long")
        if how == "missing":
            raise DuckError("duck.ai's Send button wasn't found (run `veltui doctor`)")

    def ask(self, text: str, want: Model | None, job: int = 0,
            stopped: Callable[[], bool] = lambda: False) -> tuple[str, str, str | None]:
        """Send `text`, stream the reply as chunk events.
        Returns (reply, note, error) — the reply may be partial with an error."""
        pg = self.page
        self._take()
        self.streamed = False
        base = int(pg.evaluate("window.__veltuiSeq ? window.__veltuiSeq() : 0"))
        self.type_and_send(text)
        n = None
        parts: list[str] = []
        t0 = monotonic()
        stopping = 0.0
        aborted = False
        while True:
            pg.wait_for_timeout(100)        # lets Playwright deliver the page's events
            fresh: list[str] = []
            finished, error = False, None
            for seq, kind, a, b in self._take():
                if seq <= base:
                    continue
                if n is None and kind == "req":
                    n = seq
                    self.turns += 1
                    self._check_model(a, want, job)
                    continue
                if seq != n:
                    continue
                if kind == "chunk":
                    fresh.append(a)
                elif kind == "done":
                    finished = True
                elif kind == "error":
                    error = explain(a, b)
                elif kind == "fail":
                    error = f"the connection to duck.ai broke ({a[:120]})"
            if fresh:
                parts.extend(fresh)
                self.streamed = True
                self.post(Event("chunk", "".join(fresh), job))
            reply = "".join(parts)
            if error:
                return reply, "cut off" if reply else "", error
            if finished:
                return reply, "stopped" if stopping else "", None
            now = monotonic()
            if stopped() and not stopping:
                stopping = now
            if stopping and n is not None and not aborted:
                aborted = True
                if self.click(STOP, timeout=2_000) != "clicked":
                    pg.evaluate("window.__veltuiAbort && window.__veltuiAbort()")
            if stopping and now - stopping > STOP_GRACE_S:
                return reply, "stopped", None
            if n is None and not stopping and now - t0 > FIRST_REQUEST_S:
                raise DuckError("duck.ai never sent the message — the Send click did "
                                "nothing (run `veltui doctor`)")
            if now - t0 > REPLY_LIMIT_S:
                return reply, "cut off", f"no end of the reply after {REPLY_LIMIT_S} s"

    def _check_model(self, used: str, want: Model | None, job: int):
        if not used:
            return
        self.post(Event("model", used, job))
        actual = by_id(used)
        if want and used != want.id:
            self.picked = actual           # so the next message tries the switch again
            self.post(Event("warn", f"asked for {want.name}, but duck.ai answered with "
                                    f"{name_of(used)} — the switch didn't take", job))


# --- the worker thread -------------------------------------------------------------

class Duck:
    """A Driver on its own thread. Every method here is safe to call from the UI.

    After `idle` seconds without a job Firefox is closed (≈400 MB back); the
    next job starts it again, and the primer carries the conversation over.
    """

    def __init__(self, post: Callable[[Event], None], *, headless: bool = True,
                 idle: float = 600):
        self._post = post
        self.idle = idle
        self._drv = Driver(post, headless=headless)
        self._jobs: queue.Queue = queue.Queue()
        self._ids = itertools.count(1)
        self._stop_upto = 0                 # chat jobs up to this id are cancelled
        self._thread = threading.Thread(target=self._run, name="veltui-duck", daemon=True)
        self._thread.start()

    def warm(self, model: Model):
        """Start Firefox, open duck.ai and pick `model` before anyone asks."""
        self._jobs.put(("warm", 0, (model,)))

    def chat(self, text: str, model: Model, *, fresh: bool, primed: str | None = None) -> int:
        """Ask `text`. `fresh`: in a new duck.ai chat. `primed`: the same message
        with the conversation so far in front — sent instead whenever the duck.ai
        chat turns out to be empty (a new one, a model switch, a restart)."""
        job = next(self._ids)
        self._jobs.put(("chat", job, (text, model, fresh, primed)))
        return job

    def stop(self, job: int):
        self._stop_upto = max(self._stop_upto, job)

    def close(self, timeout: float = 5.0):
        self._stop_upto = sys.maxsize
        self._jobs.put(None)
        self._thread.join(timeout)

    # --- on the worker thread ---

    def _run(self):
        while True:
            try:
                item = self._jobs.get(timeout=self.idle or None)
            except queue.Empty:
                if self._drv.alive():
                    self._drv.close()
                    self._post(Event("state", "asleep"))
                continue
            if item is None:
                self._drv.close()
                return
            kind, job, args = item
            try:
                if kind == "warm":
                    self._warm(*args)
                else:
                    self._chat(job, *args)
            except Exception as e:          # a dead worker would freeze the app
                self._failed(job, e)

    def _warm(self, model: Model):
        d = self._drv
        d.open()
        if d.picked != model:
            try:
                d.switch(model)
            except DuckError as e:
                d.picked = None
                self._post(Event("warn", f"couldn't switch to {model.name}: {e}"))
        self._post(Event("state", "ready"))

    def _chat(self, job: int, text: str, model: Model, fresh: bool, primed: str | None):
        try:
            self._chat_once(job, text, model, fresh, primed)
        except Exception:
            d = self._drv
            # Playwright only notices a dead Firefox when it next talks to it.
            # Nothing reached duck.ai then, so start over in a new browser.
            if d.alive() or d.streamed or job <= self._stop_upto:
                raise
            d.close()
            self._post(Event("status", "Firefox was gone — starting it again…", job))
            self._chat_once(job, text, model, fresh, primed)

    def _chat_once(self, job: int, text: str, model: Model, fresh: bool,
                   primed: str | None):
        def stopped():
            return job <= self._stop_upto
        if stopped():
            self._post(Event("done", "", job, "stopped"))
            return
        d = self._drv
        self._post(Event("state", "busy"))
        d.open()
        if fresh and d.turns:
            self._post(Event("status", "new duck.ai chat…", job))
            d.new_chat()
        if d.picked != model:
            self._post(Event("status", f"switching to {model.name}…", job))
            try:
                d.switch(model)
            except DuckError as e:
                d.picked = None
                self._post(Event("warn", f"couldn't switch to {model.name}: {e}", job))
        if stopped():
            self._post(Event("done", "", job, "stopped"))
            self._post(Event("state", "ready"))
            return
        message = text
        if primed and d.turns == 0:
            message = primed
            self._post(Event("primed", "", job))
        self._post(Event("status", "thinking…", job))
        reply, note, err = d.ask(message, model, job, stopped)
        if err and not reply and not stopped() and ("rate limit" in err or "anti-bot" in err):
            # duck.ai's short-window throttle: sit it out once, then try again
            self._post(Event("status", "duck.ai is throttling — retrying in 6 s…", job))
            d.page.wait_for_timeout(6_000)
            if not stopped():
                reply, note, err = d.ask(message, model, job, stopped)
        if err and not reply:
            self._post(Event("error", err, job))
        else:
            if err:
                self._post(Event("warn", err, job))
            self._post(Event("done", reply, job, note))
        self._post(Event("state", "ready"))

    def _failed(self, job: int, e: Exception):
        d = self._drv
        if isinstance(e, DuckError):
            msg = str(e)
        else:
            msg = f"{type(e).__name__}: {_short(e)}"
        if not d.alive():
            d.close()
            if not isinstance(e, DuckError):
                msg = "Firefox closed or crashed — it starts again with your next message"
            self._post(Event("state", "offline", note=msg))
        else:
            self._post(Event("state", "ready"))
        if job:
            self._post(Event("error", msg, job))
        elif d.alive() or isinstance(e, DuckError):
            self._post(Event("warn", msg))


# --- veltui doctor ---------------------------------------------------------------

_BUTTONS_JS = """
() => [...document.querySelectorAll('button,[role=button]')]
  .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim().replace(/\\s+/g, ' '))
  .filter(Boolean)
"""


def doctor(*, send: bool = False, show: bool = False, out: Path | None = None) -> int:
    """Check every piece of duck.ai veltui relies on; write a report a fix can
    be made from. Returns the process exit code."""
    out = out or Path.home() / ".cache" / "veltui" / f"doctor-{strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    bad = 0

    def line(ok: bool | None, what: str, detail: str = ""):
        nonlocal bad
        mark = {True: "ok ", False: "BAD", None: " - "}[ok]
        bad += ok is False
        s = f"[{mark}] {what}" + (f"  — {detail}" if detail else "")
        print(s, flush=True)
        report.append(s)

    def status(ev: Event):
        if ev.kind in ("status", "warn"):
            print(f"      {ev.text}", flush=True)

    drv = Driver(status, headless=not show)
    try:
        t0 = monotonic()
        try:
            pg = drv.open()
        except DuckError as e:
            line(False, "open duck.ai", str(e))
            return 1
        line(True, f"duck.ai loaded in {monotonic() - t0:.1f} s",
             f"Firefox {drv.browser.version}")
        report.append(f"      url: {pg.url}   title: {pg.title()}")
        buttons = pg.evaluate(_BUTTONS_JS)
        (out / "buttons.txt").write_text("\n".join(buttons) + "\n", encoding="utf-8")
        box = pg.locator(COMPOSER)
        line(bool(box.count()) and box.first.is_editable(), "message box", COMPOSER)
        for what, labels in (("send button", SEND), ("new chat button", NEW_CHAT),
                             ("model picker button", PICKER)):
            found = [lb for lb in labels
                     if pg.get_by_role("button", name=lb, exact=True).count()]
            ok = bool(found) if what != "model picker button" else (True if found else None)
            line(ok, what, f"found {found[0]!r}" if found else f"none of {list(labels)}")
        if drv.open_picker():
            pg.wait_for_timeout(800)
            dialog = pg.get_by_role("dialog")
            text = (dialog.first if dialog.count() else pg.locator("body")).inner_text()
            (out / "picker.txt").write_text(text, encoding="utf-8")
            line(True, "model picker opens", "dialog" if dialog.count() else "no dialog role")
            for m in MODELS:
                line(m.ui.lower() in text.lower(), f"  model {m.name}", f"label {m.ui!r}")
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(400)
        else:
            line(False, "model picker opens", "no picker button found")
        if send:
            pg.evaluate("window.__veltuiRaw = []")
            used: list[str] = []
            drv.post = lambda ev: used.append(ev.text) if ev.kind == "model" else status(ev)
            try:
                reply, note, err = drv.ask("Reply with exactly one word: pong", None)
                line(err is None and bool(reply.strip()), "test message",
                     err or f"reply {reply.strip()[:60]!r} {note}".strip())
                line(bool(used), "request model", used[0] if used else "not seen")
            except DuckError as e:
                line(False, "test message", str(e))
            rawlines = pg.evaluate("window.__veltuiRaw || []")
            (out / "stream.txt").write_text("\n".join(rawlines) + "\n", encoding="utf-8")
        else:
            line(None, "test message", "skipped (add --send to try one)")
        return 1 if bad else 0
    except Exception as e:
        line(False, "unexpected", f"{type(e).__name__}: {_short(e)}")
        return 1
    finally:
        try:
            if drv.page is not None:
                drv.page.screenshot(path=str(out / "page.png"), full_page=True)
        except Exception:
            pass
        drv.close()
        (out / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"\nreport: {out}", flush=True)
