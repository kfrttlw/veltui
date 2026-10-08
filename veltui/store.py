"""Saved chats and the model preference, in SQLite.

Nothing about a conversation touches the disk until you save it. The file is
created on the first write, readable by you only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "veltui"


LEGACY_DB = Path.home() / ".veltui" / "db.sqlite"     # veltui 0.1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title   TEXT NOT NULL,
    model   TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    shown   TEXT NOT NULL DEFAULT '',
    files   TEXT NOT NULL DEFAULT '',
    model   TEXT NOT NULL DEFAULT '',
    note    TEXT NOT NULL DEFAULT '',
    at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages(chat_id, id);
"""


@dataclass
class Message:
    role: str                         # "user" or "assistant"
    content: str                      # exactly what was sent / received
    shown: str = ""                   # user turn: what the log shows ("" = content)
    files: tuple[str, ...] = ()       # user turn: attached file names
    model: str = ""                   # assistant turn: the model duck.ai really used
    note: str = ""                    # "stopped", "cut off", …
    at: float = field(default_factory=time.time)


@dataclass
class ChatRow:
    id: int
    title: str
    model: str
    updated: float
    count: int


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "veltui.db"
        self._con: sqlite3.Connection | None = None
        if self.path.exists():
            self._open()
        elif path is None and LEGACY_DB.exists():
            self._migrate(LEGACY_DB)

    # --- connection ---

    def _open(self) -> sqlite3.Connection:
        if self._con is None:
            fresh = not self.path.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            if fresh:     # create it 0600 before sqlite writes a single byte
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
            self._con = sqlite3.connect(self.path)
            self._con.execute("PRAGMA foreign_keys = ON")
            self._con.executescript(_SCHEMA)
        return self._con

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    # --- settings ---

    def get(self, key: str, default: str = "") -> str:
        if self._con is None:
            return default
        row = self._con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str):
        con = self._open()
        with con:
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                        (key, value))

    # --- chats ---

    def save_chat(self, chat_id: int | None, title: str, model: str,
                  messages: list[Message]) -> int:
        """Write a whole chat (insert or overwrite) and return its id."""
        con = self._open()
        now = time.time()
        with con:
            if chat_id is not None and con.execute(
                    "SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                con.execute("UPDATE chats SET title=?, model=?, updated=? WHERE id=?",
                            (title, model, now, chat_id))
                con.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            else:
                chat_id = con.execute(
                    "INSERT INTO chats(title, model, created, updated) VALUES(?, ?, ?, ?)",
                    (title, model, now, now)).lastrowid
            con.executemany(
                "INSERT INTO messages(chat_id, role, content, shown, files, model, note, at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [(chat_id, m.role, m.content, m.shown, "\n".join(m.files), m.model, m.note,
                  m.at) for m in messages])
        return chat_id

    def chats(self) -> list[ChatRow]:
        if self._con is None:
            return []
        rows = self._con.execute(
            "SELECT c.id, c.title, c.model, c.updated, COUNT(m.id) FROM chats c"
            " LEFT JOIN messages m ON m.chat_id = c.id"
            " GROUP BY c.id ORDER BY c.updated DESC").fetchall()
        return [ChatRow(*r) for r in rows]

    def load(self, chat_id: int) -> tuple[ChatRow, list[Message]] | None:
        row = next((c for c in self.chats() if c.id == chat_id), None)
        if row is None:
            return None
        msgs = [Message(role, content, shown, tuple(f for f in files.split("\n") if f),
                        model, note, at)
                for role, content, shown, files, model, note, at in self._con.execute(
                    "SELECT role, content, shown, files, model, note, at FROM messages"
                    " WHERE chat_id=? ORDER BY id", (chat_id,))]
        return row, msgs

    def rename(self, chat_id: int, title: str):
        with self._open() as con:
            con.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))

    def delete(self, chat_id: int):
        with self._open() as con:
            con.execute("DELETE FROM chats WHERE id=?", (chat_id,))

    def delete_all(self) -> int:
        if self._con is None:
            return 0
        with self._con as con:
            n = con.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            con.execute("DELETE FROM chats")
        return n

    # --- veltui 0.1 ---

    def _migrate(self, legacy: Path):
        """Copy chats and the model choice over from ~/.veltui (left in place)."""
        try:
            old = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
            sessions = old.execute(
                "SELECT id, name, model, updated_at FROM sessions ORDER BY id").fetchall()
            model = old.execute("SELECT value FROM settings WHERE key='model'").fetchone()
            chats = []
            for sid, name, smodel, updated in sessions:
                rows = old.execute("SELECT role, content FROM messages WHERE session_id=?"
                                   " ORDER BY id", (sid,)).fetchall()
                chats.append((name, smodel, updated, rows))
            old.close()
        except sqlite3.Error:
            return
        if not chats and not model:
            return
        if model:
            self.put("model", model[0])
        for name, smodel, updated, rows in chats:
            msgs = [Message(role, content) for role, content in rows]
            title = name or title_from(msgs) or "untitled"
            cid = self.save_chat(None, title, smodel, msgs)
            ts = _sqlite_time(updated)
            if ts:
                with self._con as con:
                    con.execute("UPDATE chats SET created=?, updated=? WHERE id=?",
                                (ts, ts, cid))


def title_from(messages: list[Message]) -> str:
    """A chat's default title: the start of its first question."""
    for m in messages:
        if m.role == "user":
            text = " ".join(m.shown.split()) if (m.shown or m.files) else " ".join(
                m.content.split())
            text = text or ", ".join(m.files)
            return text[:48] + ("…" if len(text) > 48 else "")
    return ""


def _sqlite_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        import calendar
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return None
