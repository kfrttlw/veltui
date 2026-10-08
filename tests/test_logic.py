"""The parts without a screen or a browser: models, files, the store, the primer."""

import os
import sqlite3

import pytest

from veltui import files
from veltui.duck import explain, primer
from veltui.models import MODELS, find_model
from veltui.store import Message, Store, title_from


def test_find_model():
    assert find_model("2").id == "gpt-4o-mini"
    assert find_model("claude").id == "claude-haiku-4-5"
    assert find_model("Mistral Small 4").id == "mistral-small-2603"
    assert find_model("llama-4") is None or find_model("llama-4").id.startswith("meta-llama")
    assert find_model("gpt") is None            # ambiguous
    assert find_model("7") is None
    assert len({m.id for m in MODELS}) == len(MODELS)


def test_primer_keeps_order_and_ends_with_the_new_message():
    text, n = primer([("user", "q1"), ("assistant", "a1")], "q2")
    assert n == 2
    assert text.index("q1") < text.index("a1") < text.index("[new message]\nq2")
    assert text.endswith("q2")


def test_primer_drops_the_oldest_when_too_long():
    history = [("user", "x" * 5000), ("assistant", "y" * 5000)] * 6
    text, n = primer(history, "now")
    assert n < len(history)
    assert "first" in text and "left out" in text
    assert len(text) < 30_000


def test_explain():
    assert "rate limit" in explain("429", "")
    assert "rate limit" in explain("418", '{"type":"ERR_BN_LIMIT"}')
    assert "anti-bot" in explain("418", "ERR_CHALLENGE")
    assert explain("500", "") == "DuckDuckGo error 500"


def test_paths_with_spaces(tmp_path):
    (tmp_path / "my dir").mkdir()
    f = tmp_path / "my dir" / "notes file.py"
    f.write_text("print(1)\n")
    assert files.split_path_arg(f"{f} explain it") == (str(f), "explain it")
    assert files.split_path_arg(f'"{f}" why') == (str(f), "why")
    assert files.clean_path(f"file://{str(f).replace(' ', '%20')}") == str(f)
    labels = [c[1] for c in files.completions(f"{tmp_path}/my")]
    assert labels == ["my dir/"]
    labels = [c[1] for c in files.completions(f"{tmp_path}/my dir/")]
    assert labels == ["notes file.py"]
    assert files.completions(f"{f} what") == []        # a question has begun


def test_read_refuses_what_it_cant_send(tmp_path):
    (tmp_path / "bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "big.txt").write_text("x" * (files.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="binary"):
        files.read(str(tmp_path / "bin"))
    with pytest.raises(ValueError, match="capped"):
        files.read(str(tmp_path / "big.txt"))
    with pytest.raises(IsADirectoryError):
        files.read(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        files.read(str(tmp_path / "nope"))


def test_fence_survives_backticks(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("```\ncode\n```\n")
    block = files.read(str(f)).block()
    assert block.startswith("doc.md:\n````markdown\n")
    assert block.endswith("\n````")


def test_store_writes_nothing_until_asked(tmp_path):
    path = tmp_path / "v" / "veltui.db"
    s = Store(path)
    assert s.chats() == [] and s.get("model") == ""
    assert not path.exists()
    s.put("model", "gpt-4o-mini")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_store_round_trip(tmp_path):
    s = Store(tmp_path / "veltui.db")
    msgs = [Message("user", "a.py:\n```\nx\n```\n\nwhat", shown="what", files=("a.py",)),
            Message("assistant", "an answer", model="claude-haiku-4-5", note="stopped")]
    cid = s.save_chat(None, "first", "claude-haiku-4-5", msgs)
    row, back = s.load(cid)
    assert row.title == "first" and row.count == 2
    assert [(m.role, m.content, m.shown, m.files, m.model, m.note) for m in back] == \
        [(m.role, m.content, m.shown, m.files, m.model, m.note) for m in msgs]
    s.save_chat(cid, "first", "claude-haiku-4-5", msgs + [Message("user", "more")])
    assert s.chats()[0].count == 3
    s.rename(cid, "renamed")
    s.delete(cid)
    assert s.chats() == []
    assert s._con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_migrates_veltui_0_1(tmp_path, monkeypatch):
    legacy = tmp_path / "old.sqlite"
    con = sqlite3.connect(legacy)
    con.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, model TEXT NOT NULL,
                               created_at TEXT, updated_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, role TEXT,
                               content TEXT);
        INSERT INTO settings VALUES ('model', 'gpt-4o-mini'), ('theme', 'navy');
        INSERT INTO sessions VALUES (1, NULL, 'gpt-5-mini', '2026-01-02 03:04:05',
                                     '2026-01-02 03:04:05');
        INSERT INTO messages VALUES (1, 1, 'user', 'how do pipes work'),
                                    (2, 1, 'assistant', 'like this');
    """)
    con.commit()
    con.close()
    monkeypatch.setattr("veltui.store.LEGACY_DB", legacy)
    monkeypatch.setattr("veltui.store.data_dir", lambda: tmp_path / "new")
    s = Store()
    assert s.get("model") == "gpt-4o-mini"
    (row,) = s.chats()
    assert row.title == "how do pipes work" and row.count == 2
    assert legacy.exists()                      # left in place, never deleted


def test_title_from():
    assert title_from([Message("user", "x" * 80)]).endswith("…")
    assert title_from([Message("user", "a.py:\n```\n```", files=("a.py",))]) == "a.py"


@pytest.mark.skipif(os.name == "nt", reason="posix permissions")
def test_store_dir_is_private(tmp_path):
    s = Store(tmp_path / "d" / "veltui.db")
    s.put("k", "v")
    assert (tmp_path / "d").stat().st_mode & 0o077 == 0
