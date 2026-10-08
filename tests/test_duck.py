"""The duck.ai driver in a real headless Firefox, against tests/fakeduck.py.

Skipped when Playwright's Firefox can't start (`playwright install firefox`).
"""

import os
import signal
import time
from pathlib import Path

import pytest

from veltui import duck
from veltui.models import by_id

from . import fakeduck

GPT5, CLAUDE = by_id("gpt-5-mini"), by_id("claude-haiku-4-5")


@pytest.fixture(scope="module", autouse=True)
def site():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.firefox.launch(headless=True).close()
    except Exception as e:                      # no playwright, no Firefox, no libs
        pytest.skip(f"Firefox unavailable: {str(e).splitlines()[0][:80]}")
    url = fakeduck.serve()
    old = duck.URL, duck.REPLY_LIMIT_S
    duck.URL, duck.REPLY_LIMIT_S = url, 4
    yield
    duck.URL, duck.REPLY_LIMIT_S = old


@pytest.fixture(autouse=True)
def reset():
    fakeduck.STATE.update(mode="ok", page="normal", requests=[], aborted=0)


class Run:
    def __init__(self):
        self.events = []
        self.d = duck.Duck(self.events.append)

    def ask(self, text, model, fresh=False, primed=None, timeout=40):
        job = self.d.chat(text, model, fresh=fresh, primed=primed)
        t = time.time()
        while time.time() - t < timeout:
            end = next((e for e in self.events if e.job == job and e.kind in ("done", "error")),
                       None)
            if end:
                return job, end
            time.sleep(0.05)
        raise AssertionError(f"no answer to {text!r}")

    def kinds(self, job):
        return [e.kind for e in self.events if e.job == job]


@pytest.fixture
def r():
    run = Run()
    yield run
    run.d.close()


def last():
    return fakeduck.STATE["requests"][-1]


def test_chat_switch_fresh_and_primer(r):
    job, end = r.ask("hello", GPT5, fresh=True)
    assert end.kind == "done" and end.text.startswith("echo[gpt-5-mini] hello")
    assert [e.text for e in r.events if e.job == job and e.kind == "model"] == ["gpt-5-mini"]
    r.ask("again", GPT5)
    assert len(last()["messages"]) == 2                    # same duck.ai chat
    job, _ = r.ask("switched", CLAUDE, primed="Context: P")
    assert last()["model"] == "claude-haiku-4-5" and last()["messages"] == \
        [{"role": "user", "content": "Context: P"}] and "primed" in r.kinds(job)
    r.ask("plain", CLAUDE, primed="NOT THIS")
    assert last()["messages"][-1]["content"] == "plain"
    r.ask("new chat", CLAUDE, fresh=True, primed="Context: Q")
    assert last()["messages"] == [{"role": "user", "content": "Context: Q"}]


def test_stop(r):
    fakeduck.STATE["mode"] = "slow"
    job = r.d.chat("long", GPT5, fresh=True)
    while not any(e.job == job and e.kind == "chunk" for e in r.events):
        time.sleep(0.05)
    t = time.time()
    r.d.stop(job)
    while not any(e.job == job and e.kind == "done" for e in r.events):
        time.sleep(0.05)
        assert time.time() - t < 6
    end = next(e for e in r.events if e.job == job and e.kind == "done")
    assert end.note == "stopped" and end.text
    time.sleep(0.5)
    assert fakeduck.STATE["aborted"] >= 1                   # the page's request ended too
    fakeduck.STATE["mode"] = "ok"
    _, end = r.ask("next", GPT5)
    assert end.kind == "done"


def test_throttle_retry_then_error(r):
    fakeduck.STATE["mode"] = "429once"
    job, end = r.ask("once", GPT5, fresh=True)
    assert end.kind == "done" and any(e.kind == "status" and "throttling" in e.text
                                      for e in r.events if e.job == job)
    fakeduck.STATE["mode"] = "429"
    _, end = r.ask("twice", GPT5)
    assert end.kind == "error" and "rate limit" in end.text


def test_silence_is_an_error_not_an_empty_answer(r):
    fakeduck.STATE["mode"] = "hang"
    _, end = r.ask("hello?", GPT5, fresh=True)
    assert end.kind == "error" and "no end" in end.text


def _our_firefoxes() -> list[int]:
    """Firefox processes started by this test run — never the user's own browser."""
    parent = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                parent[int(d.name)] = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                pass
    mine, todo = set(), [os.getpid()]
    while todo:
        pid = todo.pop()
        kids = [c for c, pp in parent.items() if pp == pid and c not in mine]
        mine.update(kids)
        todo += kids
    out = []
    for pid in mine:
        try:
            if b"firefox" in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]:
                out.append(pid)
        except OSError:
            pass
    return out


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
def test_dead_firefox_restarts_and_gets_the_context(r):
    r.ask("before", CLAUDE, fresh=True)
    victims = _our_firefoxes()
    assert victims
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)
    _, end = r.ask("after", CLAUDE, primed="Context: AFTER")
    assert end.kind == "done"
    assert last()["messages"][-1]["content"] == "Context: AFTER"
    assert last()["model"] == "claude-haiku-4-5"


def test_switch_that_does_not_take_is_reported(r):
    fakeduck.STATE["page"] = "stuck"
    job, end = r.ask("hi", CLAUDE, fresh=True)
    assert end.kind == "done"
    assert any("didn't take" in e.text for e in r.events if e.kind == "warn")
    assert [e.text for e in r.events if e.job == job and e.kind == "model"] == ["gpt-5-mini"]


def test_missing_send_button_is_named(r):
    fakeduck.STATE["page"] = "nosend"
    _, end = r.ask("hi", GPT5, fresh=True)
    assert end.kind == "error" and "Send button" in end.text


def test_doctor(tmp_path, capsys):
    assert duck.doctor(send=True, out=tmp_path) == 0
    for name in ("report.txt", "page.png", "buttons.txt", "picker.txt", "stream.txt"):
        assert (tmp_path / name).exists(), name
    assert "[BAD]" not in (tmp_path / "report.txt").read_text()


def test_idle_firefox_goes_to_sleep_and_comes_back():
    events = []
    d = duck.Duck(events.append, idle=2)
    try:
        job = d.chat("before", CLAUDE, fresh=True)
        while not any(e.job == job and e.kind == "done" for e in events):
            time.sleep(0.05)
        t = time.time()
        while not any(e.kind == "state" and e.text == "asleep" for e in events):
            time.sleep(0.1)
            assert time.time() - t < 8
        assert not _our_firefoxes()                        # the memory is really back
        job = d.chat("after", CLAUDE, fresh=False, primed="Context: AWAKE")
        while not any(e.job == job and e.kind in ("done", "error") for e in events):
            time.sleep(0.05)
            assert time.time() - t < 60
        assert last()["messages"] == [{"role": "user", "content": "Context: AWAKE"}]
        assert last()["model"] == "claude-haiku-4-5"
    finally:
        d.close()
