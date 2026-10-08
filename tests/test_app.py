"""The app, headless, with a fake duck.ai thread that answers like the real one."""

import asyncio
import threading
import time

import pytest
from textual import events

from veltui.app import Veltui
from veltui.duck import Event
from veltui.store import Store

REPLY = "Use **ss**:\n\n```bash\nss -tulpn\n```\n\n- `-t` tcp\n- `-l` listening\n"


class FakeDuck:
    def __init__(self, post, headless=True, idle=600):
        self.post = post
        self.calls = []
        self.warmed = []
        self.stopped = set()
        self.fail = False
        self.delay = 0.005

    def warm(self, model):
        self.warmed.append(model.id)
        self.post(Event("state", "ready"))

    def chat(self, text, model, *, fresh, primed=None):
        job = len(self.calls) + 1
        self.calls.append(dict(text=text, model=model.id, fresh=fresh, primed=primed))
        threading.Thread(target=self._answer, args=(job, model, fresh, primed),
                         daemon=True).start()
        return job

    def _answer(self, job, model, fresh, primed):
        p = self.post
        p(Event("state", "busy"))
        time.sleep(0.05)
        if fresh and primed:
            p(Event("primed", "", job))
        if self.fail:
            p(Event("error", "DuckDuckGo rate limit — wait a few minutes", job))
        else:
            p(Event("model", model.id, job))
            out = ""
            for i in range(0, len(REPLY), 8):
                if job in self.stopped:
                    p(Event("done", out, job, "stopped"))
                    break
                out += REPLY[i:i + 8]
                p(Event("chunk", REPLY[i:i + 8], job))
                time.sleep(self.delay)
            else:
                p(Event("done", out, job))
        p(Event("state", "ready"))

    def stop(self, job):
        self.stopped.add(job)

    def close(self, timeout=5):
        pass


@pytest.fixture
def run(tmp_path, monkeypatch):
    """run(scenario): scenario(app, pilot, duck) runs inside a headless veltui."""
    monkeypatch.chdir(tmp_path)

    def go(scenario, size=(100, 30)):
        async def main():
            app = Veltui(store=Store(tmp_path / "veltui.db"), duck_factory=FakeDuck)
            app.copy_to_clipboard = lambda text: app.__dict__.setdefault("copied", []) \
                .append(text)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.1)
                await scenario(app, pilot, app.duck)
        asyncio.run(main())
    return go


async def idle(app, pilot, timeout=5.0):
    t = time.monotonic()
    while app.job and time.monotonic() - t < timeout:
        await pilot.pause(0.05)
    await pilot.pause(0.05)


async def say(app, pilot, text):
    await pilot.press(*text, "enter")
    await idle(app, pilot)


def paste(app, text):
    app.post_message(events.Paste(text))        # how a terminal's paste arrives


def test_paste_lands_once_and_keeps_lines(run):
    async def s(app, pilot, duck):
        paste(app, "hello world")
        await pilot.pause(0.1)
        assert app.prompt.text == "hello world"
        app._set_prompt("")
        paste(app, "a\nb\n  c")
        await pilot.pause(0.1)
        assert app.prompt.text == "a\nb\n  c"
    run(s)


def test_pasted_path_attaches(run, tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n")

    async def s(app, pilot, duck):
        paste(app, f"'file://{tmp_path}/x.py'")
        await pilot.pause(0.1)
        assert app.prompt.text == "" and [a.name for a in app.attachments] == ["x.py"]
        await say(app, pilot, "explain")
        assert duck.calls[-1]["text"].startswith("x.py:\n```python\nx = 1\n```")
        assert duck.calls[-1]["text"].endswith("\n\nexplain")
        assert not app.attachments
    run(s)


def test_chat_flow_and_context(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "first")
        assert duck.calls[-1] == dict(text="first", model="gpt-5-mini", fresh=True,
                                      primed=None)
        assert [m.role for m in app.messages] == ["user", "assistant"] and app.synced
        assert app.messages[-1].content == REPLY
        await say(app, pilot, "second")
        assert duck.calls[-1]["fresh"] is False
        await pilot.press(*"/model claude", "enter")
        await pilot.pause(0.1)
        assert app.model.id == "claude-haiku-4-5" and duck.warmed[-1] == "claude-haiku-4-5"
        await say(app, pilot, "third")
        c = duck.calls[-1]
        assert c["fresh"] and c["primed"].count("[user]") == 2
        assert c["primed"].endswith("[new message]\nthird")
    run(s)


def test_esc_stops_and_keeps_the_partial_reply(run):
    async def s(app, pilot, duck):
        duck.delay = 0.1
        await pilot.press(*"go", "enter")
        while not (app.pending and app.pending.msg.content):
            await pilot.pause(0.05)
        await pilot.press("escape")
        await idle(app, pilot)
        m = app.messages[-1]
        assert m.role == "assistant" and m.note == "stopped" and 0 < len(m.content) < len(REPLY)
    run(s)


def test_failed_question_returns_to_the_prompt(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "ok")
        duck.fail = True
        await say(app, pilot, "this fails")
        assert app.prompt.text == "this fails"
        assert app.messages[-1].role == "assistant" and not app.synced
        duck.fail = False
        await pilot.press("enter")
        await idle(app, pilot)
        assert duck.calls[-1]["fresh"] and duck.calls[-1]["primed"]
        assert app.messages[-1].content == REPLY
    run(s)


def test_retry_and_edit(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "q1")
        await say(app, pilot, "q2")
        await pilot.press("escape", "r")
        await idle(app, pilot)
        assert duck.calls[-1]["fresh"] and duck.calls[-1]["text"] == "q2"
        assert len(app.messages) == 4
        await pilot.press("escape", "e")
        await pilot.pause(0.1)
        assert app.prompt.text == "q2" and len(app.messages) == 2
    run(s)


def test_copy_keys(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "q")
        await pilot.press("escape", "y", "c")
        assert app.copied == [REPLY, "ss -tulpn"]
    run(s)


def test_history_recall(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "one")
        await say(app, pilot, "two")
        await pilot.press("up")
        assert app.prompt.text == "two"
        await pilot.press("up")
        assert app.prompt.text == "one"
        await pilot.press("down", "down")
        assert app.prompt.text == ""
    run(s)


def test_menu_and_file_completion(run, tmp_path):
    (tmp_path / "my dir").mkdir()
    (tmp_path / "my dir" / "a b.txt").write_text("hi\n")

    async def s(app, pilot, duck):
        await pilot.press("slash", "m", "o")
        assert [i[1].split()[0] for i in app.menu] == ["/model"]
        await pilot.press("tab")
        assert app.prompt.text == "/model " and len(app.menu) == 6
        app._set_prompt("")
        await pilot.press(*"/file my")
        assert [i[1] for i in app.menu] == ["my dir/"]
        await pilot.press("tab")
        assert [i[1] for i in app.menu] == ["a b.txt"]
        await pilot.press("tab", *"why", "enter")
        await idle(app, pilot)
        assert duck.calls[-1]["text"].startswith("a b.txt:") and \
            duck.calls[-1]["text"].endswith("why")
    run(s)


def test_save_history_open(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "keep me")
        await pilot.press("ctrl+s")
        await say(app, pilot, "more")
        assert app.store.chats()[0].count == 4          # saved chats save themselves
        await pilot.press("ctrl+n")
        await say(app, pilot, "throwaway")
        await say(app, pilot, "throwaway 2")
        await pilot.press("escape", "2", "enter")
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "Confirm"    # unsaved chat in the way
        await pilot.press("y")
        await pilot.pause(0.1)
        assert app.tab == "chat" and len(app.messages) == 4 and not app.synced
        await say(app, pilot, "continue")
        assert duck.calls[-1]["fresh"] and duck.calls[-1]["primed"].count("[assistant]") == 2
        await pilot.press("escape", "2", "d", "y")
        await pilot.pause(0.1)
        assert app.store.chats() == [] and app.chat_id is None
    run(s)


def test_a_path_is_a_message_not_a_command(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "/etc/hostname what is it")
        assert duck.calls[-1]["text"] == "/etc/hostname what is it"
        await pilot.press(*"/nope", "enter")
        await pilot.pause(0.05)
        assert any("unknown command" in str(n.render()) for n in app.query(".note"))
    run(s)


def test_export(run, tmp_path):
    async def s(app, pilot, duck):
        await say(app, pilot, "q")
        await pilot.press(*"/export out.md", "enter")
        await pilot.pause(0.1)
        text = (tmp_path / "out.md").read_text()
        assert text.startswith("# q\n") and "## you" in text and "ss -tulpn" in text
    run(s)


def test_small_terminal(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "hi")
        assert len(app.messages) == 2
    run(s, size=(50, 16))


def test_idle_app_does_not_tick(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "hi")
        await pilot.pause(0.3)
        assert app._ticker._active.is_set() is False      # paused: no wakeups while idle
        duck.delay = 0.05
        await pilot.press(*"again", "enter")
        await pilot.pause(0.1)
        assert app._ticker._active.is_set()
        await idle(app, pilot)
    run(s)


def test_asleep_firefox_wakes_up_as_you_type(run):
    async def s(app, pilot, duck):
        await say(app, pilot, "hi")
        app.post_message(__import__("veltui.app", fromlist=["DuckEvent"]).DuckEvent(
            Event("state", "asleep")))
        await pilot.pause(0.1)
        assert app.duck_state == "asleep"
        n = len(duck.warmed)
        await pilot.press("h", "e", "y")
        assert len(duck.warmed) == n + 1                   # one wake-up, not one per key
    run(s)
