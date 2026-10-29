from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'complete',
    created_at TEXT NOT NULL, day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,id);
CREATE INDEX IF NOT EXISTS messages_day ON messages(day,id);
CREATE TABLE IF NOT EXISTS summaries (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    content TEXT NOT NULL, through_id INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, content TEXT NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
    part INTEGER NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
    source_ids TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(day,part)
);
CREATE TABLE IF NOT EXISTS journal_sources (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    journal_id INTEGER NOT NULL REFERENCES journals(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS forgotten_sources (message_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS proactive_messages (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    trigger_user_id INTEGER NOT NULL REFERENCES messages(id),
    day TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS proactive_trigger ON proactive_messages(trigger_user_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "companion.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise ValueError("数据来自较新版本，请升级应用后打开")
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute("UPDATE messages SET status='interrupted' WHERE status='pending'")

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def execute(self, sql: str, args=()) -> int:
        with self.lock, self.db:
            cursor = self.db.execute(sql, args)
            return cursor.lastrowid

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta(self, key: str, default: str = "") -> str:
        rows = self.rows("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def new_session(self, title: str = "新的海边来信") -> str:
        identity = uuid.uuid4().hex
        self.execute("INSERT INTO sessions VALUES (?,?,?)", (identity, title, now()))
        return identity

    def sessions(self) -> list[dict]:
        return self.rows("SELECT s.*, MAX(m.id) AS latest FROM sessions s LEFT JOIN messages m ON m.session_id=s.id GROUP BY s.id ORDER BY COALESCE(MAX(m.id),0) DESC,s.created_at DESC")

    def add_message(self, session: str, role: str, content: str, status="complete", timestamp=None) -> int:
        stamp = timestamp or now()
        return self.execute("INSERT INTO messages(session_id,role,content,status,created_at,day) VALUES (?,?,?,?,?,?)", (session, role, content, status, stamp, stamp[:10]))

    def finish_message(self, identity: int, content: str, status="complete") -> None:
        self.execute("UPDATE messages SET content=?,status=? WHERE id=?", (content, status, identity))

    def messages(self, session: str, complete_only=False) -> list[dict]:
        clause = " AND m.status='complete'" if complete_only else ""
        return self.rows("SELECT m.*,p.message_id IS NOT NULL AS proactive FROM messages m LEFT JOIN proactive_messages p ON p.message_id=m.id WHERE m.session_id=?" + clause + " ORDER BY m.id", (session,))

    def latest_user(self) -> dict | None:
        rows = self.rows("SELECT * FROM messages WHERE role='user' AND status='complete' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def add_proactive(self, user: dict, content: str, timestamp: str) -> int:
        with self.lock, self.db:
            current = self.latest_user()
            if not current or current["id"] != user["id"]:
                raise ValueError("用户已开始新的交流，取消这封主动来信")
            if self.db.execute("SELECT 1 FROM proactive_messages WHERE trigger_user_id=?", (user["id"],)).fetchone():
                raise ValueError("这段等待期间已经主动联系过")
            cursor = self.db.execute("INSERT INTO messages(session_id,role,content,status,created_at,day) VALUES (?,'assistant',?,'complete',?,?)", (user["session_id"], content, timestamp, timestamp[:10]))
            identity = cursor.lastrowid
            self.db.execute("INSERT INTO proactive_messages VALUES (?,?,?,?)", (identity, user["id"], timestamp[:10], timestamp))
            return identity

    def remember_preference(self, slot: str, value: str, content: str, source_id: int) -> None:
        with self.lock, self.db:
            source = self.db.execute("SELECT role,status FROM messages WHERE id=?", (source_id,)).fetchone()
            if not source or source["role"] != "user" or source["status"] != "complete":
                return
            if self.db.execute("SELECT 1 FROM forgotten_sources WHERE message_id=?", (source_id,)).fetchone():
                return
            mapping = json.loads(self.meta("interaction_preferences", "{}"))
            previous = mapping.get(slot, {})
            row = self.db.execute("SELECT id FROM memories WHERE id=? AND kind='相处偏好'", (previous.get("memory_id", -1),)).fetchone()
            stamp = now()
            if row:
                identity = row["id"]
                self.db.execute("UPDATE memories SET content=?,source_ids=?,updated_at=? WHERE id=?", (content, json.dumps([source_id]), stamp, identity))
            else:
                cursor = self.db.execute("INSERT INTO memories(kind,content,source_ids,created_at,updated_at) VALUES ('相处偏好',?,?,?,?)", (content, json.dumps([source_id]), stamp, stamp))
                identity = cursor.lastrowid
            mapping[slot] = {"value": value, "memory_id": identity}
            self.db.execute("INSERT INTO meta VALUES ('interaction_preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(mapping, ensure_ascii=False),))

    def message_sources(self, identities: list[int]) -> list[dict]:
        if not identities:
            return []
        return self.rows("SELECT * FROM messages WHERE id IN (" + ",".join("?" for _ in identities) + ") ORDER BY id", identities)

    def summary(self, session: str) -> dict:
        rows = self.rows("SELECT * FROM summaries WHERE session_id=?", (session,))
        return rows[0] if rows else {"content": "", "through_id": 0}

    def set_summary(self, session: str, content: str, through: int) -> None:
        self.execute("INSERT INTO summaries VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET content=excluded.content,through_id=excluded.through_id,updated_at=excluded.updated_at", (session, content, through, now()))

    def memories(self) -> list[dict]:
        return self.rows("SELECT * FROM memories ORDER BY updated_at DESC,id DESC")

    def add_memory(self, kind: str, content: str, sources: list[int] | None = None) -> int | None:
        content = content.strip()
        if not content:
            return None
        with self.lock:
            if self.rows("SELECT id FROM memories WHERE content=?", (content,)):
                return None
            blocked = {row["message_id"] for row in self.rows("SELECT message_id FROM forgotten_sources")}
            if sources and blocked.intersection(sources):
                return None
            return self.execute("INSERT INTO memories(kind,content,source_ids,created_at,updated_at) VALUES (?,?,?,?,?)", (kind, content, json.dumps(sources or []), now(), now()))

    def update_memory(self, identity: int, kind: str, content: str) -> None:
        if not content.strip():
            raise ValueError("记忆内容不能为空")
        with self.lock, self.db:
            rows = self.rows("SELECT source_ids FROM memories WHERE id=?", (identity,))
            if rows:
                for source in json.loads(rows[0]["source_ids"]):
                    self.db.execute("INSERT OR IGNORE INTO forgotten_sources VALUES (?)", (source,))
            self.db.execute("UPDATE memories SET kind=?,content=?,source_ids='[]',updated_at=? WHERE id=?", (kind, content.strip(), now(), identity))
            # Summaries are derived data and may contain the superseded fact.
            self.db.execute("DELETE FROM summaries")

    def forget_memory(self, identity: int, erase_sources: bool = False) -> None:
        with self.lock, self.db:
            rows = self.rows("SELECT source_ids FROM memories WHERE id=?", (identity,))
            if not rows:
                return
            ids = json.loads(rows[0]["source_ids"])
            for source in ids:
                self.db.execute("INSERT OR IGNORE INTO forgotten_sources VALUES (?)", (source,))
                if erase_sources:
                    related = self.rows("SELECT journal_id FROM journal_sources WHERE message_id=?", (source,))
                    for journal in related:
                        self.db.execute("DELETE FROM journals WHERE id=?", (journal["journal_id"],))
                    # Remove the whole exchange, including an assistant's echo of private data.
                    message = self.rows("SELECT session_id FROM messages WHERE id=?", (source,))
                    if message:
                        next_user = self.rows("SELECT MIN(id) AS id FROM messages WHERE session_id=? AND role='user' AND id>?", (message[0]["session_id"], source))
                        end = next_user[0]["id"] or 9_223_372_036_854_775_807
                        exchange = self.rows("SELECT id FROM messages WHERE session_id=? AND id>=? AND id<?", (message[0]["session_id"], source, end))
                        for item in exchange:
                            self.db.execute("INSERT OR IGNORE INTO forgotten_sources VALUES (?)", (item["id"],))
                            for journal in self.rows("SELECT journal_id FROM journal_sources WHERE message_id=?", (item["id"],)):
                                self.db.execute("DELETE FROM journals WHERE id=?", (journal["journal_id"],))
                            self.db.execute("UPDATE messages SET content='[此消息已按用户要求删除]',status='erased' WHERE id=?", (item["id"],))
            self.db.execute("DELETE FROM memories WHERE id=?", (identity,))
            if erase_sources and ids:
                # Other derived memories can quote the same source exchange.
                blocked = {r["message_id"] for r in self.rows("SELECT message_id FROM forgotten_sources")}
                for memory in self.memories():
                    if blocked.intersection(json.loads(memory["source_ids"])):
                        self.db.execute("DELETE FROM memories WHERE id=?", (memory["id"],))
            self.db.execute("DELETE FROM summaries")

    def unjournaled(self) -> list[dict]:
        return self.rows("SELECT m.*,p.message_id IS NOT NULL AS proactive FROM messages m LEFT JOIN journal_sources s ON s.message_id=m.id LEFT JOIN proactive_messages p ON p.message_id=m.id WHERE s.message_id IS NULL AND m.status='complete' ORDER BY m.day,m.id")

    def add_journal(self, day: str, title: str, content: str, sources: list[int], facts: list[dict]) -> int:
        with self.lock, self.db:
            part = self.db.execute("SELECT COALESCE(MAX(part),0)+1 FROM journals WHERE day=?", (day,)).fetchone()[0]
            cursor = self.db.execute("INSERT INTO journals(day,part,title,content,source_ids,created_at) VALUES (?,?,?,?,?,?)", (day, part, title, content, json.dumps(sources), now()))
            identity = cursor.lastrowid
            for source in sources:
                self.db.execute("INSERT INTO journal_sources VALUES (?,?)", (source, identity))
            blocked = {r["message_id"] for r in self.rows("SELECT message_id FROM forgotten_sources")}
            for fact in facts:
                content = fact["content"].strip()
                if content and not blocked.intersection(fact["source_ids"]) and not self.db.execute("SELECT id FROM memories WHERE content=?", (content,)).fetchone():
                    self.db.execute("INSERT INTO memories(kind,content,source_ids,created_at,updated_at) VALUES (?,?,?,?,?)", (fact["kind"], content, json.dumps(fact["source_ids"]), now(), now()))
            return identity

    def journals(self, day="") -> list[dict]:
        if day:
            return self.rows("SELECT * FROM journals WHERE day=? ORDER BY part", (day,))
        return self.rows("SELECT * FROM journals ORDER BY day DESC,part DESC")

    def search(self, query: str, limit=6) -> list[dict]:
        terms = set(re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", query.lower()))
        # Character bigrams let Chinese queries match without an external tokenizer.
        for term in list(terms):
            if re.search(r"[\u4e00-\u9fff]", term):
                terms.update(term[i:i+2] for i in range(len(term)-1))
        dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", query))
        scored = []
        for journal in self.journals():
            haystack = (journal["title"] + " " + journal["content"]).lower()
            score = sum(min(len(term), 8) for term in terms if term in haystack)
            if journal["day"] in dates:
                score += 100
            if score:
                scored.append((score, journal["id"], journal))
        return [row[2] for row in sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)[:limit]]

    def snapshot(self, destination: Path) -> None:
        with self.lock, closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)

    def close(self) -> None:
        with self.lock:
            self.db.close()
